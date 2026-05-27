#!/usr/bin/env python3
"""
07_paper_trade.py — long-running paper trader for the overround_arb strategy.

What it does
============
Reads `data/overround_opportunities.csv` produced by `01_scan_events.py`
and simulates buying the basket of YES tokens on every flagged opportunity
that meets the (stricter) paper-trader entry criteria. Tracks each open
position via gamma `/events?slug=X` queries and closes the position when
the event settles or pre-defined take-profit / stop-loss triggers fire.

This is **paper-only** — no orders, no signing, no keys. It tells you
whether the printed scanner edge survives the round trip.

Outputs (CSV unless noted)
==========================
data/paper_positions.csv         open + closed positions, one row each
data/paper_position_legs.csv     per-market entry leg, one row per (position, market)
data/paper_trades_closed.csv     realized PnL log (subset of positions)
data/paper_position_mtm.jsonl    every refresh cycle's MtM snapshot per open position

Position math
=============
Buy `basket_units = budget / total_ask_at_entry` shares of each YES outcome.
At settlement the basket payout = basket_units (one outcome wins, paying $1
per share; the rest pay $0). PnL_settle = basket_units - cost = budget *
((1 / total_ask_at_entry) - 1).

For early MtM, value = basket_units * current_total_bid (immediate liquidation).

Exit policy
===========
- Pure `arb` (entry_total_ask < 1.0): hold to settlement. Take-profit can
  still fire if MtM_bid surges; stop-loss is disabled by default
  (OA_PT_NO_SL_ON_ARB=1).
- `near_arb` / `premium_harvest`: take-profit at +TP%, stop-loss at -SL%,
  otherwise hold to settlement.
- Settled events are detected by event.closed=True. Winner is the one
  market whose outcomePrices indicates YES = 1.0.

Resilient to crashes — all state lives in the CSVs. Re-running picks up
where it left off.
"""
from __future__ import annotations
import csv
import json
import os
import signal
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from overround_arb import config as cfg_mod                       # noqa: E402
from overround_arb.api import ApiClient                           # noqa: E402
from overround_arb.util import (                                  # noqa: E402
    setup_logging, append_csv, read_csv, utcnow_iso,
    parse_iso_to_unix,
)


POSITION_FIELDS = [
    "position_id", "opened_at", "status",
    "classification", "event_id", "event_slug", "title",
    "entry_total_ask", "entry_total_bid", "basket_units", "cost",
    "n_markets_at_entry", "days_to_end_at_entry",
    "closed_at", "exit_reason", "exit_value", "realized_pnl", "realized_pnl_pct",
    "winner_outcome_title",
]

LEG_FIELDS = [
    "position_id", "market_id", "outcome_title", "asset_id",
    "entry_ask", "shares",
]

CLOSED_FIELDS = [
    "closed_at", "position_id", "classification", "event_slug", "title",
    "opened_at", "hold_days",
    "entry_total_ask", "exit_value", "cost",
    "realized_pnl", "realized_pnl_pct",
    "exit_reason", "winner_outcome_title",
]


_running = True

def _stop(signum, frame):                                         # noqa: ARG001
    global _running
    _running = False

signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _f(x, default=0.0) -> float:
    try: return float(x)
    except (TypeError, ValueError): return default


def _parse_clob_token_ids(market: dict) -> list[str]:
    """clobTokenIds is JSON-encoded as a string; index 0 = YES, index 1 = NO."""
    t = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except Exception:
            return []
    return [str(x) for x in (t or [])]


def _parse_outcome_prices(market: dict) -> list[float]:
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            return []
    if not isinstance(op, list):
        return []
    out = []
    for v in op:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _market_active(m: dict, max_ask: float) -> bool:
    if m.get("closed"):
        return False
    ask = _f(m.get("bestAsk"))
    return 0.0 < ask < max_ask


def _basket_totals(markets: list[dict], max_ask: float) -> tuple[float, float, int]:
    total_ask = 0.0
    total_bid = 0.0
    n_active = 0
    for m in markets:
        if not _market_active(m, max_ask):
            continue
        total_ask += _f(m.get("bestAsk"))
        total_bid += _f(m.get("bestBid"))
        n_active += 1
    return total_ask, total_bid, n_active


def _winner_yes_market(markets: list[dict]) -> dict | None:
    """In a settled negRisk basket, exactly one market has outcomePrices[0] (YES) = 1.0."""
    for m in markets:
        prices = _parse_outcome_prices(m)
        if len(prices) >= 1 and prices[0] >= 0.999:
            return m
    return None


def _all_markets_settled(markets: list[dict]) -> bool:
    """Heuristic: settled when every market has a definitive outcomePrice."""
    if not markets:
        return False
    for m in markets:
        prices = _parse_outcome_prices(m)
        if not prices:
            return False
        if max(prices) < 0.999:
            return False
    return True


