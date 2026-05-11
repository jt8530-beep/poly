"""Settler regression tests. Run with: python -m arb.tests.test_settler"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile

from arb import ledger
from arb.settler import settle_open_paper_trades


@dataclass
class DummyRisk:
    closed_notional: float = 0.0

    def on_close_trade(self, notional_usd: float) -> None:
        self.closed_notional += notional_usd


class DummyClient:
    gamma_url = "https://example.invalid"

    def _get(self, url: str):
        return {
            "closed": True,
            "outcomes": '["Yes","No"]',
            "outcomePrices": "[1,0]",
        }


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "ledger.sqlite"
        db = ledger.open_db(str(db_path))
        db.execute(
            "INSERT INTO trades "
            "(ts_utc, signal_id, market_id, token_outcome, price, size, "
            "notional_usd, status, mode) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "2026-05-12T00:00:00+00:00",
                1,
                "m1",
                "YES",
                0.40,
                10.0,
                4.0,
                "OPEN",
                "paper",
            ),
        )
        db.commit()

        risk = DummyRisk()
        stats = settle_open_paper_trades(db, DummyClient(), risk)
        assert stats["settled"] == 1, stats
        assert abs(risk.closed_notional - 4.0) < 1e-9
        db.close()

        reopened = ledger.open_db(str(db_path))
        row = reopened.execute(
            "SELECT status, close_price, pnl_usd, close_reason FROM trades WHERE id=1"
        ).fetchone()
        assert row == ("CLOSED", 1.0, 6.0, "resolved"), row
        reopened.close()

    print("settler persistence test passed")


if __name__ == "__main__":
    main()
