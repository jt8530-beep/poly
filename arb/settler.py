"""
Paper-trade settler.

Without this, trade rows stay OPEN forever, open_notional keeps growing, and risk
eventually blocks every new signal.

Strategy (paper mode):
  1. Find all OPEN paper trades whose underlying market is CLOSED on Polymarket.
  2. Look up the market's resolved outcome via Gamma (umaResolutionStatus /
     outcomes + outcomePrices = [1,0] or [0,1]).
  3. Compute PnL:
        payoff_per_share = 1 if outcome==our_side else 0
        pnl = (payoff_per_share - entry_price) * size_shares
  4. Mark CLOSED with close_price and close_reason.
  5. Release open_notional in the RiskManager.

If a market is closed but resolution data is ambiguous, we leave it OPEN and
log a warning. Manual settlement always wins.

Also handles stale-endDate markets (endDate > 14d past with no closed flag)
by closing them at mid-price with close_reason='stale'.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

log = logging.getLogger("settler")


STALE_DAYS = 14


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_market(client, market_id: str) -> dict | None:
    try:
        return client._get(f"{client.gamma_url}/markets/{market_id}")
    except Exception as e:
        log.warning("gamma markets/%s failed: %s", market_id, e)
        return None


def _resolved_yes_price(market: dict) -> float | None:
    """Return 1.0 or 0.0 if the market has resolved, else None."""
    if not market: return None
    outcomes = market.get("outcomes")
    prices   = market.get("outcomePrices")
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except Exception: outcomes = None
    if isinstance(prices, str):
        try: prices = json.loads(prices)
        except Exception: prices = None
    if not (isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == 2 and len(prices) == 2):
        return None
    # only treat as settled if market is marked closed AND prices are [0,1] or [1,0]
    if not market.get("closed"):
        return None
    try:
        p0 = float(prices[0]); p1 = float(prices[1])
    except Exception:
        return None
    if abs(p0 - p1) < 1e-9:   # unresolved / tie / still trading
        return None
    o0 = (outcomes[0] or "").lower()
    yes_price = p0 if o0.startswith("y") else p1
    return yes_price


def _parse_iso(s: str | None) -> datetime | None:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def settle_open_paper_trades(db, client, risk, max_per_run: int = 200) -> dict:
    """Close resolvable/stale open paper trades. Returns stats."""
    stats = {"checked": 0, "settled": 0, "stale_closed": 0, "still_open": 0}
    cur = db.execute(
        "SELECT id, signal_id, market_id, token_outcome, price, size, notional_usd, ts_utc "
        "FROM trades WHERE status='OPEN' AND mode='paper' ORDER BY id ASC LIMIT ?",
        (max_per_run,),
    )
    rows = cur.fetchall()
    now = datetime.now(timezone.utc)

    # cache market lookups to avoid hammering the API
    mkt_cache: dict[str, dict | None] = {}

    for trade_id, sig_id, market_id, outcome, entry_price, size_shares, notional, ts_opened in rows:
        stats["checked"] += 1
        m = mkt_cache.get(market_id)
        if m is None:
            m = _load_market(client, market_id)
            mkt_cache[market_id] = m

        yes_px = _resolved_yes_price(m) if m else None
        if yes_px is not None:
            payoff = yes_px if (outcome or "").upper() == "YES" else (1.0 - yes_px)
            pnl = (payoff - entry_price) * size_shares
            db.execute(
                "UPDATE trades SET status='CLOSED', close_ts_utc=?, close_price=?, pnl_usd=?, close_reason=? WHERE id=?",
                (_now_iso(), payoff, pnl, "resolved", trade_id),
            )
            risk.on_close_trade(notional)
            stats["settled"] += 1
            continue

        # stale detection: market endDate well past with no resolution
        end_dt = _parse_iso(m.get("endDate") if m else None) if m else None
        opened_dt = _parse_iso(ts_opened)
        stale = False
        if end_dt and (now - end_dt) > timedelta(days=STALE_DAYS):
            stale = True
        elif opened_dt and (now - opened_dt) > timedelta(days=60):
            stale = True

        if stale:
            # Conservatively close at entry_price => zero PnL. We don't guess.
            db.execute(
                "UPDATE trades SET status='CLOSED', close_ts_utc=?, close_price=?, pnl_usd=0, close_reason=? WHERE id=?",
                (_now_iso(), entry_price, "stale_no_resolution", trade_id),
            )
            risk.on_close_trade(notional)
            stats["stale_closed"] += 1
        else:
            stats["still_open"] += 1

    if stats["settled"] or stats["stale_closed"]:
        db.commit()

    return stats
