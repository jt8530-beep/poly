"""
Risk / circuit breaker layer.

The engine MUST call risk.check(signal) before execution and risk.record(fill)
after. Violations cause the signal to be dropped or the whole engine to halt.
"""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass

@dataclass
class RiskState:
    open_notional_usd: float = 0.0
    daily_new_trades: int    = 0
    peak_equity_usd: float   = 0.0
    last_equity_usd: float   = 0.0
    halted: bool = False
    halt_reason: str = ""

class RiskManager:
    def __init__(self, cfg, db: sqlite3.Connection):
        self.cfg = cfg
        self.db  = db
        self.state = RiskState()
        self._load_state()

    def _load_state(self):
        c = self.db.cursor()
        r = c.execute(
            "SELECT peak_equity, last_equity, halted, halt_reason FROM risk_state WHERE id=1"
        ).fetchone()
        if r:
            self.state.peak_equity_usd = r[0] or 0.0
            self.state.last_equity_usd = r[1] or 0.0
            self.state.halted = bool(r[2])
            self.state.halt_reason = r[3] or ""
        # daily counter: count today's fills
        today = time.strftime("%Y-%m-%d", time.gmtime())
        n = c.execute(
            "SELECT COUNT(*) FROM trades WHERE substr(ts_utc,1,10)=?", (today,)
        ).fetchone()[0]
        self.state.daily_new_trades = int(n or 0)
        open_n = c.execute(
            "SELECT COALESCE(SUM(notional_usd),0) FROM trades WHERE status='OPEN'"
        ).fetchone()[0]
        self.state.open_notional_usd = float(open_n or 0)

    def check_signal(self, expected_notional_usd: float) -> tuple[bool, str]:
        s = self.state; r = self.cfg.risk
        if s.halted:
            return False, f"HALTED: {s.halt_reason}"
        if expected_notional_usd > r.max_notional_per_trade:
            return False, f"size {expected_notional_usd:.2f} > max per trade {r.max_notional_per_trade}"
        if s.open_notional_usd + expected_notional_usd > r.max_open_notional:
            return False, f"open total {s.open_notional_usd+expected_notional_usd:.2f} > max {r.max_open_notional}"
        if s.daily_new_trades >= r.max_daily_new_trades:
            return False, f"daily trade count {s.daily_new_trades} >= max {r.max_daily_new_trades}"
        return True, "ok"

    def on_new_trade(self, notional_usd: float):
        self.state.open_notional_usd += notional_usd
        self.state.daily_new_trades += 1

    def on_close_trade(self, notional_usd: float):
        self.state.open_notional_usd = max(0.0, self.state.open_notional_usd - notional_usd)

    def update_equity(self, equity_usd: float):
        s = self.state
        s.last_equity_usd = equity_usd
        if equity_usd > s.peak_equity_usd:
            s.peak_equity_usd = equity_usd
        if s.peak_equity_usd > 0:
            dd = 1.0 - equity_usd / s.peak_equity_usd
            if dd >= self.cfg.risk.hard_drawdown_stop:
                s.halted = True
                s.halt_reason = f"hard drawdown {dd*100:.1f}% >= {self.cfg.risk.hard_drawdown_stop*100:.0f}%"
        self._persist()

    def _persist(self):
        c = self.db.cursor()
        c.execute(
            "INSERT INTO risk_state (id,peak_equity,last_equity,halted,halt_reason) VALUES (1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET peak_equity=excluded.peak_equity,last_equity=excluded.last_equity,"
            "halted=excluded.halted,halt_reason=excluded.halt_reason",
            (self.state.peak_equity_usd, self.state.last_equity_usd,
             1 if self.state.halted else 0, self.state.halt_reason),
        )
        self.db.commit()
