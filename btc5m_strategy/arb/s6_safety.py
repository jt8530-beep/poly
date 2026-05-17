"""S6/S6.1 safety helpers for BTC5M strategy.

Pure helper functions only. No credentials. No live order submission.
"""
from __future__ import annotations


def calibrated_fair(raw_fair: float, shrink_to_half: float = 0.05) -> float:
    shrink = min(max(float(shrink_to_half), 0.0), 0.50)
    out = float(raw_fair) * (1.0 - shrink) + 0.5 * shrink
    return min(max(out, 0.02), 0.98)


def effective_edge_bps(
    raw_edge_bps: int,
    spread_pct: float,
    ask_size: float,
    seconds_left: float,
    *,
    spread_penalty_mult: float = 0.8,
    depth_penalty_min_shares: float = 35.0,
    depth_penalty_max_bps: int = 250,
    late_entry_sec: int = 120,
    late_entry_penalty_bps: int = 200,
    model_error_buffer_bps: int = 150,
) -> tuple[int, dict]:
    spread_penalty = int(float(spread_pct) * 10_000 * float(spread_penalty_mult))
    depth_penalty = 0
    if depth_penalty_min_shares > 0 and ask_size < depth_penalty_min_shares:
        depth_penalty = int((depth_penalty_min_shares - ask_size) / depth_penalty_min_shares * depth_penalty_max_bps)
    time_penalty = int(late_entry_penalty_bps) if seconds_left < late_entry_sec else 0
    effective = int(raw_edge_bps) - spread_penalty - depth_penalty - time_penalty - int(model_error_buffer_bps)
    parts = {
        "raw_edge_bps": int(raw_edge_bps),
        "spread_penalty_bps": spread_penalty,
        "depth_penalty_bps": depth_penalty,
        "time_penalty_bps": time_penalty,
        "model_error_bps": int(model_error_buffer_bps),
        "effective_edge_bps": effective,
    }
    return effective, parts


def shock_filter_reason(
    closes: list[float],
    *,
    max_1m_move_bps: float = 35.0,
    vol_spike_mult: float = 3.0,
    min_spike_move_bps: float = 20.0,
) -> tuple[str | None, dict]:
    if len(closes) < 3:
        return None, {"closed_1m_count": len(closes)}
    moves = [(closes[i] / closes[i - 1] - 1.0) * 10_000 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not moves:
        return None, {"closed_1m_count": len(closes)}
    latest = moves[-1]
    abs_latest = abs(latest)
    avg_abs = sum(abs(x) for x in moves[:-1] or moves) / max(1, len(moves[:-1] or moves))
    stats = {"latest_1m_move_bps": latest, "avg_abs_1m_move_bps": avg_abs, "moves_bps": moves[-5:]}
    if abs_latest > max_1m_move_bps:
        return "shock_skip", stats
    if avg_abs > 0 and abs_latest > avg_abs * vol_spike_mult and abs_latest > min_spike_move_bps:
        return "vol_spike_skip", stats
    return None, stats


def entry_exit_zone_reason(
    seconds_left: float,
    *,
    dynamic_exit_enabled: bool = True,
    exit1_sec: int = 120,
    buffer_sec: int = 20,
) -> tuple[str | None, dict]:
    """Block new entries that would start inside, or too close to, the exit zone.

    S6.0 allowed entries with MIN_SECONDS_BEFORE_END=90 while dynamic exits began
    at 120 seconds. That can open a position directly inside the loss-management
    zone and then close it seconds later. S6.1 makes this impossible even when
    environment variables are misconfigured.
    """
    threshold = int(exit1_sec) + int(buffer_sec)
    payload = {
        "seconds_left": float(seconds_left),
        "dynamic_exit_enabled": bool(dynamic_exit_enabled),
        "exit1_sec": int(exit1_sec),
        "buffer_sec": int(buffer_sec),
        "entry_block_threshold_sec": threshold,
    }
    if dynamic_exit_enabled and float(seconds_left) <= threshold:
        return "inside_exit_zone", payload
    return None, payload


def dynamic_exit_reason(
    seconds_left: float,
    pnl_usd: float,
    notional_usd: float,
    bid_size: float | None,
    *,
    min_exit_bid_depth: float = 25.0,
    exit1_sec: int = 120,
    exit1_min_loss: float = 0.25,
    exit2_sec: int = 60,
    exit2_min_loss: float = 0.12,
    exit3_sec: int = 30,
) -> tuple[str | None, dict]:
    bid_depth_ok = bid_size is not None and float(bid_size) >= float(min_exit_bid_depth)
    loss_frac = (-float(pnl_usd) / float(notional_usd)) if notional_usd > 0 and pnl_usd < 0 else 0.0
    payload = {
        "seconds_left": float(seconds_left),
        "pnl_usd": float(pnl_usd),
        "notional_usd": float(notional_usd),
        "loss_frac": loss_frac,
        "bid_size": bid_size,
        "min_exit_bid_depth": float(min_exit_bid_depth),
        "bid_depth_ok": bid_depth_ok,
        "exit1_sec": int(exit1_sec),
        "exit1_min_loss": float(exit1_min_loss),
        "exit2_sec": int(exit2_sec),
        "exit2_min_loss": float(exit2_min_loss),
        "exit3_sec": int(exit3_sec),
    }
    if seconds_left <= exit1_sec and loss_frac >= exit1_min_loss and bid_depth_ok:
        return "dynamic_loss_120", payload
    if seconds_left <= exit2_sec and loss_frac >= exit2_min_loss and bid_depth_ok:
        return "dynamic_loss_60", payload
    if seconds_left <= exit3_sec:
        return ("dynamic_tail_liquid" if bid_depth_ok else "liquidity_trapped"), payload
    return None, payload
