"""
Honest reimplementation of the "Polymarket Quant Framework" from the X/Twitter post.

We implement ALL four pieces shown in the image:
  1. EV = P_win * profit - P_loss * stake
  2. Kelly:  f* = (p*b - q) / b     where b = (1 - price) / price,   q = 1 - p
  3. Bayesian update:  P(A|B) = P(B|A) P(A) / P(B)
  4. Maker/taker edge: maker earns +1.12%, taker pays -1.12%

Then we simulate a bot that follows this framework EXACTLY, under realistic assumptions:
  - Your probability estimate `p_hat` is the true probability `p` plus noise.
  - Trades resolve binary (win/loss) at contract price 0 or 1.
  - Optional transaction costs / fees.

What it demonstrates:
  * Kelly with edge e and odds b has GROWTH RATE ~= e^2 / (2 * b * (1-b))    per bet.
  * Even with a realistic edge of a few %, turning 1200 -> 797k in 49 days
    would require either (a) ~14% compound daily return or (b) huge leverage /
    60 bets per day with consistent >5% per-bet edge. Neither survives noise.

Usage:
    python polymarket_kelly_backtest.py
    python polymarket_kelly_backtest.py --sigma 0.04 --n_bets 2000 --kelly_frac 0.5

No external deps beyond numpy/matplotlib (matplotlib optional).
"""
from __future__ import annotations
import argparse
import math
import numpy as np


# ------------------------------------------------------------------
# 1. EV
# ------------------------------------------------------------------
def expected_value(p_win: float, price: float, stake: float) -> float:
    """EV of buying `stake` dollars of a YES contract at `price` (0..1).

    profit_if_win = stake * (1/price - 1) = stake * (1-price)/price
    loss_if_lose  = stake
    """
    profit = stake * (1.0 - price) / price
    return p_win * profit - (1.0 - p_win) * stake


# ------------------------------------------------------------------
# 2. Kelly
# ------------------------------------------------------------------
def kelly_fraction(p: float, price: float) -> float:
    """Full-Kelly fraction of bankroll to wager on a YES contract.

    b = net odds = (1 - price) / price
    q = 1 - p
    f* = (p * b - q) / b
    """
    if price <= 0 or price >= 1:
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - p
    f = (p * b - q) / b
    return max(0.0, f)   # never short; never bet negative


# ------------------------------------------------------------------
# 3. Bayesian update of prior p given a noisy signal
# ------------------------------------------------------------------
def bayes_update(prior: float, likelihood_if_true: float, likelihood_if_false: float) -> float:
    """P(A|B) = P(B|A) P(A) / P(B)."""
    num = likelihood_if_true * prior
    den = num + likelihood_if_false * (1.0 - prior)
    if den == 0: return prior
    return num / den


# ------------------------------------------------------------------
# 4. Maker/taker edge adjustment on realized PnL
# ------------------------------------------------------------------
def apply_fee(pnl: float, stake: float, is_maker: bool, rate: float = 0.0112) -> float:
    """Polymarket-style: maker +rate, taker -rate (applied to stake)."""
    return pnl + (rate if is_maker else -rate) * stake


# ------------------------------------------------------------------
# Simulation
# ------------------------------------------------------------------
def simulate(
    n_bets: int = 1000,
    bankroll0: float = 1200.0,
    sigma: float = 0.04,           # std-dev of your probability estimate error
    kelly_frac: float = 0.5,       # fractional-Kelly (0.5 = half-Kelly)
    min_edge: float = 0.02,        # only take bets where p_hat - price >= min_edge
    maker_share: float = 0.65,     # fraction of trades filled as maker (post)
    fee_rate: float = 0.0112,      # Polymarket LP rebate / taker fee
    price_range=(0.05, 0.95),
    seed: int = 0,
):
    rng = np.random.default_rng(seed)
    bankroll = bankroll0
    equity_curve = [bankroll]
    wins = losses = skipped = 0
    total_pnl = 0.0
    gross_vol = 0.0

    for i in range(n_bets):
        # market price = implied prob, drawn uniformly
        price = rng.uniform(*price_range)
        # true probability: on average the market is efficient
        p_true = price + rng.normal(0, 0.03)     # tiny market mispricing
        p_true = float(np.clip(p_true, 0.01, 0.99))
        # your estimate: noisy around truth
        p_hat = p_true + rng.normal(0, sigma)
        p_hat = float(np.clip(p_hat, 0.01, 0.99))

        edge = p_hat - price
        # skip if no edge
        if edge < min_edge:
            skipped += 1
            equity_curve.append(bankroll)
            continue

        f = kelly_fraction(p_hat, price) * kelly_frac
        stake = bankroll * f
        if stake < 1e-6:
            skipped += 1; equity_curve.append(bankroll); continue

        # resolve
        win = rng.random() < p_true
        if win:
            pnl = stake * (1.0 - price) / price
            wins += 1
        else:
            pnl = -stake
            losses += 1

        is_maker = rng.random() < maker_share
        pnl = apply_fee(pnl, stake, is_maker, fee_rate)

        bankroll += pnl
        total_pnl += pnl
        gross_vol += stake
        equity_curve.append(bankroll)
        if bankroll <= 0:
            print(f"RUIN at bet {i}")
            break

    return {
        'bankroll0': bankroll0,
        'bankroll_final': bankroll,
        'roi_pct': (bankroll/bankroll0 - 1) * 100,
        'wins': wins, 'losses': losses, 'skipped': skipped,
        'win_rate': wins / max(1, wins+losses),
        'total_pnl': total_pnl,
        'gross_volume': gross_vol,
        'equity_curve': equity_curve,
    }


