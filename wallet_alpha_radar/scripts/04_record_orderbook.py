#!/usr/bin/env python3
"""
04_record_orderbook.py — long-running orderbook snapshotter.

This is the Phase-2 prerequisite. Polymarket has no public historical
orderbook API, so we have to record it ourselves going forward. Run this on
your Sydney box as a systemd service. After ~3-4 weeks of accumulation you'll
have enough to do delay-replicated PnL backtests.

Inputs:
  data/orderbook_watchlist.txt   one CLOB token_id per line
                                 lines starting with `#` are comments
                                 (use 03_score output to seed this manually)
Outputs:
  data/orderbook/YYYY-MM-DD.jsonl   one snapshot per line
    { "ts": <unix>, "token_id": "...", "bids": [{"price","size"},...],
      "asks": [...], "best_bid", "best_ask", "spread", "depth_bid", "depth_ask" }

Behaviour:
  * batches up to WAR_OB_BATCH token_ids per request
  * sleeps WAR_OB_INTERVAL between rounds
  * rotates files at UTC midnight
  * on watchlist file change, hot-reloads on next round
  * never crashes on a single bad token — logs and continues
"""
from __future__ import annotations
import datetime as dt
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alpha_radar import config as cfg_mod                          # noqa: E402
from alpha_radar.api import ApiClient                              # noqa: E402
from alpha_radar.util import setup_logging, ensure_dir, chunked    # noqa: E402


_running = True

def _stop(signum, frame):                                         # noqa: ARG001
    global _running
    _running = False

signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def _read_watchlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    # de-dupe preserving order
    seen = set()
    uniq = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq


def _today_path(base: Path) -> Path:
    return base / (dt.datetime.utcnow().strftime("%Y-%m-%d") + ".jsonl")


def _summarize_book(book: dict) -> dict:
    """Derive best_bid/ask/spread/depth from a CLOB book payload."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    def _f(v):
        try: return float(v)
        except (TypeError, ValueError): return 0.0
    # sort defensively (CLOB usually returns sorted but don't rely on it)
    bids_sorted = sorted(bids, key=lambda x: -_f(x.get("price")))
    asks_sorted = sorted(asks, key=lambda x: _f(x.get("price")))
    best_bid = _f(bids_sorted[0]["price"]) if bids_sorted else 0.0
    best_ask = _f(asks_sorted[0]["price"]) if asks_sorted else 0.0
    spread = (best_ask - best_bid) if best_bid and best_ask else None
    depth_bid = sum(_f(x.get("size")) * _f(x.get("price")) for x in bids_sorted[:5])
    depth_ask = sum(_f(x.get("size")) * _f(x.get("price")) for x in asks_sorted[:5])
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "depth_bid_top5_usd": depth_bid,
        "depth_ask_top5_usd": depth_ask,
        # keep the top 5 levels each side; full book is wasteful at 30s cadence
        "bids": [{"price": _f(x.get("price")), "size": _f(x.get("size"))} for x in bids_sorted[:5]],
        "asks": [{"price": _f(x.get("price")), "size": _f(x.get("size"))} for x in asks_sorted[:5]],
    }


def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "war.recorder")
    api = ApiClient(cfg.api)

    watchlist_path = Path(cfg.recorder.watchlist_path)
    snapshot_dir = ensure_dir(cfg.recorder.snapshot_dir)
    interval = max(5, cfg.recorder.interval_sec)
    batch_size = max(1, cfg.recorder.batch_size)

    log.info("recorder starting. watchlist=%s snapshots=%s interval=%ds batch=%d",
             watchlist_path, snapshot_dir, interval, batch_size)

    last_watchlist_mtime: Optional[float] = None
    tokens: list[str] = []
    rounds = 0

    while _running:
        # hot-reload watchlist when its mtime changes
        try:
            mt = watchlist_path.stat().st_mtime if watchlist_path.exists() else 0.0
        except OSError:
            mt = 0.0
        if mt != last_watchlist_mtime:
            tokens = _read_watchlist(watchlist_path)
            last_watchlist_mtime = mt
            log.info("watchlist reloaded: %d token_ids", len(tokens))

        if not tokens:
            log.info("empty watchlist — sleeping %ds", interval)
            for _ in range(interval):
                if not _running:
                    break
                time.sleep(1)
            continue

        round_start = time.time()
        out_path = _today_path(snapshot_dir)
        n_written = 0
        with out_path.open("a", encoding="utf-8") as f:
            for batch in chunked(tokens, batch_size):
                try:
                    books = api.books_batch(batch)
                except Exception as e:                              # noqa: BLE001
                    log.warning("books_batch failed for %d tokens: %s", len(batch), e)
                    continue
                if not books:
                    continue
                # response should be one entry per requested token; if the
                # API drops some, just record what we got
                ts = int(time.time())
                for b in books:
                    tid = b.get("asset_id") or b.get("token_id") or b.get("market") or ""
                    if not tid:
                        continue
                    summary = _summarize_book(b)
                    rec = {"ts": ts, "token_id": str(tid), **summary}
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    n_written += 1
        rounds += 1
        elapsed = time.time() - round_start
        if rounds % 10 == 0 or n_written == 0:
            log.info("round %d: wrote %d snapshots in %.1fs → %s",
                     rounds, n_written, elapsed, out_path.name)
        # sleep remainder of the interval
        sleep_left = max(0.0, interval - elapsed)
        end_at = time.time() + sleep_left
        while _running and time.time() < end_at:
            time.sleep(min(1.0, end_at - time.time()))

    log.info("recorder stopped after %d rounds.", rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
