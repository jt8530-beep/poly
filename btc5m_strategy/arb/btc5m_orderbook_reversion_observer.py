"""S5 Orderbook Reversion Observer for Polymarket BTC 5m markets.

Purpose:
  Observe and paper-test whether low/mid-liquidity orderbook behavior
  predicts last-1~2-minute BTC 5m reversion.

This is NOT a live trading runner.
It never submits real orders.

Core idea:
  - BTC below current 5m open by X bps -> observe UP reversion
  - BTC above current 5m open by X bps -> observe DOWN reversion
  - Only inside seconds_left window, default 60-150 sec
  - Target ask <= max price
  - Spread/depth must be tradable
  - Total book depth cannot be too thick
  - Previous snapshot must show target-side bid did not collapse
  - Strong 15m trend against the trade is vetoed
  - Paper entry records both theoretical ask and conservative ask+slippage
  - Hold to Binance proxy settlement
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
    _candidate,
    _env_b,
    _env_f,
    _env_i,
    _env_s,
    _fair_probability,
    _load_current_market,
    _open_db,
    _populate_market_book,
)
from .poly_client import PolyClient
from .tg_notifier import Notifier


SCHEMA = """
CREATE TABLE IF NOT EXISTS btc5m_ob_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT NOT NULL,
  start_epoch INTEGER,
  end_epoch INTEGER,
  seconds_after_start REAL,
  seconds_left REAL,
  start_price REAL,
  spot REAL,
  deviation_bps REAL,
  ret15_bps REAL,

  up_token_id TEXT,
  up_bid REAL,
  up_bid_size REAL,
  up_ask REAL,
  up_ask_size REAL,
  up_spread_pct REAL,
  up_edge_bps INTEGER,
  up_fair REAL,

  down_token_id TEXT,
  down_bid REAL,
  down_bid_size REAL,
  down_ask REAL,
  down_ask_size REAL,
  down_spread_pct REAL,
  down_edge_bps INTEGER,
  down_fair REAL,

  total_depth_shares REAL,
  response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_ob_snapshots_market ON btc5m_ob_snapshots(market_slug);
CREATE INDEX IF NOT EXISTS idx_btc5m_ob_snapshots_ts ON btc5m_ob_snapshots(ts_utc);

CREATE TABLE IF NOT EXISTS btc5m_ob_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT,
  action TEXT,
  reason TEXT,
  side TEXT,
  token_id TEXT,
  entry_price REAL,
  entry_price_slip REAL,
  notional_usd REAL,
  edge_bps INTEGER,
  seconds_after_start REAL,
  seconds_left REAL,
  deviation_bps REAL,
  ret15_bps REAL,
  response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_ob_attempts_market ON btc5m_ob_attempts(market_slug);

CREATE TABLE IF NOT EXISTS btc5m_ob_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_slug TEXT NOT NULL,
  side TEXT NOT NULL,
  token_id TEXT NOT NULL,
  open_ts_utc TEXT NOT NULL,
  close_ts_utc TEXT,
  status TEXT NOT NULL,

  entry_price REAL NOT NULL,
  entry_price_slip REAL NOT NULL,
  size_shares REAL NOT NULL,
  size_shares_slip REAL NOT NULL,
  notional_usd REAL NOT NULL,

  edge_bps INTEGER,
  start_epoch INTEGER,
  end_epoch INTEGER,
  start_price REAL,
  entry_spot REAL,
  deviation_bps REAL,
  ret15_bps REAL,

  last_mark_price REAL,
  last_mark_ts_utc TEXT,

  close_price REAL,
  close_reason TEXT,
  pnl_usd REAL DEFAULT 0,
  pnl_usd_slip REAL DEFAULT 0,

  open_response_json TEXT,
  close_response_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_ob_positions_status ON btc5m_ob_positions(status);
