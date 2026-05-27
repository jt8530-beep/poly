#!/usr/bin/env python3
"""
08_paper_report.py — offline stats over the paper trader's logs.

Reads:
  data/paper_positions.csv       open + closed
  data/paper_trades_closed.csv   realized PnL log
  data/paper_position_mtm.jsonl  per-cycle MtM (used for open-position MtM)

Prints:
  * realized PnL by classification + by exit_reason
  * win rate, avg edge captured vs scanner-printed edge
  * open positions current MtM (from latest jsonl snapshot per position)
  * top winners/losers

Run any time. No side effects.
"""
from __future__ import annotations
import csv
import json
import os
import sys
import time
from collections import defaultdict, Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from overround_arb import config as cfg_mod                       # noqa: E402
from overround_arb.util import read_csv, parse_iso_to_unix        # noqa: E402


def _f(x, default=0.0):
    try: return float(x)
    except (TypeError, ValueError): return default


def _human_age(iso_or_unix):
    ts = parse_iso_to_unix(iso_or_unix) if isinstance(iso_or_unix, str) else iso_or_unix
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 60:    return f"{delta:.0f}s"
    if delta < 3600:  return f"{delta/60:.0f}min"
    if delta < 86400: return f"{delta/3600:.1f}h"
    return f"{delta/86400:.1f}d"


