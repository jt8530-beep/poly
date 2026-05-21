"""Paper trader for Polymarket BTC Up/Down 5m markets.

This is deliberately paper-only. It estimates fair Up/Down probabilities from
Binance BTCUSDT 5-minute windows, then paper-buys only when Polymarket asks are
meaningfully below that estimate and the book is liquid enough.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as _config
from .ladder import _top_of_book
from .poly_client import PolyClient
from .tg_notifier import Notifier


SCHEMA = """
CREATE TABLE IF NOT EXISTS btc5m_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL,
  market_slug TEXT,
  action TEXT,
  reason TEXT,
  fair_up REAL,
  fair_down REAL,
  best_edge_bps INTEGER,
  opened INTEGER,
  marked INTEGER,
  closed INTEGER,
  open_positions INTEGER,
  open_notional_usd REAL,
  unrealized_pnl_usd REAL,
  realized_pnl_usd REAL,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS btc5m_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_slug TEXT NOT NULL,
  market_id TEXT,
  question TEXT,
  open_ts_utc TEXT NOT NULL,
  close_ts_utc TEXT,
  side TEXT NOT NULL,
  token_id TEXT NOT NULL,
  entry_price REAL NOT NULL,
  size_shares REAL NOT NULL,
  entry_notional_usd REAL NOT NULL,
  fair_prob REAL,
  edge_bps INTEGER,
  start_epoch INTEGER,
  end_epoch INTEGER,
  start_price REAL,
  entry_spot REAL,
  last_mark_price REAL,
  last_mark_ts_utc TEXT,
  pnl_usd REAL DEFAULT 0,
  status TEXT NOT NULL,
  close_price REAL,
  close_reason TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc5m_trades_status ON btc5m_trades(status);
