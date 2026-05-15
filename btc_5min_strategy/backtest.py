"""
Backtest for the Polymarket 5-min BTC Up/Down strategy.

ALPHA MODEL:
The key edge in 5-min BTC markets comes from:
1. Information asymmetry: We have real-time BTC price, the Polymarket book 
   is 3-10 seconds stale (latency of human/bot updates)
2. Market microstructure: The market overprices "Down" after small dips
   (retail panic) and underprices "Up" during steady trends
3. Maker rebate: +1.12% on limit orders provides a baseline edge

This backtest models a realistic scenario where:
- Our BTC feed is instant
- The Polymarket market price lags 3-10 seconds
- We can estimate the "true" probability better due to this information edge
- The edge is small (1-5%) but consistent

Usage:
    python backtest.py
    python backtest.py --windows 5000 --regime medium
    python backtest.py --monte-carlo 200
"""
from __future__ import annotations
import argparse
import numpy as np
from dataclasses import dataclass
from typing import List
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


@dataclass
class BacktestResult:
    n_windows: int
    n_trades: int
    n_wins: int
    n_losses: int
    n_skipped: int
    win_rate: float
    total_pnl: float
    roi_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    final_bankroll: float
    equity_curve: List[float]
    avg_edge: float


def kelly_size(p_win: float, price: float, bankroll: float, max_pct: float = 0.05) -> float:
    """Half-Kelly sizing."""
    if price <= 0.01 or price >= 0.99 or p_win <= price:
        return 0.0
    b = (1 - price) / price
    q = 1 - p_win
    f = (p_win * b - q) / b
    f = max(0.0, f) * 0.5  # half-Kelly
    return min(bankroll * f, bankroll * max_pct)


