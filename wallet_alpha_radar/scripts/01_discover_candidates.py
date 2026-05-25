#!/usr/bin/env python3
"""
01_discover_candidates.py — find candidate wallets to investigate.

Sources:
  1. Public leaderboard, multiple windows (Day/Week/Month/All), skip top N.
     Top wallets are crowded — alpha decays. We start at rank `WAR_LB_SKIP_TOP`
     (default 20) and pull `WAR_LB_TAKE` rows after that.
  2. Recently-resolved markets: scan winners who entered in the 0.30–0.65
     band (i.e. real prediction, not late-arrival 0.95+ buying). This pass
     is light because trade-level winner attribution requires per-market
     digging — Phase 1 only collects condition_ids and lets the user
     opt-in via WAR_DISCOVER_WINNERS=1.

Output: data/candidate_wallets.csv

Re-running is idempotent: the same wallet may appear multiple times (one row
per source/window). The history builder dedupes by address.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

# allow running directly: `python scripts/01_discover_candidates.py`
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alpha_radar import config as cfg_mod          # noqa: E402
from alpha_radar.api import ApiClient              # noqa: E402
from alpha_radar.util import (                     # noqa: E402
    setup_logging, append_csv, utcnow_iso, addr_lower,
)


CANDIDATE_FIELDS = [
    "wallet", "source", "window", "metric", "rank", "amount",
    "name", "pseudonym", "discovered_at",
]


def _row_from_lb_entry(entry: dict, *, source: str, window: str, metric: str,
                       absolute_rank: int) -> dict | None:
    # Different leaderboard payload shapes have shipped over time. Be liberal.
    addr = (
        entry.get("address")
        or entry.get("proxyWallet")
        or entry.get("user")
        or entry.get("wallet")
    )
    if not addr:
        return None
    amount = (
        entry.get("amount")
        or entry.get("profit")
        or entry.get("value")
        or entry.get("volume")
    )
    return {
        "wallet": addr_lower(addr),
        "source": source,
        "window": window,
        "metric": metric,
        "rank": absolute_rank,
        "amount": amount,
        "name": entry.get("name") or "",
        "pseudonym": entry.get("pseudonym") or "",
        "discovered_at": utcnow_iso(),
    }


def discover_from_leaderboard(api: ApiClient, dcfg, log) -> list[dict]:
    rows: list[dict] = []
    metric = dcfg.leaderboard_metric
    skip = dcfg.leaderboard_skip_top
    take = dcfg.leaderboard_take

    for window in dcfg.leaderboard_windows:
        log.info("leaderboard window=%s metric=%s skip=%d take=%d",
                 window, metric, skip, take)
        # pull `skip + take` rows so we know absolute rank
        page = api.leaderboard(window=window, metric=metric, limit=skip + take, offset=0)
        if not page:
            log.warning("empty leaderboard for window=%s — endpoint may have changed", window)
            continue
        for i, entry in enumerate(page):
            absolute_rank = i + 1
            if absolute_rank <= skip:
                continue
            r = _row_from_lb_entry(entry, source="leaderboard",
                                   window=window, metric=metric,
                                   absolute_rank=absolute_rank)
            if r:
                rows.append(r)
    return rows


def discover_from_resolved_markets(api: ApiClient, dcfg, log) -> list[dict]:
    """
    Phase-1 lightweight version: enumerate recently-closed markets above a
    volume floor and emit them as a TODO file. Per-market winner attribution
    needs trades-by-market, which is its own scan and we'll add in Phase 2.
    For now we just record the markets so the user can see what would be
    scanned.
    """
    log.info("scanning recently-closed markets (lookback=%dd, min_vol=$%.0f)",
             dcfg.winner_lookback_days, dcfg.winner_min_volume)
    markets = api.list_recently_closed_markets(limit=500, min_volume=dcfg.winner_min_volume)
    log.info("found %d closed markets above volume floor", len(markets))
    # write a sidecar so the user can plan Phase 2 scans
    todo_path = Path("data/closed_markets_todo.csv")
    rows = []
    for m in markets:
        rows.append({
            "condition_id": m.get("conditionId") or m.get("condition_id") or "",
            "slug":         m.get("slug") or "",
            "question":     (m.get("question") or "")[:200],
            "end_date":     m.get("endDate") or "",
            "volume":       m.get("volume") or m.get("volumeNum") or 0,
            "category":     m.get("category") or "",
        })
    if rows:
        append_csv(todo_path, rows,
                   ["condition_id", "slug", "question", "end_date", "volume", "category"])
        log.info("wrote %d rows to %s", len(rows), todo_path)
    # Phase-1 returns no wallets from this source; user runs Phase 2 to attribute.
    return []


def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "war.discover")
    api = ApiClient(cfg.api)

    rows: list[dict] = []
    rows += discover_from_leaderboard(api, cfg.discovery, log)

    if os.getenv("WAR_DISCOVER_WINNERS", "0") == "1":
        rows += discover_from_resolved_markets(api, cfg.discovery, log)

    # dedupe by (wallet, source, window) — keep first occurrence
    seen = set()
    unique: list[dict] = []
    for r in rows:
        k = (r["wallet"], r["source"], r["window"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(r)

    out = Path(cfg.data_dir) / "candidate_wallets.csv"
    n = append_csv(out, unique, CANDIDATE_FIELDS)
    log.info("wrote %d candidate rows to %s (unique wallets in this run: %d)",
             n, out, len({r["wallet"] for r in unique}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
