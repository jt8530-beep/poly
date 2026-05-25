#!/usr/bin/env python3
"""
05_record_wallet_trades.py — long-running tail of A-tier wallets' new trades.

Why this exists:
  * Phase 2 backtest needs to know exactly when each watched wallet placed a
    trade (timestamp, condition_id, price, size) so it can look up the
    corresponding orderbook snapshot at T+60s / T+300s / T+1800s.
  * Polymarket data-api keeps trade history available for a long time, but
    we want to capture the trade as soon as it appears so our seen_at field
    bounds the polling latency we apply during backtest.
  * Without this, four weeks from now we'd have orderbook snapshots but no
    aligned trade timestamps to backtest against.

Reads:
  data/wallet_scores.csv  (we tail wallets whose tier is in WAR_TT_TIERS)
Writes:
  data/wallet_trades_tail.csv

Behaviour:
  * Polls each watched wallet every WAR_TT_INTERVAL seconds (default 60).
  * Dedupes by transactionHash; on startup it loads existing rows so it
    doesn't double-record after a restart.
  * Hot-reloads the wallet list when wallet_scores.csv mtime changes — so
    when you re-seed scores weekly the recorder picks up the new A-tier set
    without restart.
  * Resilient to network errors; logs and continues.
  * SIGINT/SIGTERM cleanly shuts down.

Run as systemd: see wallet_alpha_radar_trades.service.
"""
from __future__ import annotations
import csv
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
from alpha_radar.util import (                                     # noqa: E402
    setup_logging, append_csv, addr_lower, parse_ts, utcnow_iso,
)


TAIL_FIELDS = [
    "ts", "wallet", "transaction_hash", "condition_id", "asset_id",
    "event_slug", "market_slug", "outcome", "outcome_index",
    "side", "price", "size", "usd_size", "seen_at",
]


_running = True

def _stop(signum, frame):                                         # noqa: ARG001
    global _running
    _running = False

signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# ---------------------------------------------------------------------------
# wallet list — reload from wallet_scores.csv when its mtime changes
# ---------------------------------------------------------------------------
class WalletList:
    def __init__(self, path: Path, tiers: tuple[str, ...], log):
        self.path = path
        self.tiers = set(t.upper() for t in tiers)
        self.log = log
        self._mtime: Optional[float] = None
        self.wallets: set[str] = set()

    def maybe_reload(self) -> bool:
        """Return True if the watched wallet set changed."""
        try:
            mt = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            mt = 0.0
        if mt == self._mtime:
            return False
        self._mtime = mt
        new = set()
        if self.path.exists():
            with self.path.open("r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("tier") or "").upper() in self.tiers:
                        addr = addr_lower(row.get("wallet"))
                        if addr:
                            new.add(addr)
        if new != self.wallets:
            added = new - self.wallets
            removed = self.wallets - new
            self.log.info("wallet list changed: %d total (added %d, removed %d)",
                          len(new), len(added), len(removed))
            if added:
                self.log.info("  added: %s", sorted(added))
            if removed:
                self.log.info("  removed: %s", sorted(removed))
            self.wallets = new
            return True
        return False


# ---------------------------------------------------------------------------
# seen-set persistence
# ---------------------------------------------------------------------------
def load_seen_hashes(out_path: Path) -> set[str]:
    """Read existing tail CSV and collect tx hashes so we don't double-record."""
    if not out_path.exists() or out_path.stat().st_size == 0:
        return set()
    seen: set[str] = set()
    with out_path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            h = row.get("transaction_hash")
            if h:
                seen.add(h)
    return seen


# ---------------------------------------------------------------------------
# trade normalization (subset of 02_build_history.py — no market metadata
# needed at tail time, we'll join during backtest)
# ---------------------------------------------------------------------------
def _normalize(trade: dict, wallet: str) -> dict | None:
    cond = trade.get("conditionId") or trade.get("condition_id")
    tx = trade.get("transactionHash") or trade.get("hash")
    if not (cond and tx):
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
    oi = trade.get("outcomeIndex")
    try:
        oi = int(oi) if oi is not None else ""
    except (TypeError, ValueError):
        oi = ""
    return {
        "ts": int(ts),
        "wallet": wallet,
        "transaction_hash": tx,
        "condition_id": cond,
        "asset_id": str(trade.get("asset") or trade.get("assetId") or trade.get("tokenId") or ""),
        "event_slug": trade.get("eventSlug") or "",
        "market_slug": trade.get("slug") or "",
        "outcome": trade.get("outcome") or "",
        "outcome_index": oi,
        "side": side,
        "price": price,
        "size": size,
        "usd_size": price * size,
        "seen_at": utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = cfg_mod.load()
    log = setup_logging(cfg.log_level, "war.tradetail")
    api = ApiClient(cfg.api)
    tcfg = cfg.tradetail

    out_path = Path(tcfg.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen = load_seen_hashes(out_path)
    log.info("starting. existing tail rows have %d unique tx hashes", len(seen))

    wallets = WalletList(Path(tcfg.scores_path), tcfg.tiers, log)
    interval = max(15, tcfg.interval_sec)
    fetch_limit = max(20, tcfg.fetch_limit)
    log.info("interval=%ds  fetch_limit=%d  tiers=%s  scores=%s",
             interval, fetch_limit, sorted(wallets.tiers), tcfg.scores_path)

    rounds = 0
    while _running:
        wallets.maybe_reload()
        if not wallets.wallets:
            log.info("no wallets to tail (scores.csv missing or no matching tier) — sleeping %ds",
                     interval)
            for _ in range(interval):
                if not _running:
                    break
                time.sleep(1)
            continue

        round_start = time.time()
        new_total = 0
        new_per_wallet = 0
        for w in sorted(wallets.wallets):
            if not _running:
                break
            try:
                trades = api.user_trades(w, limit=fetch_limit)
            except Exception as e:                                # noqa: BLE001
                log.warning("user_trades failed for %s: %s", w, e)
                continue
            new_rows = []
            for t in trades or []:
                tx = t.get("transactionHash") or t.get("hash")
                if not tx or tx in seen:
                    continue
                row = _normalize(t, w)
                if not row:
                    continue
                seen.add(tx)
                new_rows.append(row)
            if new_rows:
                append_csv(out_path, new_rows, TAIL_FIELDS)
                new_total += len(new_rows)
                new_per_wallet += 1
                log.info("[%s] +%d new trades (latest ts=%s)",
                         w, len(new_rows), max(r["ts"] for r in new_rows))

        rounds += 1
        elapsed = time.time() - round_start
        if rounds % 5 == 0 or new_total > 0:
            log.info("round %d: %d new trades from %d wallet(s) in %.1fs (seen-set=%d)",
                     rounds, new_total, new_per_wallet, elapsed, len(seen))

        # sleep remainder
        sleep_for = max(0.0, interval - elapsed)
        end_at = time.time() + sleep_for
        while _running and time.time() < end_at:
            time.sleep(min(1.0, end_at - time.time()))

    log.info("recorder stopped after %d rounds.", rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
