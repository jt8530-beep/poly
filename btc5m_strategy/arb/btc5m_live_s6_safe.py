"""BTC5M S6 safe runner for Polymarket BTC Up/Down 5m markets.

Purpose
-------
S6 is a safety-first live/dry-run runner. It keeps the S5 core idea, but adds:

1. fair probability shrinkage
2. shock / volatility-spike filter
3. overextended trend skip
4. effective edge after spread/depth/time/model-error penalties
5. order-book snapshots
6. dynamic exit with bid-depth awareness
7. liquidity-trapped accounting
8. Binance-proxy settlement audit

Default mode is dry-run. Real order submission is only attempted when:

    BTC5M_LIVE_ENABLED=true
    BTC5M_LIVE_DRY_RUN=false

This file intentionally reuses the existing live-small order submission helpers,
so credentials and CLOB behavior stay in one place.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import time
from datetime import datetime, timezone

from . import config as _config
from .btc5m_paper import (
    BinancePrice,
    BtcMarket,
    _book_by_token,
    _candidate,
    _direction_bps,
    _env_b,
    _env_f,
    _env_i,
    _env_s,
    _fair_probability,
    _load_current_market,
    _open_db,
    _populate_market_book,
    _trend_gate_decision,
)
from .btc5m_live_small import _order_response_ok, _submit_market
from .ladder import _top_of_book
from .poly_client import PolyClient
from .s6_safety import calibrated_fair, dynamic_exit_reason, effective_edge_bps, shock_filter_reason
from .tg_notifier import Notifier


S6_SCHEMA = """
CREATE TABLE IF NOT EXISTS btc5m_s6_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT,
  side TEXT,
  token_id TEXT,
  action TEXT,
  reason TEXT,
  edge_bps INTEGER,
  effective_edge_bps INTEGER,
  direction_bps REAL,
  seconds_after_start REAL,
  seconds_left REAL,
  entry_price REAL,
  notional_usd REAL,
  fair_raw REAL,
  fair_calibrated REAL,
  response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_s6_attempts_market
  ON btc5m_s6_attempts(market_slug, ts_utc);

CREATE TABLE IF NOT EXISTS btc5m_s6_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_slug TEXT NOT NULL,
  side TEXT NOT NULL,
  token_id TEXT NOT NULL,
  open_ts_utc TEXT NOT NULL,
  close_ts_utc TEXT,
  status TEXT NOT NULL,
  entry_price REAL NOT NULL,
  size_shares REAL NOT NULL,
  notional_usd REAL NOT NULL,
  edge_bps INTEGER,
  effective_edge_bps INTEGER,
  direction_bps REAL,
  start_epoch INTEGER,
  end_epoch INTEGER,
  start_price REAL,
  entry_spot REAL,
  last_mark_price REAL,
  last_mark_ts_utc TEXT,
  pnl_usd REAL DEFAULT 0,
  close_price REAL,
  close_reason TEXT,
  open_response_json TEXT,
  close_response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_s6_positions_status
  ON btc5m_s6_positions(status);

CREATE INDEX IF NOT EXISTS idx_btc5m_s6_positions_market
  ON btc5m_s6_positions(market_slug);

CREATE TABLE IF NOT EXISTS btc5m_s6_orderbook_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT NOT NULL,
  side TEXT NOT NULL,
  token_id TEXT,
  seconds_after_start REAL,
  seconds_left REAL,
  bid REAL,
  bid_size REAL,
  ask REAL,
  ask_size REAL,
  spread_pct REAL,
  mid REAL,
  fair_prob REAL,
  edge_bps INTEGER,
  effective_edge_bps INTEGER,
  raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_s6_orderbook_market
  ON btc5m_s6_orderbook_snapshots(market_slug, ts_utc);

