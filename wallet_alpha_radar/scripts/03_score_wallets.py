#!/usr/bin/env python3
"""
03_score_wallets.py — compute per-wallet metrics + score + tier.

Reads:  data/wallet_trade_history.csv
Writes: data/wallet_scores.csv

Phase 1 scoring is out of 70 (the 30-pt follow-replicability section is
Phase 2 territory because it needs forward-recorded orderbook data).

Hard filters drop a wallet outright:
  settled_trades >= cfg.min_settled_trades
  active_days   >= cfg.min_active_days
  total_volume_usd >= cfg.min_total_volume_usd
  hedge_ratio    <  cfg.max_hedge_ratio
  profit_concentration < cfg.max_profit_concentration
  entry_090_plus_ratio < cfg.max_entry_090_plus_ratio
  recent activity within last 30d

Soft components (sum out of 70):
  PnL stability         (20)
  Entry quality         (15)
  Hedge cleanliness     (10)
  Category specialization (15)
  Liquidity proxy       (10)

Tier A / B / C come from cfg.scoring.tier_a_min / tier_b_min.
"""
from __future__ import annotations
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alpha_radar import config as cfg_mod                          # noqa: E402
from alpha_radar.util import (                                     # noqa: E402
    setup_logging, read_csv, write_csv, addr_lower,
)
from alpha_radar.pnl import fifo_pnl, equity_curve, max_drawdown   # noqa: E402


