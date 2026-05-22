"""S9B Sydney observer — S9 + edge max cap for A/B testing.

Run only as package module inside the project, for example:
  python -m btc5m_strategy.arb.btc5m_live_s9b

V9 safety notes:
  - Before any live submit, write live_order_submitting to DB to lock the market.
  - Submit exceptions are treated as live_order_uncertain_submit: no retry, no position insert, manual reconcile required.
  - FAK entry is allowed, but confirmed matched responses with unparseable fill are treated as uncertain and never retried.
  - Exit SELL is FOK-only and must be confirmed matched with verifiable sold shares before the DB marks CLOSED.
  - live_order_submitting / live_order_blocked / live_order_pending / live_order_uncertain_fill / live_order_uncertain_submit lock trading until resolved.
  - Telegram notification uses send_html if available, otherwise send.
  - --resolve-position can manually close or reopen EXIT_UNCERTAIN positions after reconciliation.
  - Uncertain locks scan all unresolved locking attempts, not only the latest 100 rows.
  - --resolve-entry-filled / --resolve-entry-flat close the manual recovery loop for uncertain BUY entries.
  - Exit SELL submit exceptions are marked EXIT_UNCERTAIN to prevent duplicate sell attempts.
  - --resolve-position is guarded by default: only EXIT_UNCERTAIN positions can be changed unless --force is used.
  - --resolve-market refuses unresolved entry-uncertain locks; use explicit entry recovery commands.
  - live_order_failed is a same-market lock only. pending/uncertain/exit_uncertain are global locks.
  - Small-live filters: default edge cap 1200 bps, default abs direction floor 2 bps, block DOWN UTC00-02 and UTC13-15.

S9B = S9 + edge maximum cap (default 1800 bps).
Everything else identical to S9. Observation group, not main version.

Extra rule vs S9:
  6. Skip signals where edge_bps > BTC5M_MAX_EDGE_BPS (default 1200)

Controlled by:
  BTC5M_MAX_EDGE_BPS=1200  (set to 0 or negative to disable)
  BTC5M_MIN_ABS_DIRECTION_BPS=2.0
  BTC5M_BLOCK_DOWN_UTC00_02=true
  BTC5M_BLOCK_DOWN_UTC13_15=true
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


# ── time gate ──────────────────────────────────────────────────────────────

def _time_gate_allowed() -> tuple[bool, str]:
    """Return (allowed, reason).

    True  = current UTC hour is inside the trading window.
    False = blocked with a human-readable reason string.
    """
    enabled = _env_b("BTC5M_TIME_GATE_ENABLED", True)
    if not enabled:
        return True, "time_gate_disabled"

    start = _env_i("BTC5M_TIME_GATE_START_UTC", 8)
    end = _env_i("BTC5M_TIME_GATE_END_UTC", 24)

    now = datetime.now(timezone.utc)
    hour = now.hour + now.minute / 60.0 + now.second / 3600.0

    if start <= hour < end:
        return True, "ok"
    # wrap-around window (e.g. 22-6)
    if start > end:
        if hour >= start or hour < end:
            return True, "ok"

    return False, f"time_gate_blocked(utc={hour:.1f}, window=[{start},{end}))"


def _blocked_side_time_reason(side: str, now_ts: float) -> str | None:
    utc_hour = datetime.fromtimestamp(float(now_ts), timezone.utc).hour
    side_name = str(side).upper()
    if side_name == "DOWN" and _env_b("BTC5M_BLOCK_DOWN_UTC00_02", True) and 0 <= utc_hour < 2:
        return "blocked_down_utc00_02"
    if side_name == "DOWN" and _env_b("BTC5M_BLOCK_DOWN_UTC13_15", True) and 13 <= utc_hour < 15:
        return "blocked_down_utc13_15"
    return None


# ── record / position helpers ──────────────────────────────────────────────

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


def _manual_resolve_exists_after(db: sqlite3.Connection, market_slug: str, attempt_id: int) -> bool:
    row = db.execute(
        """
        SELECT 1
        FROM btc5m_live_attempts
        WHERE market_slug=?
          AND id>?
          AND action='manual_resolved'
        LIMIT 1
        """,
        (market_slug, attempt_id),
    ).fetchone()
    return row is not None


MARKET_LOCK_ACTIONS = (
    "live_order_submitting",
    "live_order_failed",
    "live_order_pending",
    "live_order_uncertain_fill",
    "live_order_uncertain_submit",
    "live_order_blocked",
    "exit_uncertain",
)

GLOBAL_UNCERTAIN_ACTIONS = (
    "live_order_submitting",
    "live_order_pending",
    "live_order_uncertain_fill",
    "live_order_uncertain_submit",
    "exit_uncertain",
)

ENTRY_RECOVERY_ACTIONS = (
    "live_order_submitting",
    "live_order_pending",
    "live_order_uncertain_fill",
    "live_order_uncertain_submit",
    "live_order_failed",
)

ENTRY_RECOVERY_REQUIRED_ACTIONS = (
    "live_order_submitting",
    "live_order_pending",
    "live_order_uncertain_fill",
    "live_order_uncertain_submit",
)


def _unresolved_attempts(db: sqlite3.Connection, market_slug: str | None, actions: tuple[str, ...]) -> list[sqlite3.Row]:
    if not actions:
        return []
    placeholders = ",".join("?" for _ in actions)
    params: list[object] = list(actions)
    market_clause = ""
    if market_slug is not None:
        market_clause = "AND market_slug=?"
        params.append(market_slug)
    rows = db.execute(
        f"""
        SELECT id, ts_utc, market_slug, side, token_id, action, reason, response_json
        FROM btc5m_live_attempts
        WHERE action IN ({placeholders})
          {market_clause}
        ORDER BY id DESC
        """,
        params,
    ).fetchall()
    unresolved = []
    for row in rows:
        row_market = row["market_slug"] if isinstance(row, sqlite3.Row) else row[2]
        row_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        if not row_market or not _manual_resolve_exists_after(db, str(row_market), int(row_id)):
            unresolved.append(row)
    return unresolved


def _market_attempt_exists(db: sqlite3.Connection, market_slug: str) -> bool:
    # Permanent one-trade-per-market protection. These are never unlocked by
    # manual_resolved because the strategy should not trade the same 5-minute
    # market twice after a recorded filled/paper order.
    row = db.execute(
        """
        SELECT 1
        FROM btc5m_live_attempts
        WHERE market_slug=?
          AND action IN ('live_order','dry_run_order')
        LIMIT 1
        """,
        (market_slug,),
    ).fetchone()
    if row is not None:
        return True

    # Same-market safety locks. live_order_failed blocks only this market, while
    # pending/uncertain/exit_uncertain are also handled by the global lock scan.
    # Scan all unresolved locks, not only the newest row, so an older unresolved
    # state cannot be hidden by a later resolved record.
    return bool(_unresolved_attempts(db, market_slug, MARKET_LOCK_ACTIONS))


def _live_uncertain_exists(db: sqlite3.Connection) -> bool:
    """Return True if any unresolved live uncertainty exists.

    This intentionally scans every unresolved locking attempt, not just the
    most recent rows. Trading must stay stopped until an operator reconciles
    CLOB/account state and writes manual_resolved for that market.
    """
    rows = _unresolved_attempts(db, None, GLOBAL_UNCERTAIN_ACTIONS)
    for row in rows:
        market_slug = row['market_slug'] if isinstance(row, sqlite3.Row) else row[2]
        attempt_id = row['id'] if isinstance(row, sqlite3.Row) else row[0]
        action = row['action'] if isinstance(row, sqlite3.Row) else row[5]
        if not market_slug:
            return True
        # live_order_submitting can be auto-resolved if a later terminal order
        # outcome exists for the same market. All other uncertain states require
        # a manual_resolved audit record.
        if action == 'live_order_submitting':
            terminal = db.execute(
                """
                SELECT 1
                FROM btc5m_live_attempts
                WHERE market_slug=?
                  AND id>?
                  AND action IN (
                    'live_order',
                    'live_order_failed',
                    'live_order_pending',
                    'live_order_uncertain_fill',
                    'live_order_uncertain_submit'
                  )
                LIMIT 1
                """,
                (str(market_slug), int(attempt_id)),
            ).fetchone()
            if terminal is not None:
                continue
        return True
    return False


def _open_position_count(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COUNT(*) FROM btc5m_live_positions WHERE status IN ('OPEN','EXIT_UNCERTAIN')").fetchone()
    return int(row[0] or 0)


def _position_exists(db: sqlite3.Connection, market_slug: str) -> bool:
    row = db.execute("SELECT 1 FROM btc5m_live_positions WHERE market_slug=? LIMIT 1", (market_slug,)).fetchone()
    return row is not None


def _json_loads_obj(raw: object) -> object:
    if raw is None or raw == "":
        return {}
    try:
        return json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {"raw": str(raw)}


def _validate_prediction_price(value: float, *, name: str, allow_zero: bool = False) -> float:
    price = float(value)
    if allow_zero:
        ok = 0.0 <= price <= 1.05
    else:
        ok = 0.0 < price <= 1.05
    if not ok:
        lower = "0" if allow_zero else "0+"
        raise SystemExit(f"{name} must be between {lower} and 1.05")
    return price


def _attempt_ts_epoch(row: sqlite3.Row) -> float | None:
    raw = row["ts_utc"]
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _infer_market_start_epoch(row: sqlite3.Row) -> int | None:
    slug = str(row["market_slug"] or "")
    tail = slug.rsplit("-", 1)[-1] if slug else ""
    if tail.isdigit():
        return int(tail)
    ts = _attempt_ts_epoch(row)
    seconds_after = row["seconds_after_start"]
    if ts is not None and seconds_after is not None:
        return int(round(ts - float(seconds_after)))
    return None


def _infer_market_end_epoch(row: sqlite3.Row, start_epoch: int | None) -> int | None:
    ts = _attempt_ts_epoch(row)
    seconds_left = row["seconds_left"]
    if ts is not None and seconds_left is not None:
        return int(round(ts + float(seconds_left)))
    if start_epoch is not None:
        return int(start_epoch) + 300
    return None


def _make_clob_client():
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    host = _env_s("POLY_CLOB_URL", "https://clob.polymarket.com")
    key = _env_s("POLYGON_PRIVATE_KEY", "")
    if not key:
        try:
            key = open(os.path.expanduser("~/.s9b/prvkey")).read().strip()
        except Exception:
            pass
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


def _order_type_from_env(var_name: str = "BTC5M_ORDER_TYPE", default: str = "FOK"):
    """Market-order type gate.

    Polymarket market orders should use only FOK or FAK. GTC/GTD can rest on
    the book, which is not acceptable for this 5-minute taker strategy.
    """
    from py_clob_client_v2.clob_types import OrderType

    name = _env_s(var_name, default).upper().strip()
    if name not in ("FOK", "FAK"):
        raise RuntimeError(f"Unsupported {var_name}={name}; expected FOK or FAK only")
    return getattr(OrderType, name), name


def _exit_order_type():
    """Exit orders are intentionally FOK-only.

    SELL market orders use amount=shares. If FAK is allowed on exits and only
    part of the shares are sold, the current position schema cannot safely
    represent the residual shares. Keep exits FOK until partial-close accounting
    is implemented.
    """
    order_type, name = _order_type_from_env("BTC5M_EXIT_ORDER_TYPE", "FOK")
    if name != "FOK":
        raise RuntimeError("BTC5M_EXIT_ORDER_TYPE must be FOK unless partial-close accounting is implemented")
    return order_type


def _submit_market(token_id: str, amount: float, side: str, price: float, order_type=None) -> dict:
    from py_clob_client_v2.clob_types import MarketOrderArgsV2

    client = _make_clob_client()
    args = MarketOrderArgsV2(token_id=str(token_id), amount=float(amount), side=side, price=float(price))
    if order_type is None:
        order_type, _ = _order_type_from_env("BTC5M_ORDER_TYPE", "FOK")
    return client.create_and_post_market_order(args, order_type=order_type)


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


def _notify(tg: Notifier | None, message: str) -> None:
    """Send notification without assuming a specific notifier method."""
    if not tg:
        return
    try:
        if hasattr(tg, "send_html"):
            tg.send_html(message)
        elif hasattr(tg, "send"):
            tg.send(message)
        else:
            logging.getLogger("btc5m-live-s9b").warning("notifier has no send/send_html method")
    except Exception:
        logging.getLogger("btc5m-live-s9b").exception("notification failed")


# -- slippage-retry / fill parsing -----------------------------------------

def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _fixed6_amount(x):
    """Parse Polymarket fixed-math amount strings such as '100000000' -> 100.0.

    Do not use this for normal decimal prices. It is intended only for
    makingAmount/takingAmount-like integer amount fields.
    """
    if x is None or x == "":
        return None
    try:
        s = str(x).strip()
        if not s:
            return None
        # Hex strings / non-decimal payloads are not amount fields.
        if s.startswith("0x"):
            return None
        # Polymarket response amount fields are fixed 6-decimal integer strings.
        if s.lstrip("-").isdigit():
            return float(int(s)) / 1_000_000.0
        # Fallback for SDKs that may already expose decimal strings.
        return float(s)
    except Exception:
        return None


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _response_status(response) -> str | None:
    """Return the first status-like field found in a CLOB response.

    Polymarket order responses may include status values such as live, matched,
    delayed, or unmatched. For FAK/FOK fill accounting, only confirmed matched
    states can be treated as a position when the fill is inferred from order
    amount fields.
    """
    if not isinstance(response, dict):
        return None
    for d in _walk_dicts(response):
        if not isinstance(d, dict):
            continue
        for key in ("status", "order_status", "orderStatus"):
            value = d.get(key)
            if value is not None and str(value).strip():
                return str(value).strip().lower()
    return None


def _status_is_confirmed_match(status: str | None) -> bool:
    if status is None:
        return False
    return status.lower() in {"matched", "filled", "partially_filled", "partially-filled"}


def _status_is_pending_or_unmatched(status: str | None) -> bool:
    if status is None:
        return False
    return status.lower() in {"live", "delayed", "unmatched", "open", "pending", "pending_match"}


def _status_is_uncertain_pending(status: str | None) -> bool:
    # States that may later change or do not conclusively prove flat/no-fill.
    # Do not retry blindly after these states.
    if status is None:
        return False
    return status.lower() in {"live", "delayed", "open", "pending", "pending_match"}


def _status_is_no_fill(status: str | None) -> bool:
    if status is None:
        return False
    return status.lower() in {"unmatched"}


def _maybe_normalized_amount(x):
    """Best-effort parser for nested trade size fields.

    Trade objects may expose decimal sizes, while raw CLOB response
    making/taking fields are fixed-6 integers. If a value is a huge integer
    string, treat it as fixed-6; otherwise treat it as decimal.
    """
    if x is None or x == "":
        return None
    s = str(x).strip()
    if not s:
        return None
    try:
        if s.startswith("0x"):
            return None
        if s.lstrip("-").isdigit() and abs(int(s)) >= 100_000:
            return float(int(s)) / 1_000_000.0
        return float(s)
    except Exception:
        return None


def _pair_from_making_taking(making, taking, requested_notional: float, limit_price: float) -> dict | None:
    """Infer filled notional/shares from fixed-6 making/taking fields.

    We do not blindly assume takingAmount is dollars. We evaluate both possible
    mappings and choose the one whose implied average price is closest to the
    order's limit price and whose notional is not wildly above the request.
    """
    if making is None or taking is None or making <= 0 or taking <= 0:
        return None

    candidates = []
    # Candidate A: making = notional, taking = shares
    candidates.append((making, taking, "making_notional_taking_shares"))
    # Candidate B: taking = notional, making = shares
    candidates.append((taking, making, "taking_notional_making_shares"))

    best = None
    best_score = None
    for notional, shares, source in candidates:
        if notional <= 0 or shares <= 0:
            continue
        avg = notional / shares
        # Prices should be within prediction-token range, plus small slippage margin.
        if not (0 < avg <= 1.05):
            continue
        # BUY market order notional should not materially exceed the requested spend.
        if notional > float(requested_notional) * 1.25:
            continue
        score = abs(avg - float(limit_price))
        if best is None or score < best_score:
            best = {
                "filled_notional": float(notional),
                "filled_shares": float(shares),
                "avg_price": float(avg),
                "source": source,
            }
            best_score = score
    return best


def _parse_fill_from_response(response, requested_notional: float, price: float, order_type_name: str) -> dict:
    """Parse actual fill from a CLOB order response.

    Strict rules:
      - If status is live/delayed/unmatched/open/pending, do NOT infer a fill.
      - makingAmount/takingAmount are fixed-6 order amount fields and are only
        used when status is a confirmed matched state.
      - FAK must prove positive shares before a position is inserted.
      - FOK may assume full fill only when the response is successful and there
        is no explicit non-matched status.
    """
    result = {
        "filled_shares": 0.0,
        "filled_notional": 0.0,
        "avg_price": float(price),
        "source": "none",
        "status": None,
    }

    if not isinstance(response, dict):
        return result

    status = _response_status(response)
    result["status"] = status

    # A delayed/live/unmatched response is not a confirmed fill. Do not insert
    # a position based on making/taking fields that may only describe the order.
    if _status_is_pending_or_unmatched(status):
        result["source"] = f"status_{status}_not_confirmed"
        return result

    # 1) Prefer explicit fills/trades when SDK exposes them. If a response has
    # explicit fills, they are the strongest evidence. Pending statuses were
    # already rejected above.
    for d in _walk_dicts(response):
        for key in ("trades", "fills", "matches"):
            trades = d.get(key)
            if isinstance(trades, list) and trades:
                shares_total = 0.0
                notional_total = 0.0
                for t in trades:
                    if not isinstance(t, dict):
                        continue
                    sz = (
                        _maybe_normalized_amount(t.get("size"))
                        or _maybe_normalized_amount(t.get("shares"))
                        or _maybe_normalized_amount(t.get("filled_size"))
                        or _maybe_normalized_amount(t.get("matched_size"))
                    )
                    pr = _num(t.get("price")) or float(price)
                    if sz and pr and sz > 0 and pr > 0:
                        shares_total += sz
                        notional_total += sz * pr
                if shares_total > 0 and notional_total > 0:
                    return {
                        "filled_shares": shares_total,
                        "filled_notional": notional_total,
                        "avg_price": notional_total / shares_total,
                        "source": key,
                        "status": status,
                    }

    confirmed = _status_is_confirmed_match(status)

    # 2) Parse raw Polymarket makingAmount/takingAmount as fixed-6 only when
    # status confirms a matched order. These fields can exist on live/delayed
    # orders and are not by themselves proof of a fill.
    if confirmed:
        for d in _walk_dicts(response):
            making = _fixed6_amount(d.get("makingAmount") or d.get("making_amount"))
            taking = _fixed6_amount(d.get("takingAmount") or d.get("taking_amount"))
            pair = _pair_from_making_taking(making, taking, float(requested_notional), float(price))
            if pair:
                pair["status"] = status
                return pair

    # 3) Parse direct explicit filled shares/notional fields, but only when
    # status is matched or no status is present. If status is explicitly not
    # matched, we would already have returned above.
    if confirmed or status is None:
        for d in _walk_dicts(response):
            shares = (
                _maybe_normalized_amount(d.get("filled_shares"))
                or _maybe_normalized_amount(d.get("filled_size"))
                or _maybe_normalized_amount(d.get("matched_size"))
                or _maybe_normalized_amount(d.get("size_matched"))
            )
            avg_price = _num(d.get("avg_price")) or _num(d.get("average_price")) or _num(d.get("price")) or float(price)
            if shares and shares > 0 and avg_price and avg_price > 0:
                return {
                    "filled_shares": shares,
                    "filled_notional": shares * avg_price,
                    "avg_price": avg_price,
                    "source": "direct_shares",
                    "status": status,
                }

    # 4) FOK only: if the API says success and does not expose a non-matched
    # status, assume full fill. Never do this for FAK.
    if order_type_name == "FOK" and _order_response_ok(response) and not _status_is_pending_or_unmatched(status):
        return {
            "filled_notional": float(requested_notional),
            "filled_shares": float(requested_notional) / float(price),
            "avg_price": float(price),
            "source": "fok_assumed_full",
            "status": status,
        }

    return result

def _submit_market_with_retry(token_id, notional, side, base_price, edge_bps, max_entry):
    """FOK/FAK entry with slippage retry.

    Returns:
      response, final_limit_price, filled_notional, filled_shares, avg_price, ok

    V4 entry-safety rules:
      - Any positive, provable filled_shares is a real position and must be DB-recorded.
      - status=matched with unparseable fill is UNCERTAIN: do not retry, do not insert a fake position.
      - status=live/delayed/open/pending is PENDING: do not retry blindly.
      - status=unmatched/no-fill can retry with slippage if edge remains acceptable.
    """
    slippage = _env_f("BTC5M_ENTRY_SLIPPAGE", 0.01)
    max_retries = _env_i("BTC5M_MAX_ORDER_RETRIES", 1)
    min_edge_after = _env_i("BTC5M_MIN_EDGE_BPS_AFTER_SLIPPAGE", 500)
    min_filled = _env_f("BTC5M_MIN_FILLED_NOTIONAL_USD", 1.0)
    order_type, order_type_name = _order_type_from_env("BTC5M_ORDER_TYPE", "FOK")

    price = float(base_price)
    last_response = None
    last_fill = {"filled_notional": 0.0, "filled_shares": 0.0, "avg_price": price}

    for attempt in range(max_retries + 1):
        try:
            response = _submit_market(str(token_id), float(notional), str(side), price, order_type=order_type)
        except Exception as exc:
            # A submit exception/timeout can happen after the order reached the
            # exchange. Retrying could duplicate the buy. Treat it as uncertain
            # and require operator reconciliation.
            response = {
                "error": "exception",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:500],
                "exception_repr": repr(exc)[:500],
                "attempt": attempt,
                "price": price,
                "order_type": order_type_name,
                "block_reason": "uncertain_submit_exception",
                "requires_manual_reconcile": True,
            }
            return response, price, 0.0, 0.0, price, False

        fill = _parse_fill_from_response(response, float(notional), price, order_type_name)
        filled_notional = float(fill.get("filled_notional") or 0.0)
        filled_shares = float(fill.get("filled_shares") or 0.0)
        avg_price = float(fill.get("avg_price") or price)
        status = fill.get("status")

        response["fill_parse"] = fill
        response["filled_notional"] = round(filled_notional, 6)
        response["filled_shares"] = round(filled_shares, 8)
        response["avg_price"] = round(avg_price, 6)
        response["order_type"] = order_type_name
        response["attempt"] = attempt
        response["below_min_fill"] = bool(0 < filled_notional < min_filled)
        response["min_filled_notional"] = min_filled

        last_response = response
        last_fill = {
            "filled_notional": filled_notional,
            "filled_shares": filled_shares,
            "avg_price": avg_price,
        }

        # Any positive fill is a real position. Do not discard dust fills.
        if _order_response_ok(response) and filled_shares > 0 and filled_notional > 0:
            return response, price, filled_notional, filled_shares, avg_price, True

        # Matched but unparseable is the most dangerous state: the order may
        # have filled, but local code cannot prove amount/shares. Stop and lock.
        if _order_response_ok(response) and _status_is_confirmed_match(status) and filled_shares <= 0:
            response["block_reason"] = "uncertain_fill_confirmed_match_unparseable"
            response["requires_manual_reconcile"] = True
            return response, price, 0.0, 0.0, price, False

        # Pending/delayed/live is not conclusively flat. Do not retry blindly.
        if _status_is_uncertain_pending(status):
            response["block_reason"] = f"unconfirmed_order_status_{status}"
            response["requires_manual_reconcile"] = True
            return response, price, 0.0, 0.0, price, False

        if attempt >= max_retries:
            if _order_response_ok(response):
                response["block_reason"] = "no_fill"
            return response, price, filled_notional, filled_shares, avg_price, False

        next_price = round(float(base_price) + slippage, 2)
        if next_price > float(max_entry):
            response["block_reason"] = "slippage_exceeds_max_entry"
            response["next_price"] = next_price
            response["max_entry"] = float(max_entry)
            return response, price, 0.0, 0.0, price, False

        slip_bps = int(slippage / float(base_price) * 10_000.0)
        edge_after = int(edge_bps) - slip_bps
        if edge_after < min_edge_after:
            response["block_reason"] = "edge_below_min_after_slippage"
            response["edge_after_slip"] = edge_after
            response["min_edge_after"] = min_edge_after
            response["slip_bps_penalty"] = slip_bps
            return response, price, 0.0, 0.0, price, False

        price = next_price

    return last_response or {}, price, float(last_fill["filled_notional"]), float(last_fill["filled_shares"]), float(last_fill["avg_price"]), False

def _pair_from_making_taking_for_sell(making, taking, expected_shares: float, limit_price: float) -> dict | None:
    """Infer sold shares/notional from fixed-6 making/taking fields for SELL.

    SELL market order uses amount=shares and price=worst acceptable price. We
    evaluate both mappings and choose the one whose average price is closest to
    the limit and whose sold shares are not wildly above expected_shares.
    """
    if making is None or taking is None or making <= 0 or taking <= 0:
        return None
    candidates = [
        # Candidate A: making = shares, taking = notional
        (making, taking, "making_shares_taking_notional"),
        # Candidate B: taking = shares, making = notional
        (taking, making, "taking_shares_making_notional"),
    ]
    best = None
    best_score = None
    for shares, notional, source in candidates:
        if shares <= 0 or notional <= 0:
            continue
        avg = notional / shares
        if not (0 < avg <= 1.05):
            continue
        if shares > float(expected_shares) * 1.25:
            continue
        score = abs(avg - float(limit_price))
        if best is None or score < best_score:
            best = {
                "sold_shares": float(shares),
                "sold_notional": float(notional),
                "avg_price": float(avg),
                "source": source,
            }
            best_score = score
    return best


def _parse_sell_fill_from_response(response, expected_shares: float, price: float) -> dict:
    """Parse actual SELL fill.

    Exit is FOK-only. We still require status=matched plus verifiable sold
    shares before marking DB CLOSED. If matched but unparseable, return zero
    shares and let caller mark EXIT_UNCERTAIN / require manual reconcile.
    """
    result = {
        "sold_shares": 0.0,
        "sold_notional": 0.0,
        "avg_price": float(price),
        "source": "none",
        "status": None,
    }
    if not isinstance(response, dict):
        return result

    status = _response_status(response)
    result["status"] = status

    if not _status_is_confirmed_match(status):
        result["source"] = f"status_{status}_not_confirmed"
        return result

    for d in _walk_dicts(response):
        for key in ("trades", "fills", "matches"):
            trades = d.get(key)
            if isinstance(trades, list) and trades:
                shares_total = 0.0
                notional_total = 0.0
                for t in trades:
                    if not isinstance(t, dict):
                        continue
                    sz = (
                        _maybe_normalized_amount(t.get("size"))
                        or _maybe_normalized_amount(t.get("shares"))
                        or _maybe_normalized_amount(t.get("filled_size"))
                        or _maybe_normalized_amount(t.get("matched_size"))
                    )
                    pr = _num(t.get("price")) or float(price)
                    if sz and pr and sz > 0 and pr > 0:
                        shares_total += sz
                        notional_total += sz * pr
                if shares_total > 0 and notional_total > 0:
                    return {
                        "sold_shares": shares_total,
                        "sold_notional": notional_total,
                        "avg_price": notional_total / shares_total,
                        "source": key,
                        "status": status,
                    }

    for d in _walk_dicts(response):
        making = _fixed6_amount(d.get("makingAmount") or d.get("making_amount"))
        taking = _fixed6_amount(d.get("takingAmount") or d.get("taking_amount"))
        pair = _pair_from_making_taking_for_sell(making, taking, float(expected_shares), float(price))
        if pair:
            pair["status"] = status
            return pair

    for d in _walk_dicts(response):
        shares = (
            _maybe_normalized_amount(d.get("sold_shares"))
            or _maybe_normalized_amount(d.get("filled_shares"))
            or _maybe_normalized_amount(d.get("filled_size"))
            or _maybe_normalized_amount(d.get("matched_size"))
            or _maybe_normalized_amount(d.get("size_matched"))
        )
        avg_price = _num(d.get("avg_price")) or _num(d.get("average_price")) or _num(d.get("price")) or float(price)
        if shares and shares > 0 and avg_price and avg_price > 0:
            return {
                "sold_shares": shares,
                "sold_notional": shares * avg_price,
                "avg_price": avg_price,
                "source": "direct_shares",
                "status": status,
            }

    return result


def _exit_response_ok(response, expected_shares: float, limit_price: float) -> tuple[bool, dict]:
    """Return (ok, parsed_sell_fill). Requires full-ish verified FOK exit."""
    fill = _parse_sell_fill_from_response(response, expected_shares, limit_price)
    sold_shares = float(fill.get("sold_shares") or 0.0)
    tolerance = _env_f("BTC5M_EXIT_SHARE_TOLERANCE", 0.000001)
    ok = (
        _order_response_ok(response)
        and _status_is_confirmed_match(fill.get("status"))
        and sold_shares + tolerance >= float(expected_shares)
    )
    return ok, fill


def _submit_exit_fok_with_uncertain(token_id: str, shares: float, limit_price: float) -> tuple[object, bool, dict]:
    try:
        response = _submit_market(token_id, shares, "SELL", limit_price, order_type=_exit_order_type())
    except Exception as exc:
        response = {
            "error": "exception",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc)[:500],
            "exception_repr": repr(exc)[:500],
            "block_reason": "exit_submit_exception_uncertain",
            "requires_manual_reconcile": True,
            "order_type": "FOK",
            "side": "SELL",
            "token_id": str(token_id),
            "shares": float(shares),
            "limit_price": float(limit_price),
        }
        fill = {
            "sold_shares": 0.0,
            "sold_notional": 0.0,
            "avg_price": float(limit_price),
            "source": "submit_exception",
            "status": None,
        }
        return response, False, fill

    ok, fill = _exit_response_ok(response, shares, limit_price)
    if isinstance(response, dict) and not ok:
        response["requires_manual_reconcile"] = True
        response["block_reason"] = response.get("block_reason") or "exit_unverified_or_unmatched"
        response["order_type"] = "FOK"
    return response, ok, fill


def _mark_exit_uncertain(db: sqlite3.Connection, row: sqlite3.Row, mark: float, pnl: float, close_response: dict) -> None:
    db.execute(
        """
        UPDATE btc5m_live_positions
        SET status='EXIT_UNCERTAIN', last_mark_price=?, last_mark_ts_utc=?, pnl_usd=?,
            close_reason='exit_uncertain', close_response_json=?
        WHERE id=?
        """,
        (mark, _now_iso(), pnl, json.dumps(close_response, sort_keys=True, default=str), row["id"]),
    )
    db.commit()


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
    size_shares: float | None = None,
    entry_price: float | None = None,
) -> None:
    entry = float(entry_price if entry_price is not None else candidate["ask"])
    if entry <= 0:
        raise RuntimeError(f"invalid entry price for position insert: {entry}")
    size = float(size_shares) if size_shares is not None else float(notional) / entry
    if size <= 0 or float(notional) <= 0:
        raise RuntimeError(f"refusing to insert zero/negative position: notional={notional}, shares={size}")
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
            float(notional),
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


# ── S9: hold-to-expiry only ────────────────────────────────────────────────
#   Stop-loss, tail-force-exit, pre-settle-loss-cap, disaster-trail
#   are all DISABLED by default env vars in the launcher script.
#   Code retains the logic paths for observability, but they will
#   never fire when env vars are set to false/0.

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
    # S9 defaults: all exits disabled
    tail_force_exit_sec = _env_i("BTC5M_TAIL_FORCE_EXIT_SEC", 0)
    pre_enabled = _env_b("BTC5M_PRE_SETTLE_LOSS_CAP_ENABLED", False)
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
        seconds_left = float(row["end_epoch"]) - now_ts
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
                order_response, exit_ok, exit_fill = _submit_exit_fok_with_uncertain(token_id, size, close_price)
                close_response["order_response"] = order_response
                close_response["exit_fill_parse"] = exit_fill
                if not exit_ok:
                    close_response["requires_manual_reconcile"] = True
                    _record_attempt(
                        db,
                        market_slug=row["market_slug"],
                        side=row["side"],
                        token_id=token_id,
                        action="exit_uncertain",
                        reason="tail_force_exit_unverified",
                        edge_bps=row["edge_bps"],
                        direction_bps=row["direction_bps"],
                        seconds_after_start=None,
                        seconds_left=seconds_left,
                        entry_price=entry,
                        notional_usd=notional,
                        fair_prob=None,
                        response=close_response,
                    )
                    _mark_exit_uncertain(db, row, mark, pnl, close_response)
                    logging.getLogger("btc5m-live-s9b").warning(
                        "tail-force close uncertain for position %s: %s",
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
                    order_response, exit_ok, exit_fill = _submit_exit_fok_with_uncertain(token_id, size, close_price)
                    close_response["order_response"] = order_response
                    close_response["exit_fill_parse"] = exit_fill
                    if not exit_ok:
                        close_response["requires_manual_reconcile"] = True
                        _record_attempt(
                            db,
                            market_slug=row["market_slug"],
                            side=row["side"],
                            token_id=token_id,
                            action="exit_uncertain",
                            reason="pre_settle_exit_unverified",
                            edge_bps=row["edge_bps"],
                            direction_bps=row["direction_bps"],
                            seconds_after_start=None,
                            seconds_left=seconds_left,
                            entry_price=entry,
                            notional_usd=notional,
                            fair_prob=None,
                            response=close_response,
                        )
                        _mark_exit_uncertain(db, row, mark, pnl, close_response)
                        logging.getLogger("btc5m-live-s9b").warning(
                            "pre-settle close uncertain for position %s: %s",
                            row["id"],
                            order_response,
                        )
                        close_reason = None
        elif stop_loss_enabled and notional > 0 and pnl <= -notional * stop_loss:
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
                order_response, exit_ok, exit_fill = _submit_exit_fok_with_uncertain(token_id, size, close_price)
                close_response["order_response"] = order_response
                close_response["exit_fill_parse"] = exit_fill
                if not exit_ok:
                    close_response["requires_manual_reconcile"] = True
                    _record_attempt(
                        db,
                        market_slug=row["market_slug"],
                        side=row["side"],
                        token_id=token_id,
                        action="exit_uncertain",
                        reason="stop_loss_exit_unverified",
                        edge_bps=row["edge_bps"],
                        direction_bps=row["direction_bps"],
                        seconds_after_start=None,
                        seconds_left=seconds_left,
                        entry_price=entry,
                        notional_usd=notional,
                        fair_prob=None,
                        response=close_response,
                    )
                    _mark_exit_uncertain(db, row, mark, pnl, close_response)
                    logging.getLogger("btc5m-live-s9b").warning(
                        "stop-loss close uncertain for position %s: %s",
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


# ── main loop ──────────────────────────────────────────────────────────────

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

    if _live_uncertain_exists(db):
        summary["reason"] = "live_uncertain_reconcile_required"
        return summary

    # ── S9 time gate ──
    time_ok, time_reason = _time_gate_allowed()
    if not time_ok:
        summary["reason"] = time_reason
        return summary

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
    trend_threshold_bps = _env_f("BTC5M_TREND_THRESHOLD_BPS", 2.0)
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
    max_edge = _env_i("BTC5M_MAX_EDGE_BPS", 1200)  # V9: tightened edge cap
    min_abs_direction = _env_f("BTC5M_MIN_ABS_DIRECTION_BPS", 2.0)
    min_direction = _env_f("BTC5M_MIN_DIRECTION_BPS", 0.0)
    max_spread = _env_f("BTC5M_MAX_SPREAD_PCT", 0.03)
    max_entry = _env_f("BTC5M_MAX_ENTRY_PRICE", 0.82)
    min_depth = _env_f("BTC5M_MIN_ASK_DEPTH_SHARES", 10.0)
    max_notional = _env_f("BTC5M_MAX_NOTIONAL_USD", 5.0)
    if int(best["edge_bps"]) < min_edge:
        summary["reason"] = "edge_too_small"
    elif max_edge > 0 and int(best["edge_bps"]) > max_edge:
        summary["reason"] = "edge_too_high"
    elif min_abs_direction > 0 and abs(float(direction)) < min_abs_direction:
        summary["reason"] = "abs_direction_too_small"
    elif _blocked_side_time_reason(str(best["side"]), now_ts):
        summary["reason"] = _blocked_side_time_reason(str(best["side"]), now_ts)
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
                if not _env_b("BTC5M_ALLOW_LIVE_ORDERS", False):
                    response["safety_block"] = "BTC5M_ALLOW_LIVE_ORDERS is not true"
                    _record_attempt(
                        db,
                        market_slug=market.slug,
                        side=best["side"],
                        token_id=best["token_id"],
                        action="live_order_blocked",
                        reason="live_safety_not_allowed",
                        edge_bps=best["edge_bps"],
                        direction_bps=direction,
                        seconds_after_start=seconds_after_start,
                        seconds_left=seconds_left,
                        entry_price=best["ask"],
                        notional_usd=0,
                        fair_prob=best["fair"],
                        response=response,
                    )
                    summary["action"] = "live_order_blocked"
                    summary["reason"] = "live_safety_not_allowed"
                    return summary

                submit_response = {
                    **response,
                    "phase": "before_submit",
                    "order_type": _env_s("BTC5M_ORDER_TYPE", "FOK").upper().strip(),
                    "intended_notional": notional,
                    "intended_price": best["ask"],
                }
                _record_attempt(
                    db,
                    market_slug=market.slug,
                    side=best["side"],
                    token_id=best["token_id"],
                    action="live_order_submitting",
                    reason="before_submit",
                    edge_bps=best["edge_bps"],
                    direction_bps=direction,
                    seconds_after_start=seconds_after_start,
                    seconds_left=seconds_left,
                    entry_price=best["ask"],
                    notional_usd=notional,
                    fair_prob=best["fair"],
                    response=submit_response,
                )

                order_response, final_price, filled_notional, filled_shares, avg_price, ok = _submit_market_with_retry(
                    best["token_id"], notional, "BUY",
                    best["ask"], best["edge_bps"], max_entry,
                )
                response["dry_run"] = False
                response["live_enabled"] = True
                response["order_response"] = order_response
                response["final_price"] = final_price
                response["avg_price"] = avg_price
                response["slippage_used"] = round(avg_price - float(best["ask"]), 4)
                response["filled_notional"] = round(filled_notional, 6)
                response["filled_shares"] = round(filled_shares, 8)
                response["order_type"] = _env_s("BTC5M_ORDER_TYPE", "FOK").upper().strip()

                if not ok or filled_notional <= 0 or filled_shares <= 0:
                    block_reason = str(order_response.get("block_reason", "")) if isinstance(order_response, dict) else ""
                    if "uncertain_submit" in block_reason:
                        action_failed = "live_order_uncertain_submit"
                        reason_failed = "uncertain_submit"
                    elif "uncertain_fill" in block_reason:
                        action_failed = "live_order_uncertain_fill"
                        reason_failed = "uncertain_fill"
                    elif "unconfirmed_order_status" in block_reason:
                        action_failed = "live_order_pending"
                        reason_failed = "pending_or_delayed"
                    else:
                        action_failed = "live_order_failed"
                        reason_failed = "no_fill" if filled_notional <= 0 or filled_shares <= 0 else "submit_failed"
                    _record_attempt(
                        db,
                        market_slug=market.slug,
                        side=best["side"],
                        token_id=best["token_id"],
                        action=action_failed,
                        reason=reason_failed,
                        edge_bps=best["edge_bps"],
                        direction_bps=direction,
                        seconds_after_start=seconds_after_start,
                        seconds_left=seconds_left,
                        entry_price=final_price,
                        notional_usd=filled_notional,
                        fair_prob=best["fair"],
                        response=response,
                    )
                    summary["action"] = action_failed
                    summary["reason"] = reason_failed
                    return summary

                action = "live_order"
                reason = "submitted_below_min_fill" if bool(order_response.get("below_min_fill")) else "submitted"
                actual_notional = filled_notional
                actual_shares = filled_shares
                actual_entry = avg_price
                if avg_price != float(best["ask"]):
                    best = dict(best)
                    best["ask"] = avg_price
            else:
                actual_notional = notional
                actual_shares = None
                actual_entry = float(best["ask"])

            _insert_position(
                db,
                market,
                best,
                start_price,
                spot,
                actual_notional,
                direction,
                response,
                size_shares=actual_shares,
                entry_price=actual_entry,
            )
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
                entry_price=actual_entry,
                notional_usd=actual_notional,
                fair_prob=best["fair"],
                response=response,
            )
            summary["action"] = action
            summary["reason"] = reason
            _notify(
                tg,
                "BTC5M S9B Sydney candidate\n"
                f"mode={'LIVE' if live_enabled and not dry_run else 'DRY'}\n"
                f"{market.slug} {best['side']} edge={best['edge_bps']} dir={direction:.2f} "
                f"ask={best['ask']:.2f} notional=${actual_notional:.2f}"
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



def _print_uncertain(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """
        SELECT id, ts_utc, market_slug, side, action, reason, entry_price, notional_usd, response_json
        FROM btc5m_live_attempts
        WHERE action IN (
          'live_order_submitting',
          'live_order_pending',
          'live_order_uncertain_fill',
          'live_order_uncertain_submit',
          'exit_uncertain',
          'live_order_blocked',
          'live_order_failed'
        )
        ORDER BY id DESC
        """
    ).fetchall()

    printed = 0
    for r in rows:
        market = r["market_slug"]
        resolved = bool(market and _manual_resolve_exists_after(db, str(market), int(r["id"])))
        if resolved:
            continue
        printed += 1
        print("-" * 100)
        print("type: attempt")
        print("id:", r["id"])
        print("ts_utc:", r["ts_utc"])
        print("market_slug:", market)
        print("side:", r["side"])
        print("action:", r["action"])
        print("reason:", r["reason"])
        print("entry_price:", r["entry_price"])
        print("notional_usd:", r["notional_usd"])

    pos_rows = db.execute(
        """
        SELECT id, market_slug, side, status, entry_price, size_shares, notional_usd,
               pnl_usd, close_price, close_reason, open_ts_utc, close_ts_utc
        FROM btc5m_live_positions
        WHERE status='EXIT_UNCERTAIN'
        ORDER BY id DESC
        """
    ).fetchall()
    for p in pos_rows:
        printed += 1
        print("-" * 100)
        print("type: position")
        print("id:", p["id"])
        print("market_slug:", p["market_slug"])
        print("side:", p["side"])
        print("status:", p["status"])
        print("entry_price:", p["entry_price"])
        print("size_shares:", p["size_shares"])
        print("notional_usd:", p["notional_usd"])
        print("pnl_usd:", p["pnl_usd"])
        print("close_price:", p["close_price"])
        print("close_reason:", p["close_reason"])
        print("open_ts_utc:", p["open_ts_utc"])
        print("close_ts_utc:", p["close_ts_utc"])

    if printed == 0:
        print("no unresolved uncertain/locked attempts or EXIT_UNCERTAIN positions found")


def _manual_resolve_market(db: sqlite3.Connection, market_slug: str, note: str | None = None) -> None:
    if not market_slug:
        raise SystemExit("--resolve-market requires a market slug")
    entry_required = _unresolved_attempts(db, market_slug, ENTRY_RECOVERY_REQUIRED_ACTIONS)
    if entry_required:
        details = ", ".join(
            f"{int(row['id'] if isinstance(row, sqlite3.Row) else row[0])}:"
            f"{row['action'] if isinstance(row, sqlite3.Row) else row[5]}"
            for row in entry_required
        )
        raise SystemExit(
            "--resolve-market refuses unresolved entry uncertainty for "
            f"{market_slug} ({details}). Use --resolve-entry-filled <attempt_id> "
            "or --resolve-entry-flat <attempt_id> after checking the CLOB account."
        )
    payload = {
        "note": note or "manual operator resolved uncertain state",
        "resolved_market_slug": market_slug,
        "operator_action": "manual_resolved",
    }
    _record_attempt(
        db,
        market_slug=market_slug,
        side=None,
        token_id=None,
        action="manual_resolved",
        reason="operator_reconciled",
        edge_bps=None,
        direction_bps=None,
        seconds_after_start=None,
        seconds_left=None,
        entry_price=None,
        notional_usd=0,
        fair_prob=None,
        response=payload,
    )
    print(f"manual_resolved written for {market_slug}")


def _load_attempt_for_entry_recovery(db: sqlite3.Connection, attempt_id: int, force: bool) -> sqlite3.Row:
    row = db.execute("SELECT * FROM btc5m_live_attempts WHERE id=?", (int(attempt_id),)).fetchone()
    if row is None:
        raise SystemExit(f"attempt id not found: {attempt_id}")
    action = str(row["action"] or "")
    if action not in ENTRY_RECOVERY_ACTIONS and not force:
        raise SystemExit(
            f"attempt {attempt_id} action={action!r} is not entry-recoverable; use --force only after manual audit"
        )
    if not row["market_slug"] or not row["side"] or not row["token_id"]:
        raise SystemExit(f"attempt {attempt_id} is missing market/side/token_id and cannot recover an entry")
    return row


def _manual_resolve_entry_flat(
    db: sqlite3.Connection,
    attempt_id: int,
    note: str | None = None,
    force: bool = False,
) -> None:
    row = _load_attempt_for_entry_recovery(db, attempt_id, force)
    market_slug = str(row["market_slug"])
    if _position_exists(db, market_slug) and not force:
        raise SystemExit(f"position already exists for {market_slug}; refusing flat resolution without --force")
    if _manual_resolve_exists_after(db, market_slug, int(attempt_id)) and not force:
        raise SystemExit(f"attempt {attempt_id} already has a later manual_resolved record")
    payload = {
        "note": note or "manual operator confirmed entry was flat",
        "operator_action": "manual_resolve_entry_flat",
        "attempt_id": int(attempt_id),
        "original_action": row["action"],
        "original_response": _json_loads_obj(row["response_json"]),
    }
    _record_attempt(
        db,
        market_slug=market_slug,
        side=row["side"],
        token_id=row["token_id"],
        action="manual_resolved",
        reason="operator_resolved_entry_flat",
        edge_bps=row["edge_bps"],
        direction_bps=row["direction_bps"],
        seconds_after_start=None,
        seconds_left=None,
        entry_price=row["entry_price"],
        notional_usd=0,
        fair_prob=row["fair_prob"],
        response=payload,
    )
    print(f"entry attempt {attempt_id} resolved flat; manual_resolved written for {market_slug}")


def _manual_recovery_start_price(row: sqlite3.Row, start_epoch: int, provided: float | None) -> float:
    if provided is not None:
        price = float(provided)
        if price <= 0:
            raise SystemExit("--start-price must be positive")
        return price
    try:
        price_feed = BinancePrice(_env_s("BTC5M_BINANCE_URL", "https://api.binance.com"))
        return float(price_feed.window_open(int(start_epoch)))
    except Exception as exc:
        raise SystemExit(
            f"could not fetch start price for recovered entry; rerun with --start-price. error={type(exc).__name__}: {exc}"
        )


def _manual_resolve_entry_filled(
    db: sqlite3.Connection,
    attempt_id: int,
    shares: float,
    entry_price: float,
    notional: float,
    note: str | None = None,
    start_price: float | None = None,
    force: bool = False,
) -> None:
    row = _load_attempt_for_entry_recovery(db, attempt_id, force)
    market_slug = str(row["market_slug"])
    if _position_exists(db, market_slug):
        raise SystemExit(f"position already exists for {market_slug}; refusing to insert duplicate recovered entry")
    if _manual_resolve_exists_after(db, market_slug, int(attempt_id)) and not force:
        raise SystemExit(f"attempt {attempt_id} already has a later manual_resolved record")

    filled_shares = float(shares)
    filled_notional = float(notional)
    avg_price = _validate_prediction_price(entry_price, name="--entry-price")
    if filled_shares <= 0:
        raise SystemExit("--shares must be positive")
    if filled_notional <= 0:
        raise SystemExit("--notional must be positive")
    implied = filled_shares * avg_price
    if abs(implied - filled_notional) > max(0.05, filled_notional * 0.10) and not force:
        raise SystemExit(
            "--notional is not close to --shares * --entry-price; use --force only after confirming the CLOB fill"
        )

    start_epoch = _infer_market_start_epoch(row)
    if start_epoch is None:
        raise SystemExit("could not infer market start epoch for recovered entry")
    end_epoch = _infer_market_end_epoch(row, start_epoch) or (int(start_epoch) + 300)
    recovered_start_price = _manual_recovery_start_price(row, int(start_epoch), start_price)
    original_response = _json_loads_obj(row["response_json"])
    payload = {
        "note": note or "manual operator confirmed entry was filled",
        "operator_action": "manual_resolve_entry_filled",
        "attempt_id": int(attempt_id),
        "original_action": row["action"],
        "filled_shares": filled_shares,
        "filled_notional": filled_notional,
        "entry_price": avg_price,
        "start_price": recovered_start_price,
        "original_response": original_response,
    }
    open_response = {
        "dry_run": False,
        "live_enabled": True,
        "manual_recovered_entry": True,
        **payload,
    }
    db.execute(
        """
        INSERT INTO btc5m_live_positions
        (market_slug, side, token_id, open_ts_utc, status, entry_price, size_shares, notional_usd,
         edge_bps, direction_bps, start_epoch, end_epoch, start_price, entry_spot, last_mark_price,
         last_mark_ts_utc, pnl_usd, open_response_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            market_slug,
            row["side"],
            row["token_id"],
            row["ts_utc"] or _now_iso(),
            "OPEN",
            avg_price,
            filled_shares,
            filled_notional,
            row["edge_bps"],
            row["direction_bps"],
            int(start_epoch),
            int(end_epoch),
            recovered_start_price,
            None,
            avg_price,
            _now_iso(),
            0.0,
            json.dumps(open_response, sort_keys=True, default=str),
        ),
    )
    db.commit()
    _record_attempt(
        db,
        market_slug=market_slug,
        side=row["side"],
        token_id=row["token_id"],
        action="manual_resolved",
        reason="operator_resolved_entry_filled",
        edge_bps=row["edge_bps"],
        direction_bps=row["direction_bps"],
        seconds_after_start=None,
        seconds_left=None,
        entry_price=avg_price,
        notional_usd=filled_notional,
        fair_prob=row["fair_prob"],
        response=payload,
    )
    print(f"entry attempt {attempt_id} recovered as filled; position inserted for {market_slug}")