# ---------------------------------------------------------------------------
# state I/O
# ---------------------------------------------------------------------------
def load_positions(path: Path) -> list[dict]:
    return read_csv(path)


def open_position_rows(positions: list[dict]) -> list[dict]:
    return [p for p in positions if p.get("status") == "open"]


def upsert_position(positions: list[dict], path: Path, row: dict) -> None:
    """Append-or-replace a single position by position_id, then rewrite the CSV."""
    pid = row["position_id"]
    found = False
    for i, p in enumerate(positions):
        if p.get("position_id") == pid:
            positions[i] = row
            found = True
            break
    if not found:
        positions.append(row)
    # rewrite from scratch (small file — open positions never grow huge)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=POSITION_FIELDS, extrasaction="ignore")
        w.writeheader()
        for p in positions:
            w.writerow(p)


# ---------------------------------------------------------------------------
# paper-trade primitives
# ---------------------------------------------------------------------------
def evaluate_entry(opp: dict, paper_cfg) -> tuple[bool, str]:
    """Should we open a position for this scanner row?"""
    cls = (opp.get("classification") or "").lower()
    if cls not in paper_cfg.entry_classes:
        return False, f"class={cls!r} not in {paper_cfg.entry_classes}"
    total_ask = _f(opp.get("total_ask"))
    if total_ask <= 0 or total_ask > paper_cfg.max_total_ask_entry:
        return False, f"total_ask {total_ask} > max {paper_cfg.max_total_ask_entry}"
    days = _f(opp.get("days_to_end"))
    if days < paper_cfg.min_days_to_end or days > paper_cfg.max_days_to_end:
        return False, f"days_to_end {days} outside [{paper_cfg.min_days_to_end}, {paper_cfg.max_days_to_end}]"
    vol = _f(opp.get("volume"))
    if vol < paper_cfg.min_volume_at_entry:
        return False, f"volume ${vol:.0f} < ${paper_cfg.min_volume_at_entry:.0f}"
    return True, "ok"


def open_position(api: ApiClient, opp: dict, paper_cfg, scan_cfg, log) -> tuple[dict, list[dict]] | None:
    """
    Re-fetch live event, snap basket prices, and write position + legs.
    Returns (position_row, leg_rows) or None on failure.
    """
    slug = opp.get("event_slug")
    if not slug:
        return None
    event = api.get_event_by_slug(slug)
    if not event or not event.get("markets"):
        log.warning("could not fetch event for slug=%s", slug)
        return None
    if event.get("closed"):
        log.info("skip slug=%s: event already closed", slug)
        return None

    markets = event["markets"]
    active = [m for m in markets if _market_active(m, scan_cfg.max_market_ask)]
    if len(active) < scan_cfg.min_outcomes:
        log.info("skip slug=%s: only %d active markets", slug, len(active))
        return None

    total_ask, total_bid, _ = _basket_totals(markets, scan_cfg.max_market_ask)
    if total_ask <= 0 or total_ask > paper_cfg.max_total_ask_entry:
        log.info("skip slug=%s: live total_ask=%.4f", slug, total_ask)
        return None

    end_unix = parse_iso_to_unix(event.get("endDate"))
    days_to_end = (end_unix - time.time()) / 86400.0 if end_unix else 9999.0
    if days_to_end < paper_cfg.min_days_to_end:
        log.info("skip slug=%s: only %.1f days to end", slug, days_to_end)
        return None

    basket_units = paper_cfg.budget_per_basket / total_ask
    cost = basket_units * total_ask  # exactly budget_per_basket
    pid = "p_" + uuid.uuid4().hex[:12]

    # build legs (one per active market — at settlement we hold every outcome)
    legs: list[dict] = []
    for m in active:
        token_ids = _parse_clob_token_ids(m)
        yes_token = token_ids[0] if token_ids else ""
        legs.append({
            "position_id": pid,
            "market_id": m.get("conditionId") or "",
            "outcome_title": m.get("groupItemTitle") or m.get("question") or "",
            "asset_id": yes_token,
            "entry_ask": round(_f(m.get("bestAsk")), 6),
            "shares": round(basket_units, 6),
        })

    pos = {
        "position_id": pid,
        "opened_at": utcnow_iso(),
        "status": "open",
        "classification": opp.get("classification"),
        "event_id": event.get("id"),
        "event_slug": slug,
        "title": (event.get("title") or "")[:120],
        "entry_total_ask": round(total_ask, 6),
        "entry_total_bid": round(total_bid, 6),
        "basket_units": round(basket_units, 6),
        "cost": round(cost, 4),
        "n_markets_at_entry": len(active),
        "days_to_end_at_entry": round(days_to_end, 1),
        "closed_at": "",
        "exit_reason": "",
        "exit_value": "",
        "realized_pnl": "",
        "realized_pnl_pct": "",
        "winner_outcome_title": "",
    }
    return pos, legs


