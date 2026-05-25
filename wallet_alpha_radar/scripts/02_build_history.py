#!/usr/bin/env python3
"""
02_build_history.py — pull per-wallet trade history and join market resolutions.

Reads:  data/candidate_wallets.csv
Writes: data/wallet_trade_history.csv  (flat: one row per fill)
        data/markets_cache.json        (gamma metadata cache)

Resume-safe: each wallet's trades are reloaded; if you re-run, existing rows
for that wallet are overwritten by re-fetching. Use WAR_HISTORY_SKIP_EXISTING=1
to skip wallets already present in the output (faster re-runs).
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alpha_radar import config as cfg_mod                          # noqa: E402
from alpha_radar.api import ApiClient                              # noqa: E402
from alpha_radar.util import (                                     # noqa: E402
    setup_logging, read_csv, write_csv, append_csv, addr_lower,
    parse_ts, chunked,
)


HISTORY_FIELDS = [
    "wallet", "ts", "transaction_hash", "condition_id", "asset_id",
    "event_slug", "market_slug", "category", "question",
    "outcome", "outcome_index", "side", "price", "size", "usd_size",
    "resolved", "winning_outcome_index", "won",
]


# ---------------------------------------------------------------------------
# market metadata cache
# ---------------------------------------------------------------------------
class MarketCache:
    def __init__(self, path: Path, ttl_sec: int):
        self.path = path
        self.ttl = ttl_sec
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def get(self, condition_id: str) -> dict | None:
        e = self.data.get(condition_id)
        if not e:
            return None
        if time.time() - e.get("_cached_at", 0) > self.ttl:
            return None
        return e.get("market")

    def put(self, condition_id: str, market: dict) -> None:
        self.data[condition_id] = {"_cached_at": time.time(), "market": market}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f)


# ---------------------------------------------------------------------------
# market metadata helpers
# ---------------------------------------------------------------------------
def _winning_outcome_index(market: dict) -> int | None:
    """
    Extract the winning outcome index for a resolved Polymarket binary market.
    Different gamma payload versions ship different fields — try them all.
    """
    if not market:
        return None
    # explicit numeric winner
    for k in ("winnerOutcomeIndex", "resolvedOutcomeIndex"):
        v = market.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    # outcomePrices: ["1","0"] or [1,0] → winner is the index of the 1
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            op = None
    if isinstance(op, list) and op:
        try:
            floats = [float(x) for x in op]
            if max(floats) >= 0.999:
                return floats.index(max(floats))
        except (TypeError, ValueError):
            pass
    return None


def _outcomes(market: dict) -> list[str]:
    o = market.get("outcomes")
    if isinstance(o, str):
        try:
            return json.loads(o)
        except Exception:
            return []
    if isinstance(o, list):
        return o
    return []


def _clob_token_ids(market: dict) -> list[str]:
    t = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(t, str):
        try:
            return json.loads(t)
        except Exception:
            return []
    if isinstance(t, list):
        return [str(x) for x in t]
    return []


def _outcome_index_from_trade(trade: dict, market: dict | None) -> int | None:
    """Resolve which outcome (0=Yes, 1=No, etc.) a trade refers to."""
    # data-api sometimes returns outcomeIndex directly
    for k in ("outcomeIndex", "outcome_index"):
        v = trade.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    # otherwise match asset_id / token_id against clobTokenIds
    asset = trade.get("asset") or trade.get("assetId") or trade.get("token_id") or trade.get("tokenId")
    if asset and market:
        ids = _clob_token_ids(market)
        a = str(asset)
        for i, tid in enumerate(ids):
            if str(tid) == a:
                return i
    # fall back: match the human-readable outcome string
    name = trade.get("outcome")
    if name and market:
        outs = _outcomes(market)
        for i, n in enumerate(outs):
            if str(n).strip().lower() == str(name).strip().lower():
                return i
    return None


# ---------------------------------------------------------------------------
# trade normalization
# ---------------------------------------------------------------------------
def _normalize_trade(trade: dict, wallet: str, market: dict | None) -> dict | None:
    cond = trade.get("conditionId") or trade.get("condition_id") or (market or {}).get("conditionId")
    if not cond:
        return None
    ts = parse_ts(trade.get("timestamp") or trade.get("ts") or trade.get("matchTime"))
    if ts is None:
        return None
    side_raw = (trade.get("side") or trade.get("type") or "").upper()
    side = "BUY" if side_raw in ("BUY", "B") else ("SELL" if side_raw in ("SELL", "S") else side_raw)
    try:
        price = float(trade.get("price") or 0.0)
        size = float(trade.get("size") or trade.get("amount") or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or size <= 0:
        return None

    outcome_idx = _outcome_index_from_trade(trade, market)
    win_idx = _winning_outcome_index(market) if market else None
    # _winning_outcome_index returns a value only when there's a definitive
    # winner (explicit field OR outcomePrices showing 1.0). Treat that alone
    # as resolved; the `closed` flag is sometimes lagging on gamma.
    resolved = bool(market and win_idx is not None)
    won = (resolved and outcome_idx is not None and outcome_idx == win_idx) if resolved else None

    return {
        "wallet": wallet,
        "ts": int(ts),
        "transaction_hash": trade.get("transactionHash") or trade.get("hash") or "",
        "condition_id": cond,
        "asset_id": str(trade.get("asset") or trade.get("assetId") or trade.get("tokenId") or ""),
        "event_slug": trade.get("eventSlug") or (market or {}).get("eventSlug") or "",
        "market_slug": trade.get("slug") or (market or {}).get("slug") or "",
        "category": (market or {}).get("category") or "",
        "question": ((market or {}).get("question") or "")[:200],
        "outcome": trade.get("outcome") or "",
        "outcome_index": outcome_idx if outcome_idx is not None else "",
        "side": side,
        "price": price,
        "size": size,
        "usd_size": price * size,
        "resolved": "1" if resolved else "0",
        "winning_outcome_index": win_idx if win_idx is not None else "",
        "won": ("1" if won else "0") if won is not None else "",
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _load_existing_wallets(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    rows = read_csv(path)
    for r in rows:
        out.add(addr_lower(r.get("wallet")))
    return out


def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "war.history")
    api = ApiClient(cfg.api)

    cand_path = Path(cfg.data_dir) / "candidate_wallets.csv"
    if not cand_path.exists():
        log.error("missing %s — run 01_discover_candidates.py first", cand_path)
        return 2
    candidates = read_csv(cand_path)
    wallets = sorted({addr_lower(c.get("wallet")) for c in candidates if c.get("wallet")})
    log.info("loaded %d unique candidate wallets from %s", len(wallets), cand_path)

    out_path = Path(cfg.data_dir) / "wallet_trade_history.csv"
    cache = MarketCache(Path(cfg.data_dir) / "markets_cache.json",
                        cfg.history.market_cache_sec)

    skip_existing = os.getenv("WAR_HISTORY_SKIP_EXISTING", "0") == "1"
    already = _load_existing_wallets(out_path) if skip_existing else set()
    if skip_existing:
        log.info("skip-existing: %d wallets already have history rows", len(already))

    if not skip_existing and out_path.exists():
        # full rebuild: truncate so we don't double-write
        out_path.unlink()
        log.info("truncated %s for full rebuild", out_path)

    total_rows = 0
    for i, wallet in enumerate(wallets, 1):
        if wallet in already:
            continue
        log.info("[%d/%d] fetching trades for %s", i, len(wallets), wallet)
        trades = api.user_trades_all(
            wallet,
            page_size=cfg.history.trades_page_size,
            hard_cap=cfg.history.trades_hard_cap,
        )
        if not trades:
            log.info("  no trades")
            continue
        log.info("  %d raw trades", len(trades))

        # gather missing condition_ids for this wallet
        cond_ids = sorted({(t.get("conditionId") or t.get("condition_id"))
                           for t in trades if t.get("conditionId") or t.get("condition_id")})
        missing = [c for c in cond_ids if c and cache.get(c) is None]
        # batch the fetches
        for batch in chunked(missing, 50):
            mkts = api.markets_by_condition_ids(batch)
            for m in mkts:
                cid = m.get("conditionId") or m.get("condition_id")
                if cid:
                    cache.put(cid, m)
        cache.save()

        rows = []
        for t in trades:
            cid = t.get("conditionId") or t.get("condition_id")
            mkt = cache.get(cid) if cid else None
            r = _normalize_trade(t, wallet, mkt)
            if r:
                rows.append(r)
        if rows:
            append_csv(out_path, rows, HISTORY_FIELDS)
            total_rows += len(rows)
        log.info("  appended %d rows (running total: %d)", len(rows), total_rows)

    log.info("done. total trade rows: %d → %s", total_rows, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
