#!/usr/bin/env python3
"""
06_wallet_follow_backtest.py — simulate delayed copy-trading of A-tier wallets.

Reads:
  data/wallet_scores.csv          (filter by tier — default A)
  data/wallet_trades_tail.csv     (forward-recorded trades, primary)
  data/wallet_trade_history.csv   (historical trades — used if WAR_BT_SOURCE=history|both)
  data/orderbook/*.jsonl          (own forward-recorded snapshots)
  data/markets_cache.json         (gamma metadata for resolution status; gaps backfilled live)

Writes:
  data/wallet_follow_simulated.csv   (one row per (trade × delay))
  data/wallet_follow_backtest.csv    (per-wallet × delay summary — the Phase 2 verdict)

Algorithm:
  1. Build per-token orderbook index from jsonl (compact arrays — keeps memory
     bounded for multi-week corpora).
  2. Pull trades from chosen sources, dedupe by tx hash, keep only BUY trades
     of selected-tier wallets within the orderbook time coverage.
  3. For each trade T (ts=t, token=k, wallet_price=p_w, side=BUY):
        for each delay in WAR_BT_DELAYS:
          find first snapshot of token k at ts >= t + delay,
            within WAR_BT_MAX_GAP seconds tolerance
          if no snapshot           → "no_snapshot"
          fill = best_ask
          copyable iff:
            fill - p_w <= WAR_BT_MAX_SLIP
            spread     <= WAR_BT_MAX_SPREAD
            depth_ask  >= WAR_BT_MIN_DEPTH
          if not copyable          → "not_copyable" (with reason)
          else simulate $2 buy at fill
  4. Resolve markets via markets_cache.json (fetches missing ones live).
  5. Per simulated trade: pnl = shares*(settle - fill), where shares = $2/fill,
     settle = 1 if side won else 0. For unresolved markets: skip from pnl
     (counted separately as `pending`).
  6. Aggregate per (wallet, delay): copyable_ratio, follow_pnl, win_rate,
     avg_slippage, avg_spread.

This is the "30 points" Phase 1 deliberately left on the table. The outputs
are what the original methodology calls "follow_60s_pnl / follow_300s_pnl /
follow_1800s_pnl" — the difference between "this wallet earned $X" and
"copying it $2 at a time would earn / lose $Y".

Run only after the orderbook recorder has accumulated >= 7 days of data;
earlier runs will mostly emit `no_snapshot` and aren't informative. Best
results at >= 28 days.
"""
from __future__ import annotations
import array
import bisect
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alpha_radar import config as cfg_mod                          # noqa: E402
from alpha_radar.api import ApiClient                              # noqa: E402
from alpha_radar.util import (                                     # noqa: E402
    setup_logging, read_csv, write_csv, addr_lower, parse_ts, chunked,
)


SIMULATED_FIELDS = [
    "wallet", "transaction_hash", "trade_ts", "delay_sec",
    "condition_id", "token_id", "side",
    "wallet_price", "snap_ts", "snap_gap_sec", "best_ask", "best_bid",
    "spread", "depth_ask_usd", "fill_price", "slippage",
    "result",                       # filled | no_snapshot | not_copyable
    "drop_reason",                  # which gate failed for not_copyable
    "follow_usd", "shares",
    "resolved", "won", "pnl",
]

SUMMARY_FIELDS = [
    "wallet", "delay_sec",
    "n_trades", "n_filled", "n_not_copyable", "n_no_snapshot",
    "copyable_ratio",
    "n_resolved", "n_won",
    "follow_pnl", "follow_pnl_pct_of_stake",
    "follow_win_rate",
    "avg_slippage", "avg_spread", "avg_depth_ask_usd",
]