CREATE INDEX IF NOT EXISTS idx_btc5m_trades_market ON btc5m_trades(market_slug);
"""


@dataclass
class BtcMarket:
    slug: str
    market_id: str
    question: str
    start_epoch: int
    end_epoch: int
    token_up: str
    token_down: str
    up_bid: float | None = None
    up_bid_size: float | None = None
    up_ask: float | None = None
    up_ask_size: float | None = None
    down_bid: float | None = None
    down_bid_size: float | None = None
    down_ask: float | None = None
    down_ask_size: float | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _env_i(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_f(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _env_s(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_b(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _open_db(path: str) -> sqlite3.Connection:
    expanded = Path(path).expanduser()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(expanded))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    return db


def _http_json(url: str, params: dict | None = None, timeout: float = 10.0) -> Any:
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "poly-btc5m-paper/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


class BinancePrice:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def price(self) -> float:
        data = _http_json(f"{self.base_url}/api/v3/ticker/price", {"symbol": "BTCUSDT"})
        return float(data["price"])

    def klines(self, interval: str, limit: int = 120, start_ms: int | None = None) -> list:
        params: dict[str, Any] = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        return _http_json(f"{self.base_url}/api/v3/klines", params)

    def window_open(self, start_epoch: int) -> float:
        rows = self.klines("5m", limit=1, start_ms=start_epoch * 1000)
        if not rows:
            raise RuntimeError("missing Binance 5m candle")
        return float(rows[0][1])

    def window_close(self, start_epoch: int) -> float:
        rows = self.klines("5m", limit=1, start_ms=start_epoch * 1000)
        if not rows:
            raise RuntimeError("missing Binance 5m candle")
        return float(rows[0][4])

    def one_minute_vol(self, lookback: int) -> float:
        rows = self.klines("1m", limit=max(5, lookback))
        closes = [float(row[4]) for row in rows if row and len(row) > 4]
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(returns) < 5:
            return _env_f("BTC5M_MIN_VOL_1M", 0.00035)
        mean = sum(returns) / len(returns)
        variance = sum((ret - mean) ** 2 for ret in returns) / max(1, len(returns) - 1)
        return max(math.sqrt(variance), _env_f("BTC5M_MIN_VOL_1M", 0.00035))

    def closed_1m_closes(self, now_ts: float, limit: int = 8) -> list[tuple[int, float]]:
        rows = self.klines("1m", limit=max(5, limit))
        now_ms = int(now_ts * 1000)
        out: list[tuple[int, float]] = []
        for row in rows:
            if not row or len(row) <= 6:
                continue
            try:
                close_time_ms = int(row[6])
                close = float(row[4])
            except Exception:
                continue
            if close_time_ms < now_ms:
                out.append((close_time_ms // 1000, close))
        return out


def _book_by_token(client: PolyClient, token_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for book in client.get_books_batch(token_ids) or []:
        token = str(book.get("asset_id") or book.get("token_id") or "")
        if token:
            out[token] = book
    return out


def _load_current_market(client: PolyClient, now_ts: float) -> BtcMarket | None:
    start_epoch = int(now_ts // 300) * 300
    slug = f"btc-updown-5m-{start_epoch}"
    event = client._get(f"{client.gamma_url}/events/slug/{slug}")
    if not event:
        return None
    market = (event.get("markets") or [{}])[0]
    if market.get("closed") or market.get("archived") or not market.get("active", True):
        return None
    if not market.get("enableOrderBook"):
        return None
    tokens = _parse_json_list(market.get("clobTokenIds"))
    outcomes = [str(item).lower() for item in _parse_json_list(market.get("outcomes"))]
    if len(tokens) < 2 or len(outcomes) < 2:
        return None
    up_idx = outcomes.index("up") if "up" in outcomes else 0
    down_idx = outcomes.index("down") if "down" in outcomes else 1
    end_epoch = start_epoch + 300
    return BtcMarket(
        slug=str(market.get("slug") or slug),
        market_id=str(market.get("id") or ""),
        question=str(market.get("question") or ""),
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        token_up=str(tokens[up_idx]),
        token_down=str(tokens[down_idx]),
    )


def _populate_market_book(client: PolyClient, market: BtcMarket) -> None:
    books = _book_by_token(client, [market.token_up, market.token_down])
    ub, ubs, ua, uas = _top_of_book(books.get(market.token_up, {}))
    db, dbs, da, das = _top_of_book(books.get(market.token_down, {}))
    market.up_bid, market.up_bid_size, market.up_ask, market.up_ask_size = ub, ubs, ua, uas
    market.down_bid, market.down_bid_size, market.down_ask, market.down_ask_size = db, dbs, da, das


def _fair_probability(start_price: float, spot: float, seconds_left: float, vol_1m: float) -> float:
    minutes_left = max(seconds_left / 60.0, 0.05)
    denom = max(vol_1m * math.sqrt(minutes_left), 1e-8)
    z = math.log(max(spot, 1e-9) / max(start_price, 1e-9)) / denom
    fair = _normal_cdf(z)
    return min(max(fair, 0.02), 0.98)


def _candidate(side: str, fair: float, market: BtcMarket) -> dict | None:
    if side == "UP":
        ask, ask_size, bid, token_id = market.up_ask, market.up_ask_size, market.up_bid, market.token_up
    else:
        ask, ask_size, bid, token_id = market.down_ask, market.down_ask_size, market.down_bid, market.token_down
    if ask is None or ask_size is None or bid is None or ask <= 0 or ask_size <= 0:
        return None
    spread_pct = (ask - bid) / ask if ask > 0 else 1.0
    edge = fair - ask
    return {
        "side": side,
        "token_id": token_id,
        "ask": ask,
        "bid": bid,
        "ask_size": ask_size,
        "spread_pct": spread_pct,
        "fair": fair,
        "edge": edge,
        "edge_bps": int(edge * 10_000),
    }


def _direction_bps(side: str, start_price: float, spot: float) -> float:
    if start_price <= 0:
        return 0.0
    raw_bps = (spot - start_price) / start_price * 10_000
    return raw_bps if side == "UP" else -raw_bps


def _trend_gate_decision_from_closes(closes: list[float], lookback_min: int, threshold_bps: float) -> tuple[str, float] | None:
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


def _trend_gate_decision(price: BinancePrice, now_ts: float, lookback_min: int, threshold_bps: float) -> tuple[str, float] | None:
    closed = price.closed_1m_closes(now_ts, limit=max(lookback_min + 4, 8))
    closes = [close for _, close in closed]
    return _trend_gate_decision_from_closes(closes, lookback_min, threshold_bps)


def _entry_exitability_reason(candidate: dict) -> str | None:
    max_spread_pct = _env_f("BTC5M_ENTRY_EXITABILITY_MAX_SPREAD_PCT", 0.0145)
    min_ask_depth = _env_f("BTC5M_ENTRY_EXITABILITY_MIN_ASK_DEPTH_SHARES", 25.0)
    if float(candidate["spread_pct"]) > max_spread_pct:
        return "entry_exitability_spread"
    if float(candidate["ask_size"]) < min_ask_depth:
        return "entry_exitability_depth"
    return None


def _entry_filter_reason(
    candidate: dict,
    seconds_after_start: float,
    start_price: float,
    spot: float,
    use_direction_confirm: bool = True,
) -> tuple[str | None, float | None]:
    if not use_direction_confirm:
        return None, None
    direction_bps = _direction_bps(str(candidate["side"]), start_price, spot)
    low_edge_max = _env_i("BTC5M_LOW_EDGE_CONFIRM_MAX_BPS", 1000)
    low_edge_min_direction = _env_f("BTC5M_LOW_EDGE_MIN_DIRECTION_BPS", 4.0)
    high_edge_min_direction = _env_f("BTC5M_HIGH_EDGE_MIN_DIRECTION_BPS", 0.0)
    if int(candidate["edge_bps"]) < low_edge_max and direction_bps < low_edge_min_direction:
        return "low_edge_weak_direction", direction_bps
    if int(candidate["edge_bps"]) >= low_edge_max and direction_bps < high_edge_min_direction:
        return "high_edge_weak_direction", direction_bps
    return None, direction_bps


def _open_trade(db: sqlite3.Connection, market: BtcMarket, candidate: dict, start_price: float, spot: float, payload: dict) -> int:
    max_notional = _env_f("BTC5M_MAX_NOTIONAL_USD", 10.0)
    min_notional = _env_f("BTC5M_MIN_NOTIONAL_USD", 5.0)
    notional = min(max_notional, float(candidate["ask_size"]) * float(candidate["ask"]))
    if notional < min_notional:
        return 0
    size = notional / float(candidate["ask"])
    db.execute(
        "INSERT INTO btc5m_trades (market_slug,market_id,question,open_ts_utc,side,token_id,entry_price,size_shares,"
        "entry_notional_usd,fair_prob,edge_bps,start_epoch,end_epoch,start_price,entry_spot,last_mark_price,"
        "last_mark_ts_utc,pnl_usd,status,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            market.slug,
            market.market_id,
            market.question,
            _now_iso(),
            candidate["side"],
            candidate["token_id"],
            candidate["ask"],
            size,
            notional,
            candidate["fair"],
            candidate["edge_bps"],
            market.start_epoch,
            market.end_epoch,
            start_price,
            spot,
            candidate["bid"],
            _now_iso(),
            (float(candidate["bid"]) - float(candidate["ask"])) * size,
            "OPEN",
            json.dumps(payload, sort_keys=True),
        ),
    )
    db.commit()
    return 1


def _open_trade_count(db: sqlite3.Connection, market_slug: str | None = None) -> int:
    if market_slug:
        row = db.execute("SELECT COUNT(*) FROM btc5m_trades WHERE status='OPEN' AND market_slug=?", (market_slug,)).fetchone()
    else:
        row = db.execute("SELECT COUNT(*) FROM btc5m_trades WHERE status='OPEN'").fetchone()
    return int(row[0] or 0)


def _market_trade_exists(db: sqlite3.Connection, market_slug: str) -> bool:
    row = db.execute("SELECT 1 FROM btc5m_trades WHERE market_slug=? LIMIT 1", (market_slug,)).fetchone()
    return row is not None


def _trade_payload(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload_json"] or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _fmt_usd(value: float | None) -> str:
    value = float(value or 0.0)
    return f"+${value:.4f}" if value >= 0 else f"-${abs(value):.4f}"


def _fmt_pct(value: float | None) -> str:
    value = float(value or 0.0)
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.2f}%"


def _reason_zh(reason: str | None) -> str:
    mapping = {
        "settled_binance_proxy": "5分钟结算（Binance 代理）",
        "stop_loss": "止损",
        "take_profit": "止盈",
        "disaster_trail": "大浮盈灾难保护",
    }
    return mapping.get(str(reason or ""), str(reason or "未知"))


def _summary_since(db: sqlite3.Connection, since: str | None) -> dict:
    where = ""
    args: tuple[str, ...] = ()
    if since:
        where = "WHERE open_ts_utc >= ?"
        args = (since,)
    row = db.execute(
        f"""
        SELECT
          COUNT(*) AS trades,
          COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),0) AS open_trades,
          COALESCE(SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),0) AS closed_trades,
          COALESCE(SUM(CASE WHEN status='CLOSED' AND pnl_usd > 0 THEN 1 ELSE 0 END),0) AS wins,
          COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl_usd ELSE 0 END),0) AS realized_pnl,
          COALESCE(SUM(CASE WHEN status='OPEN' THEN pnl_usd ELSE 0 END),0) AS unrealized_pnl
        FROM btc5m_trades
        {where}
        """,
        args,
    ).fetchone()
    closed = int(row["closed_trades"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "trades": int(row["trades"] or 0),
        "open_trades": int(row["open_trades"] or 0),
        "closed_trades": closed,
        "wins": wins,
        "win_rate": (wins / closed) if closed else None,
        "realized_pnl": float(row["realized_pnl"] or 0.0),
        "unrealized_pnl": float(row["unrealized_pnl"] or 0.0),
    }


def _notify_close(tg: Notifier | None, row: sqlite3.Row, reason: str | None, close_price: float | None, pnl: float, summary: dict) -> None:
    if tg is None or not tg.enabled:
        return
    entry_notional = float(row["entry_notional_usd"] or 0.0)
    roi = pnl / entry_notional if entry_notional else 0.0
    win_rate = summary["win_rate"]
    win_rate_text = "暂无" if win_rate is None else _fmt_pct(win_rate)
    title = "盈利" if pnl > 0 else "亏损" if pnl < 0 else "持平"
    text = "\n".join(
        [
            f"<b>BTC 5分钟 paper 关仓：{html.escape(title)}</b>",
            f"方向：<b>{html.escape(str(row['side']))}</b>",
            f"原因：{html.escape(_reason_zh(reason))}",
            f"开仓价：<code>{float(row['entry_price']):.4f}</code>",
            f"平仓价：<code>{float(close_price if close_price is not None else row['last_mark_price'] or 0.0):.4f}</code>",
            f"投入：<b>${entry_notional:.4f}</b>",
            f"本笔盈亏：<b>{html.escape(_fmt_usd(pnl))}</b>（{html.escape(_fmt_pct(roi))}）",
            f"Edge：<b>{int(row['edge_bps'] or 0)} bps</b>",
            f"市场：<code>{html.escape(str(row['market_slug']))}</code>",
            "",
            "<b>累计结果（新版参数后）</b>",
            f"交易：{summary['trades']} 笔，已关：{summary['closed_trades']} 笔，持仓：{summary['open_trades']} 笔",
            f"胜率：{summary['wins']}/{summary['closed_trades']} = {html.escape(win_rate_text)}",
            f"已实现：<b>{html.escape(_fmt_usd(summary['realized_pnl']))}</b>",
            f"浮动：{html.escape(_fmt_usd(summary['unrealized_pnl']))}",
        ]
    )
    tg.send(text)


def _mark_open(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice, now_ts: float, tg: Notifier | None = None) -> tuple[int, int]:
    rows = db.execute("SELECT * FROM btc5m_trades WHERE status='OPEN' ORDER BY id").fetchall()
    if not rows:
        return 0, 0
    books = _book_by_token(poly, [str(row["token_id"]) for row in rows])
    marked = 0
    closed = 0
    closed_notifications = []
    take_profit = _env_f("BTC5M_TAKE_PROFIT", 0.20)
    stop_loss = _env_f("BTC5M_STOP_LOSS", 0.12)
    tail_force_exit_sec = _env_i("BTC5M_TAIL_FORCE_EXIT_SEC", 60)
    # Pre-settle loss-capping exits (time gradient).
    # Only applies to losing positions; intended to avoid full -$notional losses at settlement.
    # Configure as fractions of notional (e.g. 0.60 = 60% loss).
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
    # If enabled, this is intended to replace a wide stop-loss (e.g. 30%).
    stop_loss_enabled = _env_b("BTC5M_STOP_LOSS_ENABLED", not pre_enabled)
    for row in rows:
        token_id = str(row["token_id"])
        bb, _, _, _ = _top_of_book(books.get(token_id, {}))
        mark = bb if bb is not None else row["last_mark_price"]
        if mark is None:
            continue
        entry = float(row["entry_price"])
        size = float(row["size_shares"])
        notional = float(row["entry_notional_usd"])
        pnl = (float(mark) - entry) * size
        payload = _trade_payload(row)
        exit_state = payload.get("exit_state")
        if not isinstance(exit_state, dict):
            exit_state = {}
        peak_pnl = max(float(exit_state.get("peak_pnl_usd", pnl)), pnl)
        exit_state["peak_pnl_usd"] = peak_pnl
        seconds_left = float(row["end_epoch"]) - now_ts
        disaster_active = bool(exit_state.get("disaster_trail_active", False))
        disaster_enabled = _env_b("BTC5M_DISASTER_TRAIL_ENABLED", False)
        disaster_activate = _env_f("BTC5M_TRAIL_ACTIVATE_PROFIT", 0.80)
        disaster_giveback = _env_f("BTC5M_TRAIL_GIVEBACK", 0.20)
        disaster_min_seconds_left = _env_i("BTC5M_TRAIL_MIN_SECONDS_LEFT", 120)
        if (
            disaster_enabled
            and not disaster_active
            and notional > 0
            and peak_pnl >= notional * disaster_activate
            and seconds_left > disaster_min_seconds_left
        ):
            disaster_active = True
            exit_state["disaster_trail_active"] = True
            exit_state["disaster_trail_activated_ts_utc"] = _now_iso()
        payload["exit_state"] = exit_state
        payload_json = json.dumps(payload, sort_keys=True)
        status = "OPEN"
        close_reason = None
        close_price = None
        if now_ts >= float(row["end_epoch"]) + _env_i("BTC5M_SETTLE_DELAY_SEC", 8):
            try:
                close_spot = price.window_close(int(row["start_epoch"]))
                up_wins = close_spot >= float(row["start_price"])
                winner = "UP" if up_wins else "DOWN"
                close_price = 1.0 if row["side"] == winner else 0.0
                pnl = (close_price - entry) * size
                close_reason = "settled_binance_proxy"
                status = "CLOSED"
            except Exception:
                close_reason = None
        elif tail_force_exit_sec > 0 and seconds_left <= tail_force_exit_sec:
            close_price = float(mark)
            close_reason = "tail_force_exit"
            status = "CLOSED"
        # Time-gradient loss cap near settlement:
        # If the position is losing late in the window, cut it at the current bid (mark)
        # based on how close we are to expiry.
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
                status = "CLOSED"
        elif disaster_active and notional > 0 and peak_pnl - pnl >= notional * disaster_giveback:
            close_price = float(mark)
            close_reason = "disaster_trail"
            status = "CLOSED"
        elif notional > 0 and pnl >= notional * take_profit:
            close_price = float(mark)
            close_reason = "take_profit"
            status = "CLOSED"
        elif stop_loss_enabled and notional > 0 and pnl <= -notional * stop_loss:
            close_price = float(mark)
            close_reason = "stop_loss"
            status = "CLOSED"
        if status == "CLOSED":
            db.execute(
                "UPDATE btc5m_trades SET status='CLOSED', close_ts_utc=?, close_price=?, close_reason=?, "
                "last_mark_price=?, last_mark_ts_utc=?, pnl_usd=?, payload_json=? WHERE id=?",
                (_now_iso(), close_price, close_reason, mark, _now_iso(), pnl, payload_json, row["id"]),
            )
            closed += 1
            closed_notifications.append((row, close_reason, close_price, pnl))
        else:
            db.execute(
                "UPDATE btc5m_trades SET last_mark_price=?, last_mark_ts_utc=?, pnl_usd=?, payload_json=? WHERE id=?",
                (mark, _now_iso(), pnl, payload_json, row["id"]),
            )
        marked += 1
    db.commit()
    stats_since = _env_s("BTC5M_STATS_SINCE_UTC", "")
    for row, close_reason, close_price, pnl in closed_notifications:
        _notify_close(tg, row, close_reason, close_price, pnl, _summary_since(db, stats_since))
    return marked, closed


def _summary(db: sqlite3.Connection) -> dict:
    open_count, open_notional, unrealized = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(entry_notional_usd),0), COALESCE(SUM(pnl_usd),0) FROM btc5m_trades WHERE status='OPEN'"
    ).fetchone()
    closed_count, wins, realized = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),0), COALESCE(SUM(pnl_usd),0) FROM btc5m_trades WHERE status='CLOSED'"
    ).fetchone()
    return {
        "open_positions": int(open_count or 0),
        "open_notional_usd": round(float(open_notional or 0.0), 4),
        "unrealized_pnl_usd": round(float(unrealized or 0.0), 4),
        "closed_positions": int(closed_count or 0),
        "winning_closed": int(wins or 0),
        "win_rate": round(float(wins or 0) / float(closed_count or 1), 4) if closed_count else None,
        "realized_pnl_usd": round(float(realized or 0.0), 4),
    }


def run_once(db: sqlite3.Connection, poly: PolyClient, price: BinancePrice, tg: Notifier | None = None) -> dict:
    now_ts = time.time()
    marked, closed = _mark_open(db, poly, price, now_ts, tg)
    opened = 0
    action = "hold"
    reason = "no_signal"
    fair_up = fair_down = None
    best_edge_bps = None
    best_direction_bps = None
    market_slug = None
    trend_side = None
    trend_score_bps = None
    try:
        market = _load_current_market(poly, now_ts)
        if not market:
            reason = "no_current_market"
        else:
            market_slug = market.slug
            seconds_after_start = now_ts - market.start_epoch
            seconds_left = market.end_epoch - now_ts
            if seconds_after_start < _env_i("BTC5M_MIN_SECONDS_AFTER_START", 45):
                reason = "too_early"
            elif seconds_left < _env_i("BTC5M_MIN_SECONDS_BEFORE_END", 45):
                reason = "too_late"
            elif _open_trade_count(db) >= _env_i("BTC5M_MAX_OPEN_POSITIONS", 1):
                reason = "max_open"
            elif _market_trade_exists(db, market.slug):
                reason = "already_traded_market"
            else:
                trend_gate_enabled = _env_b("BTC5M_TREND_GATE_ENABLED", True)
                trend_lookback_min = _env_i("BTC5M_TREND_LOOKBACK_MIN", 2)
                trend_threshold_bps = _env_f("BTC5M_TREND_THRESHOLD_BPS", 0.0)
                if trend_gate_enabled:
                    trend_pick = _trend_gate_decision(price, now_ts, trend_lookback_min, trend_threshold_bps)
                    if trend_pick is None:
                        reason = "trend_skip"
                        trend_side = None
                    else:
                        trend_side, trend_score_bps = trend_pick
                if reason == "trend_skip":
                    pass
                else:
                    _populate_market_book(poly, market)
                    start_price = price.window_open(market.start_epoch)
                    spot = price.price()
                    vol_1m = price.one_minute_vol(_env_i("BTC5M_VOL_LOOKBACK_MIN", 120))
                    fair_up = _fair_probability(start_price, spot, seconds_left, vol_1m)
                    fair_down = 1.0 - fair_up
                    if trend_gate_enabled:
                        if trend_side == "UP":
                            candidates = [_candidate("UP", fair_up, market)]
                        else:
                            candidates = [_candidate("DOWN", fair_down, market)]
                    else:
                        candidates = [
                            _candidate("UP", fair_up, market),
                            _candidate("DOWN", fair_down, market),
                        ]
                    candidates = [candidate for candidate in candidates if candidate]
                    if not candidates:
                        reason = "missing_book"
                    else:
                        best = max(candidates, key=lambda candidate: candidate["edge_bps"])
                        best_edge_bps = best["edge_bps"]
                        entry_filter_reason, direction_confirm_bps = _entry_filter_reason(
                            best,
                            seconds_after_start,
                            start_price,
                            spot,
                            use_direction_confirm=True,
                        )
                        best_direction_bps = direction_confirm_bps
                        spread_ok = best["spread_pct"] <= _env_f("BTC5M_MAX_SPREAD_PCT", 0.04)
                        price_ok = best["ask"] <= _env_f("BTC5M_MAX_ENTRY_PRICE", 0.82)
                        edge_ok = best["edge_bps"] >= _env_i("BTC5M_MIN_EDGE_BPS", 700)
                        depth_ok = best["ask_size"] >= _env_f("BTC5M_MIN_ASK_DEPTH_SHARES", 10.0)
                        exitability_reason = _entry_exitability_reason(best)
                        if not edge_ok:
                            reason = "edge_too_small"
                        elif not spread_ok:
                            reason = "spread_too_wide"
                        elif not price_ok:
                            reason = "price_too_high"
                        elif not depth_ok:
                            reason = "depth_too_low"
                        elif exitability_reason:
                            reason = exitability_reason
                        elif entry_filter_reason:
                            reason = entry_filter_reason
                        else:
                            payload = {
                                "start_price": start_price,
                                "entry_spot": spot,
                                "vol_1m": vol_1m,
                                "seconds_after_start": seconds_after_start,
                                "seconds_left": seconds_left,
                                "trend_gate_enabled": trend_gate_enabled,
                                "trend_side": trend_side,
                                "trend_score_bps": trend_score_bps,
                                "trend_lookback_min": trend_lookback_min,
                                "trend_threshold_bps": trend_threshold_bps,
                                "direction_bps": best_direction_bps,
                                "up_book": {"bid": market.up_bid, "ask": market.up_ask, "ask_size": market.up_ask_size},
                                "down_book": {"bid": market.down_bid, "ask": market.down_ask, "ask_size": market.down_ask_size},
                            }
                            opened = _open_trade(db, market, best, start_price, spot, payload)
                            action = "opened" if opened else "hold"
                            reason = "opened" if opened else "notional_too_low"
    except Exception as exc:
        logging.getLogger("btc5m").warning("btc5m run failed: %s", exc)
        reason = f"error:{type(exc).__name__}"
    summary = _summary(db)
    summary.update(
        {
            "opened": opened,
            "marked": marked,
            "closed": closed,
            "market_slug": market_slug,
            "action": action,
            "reason": reason,
            "fair_up": round(fair_up, 4) if fair_up is not None else None,
            "fair_down": round(fair_down, 4) if fair_down is not None else None,
            "best_edge_bps": best_edge_bps,
            "best_direction_bps": round(best_direction_bps, 2) if best_direction_bps is not None else None,
            "trend_side": trend_side,
            "trend_score_bps": round(trend_score_bps, 2) if trend_score_bps is not None else None,
        }
    )
    db.execute(
        "INSERT INTO btc5m_runs (ts_utc,market_slug,action,reason,fair_up,fair_down,best_edge_bps,opened,marked,closed,"
        "open_positions,open_notional_usd,unrealized_pnl_usd,realized_pnl_usd,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _now_iso(),
            market_slug,
            action,
            reason,
            fair_up,
            fair_down,
            best_edge_bps,
            opened,
            marked,
            closed,
            summary["open_positions"],
            summary["open_notional_usd"],
            summary["unrealized_pnl_usd"],
            summary["realized_pnl_usd"],
            json.dumps(summary, sort_keys=True),
        ),
    )
    db.commit()
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
    log = logging.getLogger("btc5m")
    db = _open_db(_env_s("BTC5M_DB_PATH", os.path.expanduser("~/.local/share/arb-engine/btc5m.sqlite")))
    poly = PolyClient(cfg.poly.gamma_url, cfg.poly.clob_url)
    price = BinancePrice(_env_s("BTC5M_BINANCE_URL", "https://api.binance.com"))
    tg = Notifier(cfg.tg.bot_token, cfg.tg.chat_id, cfg.tg.enabled and _env_b("BTC5M_TG_ENABLED", False))
    interval = _env_i("BTC5M_INTERVAL_SEC", 10)
    stop = {"flag": False}

    def _handle(sig, frame):
        log.warning("signal %s - shutting down", sig)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    while not stop["flag"]:
        summary = run_once(db, poly, price, tg)
        log.info("btc5m paper done %s", summary)
        if args.once:
            break
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
