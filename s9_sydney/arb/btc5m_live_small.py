"""Small live runner for Polymarket BTC 5m markets.

This is intentionally separate from btc5m_paper.py. It uses a conservative
entry rule and a tiny max notional. Live order submission is disabled unless
BTC5M_LIVE_ENABLED=true and BTC5M_LIVE_DRY_RUN=false.
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
from pathlib import Path

from . import config as _config
from .btc5m_paper import (
    BinancePrice,
    _book_by_token,
    _candidate,
    _direction_bps,
    _env_b,
    _env_f,
    _env_i,
    _env_s,
    _load_current_market,
    _open_db,
    _populate_market_book,
    _fair_probability,
)
from .ladder import _top_of_book
from .poly_client import PolyClient
from .tg_notifier import Notifier


LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS btc5m_live_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT,
  side TEXT,
  token_id TEXT,
  action TEXT,
  reason TEXT,
  edge_bps INTEGER,
  direction_bps REAL,
  seconds_after_start REAL,
  seconds_left REAL,
  entry_price REAL,
  notional_usd REAL,
  fair_prob REAL,
  response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_live_attempts_market ON btc5m_live_attempts(market_slug);

CREATE TABLE IF NOT EXISTS btc5m_live_positions (
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

CREATE INDEX IF NOT EXISTS idx_btc5m_live_positions_status ON btc5m_live_positions(status);
CREATE INDEX IF NOT EXISTS idx_btc5m_live_positions_market ON btc5m_live_positions(market_slug);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _live_db(path: str) -> sqlite3.Connection:
    db = _open_db(path)
    db.executescript(LIVE_SCHEMA)
    db.commit()
    return db


def _record_attempt(db: sqlite3.Connection, **values) -> None:
    db.execute(
        """
        INSERT INTO btc5m_live_attempts
        (ts_utc, market_slug, side, token_id, action, reason, edge_bps, direction_bps,
         seconds_after_start, seconds_left, entry_price, notional_usd, fair_prob, response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now_iso(),
            values.get("market_slug"),
            values.get("side"),
            values.get("token_id"),
            values.get("action"),
            values.get("reason"),
            values.get("edge_bps"),
            values.get("direction_bps"),
            values.get("seconds_after_start"),
            values.get("seconds_left"),
            values.get("entry_price"),
            values.get("notional_usd"),
            values.get("fair_prob"),
            json.dumps(values.get("response"), sort_keys=True, default=str) if values.get("response") is not None else None,
        ),
    )
    db.commit()


def _market_attempt_exists(db: sqlite3.Connection, market_slug: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM btc5m_live_attempts WHERE market_slug=? AND action IN ('live_order','dry_run_order') LIMIT 1",
        (market_slug,),
    ).fetchone()
    return row is not None


def _open_position_count(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) FROM btc5m_live_positions WHERE status='OPEN'").fetchone()
    return int(row[0] or 0)


def _position_exists(db: sqlite3.Connection, market_slug: str) -> bool:
    row = db.execute("SELECT 1 FROM btc5m_live_positions WHERE market_slug=? LIMIT 1", (market_slug,)).fetchone()
    return row is not None


