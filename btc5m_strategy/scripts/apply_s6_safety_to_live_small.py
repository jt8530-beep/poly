"""Inject S6 safety imports and edge filtering into btc5m_live_small.py.

This patch does not change live switches. It only makes the existing runner more
conservative when BTC5M_USE_EFFECTIVE_EDGE=1.

Run from repository root:

    python btc5m_strategy/scripts/apply_s6_safety_to_live_small.py

Review the diff before running any service.
"""
from __future__ import annotations

from pathlib import Path

TARGET = Path("btc5m_strategy/arb/btc5m_live_small.py")

IMPORT_MARKER = "from .tg_notifier import Notifier\n"
IMPORT_INSERT = "from .s6_safety import calibrated_fair, effective_edge_bps, shock_filter_reason\n"

FAIR_OLD = '''    fair_up = _fair_probability(start_price, spot, seconds_left, vol_1m)
    fair_down = 1.0 - fair_up
'''
FAIR_NEW = '''    fair_up_raw = _fair_probability(start_price, spot, seconds_left, vol_1m)
    fair_down_raw = 1.0 - fair_up_raw
    fair_up = calibrated_fair(fair_up_raw, _env_f("BTC5M_FAIR_SHRINK_TO_HALF", 0.05))
    fair_down = calibrated_fair(fair_down_raw, _env_f("BTC5M_FAIR_SHRINK_TO_HALF", 0.05))
'''

EDGE_OLD = '''    min_edge = _env_i("BTC5M_MIN_EDGE_BPS", 500)
    max_spread = _env_f("BTC5M_MAX_SPREAD_PCT", 0.03)
    max_entry = _env_f("BTC5M_MAX_ENTRY_PRICE", 0.82)
    min_depth = _env_f("BTC5M_MIN_ASK_DEPTH_SHARES", 10.0)
    max_notional = _env_f("BTC5M_MAX_NOTIONAL_USD", 5.0)
    if int(best["edge_bps"]) < min_edge:
        summary["reason"] = "edge_too_small"
    elif float(best["spread_pct"]) > max_spread:
'''
EDGE_NEW = '''    min_edge = _env_i("BTC5M_MIN_EDGE_BPS", 500)
    effective_edge, edge_parts = effective_edge_bps(
        int(best["edge_bps"]),
        float(best["spread_pct"]),
        float(best["ask_size"]),
        float(seconds_left),
        spread_penalty_mult=_env_f("BTC5M_SPREAD_EDGE_PENALTY_MULT", 0.8),
        depth_penalty_min_shares=_env_f("BTC5M_DEPTH_PENALTY_MIN_SHARES", 35.0),
        depth_penalty_max_bps=_env_i("BTC5M_DEPTH_PENALTY_MAX_BPS", 250),
        late_entry_sec=_env_i("BTC5M_LATE_ENTRY_SEC", 120),
        late_entry_penalty_bps=_env_i("BTC5M_LATE_ENTRY_EDGE_PENALTY_BPS", 200),
        model_error_buffer_bps=_env_i("BTC5M_MODEL_ERROR_BUFFER_BPS", 150),
    )
    summary["best_effective_edge_bps"] = effective_edge
    summary["edge_parts"] = edge_parts
    max_spread = _env_f("BTC5M_MAX_SPREAD_PCT", 0.03)
    max_entry = _env_f("BTC5M_MAX_ENTRY_PRICE", 0.82)
    min_depth = _env_f("BTC5M_MIN_ASK_DEPTH_SHARES", 10.0)
    max_notional = _env_f("BTC5M_MAX_NOTIONAL_USD", 5.0)
    if int(best["edge_bps"]) < min_edge:
        summary["reason"] = "edge_too_small"
    elif _env_b("BTC5M_USE_EFFECTIVE_EDGE", True) and effective_edge < _env_i("BTC5M_MIN_EFFECTIVE_EDGE_BPS", 500):
        summary["reason"] = "effective_edge_too_small"
    elif float(best["spread_pct"]) > max_spread:
'''

RESPONSE_OLD = '''                "max_price": best["ask"],
            }
'''
RESPONSE_NEW = '''                "max_price": best["ask"],
                "effective_edge_bps": summary.get("best_effective_edge_bps"),
                "edge_parts": summary.get("edge_parts"),
            }
'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new.strip() in text:
        print(f"already patched: {label}")
        return text
    if old not in text:
        raise SystemExit(f"target block not found: {label}")
    return text.replace(old, new, 1)


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    if IMPORT_INSERT not in text:
        if IMPORT_MARKER not in text:
            raise SystemExit("import marker not found")
        text = text.replace(IMPORT_MARKER, IMPORT_MARKER + IMPORT_INSERT, 1)
    text = replace_once(text, FAIR_OLD, FAIR_NEW, "fair calibration")
    text = replace_once(text, EDGE_OLD, EDGE_NEW, "effective edge gate")
    text = replace_once(text, RESPONSE_OLD, RESPONSE_NEW, "response edge metadata")
    TARGET.write_text(text, encoding="utf-8")
    print(f"patched {TARGET} with S6 safety gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
