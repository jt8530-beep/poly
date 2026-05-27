#!/usr/bin/env python3
"""
01_scan_events.py — find Polymarket multi-outcome events trading at a
discount or low premium relative to their guaranteed $1 settlement value.

Reads:
  (live) gamma /events

Writes:
  data/overround_opportunities.csv         — current snapshot, overwritten each run
  data/scan_history.csv                    — appended (only if OA_APPEND_HISTORY=1)

Strategy background:
  Polymarket "negRisk" events are mutually-exclusive baskets — exactly one
  outcome resolves YES at $1, the rest at $0. If you can buy the YES side
  of EVERY outcome for a total below $1, you are guaranteed profit on
  settlement. Even a small premium (1.00–1.10) can be exploited the
  gucky-gu45 way: buy the basket cheaply when liquidity is thin, then sell
  individual positions back to the market as attention/inflow pushes the
  total ask above $1.

What this scanner does NOT do:
  * Trade. This is read-only.
  * Account for transaction costs / gas / API fees.
  * Verify orderbook depth (so a printed "arb" might disappear once you
    hit the book — `total_bid` and per-market liquidity in the CSV are
    your sanity check).

Classification (sorted ascending by `total_ask`):
  arb              total_ask < OA_ARB_MAX           (default < 1.00)
  near_arb         total_ask < OA_NEAR_ARB_MAX      (default < 1.05)
  premium_harvest  total_ask < OA_PREMIUM_MAX       AND days_to_end >= OA_PREMIUM_MIN_DAYS
  overpriced       total_ask >= OA_PREMIUM_MAX
  anomalous        total_ask outside [sanity_min, sanity_max]
                   (likely contaminated event — investigate manually)

Run:
  python3 scripts/01_scan_events.py
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from overround_arb import config as cfg_mod                       # noqa: E402
from overround_arb.api import ApiClient                           # noqa: E402
from overround_arb.util import (                                  # noqa: E402
    setup_logging, write_csv, append_csv, utcnow_iso,
    parse_iso_to_unix,
)


OPP_FIELDS = [
    "scan_ts", "classification", "event_id", "event_slug", "title",
    "n_markets_total", "n_markets_active", "n_markets_eliminated",
    "total_ask", "total_bid", "edge_usd_per_dollar",
    "spread", "volume", "liquidity", "end_date", "days_to_end",
    "top_outcome_title", "top_outcome_ask",
    "second_outcome_title", "second_outcome_ask",
    "negRiskMarketID",
]


def _f(x, default=0.0) -> float:
    try: return float(x)
    except (TypeError, ValueError): return default


def _classify(total_ask: float, days_to_end: float, scfg) -> str:
    if total_ask < scfg.sanity_total_min or total_ask > scfg.sanity_total_max:
        return "anomalous"
    if total_ask < scfg.arb_max:
        return "arb"
    if total_ask < scfg.near_arb_max:
        return "near_arb"
    if total_ask < scfg.premium_max and days_to_end >= scfg.premium_min_days:
        return "premium_harvest"
    return "overpriced"


def analyze_event(event: dict, scfg, now_unix: float) -> dict | None:
    """Return an opportunity row, or None if event not eligible at all."""
    # event-level eligibility
    if not event.get("negRisk"):
        return None
    if event.get("closed"):
        return None

    end_iso = event.get("endDate")
    end_unix = parse_iso_to_unix(end_iso)
    days_to_end = (end_unix - now_unix) / 86400.0 if end_unix else 9999.0
    if days_to_end < scfg.days_until_end_min:
        return None
    if days_to_end > scfg.days_until_end_max:
        return None

    volume = _f(event.get("volume"))
    if volume < scfg.min_event_volume:
        return None

    markets = event.get("markets") or []
    if not markets:
        return None

    # market-level filter — drop closed + placeholder asks
    active = []
    eliminated = 0
    for m in markets:
        if m.get("closed"):
            eliminated += 1
            continue
        ask = _f(m.get("bestAsk"))
        if ask <= scfg.min_market_ask:
            eliminated += 1
            continue
        if ask >= scfg.max_market_ask:
            # team eliminated / no real liquidity — exclude from sum but count
            eliminated += 1
            continue
        active.append(m)

    if len(active) < scfg.min_outcomes:
        return None

    total_ask = sum(_f(m.get("bestAsk")) for m in active)
    total_bid = sum(_f(m.get("bestBid")) for m in active)
    spread = total_ask - total_bid

    classification = _classify(total_ask, days_to_end, scfg)

    # top two by ask (the favorites — gives readers a sense of the field)
    by_ask = sorted(active, key=lambda m: -_f(m.get("bestAsk")))
    top1 = by_ask[0]
    top2 = by_ask[1] if len(by_ask) > 1 else None

    return {
        "scan_ts": utcnow_iso(),
        "classification": classification,
        "event_id": event.get("id"),
        "event_slug": event.get("slug"),
        "title": (event.get("title") or "")[:120],
        "n_markets_total": len(markets),
        "n_markets_active": len(active),
        "n_markets_eliminated": eliminated,
        "total_ask": round(total_ask, 4),
        "total_bid": round(total_bid, 4),
        "edge_usd_per_dollar": round(1.0 - total_ask, 4),  # > 0 means arb
        "spread": round(spread, 4),
        "volume": round(volume, 2),
        "liquidity": round(_f(event.get("liquidity")), 2),
        "end_date": end_iso or "",
        "days_to_end": round(days_to_end, 1),
        "top_outcome_title": (top1.get("groupItemTitle")
                              or top1.get("question") or "")[:60],
        "top_outcome_ask": round(_f(top1.get("bestAsk")), 4),
        "second_outcome_title": ((top2 or {}).get("groupItemTitle")
                                 or (top2 or {}).get("question") or "")[:60] if top2 else "",
        "second_outcome_ask": round(_f((top2 or {}).get("bestAsk")), 4) if top2 else "",
        "negRiskMarketID": event.get("negRiskMarketID") or "",
    }


def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "oa.scan")
    api = ApiClient(cfg.api)
    scfg = cfg.scan

    log.info("scanning events: page_size=%d max_pages=%d  min_outcomes=%d  min_volume=$%.0f",
             scfg.events_page_size, scfg.events_max_pages,
             scfg.min_outcomes, scfg.min_event_volume)

    events = api.iter_events(page_size=scfg.events_page_size,
                             max_pages=scfg.events_max_pages,
                             closed=False)
    log.info("pulled %d open events", len(events))

    now = time.time()
    rows: list[dict] = []
    skipped = {"not_negrisk": 0, "no_active_markets": 0, "low_volume": 0,
               "out_of_horizon": 0, "no_markets": 0}

    for e in events:
        # Pre-filter for cheap diagnostics
        if not e.get("negRisk"):
            skipped["not_negrisk"] += 1
            continue
        v = _f(e.get("volume"))
        if v < scfg.min_event_volume:
            skipped["low_volume"] += 1
            continue
        end = parse_iso_to_unix(e.get("endDate"))
        if end is not None:
            days = (end - now) / 86400.0
            if days < scfg.days_until_end_min or days > scfg.days_until_end_max:
                skipped["out_of_horizon"] += 1
                continue
        if not (e.get("markets") or []):
            skipped["no_markets"] += 1
            continue

        row = analyze_event(e, scfg, now)
        if row is None:
            skipped["no_active_markets"] += 1
            continue
        rows.append(row)

    log.info("eligible: %d events  skipped: %s", len(rows), skipped)

    # sort: arb first, then near_arb, then premium_harvest, then overpriced, then anomalous
    order = {"arb": 0, "near_arb": 1, "premium_harvest": 2, "overpriced": 3, "anomalous": 4}
    rows.sort(key=lambda r: (order.get(r["classification"], 9), r["total_ask"]))

    out = Path(cfg.data_dir) / "overround_opportunities.csv"
    write_csv(out, rows, OPP_FIELDS)
    log.info("wrote %d rows → %s", len(rows), out)

    if os.getenv("OA_APPEND_HISTORY", "0") == "1":
        hist = Path(cfg.data_dir) / "scan_history.csv"
        append_csv(hist, rows, OPP_FIELDS)
        log.info("appended %d rows → %s (history mode)", len(rows), hist)

    # console readout — surface the actionable buckets
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(r["classification"], []).append(r)

    for cls in ("arb", "near_arb", "premium_harvest"):
        items = by_class.get(cls, [])
        if not items:
            log.info("=== %s: 0 ===", cls)
            continue
        log.info("=== %s: %d ===", cls, len(items))
        for r in items[:10]:
            log.info("  total_ask=%.4f edge=%+.3f n=%d/%d  vol=$%.0f  end=%s (%dd)  %s",
                     r["total_ask"], r["edge_usd_per_dollar"],
                     r["n_markets_active"], r["n_markets_total"],
                     r["volume"], r["end_date"][:10], int(r["days_to_end"]),
                     r["title"][:50])
            log.info("       fav: %s @ %.3f%s",
                     r["top_outcome_title"], r["top_outcome_ask"],
                     f"   2nd: {r['second_outcome_title']} @ {r['second_outcome_ask']}"
                     if r["second_outcome_title"] else "")

    n_anom = len(by_class.get("anomalous", []))
    if n_anom:
        log.info("note: %d events flagged 'anomalous' (total_ask outside sanity range — "
                 "may be contaminated with prop markets)", n_anom)

    return 0


if __name__ == "__main__":
    sys.exit(main())
