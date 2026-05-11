"""
Main loop for the arb engine.

Flow (paper mode):
  while True:
    1. pull all active events
    2. for each ladder-like event, fetch top-of-book
    3. detect violations -> LadderSignal[]
    4. risk.check_signal()
    5. if ok: record_signal(), record_paper_trade() (both legs), TG notify
    6. sleep(scan_interval_sec)

Live mode adds: sign and submit real orders through clob.polymarket.com.
We intentionally STOP before live execution in this v0 -- executor.py has
stubs. You'll flip PAPER_MODE=false only after a week of paper data.
"""
from __future__ import annotations
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from . import config as _config
from . import ladder as _ladder
from . import ledger as _ledger
from .poly_client import PolyClient
from .risk import RiskManager
from .tg_notifier import Notifier


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_once(cfg, client, db, risk, tg, seen_fingerprints: set) -> dict:
    """One scan pass. Returns small stats dict."""
    t0 = time.time()
    events = client.list_all_active_events(page_size=500, hard_cap=3000)
    # filter ladder-ish by market count early (cheaper than parsing)
    events = [e for e in events if len(e.get("markets") or []) >= 3]

    stats = {"events_scanned": len(events), "signals": 0, "executed_paper": 0, "skipped_risk": 0}

    for ev in events:
        try:
            sigs = _ladder.scan_event(
                client, ev,
                min_edge_bps=cfg.ladder_min_violation_bps,
                min_depth_usd=cfg.ladder_min_depth_usd,
            )
        except Exception as e:
            logging.warning("scan_event error: %s", e)
            continue

        for s in sigs:
            # dedup: don't fire the same pair twice in a row
            fp = (s.event_slug, s.leg_a_market.market_id, s.leg_b_market.market_id, s.edge_bps // 50)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

            stats["signals"] += 1
            notional = min(s.max_size_usd, cfg.risk.max_notional_per_trade)
            ok, why = risk.check_signal(notional)
            if not ok:
                stats["skipped_risk"] += 1
                logging.info("risk skip: %s  (%s)", why, s.event_slug)
                continue

            # record signal
            raw = json.dumps({
                "event_slug": s.event_slug, "edge_bps": s.edge_bps,
                "exp_profit": s.expected_profit_usd, "max_size": s.max_size_usd,
                "leg_a_q": s.leg_a_market.question, "leg_b_q": s.leg_b_market.question,
            })
            sid = _ledger.record_signal(db, "ladder_C", s, raw, _now_iso())

            # paper "execute" both legs
            if cfg.paper_mode:
                # leg A = SELL YES at best_bid  (sell the overpriced one)
                _ledger.record_paper_trade(
                    db, _now_iso(), sid, s.leg_a_market.market_id,
                    "SELL", "YES",
                    price=s.leg_a_market.best_bid_yes or 0.0,
                    size=notional / max(s.leg_a_market.best_bid_yes or 1e-6, 1e-6),
                    note=s.leg_a_action,
                )
                # leg B = BUY YES at best_ask
                _ledger.record_paper_trade(
                    db, _now_iso(), sid, s.leg_b_market.market_id,
                    "BUY", "YES",
                    price=s.leg_b_market.best_ask_yes or 0.0,
                    size=notional / max(s.leg_b_market.best_ask_yes or 1e-6, 1e-6),
                    note=s.leg_b_action,
                )
                risk.on_new_trade(notional)
                stats["executed_paper"] += 1
                tg.signal(s, "ladder_C", paper=True)
            else:
                # live execution - kept OFF until executor.py is hardened
                logging.warning("LIVE mode not implemented yet; signal dropped (safety)")

    stats["elapsed_sec"] = round(time.time() - t0, 2)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single scan, then exit (useful for cron/tests)")
    ap.add_argument("--paper", action="store_true", help="force paper mode (default on)")
    args = ap.parse_args(argv)

    cfg = _config.load()
    if args.paper:
        cfg.paper_mode = True

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    log = logging.getLogger("engine")
    log.info("starting engine  paper=%s  scan_interval=%ss  min_edge_bps=%s",
             cfg.paper_mode, cfg.scan_interval_sec, cfg.ladder_min_violation_bps)

    client = PolyClient(cfg.poly.gamma_url, cfg.poly.clob_url)
    db     = _ledger.open_db(cfg.db_path)
    risk   = RiskManager(cfg, db)
    tg     = Notifier(cfg.tg.bot_token, cfg.tg.chat_id, cfg.tg.enabled)

    tg.send(f"engine started, paper={cfg.paper_mode}, min_edge={cfg.ladder_min_violation_bps}bps")

    _stop = {"flag": False}
    def _handle(sig, frame):
        log.warning("signal %s - shutting down", sig)
        _stop["flag"] = True
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT,  _handle)

    seen: set = set()
    loop_i = 0
    while not _stop["flag"]:
        try:
            stats = run_once(cfg, client, db, risk, tg, seen)
            log.info("scan done  %s", stats)
            loop_i += 1
            # periodic heartbeat, keep seen set bounded
            if loop_i % 20 == 0:
                tg.heartbeat(json.dumps(stats))
                seen.clear()
        except Exception as e:
            log.exception("loop error: %s", e)
        if args.once:
            break
        time.sleep(cfg.scan_interval_sec)

    tg.send("engine stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