def multi_run(n_runs=200, **kw):
    finals = []
    for seed in range(n_runs):
        r = simulate(seed=seed, **kw)
        finals.append(r['bankroll_final'])
    finals = np.array(finals)
    start = kw.get('bankroll0', 1200.0)
    return {
        'median_final': float(np.median(finals)),
        'mean_final':   float(np.mean(finals)),
        'p05':  float(np.percentile(finals, 5)),
        'p95':  float(np.percentile(finals, 95)),
        'prob_ruin': float(np.mean(finals <= 0.01 * start)),
        'prob_10x':  float(np.mean(finals >= 10 * start)),
        'prob_100x': float(np.mean(finals >= 100 * start)),
        'prob_664x': float(np.mean(finals >= 664 * start)),   # the Tweet's claim
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n_bets',  type=int,   default=1000)
    ap.add_argument('--sigma',   type=float, default=0.04,
                    help='std-dev of your probability estimate error (lower=more edge)')
    ap.add_argument('--kelly_frac', type=float, default=0.5)
    ap.add_argument('--bankroll', type=float, default=1200.0)
    ap.add_argument('--n_runs',  type=int,   default=500)
    ap.add_argument('--plot',    action='store_true')
    args = ap.parse_args()

    print(f"=== honest Polymarket-Kelly backtest ===")
    print(f"bets={args.n_bets}  starting bankroll=${args.bankroll}  "
          f"sigma={args.sigma}  kelly_frac={args.kelly_frac}  n_runs={args.n_runs}\n")

    # single illustrative run
    r = simulate(n_bets=args.n_bets, bankroll0=args.bankroll,
                 sigma=args.sigma, kelly_frac=args.kelly_frac, seed=1)
    print(f"[single run, seed=1]")
    print(f"  final bankroll  : ${r['bankroll_final']:,.2f}")
    print(f"  ROI             : {r['roi_pct']:+.1f} %")
    print(f"  wins/losses     : {r['wins']} / {r['losses']}  "
          f"(win rate {100*r['win_rate']:.1f}%)")
    print(f"  skipped (no edge): {r['skipped']}")
    print(f"  gross volume    : ${r['gross_volume']:,.2f}\n")

    # monte carlo
    mc = multi_run(n_runs=args.n_runs, n_bets=args.n_bets,
                   bankroll0=args.bankroll, sigma=args.sigma,
                   kelly_frac=args.kelly_frac)
    print(f"[monte carlo over {args.n_runs} runs]")
    print(f"  median final    : ${mc['median_final']:,.2f}")
    print(f"  mean   final    : ${mc['mean_final']:,.2f}")
    print(f"  p05 .. p95      : ${mc['p05']:,.2f} .. ${mc['p95']:,.2f}")
    print(f"  P(lose >99%)    : {100*mc['prob_ruin']:.1f} %")
    print(f"  P(>=10x)        : {100*mc['prob_10x']:.2f} %")
    print(f"  P(>=100x)       : {100*mc['prob_100x']:.3f} %")
    print(f"  P(>=664x  as in the Tweet) : {100*mc['prob_664x']:.4f} %")

    if args.plot:
        import matplotlib.pyplot as plt
        plt.plot(r['equity_curve']); plt.yscale('log')
        plt.title('honest Kelly backtest - equity curve')
        plt.xlabel('bet #'); plt.ylabel('bankroll $'); plt.grid(True)
        plt.savefig('equity_curve.png', dpi=110); print("saved equity_curve.png")


if __name__ == '__main__':
    main()
