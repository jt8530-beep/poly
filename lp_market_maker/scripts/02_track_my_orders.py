#!/usr/bin/env python3
"""
02_track_my_orders.py — read-only tracker for the LP market-maker strategy.

Given a Polymarket proxy wallet address, pulls:
  * current positions (data-api /positions)
  * recent trades (data-api /trades)
  * activity feed (data-api /activity) — looks for REWARD entries
  * portfolio value (data-api /value)
  * gamma /events for live mid prices on each position

Filters out non-LP-strategy positions (e.g., btc-updown-* slugs that come
from a co-running quant bot on the same wallet — see config knob below).

Outputs:
  console: human-readable PnL summary by position + portfolio totals
  data/tracker_<date>.csv: per-position snapshot for trend analysis

Run:
  python3 scripts/02_track_my_orders.py 0xYOURPROXYADDR
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from lp_market_maker import config as cfg_mod                    # noqa: E402
from lp_market_maker.util import setup_logging, write_csv        # noqa: E402


# Slug prefix exclusions — non-LP-strategy positions co-living on the wallet.
# The btc5m_strategy quant runs on the same Polymarket proxy and creates
# btc-updown-5m-* positions. We don't want to confuse those into LP PnL.
EXCLUDED_SLUG_PREFIXES = (
    "btc-updown-",      # btc5m_strategy quant
    "eth-updown-",
    "sol-updown-",
)

POSITION_FIELDS = [
    "snapshot_ts", "wallet", "slug", "title", "outcome", "side",
    "size", "entry_avg_price", "current_price", "cost_basis_usd",
    "mtm_value_usd", "unrealized_pnl_usd", "unrealized_pnl_pct",
    "asset_id", "condition_id", "is_lp_position",
]


def _f(x, default=0.0) -> float:
    try: return float(x)
    except (TypeError, ValueError): return default


def _slug_excluded(slug: str) -> bool:
    s = (slug or "").lower()
    return any(s.startswith(p) for p in EXCLUDED_SLUG_PREFIXES)


def _http_get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(
        url, headers={"User-Agent": "lp-tracker/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_positions(addr: str) -> list[dict]:
    try:
        return _http_get(f"https://data-api.polymarket.com/positions?user={addr}&limit=200") or []
    except Exception as e:
        print(f"  ! positions fetch failed: {e}", file=sys.stderr)
        return []


def fetch_trades(addr: str, limit: int = 500) -> list[dict]:
    try:
        return _http_get(f"https://data-api.polymarket.com/trades?user={addr}&limit={limit}") or []
    except Exception as e:
        print(f"  ! trades fetch failed: {e}", file=sys.stderr)
        return []


def fetch_activity(addr: str, limit: int = 200) -> list[dict]:
    try:
        return _http_get(f"https://data-api.polymarket.com/activity?user={addr}&limit={limit}") or []
    except Exception as e:
        print(f"  ! activity fetch failed: {e}", file=sys.stderr)
        return []


def fetch_value(addr: str) -> float | None:
    try:
        v = _http_get(f"https://data-api.polymarket.com/value?user={addr}")
        if isinstance(v, list) and v:
            return _f(v[0].get("value"))
        if isinstance(v, dict):
            return _f(v.get("value"))
    except Exception:
        pass
    return None


def fetch_event_by_slug(slug: str) -> dict | None:
    try:
        out = _http_get(
            f"https://gamma-api.polymarket.com/events?slug={urllib.parse.quote(slug)}")
        if isinstance(out, list) and out:
            return out[0]
    except Exception:
        pass
    return None


def find_market_by_asset(event: dict, asset_id: str) -> dict | None:
    for m in (event.get("markets") or []):
        ids = m.get("clobTokenIds")
        if isinstance(ids, str):
            try: ids = json.loads(ids)
            except Exception: ids = []
        for tid in (ids or []):
            if str(tid) == str(asset_id):
                return m
    return None


def build_cost_basis(trades: list[dict], activity: list[dict] | None = None) -> dict[str, dict]:
    """
    Build per-asset (proxy for per-position) cost basis from trade history.
    Merges /trades + /activity (TRADE entries) — /trades sometimes paginates
    older trades while /activity surfaces recent ones; using both improves
    coverage of fresh positions.
    Returns:  asset_id -> {qty_bought, qty_sold, total_buy_usd, total_sell_usd}
    """
    out: dict[str, dict] = defaultdict(lambda: {
        "qty_bought": 0.0, "qty_sold": 0.0,
        "total_buy_usd": 0.0, "total_sell_usd": 0.0,
        "first_buy_ts": None, "last_trade_ts": None,
    })

    # Dedupe by transaction hash so we don't double-count when a trade
    # appears in both feeds.
    seen_tx: set[str] = set()

    def absorb(records: list[dict], is_activity: bool = False):
        for t in records:
            if is_activity and t.get("type") != "TRADE":
                continue
            tx = t.get("transactionHash") or t.get("hash") or ""
            if tx and tx in seen_tx:
                continue
            asset = t.get("asset")
            if not asset:
                continue
            side = (t.get("side") or "").upper()
            size = _f(t.get("size"))
            price = _f(t.get("price"))
            ts = _f(t.get("timestamp"))
            if size <= 0 or price <= 0:
                continue
            d = out[asset]
            if side == "BUY":
                d["qty_bought"] += size
                d["total_buy_usd"] += size * price
                if d["first_buy_ts"] is None or ts < d["first_buy_ts"]:
                    d["first_buy_ts"] = ts
            elif side == "SELL":
                d["qty_sold"] += size
                d["total_sell_usd"] += size * price
            if d["last_trade_ts"] is None or ts > d["last_trade_ts"]:
                d["last_trade_ts"] = ts
            if tx:
                seen_tx.add(tx)

    absorb(trades or [])
    absorb(activity or [], is_activity=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("address", nargs="?", default=os.getenv("LP_TRACK_ADDRESS", ""),
                    help="Polymarket proxy wallet address (or env LP_TRACK_ADDRESS)")
    ap.add_argument("--include-quant", action="store_true",
                    help="Also include btc-updown-* and similar quant positions in PnL totals")
    args = ap.parse_args()

    addr = (args.address or "").strip().lower()
    if not addr or not addr.startswith("0x"):
        print("usage: 02_track_my_orders.py <0xPROXYADDRESS>")
        return 2

    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "lp.track")
    log.info("tracking wallet %s", addr)

    # --- pull all data
    positions = fetch_positions(addr)
    trades = fetch_trades(addr, limit=500)
    activity = fetch_activity(addr, limit=200)
    portfolio_value = fetch_value(addr)
    log.info("pulled: %d positions, %d trades, %d activity items, value=$%s",
             len(positions), len(trades), len(activity),
             f"{portfolio_value:.2f}" if portfolio_value is not None else "?")

    # --- cost basis index (merged trades + activity feed)
    basis = build_cost_basis(trades, activity)

    # --- per-position snapshot
    rows: list[dict] = []
    lp_total_cost = 0.0
    lp_total_mtm = 0.0
    quant_total_cost = 0.0
    quant_total_mtm = 0.0
    snap_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for p in positions:
        slug = p.get("slug") or ""
        is_quant = _slug_excluded(slug)
        is_lp = not is_quant

        size = _f(p.get("size"))
        if size <= 1e-9:
            continue
        asset = p.get("asset")
        b = basis.get(asset, {}) if asset else {}
        net_qty = b.get("qty_bought", 0.0) - b.get("qty_sold", 0.0)
        net_cost = b.get("total_buy_usd", 0.0) - b.get("total_sell_usd", 0.0)
        # avg entry price for the held portion
        entry_avg = (net_cost / net_qty) if net_qty > 1e-9 else 0.0

        # current price via gamma
        current_price = 0.0
        market = None
        ev = fetch_event_by_slug(slug) if slug else None
        if ev and asset:
            market = find_market_by_asset(ev, asset)
            if market:
                # for a YES position, use the bid (we'd sell at bid)
                bid = _f(market.get("bestBid"))
                ask = _f(market.get("bestAsk"))
                # MtM at bid is conservative (immediate liquidation)
                current_price = bid if bid > 0 else (ask if ask > 0 else _f(p.get("curPrice")))
        if current_price <= 0:
            current_price = _f(p.get("curPrice"))

        cost_basis_usd = entry_avg * size if entry_avg > 0 else _f(p.get("initialValue"))
        mtm_value_usd = size * current_price
        unreal = mtm_value_usd - cost_basis_usd
        unreal_pct = (unreal / cost_basis_usd) if cost_basis_usd > 0 else 0.0

        if is_lp:
            lp_total_cost += cost_basis_usd
            lp_total_mtm += mtm_value_usd
        else:
            quant_total_cost += cost_basis_usd
            quant_total_mtm += mtm_value_usd

        rows.append({
            "snapshot_ts": snap_ts,
            "wallet": addr,
            "slug": slug,
            "title": (p.get("title") or "")[:120],
            "outcome": p.get("outcome") or "",
            "side": "YES" if (p.get("outcomeIndex") in (0, "0", None)) else "NO",
            "size": round(size, 4),
            "entry_avg_price": round(entry_avg, 4),
            "current_price": round(current_price, 4),
            "cost_basis_usd": round(cost_basis_usd, 4),
            "mtm_value_usd": round(mtm_value_usd, 4),
            "unrealized_pnl_usd": round(unreal, 4),
            "unrealized_pnl_pct": round(unreal_pct, 4),
            "asset_id": asset or "",
            "condition_id": p.get("conditionId") or "",
            "is_lp_position": "1" if is_lp else "0",
        })

    # --- console report
    print(f"\n=== Wallet {addr} @ {snap_ts} ===")
    if portfolio_value is not None:
        print(f"Polymarket portfolio /value endpoint says: ${portfolio_value:.2f}")
    else:
        print("Polymarket /value: unknown")

    print(f"\n--- LP-strategy positions ---")
    lp_rows = [r for r in rows if r["is_lp_position"] == "1"]
    if not lp_rows:
        print("  (none)")
    else:
        print(f"  {'slug':<40s} {'size':>8s} {'entry':>7s} {'now':>7s} {'cost':>9s} {'mtm':>9s} {'pnl':>9s} {'pnl%':>7s}")
        for r in sorted(lp_rows, key=lambda x: x["unrealized_pnl_usd"]):
            print(f"  {(r['slug'] or '')[:40]:<40s} {r['size']:>8.2f} "
                  f"{r['entry_avg_price']:>7.4f} {r['current_price']:>7.4f} "
                  f"${r['cost_basis_usd']:>8.2f} ${r['mtm_value_usd']:>8.2f} "
                  f"${r['unrealized_pnl_usd']:>+8.2f} {100*r['unrealized_pnl_pct']:>+6.1f}%")
        unreal = lp_total_mtm - lp_total_cost
        pct = (unreal / lp_total_cost * 100) if lp_total_cost > 0 else 0
        print(f"\n  LP total cost:    ${lp_total_cost:>8.2f}")
        print(f"  LP total MtM:     ${lp_total_mtm:>8.2f}")
        print(f"  LP unrealized:    ${unreal:>+8.2f}  ({pct:+.1f}%)")

    # --- co-living quant positions
    quant_rows = [r for r in rows if r["is_lp_position"] == "0"]
    if quant_rows:
        print(f"\n--- co-living quant positions (not LP) ---")
        print(f"  {len(quant_rows)} positions, total cost ${quant_total_cost:.2f}, "
              f"MtM ${quant_total_mtm:.2f}, "
              f"unrealized ${quant_total_mtm-quant_total_cost:+.2f}")

    # --- activity feed: look for rewards
    print(f"\n--- recent activity (last 24h, types other than TRADE) ---")
    cutoff = time.time() - 86400
    non_trade = [a for a in activity if a.get("type") != "TRADE"
                 and _f(a.get("timestamp")) >= cutoff]
    if not non_trade:
        print("  (no non-trade activity in last 24h — REWARDs typically arrive at UTC 00:00 daily)")
    else:
        for a in non_trade[:10]:
            t = a.get("type", "?")
            ts = _f(a.get("timestamp"))
            ts_iso = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
            usd = _f(a.get("usdcSize"))
            slug = (a.get("slug") or "")[:40]
            print(f"  {ts_iso}  {t:<10s}  ${usd:+.4f}  {slug}")

    # --- LP-relevant trades in last 24h (use activity feed — better coverage of recent)
    print(f"\n--- LP trades last 24h ---")
    lp_trades_recent = [a for a in activity
                        if a.get("type") == "TRADE"
                        and _f(a.get("timestamp")) >= cutoff
                        and not _slug_excluded(a.get("slug") or "")]
    if not lp_trades_recent:
        print("  (none)")
    for t in lp_trades_recent:
        ts = dt.datetime.utcfromtimestamp(_f(t.get("timestamp"))).strftime("%H:%M:%S")
        print(f"  {ts}  {(t.get('side') or '').ljust(4)}  "
              f"{_f(t.get('size')):>7.2f} @ {_f(t.get('price')):.4f}  "
              f"${_f(t.get('size'))*_f(t.get('price')):>7.4f}  "
              f"{(t.get('slug') or '')[:50]}")

    # --- write csv snapshot for trend analysis
    out = Path(cfg.data_dir) / f"tracker_{dt.datetime.utcnow().strftime('%Y-%m-%d')}.csv"
    write_csv(out, rows, POSITION_FIELDS)
    log.info("wrote %d positions → %s", len(rows), out)

    # --- key reminder
    print(f"\n--- reminders ---")
    print("  Polymarket LP rewards typically credit at UTC 00:00 daily.")
    print("  If you ran this within a few hours of opening positions,")
    print("  inventory MtM moves but reward column may not yet show.")
    print("  Re-run after 00:00 UTC for real reward delta.")
    print(f"\n  CSV snapshot: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