CREATE INDEX IF NOT EXISTS idx_btc5m_ob_positions_market ON btc5m_ob_positions(market_slug);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db(path: str) -> sqlite3.Connection:
    con = _open_db(path)
    con.executescript(SCHEMA)
    con.commit()
    return con


def _bps(a: float, b: float) -> float | None:
    if a <= 0 or b <= 0:
        return None
    return (b / a - 1.0) * 10_000.0


def _closed_1m_closes(price: BinancePrice, now_ts: float, limit: int = 30) -> list[float]:
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


def _ret_bps(price: BinancePrice, now_ts: float, minutes: int) -> float | None:
    minutes = max(1, int(minutes))
    closes = _closed_1m_closes(price, now_ts, limit=minutes + 5)
    if len(closes) <= minutes:
        return None
    return _bps(float(closes[-1 - minutes]), float(closes[-1]))


def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _latest_snapshot(db: sqlite3.Connection, market_slug: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM btc5m_ob_snapshots WHERE market_slug=? ORDER BY id DESC LIMIT 1",
        (market_slug,),
    ).fetchone()


def _market_already_traded(db: sqlite3.Connection, market_slug: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM btc5m_ob_positions WHERE market_slug=? LIMIT 1",
        (market_slug,),
    ).fetchone()
    return row is not None


def _open_position_count(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) FROM btc5m_ob_positions WHERE status='OPEN'").fetchone()
    return int(row[0] or 0)


def _record_attempt(db: sqlite3.Connection, **v) -> None:
    db.execute(
        """
        INSERT INTO btc5m_ob_attempts
        (ts_utc, market_slug, action, reason, side, token_id, entry_price, entry_price_slip,
         notional_usd, edge_bps, seconds_after_start, seconds_left, deviation_bps, ret15_bps, response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now_iso(),
            v.get("market_slug"),
            v.get("action"),
            v.get("reason"),
            v.get("side"),
            v.get("token_id"),
            v.get("entry_price"),
            v.get("entry_price_slip"),
            v.get("notional_usd"),
            v.get("edge_bps"),
            v.get("seconds_after_start"),
            v.get("seconds_left"),
            v.get("deviation_bps"),
            v.get("ret15_bps"),
            json.dumps(v.get("response"), sort_keys=True, default=str) if v.get("response") is not None else None,
        ),
    )
    db.commit()


def _snapshot_payload(up: dict | None, down: dict | None, spot: float, prev: sqlite3.Row | None) -> dict:
    payload = {
        "spot_delta_bps": None,
        "up_bid_delta": None,
        "up_bid_size_delta": None,
        "down_bid_delta": None,
        "down_bid_size_delta": None,
    }
    if prev:
        prev_spot = _safe_float(prev["spot"])
        if prev_spot and spot:
            payload["spot_delta_bps"] = _bps(prev_spot, spot)

        for side, cand in [("up", up), ("down", down)]:
            if not cand:
                continue
            bid = _safe_float(cand.get("bid"))
            bid_size = _safe_float(cand.get("bid_size"))
            prev_bid = _safe_float(prev[f"{side}_bid"])
            prev_bid_size = _safe_float(prev[f"{side}_bid_size"])
            if bid is not None and prev_bid is not None:
                payload[f"{side}_bid_delta"] = bid - prev_bid
            if bid_size is not None and prev_bid_size is not None:
                payload[f"{side}_bid_size_delta"] = bid_size - prev_bid_size
    return payload


def _insert_snapshot(
    db: sqlite3.Connection,
    market,
    up: dict | None,
    down: dict | None,
    start_price: float,
    spot: float,
    deviation_bps: float,
    ret15_bps: float | None,
    seconds_after_start: float,
    seconds_left: float,
    payload: dict,
) -> None:
    def g(c, k):
        return c.get(k) if c else None

    total_depth = 0.0
    for c in [up, down]:
        if c:
            for k in ["bid_size", "ask_size"]:
                total_depth += float(c.get(k) or 0)

    db.execute(
        """
        INSERT INTO btc5m_ob_snapshots
        (ts_utc, market_slug, start_epoch, end_epoch, seconds_after_start, seconds_left,
         start_price, spot, deviation_bps, ret15_bps,
         up_token_id, up_bid, up_bid_size, up_ask, up_ask_size, up_spread_pct, up_edge_bps, up_fair,
         down_token_id, down_bid, down_bid_size, down_ask, down_ask_size, down_spread_pct, down_edge_bps, down_fair,
         total_depth_shares, response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now_iso(),
            market.slug,
            market.start_epoch,
            market.end_epoch,
            seconds_after_start,
            seconds_left,
            start_price,
            spot,
            deviation_bps,
            ret15_bps,

            g(up, "token_id"),
            g(up, "bid"),
            g(up, "bid_size"),
            g(up, "ask"),
            g(up, "ask_size"),
            g(up, "spread_pct"),
            g(up, "edge_bps"),
            g(up, "fair"),

            g(down, "token_id"),
            g(down, "bid"),
            g(down, "bid_size"),
            g(down, "ask"),
            g(down, "ask_size"),
            g(down, "spread_pct"),
            g(down, "edge_bps"),
            g(down, "fair"),

            total_depth,
            json.dumps(payload, sort_keys=True, default=str),
        ),
    )
    db.commit()