CREATE TABLE IF NOT EXISTS btc5m_s6_settlement_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT NOT NULL,
  position_id INTEGER,
  start_epoch INTEGER,
  end_epoch INTEGER,
  start_price_binance REAL,
  close_price_binance REAL,
  proxy_winner TEXT,
  side TEXT,
  proxy_matched INTEGER,
  boundary_bps REAL,
  raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_s6_settlement_market
  ON btc5m_s6_settlement_audit(market_slug, ts_utc);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_s6_db(path: str) -> sqlite3.Connection:
    db = _open_db(path)
    db.executescript(S6_SCHEMA)
    db.commit()
    return db


def _json(obj) -> str:
    return json.dumps(obj or {}, sort_keys=True, default=str)


def _record_attempt(db: sqlite3.Connection, **v) -> None:
    db.execute(
        """
        INSERT INTO btc5m_s6_attempts
        (ts_utc, market_slug, side, token_id, action, reason, edge_bps, effective_edge_bps,
         direction_bps, seconds_after_start, seconds_left, entry_price, notional_usd,
         fair_raw, fair_calibrated, response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now_iso(),
            v.get("market_slug"),
            v.get("side"),
            v.get("token_id"),
            v.get("action"),
            v.get("reason"),
            v.get("edge_bps"),
            v.get("effective_edge_bps"),
            v.get("direction_bps"),
            v.get("seconds_after_start"),
            v.get("seconds_left"),
            v.get("entry_price"),
            v.get("notional_usd"),
            v.get("fair_raw"),
            v.get("fair_calibrated"),
            _json(v.get("response")),
        ),
    )
    db.commit()


def _snapshot_side(market: BtcMarket, side: str, fair_prob: float | None, effective_edge: int | None = None) -> dict:
    if side == "UP":
        token_id = market.token_up
        bid = market.up_bid
        bid_size = market.up_bid_size
        ask = market.up_ask
        ask_size = market.up_ask_size
    else:
        token_id = market.token_down
        bid = market.down_bid
        bid_size = market.down_bid_size
        ask = market.down_ask
        ask_size = market.down_ask_size

    mid = None
    spread_pct = None
    edge_bps = None
    if bid is not None and ask is not None:
        bid_f = float(bid)
        ask_f = float(ask)
        mid = (bid_f + ask_f) / 2.0
        if ask_f > 0:
            spread_pct = (ask_f - bid_f) / ask_f
            if fair_prob is not None:
                edge_bps = int((float(fair_prob) - ask_f) * 10_000)

    return {
        "side": side,
        "token_id": token_id,
        "bid": bid,
        "bid_size": bid_size,
        "ask": ask,
        "ask_size": ask_size,
        "spread_pct": spread_pct,
        "mid": mid,
        "fair_prob": fair_prob,
        "edge_bps": edge_bps,
        "effective_edge_bps": effective_edge,
    }


def _record_orderbook_snapshot(
    db: sqlite3.Connection,
    market: BtcMarket,
    seconds_after_start: float,
    seconds_left: float,
    fair_up: float | None,
    fair_down: float | None,
    best_side: str | None = None,
    best_effective_edge_bps: int | None = None,
    extra: dict | None = None,
) -> None:
    rows = [
        _snapshot_side(market, "UP", fair_up, best_effective_edge_bps if best_side == "UP" else None),
        _snapshot_side(market, "DOWN", fair_down, best_effective_edge_bps if best_side == "DOWN" else None),
    ]
    ts = _now_iso()
    for row in rows:
        raw = {
            "market_id": market.market_id,
            "question": market.question,
            "best_side": best_side,
            "extra": extra or {},
        }
        db.execute(
            """
            INSERT INTO btc5m_s6_orderbook_snapshots
            (ts_utc, market_slug, side, token_id, seconds_after_start, seconds_left,
             bid, bid_size, ask, ask_size, spread_pct, mid, fair_prob, edge_bps,
             effective_edge_bps, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts,
                market.slug,
                row["side"],
                row["token_id"],
                seconds_after_start,
                seconds_left,
                row["bid"],
                row["bid_size"],
                row["ask"],
                row["ask_size"],
                row["spread_pct"],
                row["mid"],
                row["fair_prob"],
                row["edge_bps"],
                row["effective_edge_bps"],
                _json(raw),
            ),
        )
    db.commit()


