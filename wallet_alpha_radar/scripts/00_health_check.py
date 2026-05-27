#!/usr/bin/env python3
"""
00_health_check.py — one-shot diagnostic for the whole pipeline state.

Run any time. Reports on:
  * data/candidate_wallets.csv  — count, sources
  * data/wallet_trade_history.csv — count, %resolved, win rate, time span
  * data/markets_cache.json — size, hit rate vs history
  * data/wallet_scores.csv — A/B/C breakdown, drop_reasons distribution
  * data/orderbook_watchlist.txt — token count
  * data/orderbook/*.jsonl — file sizes, last snapshot age

This is what you'd run before believing any downstream output. The previous
"every wallet has settled_trades=0" bug would have been spotted in seconds
by the %resolved line.

Exit code 0 = healthy
Exit code 2 = something looks wrong (still prints the report)
"""
from __future__ import annotations
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


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


def main() -> int:
    data = Path(os.getenv("WAR_DATA_DIR", "data"))
    print(f"=== Wallet Alpha Radar health check  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}) ===")
    print(f"data dir: {data.resolve()}\n")

    issues: list[str] = []

    # ---- candidate_wallets.csv
    cands = _load(data / "candidate_wallets.csv")
    print(f"[01] candidate_wallets.csv: {len(cands)} rows")
    if cands:
        sources = Counter(c.get("source", "?") for c in cands)
        windows = Counter(c.get("window", "?") for c in cands)
        unique = len({c.get("wallet") for c in cands})
        print(f"     unique wallets: {unique}")
        print(f"     sources:        {dict(sources)}")
        print(f"     windows:        {dict(windows)}")
    else:
        issues.append("candidate_wallets.csv missing or empty — run 01_discover_candidates.py")

    # ---- wallet_trade_history.csv
    hist = _load(data / "wallet_trade_history.csv")
    print(f"\n[02] wallet_trade_history.csv: {len(hist)} rows")
    if hist:
        wallets = {h.get("wallet") for h in hist}
        resolved = sum(1 for h in hist if h.get("resolved") == "1")
        won = sum(1 for h in hist if h.get("won") == "1")
        lost = sum(1 for h in hist if h.get("won") == "0")
        ts_min = min((_f(h.get("ts")) for h in hist if _f(h.get("ts")) > 0), default=0)
        ts_max = max((_f(h.get("ts")) for h in hist if _f(h.get("ts")) > 0), default=0)
        pct_resolved = 100 * resolved / len(hist)
        print(f"     unique wallets:  {len(wallets)}")
        print(f"     resolved:        {resolved} ({pct_resolved:.1f}%)")
        print(f"       won/lost:      {won}/{lost}")
        if ts_min and ts_max:
            print(f"     time span:       {time.strftime('%Y-%m-%d', time.gmtime(ts_min))} "
                  f"→ {time.strftime('%Y-%m-%d', time.gmtime(ts_max))}")
        if pct_resolved < 30:
            issues.append(f"history resolved ratio is only {pct_resolved:.1f}% — gamma "
                          f"closed-market filter may be biting again. Investigate before scoring.")
    else:
        issues.append("wallet_trade_history.csv missing or empty — run 02_build_history.py")

    # ---- markets_cache.json
    cache_path = data / "markets_cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        size = cache_path.stat().st_size
        # how many history cids are in cache?
        if hist:
            hist_cids = {h.get("condition_id") for h in hist if h.get("condition_id")}
            cached_cids = set(cache.keys())
            covered = len(hist_cids & cached_cids)
            uncov = len(hist_cids - cached_cids)
            print(f"\n[02] markets_cache.json: {len(cache)} entries, {_human_size(size)}")
            print(f"     coverage of history cids: {covered}/{len(hist_cids)} "
                  f"({100*covered/max(1,len(hist_cids)):.1f}%)  uncovered={uncov}")
            if hist_cids and uncov > 0.2 * len(hist_cids):
                issues.append(f"cache misses {uncov}/{len(hist_cids)} history cids — "
                              "rerun 02_build_history.py with full fetch")
        else:
            print(f"\n[02] markets_cache.json: {len(cache)} entries, {_human_size(size)}")
    else:
        print("\n[02] markets_cache.json: MISSING")

    # ---- wallet_scores.csv
    scores = _load(data / "wallet_scores.csv")
    print(f"\n[03] wallet_scores.csv: {len(scores)} rows")
    if scores:
        tiers = Counter(s.get("tier", "?") for s in scores)
        print(f"     tier breakdown:  A={tiers.get('A',0)}  B={tiers.get('B',0)}  C={tiers.get('C',0)}")
        c_drops = Counter()
        for s in scores:
            if s.get("tier") == "C":
                for r in (s.get("drop_reasons") or "").split(","):
                    if r:
                        c_drops[r] += 1
        if c_drops:
            print(f"     C-tier drop reasons (top 6):")
            for r, n in c_drops.most_common(6):
                print(f"       {r:25s}: {n}")
        # show top A by score
        a_sorted = sorted([s for s in scores if s.get("tier") == "A"],
                          key=lambda s: -_f(s.get("score")))
        if a_sorted:
            print(f"     A-tier top 5:")
            for s in a_sorted[:5]:
                print(f"       {s['wallet']}  score={s['score']}  pnl=${_f(s['total_pnl']):,.0f}  "
                      f"settled={s['settled_trades']}  cluster={s.get('top_category','-')}")
    else:
        issues.append("wallet_scores.csv missing — run 03_score_wallets.py")

    # ---- orderbook_watchlist.txt
    wl = data / "orderbook_watchlist.txt"
    if wl.exists():
        lines = [l.strip() for l in wl.read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        mtime = wl.stat().st_mtime
        age_min = (time.time() - mtime) / 60
        print(f"\n[wl] orderbook_watchlist.txt: {len(lines)} token_ids "
              f"(mtime {age_min:.0f} min ago)")
    else:
        print(f"\n[wl] orderbook_watchlist.txt: MISSING (run scripts/seed_watchlist.py)")

    # ---- orderbook recorder output
    ob_dir = data / "orderbook"
    if ob_dir.exists() and ob_dir.is_dir():
        files = sorted(ob_dir.glob("*.jsonl"))
        if files:
            total_bytes = sum(f.stat().st_size for f in files)
            print(f"\n[04] orderbook/: {len(files)} jsonl files, {_human_size(total_bytes)} total")
            # most recent
            newest = files[-1]
            n_lines = sum(1 for _ in newest.open("r", encoding="utf-8"))
            mtime = newest.stat().st_mtime
            age_sec = time.time() - mtime
            print(f"     newest:        {newest.name}  {n_lines} lines  "
                  f"({_human_size(newest.stat().st_size)})  last-write {age_sec:.0f}s ago")
            if age_sec > 5 * 60:
                issues.append(f"orderbook last write was {age_sec/60:.1f} min ago — "
                              "recorder may be stuck. Check `systemctl status wallet_alpha_radar`.")
        else:
            print(f"\n[04] orderbook/: empty (recorder not yet writing)")
    else:
        print(f"\n[04] orderbook/: MISSING")

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