def _support_reason(side: str, cand: dict, prev: sqlite3.Row | None) -> tuple[str | None, dict]:
    if prev is None:
        return "need_prev_snapshot", {}

    side_key = side.lower()
    bid = float(cand.get("bid") or 0)
    bid_size = float(cand.get("bid_size") or 0)
    prev_bid = _safe_float(prev[f"{side_key}_bid"], 0.0)
    prev_bid_size = _safe_float(prev[f"{side_key}_bid_size"], 0.0)

    max_bid_drop = _env_f("BTC5M_OB_MAX_BID_DROP", 0.02)
    min_bid_size_ratio = _env_f("BTC5M_OB_MIN_BID_SIZE_RATIO", 0.80)

    payload = {
        "side": side,
        "bid": bid,
        "bid_size": bid_size,
        "prev_bid": prev_bid,
        "prev_bid_size": prev_bid_size,
        "bid_delta": bid - prev_bid,
        "bid_size_delta": bid_size - prev_bid_size,
        "max_bid_drop": max_bid_drop,
        "min_bid_size_ratio": min_bid_size_ratio,
    }

    if prev_bid and bid < prev_bid - max_bid_drop:
        return "support_bid_collapsed", payload

    if prev_bid_size and bid_size < prev_bid_size * min_bid_size_ratio:
        return "support_bid_size_collapsed", payload

    return None, payload


