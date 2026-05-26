#!/usr/bin/env python3
"""
00_health_check.py — diagnostic for the overround_arb scanner.

Reports on:
  * data/overround_opportunities.csv  — last scan time, breakdown by class
  * data/scan_history.csv             — time series, distinct events tracked
  * gamma API connectivity            — one canary /events call

Exit code 0 = healthy
Exit code 2 = anomaly (still prints the report)
"""
from __future__ import annotations
import csv
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from overround_arb import config as cfg_mod                       # noqa: E402
from overround_arb.api import ApiClient                           # noqa: E402


def _f(x, default=0.0):
    try: return float(x)
    except (TypeError, ValueError): return default


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _human_size(b: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}TB"


def _age(mtime: float) -> str:
    delta = time.time() - mtime
    if delta < 60:    return f"{delta:.0f}s ago"
    if delta < 3600:  return f"{delta/60:.0f}min ago"
    if delta < 86400: return f"{delta/3600:.1f}h ago"
    return f"{delta/86400:.1f}d ago"


def main() -> int:
    cfg = cfg_mod.load()
    data = Path(cfg.data_dir)
    print(f"=== Overround Arb health check  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}) ===")
    print(f"data dir: {data.resolve()}\n")

    issues: list[str] = []

    # ---- snapshot file
    snap = data / "overround_opportunities.csv"
    if snap.exists():
        rows = _load(snap)
        st = snap.stat()
        print(f"[01] overround_opportunities.csv: {len(rows)} rows ({_human_size(st.st_size)})")
        print(f"     last scan:    {_age(st.st_mtime)}")
        if rows:
            cls = Counter(r.get("classification", "?") for r in rows)
            print(f"     by class:     "
                  f"arb={cls.get('arb',0)}  near_arb={cls.get('near_arb',0)}  "
                  f"premium_harvest={cls.get('premium_harvest',0)}  "
                  f"overpriced={cls.get('overpriced',0)}  "
                  f"anomalous={cls.get('anomalous',0)}")
            # show top 3 actionable
            for cl in ("arb", "near_arb", "premium_harvest"):
                items = [r for r in rows if r.get("classification") == cl]
                if not items:
                    continue
                print(f"     top 3 {cl}:")
                for r in items[:3]:
                    print(f"       total_ask={r['total_ask']}  edge={r['edge_usd_per_dollar']}  "
                          f"vol=${_f(r['volume']):,.0f}  {r['title'][:60]}")
        if time.time() - st.st_mtime > 6 * 3600:
            issues.append("snapshot is older than 6 hours — rerun scripts/01_scan_events.py")
    else:
        print("[01] overround_opportunities.csv: MISSING")
        issues.append("no snapshot — run scripts/01_scan_events.py first")

    # ---- history file (optional)
    hist = data / "scan_history.csv"
    if hist.exists():
        rows = _load(hist)
        st = hist.stat()
        print(f"\n[02] scan_history.csv: {len(rows)} rows ({_human_size(st.st_size)})")
        if rows:
            scans = sorted({r.get("scan_ts", "") for r in rows})
            evts = {r.get("event_slug") for r in rows}
            print(f"     {len(scans)} distinct scans, {len(evts)} distinct events tracked")
            print(f"     first scan:   {scans[0] if scans else '?'}")
            print(f"     last scan:    {scans[-1] if scans else '?'}")
            # quick trend: arb count per scan, last 10
            by_scan: dict[str, Counter] = {}
            for r in rows:
                by_scan.setdefault(r.get("scan_ts", ""), Counter())[r.get("classification", "?")] += 1
            recent = sorted(by_scan.keys())[-10:]
            print(f"     last 10 scans (arb / near_arb / premium):")
            for ts in recent:
                c = by_scan[ts]
                print(f"       {ts}  arb={c.get('arb',0):3d} near={c.get('near_arb',0):3d} "
                      f"prem={c.get('premium_harvest',0):3d}")
    else:
        print("\n[02] scan_history.csv: not present (set OA_APPEND_HISTORY=1 to track over time)")

    # ---- API canary
    print(f"\n[api] gamma canary:")
    api = ApiClient(cfg.api)
    try:
        t0 = time.time()
        ev = api.list_events_page(closed=False, limit=1)
        elapsed = time.time() - t0
        if ev:
            print(f"     OK  ({elapsed*1000:.0f}ms)  example: {ev[0].get('slug', '?')}")
        else:
            print(f"     EMPTY response — gamma /events might be down")
            issues.append("gamma /events returned empty — endpoint or network problem")
    except Exception as e:                                        # noqa: BLE001
        print(f"     FAILED: {e}")
        issues.append(f"gamma canary failed: {e}")

    # ---- summary
    print("\n=== summary ===")
    if not issues:
        print("HEALTHY — no anomalies detected.")
        return 0
    print(f"{len(issues)} issue(s):")
    for i, m in enumerate(issues, 1):
        print(f"  {i}. {m}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
