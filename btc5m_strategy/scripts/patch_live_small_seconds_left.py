"""Patch btc5m_live_small.py to define seconds_left inside _mark_positions.

Run from repository root:

    python btc5m_strategy/scripts/patch_live_small_seconds_left.py

The script is idempotent. It only inserts the missing line if absent.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("btc5m_strategy/arb/btc5m_live_small.py")
OLD = '''        entry = float(row["entry_price"])
        size = float(row["size_shares"])
        notional = float(row["notional_usd"])
        pnl = (float(mark) - entry) * size
'''
NEW = '''        entry = float(row["entry_price"])
        size = float(row["size_shares"])
        notional = float(row["notional_usd"])
        seconds_left = float(row["end_epoch"]) - now_ts
        pnl = (float(mark) - entry) * size
'''


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    if 'seconds_left = float(row["end_epoch"]) - now_ts' in text:
        print("already patched")
        return 0
    if OLD not in text:
        raise SystemExit("target block not found; patch manually")
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"patched {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