def _choose_reversion_candidate(
    db: sqlite3.Connection,
    market_slug: str,
    up: dict | None,
    down: dict | None,
    deviation_bps: float,
    ret15_bps: float | None,
    total_depth: float,
    prev: sqlite3.Row | None,
) -> tuple[dict | None, str, dict]:
    min_dev = _env_f("BTC5M_OB_MIN_DEVIATION_BPS", 6.0)
    max_ask = _env_f("BTC5M_OB_MAX_ENTRY_PRICE", 0.55)
    max_spread = _env_f("BTC5M_OB_MAX_SPREAD_PCT", 0.03)
    min_ask_size = _env_f("BTC5M_OB_MIN_ASK_DEPTH_SHARES", 10.0)
    min_total_depth = _env_f("BTC5M_OB_MIN_TOTAL_DEPTH_SHARES", 20.0)
    max_total_depth = _env_f("BTC5M_OB_MAX_TOTAL_DEPTH_SHARES", 2500.0)
    ret15_veto = _env_f("BTC5M_OB_RET15_VETO_BPS", 25.0)

    payload = {
        "min_dev": min_dev,
        "max_ask": max_ask,
        "max_spread": max_spread,
        "min_ask_size": min_ask_size,
        "min_total_depth": min_total_depth,
        "max_total_depth": max_total_depth,
        "ret15_veto": ret15_veto,
        "total_depth": total_depth,
        "deviation_bps": deviation_bps,
        "ret15_bps": ret15_bps,
    }

    if abs(float(deviation_bps)) < min_dev:
        return None, "deviation_too_small", payload

    if total_depth < min_total_depth:
        return None, "book_too_thin", payload
    if total_depth > max_total_depth:
        return None, "book_too_thick", payload

    if deviation_bps <= -min_dev:
        side = "UP"
        cand = up
        if ret15_bps is not None and ret15_bps <= -ret15_veto:
            return None, "ret15_downtrend_veto_up", payload
    elif deviation_bps >= min_dev:
        side = "DOWN"
        cand = down
        if ret15_bps is not None and ret15_bps >= ret15_veto:
            return None, "ret15_uptrend_veto_down", payload
    else:
        return None, "no_side", payload

    payload["target_side"] = side

    if not cand:
        return None, "missing_target_book", payload

    ask = float(cand.get("ask") or 999)
    spread = float(cand.get("spread_pct") or 999)
    ask_size = float(cand.get("ask_size") or 0)

    payload.update({
        "ask": ask,
        "spread": spread,
        "ask_size": ask_size,
        "edge_bps": cand.get("edge_bps"),
        "bid": cand.get("bid"),
        "bid_size": cand.get("bid_size"),
    })

    if ask > max_ask:
        return None, "target_price_too_high", payload
    if spread > max_spread:
        return None, "target_spread_too_wide", payload
    if ask_size < min_ask_size:
        return None, "target_depth_too_low", payload

    support_reason, support_payload = _support_reason(side, cand, prev)
    payload["support"] = support_payload
    if support_reason:
        return None, support_reason, payload

    return cand, "ok", payload