def run_backtest(
    n_windows: int = 2000,
    regime: str = "medium",
    bankroll: float = 100.0,
    seed: int = 42,
    verbose: bool = False,
) -> BacktestResult:
    """
    Run backtest with realistic alpha model.
    
    The key insight: at any point during the 5-min window, the TRUE probability
    that BTC finishes above start depends on:
    - Current price vs start (move so far)
    - Time remaining
    - Volatility
    
    Our edge is that we see current price INSTANTLY while the market
    is stale by several seconds. This gives us a small but real edge.
    """
    rng = np.random.default_rng(seed)

    vol_map = {"low": 0.30, "medium": 0.55, "high": 0.90}
    ann_vol = vol_map[regime]
    sec_vol = ann_vol / np.sqrt(31_536_000)

    bankroll_current = bankroll
    equity_curve = [bankroll_current]
    pnl_list = []
    n_trades = 0
    n_wins = 0
    n_skipped = 0
    edges_captured = []

    btc_price = 104000.0

    for w in range(n_windows):
        # Generate full 300-second price path
        innovations = rng.standard_t(df=5, size=300) / np.sqrt(5/3)
        log_rets = sec_vol * innovations
        log_prices = np.concatenate([[0], np.cumsum(log_rets)])
        prices = btc_price * np.exp(log_prices)

        start_price = prices[0]
        end_price = prices[-1]
        outcome_up = end_price >= start_price

        # EVALUATION POINT: We look at the market 30-90 seconds in
        eval_sec = rng.integers(30, 90)
        current_price = prices[eval_sec]

        # ---- TRUE PROBABILITY calculation ----
        # Given current price at time t, what's P(end >= start)?
        # Under GBM: P(end >= start) = Phi(d) where
        # d = log(current/start) / (vol * sqrt(remaining_time))
        # This is our "oracle" estimate
        remaining = 300 - eval_sec
        vol_remaining = sec_vol * np.sqrt(remaining)
        if vol_remaining > 0:
            d = np.log(current_price / start_price) / vol_remaining
            from scipy.stats import norm
            p_true = norm.cdf(d)
        else:
            p_true = 1.0 if current_price >= start_price else 0.0
        p_true = float(np.clip(p_true, 0.05, 0.95))

        # ---- OUR ESTIMATE (slightly noisy vs true) ----
        our_noise = rng.normal(0, 0.02)  # 2% estimation error
        p_our = float(np.clip(p_true + our_noise, 0.05, 0.95))

        # ---- MARKET PRICE (lagged + noisy = less accurate) ----
        # Market sees price from 3-10 seconds ago
        lag = rng.integers(3, 12)
        lagged_price = prices[max(0, eval_sec - lag)]
        vol_remaining_market = sec_vol * np.sqrt(remaining + lag)
        if vol_remaining_market > 0:
            d_market = np.log(lagged_price / start_price) / vol_remaining_market
            from scipy.stats import norm
            p_market_base = norm.cdf(d_market)
        else:
            p_market_base = 0.5
        # Market also has extra noise (inefficiency)
        market_noise = rng.normal(0, 0.04)  # 4% market noise
        p_market = float(np.clip(p_market_base + market_noise, 0.15, 0.85))
        # Market is "sticky" near 50 early on
        stickiness = max(0, 0.2 - eval_sec / 300)
        p_market = p_market * (1 - stickiness) + 0.5 * stickiness

        # ---- DECISION ----
        # Our edge = our estimate - market price
        if p_our > 0.5:
            side_up = True
            our_p_win = p_our
            market_price = p_market
        else:
            side_up = False
            our_p_win = 1 - p_our
            market_price = 1 - p_market

        edge = our_p_win - market_price

        # Only trade if edge > threshold
        min_edge = config.MIN_EDGE
        if edge < min_edge:
            n_skipped += 1
            equity_curve.append(bankroll_current)
            btc_price = float(end_price)
            continue

        # High vol regime: require more edge
        if ann_vol > 0.7 and edge < min_edge * 1.5:
            n_skipped += 1
            equity_curve.append(bankroll_current)
            btc_price = float(end_price)
            continue

        # Size with half-Kelly
        size = kelly_size(our_p_win, market_price, bankroll_current, config.MAX_POSITION_PCT)
        if size < 0.50:
            n_skipped += 1
            equity_curve.append(bankroll_current)
            btc_price = float(end_price)
            continue

        # Maker fill simulation (75% fill rate for limit orders)
        is_maker = rng.random() < 0.75
        if not is_maker and rng.random() > 0.85:
            # Taker order that doesn't fill immediately (rare)
            n_skipped += 1
            equity_curve.append(bankroll_current)
            btc_price = float(end_price)
            continue

        # ---- RESOLVE ----
        n_trades += 1
        edges_captured.append(edge)

        won = (side_up and outcome_up) or (not side_up and not outcome_up)

        if won:
            pnl = size * (1 - market_price) / market_price
            n_wins += 1
        else:
            pnl = -size

        # Fee adjustment
        if is_maker:
            pnl += size * config.MAKER_REBATE  # +1.12% rebate
        else:
            pnl -= size * config.TAKER_FEE     # -1.12% fee

        bankroll_current += pnl
        pnl_list.append(pnl)
        equity_curve.append(bankroll_current)

        if verbose and w % 500 == 0 and w > 0:
            wr = n_wins / n_trades if n_trades > 0 else 0
            print(f"  [{w}/{n_windows}] bankroll=${bankroll_current:.2f} "
                  f"trades={n_trades} wr={wr:.1%} edge={np.mean(edges_captured):.3f}")

        btc_price = float(end_price)
        if bankroll_current <= 1.0:
            if verbose:
                print(f"  RUIN at window {w}")
            break

    # ---- METRICS ----
    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / np.where(peak > 0, peak, 1)
    max_dd = float(np.max(drawdown))

    n_losses = n_trades - n_wins
    win_rate = n_wins / n_trades if n_trades > 0 else 0

    if len(pnl_list) > 1:
        returns = np.array(pnl_list) / bankroll
        sharpe = float(np.mean(returns) / (np.std(returns) + 1e-10)) * np.sqrt(len(pnl_list))
    else:
        sharpe = 0.0

    gross_profit = sum(p for p in pnl_list if p > 0)
    gross_loss = abs(sum(p for p in pnl_list if p < 0))
    profit_factor = gross_profit / (gross_loss + 1e-10)

    avg_edge = float(np.mean(edges_captured)) if edges_captured else 0

    return BacktestResult(
        n_windows=n_windows,
        n_trades=n_trades,
        n_wins=n_wins,
        n_losses=n_losses,
        n_skipped=n_skipped,
        win_rate=win_rate,
        total_pnl=sum(pnl_list),
        roi_pct=(bankroll_current / bankroll - 1) * 100,
        max_drawdown_pct=max_dd * 100,
        sharpe_ratio=sharpe,
        profit_factor=profit_factor,
        final_bankroll=bankroll_current,
        equity_curve=equity_curve,
        avg_edge=avg_edge,
    )


