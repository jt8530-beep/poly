#!/usr/bin/env python3
"""
01_scan_lp.py — find Polymarket markets where you can dominate the LP
reward pool with very little capital.

The play (gucky-gu45 / OP esports anecdote):
  Some markets have a daily reward pool ($30-$300/day) but minimal real
  trading. If you place YES bid + ask just inside `rewardsMaxSpread` of
  mid, with `rewardsMinSize` shares each side, you can capture the bulk
  of that day's pool against $5-$50 in capital. The play is especially
  juicy in dead zones (esports mid-game breaks, late US night for non-US
  events) where you have no top-of-book competitors.

This scanner is read-only — no orders, no signing. It outputs a ranked
CSV of opportunities so you can place orders on the Polymarket UI by
hand (Phase 0) or feed into an order placer later (Phase 1).

Outputs:
  data/lp_opportunities.csv   ranked CSV (overwritten each run)

Run:
  python3 scripts/01_scan_lp.py
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lp_market_maker import config as cfg_mod                      # noqa: E402
from lp_market_maker.api import ApiClient                          # noqa: E402
from lp_market_maker.util import (                                 # noqa: E402
    setup_logging, write_csv, utcnow_iso, parse_iso_to_unix,
)


OPP_FIELDS = [
    "scan_ts", "rank",
    "event_slug", "market_question",
    "outcome_title", "yes_token_id",
    "best_bid", "best_ask", "mid", "current_spread",
    "rewards_daily_rate_usd", "rewards_max_spread_cents",
    "rewards_min_size", "holding_rewards",
    "competition", "estimated_capture_pct", "estimated_daily_reward_usd",
    "suggested_bid_price", "suggested_ask_price",
    "suggested_size_shares",
    "capital_inventory_usd", "capital_bid_max_usd", "total_capital_usd",
    "roi_per_day", "hours_to_end", "tags", "event_volume_usd",
]


def _f(x, default=0.0) -> float:
    try: return float(x)
    except (TypeError, ValueError): return default


def _parse_clob_token_ids(market: dict) -> list[str]:
    t = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(t, str):
        try: t = json.loads(t)
        except Exception: return []
    return [str(x) for x in (t or [])]


def _event_tags(event: dict) -> list[str]:
    tags = event.get("tags") or []
    out = []
    for t in tags:
        if isinstance(t, dict):
            lbl = t.get("label")
            if lbl: out.append(lbl)
        elif isinstance(t, str):
            out.append(t)
    return out


def _tag_match(tags: list[str], filter_tags: tuple) -> bool:
    """Case-insensitive: True if any tag in `tags` matches any filter."""
    if not filter_tags:
        return False
    tags_lower = {t.lower() for t in tags}
    for f in filter_tags:
        if f.lower() in tags_lower:
            return True
    return False


def _daily_rate(market: dict) -> float:
    """Sum rewardsDailyRate across all clobRewards entries (per-asset)."""
    cr = market.get("clobRewards") or []
    if not isinstance(cr, list):
        return 0.0
    total = 0.0
    for entry in cr:
        if isinstance(entry, dict):
            total += _f(entry.get("rewardsDailyRate"))
    return total


def analyze_market(event: dict, market: dict, scfg, now_unix: float) -> dict | None:
    # --- need a real reward pool
    daily_rate = _daily_rate(market)
    if daily_rate < scfg.min_daily_rate_usd:
        return None

    # --- v2: tag-based filtering (gate before any expensive analysis)
    tags = _event_tags(event)
    if scfg.blacklist_tags and _tag_match(tags, scfg.blacklist_tags):
        return None
    if scfg.whitelist_tags and not _tag_match(tags, scfg.whitelist_tags):
        return None
    # cap on event volume — big events have pro market makers
    if scfg.max_event_volume_usd > 0 and _f(event.get("volume")) > scfg.max_event_volume_usd:
        return None

    max_spread_cents = _f(market.get("rewardsMaxSpread"))
    if max_spread_cents <= 0:
        return None
    # The field is in cents (e.g. 4.5 = 4.5¢ from mid). Convert to price units.
    max_spread_price = max_spread_cents / 100.0

    min_size = _f(market.get("rewardsMinSize"))
    if min_size <= 0:
        min_size = 100.0  # sane default

    bid = _f(market.get("bestBid"))
    ask = _f(market.get("bestAsk"))
    if scfg.require_both_sides and (bid <= 0 or ask <= 0):
        return None
    if bid <= 0 and ask <= 0:
        return None

    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (ask if ask > 0 else bid)
    if mid < scfg.min_mid_price or mid > scfg.max_mid_price:
        return None

    # Time horizon
    end_iso = market.get("endDate") or event.get("endDate")
    end_unix = parse_iso_to_unix(end_iso)
    if end_unix is None:
        return None
    hours_to_end = (end_unix - now_unix) / 3600.0
    if hours_to_end < scfg.min_hours_to_end:
        return None
    if hours_to_end / 24.0 > scfg.max_days_to_end:
        return None

    # --- competition assessment
    current_spread = ask - bid if (bid > 0 and ask > 0) else max_spread_price * 4
    # If observed spread is much wider than reward band, no one is competing
    # for top-of-book within the band — we'd dominate.
    if current_spread > max_spread_price * scfg.sparse_spread_multiple:
        competition = "sparse"
        capture = scfg.capture_alone_factor
    elif current_spread > max_spread_price * 1.5:
        competition = "moderate"
        capture = (scfg.capture_alone_factor + scfg.capture_competitive) / 2
    else:
        competition = "competitive"
        capture = scfg.capture_competitive

    est_daily = daily_rate * capture

    # --- suggested orders
    # Place inside the reward band by `inside_band_factor` of max_spread.
    # Both bid and ask are in price terms.
    band = max_spread_price * scfg.inside_band_factor
    suggested_bid = max(0.001, round(mid - band, 4))
    suggested_ask = min(0.999, round(mid + band, 4))
    size = min_size * scfg.size_multiplier

    # --- capital required
    # To place an ASK at suggested_ask × size, you need to OWN that many
    # shares first → buy them at current ask = ask price × size.
    # To place a BID at suggested_bid × size, you risk that USD if filled.
    # (For YES side only — we skip NO side LPing in this v1.)
    capital_inventory = ask * size if ask > 0 else suggested_ask * size
    capital_bid_max = suggested_bid * size
    total_capital = capital_inventory + capital_bid_max
    roi_per_day = est_daily / total_capital if total_capital > 0 else 0.0

    yes_token = ""
    tids = _parse_clob_token_ids(market)
    if tids:
        yes_token = tids[0]  # YES is index 0 in negRisk binary structure

    return {
        "scan_ts": utcnow_iso(),
        "event_slug": event.get("slug"),
        "market_question": (market.get("question") or "")[:120],
        "outcome_title": market.get("groupItemTitle") or "",
        "yes_token_id": yes_token,
        "best_bid": round(bid, 4),
        "best_ask": round(ask, 4),
        "mid": round(mid, 4),
        "current_spread": round(current_spread, 4),
        "rewards_daily_rate_usd": round(daily_rate, 2),
        "rewards_max_spread_cents": round(max_spread_cents, 2),
        "rewards_min_size": int(min_size),
        "holding_rewards": "1" if market.get("holdingRewardsEnabled") else "0",
        "competition": competition,
        "estimated_capture_pct": round(capture * 100, 1),
        "estimated_daily_reward_usd": round(est_daily, 2),
        "suggested_bid_price": suggested_bid,
        "suggested_ask_price": suggested_ask,
        "suggested_size_shares": int(size),
        "capital_inventory_usd": round(capital_inventory, 2),
        "capital_bid_max_usd": round(capital_bid_max, 2),
        "total_capital_usd": round(total_capital, 2),
        "roi_per_day": round(roi_per_day, 4),
        "hours_to_end": round(hours_to_end, 1),
        "tags": ",".join(_event_tags(event)[:6]),
        "event_volume_usd": round(_f(event.get("volume")), 2),
    }


def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "lp.scan")
    api = ApiClient(cfg.api)
    scfg = cfg.scan

    log.info("scanning events: max_pages=%d page_size=%d  min_daily_rate=$%.0f",
             scfg.events_max_pages, scfg.events_page_size, scfg.min_daily_rate_usd)
    if scfg.blacklist_tags:
        log.info("  blacklist tags: %s", ", ".join(scfg.blacklist_tags[:8])
                 + ("..." if len(scfg.blacklist_tags) > 8 else ""))
    if scfg.whitelist_tags:
        log.info("  whitelist tags: %s (only events with at least one match)",
                 ", ".join(scfg.whitelist_tags))
    else:
        log.warning("  no whitelist set — recommend setting LP_WHITELIST_TAGS=Esports,Games,Sports,Weather "
                    "to avoid equity-derivative traps (POSTMORTEM.md)")

    events = api.iter_events(page_size=scfg.events_page_size,
                             max_pages=scfg.events_max_pages)
    log.info("pulled %d open events", len(events))

    now = time.time()
    rows: list[dict] = []
    n_no_rewards = 0
    n_no_liquidity = 0
    n_eliminated = 0
    for e in events:
        for m in (e.get("markets") or []):
            if m.get("closed"):
                n_eliminated += 1
                continue
            if not m.get("clobRewards"):
                n_no_rewards += 1
                continue
            r = analyze_market(e, m, scfg, now)
            if r is None:
                n_no_liquidity += 1
                continue
            rows.append(r)

    log.info("scanned: %d eligible  (skipped: no_rewards=%d no_liquidity=%d closed=%d)",
             len(rows), n_no_rewards, n_no_liquidity, n_eliminated)

    # Rank by ROI per day, then by daily reward absolute (so $200/day pool with
    # decent ROI ranks above $30/day pool with great ROI when rare).
    rows.sort(key=lambda r: (-r["roi_per_day"], -r["estimated_daily_reward_usd"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    out = Path(cfg.data_dir) / "lp_opportunities.csv"
    write_csv(out, rows, OPP_FIELDS)
    log.info("wrote %d ranked opportunities → %s", len(rows), out)

    # ---- console summary: top N
    if rows:
        log.info("=== TOP %d OPPORTUNITIES ===", min(scfg.top_n, len(rows)))
        for r in rows[:scfg.top_n]:
            log.info("#%d  %s daily=$%.0f  capture~%.0f%%  est=$%.2f/day  cap=$%.2f  ROI/day=%.2f×",
                     r["rank"], r["competition"], r["rewards_daily_rate_usd"],
                     r["estimated_capture_pct"], r["estimated_daily_reward_usd"],
                     r["total_capital_usd"], r["roi_per_day"])
            log.info("       q=%s", r["market_question"][:100])
            log.info("       bid %.4f / mid %.4f / ask %.4f   suggest BID@%.4f ASK@%.4f size=%d",
                     r["best_bid"], r["mid"], r["best_ask"],
                     r["suggested_bid_price"], r["suggested_ask_price"],
                     r["suggested_size_shares"])
            log.info("       end in %.1fh  vol=$%.0f  tags=%s",
                     r["hours_to_end"], r["event_volume_usd"], r["tags"][:50])
    return 0


if __name__ == "__main__":
    sys.exit(main())