def _manual_resolve_position(
    db: sqlite3.Connection,
    position_id: int,
    status: str,
    close_price: float | None = None,
    note: str | None = None,
    force: bool = False,
) -> None:
    row = db.execute("SELECT * FROM btc5m_live_positions WHERE id=?", (int(position_id),)).fetchone()
    if row is None:
        raise SystemExit(f"position id not found: {position_id}")

    status = str(status or "").upper().strip()
    if status not in {"OPEN", "CLOSED"}:
        raise SystemExit("--position-status must be OPEN or CLOSED")
    if str(row["status"] or "") != "EXIT_UNCERTAIN" and not force:
        raise SystemExit("--resolve-position can only change EXIT_UNCERTAIN positions unless --force is used")

    market_slug = str(row["market_slug"])
    payload = {
        "note": note or "manual operator resolved position state",
        "operator_action": "manual_resolve_position",
        "position_id": int(position_id),
        "from_status": row["status"],
        "to_status": status,
    }

    if status == "CLOSED":
        if close_price is None:
            raise SystemExit("--position-status CLOSED requires --close-price")
        cp = _validate_prediction_price(close_price, name="--close-price", allow_zero=True)
        entry = float(row["entry_price"])
        size = float(row["size_shares"])
        pnl = (cp - entry) * size
        payload["manual_close_price"] = cp
        payload["manual_pnl_usd"] = pnl
        db.execute(
            """
            UPDATE btc5m_live_positions
            SET status='CLOSED', close_ts_utc=?, close_price=?, close_reason='manual_exit_resolved',
                pnl_usd=?, close_response_json=?
            WHERE id=?
            """,
            (_now_iso(), cp, pnl, json.dumps(payload, sort_keys=True, default=str), int(position_id)),
        )
    else:
        db.execute(
            """
            UPDATE btc5m_live_positions
            SET status='OPEN', close_ts_utc=NULL, close_price=NULL,
                close_reason='manual_exit_uncertain_reopened', close_response_json=?
            WHERE id=?
            """,
            (json.dumps(payload, sort_keys=True, default=str), int(position_id)),
        )

    db.commit()
    _record_attempt(
        db,
        market_slug=market_slug,
        side=row["side"],
        token_id=row["token_id"],
        action="manual_resolved",
        reason="operator_resolved_position",
        edge_bps=row["edge_bps"],
        direction_bps=row["direction_bps"],
        seconds_after_start=None,
        seconds_left=None,
        entry_price=row["entry_price"],
        notional_usd=row["notional_usd"],
        fair_prob=None,
        response=payload,
    )
    print(f"position {position_id} resolved to {status}; manual_resolved written for {market_slug}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--list-uncertain", action="store_true", help="List unresolved live/pending/uncertain attempts")
    parser.add_argument("--resolve-market", default=None, help="Resolve non-entry market locks; entry uncertainty must use --resolve-entry-filled or --resolve-entry-flat")
    parser.add_argument("--resolve-entry-filled", type=int, default=None, help="Recover an uncertain BUY attempt as filled and insert a position")
    parser.add_argument("--resolve-entry-flat", type=int, default=None, help="Recover an uncertain BUY attempt as flat/no-position")
    parser.add_argument("--resolve-position", type=int, default=None, help="Resolve an EXIT_UNCERTAIN position after manual reconciliation")
    parser.add_argument("--position-status", choices=["OPEN", "CLOSED"], default=None, help="Target status for --resolve-position")
    parser.add_argument("--shares", type=float, default=None, help="Filled shares for --resolve-entry-filled")
    parser.add_argument("--entry-price", type=float, default=None, help="Average entry price for --resolve-entry-filled")
    parser.add_argument("--notional", type=float, default=None, help="Filled notional for --resolve-entry-filled")
    parser.add_argument("--start-price", type=float, default=None, help="Optional BTC start price for recovered entry if Binance lookup is unavailable")
    parser.add_argument("--close-price", type=float, default=None, help="Manual close price when --position-status CLOSED")
    parser.add_argument("--note", default="", help="Optional note for resolve commands")
    parser.add_argument("--force", action="store_true", help="Allow manual recovery outside default guardrails after operator audit")
    args = parser.parse_args(argv)
    cfg = _config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("btc5m-live-s9b")
    db = _live_db(_env_s("BTC5M_LIVE_DB_PATH", os.path.expanduser("~/.local/share/arb-engine-s9b-sydney/btc5m-s9b.sqlite")))

    if args.list_uncertain:
        _print_uncertain(db)
        return 0
    if args.resolve_entry_filled is not None and args.resolve_entry_flat is not None:
        raise SystemExit("choose only one of --resolve-entry-filled or --resolve-entry-flat")
    if args.resolve_entry_flat is not None:
        _manual_resolve_entry_flat(db, args.resolve_entry_flat, args.note, args.force)
        return 0
    if args.resolve_entry_filled is not None:
        missing = [
            name for name, value in (
                ("--shares", args.shares),
                ("--entry-price", args.entry_price),
                ("--notional", args.notional),
            )
            if value is None
        ]
        if missing:
            raise SystemExit("--resolve-entry-filled requires " + ", ".join(missing))
        _manual_resolve_entry_filled(
            db,
            args.resolve_entry_filled,
            args.shares,
            args.entry_price,
            args.notional,
            args.note,
            start_price=args.start_price,
            force=args.force,
        )
        return 0
    if args.resolve_market:
        _manual_resolve_market(db, args.resolve_market, args.note)
        return 0
    if args.resolve_position is not None:
        if not args.position_status:
            raise SystemExit("--resolve-position requires --position-status OPEN or CLOSED")
        _manual_resolve_position(db, args.resolve_position, args.position_status, args.close_price, args.note, args.force)
        return 0

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
            log.info("btc5m s9b done %s", summary)
        except Exception:
            log.exception("btc5m s9b failed")
        if args.once:
            break
        time.sleep(_env_i("BTC5M_INTERVAL_SEC", 10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
