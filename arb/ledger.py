"""SQLite ledger - one file, zero external deps."""
from __future__ import annotations
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
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  signal_id INTEGER,
  market_id TEXT,
  side TEXT,          -- BUY / SELL
  token_outcome TEXT, -- YES / NO
  price REAL,
  size REAL,
  notional_usd REAL,
  status TEXT,        -- OPEN / CLOSED / CANCELLED
  mode TEXT,          -- paper / live
  external_id TEXT,
  pnl_usd REAL,
  note TEXT,
  FOREIGN KEY(signal_id) REFERENCES signals(id)
);
CREATE TABLE IF NOT EXISTS risk_state (
  id INTEGER PRIMARY KEY,
  peak_equity REAL,
  last_equity REAL,
  halted INTEGER,
  halt_reason TEXT
);
"""

def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript(SCHEMA)
    db.commit()
    return db

def record_signal(db, strategy: str, sig, raw_json: str, ts_utc: str) -> int:
    c = db.cursor()
    c.execute(
      "INSERT INTO signals (ts_utc,strategy,event_slug,event_title,kind,leg_a_market_id,leg_b_market_id,"
      "leg_a_action,leg_b_action,edge_bps,expected_profit_usd,max_size_usd,raw_json) "
      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (ts_utc, strategy, sig.event_slug, sig.event_title, sig.kind,
       sig.leg_a_market.market_id, sig.leg_b_market.market_id,
       sig.leg_a_action, sig.leg_b_action,
       sig.edge_bps, sig.expected_profit_usd, sig.max_size_usd, raw_json)
    )
    db.commit()
    return c.lastrowid or 0

def record_paper_trade(db, ts_utc, signal_id, market_id, side, outcome, price, size, note=""):
    c = db.cursor()
    c.execute(
      "INSERT INTO trades (ts_utc,signal_id,market_id,side,token_outcome,price,size,notional_usd,status,mode,note) "
      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
      (ts_utc, signal_id, market_id, side, outcome, price, size, price*size,
       "OPEN", "paper", note)
    )
    db.commit()