def evaluate_exit(pos: dict, event: dict | None, paper_cfg, scan_cfg, now: float) -> tuple[str, float, str]:
    """
    Decide whether to close this position.
    Returns (reason, exit_value, winner_outcome_title) where reason='' means HOLD.
    exit_value is the dollar payout we'd receive on this exit path.
    """
    basket_units = _f(pos.get("basket_units"))
    cost = _f(pos.get("cost")) or paper_cfg.budget_per_basket

    # If the event vanished, hold (and warn upstream)
    if event is None:
        return "", 0.0, ""

    markets = event.get("markets") or []

    # 1. Settlement detection — closed event + every market resolved
    if event.get("closed") or _all_markets_settled(markets):
        winner = _winner_yes_market(markets)
        winner_title = ""
        if winner is not None:
            winner_title = winner.get("groupItemTitle") or winner.get("question") or ""
            # We hold every outcome, so payout = basket_units * $1
            payout = basket_units * 1.0
        else:
            # Could not detect winner from outcomePrices — fall back to no payout
            # so the position records a worst-case settlement
            payout = 0.0
        return "settled", round(payout, 4), winner_title

    # 2. MtM-based early exit (only if event still live)
    total_ask, total_bid, n_active = _basket_totals(markets, scan_cfg.max_market_ask)
    if n_active <= 0 or total_bid <= 0:
        return "", 0.0, ""
    mtm_value = basket_units * total_bid
    pnl = mtm_value - cost
    pnl_pct = pnl / cost if cost else 0.0
    cls = (pos.get("classification") or "").lower()

    # 2a. Take profit
    if pnl_pct >= paper_cfg.take_profit_pct:
        return "take_profit", round(mtm_value, 4), ""

    # 2b. Stop loss (skipped for arb-class positions if so configured)
    if not (cls == "arb" and paper_cfg.skip_stoploss_for_arb):
        if pnl_pct <= -paper_cfg.stop_loss_pct:
            return "stop_loss", round(mtm_value, 4), ""

    # 2c. Force-close near settlement (optional)
    if paper_cfg.force_close_days > 0:
        end_unix = parse_iso_to_unix(event.get("endDate"))
        if end_unix is not None:
            days_left = (end_unix - now) / 86400.0
            if days_left <= paper_cfg.force_close_days:
                return "force_close", round(mtm_value, 4), ""

    return "", 0.0, ""


