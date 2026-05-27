#!/usr/bin/env python3
"""
seed_watchlist.py — populate data/orderbook_watchlist.txt from top-tier wallets.

Reads:
  data/wallet_scores.csv          (output of 03_score_wallets.py)
  data/wallet_trade_history.csv   (output of 02_build_history.py)
  data/markets_cache.json         (output of 02_build_history.py)

Writes:
  data/orderbook_watchlist.txt    (one CLOB token_id per line, with comments)

Selection logic, in priority order:
  1. All A-tier wallets if any exist
  2. Else top-N B-tier wallets by score
  3. Else top-N hard-pass-failing wallets sorted by total_volume_usd desc
     (last resort — only used when there's no positive signal at all)

For each selected wallet, we collect every UNRESOLVED trade's condition_id,
look up the market's clobTokenIds, and emit them as the watchlist. Tokens
are deduped and capped at WAR_WATCHLIST_CAP (default 200) to keep recorder
batch sizes sane.

The recorder hot-reloads when this file's mtime changes — no service
restart needed.

Env:
  WAR_DATA_DIR          (default "data")
  WAR_WATCHLIST_CAP     (default 200)
  WAR_WATCHLIST_B_TOP   (default 5)   how many B-tier wallets to use if no A
"""
from __future__ import annotations
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alpha_radar.util import setup_logging                          # noqa: E402


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(x, default=0.0):
    try: return float(x)
    except (TypeError, ValueError): return default


def _parse_clob_ids(market: dict) -> list[str]:
    t = (market or {}).get("clobTokenIds") or (market or {}).get("clob_token_ids")
    if isinstance(t, str):
        try: t = json.loads(t)
        except Exception: return []
    return [str(x) for x in (t or [])]


def main() -> int:
    log = setup_logging(os.getenv("WAR_LOG_LEVEL", "INFO"), "war.seed")
    data_dir = Path(os.getenv("WAR_DATA_DIR", "data"))
    cap = int(os.getenv("WAR_WATCHLIST_CAP", "200"))
    b_top = int(os.getenv("WAR_WATCHLIST_B_TOP", "5"))

    scores = _load_csv(data_dir / "wallet_scores.csv")
    history = _load_csv(data_dir / "wallet_trade_history.csv")
    cache_path = data_dir / "markets_cache.json"
    if not scores or not history or not cache_path.exists():
        log.error("missing inputs. Need: wallet_scores.csv, wallet_trade_history.csv, "
                  "markets_cache.json under %s", data_dir)
        return 2

    cache = json.loads(cache_path.read_text())

    # ---- selection ----
    a_wallets = [r for r in scores if r["tier"] == "A"]
    b_wallets = [r for r in scores if r["tier"] == "B"]

    if a_wallets:
        selected = a_wallets
        source_label = f"A-tier ({len(a_wallets)} wallets)"
    elif b_wallets:
        b_sorted = sorted(b_wallets, key=lambda r: -_f(r.get("score")))[:b_top]
        selected = b_sorted
        source_label = f"top-{len(b_sorted)} B-tier (no A available)"
    else:
        # last resort: by total_volume_usd. Only useful for testing the recorder.
        c_sorted = sorted(scores, key=lambda r: -_f(r.get("total_volume_usd")))[:b_top]
        selected = c_sorted
        source_label = f"top-{len(c_sorted)} by volume (no A or B available)"

    selected_addrs = {r["wallet"] for r in selected}
    log.info("selected %d wallets: %s", len(selected_addrs), source_label)

    # ---- collect open-market condition_ids per wallet ----
    open_cids: dict[str, int] = defaultdict(int)         # cid -> hit count
    for h in history:
        if h.get("wallet") not in selected_addrs:
            continue
        if h.get("resolved") == "1":
            continue
        cid = h.get("condition_id")
        if cid:
            open_cids[cid] += 1

    log.info("found %d unique unresolved condition_ids across selected wallets",
             len(open_cids))

    # ---- map cid -> clobTokenIds via cache ----
    tokens: list[tuple[str, str, int, str]] = []   # (token_id, cid, hits, question)
    cids_with_tokens = 0
    cids_missing = 0
    for cid, hits in sorted(open_cids.items(), key=lambda x: -x[1]):
        entry = cache.get(cid) or {}
        market = entry.get("market") if isinstance(entry, dict) else None
        ids = _parse_clob_ids(market or {})
        if not ids:
            cids_missing += 1
            continue
        cids_with_tokens += 1
        q = ((market or {}).get("question") or "")[:80]
        for t in ids:
            tokens.append((t, cid, hits, q))

    log.info("market cache: %d/%d cids had clobTokenIds (%d missing)",
             cids_with_tokens, len(open_cids), cids_missing)

    # ---- dedupe + cap ----
    seen = set()
    uniq: list[tuple[str, str, int, str]] = []
    for tok, cid, hits, q in tokens:
        if tok in seen:
            continue
        seen.add(tok)
        uniq.append((tok, cid, hits, q))
        if len(uniq) >= cap:
            break

    # ---- write ----
    out = data_dir / "orderbook_watchlist.txt"
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# auto-seeded by seed_watchlist.py\n")
        f.write(f"# source: {source_label}\n")
        f.write(f"# {len(uniq)} token_ids covering {len(open_cids)} open markets\n")
        f.write(f"# wallets: {sorted(selected_addrs)}\n\n")
        for tok, cid, hits, q in uniq:
            f.write(f"# hits={hits} cid={cid[:14]}... q={q}\n")
            f.write(f"{tok}\n")

    log.info("wrote %d token_ids → %s", len(uniq), out)
    log.info("recorder will hot-reload on next round (mtime changed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