def latest_mtm_snapshots(path: Path) -> dict[str, dict]:
    """Return latest snapshot per position_id (streams the jsonl)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            pid = rec.get("position_id")
            if not pid:
                continue
            existing = out.get(pid)
            if existing is None or rec.get("ts", 0) > existing.get("ts", 0):
                out[pid] = rec
    return out


def main() -> int:
    cfg = cfg_mod.load()
    pcfg = cfg.paper

    pos_rows = read_csv(Path(pcfg.positions_path))
    closed_rows = read_csv(Path(pcfg.closed_path))
    snaps = latest_mtm_snapshots(Path(pcfg.snapshots_path))

    print(f"=== Overround Arb paper-trader report  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}) ===")

    # ---- counts
    open_pos = [p for p in pos_rows if p.get("status") == "open"]
    closed_pos = [p for p in pos_rows if p.get("status") == "closed"]
    print(f"\nPositions:  {len(pos_rows)} total  ({len(open_pos)} open, {len(closed_pos)} closed)")
    if not pos_rows:
        print("\nNo positions yet — paper trader hasn't opened anything. Make sure")
        print("01_scan_events.py has been run and 07_paper_trade.py is running.")
        return 0

    # ---- realized PnL summary
    if closed_rows:
        total_cost = sum(_f(r.get("cost")) for r in closed_rows)
        total_pnl  = sum(_f(r.get("realized_pnl")) for r in closed_rows)
        wins = sum(1 for r in closed_rows if _f(r.get("realized_pnl")) > 0)
        losses = sum(1 for r in closed_rows if _f(r.get("realized_pnl")) < 0)
        flats = len(closed_rows) - wins - losses
        roi = (total_pnl / total_cost) if total_cost else 0.0
        print(f"\nClosed:  trades={len(closed_rows)}  win={wins}  loss={losses}  flat={flats}")
        print(f"         realized_pnl=${total_pnl:+,.2f}  on cost=${total_cost:,.2f}  ROI={100*roi:+.2f}%")

        # ---- by classification
        by_class = defaultdict(lambda: {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0})
        for r in closed_rows:
            c = r.get("classification", "?")
            by_class[c]["n"] += 1
            by_class[c]["cost"] += _f(r.get("cost"))
            by_class[c]["pnl"]  += _f(r.get("realized_pnl"))
            if _f(r.get("realized_pnl")) > 0:
                by_class[c]["win"] += 1
        print(f"\nBy classification:")
        print(f"  {'class':<20s} {'n':>4s} {'win%':>6s} {'cost':>10s} {'pnl':>12s} {'roi':>8s}")
        for c, d in sorted(by_class.items()):
            wr = 100 * d["win"] / d["n"] if d["n"] else 0
            roi = 100 * d["pnl"] / d["cost"] if d["cost"] else 0
            print(f"  {c:<20s} {d['n']:>4d} {wr:>5.1f}% ${d['cost']:>9,.0f} ${d['pnl']:>+11,.2f} {roi:>+7.2f}%")

        # ---- by exit_reason
        by_exit = defaultdict(lambda: {"n": 0, "cost": 0.0, "pnl": 0.0})
        for r in closed_rows:
            e = r.get("exit_reason", "?")
            by_exit[e]["n"] += 1
            by_exit[e]["cost"] += _f(r.get("cost"))
            by_exit[e]["pnl"]  += _f(r.get("realized_pnl"))
        print(f"\nBy exit_reason:")
        print(f"  {'reason':<20s} {'n':>4s} {'cost':>10s} {'pnl':>12s} {'roi':>8s}")
        for e, d in sorted(by_exit.items(), key=lambda kv: -kv[1]["pnl"]):
            roi = 100 * d["pnl"] / d["cost"] if d["cost"] else 0
            print(f"  {e:<20s} {d['n']:>4d} ${d['cost']:>9,.0f} ${d['pnl']:>+11,.2f} {roi:>+7.2f}%")

        # ---- top winners / losers
        sorted_by_pnl = sorted(closed_rows, key=lambda r: _f(r.get("realized_pnl")), reverse=True)
        print(f"\nTop 3 winners:")
        for r in sorted_by_pnl[:3]:
            print(f"  ${_f(r['realized_pnl']):>+8,.2f} ({100*_f(r['realized_pnl_pct']):+.1f}%) "
                  f"reason={r['exit_reason']:<14s} hold={_f(r['hold_days']):4.1f}d  "
                  f"{(r.get('title') or '')[:50]}")
        print(f"\nTop 3 losers:")
        for r in sorted_by_pnl[-3:][::-1]:
            print(f"  ${_f(r['realized_pnl']):>+8,.2f} ({100*_f(r['realized_pnl_pct']):+.1f}%) "
                  f"reason={r['exit_reason']:<14s} hold={_f(r['hold_days']):4.1f}d  "
                  f"{(r.get('title') or '')[:50]}")

        # ---- edge capture: did we realize the printed scanner edge?
        # Edge expected at entry = 1 - entry_total_ask. Realized edge = realized_pnl_pct.
        # For settled positions only.
        settled = [r for r in closed_rows if r.get("exit_reason") == "settled"]
        if settled:
            avg_printed_edge = sum(1 - _f(r.get("entry_total_ask")) for r in settled) / len(settled)
            avg_realized_edge = sum(_f(r.get("realized_pnl_pct")) for r in settled) / len(settled)
            print(f"\nEdge capture (settled trades only):")
            print(f"  printed edge per $ at entry:  {100*avg_printed_edge:+.2f}%")
            print(f"  realized return per $ stake:  {100*avg_realized_edge:+.2f}%")
            print(f"  difference:                   {100*(avg_realized_edge - avg_printed_edge):+.2f} pp")
    else:
        print("\nNo closed trades yet.")

    # ---- open positions MtM
    if open_pos:
        print(f"\nOpen positions ({len(open_pos)}):")
        print(f"  {'opened':>6s} {'class':<14s} {'entry_ask':>10s} {'now_bid':>9s} "
              f"{'mtm_pnl':>10s} {'mtm_%':>7s}  title")
        rows_with_age = []
        for p in open_pos:
            opened = parse_iso_to_unix(p.get("opened_at"))
            age_days = (time.time() - opened) / 86400.0 if opened else 0.0
            snap = snaps.get(p.get("position_id"))
            mtm_pnl = snap.get("mtm_pnl") if snap else None
            mtm_pct = snap.get("mtm_pnl_pct") if snap else None
            cur_bid = snap.get("current_total_bid") if snap else None
            rows_with_age.append((age_days, p, snap, mtm_pnl, mtm_pct, cur_bid))
        rows_with_age.sort(key=lambda x: -(x[3] or -1e9) if x[3] is not None else 1)
        for age, p, snap, mtm_pnl, mtm_pct, cur_bid in rows_with_age:
            mtm_pnl_s = f"${mtm_pnl:+,.2f}" if mtm_pnl is not None else "—"
            mtm_pct_s = f"{100*mtm_pct:+.2f}%" if mtm_pct is not None else "—"
            cur_bid_s = f"{cur_bid:.4f}" if cur_bid is not None else "—"
            print(f"  {age:>5.1f}d {p.get('classification',''):<14s} "
                  f"{_f(p.get('entry_total_ask')):>10.4f} {cur_bid_s:>9s} "
                  f"{mtm_pnl_s:>10s} {mtm_pct_s:>7s}  {(p.get('title') or '')[:45]}")
        # aggregate open MtM
        total_open_cost = sum(_f(p.get("cost")) for p in open_pos)
        total_open_mtm  = sum((snaps.get(p.get("position_id")) or {}).get("mtm_bid", _f(p.get("cost")))
                              for p in open_pos)
        unrealized = total_open_mtm - total_open_cost
        print(f"\n  open cost:        ${total_open_cost:,.2f}")
        print(f"  open MtM (bid):   ${total_open_mtm:,.2f}")
        print(f"  unrealized PnL:   ${unrealized:+,.2f}  ({100*unrealized/total_open_cost:+.2f}%)"
              if total_open_cost else "  unrealized PnL:   —")

    return 0


if __name__ == "__main__":
    sys.exit(main())