SCORE_FIELDS = [
    "wallet", "tier", "score", "hard_pass",
    "settled_trades", "active_days", "total_volume_usd", "total_pnl",
    "last_30d_pnl", "max_drawdown", "max_drawdown_pct",
    "profit_concentration", "median_entry_price",
    "entry_090_plus_ratio", "entry_mid_band_ratio",
    "hedge_ratio", "top_category", "top_category_settled",
    "top_category_pnl", "top_category_roi",
    "median_trade_usd",
    "drop_reasons", "score_breakdown",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def _i(x, default=0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default

def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


# ---------------------------------------------------------------------------
# per-wallet metric computation
# ---------------------------------------------------------------------------
def compute_metrics(rows: list[dict]) -> dict:
    """rows = all trades for one wallet (already filtered)."""
    if not rows:
        return {}

    now = time.time()
    cutoff_30d = now - 30 * 86400

    # group trades by (condition_id, asset_id) for FIFO PnL
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    market_meta: dict[str, dict] = {}                # condition_id -> meta

    buy_prices_weighted: list[tuple[float, float]] = []   # (price, size)
    trade_sizes_usd: list[float] = []
    yes_no_per_event: dict[str, set[str]] = defaultdict(set)   # condition_id -> outcome_indexes touched
    active_ts: list[float] = []
    total_volume_usd = 0.0

    for r in rows:
        ts = _f(r.get("ts"))
        if ts <= 0:
            continue
        active_ts.append(ts)
        side = (r.get("side") or "").upper()
        price = _f(r.get("price"))
        size = _f(r.get("size"))
        usd = _f(r.get("usd_size"))
        if usd <= 0:
            usd = price * size
        total_volume_usd += usd
        trade_sizes_usd.append(usd)

        cid = r.get("condition_id") or ""
        aid = r.get("asset_id") or ""
        groups[(cid, aid)].append({"side": side, "price": price, "size": size, "ts": ts})

        if cid and r.get("outcome_index") not in (None, "", "None"):
            yes_no_per_event[cid].add(str(r.get("outcome_index")))

        if cid:
            market_meta.setdefault(cid, {
                "resolved": r.get("resolved") == "1",
                "winning_outcome_index": r.get("winning_outcome_index"),
                "category": r.get("category") or "",
                "event_slug": r.get("event_slug") or "",
            })

        if side == "BUY" and 0.0 < price <= 1.0 and size > 0:
            buy_prices_weighted.append((price, size))

    # --- per-group settled PnL
    # cluster := category if non-empty else event_slug else "unknown"
    # We use cluster (not raw category) for specialization scoring because gamma
    # /markets does not populate the `category` field — it lives on /events. The
    # event_slug groups markets within one event (e.g. all NBA-finals-2026 sub-
    # markets) which is a reasonable proxy for "specialty area".
    settled_pnls: list[tuple[float, float, str, str]] = []   # (ts, pnl, condition_id, cluster)
    settled_trades_count = 0
    last_ts_per_group: dict[tuple[str, str], float] = {}

    # Build a (condition_id, asset_id) -> outcome_index lookup once.
    group_to_outcome_idx: dict[tuple[str, str], int] = {}
    for r in rows:
        cid_r = r.get("condition_id") or ""
        aid_r = r.get("asset_id") or ""
        oi = r.get("outcome_index")
        if oi in (None, "", "None"):
            continue
        try:
            group_to_outcome_idx[(cid_r, aid_r)] = int(float(oi))
        except (TypeError, ValueError):
            pass

    for (cid, aid), trades in groups.items():
        meta = market_meta.get(cid) or {}
        resolved = bool(meta.get("resolved"))
        win_idx = meta.get("winning_outcome_index")
        asset_idx = group_to_outcome_idx.get((cid, aid))
        won = None
        if resolved and win_idx not in (None, "", "None") and asset_idx is not None:
            try:
                won = int(float(win_idx)) == int(asset_idx)
            except (TypeError, ValueError):
                won = None

        result = fifo_pnl(trades, resolved=resolved, won=won)
        last_ts_per_group[(cid, aid)] = max(t["ts"] for t in trades)

        # cluster: category > event_slug > "unknown"
        cluster = (meta.get("category") or "").strip()
        if not cluster or cluster.lower() == "uncategorized":
            cluster = (meta.get("event_slug") or "").strip() or "unknown"

        # only count as a "settled trade" if the market actually resolved
        if resolved:
            settled_trades_count += result["n_buys"] + result["n_sells"]
            settled_pnls.append(
                (last_ts_per_group[(cid, aid)], result["pnl"], cid, cluster)
            )

    total_pnl = sum(p for _, p, _, _ in settled_pnls)
    last_30d_pnl = sum(p for ts, p, _, _ in settled_pnls if ts >= cutoff_30d)

    # --- equity curve & drawdown
    curve = equity_curve([(ts, p) for ts, p, _, _ in settled_pnls])
    mdd = max_drawdown(curve)
    peak = max((eq for _, eq in curve), default=0.0)
    mdd_pct = _safe_div(mdd, peak) if peak > 0 else (1.0 if mdd > 0 else 0.0)

    # --- profit concentration: largest single-market PnL contribution / sum of POSITIVE PnLs
    pos_total = sum(p for _, p, _, _ in settled_pnls if p > 0)
    largest_pos = max((p for _, p, _, _ in settled_pnls if p > 0), default=0.0)
    profit_concentration = _safe_div(largest_pos, pos_total) if pos_total > 0 else 0.0

    # --- entry-price stats (size-weighted on buys only)
    total_buy_size = sum(s for _, s in buy_prices_weighted)
    weighted_median_entry = 0.0
    entry_090_plus_ratio = 0.0
    entry_mid_band_ratio = 0.0
    if total_buy_size > 0:
        # weighted median via sort
        srt = sorted(buy_prices_weighted, key=lambda x: x[0])
        cum = 0.0
        half = total_buy_size / 2.0
        for p, s in srt:
            cum += s
            if cum >= half:
                weighted_median_entry = p
                break
        entry_090_plus_ratio = _safe_div(
            sum(s for p, s in buy_prices_weighted if p >= 0.90), total_buy_size,
        )
        entry_mid_band_ratio = _safe_div(
            sum(s for p, s in buy_prices_weighted if 0.35 <= p <= 0.65), total_buy_size,
        )

    # --- hedge ratio: condition_ids where the wallet bought BOTH outcome indexes
    total_events = len(yes_no_per_event)
    hedged_events = sum(1 for s in yes_no_per_event.values() if len(s) >= 2)
    hedge_ratio = _safe_div(hedged_events, total_events)

    # --- top cluster (formerly "category" — now category-OR-event_slug fallback)
    cat_pnl: dict[str, float] = defaultdict(float)
    cat_settled: dict[str, int] = defaultdict(int)
    cat_volume: dict[str, float] = defaultdict(float)
    cat_30d_pnl: dict[str, float] = defaultdict(float)
    for ts, p, _, cluster in settled_pnls:
        c = cluster or "unknown"
        cat_pnl[c] += p
        cat_settled[c] += 1
        if ts >= cutoff_30d:
            cat_30d_pnl[c] += p
    # rough cluster volume by re-walking rows (use same fallback logic)
    for r in rows:
        if r.get("resolved") == "1":
            c = (r.get("category") or "").strip()
            if not c or c.lower() == "uncategorized":
                c = (r.get("event_slug") or "").strip() or "unknown"
            cat_volume[c] += _f(r.get("usd_size"))
    top_cat = max(cat_settled, key=lambda k: cat_settled[k]) if cat_settled else ""
    top_cat_settled = cat_settled.get(top_cat, 0)
    top_cat_pnl = cat_pnl.get(top_cat, 0.0)
    top_cat_vol = cat_volume.get(top_cat, 0.0)
    top_cat_roi = _safe_div(top_cat_pnl, top_cat_vol)
    top_cat_30d_pnl = cat_30d_pnl.get(top_cat, 0.0)

    # --- liquidity proxy
    sizes_sorted = sorted(trade_sizes_usd)
    median_trade_usd = sizes_sorted[len(sizes_sorted) // 2] if sizes_sorted else 0.0

    # --- active span
    active_days = _safe_div(max(active_ts) - min(active_ts), 86400.0) if active_ts else 0.0
    last_trade_ts = max(active_ts) if active_ts else 0.0
    active_recent = (now - last_trade_ts) <= 30 * 86400 if last_trade_ts else False

    return {
        "settled_trades": settled_trades_count,
        "active_days": active_days,
        "active_recent": active_recent,
        "total_volume_usd": total_volume_usd,
        "total_pnl": total_pnl,
        "last_30d_pnl": last_30d_pnl,
        "max_drawdown": mdd,
        "max_drawdown_pct": mdd_pct,
        "profit_concentration": profit_concentration,
        "median_entry_price": weighted_median_entry,
        "entry_090_plus_ratio": entry_090_plus_ratio,
        "entry_mid_band_ratio": entry_mid_band_ratio,
        "hedge_ratio": hedge_ratio,
        "top_category": top_cat,
        "top_category_settled": top_cat_settled,
        "top_category_pnl": top_cat_pnl,
        "top_category_roi": top_cat_roi,
        "top_category_30d_pnl": top_cat_30d_pnl,
        "median_trade_usd": median_trade_usd,
    }


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def hard_filter(m: dict, scfg) -> list[str]:
    drops = []
    if m["settled_trades"] < scfg.min_settled_trades:
        drops.append(f"settled<{scfg.min_settled_trades}")
    if m["active_days"] < scfg.min_active_days:
        drops.append(f"active_days<{scfg.min_active_days}")
    if m["total_volume_usd"] < scfg.min_total_volume_usd:
        drops.append(f"volume<{scfg.min_total_volume_usd:.0f}")
    if m["hedge_ratio"] >= scfg.max_hedge_ratio:
        drops.append(f"hedge>={scfg.max_hedge_ratio:.2f}")
    if m["profit_concentration"] >= scfg.max_profit_concentration:
        drops.append(f"conc>={scfg.max_profit_concentration:.2f}")
    if m["entry_090_plus_ratio"] >= scfg.max_entry_090_plus_ratio:
        drops.append(f"090+>={scfg.max_entry_090_plus_ratio:.2f}")
    if not m["active_recent"]:
        drops.append("inactive_30d")
    return drops


def score(m: dict, scfg) -> tuple[float, dict]:
    s = 0.0
    bd: dict[str, float] = {}

    # 1) PnL stability — 20
    pnl_s = 0.0
    if m["total_pnl"] > 0:                                  pnl_s += 8
    if m["last_30d_pnl"] > 0:                               pnl_s += 5
    if m["max_drawdown_pct"] < 0.35:                        pnl_s += 4
    if m["profit_concentration"] < 0.35:                    pnl_s += 3
    s += pnl_s; bd["pnl_stability"] = pnl_s

    # 2) Entry quality — 15
    eq = 0.0
    if scfg.entry_mid_lo <= m["median_entry_price"] <= scfg.entry_mid_hi:  eq += 8
    if m["entry_090_plus_ratio"] < 0.20:                                   eq += 4
    if m["entry_mid_band_ratio"] >= 0.40:                                  eq += 3
    s += eq; bd["entry_quality"] = eq

    # 3) Hedge cleanliness — 10
    h = 0.0
    if m["hedge_ratio"] < 0.10:    h = 10
    elif m["hedge_ratio"] < 0.20:  h = 6
    elif m["hedge_ratio"] < 0.35:  h = 2
    s += h; bd["hedge_clean"] = h

    # 4) Category specialization — 15
    cs = 0.0
    if m["top_category_settled"] >= 30:                                cs += 5
    if m["top_category_pnl"] > 0:                                      cs += 5
    if m.get("top_category_30d_pnl", 0) > 0:                           cs += 5
    s += cs; bd["category"] = cs

    # 5) Liquidity proxy — 10
    lp = 0.0
    if m["median_trade_usd"] >= 100:    lp += 5
    elif m["median_trade_usd"] >= 50:   lp += 3
    if m["total_volume_usd"] >= 10_000: lp += 5
    elif m["total_volume_usd"] >= 5_000: lp += 3
    s += lp; bd["liquidity"] = lp

    return s, bd


def tier_for(score_val: float, hard_pass: bool, m: dict, scfg) -> str:
    if not hard_pass:
        return "C"
    # Tier A requires BOTH lifetime AND last-30d positive PnL. The lifetime
    # gate kicks out wallets that lost large amounts long-term but happen to
    # have a recently positive month — those are usually variance, not edge.
    if score_val >= scfg.tier_a_min and m["last_30d_pnl"] > 0 and m["total_pnl"] > 0:
        return "A"
    if score_val >= scfg.tier_b_min:
        return "B"
    return "C"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "war.score")

    hist_path = Path(cfg.data_dir) / "wallet_trade_history.csv"
    if not hist_path.exists():
        log.error("missing %s — run 02_build_history.py first", hist_path)
        return 2

    rows = read_csv(hist_path)
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_wallet[addr_lower(r.get("wallet"))].append(r)
    log.info("scoring %d wallets across %d total trade rows", len(by_wallet), len(rows))

    out_rows: list[dict] = []
    for w, wrows in sorted(by_wallet.items()):
        m = compute_metrics(wrows)
        if not m:
            continue
        drops = hard_filter(m, cfg.scoring)
        hard_pass = not drops
        sc, bd = score(m, cfg.scoring)
        tier = tier_for(sc, hard_pass, m, cfg.scoring)
        out_rows.append({
            "wallet": w,
            "tier": tier,
            "score": round(sc, 2),
            "hard_pass": "1" if hard_pass else "0",
            "settled_trades": m["settled_trades"],
            "active_days": round(m["active_days"], 1),
            "total_volume_usd": round(m["total_volume_usd"], 2),
            "total_pnl": round(m["total_pnl"], 2),
            "last_30d_pnl": round(m["last_30d_pnl"], 2),
            "max_drawdown": round(m["max_drawdown"], 2),
            "max_drawdown_pct": round(m["max_drawdown_pct"], 4),
            "profit_concentration": round(m["profit_concentration"], 4),
            "median_entry_price": round(m["median_entry_price"], 4),
            "entry_090_plus_ratio": round(m["entry_090_plus_ratio"], 4),
            "entry_mid_band_ratio": round(m["entry_mid_band_ratio"], 4),
            "hedge_ratio": round(m["hedge_ratio"], 4),
            "top_category": m["top_category"],
            "top_category_settled": m["top_category_settled"],
            "top_category_pnl": round(m["top_category_pnl"], 2),
            "top_category_roi": round(m["top_category_roi"], 4),
            "median_trade_usd": round(m["median_trade_usd"], 2),
            "drop_reasons": ",".join(drops),
            "score_breakdown": ";".join(f"{k}={v:.0f}" for k, v in bd.items()),
        })

    # sort: A first, then by score desc
    out_rows.sort(key=lambda r: ({"A": 0, "B": 1, "C": 2}[r["tier"]], -r["score"]))

    out = Path(cfg.data_dir) / "wallet_scores.csv"
    write_csv(out, out_rows, SCORE_FIELDS)

    n_a = sum(1 for r in out_rows if r["tier"] == "A")
    n_b = sum(1 for r in out_rows if r["tier"] == "B")
    n_c = sum(1 for r in out_rows if r["tier"] == "C")
    log.info("scored: %d wallets   A=%d  B=%d  C=%d   → %s",
             len(out_rows), n_a, n_b, n_c, out)

    if n_a:
        log.info("--- A-tier wallets ---")
        for r in out_rows:
            if r["tier"] != "A":
                break
            log.info("  %s  score=%.1f  pnl=$%.0f  cat=%s  settled=%d",
                     r["wallet"], r["score"], r["total_pnl"],
                     r["top_category"], r["settled_trades"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