def _make_clob_client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    host = _env_s("POLY_CLOB_URL", "https://clob.polymarket.com")
    key = _env_s("POLYGON_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("POLYGON_PRIVATE_KEY is required for live orders")
    signature_type = _env_i("POLY_SIGNATURE_TYPE", 0)
    funder = _env_s("POLY_FUNDER", "") or None
    client = ClobClient(host, chain_id=137, key=key, signature_type=signature_type, funder=funder)
    api_key = _env_s("POLY_CLOB_API_KEY", "")
    api_secret = _env_s("POLY_CLOB_API_SECRET", "")
    api_passphrase = _env_s("POLY_CLOB_API_PASSPHRASE", "")
    if api_key and api_secret and api_passphrase:
        client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
    else:
        client.set_api_creds(client.create_or_derive_api_key())
    return client


def _submit_market(token_id: str, amount: float, side: str, price: float) -> dict:
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType

    client = _make_clob_client()
    args = MarketOrderArgsV2(token_id=str(token_id), amount=float(amount), side=side, price=float(price))
    return client.create_and_post_market_order(args, order_type=OrderType.FOK)


def _order_response_ok(response: object) -> bool:
    if not isinstance(response, dict):
        return bool(response)
    if response.get("success") is False:
        return False
    for key in ("error", "error_msg", "errorMsg"):
        if response.get(key):
            return False
    if "success" in response:
        return bool(response.get("success"))
    return bool(response)


def _opened_with_live_order(row: sqlite3.Row) -> bool:
    try:
        response = json.loads(row["open_response_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(response.get("live_enabled")) and not bool(response.get("dry_run"))


def _closed_1m_closes(price: BinancePrice, now_ts: float, limit: int = 8) -> list[float]:
    rows = price.klines("1m", limit=max(5, limit))
    now_ms = int(now_ts * 1000)
    closes: list[float] = []
    for row in rows:
        if not row or len(row) <= 6:
            continue
        try:
            close_time_ms = int(row[6])
            close = float(row[4])
        except Exception:
            continue
        if close_time_ms < now_ms:
            closes.append(close)
    return closes


def _trend_gate_decision(price: BinancePrice, now_ts: float, lookback_min: int, threshold_bps: float) -> tuple[str, float] | None:
    closes = _closed_1m_closes(price, now_ts, limit=max(lookback_min + 4, 8))
    lookback_min = max(1, int(lookback_min))
    if len(closes) <= lookback_min:
        return None
    prev = float(closes[-1 - lookback_min])
    last = float(closes[-1])
    if prev <= 0 or last <= 0:
        return None
    score = (last / prev - 1.0) * 10_000.0
    if score > threshold_bps:
        return "UP", score
    if score < -threshold_bps:
        return "DOWN", score
    return None


def _entry_exitability_reason(candidate: dict) -> str | None:
    max_spread_pct = _env_f("BTC5M_ENTRY_EXITABILITY_MAX_SPREAD_PCT", 0.0145)
    min_ask_depth = _env_f("BTC5M_ENTRY_EXITABILITY_MIN_ASK_DEPTH_SHARES", 25.0)
    if float(candidate["spread_pct"]) > max_spread_pct:
        return "entry_exitability_spread"
    if float(candidate["ask_size"]) < min_ask_depth:
        return "entry_exitability_depth"
    return None


def _insert_position(
    db: sqlite3.Connection,
    market,
    candidate: dict,
    start_price: float,
    spot: float,
    notional: float,
    direction_bps: float,
    response: dict,
) -> None:
    entry = float(candidate["ask"])
    size = notional / entry
    bid = float(candidate.get("bid") or entry)
    db.execute(
        """
        INSERT INTO btc5m_live_positions
        (market_slug, side, token_id, open_ts_utc, status, entry_price, size_shares, notional_usd,
         edge_bps, direction_bps, start_epoch, end_epoch, start_price, entry_spot, last_mark_price,
         last_mark_ts_utc, pnl_usd, open_response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            direction_bps,
            market.start_epoch,
            market.end_epoch,
            start_price,
            spot,
            bid,
            _now_iso(),
            (bid - entry) * size,
            json.dumps(response, sort_keys=True, default=str),
        ),
    )
    db.commit()


def _mark_positions(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice) -> dict:
    rows = list(db.execute("SELECT * FROM btc5m_live_positions WHERE status='OPEN' ORDER BY id"))
    if not rows:
        return {"marked": 0, "closed": 0, "open_positions": 0}
    books = _book_by_token(poly, [str(row["token_id"]) for row in rows])
    now_ts = time.time()
    marked = 0
    closed = 0
    live_enabled = _env_b("BTC5M_LIVE_ENABLED", False)
    dry_run = _env_b("BTC5M_LIVE_DRY_RUN", True)
    tail_force_exit_sec = _env_i("BTC5M_TAIL_FORCE_EXIT_SEC", 60)
    pre_enabled = _env_b("BTC5M_PRE_SETTLE_LOSS_CAP_ENABLED", True)
    pre1_sec = _env_i("BTC5M_PRE_SETTLE_EXIT1_SEC", 150)
    pre1_max_loss = _env_f("BTC5M_PRE_SETTLE_EXIT1_MAX_LOSS", 0.60)
    pre2_sec = _env_i("BTC5M_PRE_SETTLE_EXIT2_SEC", 120)
    pre2_max_loss = _env_f("BTC5M_PRE_SETTLE_EXIT2_MAX_LOSS", 0.50)
    pre3_sec = _env_i("BTC5M_PRE_SETTLE_EXIT3_SEC", 90)
    pre3_max_loss = _env_f("BTC5M_PRE_SETTLE_EXIT3_MAX_LOSS", 0.40)
    pre4_sec = _env_i("BTC5M_PRE_SETTLE_EXIT4_SEC", 60)
    pre4_max_loss = _env_f("BTC5M_PRE_SETTLE_EXIT4_MAX_LOSS", 0.30)
    pre5_sec = _env_i("BTC5M_PRE_SETTLE_EXIT5_SEC", 30)
    pre5_max_loss = _env_f("BTC5M_PRE_SETTLE_EXIT5_MAX_LOSS", 0.20)
    pre6_sec = _env_i("BTC5M_PRE_SETTLE_EXIT6_SEC", 10)
    pre6_max_loss = _env_f("BTC5M_PRE_SETTLE_EXIT6_MAX_LOSS", 0.00)
    stop_loss = _env_f("BTC5M_STOP_LOSS", 0.30)
    stop_loss_enabled = _env_b("BTC5M_STOP_LOSS_ENABLED", False)
    settle_delay = _env_i("BTC5M_SETTLE_DELAY_SEC", 8)
    for row in rows:
        token_id = str(row["token_id"])
        bid, _, _, _ = _top_of_book(books.get(token_id, {}))
        mark = float(bid) if bid is not None else row["last_mark_price"]
        if mark is None:
            continue
        entry = float(row["entry_price"])
        size = float(row["size_shares"])
        notional = float(row["notional_usd"])
        pnl = (float(mark) - entry) * size
        close_price = None
        close_reason = None
        close_response = None
        if now_ts >= float(row["end_epoch"]) + settle_delay:
            close_spot = price.window_close(int(row["start_epoch"]))
            up_wins = close_spot >= float(row["start_price"])
            winner = "UP" if up_wins else "DOWN"
            close_price = 1.0 if row["side"] == winner else 0.0
            pnl = (close_price - entry) * size
            close_reason = "settled_binance_proxy"
            close_response = {"winner": winner, "close_spot": close_spot}
        elif tail_force_exit_sec > 0 and seconds_left <= tail_force_exit_sec:
            close_price = float(mark)
            close_reason = "tail_force_exit"
            close_response = {
                "dry_run": dry_run,
                "live_enabled": live_enabled,
                "opened_with_live_order": _opened_with_live_order(row),
                "token_id": token_id,
                "shares": size,
                "target_price": close_price,
            }
            if _opened_with_live_order(row) and live_enabled and not dry_run:
                order_response = _submit_market(token_id, size, "SELL", close_price)
                close_response["order_response"] = order_response
                if not _order_response_ok(order_response):
                    logging.getLogger("btc5m-live-small").warning(
                        "tail-force close order failed for position %s: %s",
                        row["id"],
                        order_response,
                    )
                    close_reason = None
        elif pre_enabled and notional > 0 and pnl < 0:
            loss_frac = (-pnl) / notional
            max_loss = None
            if seconds_left <= pre6_sec:
                max_loss = pre6_max_loss
            elif seconds_left <= pre5_sec:
                max_loss = pre5_max_loss
            elif seconds_left <= pre4_sec:
                max_loss = pre4_max_loss
            elif seconds_left <= pre3_sec:
                max_loss = pre3_max_loss
            elif seconds_left <= pre2_sec:
                max_loss = pre2_max_loss
            elif seconds_left <= pre1_sec:
                max_loss = pre1_max_loss
            if max_loss is not None and loss_frac >= max_loss:
                close_price = float(mark)
                close_reason = "pre_settle_loss_cap"
                close_response = {
                    "dry_run": dry_run,
                    "live_enabled": live_enabled,
                    "opened_with_live_order": _opened_with_live_order(row),
                    "token_id": token_id,
                    "shares": size,
                    "target_price": close_price,
                    "loss_frac": loss_frac,
                }
                if _opened_with_live_order(row) and live_enabled and not dry_run:
                    order_response = _submit_market(token_id, size, "SELL", close_price)
                    close_response["order_response"] = order_response
                    if not _order_response_ok(order_response):
                        logging.getLogger("btc5m-live-small").warning(
                            "pre-settle close order failed for position %s: %s",
                            row["id"],
                            order_response,
                        )
                        close_reason = None
        elif notional > 0 and pnl <= -notional * stop_loss:
            close_price = float(mark)
            close_reason = "stop_loss"
            close_response = {
                "dry_run": dry_run,
                "live_enabled": live_enabled,
                "opened_with_live_order": _opened_with_live_order(row),
                "token_id": token_id,
                "shares": size,
                "min_price": close_price,
            }
            if _opened_with_live_order(row) and live_enabled and not dry_run:
                order_response = _submit_market(token_id, size, "SELL", close_price)
                close_response["order_response"] = order_response
                if not _order_response_ok(order_response):
                    logging.getLogger("btc5m-live-small").warning(
                        "stop-loss close order failed for position %s: %s",
                        row["id"],
                        order_response,
                    )
                    close_reason = None
        if close_reason:
            db.execute(
                """
                UPDATE btc5m_live_positions
                SET status='CLOSED', close_ts_utc=?, close_price=?, close_reason=?, last_mark_price=?,
                    last_mark_ts_utc=?, pnl_usd=?, close_response_json=?
                WHERE id=?
                """,
                (
                    _now_iso(),
                    close_price,
                    close_reason,
                    mark,
                    _now_iso(),
                    pnl,
                    json.dumps(close_response, sort_keys=True, default=str),
                    row["id"],
                ),
            )
            closed += 1
        else:
            db.execute(
                "UPDATE btc5m_live_positions SET last_mark_price=?, last_mark_ts_utc=?, pnl_usd=? WHERE id=?",
                (mark, _now_iso(), pnl, row["id"]),
            )
        marked += 1
    db.commit()
    return {"marked": marked, "closed": closed, "open_positions": _open_position_count(db)}


def run_once(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice, tg: Notifier | None = None) -> dict:
    now_ts = time.time()
    position_stats = _mark_positions(db, poly, price)
    summary = {
        "action": "hold",
        "reason": "no_signal",
        "market_slug": None,
        "best_edge_bps": None,
        "best_direction_bps": None,
        "live_enabled": _env_b("BTC5M_LIVE_ENABLED", False),
        "dry_run": _env_b("BTC5M_LIVE_DRY_RUN", True),
        **position_stats,
    }
    market = _load_current_market(poly, now_ts)
    if not market:
        summary["reason"] = "no_current_market"
        return summary
    summary["market_slug"] = market.slug
    seconds_after_start = now_ts - market.start_epoch
    seconds_left = market.end_epoch - now_ts
    if seconds_after_start < _env_i("BTC5M_MIN_SECONDS_AFTER_START", 60):
        summary["reason"] = "too_early"
        return summary
    if seconds_left < _env_i("BTC5M_MIN_SECONDS_BEFORE_END", 45):
        summary["reason"] = "too_late"
        return summary
    if _open_position_count(db) >= _env_i("BTC5M_MAX_OPEN_POSITIONS", 1):
        summary["reason"] = "max_open"
        return summary
    if _market_attempt_exists(db, market.slug) or _position_exists(db, market.slug):
        summary["reason"] = "already_traded_market"
        return summary

    _populate_market_book(poly, market)
    start_price = price.window_open(market.start_epoch)
    spot = price.price()
    vol_1m = price.one_minute_vol(_env_i("BTC5M_VOL_LOOKBACK_MIN", 120))
    fair_up = _fair_probability(start_price, spot, seconds_left, vol_1m)
    fair_down = 1.0 - fair_up
    trend_gate_enabled = _env_b("BTC5M_TREND_GATE_ENABLED", True)
    trend_lookback_min = _env_i("BTC5M_TREND_LOOKBACK_MIN", 2)
    trend_threshold_bps = _env_f("BTC5M_TREND_THRESHOLD_BPS", 0.0)
    trend_side = None
    trend_score_bps = None
    if trend_gate_enabled:
        trend_pick = _trend_gate_decision(price, now_ts, trend_lookback_min, trend_threshold_bps)
        if trend_pick is None:
            summary["reason"] = "trend_skip"
            _record_attempt(
                db,
                market_slug=market.slug,
                side=None,
                token_id=None,
                action="hold",
                reason=summary["reason"],
                edge_bps=None,
                direction_bps=None,
                seconds_after_start=seconds_after_start,
                seconds_left=seconds_left,
                entry_price=None,
                notional_usd=0,
                fair_prob=None,
                response=summary,
            )
            return summary
        trend_side, trend_score_bps = trend_pick
    if trend_gate_enabled:
        candidates = [_candidate(trend_side, fair_up if trend_side == "UP" else fair_down, market)]
    else:
        candidates = [_candidate("UP", fair_up, market), _candidate("DOWN", fair_down, market)]
    candidates = [candidate for candidate in candidates if candidate]
    if not candidates:
        summary["reason"] = "missing_book"
        return summary
    best = max(candidates, key=lambda item: item["edge_bps"])
    direction = trend_score_bps if trend_score_bps is not None else _direction_bps(str(best["side"]), start_price, spot)
    summary["best_edge_bps"] = best["edge_bps"]
    summary["best_direction_bps"] = round(direction, 2)

    min_edge = _env_i("BTC5M_MIN_EDGE_BPS", 500)
    min_direction = _env_f("BTC5M_MIN_DIRECTION_BPS", 4.0)
    max_spread = _env_f("BTC5M_MAX_SPREAD_PCT", 0.03)
    max_entry = _env_f("BTC5M_MAX_ENTRY_PRICE", 0.82)
    min_depth = _env_f("BTC5M_MIN_ASK_DEPTH_SHARES", 10.0)
    max_notional = _env_f("BTC5M_MAX_NOTIONAL_USD", 5.0)
    if int(best["edge_bps"]) < min_edge:
        summary["reason"] = "edge_too_small"
    elif (not trend_gate_enabled) and direction < min_direction:
        summary["reason"] = "weak_direction"
    elif float(best["spread_pct"]) > max_spread:
        summary["reason"] = "spread_too_wide"
    elif float(best["ask"]) > max_entry:
        summary["reason"] = "price_too_high"
    elif float(best["ask_size"]) < min_depth:
        summary["reason"] = "depth_too_low"
    elif _entry_exitability_reason(best):
        summary["reason"] = _entry_exitability_reason(best)
    else:
        notional = min(max_notional, float(best["ask_size"]) * float(best["ask"]))
        if notional < _env_f("BTC5M_MIN_NOTIONAL_USD", 5.0):
            summary["reason"] = "notional_too_low"
        else:
            dry_run = _env_b("BTC5M_LIVE_DRY_RUN", True)
            live_enabled = _env_b("BTC5M_LIVE_ENABLED", False)
            response = {
                "dry_run": dry_run,
                "live_enabled": live_enabled,
                "side": best["side"],
                "token_id": best["token_id"],
                "notional": notional,
                "max_price": best["ask"],
            }
            action = "dry_run_order"
            reason = "dry_run"
            if live_enabled and not dry_run:
                order_response = _submit_market(str(best["token_id"]), notional, "BUY", float(best["ask"]))
                response["dry_run"] = False
                response["live_enabled"] = True
                response["order_response"] = order_response
                if not _order_response_ok(order_response):
                    _record_attempt(
                        db,
                        market_slug=market.slug,
                        side=best["side"],
                        token_id=best["token_id"],
                        action="live_order_failed",
                        reason="submit_failed",
                        edge_bps=best["edge_bps"],
                        direction_bps=direction,
                        seconds_after_start=seconds_after_start,
                        seconds_left=seconds_left,
                        entry_price=best["ask"],
                        notional_usd=notional,
                        fair_prob=best["fair"],
                        response=response,
                    )
                    summary["action"] = "live_order_failed"
                    summary["reason"] = "submit_failed"
                    return summary
                action = "live_order"
                reason = "submitted"
            _insert_position(db, market, best, start_price, spot, notional, direction, response)
            _record_attempt(
                db,
                market_slug=market.slug,
                side=best["side"],
                token_id=best["token_id"],
                action=action,
                reason=reason,
                edge_bps=best["edge_bps"],
                direction_bps=direction,
                seconds_after_start=seconds_after_start,
                seconds_left=seconds_left,
                entry_price=best["ask"],
                notional_usd=notional,
                fair_prob=best["fair"],
                response=response,
            )
            summary["action"] = action
            summary["reason"] = reason
            if tg:
                tg.send_html(
                    "<b>BTC5M live-small candidate</b>\n"
                    f"mode={'LIVE' if live_enabled and not dry_run else 'DRY'}\n"
                    f"{market.slug} {best['side']} edge={best['edge_bps']} dir={direction:.2f} "
                    f"ask={best['ask']:.2f} notional=${notional:.2f}"
                )
            return summary

    _record_attempt(
        db,
        market_slug=market.slug,
        side=best["side"],
        token_id=best["token_id"],
        action="hold",
        reason=summary["reason"],
        edge_bps=best["edge_bps"],
        direction_bps=direction,
        seconds_after_start=seconds_after_start,
        seconds_left=seconds_left,
        entry_price=best["ask"],
        notional_usd=0,
        fair_prob=best["fair"],
        response=summary,
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
    log = logging.getLogger("btc5m-live-small")
    db = _live_db(_env_s("BTC5M_LIVE_DB_PATH", os.path.expanduser("~/.local/share/arb-engine-live-small/btc5m-live.sqlite")))
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
            log.info("btc5m live-small done %s", summary)
        except Exception as exc:
            log.warning("btc5m live-small failed: %s", exc)
        if args.once:
            break
        time.sleep(_env_i("BTC5M_INTERVAL_SEC", 10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