# ---------------------------------------------------------------------------
# orderbook index
# ---------------------------------------------------------------------------
class OrderbookIndex:
    """
    Compact, per-token sorted index of snapshots.

    Memory budget: ~24 bytes per snapshot (5x float32 + 4-byte uint32). For
    100 tokens × 2880 snaps/day × 60 days that's ~415 MB — tractable on a
    server box. If pressed, drop best_bid (we only need the ask side for
    BUY-follow simulation).
    """
    __slots__ = ("by_token", "ts_min", "ts_max")

    def __init__(self):
        self.by_token: dict[str, dict] = {}
        self.ts_min: int = 0
        self.ts_max: int = 0

    def _ensure(self, token: str) -> dict:
        e = self.by_token.get(token)
        if e is None:
            e = {
                "ts":     array.array("I"),
                "ask":    array.array("f"),
                "bid":    array.array("f"),
                "spr":    array.array("f"),
                "depthA": array.array("f"),
            }
            self.by_token[token] = e
        return e

    def load_dir(self, d: Path, log) -> None:
        files = sorted(d.glob("*.jsonl"))
        if not files:
            log.warning("no jsonl files under %s", d)
            return
        n_lines = 0
        ts_min = 10**12
        ts_max = 0
        # raw rows are appended in time order within each daily file, so we
        # don't need to sort if we process files in chronological order — but
        # we sort defensively at the end.
        for fp in files:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    tok = rec.get("token_id")
                    ts  = rec.get("ts")
                    if not tok or ts is None:
                        continue
                    e = self._ensure(str(tok))
                    e["ts"].append(int(ts))
                    e["ask"].append(float(rec.get("best_ask") or 0.0))
                    e["bid"].append(float(rec.get("best_bid") or 0.0))
                    spr = rec.get("spread")
                    e["spr"].append(float(spr) if spr is not None else 0.0)
                    e["depthA"].append(float(rec.get("depth_ask_top5_usd") or 0.0))
                    n_lines += 1
                    if ts < ts_min: ts_min = ts
                    if ts > ts_max: ts_max = ts
            log.info("indexed %s (%d snapshots)", fp.name, n_lines)
        self.ts_min, self.ts_max = ts_min, ts_max

        # ensure each token's series is sorted by ts (defensive — files may
        # have multiple writers, retries, etc.)
        for tok, e in self.by_token.items():
            ts_list = e["ts"]
            if all(ts_list[i] <= ts_list[i + 1] for i in range(len(ts_list) - 1)):
                continue
            order = sorted(range(len(ts_list)), key=lambda i: ts_list[i])
            for k in ("ts", "ask", "bid", "spr", "depthA"):
                arr = e[k]
                # rebuild
                code = arr.typecode
                e[k] = array.array(code, (arr[i] for i in order))
        log.info("orderbook index built: %d tokens, %d snapshots, span %s..%s",
                 len(self.by_token), n_lines,
                 _fmt_ts(self.ts_min) if self.ts_min else "—",
                 _fmt_ts(self.ts_max) if self.ts_max else "—")

    def lookup(self, token: str, target_ts: int, max_gap_sec: int) -> dict | None:
        """Find the first snapshot at ts >= target_ts, within max_gap_sec."""
        e = self.by_token.get(token)
        if not e:
            return None
        ts_list = e["ts"]
        if not ts_list:
            return None
        # bisect_left over an array.array — array.array supports __getitem__
        # so bisect works on it directly.
        i = bisect.bisect_left(ts_list, target_ts)
        if i >= len(ts_list):
            return None
        snap_ts = int(ts_list[i])
        if snap_ts - target_ts > max_gap_sec:
            return None
        return {
            "ts": snap_ts,
            "ask": float(e["ask"][i]),
            "bid": float(e["bid"][i]),
            "spr": float(e["spr"][i]),
            "depthA": float(e["depthA"][i]),
        }


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