def _insert_position(
    db: sqlite3.Connection,
    market,
    cand: dict,
    start_price: float,
    spot: float,
    deviation_bps: float,
    ret15_bps: float | None,
    payload: dict,
) -> None:
    notional = _env_f("BTC5M_OB_NOTIONAL_USD", 5.0)
    entry = float(cand["ask"])
    slip = _env_f("BTC5M_OB_ENTRY_SLIPPAGE", 0.01)
    entry_slip = min(0.99, entry + slip)

    size = notional / entry
    size_slip = notional / entry_slip
    mark = float(cand.get("bid") or entry)

    db.execute(
        """
        INSERT INTO btc5m_ob_positions
        (market_slug, side, token_id, open_ts_utc, status,
         entry_price, entry_price_slip, size_shares, size_shares_slip, notional_usd,
         edge_bps, start_epoch, end_epoch, start_price, entry_spot, deviation_bps, ret15_bps,
         last_mark_price, last_mark_ts_utc, pnl_usd, pnl_usd_slip, open_response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            market.slug,
            cand["side"],
            cand["token_id"],
            _now_iso(),
            "OPEN",
            entry,
            entry_slip,
            size,
            size_slip,
            notional,
            int(cand["edge_bps"]),
            market.start_epoch,
            market.end_epoch,
            start_price,
            spot,
            deviation_bps,
            ret15_bps,
            mark,
            _now_iso(),
            (mark - entry) * size,
            (mark - entry_slip) * size_slip,
            json.dumps(payload, sort_keys=True, default=str),
        ),
    )
    db.commit()


def _mark_positions(db: sqlite3.Connection, price: BinancePrice) -> dict:
    rows = list(db.execute("SELECT * FROM btc5m_ob_positions WHERE status='OPEN' ORDER BY id"))
    if not rows:
        return {"marked": 0, "closed": 0, "open_positions": 0}

    now_ts = time.time()
    settle_delay = _env_i("BTC5M_SETTLE_DELAY_SEC", 8)
    marked = 0
    closed = 0

    for row in rows:
        close_reason = None
        close_price = None
        pnl = float(row["pnl_usd"] or 0)
        pnl_slip = float(row["pnl_usd_slip"] or 0)
        close_response = None

        if now_ts >= float(row["end_epoch"]) + settle_delay:
            try:
                close_spot = price.window_close(int(row["start_epoch"]))
            except Exception as exc:
                logging.getLogger("btc5m-ob-reversion").warning("settle skipped: %s", exc)
                continue

            up_wins = close_spot >= float(row["start_price"])
            winner = "UP" if up_wins else "DOWN"
            close_price = 1.0 if row["side"] == winner else 0.0

            pnl = (close_price - float(row["entry_price"])) * float(row["size_shares"])
            pnl_slip = (close_price - float(row["entry_price_slip"])) * float(row["size_shares_slip"])
            close_reason = "settled_binance_proxy"
            close_response = {
                "winner": winner,
                "close_spot": close_spot,
                "entry_price": row["entry_price"],
                "entry_price_slip": row["entry_price_slip"],
            }

        if close_reason:
            db.execute(
                """
                UPDATE btc5m_ob_positions
                SET status='CLOSED', close_ts_utc=?, close_price=?, close_reason=?,
                    pnl_usd=?, pnl_usd_slip=?, close_response_json=?
                WHERE id=?
                """,
                (
                    _now_iso(),
                    close_price,
                    close_reason,
                    pnl,
                    pnl_slip,
                    json.dumps(close_response, sort_keys=True, default=str),
                    row["id"],
                ),
            )
            closed += 1
        marked += 1

    db.commit()
    return {"marked": marked, "closed": closed, "open_positions": _open_position_count(db)}


def run_once(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice, tg: Notifier | None = None) -> dict:
    now_ts = time.time()
    pos_stats = _mark_positions(db, price)

    summary = {
        "action": "observe",
        "reason": "init",
        "market_slug": None,
        "live_enabled": False,
        "dry_run": True,
        **pos_stats,
    }

    market = _load_current_market(poly, now_ts)
    if not market:
        summary["reason"] = "no_current_market"
        return summary

    summary["market_slug"] = market.slug
    seconds_after_start = now_ts - market.start_epoch
    seconds_left = market.end_epoch - now_ts

    _populate_market_book(poly, market)

    start_price = price.window_open(market.start_epoch)
    spot = price.price()
    deviation = _bps(start_price, spot)
    if deviation is None:
        summary["reason"] = "bad_price"
        return summary

    ret15 = _ret_bps(price, now_ts, _env_i("BTC5M_OB_RET15_MIN", 15))

    vol_1m = price.one_minute_vol(_env_i("BTC5M_VOL_LOOKBACK_MIN", 120))
    fair_up = _fair_probability(start_price, spot, seconds_left, vol_1m)
    fair_down = 1.0 - fair_up

    up = _candidate("UP", fair_up, market)
    down = _candidate("DOWN", fair_down, market)

    prev = _latest_snapshot(db, market.slug)

    total_depth = 0.0
    for c in [up, down]:
        if c:
            total_depth += float(c.get("bid_size") or 0) + float(c.get("ask_size") or 0)

    snap_payload = _snapshot_payload(up, down, spot, prev)
    _insert_snapshot(
        db,
        market,
        up,
        down,
        start_price,
        spot,
        float(deviation),
        ret15,
        seconds_after_start,
        seconds_left,
        snap_payload,
    )

    summary.update({
        "seconds_after_start": round(seconds_after_start, 2),
        "seconds_left": round(seconds_left, 2),
        "deviation_bps": round(float(deviation), 2),
        "ret15_bps": round(float(ret15), 2) if ret15 is not None else None,
        "total_depth": round(total_depth, 2),
    })

    min_left = _env_f("BTC5M_OB_MIN_SECONDS_LEFT", 60.0)
    max_left = _env_f("BTC5M_OB_MAX_SECONDS_LEFT", 150.0)

    if seconds_left > max_left:
        summary["reason"] = "outside_window_too_early"
        _record_attempt(db, market_slug=market.slug, action="observe", reason=summary["reason"],
                        seconds_after_start=seconds_after_start, seconds_left=seconds_left,
                        deviation_bps=deviation, ret15_bps=ret15, response=summary)
        return summary

    if seconds_left < min_left:
        summary["reason"] = "outside_window_too_late"
        _record_attempt(db, market_slug=market.slug, action="observe", reason=summary["reason"],
                        seconds_after_start=seconds_after_start, seconds_left=seconds_left,
                        deviation_bps=deviation, ret15_bps=ret15, response=summary)
        return summary

    if _open_position_count(db) >= _env_i("BTC5M_OB_MAX_OPEN_POSITIONS", 1):
        summary["reason"] = "max_open"
        _record_attempt(db, market_slug=market.slug, action="observe", reason=summary["reason"],
                        seconds_after_start=seconds_after_start, seconds_left=seconds_left,
                        deviation_bps=deviation, ret15_bps=ret15, response=summary)
        return summary

    if _market_already_traded(db, market.slug):
        summary["reason"] = "already_traded_market"
        _record_attempt(db, market_slug=market.slug, action="observe", reason=summary["reason"],
                        seconds_after_start=seconds_after_start, seconds_left=seconds_left,
                        deviation_bps=deviation, ret15_bps=ret15, response=summary)
        return summary

    cand, reason, payload = _choose_reversion_candidate(
        db, market.slug, up, down, float(deviation), ret15, total_depth, prev
    )

    summary["reason"] = reason
    summary["observer_payload"] = payload

    if cand is None:
        _record_attempt(
            db,
            market_slug=market.slug,
            action="observe",
            reason=reason,
            side=payload.get("target_side"),
            entry_price=payload.get("ask"),
            edge_bps=payload.get("edge_bps"),
            seconds_after_start=seconds_after_start,
            seconds_left=seconds_left,
            deviation_bps=deviation,
            ret15_bps=ret15,
            response=summary,
        )
        return summary

    _insert_position(db, market, cand, start_price, spot, float(deviation), ret15, payload)

    entry = float(cand["ask"])
    entry_slip = min(0.99, entry + _env_f("BTC5M_OB_ENTRY_SLIPPAGE", 0.01))

    _record_attempt(
        db,
        market_slug=market.slug,
        action="dry_run_order",
        reason="orderbook_reversion",
        side=cand["side"],
        token_id=cand["token_id"],
        entry_price=entry,
        entry_price_slip=entry_slip,
        notional_usd=_env_f("BTC5M_OB_NOTIONAL_USD", 5.0),
        edge_bps=cand["edge_bps"],
        seconds_after_start=seconds_after_start,
        seconds_left=seconds_left,
        deviation_bps=deviation,
        ret15_bps=ret15,
        response=payload,
    )

    summary["action"] = "dry_run_order"
    summary["reason"] = "orderbook_reversion"
    summary["side"] = cand["side"]
    summary["entry_price"] = entry
    summary["entry_price_slip"] = entry_slip
    summary["edge_bps"] = cand["edge_bps"]

    if tg:
        tg.send_html(
            "<b>BTC5M OB Reversion Observer</b>\n"
            f"{market.slug} {cand['side']} ask={entry:.2f} slip={entry_slip:.2f} "
            f"dev={float(deviation):.2f}bps left={seconds_left:.1f}s"
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
    log = logging.getLogger("btc5m-ob-reversion")

    db = _db(_env_s(
        "BTC5M_OB_DB_PATH",
        os.path.expanduser("~/.local/share/arb-engine-ob-reversion/btc5m-ob-reversion.sqlite"),
    ))
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
            log.info("btc5m ob-reversion done %s", summary)
        except Exception as exc:
            log.warning("btc5m ob-reversion failed: %s", exc)
        if args.once:
            break
        time.sleep(_env_i("BTC5M_INTERVAL_SEC", 10))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
