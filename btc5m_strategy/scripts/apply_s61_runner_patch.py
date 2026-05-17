"""Patch btc5m_live_s6_safe.py to actually use S6.1 safety guards.

Run from repository root:

    python btc5m_strategy/scripts/apply_s61_runner_patch.py

What this patches:
  1. import entry_exit_zone_reason
  2. block new entries inside the dynamic-exit zone
  3. replace the old dynamic_loss_90 behavior with loss-thresholded S6.1 exits

The script is idempotent. Review git diff after running.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("btc5m_strategy/arb/btc5m_live_s6_safe.py")

OLD_IMPORT = "from .s6_safety import calibrated_fair, dynamic_exit_reason, effective_edge_bps, shock_filter_reason\n"
NEW_IMPORT = "from .s6_safety import calibrated_fair, dynamic_exit_reason, effective_edge_bps, entry_exit_zone_reason, shock_filter_reason\n"

OLD_DYNAMIC = '''                exit1_sec=_env_i("BTC5M_DYNAMIC_EXIT1_SEC", 120),
                exit1_min_loss=_env_f("BTC5M_DYNAMIC_EXIT1_MIN_LOSS", 0.15),
                exit2_sec=_env_i("BTC5M_DYNAMIC_EXIT2_SEC", 90),
                exit3_sec=_env_i("BTC5M_DYNAMIC_EXIT3_SEC", 60),
'''
NEW_DYNAMIC = '''                exit1_sec=_env_i("BTC5M_DYNAMIC_EXIT1_SEC", 120),
                exit1_min_loss=_env_f("BTC5M_DYNAMIC_EXIT1_MIN_LOSS", 0.25),
                exit2_sec=_env_i("BTC5M_DYNAMIC_EXIT2_SEC", 60),
                exit2_min_loss=_env_f("BTC5M_DYNAMIC_EXIT2_MIN_LOSS", 0.12),
                exit3_sec=_env_i("BTC5M_DYNAMIC_EXIT3_SEC", 30),
'''

ANCHOR = '''    if seconds_left < _env_i("BTC5M_MIN_SECONDS_BEFORE_END", 90):
        summary["reason"] = "too_late"
        return _record_hold(db, market_slug, "too_late", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

'''
INSERT = '''    if seconds_left < _env_i("BTC5M_MIN_SECONDS_BEFORE_END", 90):
        summary["reason"] = "too_late"
        return _record_hold(db, market_slug, "too_late", summary, seconds_after_start=seconds_after_start, seconds_left=seconds_left)

    exit_zone_reason, exit_zone_payload = entry_exit_zone_reason(
        seconds_left,
        dynamic_exit_enabled=_env_b("BTC5M_DYNAMIC_EXIT_ENABLED", True),
        exit1_sec=_env_i("BTC5M_DYNAMIC_EXIT1_SEC", 120),
        buffer_sec=_env_i("BTC5M_ENTRY_EXIT_BUFFER_SEC", 20),
    )
    if exit_zone_reason:
        summary["reason"] = exit_zone_reason
        summary["entry_exit_zone"] = exit_zone_payload
        return _record_hold(
            db,
            market_slug,
            exit_zone_reason,
            summary,
            seconds_after_start=seconds_after_start,
            seconds_left=seconds_left,
        )

'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        print(f"already patched: {label}")
        return text
    if old not in text:
        raise SystemExit(f"target block not found: {label}")
    print(f"patching: {label}")
    return text.replace(old, new, 1)


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    text = replace_once(text, OLD_IMPORT, NEW_IMPORT, "S6.1 import")
    text = replace_once(text, OLD_DYNAMIC, NEW_DYNAMIC, "S6.1 dynamic exit parameters")
    text = replace_once(text, ANCHOR, INSERT, "inside-exit-zone entry block")
    TARGET.write_text(text, encoding="utf-8")
    print(f"patched {TARGET}")
    print("next: python -m py_compile btc5m_strategy/arb/btc5m_live_s6_safe.py btc5m_strategy/arb/s6_safety.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