def write_mtm_snapshot(path: Path, pos: dict, event: dict | None, scan_cfg) -> None:
    """Append a JSONL line per refresh — feeds into trend analysis."""
    rec = {
        "ts": int(time.time()),
        "position_id": pos.get("position_id"),
        "event_slug": pos.get("event_slug"),
        "classification": pos.get("classification"),
        "entry_total_ask": _f(pos.get("entry_total_ask")),
        "basket_units": _f(pos.get("basket_units")),
        "cost": _f(pos.get("cost")),
    }
    if event:
        markets = event.get("markets") or []
        total_ask, total_bid, n_active = _basket_totals(markets, scan_cfg.max_market_ask)
        rec.update({
            "current_total_ask": round(total_ask, 6),
            "current_total_bid": round(total_bid, 6),
            "current_active_markets": n_active,
            "event_closed": bool(event.get("closed")),
        })
        bu = _f(pos.get("basket_units"))
        cost = _f(pos.get("cost"))
        if bu and cost:
            mtm = bu * total_bid
            rec["mtm_bid"] = round(mtm, 4)
            rec["mtm_pnl"] = round(mtm - cost, 4)
            rec["mtm_pnl_pct"] = round((mtm - cost) / cost, 6) if cost else 0.0
    else:
        rec["event_missing"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "oa.paper")
    api = ApiClient(cfg.api)
    pcfg = cfg.paper
    scfg = cfg.scan

    positions_path = Path(pcfg.positions_path)
    legs_path      = Path(pcfg.legs_path)
    closed_path    = Path(pcfg.closed_path)
    snap_path      = Path(pcfg.snapshots_path)
    opps_path      = Path(pcfg.opportunities_path)

    log.info("paper trader starting. budget=$%.0f/basket  max_open=%d  classes=%s  TP=%.0f%%  SL=%.0f%%",
             pcfg.budget_per_basket, pcfg.max_open_positions,
             pcfg.entry_classes, 100*pcfg.take_profit_pct, 100*pcfg.stop_loss_pct)

    rounds = 0
    while _running:
        round_start = time.time()
        rounds += 1

        # ---------- load state ----------
        positions = load_positions(positions_path)
        open_pos = open_position_rows(positions)
        log.info("round %d: %d open positions", rounds, len(open_pos))

        # ---------- ENTRY: scan opportunities and open new positions ----------
        if not opps_path.exists():
            log.warning("no opportunities CSV at %s — has 01_scan_events.py run yet?", opps_path)
        else:
            opps = read_csv(opps_path)
            open_slugs = {p.get("event_slug") for p in open_pos}
            already_closed_slugs = {p.get("event_slug") for p in positions if p.get("status") == "closed"}
            n_skipped_open = 0
            n_skipped_filter = 0
            n_opened = 0
            for opp in opps:
                if len(open_pos) + n_opened >= pcfg.max_open_positions:
                    log.info("max_open_positions=%d reached, stopping entries this round",
                             pcfg.max_open_positions)
                    break
                slug = opp.get("event_slug")
                if not slug:
                    continue
                if slug in open_slugs:
                    n_skipped_open += 1
                    continue
                if slug in already_closed_slugs:
                    # don't reopen positions we've already taken to completion
                    continue
                ok, reason = evaluate_entry(opp, pcfg)
                if not ok:
                    n_skipped_filter += 1
                    continue
                result = open_position(api, opp, pcfg, scfg, log)
                if result is None:
                    continue
                pos_row, leg_rows = result
                upsert_position(positions, positions_path, pos_row)
                append_csv(legs_path, leg_rows, LEG_FIELDS)
                n_opened += 1
                log.info("OPEN %s class=%s slug=%s total_ask=%.4f cost=$%.2f units=%.2f",
                         pos_row["position_id"], pos_row["classification"],
                         pos_row["event_slug"], _f(pos_row["entry_total_ask"]),
                         _f(pos_row["cost"]), _f(pos_row["basket_units"]))
                # refresh derived sets
                open_pos.append(pos_row)
                open_slugs.add(slug)
            log.info("entries: opened=%d  skipped_open=%d  skipped_filter=%d",
                     n_opened, n_skipped_open, n_skipped_filter)

        # ---------- REFRESH + EXIT ----------
        n_closed = 0
        n_held = 0
        for pos in list(open_pos):
            if not _running:
                break
            slug = pos.get("event_slug")
            event = api.get_event_by_slug(slug) if slug else None
            write_mtm_snapshot(snap_path, pos, event, scfg)
            reason, exit_value, winner_title = evaluate_exit(pos, event, pcfg, scfg, time.time())
            if not reason:
                n_held += 1
                continue
            cost = _f(pos.get("cost")) or pcfg.budget_per_basket
            pnl = exit_value - cost
            pnl_pct = pnl / cost if cost else 0.0
            opened_unix = parse_iso_to_unix(pos.get("opened_at")) or time.time()
            hold_days = (time.time() - opened_unix) / 86400.0

            pos.update({
                "status": "closed",
                "closed_at": utcnow_iso(),
                "exit_reason": reason,
                "exit_value": round(exit_value, 4),
                "realized_pnl": round(pnl, 4),
                "realized_pnl_pct": round(pnl_pct, 6),
                "winner_outcome_title": winner_title,
            })
            upsert_position(positions, positions_path, pos)
            append_csv(closed_path, [{
                "closed_at": pos["closed_at"],
                "position_id": pos["position_id"],
                "classification": pos.get("classification"),
                "event_slug": slug,
                "title": pos.get("title"),
                "opened_at": pos.get("opened_at"),
                "hold_days": round(hold_days, 2),
                "entry_total_ask": pos.get("entry_total_ask"),
                "exit_value": pos["exit_value"],
                "cost": pos.get("cost"),
                "realized_pnl": pos["realized_pnl"],
                "realized_pnl_pct": pos["realized_pnl_pct"],
                "exit_reason": reason,
                "winner_outcome_title": winner_title,
            }], CLOSED_FIELDS)
            n_closed += 1
            log.info("CLOSE %s reason=%s pnl=$%+.2f (%+.1f%%) hold=%.1fd  %s",
                     pos["position_id"], reason, pnl, 100*pnl_pct, hold_days,
                     pos.get("title", "")[:60])
        if n_held or n_closed:
            log.info("refresh: %d held, %d closed", n_held, n_closed)

        # ---------- sleep ----------
        elapsed = time.time() - round_start
        sleep_for = max(5.0, pcfg.interval_sec - elapsed)
        end_at = time.time() + sleep_for
        while _running and time.time() < end_at:
            time.sleep(min(1.0, end_at - time.time()))

    log.info("paper trader stopped after %d rounds.", rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
