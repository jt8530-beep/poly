"""
Main loop for the arb engine (v1 paper-safe).

Changes vs v0:
  * Both paper legs use the SAME q_shares (from ladder.v1 signal).
  * Paper legs are BUY orders (BUY NO + BUY YES), no naked SELL.
  * Risk counts distinct SIGNALS per day (not legs).
  * Risk state is refreshed from DB at top of each scan (survives restarts).
  * Paper settler runs at the top of each loop to close resolved positions.
  * Telegram uses HTML parse_mode.
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
from . import settler as _settler
from .poly_client import PolyClient
from .risk import RiskManager
from .tg_notifier import Notifier


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_once(cfg, client, db, risk, tg, seen_fingerprints: set) -> dict:
    """One scan pass. Returns small stats dict."""
    t0 = time.time()

    # 0) keep risk counters fresh; close resolved/stale paper trades
    risk.refresh_from_db()
    try:
        settle_stats = _settler.settle_open_paper_trades(db, client, risk)
    except Exception as e:
        logging.warning("settler error: %s", e)
        settle_stats = {}

    # 1) pull events + filter to ladder-likes
    events = client.list_all_active_events(page_size=500, hard_cap=3000)
    events = [e for e in events if len(e.get("markets") or []) >= 3]

    stats = {
        "events_scanned": len(events),
        "signals": 0, "executed_paper": 0, "skipped_risk": 0,
        "settled": settle_stats.get("settled", 0),
        "stale_closed": settle_stats.get("stale_closed", 0),
    }

    for ev in events:
        try:
            sigs = _ladder.scan_event(
                client, ev,
                min_edge_bps=cfg.ladder_min_violation_bps,
                min_notional_usd=cfg.ladder_min_depth_usd,
                max_notional_per_leg=cfg.risk.max_notional_per_trade,
            )
        except Exception as e:
            logging.warning("scan_event error: %s", e)
            continue

        for s in sigs:
            # dedup (event, pair, coarse edge bucket)
            fp = (s.event_slug, s.leg_a.market_id, s.leg_b.market_id, s.edge_bps // 50)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)

            stats["signals"] += 1

            total_notional = s.max_size_usd
            ok, why = risk.check_signal(total_notional)
            if not ok:
                stats["skipped_risk"] += 1
                logging.info("risk skip: %s  (%s)", why, s.event_slug)
                continue

            # --- record signal (structured)
            raw = json.dumps({
                "event_slug": s.event_slug,
                "edge_bps": s.edge_bps,
                "exp_profit": s.expected_profit_usd,
                "max_size": s.max_size_usd,
                "q_shares": s.q_shares,
                "leg_a_q": s.leg_a.question,
                "leg_b_q": s.leg_b.question,
            })
            sid = _ledger.record_signal(db, "ladder_C", s, raw, _now_iso())

            if cfg.paper_mode:
                # both legs are BUY orders, same q_shares
                _ledger.record_paper_trade(
                    db, _now_iso(), sid,
                    s.leg_a.market_id, s.leg_a.token_id,
                    "BUY", s.leg_a.token_outcome,
                    price=s.leg_a.price,
                    size_shares=s.leg_a.size_shares,
                    note=s.leg_a.action_str,
                )
                _ledger.record_paper_trade(
                    db, _now_iso(), sid,
                    s.leg_b.market_id, s.leg_b.token_id,
                    "BUY", s.leg_b.token_outcome,
                    price=s.leg_b.price,
                    size_shares=s.leg_b.size_shares,
                    note=s.leg_b.action_str,
                )
                # ONE signal, ONE risk increment (total notional, not per leg)
                risk.on_new_signal_opened(total_notional)
                stats["executed_paper"] += 1
                tg.signal(s, "ladder_C", paper=True)
            else:
                logging.warning("LIVE mode not implemented yet; signal dropped (safety)")

    stats["elapsed_sec"] = round(time.time() - t0, 2)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",  action="store_true", help="single scan, then exit")
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
    log.info("starting engine (v1)  paper=%s  scan_interval=%ss  min_edge_bps=%s",
             cfg.paper_mode, cfg.scan_interval_sec, cfg.ladder_min_violation_bps)

    client = PolyClient(cfg.poly.gamma_url, cfg.poly.clob_url)
    db     = _ledger.open_db(cfg.db_path)
    risk   = RiskManager(cfg, db)
    tg     = Notifier(cfg.tg.bot_token, cfg.tg.chat_id, cfg.tg.enabled)

    tg.send(f"engine v1 started, paper={cfg.paper_mode}, min_edge={cfg.ladder_min_violation_bps}bps")

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