def _open_position_count(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) FROM btc5m_s6_positions WHERE status='OPEN'").fetchone()
    return int(row[0] or 0)


def _market_seen(db: sqlite3.Connection, market_slug: str) -> bool:
    row = db.execute(
        """
        SELECT 1 FROM btc5m_s6_attempts
        WHERE market_slug=? AND action IN ('dry_run_order','live_order','live_order_failed')
        LIMIT 1
        """,
        (market_slug,),
    ).fetchone()
    if row is not None:
        return True
    row = db.execute("SELECT 1 FROM btc5m_s6_positions WHERE market_slug=? LIMIT 1", (market_slug,)).fetchone()
    return row is not None


def _opened_with_live_order(row: sqlite3.Row) -> bool:
    try:
        payload = json.loads(row["open_response_json"] or "{}")
    except Exception:
        return False
    return bool(payload.get("live_enabled")) and not bool(payload.get("dry_run"))


def _record_settlement_audit(db: sqlite3.Connection, row: sqlite3.Row, close_spot: float, winner: str) -> None:
    start_price = float(row["start_price"] or 0.0)
    boundary_bps = abs(close_spot / start_price - 1.0) * 10_000 if start_price > 0 else None
    side = str(row["side"])
    db.execute(
        """
        INSERT INTO btc5m_s6_settlement_audit
        (ts_utc, market_slug, position_id, start_epoch, end_epoch, start_price_binance,
         close_price_binance, proxy_winner, side, proxy_matched, boundary_bps, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now_iso(),
            row["market_slug"],
            row["id"],
            row["start_epoch"],
            row["end_epoch"],
            row["start_price"],
            close_spot,
            winner,
            side,
            1 if side == winner else 0,
            boundary_bps,
            _json({"source": "binance_proxy", "winner": winner, "close_spot": close_spot}),
        ),
    )
    db.commit()


def _submit_exit_if_live(row: sqlite3.Row, token_id: str, shares: float, target_price: float, payload: dict) -> tuple[bool, dict]:
    live_enabled = _env_b("BTC5M_LIVE_ENABLED", False)
    dry_run = _env_b("BTC5M_LIVE_DRY_RUN", True)
    payload.update(
        {
            "live_enabled": live_enabled,
            "dry_run": dry_run,
            "opened_with_live_order": _opened_with_live_order(row),
            "token_id": token_id,
            "shares": shares,
            "target_price": target_price,
        }
    )
    if _opened_with_live_order(row) and live_enabled and not dry_run:
        response = _submit_market(token_id, shares, "SELL", target_price)
        payload["order_response"] = response
        return _order_response_ok(response), payload
    return True, payload


def _mark_positions(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice) -> dict:
    rows = list(db.execute("SELECT * FROM btc5m_s6_positions WHERE status='OPEN' ORDER BY id"))
    if not rows:
        return {"marked": 0, "closed": 0, "open_positions": 0, "liquidity_trapped": 0}

    books = _book_by_token(poly, [str(row["token_id"]) for row in rows])
    now_ts = time.time()
    marked = 0
    closed = 0
    trapped = 0
    settle_delay = _env_i("BTC5M_SETTLE_DELAY_SEC", 8)

    for row in rows:
        token_id = str(row["token_id"])
        bid, bid_size, ask, ask_size = _top_of_book(books.get(token_id, {}))
        mark = float(bid) if bid is not None else row["last_mark_price"]
        if mark is None:
            continue

        entry = float(row["entry_price"])
        size = float(row["size_shares"])
        notional = float(row["notional_usd"])
        seconds_left = float(row["end_epoch"]) - now_ts
        pnl = (float(mark) - entry) * size

        close_reason = None
        close_price = None
        close_payload: dict | None = None

        if now_ts >= float(row["end_epoch"]) + settle_delay:
            close_spot = price.window_close(int(row["start_epoch"]))
            winner = "UP" if close_spot >= float(row["start_price"]) else "DOWN"
            close_price = 1.0 if str(row["side"]) == winner else 0.0
            pnl = (close_price - entry) * size
            close_reason = "settled_binance_proxy"
            close_payload = {"winner": winner, "close_spot": close_spot}
            _record_settlement_audit(db, row, close_spot, winner)
        elif _env_b("BTC5M_DYNAMIC_EXIT_ENABLED", True):
            reason, payload = dynamic_exit_reason(
                seconds_left,
                pnl,
                notional,
                bid_size,
                min_exit_bid_depth=_env_f("BTC5M_EXIT_MIN_BID_DEPTH_SHARES", 25.0),
                exit1_sec=_env_i("BTC5M_DYNAMIC_EXIT1_SEC", 120),
                exit1_min_loss=_env_f("BTC5M_DYNAMIC_EXIT1_MIN_LOSS", 0.15),
                exit2_sec=_env_i("BTC5M_DYNAMIC_EXIT2_SEC", 90),
                exit3_sec=_env_i("BTC5M_DYNAMIC_EXIT3_SEC", 60),
            )
            if reason == "liquidity_trapped":
                trapped += 1
                close_payload = payload
            elif reason:
                close_reason = reason
                close_price = float(mark)
                close_payload = payload
        elif _env_i("BTC5M_TAIL_FORCE_EXIT_SEC", 0) > 0 and seconds_left <= _env_i("BTC5M_TAIL_FORCE_EXIT_SEC", 0):
            close_reason = "tail_force_exit"
            close_price = float(mark)
            close_payload = {"seconds_left": seconds_left, "bid_size": bid_size}

        if close_reason is None and _env_b("BTC5M_STOP_LOSS_ENABLED", True) and notional > 0:
            stop_loss = _env_f("BTC5M_STOP_LOSS", 0.30)
            if pnl <= -notional * stop_loss:
                if bid_size is not None and float(bid_size) >= _env_f("BTC5M_EXIT_MIN_BID_DEPTH_SHARES", 25.0):
                    close_reason = "stop_loss"
                    close_price = float(mark)
                    close_payload = {"seconds_left": seconds_left, "bid_size": bid_size}
                else:
                    trapped += 1
                    close_payload = {
                        "reason": "stop_loss_liquidity_trapped",
                        "seconds_left": seconds_left,
                        "bid_size": bid_size,
                    }

        if close_reason and close_price is not None:
            ok, close_payload = _submit_exit_if_live(row, token_id, size, float(close_price), close_payload or {})
            if not ok:
                logging.getLogger("btc5m-live-s6-safe").warning("exit order failed for position %s: %s", row["id"], close_payload)
                close_reason = None
                close_price = None

        if close_reason:
            db.execute(
                """
                UPDATE btc5m_s6_positions
                SET status='CLOSED', close_ts_utc=?, close_price=?, close_reason=?,
                    last_mark_price=?, last_mark_ts_utc=?, pnl_usd=?, close_response_json=?
                WHERE id=?
                """,
                (
                    _now_iso(),
                    close_price,
                    close_reason,
                    mark,
                    _now_iso(),
                    pnl,
                    _json(close_payload),
                    row["id"],
                ),
            )
            closed += 1
        else:
            db.execute(
                """
                UPDATE btc5m_s6_positions
                SET last_mark_price=?, last_mark_ts_utc=?, pnl_usd=?, close_response_json=?
                WHERE id=?
                """,
                (mark, _now_iso(), pnl, _json(close_payload), row["id"]),
            )
        marked += 1

    db.commit()
    return {"marked": marked, "closed": closed, "open_positions": _open_position_count(db), "liquidity_trapped": trapped}


def _closed_1m_closes(price: BinancePrice, now_ts: float, limit: int) -> list[float]:
    rows = price.closed_1m_closes(now_ts, limit=max(5, limit))
    return [close for _, close in rows]


def _record_hold(db: sqlite3.Connection, market_slug: str | None, reason: str, summary: dict, **extra) -> dict:
    _record_attempt(
        db,
        market_slug=market_slug,
        action="hold",
        reason=reason,
        side=extra.get("side"),
        token_id=extra.get("token_id"),
        edge_bps=extra.get("edge_bps"),
        effective_edge_bps=extra.get("effective_edge_bps"),
        direction_bps=extra.get("direction_bps"),
        seconds_after_start=extra.get("seconds_after_start"),
        seconds_left=extra.get("seconds_left"),
        entry_price=extra.get("entry_price"),
        notional_usd=0,
        fair_raw=extra.get("fair_raw"),
        fair_calibrated=extra.get("fair_calibrated"),
        response=summary,
    )
    return summary


def _submit_entry_if_live(token_id: str, notional: float, max_price: float, payload: dict) -> tuple[str, str, dict]:
    live_enabled = _env_b("BTC5M_LIVE_ENABLED", False)
    dry_run = _env_b("BTC5M_LIVE_DRY_RUN", True)
    payload.update({"live_enabled": live_enabled, "dry_run": dry_run, "token_id": token_id, "notional": notional, "max_price": max_price})
    if live_enabled and not dry_run:
        response = _submit_market(token_id, notional, "BUY", max_price)
        payload["order_response"] = response
        if not _order_response_ok(response):
            return "live_order_failed", "submit_failed", payload
        return "live_order", "submitted", payload
    return "dry_run_order", "dry_run", payload


def _insert_position(
    db: sqlite3.Connection,
    market: BtcMarket,
    candidate: dict,
    start_price: float,
    spot: float,
    notional: float,
    direction_bps: float,
    effective_edge: int,
    response: dict,
) -> None:
    entry = float(candidate["ask"])
    size = notional / entry
    bid = float(candidate.get("bid") or entry)
    db.execute(
        """
        INSERT INTO btc5m_s6_positions
        (market_slug, side, token_id, open_ts_utc, status, entry_price, size_shares, notional_usd,
         edge_bps, effective_edge_bps, direction_bps, start_epoch, end_epoch, start_price, entry_spot,
         last_mark_price, last_mark_ts_utc, pnl_usd, open_response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            market.slug,
            candidate["side"],
            candidate["token_id"],
            _now_iso(),
            "OPEN",
            entry,
            size,
            notional,
            int(candidate["edge_bps"]),
            effective_edge,
            direction_bps,
            market.start_epoch,
            market.end_epoch,
            start_price,
            spot,
            bid,
            _now_iso(),
            (bid - entry) * size,
            _json(response),
        ),
    )
    db.commit()