def monte_carlo(n_runs: int, n_windows: int, regime: str, bankroll: float) -> dict:
    results = []
    for seed in range(n_runs):
        r = run_backtest(n_windows=n_windows, regime=regime, bankroll=bankroll, seed=seed)
        results.append(r)

    finals = np.array([r.final_bankroll for r in results])
    rois = np.array([r.roi_pct for r in results])

    return {
        'n_runs': n_runs,
        'regime': regime,
        'median_final': float(np.median(finals)),
        'mean_final': float(np.mean(finals)),
        'p10': float(np.percentile(finals, 10)),
        'p25': float(np.percentile(finals, 25)),
        'p75': float(np.percentile(finals, 75)),
        'p90': float(np.percentile(finals, 90)),
        'median_roi': float(np.median(rois)),
        'prob_profit': float(np.mean(finals > bankroll)),
        'prob_ruin': float(np.mean(finals < bankroll * 0.1)),
        'median_sharpe': float(np.median([r.sharpe_ratio for r in results])),
        'median_wr': float(np.median([r.win_rate for r in results])),
        'median_dd': float(np.median([r.max_drawdown_pct for r in results])),
        'avg_trades': float(np.mean([r.n_trades for r in results])),
        'median_pf': float(np.median([r.profit_factor for r in results])),
        'avg_edge': float(np.mean([r.avg_edge for r in results])),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--windows', type=int, default=2000)
    parser.add_argument('--regime', choices=['low', 'medium', 'high'], default='medium')
    parser.add_argument('--bankroll', type=float, default=100.0)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--monte-carlo', type=int, default=0)
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    print("=" * 60)
    print("  Polymarket 5-Min BTC Strategy Backtest")
    print("  Alpha: Information lag + Market inefficiency + Maker rebate")
    print("=" * 60)

    if args.monte_carlo > 0:
        print(f"\n  Monte Carlo: {args.monte_carlo} runs x {args.windows} windows")
        print(f"  Regime: {args.regime} | Bankroll: ${args.bankroll}")
        print()

        mc = monte_carlo(args.monte_carlo, args.windows, args.regime, args.bankroll)

        print(f"  {'Metric':<25} {'Value':>12}")
        print(f"  {'-'*40}")
        print(f"  {'Median Final Bankroll':<25} ${mc['median_final']:>10.2f}")
        print(f"  {'Mean Final Bankroll':<25} ${mc['mean_final']:>10.2f}")
        print(f"  {'P10 / P25 / P75 / P90':<25} ${mc['p10']:.0f}/${mc['p25']:.0f}/${mc['p75']:.0f}/${mc['p90']:.0f}")
        print(f"  {'Median ROI':<25} {mc['median_roi']:>+10.1f}%")
        print(f"  {'P(Profit)':<25} {mc['prob_profit']:>10.1%}")
        print(f"  {'P(Ruin <10%)':<25} {mc['prob_ruin']:>10.1%}")
        print(f"  {'Median Sharpe':<25} {mc['median_sharpe']:>10.2f}")
        print(f"  {'Median Win Rate':<25} {mc['median_wr']:>10.1%}")
        print(f"  {'Median Max Drawdown':<25} {mc['median_dd']:>10.1f}%")
        print(f"  {'Avg Trades/Run':<25} {mc['avg_trades']:>10.0f}")
        print(f"  {'Median Profit Factor':<25} {mc['median_pf']:>10.2f}")
        print(f"  {'Avg Edge Captured':<25} {mc['avg_edge']:>10.3f}")

        # Regime comparison
        print(f"\n  --- Regime Comparison (50 runs each, {args.windows} windows) ---")
        print(f"  {'Regime':<8} {'ROI':>8} {'P(Win)':>8} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>8} {'Edge':>7}")
        for reg in ['low', 'medium', 'high']:
            mc_r = monte_carlo(50, args.windows, reg, args.bankroll)
            print(f"  {reg:<8} {mc_r['median_roi']:>+7.1f}% "
                  f"{mc_r['prob_profit']:>7.0%} "
                  f"{mc_r['median_sharpe']:>8.2f} "
                  f"{mc_r['median_dd']:>7.1f}% "
                  f"{mc_r['avg_trades']:>7.0f} "
                  f"{mc_r['avg_edge']:>6.3f}")
    else:
        print(f"\n  Single run: {args.windows} windows, regime={args.regime}, seed={args.seed}")
        r = run_backtest(
            n_windows=args.windows, regime=args.regime,
            bankroll=args.bankroll, seed=args.seed, verbose=args.verbose,
        )
        print(f"\n  {'Metric':<25} {'Value':>12}")
        print(f"  {'-'*40}")
        print(f"  {'Windows':<25} {r.n_windows:>12}")
        print(f"  {'Trades':<25} {r.n_trades:>12}")
        print(f"  {'Skipped':<25} {r.n_skipped:>12}")
        print(f"  {'Trade Rate':<25} {r.n_trades/r.n_windows:>11.1%}")
        print(f"  {'Wins / Losses':<25} {r.n_wins:>5} / {r.n_losses}")
        print(f"  {'Win Rate':<25} {r.win_rate:>11.1%}")
        print(f"  {'Avg Edge':<25} {r.avg_edge:>11.3f}")
        print(f"  {'Profit Factor':<25} {r.profit_factor:>12.2f}")
        print(f"  {'Total PnL':<25} ${r.total_pnl:>+10.2f}")
        print(f"  {'ROI':<25} {r.roi_pct:>+10.1f}%")
        print(f"  {'Final Bankroll':<25} ${r.final_bankroll:>10.2f}")
        print(f"  {'Max Drawdown':<25} {r.max_drawdown_pct:>10.1f}%")
        print(f"  {'Sharpe Ratio':<25} {r.sharpe_ratio:>12.2f}")

    print("\n" + "=" * 60)


if __name__ == '__main__':
    main()