# ---------------------------------------------------------------------------
# trade source loader
# ---------------------------------------------------------------------------
def load_trades(cfg, scores_by_wallet: dict[str, dict],
                allowed_tiers: set[str], log) -> list[dict]:
    bcfg = cfg.backtest
    rows: list[dict] = []

    sources_to_load: list[tuple[str, Path]] = []
    if bcfg.trade_source in ("tail", "both"):
        sources_to_load.append(("tail", Path(bcfg.trades_tail_path)))
    if bcfg.trade_source in ("history", "both"):
        sources_to_load.append(("history", Path(bcfg.history_path)))

    seen_tx: set[str] = set()
    for label, p in sources_to_load:
        if not p.exists():
            log.warning("source %s missing: %s", label, p)
            continue
        n_loaded = 0
        n_skipped_tier = 0
        n_skipped_dup  = 0
        n_skipped_side = 0
        with p.open("r", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                wallet = addr_lower(r.get("wallet"))
                if wallet not in scores_by_wallet:
                    continue
                tier = (scores_by_wallet[wallet].get("tier") or "").upper()
                if "*" not in allowed_tiers and tier not in allowed_tiers:
                    n_skipped_tier += 1
                    continue
                tx = r.get("transaction_hash")
                if not tx:
                    continue
                if tx in seen_tx:
                    n_skipped_dup += 1
                    continue
                seen_tx.add(tx)
                # only follow BUYs (selling-follow is a different game)
                side = (r.get("side") or "").upper()
                if side != "BUY":
                    n_skipped_side += 1
                    continue
                try:
                    ts = int(float(r.get("ts") or 0))
                    price = float(r.get("price") or 0)
                    size = float(r.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                if ts <= 0 or price <= 0 or size <= 0:
                    continue
                rows.append({
                    "wallet": wallet,
                    "transaction_hash": tx,
                    "ts": ts,
                    "condition_id": r.get("condition_id") or "",
                    "token_id": r.get("asset_id") or "",
                    "side": side,
                    "price": price,
                    "size": size,
                    "outcome_index": r.get("outcome_index") or "",
                    # history rows already carry resolved/won; tail rows do not
                    "resolved": r.get("resolved") or "",
                    "won": r.get("won") or "",
                })
                n_loaded += 1
        log.info("loaded %d trades from %s (skipped: tier=%d, dup=%d, side!=BUY=%d)",
                 n_loaded, label, n_skipped_tier, n_skipped_dup, n_skipped_side)
    return rows


# ---------------------------------------------------------------------------
# resolution lookup
# ---------------------------------------------------------------------------
def load_market_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    out = {}
    for cid, entry in raw.items():
        m = entry.get("market") if isinstance(entry, dict) else None
        if isinstance(m, dict):
            out[cid] = m
    return out


def winning_outcome_index(market: dict | None) -> int | None:
    if not market:
        return None
    for k in ("winnerOutcomeIndex", "resolvedOutcomeIndex"):
        v = market.get(k)
        if v is not None:
            try: return int(v)
            except (TypeError, ValueError): pass
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try: op = json.loads(op)
        except Exception: op = None
    if isinstance(op, list) and op:
        try:
            floats = [float(x) for x in op]
            if max(floats) >= 0.999:
                return floats.index(max(floats))
        except (TypeError, ValueError): pass
    return None


def resolve_market(api: ApiClient, market_cache: dict[str, dict],
                   needed_cids: list[str], log) -> dict[str, tuple[bool, int | None]]:
    """
    Returns cid -> (resolved_bool, winning_outcome_index_or_None).
    Backfills cache misses by calling gamma in batches.
    """
    out: dict[str, tuple[bool, int | None]] = {}
    missing = []
    for cid in needed_cids:
        m = market_cache.get(cid)
        if m is None:
            missing.append(cid)
            continue
        win = winning_outcome_index(m)
        out[cid] = (win is not None, win)
    if missing:
        log.info("backfilling %d markets from gamma...", len(missing))
        for batch in chunked(missing, 50):
            mkts = api.markets_by_condition_ids(batch)
            for m in mkts:
                cid = m.get("conditionId") or m.get("condition_id")
                if cid:
                    market_cache[cid] = m
                    win = winning_outcome_index(m)
                    out[cid] = (win is not None, win)
        # any still missing → unresolved
        for cid in missing:
            out.setdefault(cid, (False, None))
    return out


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------
def simulate_trade(trade: dict, snap: dict | None, bcfg, follow_usd: float = 2.0) -> dict:
    """Decide copyability + compute PnL fields (resolution applied later)."""
    base = {
        "wallet": trade["wallet"],
        "transaction_hash": trade["transaction_hash"],
        "trade_ts": trade["ts"],
        "condition_id": trade["condition_id"],
        "token_id": trade["token_id"],
        "side": trade["side"],
        "wallet_price": round(trade["price"], 4),
        "snap_ts": "",
        "snap_gap_sec": "",
        "best_ask": "",
        "best_bid": "",
        "spread": "",
        "depth_ask_usd": "",
        "fill_price": "",
        "slippage": "",
        "result": "",
        "drop_reason": "",
        "follow_usd": follow_usd,
        "shares": "",
        "resolved": "",
        "won": "",
        "pnl": "",
    }
    if snap is None:
        base["result"] = "no_snapshot"
        return base
    fill = snap["ask"]
    spr = snap["spr"]
    depth = snap["depthA"]
    slippage = round(fill - trade["price"], 4)
    base.update({
        "snap_ts": snap["ts"],
        "snap_gap_sec": snap["ts"] - (trade["ts"] + 0),  # filled later by caller
        "best_ask": round(fill, 4),
        "best_bid": round(snap["bid"], 4),
        "spread": round(spr, 4),
        "depth_ask_usd": round(depth, 2),
        "fill_price": round(fill, 4),
        "slippage": slippage,
    })
    # gate
    drop = []
    if fill <= 0:
        drop.append("zero_ask")
    if slippage > bcfg.max_slip_cents:
        drop.append(f"slip>{bcfg.max_slip_cents:.2f}")
    if spr > bcfg.max_spread:
        drop.append(f"spread>{bcfg.max_spread:.2f}")
    if depth < bcfg.min_depth_usd:
        drop.append(f"depth<{bcfg.min_depth_usd:.0f}")
    if drop:
        base["result"] = "not_copyable"
        base["drop_reason"] = ",".join(drop)
        return base
    # filled
    shares = follow_usd / fill if fill > 0 else 0.0
    base["result"] = "filled"
    base["shares"] = round(shares, 4)
    return base


def apply_resolution(sim: dict, resolved: bool, win_idx: int | None, asset_idx: int | None,
                     follow_usd: float) -> None:
    """Mutate sim in place to add resolved/won/pnl."""
    if sim["result"] != "filled":
        return
    if not resolved or win_idx is None or asset_idx is None:
        sim["resolved"] = "0"
        sim["pnl"] = ""
        return
    won = (asset_idx == win_idx)
    sim["resolved"] = "1"
    sim["won"] = "1" if won else "0"
    fill = float(sim["fill_price"])
    shares = float(sim["shares"])
    if won:
        sim["pnl"] = round(shares * (1.0 - fill), 4)
    else:
        sim["pnl"] = round(-follow_usd, 4)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "war.backtest")
    bcfg = cfg.backtest
    api = ApiClient(cfg.api)

    # --- scores → tier filter
    scores_path = Path(cfg.data_dir) / "wallet_scores.csv"
    scores_rows = read_csv(scores_path)
    if not scores_rows:
        log.error("no scores at %s — run 03_score_wallets.py first", scores_path)
        return 2
    scores_by_wallet = {addr_lower(r.get("wallet")): r for r in scores_rows}
    allowed_tiers = set(t.upper() for t in bcfg.tiers)
    log.info("filtering to tiers=%s (%d wallets in scores)",
             sorted(allowed_tiers), len(scores_by_wallet))

    # --- orderbook index
    obi = OrderbookIndex()
    obi.load_dir(Path(bcfg.snapshot_dir), log)
    if not obi.by_token:
        log.error("no orderbook snapshots — has the recorder been running?")
        return 2

    # --- load trades
    trades = load_trades(cfg, scores_by_wallet, allowed_tiers, log)
    log.info("loaded %d candidate trades total", len(trades))
    # restrict to trades within obi coverage (with margin = max delay)
    max_delay = max(bcfg.delays_sec) if bcfg.delays_sec else 0
    in_window = [t for t in trades
                 if obi.ts_min - 60 <= t["ts"] <= obi.ts_max - max_delay + 60]
    log.info("%d trades fall within orderbook coverage minus max-delay margin",
             len(in_window))
    if not in_window:
        log.warning("no trades within orderbook window — recorder needs more time")
        return 0

    # --- simulate
    market_cache = load_market_cache(Path(cfg.data_dir) / "markets_cache.json")
    needed_cids = sorted({t["condition_id"] for t in in_window if t["condition_id"]})
    resolution = resolve_market(api, market_cache, needed_cids, log)
    # save augmented cache
    try:
        out_cache = {cid: {"_cached_at": time.time(), "market": market_cache[cid]}
                     for cid in market_cache if isinstance(market_cache[cid], dict)}
        (Path(cfg.data_dir) / "markets_cache.json").write_text(json.dumps(out_cache))
    except Exception as e:                                        # noqa: BLE001
        log.warning("failed to save markets_cache.json: %s", e)

    sim_rows: list[dict] = []
    for t in in_window:
        for delay in bcfg.delays_sec:
            target = t["ts"] + delay
            snap = obi.lookup(t["token_id"], target, bcfg.snap_max_gap_sec)
            sim = simulate_trade(t, snap, bcfg, follow_usd=2.0)
            sim["delay_sec"] = delay
            if snap is not None:
                sim["snap_gap_sec"] = snap["ts"] - target  # signed: > 0 = late
            # apply resolution
            try:
                asset_idx = int(t["outcome_index"]) if t["outcome_index"] != "" else None
            except (TypeError, ValueError):
                asset_idx = None
            resolved, win_idx = resolution.get(t["condition_id"], (False, None))
            apply_resolution(sim, resolved, win_idx, asset_idx, follow_usd=2.0)
            sim_rows.append(sim)

    out_per = Path(bcfg.output_per_trade)
    write_csv(out_per, sim_rows, SIMULATED_FIELDS)
    log.info("wrote %d simulated trade rows → %s", len(sim_rows), out_per)

    # --- aggregate per (wallet, delay)
    agg: dict[tuple[str, int], dict] = defaultdict(lambda: {
        "n_trades": 0, "n_filled": 0, "n_not_copyable": 0, "n_no_snapshot": 0,
        "n_resolved": 0, "n_won": 0, "follow_pnl": 0.0,
        "slip_sum": 0.0, "spread_sum": 0.0, "depth_sum": 0.0, "filled_for_avg": 0,
    })
    for s in sim_rows:
        key = (s["wallet"], s["delay_sec"])
        a = agg[key]
        a["n_trades"] += 1
        if s["result"] == "filled":
            a["n_filled"] += 1
            try:
                a["slip_sum"] += float(s["slippage"])
                a["spread_sum"] += float(s["spread"])
                a["depth_sum"] += float(s["depth_ask_usd"])
                a["filled_for_avg"] += 1
            except (TypeError, ValueError):
                pass
            if s["resolved"] == "1":
                a["n_resolved"] += 1
                if s["won"] == "1":
                    a["n_won"] += 1
                try:
                    a["follow_pnl"] += float(s["pnl"])
                except (TypeError, ValueError):
                    pass
        elif s["result"] == "not_copyable":
            a["n_not_copyable"] += 1
        elif s["result"] == "no_snapshot":
            a["n_no_snapshot"] += 1

    summary_rows = []
    for (wallet, delay), a in sorted(agg.items()):
        n = a["n_trades"]
        filled = a["n_filled"]
        avg_n = max(1, a["filled_for_avg"])
        stake = 2.0 * filled
        summary_rows.append({
            "wallet": wallet,
            "delay_sec": delay,
            "n_trades": n,
            "n_filled": filled,
            "n_not_copyable": a["n_not_copyable"],
            "n_no_snapshot": a["n_no_snapshot"],
            "copyable_ratio": round(filled / n, 4) if n else 0.0,
            "n_resolved": a["n_resolved"],
            "n_won": a["n_won"],
            "follow_pnl": round(a["follow_pnl"], 4),
            "follow_pnl_pct_of_stake": round(a["follow_pnl"] / stake, 4) if stake else 0.0,
            "follow_win_rate": round(a["n_won"] / a["n_resolved"], 4) if a["n_resolved"] else 0.0,
            "avg_slippage": round(a["slip_sum"] / avg_n, 4),
            "avg_spread": round(a["spread_sum"] / avg_n, 4),
            "avg_depth_ask_usd": round(a["depth_sum"] / avg_n, 2),
        })

    out_sum = Path(bcfg.output_summary)
    write_csv(out_sum, summary_rows, SUMMARY_FIELDS)
    log.info("wrote %d summary rows → %s", len(summary_rows), out_sum)

    # --- short readout
    log.info("=== Phase 2 summary by delay ===")
    for delay in sorted(bcfg.delays_sec):
        rows = [s for s in summary_rows if s["delay_sec"] == delay]
        if not rows:
            continue
        n_trades = sum(s["n_trades"] for s in rows)
        n_filled = sum(s["n_filled"] for s in rows)
        n_resolved = sum(s["n_resolved"] for s in rows)
        n_won = sum(s["n_won"] for s in rows)
        pnl = sum(s["follow_pnl"] for s in rows)
        log.info("  T+%ds: trades=%d filled=%d (%.1f%%) resolved=%d won=%d (%.1f%%) follow_pnl=$%.2f",
                 delay, n_trades, n_filled, 100*n_filled/max(1,n_trades),
                 n_resolved, n_won, 100*n_won/max(1,n_resolved), pnl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
