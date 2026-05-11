"""SQLite ledger. v1 additions:
  * signals.legs_json for structured leg info (in addition to legacy raw_json)
  * trades.close_ts / close_price for paper settlement
  * per-signal trade-count helper (used by risk)
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  strategy TEXT NOT NULL,
  event_slug TEXT,
  event_title TEXT,
  kind TEXT,
  leg_a_market_id TEXT,
  leg_b_market_id TEXT,
  leg_a_action TEXT,
  leg_b_action TEXT,
  edge_bps INTEGER,
  expected_profit_usd REAL,
  max_size_usd REAL,
  q_shares REAL,
  legs_json TEXT,                -- v1: structured
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  signal_id INTEGER,
  market_id TEXT,
  token_id TEXT,
  side TEXT,                     -- BUY / SELL
  token_outcome TEXT,            -- YES / NO
  price REAL,
  size REAL,                     -- shares
  notional_usd REAL,
  status TEXT,                   -- OPEN / CLOSED / CANCELLED
  mode TEXT,                     -- paper / live
  external_id TEXT,
  pnl_usd REAL,
  note TEXT,
  close_ts_utc TEXT,             -- v1
  close_price REAL,              -- v1
  close_reason TEXT,             -- v1
  FOREIGN KEY(signal_id) REFERENCES signals(id)
);
CREATE TABLE IF NOT EXISTS risk_state (
  id INTEGER PRIMARY KEY,
  peak_equity REAL,
  last_equity REAL,
  halted INTEGER,
  halt_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
"""

# --- one-shot migration for existing databases from v0
_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN q_shares REAL",
    "ALTER TABLE signals ADD COLUMN legs_json TEXT",
    "ALTER TABLE trades  ADD COLUMN token_id TEXT",
    "ALTER TABLE trades  ADD COLUMN close_ts_utc TEXT",
    "ALTER TABLE trades  ADD COLUMN close_price REAL",
    "ALTER TABLE trades  ADD COLUMN close_reason TEXT",
]


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript(SCHEMA)
    # best-effort migrations (ignore "duplicate column" errors on fresh DBs)
    for stmt in _MIGRATIONS:
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


def record_signal(db, strategy: str, sig, raw_json: str, ts_utc: str) -> int:
    legs_json = json.dumps({
        "leg_a": {
            "market_id": sig.leg_a.market_id, "question": sig.leg_a.question,
            "token_id": sig.leg_a.token_id, "outcome": sig.leg_a.token_outcome,
            "side": sig.leg_a.side, "price": sig.leg_a.price,
            "size_shares": sig.leg_a.size_shares, "notional_usd": sig.leg_a.notional_usd,
            "action": sig.leg_a.action_str,
        },
        "leg_b": {
            "market_id": sig.leg_b.market_id, "question": sig.leg_b.question,
            "token_id": sig.leg_b.token_id, "outcome": sig.leg_b.token_outcome,
            "side": sig.leg_b.side, "price": sig.leg_b.price,
            "size_shares": sig.leg_b.size_shares, "notional_usd": sig.leg_b.notional_usd,
            "action": sig.leg_b.action_str,
        },
    })
    c = db.cursor()
    c.execute(
      "INSERT INTO signals (ts_utc,strategy,event_slug,event_title,kind,"
      "leg_a_market_id,leg_b_market_id,leg_a_action,leg_b_action,"
      "edge_bps,expected_profit_usd,max_size_usd,q_shares,legs_json,raw_json) "
      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ts_utc, strategy, sig.event_slug, sig.event_title, sig.kind,
       sig.leg_a.market_id, sig.leg_b.market_id,
       sig.leg_a.action_str, sig.leg_b.action_str,
       sig.edge_bps, sig.expected_profit_usd, sig.max_size_usd, sig.q_shares,
       legs_json, raw_json)
    )
    db.commit()
    return c.lastrowid or 0


def record_paper_trade(db, ts_utc, signal_id, market_id, token_id,
                       side, outcome, price, size_shares, note=""):
    """Record one leg of a paired paper trade."""
    notional = price * size_shares
    c = db.cursor()
    c.execute(
      "INSERT INTO trades (ts_utc,signal_id,market_id,token_id,side,token_outcome,"
      "price,size,notional_usd,status,mode,note) "
      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      (ts_utc, signal_id, market_id, token_id, side, outcome,
       price, size_shares, notional, "OPEN", "paper", note)
    )
    db.commit()


def close_paper_trade(db, trade_id: int, close_ts_utc: str, close_price: float,
                      pnl_usd: float, reason: str = "") -> None:
    """Mark a paper trade CLOSED with realised PnL."""
    c = db.cursor()
    c.execute(
        "UPDATE trades SET status='CLOSED', close_ts_utc=?, close_price=?, "
        "pnl_usd=?, close_reason=? WHERE id=?",
        (close_ts_utc, close_price, pnl_usd, reason, trade_id),
    )
    db.commit()


# --- helpers used by risk
def count_signals_today(db, today_iso_date: str) -> int:
    r = db.execute(
        "SELECT COUNT(DISTINCT signal_id) FROM trades WHERE substr(ts_utc,1,10)=?",
        (today_iso_date,),
    ).fetchone()
    return int(r[0] or 0)


def sum_open_notional(db) -> float:
    r = db.execute(
        "SELECT COALESCE(SUM(notional_usd),0) FROM trades WHERE status='OPEN'"
    ).fetchone()
    return float(r[0] or 0.0)