def run_once(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice, tg: Notifier | None = None) -> dict:
    now_ts = time.time()
    position_stats = _mark_positions(db, poly, price)
    summary: dict = {
        "action": "hold",
        "reason": "no_signal",
        "market_slug": None,
        "best_edge_bps": None,
        "best_effective_edge_bps": None,
        "best_direction_bps": None,
        "live_enabled": _env_b("BTC5M_LIVE_ENABLED", False),
        "dry_run": _env_b("BTC5M_LIVE_DRY_RUN", True),
        **position_stats,
    }

    market = _load_current_market(poly, now_ts)
    if not market:
        summary["reason"] = "no_current_market"
        return summary

    market_slug = market.slug
    summary["market_slug"] = market_slug
    seconds_after_start = now_ts - market.start_epoch
    seconds_left = market.end_epoch - now_ts

    if seconds_after_start < _env_i("BTC5M_MIN_SECONDS_AFTER_START", 90):
        summary["reason"] = "too_early"
        return _record_hold(db, market_slug, "too_early", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    if seconds_left < _env_i("BTC5M_MIN_SECONDS_BEFORE_END", 90):
        summary["reason"] = "too_late"
        return _record_hold(db, market_slug, "too_late", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    if _open_position_count(db) >= _env_i("BTC5M_MAX_OPEN_POSITIONS", 1):
        summary["reason"] = "max_open"
        return _record_hold(db, market_slug, "max_open", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    if _market_seen(db, market_slug):
        summary["reason"] = "already_traded_market"
        return _record_hold(db, market_slug, "already_traded_market", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    if _env_b("BTC5M_SHOCK_FILTER_ENABLED", True):
        closes = _closed_1m_closes(price, now_ts, _env_i("BTC5M_SHOCK_LOOKBACK_MIN", 8))
        shock_reason, shock_stats = shock_filter_reason(
            closes,
            max_1m_move_bps=_env_f("BTC5M_MAX_1M_MOVE_BPS", 35.0),
            vol_spike_mult=_env_f("BTC5M_VOL_SPIKE_MULT", 3.0),
            min_spike_move_bps=_env_f("BTC5M_MIN_SPIKE_MOVE_BPS", 20.0),
        )
        summary["shock_stats"] = shock_stats
        if shock_reason:
            summary["reason"] = shock_reason
            return _record_hold(db, market_slug, shock_reason, summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    _populate_market_book(poly, market)
    start_price = price.window_open(market.start_epoch)
    spot = price.price()
    vol_1m = price.one_minute_vol(_env_i("BTC5M_VOL_LOOKBACK_MIN", 120))

    fair_up_raw = _fair_probability(start_price, spot, seconds_left, vol_1m)
    fair_down_raw = 1.0 - fair_up_raw
    fair_up = calibrated_fair(fair_up_raw, _env_f("BTC5M_FAIR_SHRINK_TO_HALF", 0.05))
    fair_down = calibrated_fair(fair_down_raw, _env_f("BTC5M_FAIR_SHRINK_TO_HALF", 0.05))

    trend_gate_enabled = _env_b("BTC5M_TREND_GATE_ENABLED", True)
    trend_side = None
    trend_score_bps = None
    if trend_gate_enabled:
        trend_pick = _trend_gate_decision(price, now_ts, _env_i("BTC5M_TREND_LOOKBACK_MIN", 2), _env_f("BTC5M_TREND_THRESHOLD_BPS", 0.0))
        if trend_pick is None:
            summary["reason"] = "trend_skip"
            _record_orderbook_snapshot(db, market, seconds_after_start, seconds_left, fair_up, fair_down, extra={"trend": "skip"})
            return _record_hold(db, market_slug, "trend_skip", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)
        trend_side, trend_score_bps = trend_pick

        if abs(float(trend_score_bps)) > _env_f("BTC5M_MAX_TREND_BPS", 45.0):
            summary["reason"] = "overextended_skip"
            summary["trend_side"] = trend_side
            summary["trend_score_bps"] = round(float(trend_score_bps), 2)
            _record_orderbook_snapshot(db, market, seconds_after_start, seconds_left, fair_up, fair_down, extra={"trend": "overextended"})
            return _record_hold(db, market_slug, "overextended_skip", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    if trend_gate_enabled:
        chosen_fair = fair_up if trend_side == "UP" else fair_down
        chosen_raw = fair_up_raw if trend_side == "UP" else fair_down_raw
        candidates = [_candidate(str(trend_side), chosen_fair, market)]
    else:
        candidates = [_candidate("UP", fair_up, market), _candidate("DOWN", fair_down, market)]
        chosen_raw = None

    candidates = [item for item in candidates if item]
    if not candidates:
        summary["reason"] = "missing_book"
        _record_orderbook_snapshot(db, market, seconds_after_start, seconds_left, fair_up, fair_down, extra={"reason": "missing_book"})
        return _record_hold(db, market_slug, "missing_book", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    best = max(candidates, key=lambda item: int(item["edge_bps"]))
    if chosen_raw is None:
        chosen_raw = fair_up_raw if best["side"] == "UP" else fair_down_raw

    direction = _direction_bps(str(best["side"]), start_price, spot)
    effective_edge, edge_parts = effective_edge_bps(
        int(best["edge_bps"]),
        float(best["spread_pct"]),
        float(best["ask_size"]),
        float(seconds_left),
        spread_penalty_mult=_env_f("BTC5M_SPREAD_EDGE_PENALTY_MULT", 0.8),
        depth_penalty_min_shares=_env_f("BTC5M_DEPTH_PENALTY_MIN_SHARES", 35.0),
        depth_penalty_max_bps=_env_i("BTC5M_DEPTH_PENALTY_MAX_BPS", 250),
        late_entry_sec=_env_i("BTC5M_LATE_ENTRY_SEC", 120),
        late_entry_penalty_bps=_env_i("BTC5M_LATE_ENTRY_EDGE_PENALTY_BPS", 200),
        model_error_buffer_bps=_env_i("BTC5M_MODEL_ERROR_BUFFER_BPS", 150),
    )

    summary.update(
        {
            "best_edge_bps": int(best["edge_bps"]),
            "best_effective_edge_bps": effective_edge,
            "best_direction_bps": round(direction, 2),
            "trend_side": trend_side,
            "trend_score_bps": round(float(trend_score_bps), 2) if trend_score_bps is not None else None,
            "fair_up_raw": round(fair_up_raw, 4),
            "fair_down_raw": round(fair_down_raw, 4),
            "fair_up": round(fair_up, 4),
            "fair_down": round(fair_down, 4),
            "edge_parts": edge_parts,
        }
    )

    _record_orderbook_snapshot(
        db,
        market,
        seconds_after_start,
        seconds_left,
        fair_up,
        fair_down,
        best_side=str(best["side"]),
        best_effective_edge_bps=effective_edge,
        extra={"summary": summary, "best": best},
    )

    reason = None
    if int(best["edge_bps"]) < _env_i("BTC5M_MIN_EDGE_BPS", 800):
        reason = "edge_too_small"
    elif _env_b("BTC5M_USE_EFFECTIVE_EDGE", True) and effective_edge < _env_i("BTC5M_MIN_EFFECTIVE_EDGE_BPS", 500):
        reason = "effective_edge_too_small"
    elif float(best["spread_pct"]) > _env_f("BTC5M_MAX_SPREAD_PCT", 0.03):
        reason = "spread_too_wide"
    elif float(best["ask"]) > _env_f("BTC5M_MAX_ENTRY_PRICE", 0.82):
        reason = "price_too_high"
    elif float(best["ask_size"]) < _env_f("BTC5M_MIN_ASK_DEPTH_SHARES", 10.0):
        reason = "depth_too_low"
    elif int(best["edge_bps"]) < _env_i("BTC5M_LOW_EDGE_CONFIRM_MAX_BPS", 1000) and direction < _env_f("BTC5M_LOW_EDGE_MIN_DIRECTION_BPS", 4.0):
        reason = "low_edge_weak_direction"
    elif int(best["edge_bps"]) >= _env_i("BTC5M_LOW_EDGE_CONFIRM_MAX_BPS", 1000) and direction < _env_f("BTC5M_HIGH_EDGE_MIN_DIRECTION_BPS", 2.0):
        reason = "high_edge_weak_direction"

    if reason:
        summary["reason"] = reason
        return _record_hold(
            db,
            market_slug,
            reason,
            summary,
            side=best["side"],
            token_id=best["token_id"],
            edge_bps=best["edge_bps"],
            effective_edge_bps=effective_edge,
            direction_bps=direction,
            seconds_after_start=seconds_after_start,
            seconds_left=seconds_left,
            entry_price=best["ask"],
            fair_raw=chosen_raw,
            fair_calibrated=best["fair"],
        )

    notional = min(_env_f("BTC5M_MAX_NOTIONAL_USD", 5.0), float(best["ask_size"]) * float(best["ask"]))
    if notional < _env_f("BTC5M_MIN_NOTIONAL_USD", 5.0):
        summary["reason"] = "notional_too_low"
        return _record_hold(
            db,
            market_slug,
            "notional_too_low",
            summary,
            side=best["side"],
            token_id=best["token_id"],
            edge_bps=best["edge_bps"],
            effective_edge_bps=effective_edge,
            direction_bps=direction,
            seconds_after_start=seconds_after_start,
            seconds_left=seconds_left,
            entry_price=best["ask"],
            fair_raw=chosen_raw,
            fair_calibrated=best["fair"],
        )

    response_payload = {
        "side": best["side"],
        "token_id": best["token_id"],
        "notional": notional,
        "max_price": best["ask"],
        "effective_edge_bps": effective_edge,
        "edge_parts": edge_parts,
        "fair_raw": chosen_raw,
        "fair_calibrated": best["fair"],
        "start_price": start_price,
        "entry_spot": spot,
        "vol_1m": vol_1m,
        "seconds_after_start": seconds_after_start,
        "seconds_left": seconds_left,
        "trend_side": trend_side,
        "trend_score_bps": trend_score_bps,
    }
    action, entry_reason, response_payload = _submit_entry_if_live(str(best["token_id"]), notional, float(best["ask"]), response_payload)

    _record_attempt(
        db,
        market_slug=market_slug,
        side=best["side"],
        token_id=best["token_id"],
        action=action,
        reason=entry_reason,
        edge_bps=best["edge_bps"],
        effective_edge_bps=effective_edge,
        direction_bps=direction,
        seconds_after_start=seconds_after_start,
        seconds_left=seconds_left,
        entry_price=best["ask"],
        notional_usd=notional,
        fair_raw=chosen_raw,
        fair_calibrated=best["fair"],
        response=response_payload,
    )

    if action == "live_order_failed":
        summary["action"] = action
        summary["reason"] = entry_reason
        return summary

    _insert_position(db, market, best, start_price, spot, notional, direction, effective_edge, response_payload)
    summary["action"] = action
    summary["reason"] = entry_reason

    if tg:
        tg.send(
            "<b>BTC5M S6 safe candidate</b>\n"
            f"mode={'LIVE' if summary['live_enabled'] and not summary['dry_run'] else 'DRY'}\n"
            f"{market.slug} {best['side']} raw_edge={best['edge_bps']} eff_edge={effective_edge} "
            f"dir={direction:.2f} ask={best['ask']:.2f} notional=${notional:.2f}"
        )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    cfg = _config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("btc5m-live-s6-safe")

    db = _open_s6_db(_env_s("BTC5M_S6_DB_PATH", os.path.expanduser("~/.local/share/arb-engine-live-s6/btc5m-s6.sqlite")))
    poly = PolyClient(cfg.poly.gamma_url, cfg.poly.clob_url)
    price = BinancePrice(_env_s("BTC5M_BINANCE_URL", "https://api.binance.com"))
    tg = Notifier(cfg.tg.bot_token, cfg.tg.chat_id, cfg.tg.enabled and _env_b("BTC5M_TG_ENABLED", False))

    stop = {"flag": False}

    def _handle(sig, frame):
        log.warning("signal %s - shutting down", sig)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    while not stop["flag"]:
        try:
            summary = run_once(db, poly, price, tg)
            log.info("btc5m live-s6-safe done %s", summary)
        except Exception as exc:
            log.warning("btc5m live-s6-safe failed: %s", exc)
        if args.once:
            break
        time.sleep(_env_i("BTC5M_INTERVAL_SEC", 10))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
