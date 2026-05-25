"""
Settled PnL math.

Polymarket binary markets resolve each YES token to $1 and the NO token to $0
(or vice versa). For a given (wallet, token_id) we replay buys/sells in time
order using a FIFO inventory, then settle any residual position at the
resolution value.

Inputs are normalized trade dicts; see schema in build_history.

PnL = realized_from_sells + settlement_value_of_residual − cost_of_residual
where settlement_value is:
  * resolved_won  → 1.0 * residual_size
  * resolved_lost → 0.0
  * unresolved    → mark with residual_mark (caller-supplied; default = avg cost)
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class TradeLot:
    size: float
    price: float


def fifo_pnl(trades: list[dict], *, resolved: bool | None, won: bool | None,
             residual_mark: float | None = None) -> dict:
    """
    trades: list of dicts with keys
        side    : "BUY" | "SELL"
        size    : float (shares, positive)
        price   : float in [0, 1]
        ts      : unix seconds (used only for ordering)
    resolved/won describe the *token's* outcome (not the side-specific outcome).

    Returns a dict with keys:
        realized, settlement, cost_residual, pnl, residual_size, n_buys, n_sells,
        gross_buy_size, gross_sell_size, gross_buy_cost, gross_sell_proceeds
    """
    sorted_t = sorted(trades, key=lambda t: float(t.get("ts") or 0.0))
    inv: list[TradeLot] = []
    realized = 0.0
    gross_buy_size = gross_sell_size = 0.0
    gross_buy_cost = gross_sell_proceeds = 0.0
    n_buys = n_sells = 0

    for t in sorted_t:
        side = (t.get("side") or "").upper()
        size = float(t.get("size") or 0.0)
        price = float(t.get("price") or 0.0)
        if size <= 0 or price < 0 or price > 1:
            continue
        if side == "BUY":
            inv.append(TradeLot(size, price))
            gross_buy_size += size
            gross_buy_cost += size * price
            n_buys += 1
        elif side == "SELL":
            remaining = size
            while remaining > 0 and inv:
                lot = inv[0]
                take = min(remaining, lot.size)
                realized += (price - lot.price) * take
                lot.size -= take
                remaining -= take
                if lot.size <= 1e-12:
                    inv.pop(0)
            # if remaining > 0 here, the wallet went short — Polymarket binary
            # tokens generally don't allow that. Treat as opening a new short
            # position by adding a negative lot at the sell price.
            if remaining > 0:
                inv.append(TradeLot(-remaining, price))
            gross_sell_size += size
            gross_sell_proceeds += size * price
            n_sells += 1

    residual_size = sum(l.size for l in inv)
    cost_residual = sum(l.size * l.price for l in inv)

    if resolved and won is True:
        settle = 1.0 * residual_size
    elif resolved and won is False:
        settle = 0.0
    else:
        # not resolved — mark to caller-supplied price; if None, mark at cost
        # so unresolved positions contribute 0 to PnL (neutral).
        if residual_mark is None and residual_size != 0:
            mark = (cost_residual / residual_size)
        else:
            mark = residual_mark or 0.0
        settle = mark * residual_size

    pnl = realized + settle - cost_residual
    return {
        "realized": realized,
        "settlement": settle,
        "cost_residual": cost_residual,
        "pnl": pnl,
        "residual_size": residual_size,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "gross_buy_size": gross_buy_size,
        "gross_sell_size": gross_sell_size,
        "gross_buy_cost": gross_buy_cost,
        "gross_sell_proceeds": gross_sell_proceeds,
    }


def equity_curve(per_market_pnl: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Given (settlement_ts, pnl) pairs, return cumulative equity curve."""
    curve: list[tuple[float, float]] = []
    eq = 0.0
    for ts, p in sorted(per_market_pnl, key=lambda x: x[0]):
        eq += p
        curve.append((ts, eq))
    return curve


def max_drawdown(curve: list[tuple[float, float]]) -> float:
    """Return absolute max drawdown (positive number, in same currency)."""
    peak = float("-inf")
    mdd = 0.0
    for _, eq in curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > mdd:
            mdd = dd
    return mdd
