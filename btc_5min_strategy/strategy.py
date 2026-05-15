"""
Core trading strategy for Polymarket 5-min BTC Up/Down.

Strategy: "Momentum Market-Maker with Directional Tilt"

The idea:
1. We act primarily as a MAKER (post limit orders) to earn the +1.12% rebate.
2. We tilt our quotes directionally based on our signal (momentum + RSI + book).
3. We only cross the spread (take liquidity) when we have very high conviction.

This approach is designed for STABILITY:
- Maker rebates provide a baseline income (~1.12% per filled order)
- Directional tilt gives us an edge on which side gets filled
- Kelly sizing + half-Kelly keeps risk controlled
- Strict entry/exit timing avoids last-second chaos

Key Edge Sources:
- Maker rebate: +1.12% on every filled limit order
- Momentum alpha: BTC 2-min momentum is weakly predictive of 5-min direction
- Adverse selection avoidance: skip when vol is too high / signals are mixed
"""
from __future__ import annotations
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from enum import Enum

from signals import Signal, SignalEngine
import config

logger = logging.getLogger(__name__)


class Side(Enum):
    UP = "up"
    DOWN = "down"


class OrderType(Enum):
    MAKER = "maker"     # Limit order (post-only)
    TAKER = "taker"     # Market order (cross spread)


@dataclass
class TradeDecision:
    action: str             # "buy_up", "buy_down", "quote_both", "skip"
    side: Optional[Side]
    order_type: OrderType
    size_usd: float         # position size in USDC
    limit_price: Optional[float]  # for maker orders
    confidence: float
    reason: str


@dataclass
class Position:
    side: Side
    size: float
    entry_price: float
    entry_time: float
    market_id: str


class TradingStrategy:
    """
    Main strategy logic.

    Lifecycle per 5-min window:
    1. Window opens -> start collecting BTC price data
    2. After ENTRY_WINDOW_START seconds -> evaluate signal
    3. If edge > MIN_EDGE -> place order (maker preferred)
    4. Monitor fill + update signal
    5. At EXIT_DEADLINE -> close any open position
    6. Window resolves -> collect P&L
    """

    def __init__(self):
        self.signal_engine = SignalEngine(
            momentum_lookback=config.MOMENTUM_LOOKBACK_SECONDS,
            rsi_period=config.RSI_PERIOD,
            vol_lookback=config.VOLATILITY_LOOKBACK_SECONDS,
        )
        self.bankroll = config.BANKROLL
        self.daily_pnl = 0.0
        self.positions: list[Position] = []
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0

    def kelly_size(self, p_win: float, price: float) -> float:
        """
        Half-Kelly position sizing.

        For a binary market at price `price`, with estimated P(win) = p_win:
        - b = (1 - price) / price  (net odds)
        - f* = (p*b - q) / b
        - We use f*/2 (half-Kelly) for safety
        """
        if price <= 0.01 or price >= 0.99:
            return 0.0
        b = (1 - price) / price
        q = 1 - p_win
        f = (p_win * b - q) / b
        f = max(0.0, f) * 0.5  # half-Kelly

        # Apply position limit
        max_size = self.bankroll * config.MAX_POSITION_PCT
        size = self.bankroll * f
        return min(size, max_size)

    def should_trade(self, signal: Signal, elapsed_seconds: float, skip_daily_check: bool = False) -> bool:
        """Check if conditions allow trading."""
        # Time window check
        if elapsed_seconds < config.ENTRY_WINDOW_START:
            return False
        if elapsed_seconds > config.ENTRY_WINDOW_END:
            return False

        # Daily loss limit
        if not skip_daily_check and self.daily_pnl <= -self.bankroll * config.MAX_DAILY_LOSS_PCT:
            logger.warning("Daily loss limit reached. Stopping.")
            return False

        # Max concurrent positions
        if len(self.positions) >= config.MAX_CONCURRENT_POSITIONS:
            return False

        # Minimum edge requirement
        if signal.edge_vs_market < config.MIN_EDGE:
            return False

        # Skip in high-vol regime with low confidence
        if signal.vol_regime == "high" and signal.confidence < 0.5:
            logger.info("Skipping: high vol + low confidence")
            return False

        return True

    def decide(
        self,
        signal: Signal,
        elapsed_seconds: float,
        market_price_up: float,
        market_price_down: float,
        skip_daily_check: bool = False,
    ) -> TradeDecision:
        """
        Core decision logic. Returns what action to take.

        Strategy modes:
        A) HIGH CONVICTION (edge > 5%, confidence > 0.6):
           -> Take liquidity (market order) on the strong side
        B) MODERATE CONVICTION (edge 3-5%):
           -> Post limit order (maker) on the strong side, slightly better than market
        C) LOW CONVICTION:
           -> Skip or quote both sides tight for rebate farming
        """
        if not self.should_trade(signal, elapsed_seconds, skip_daily_check=skip_daily_check):
            return TradeDecision(
                action="skip", side=None, order_type=OrderType.MAKER,
                size_usd=0, limit_price=None, confidence=signal.confidence,
                reason="Conditions not met"
            )

        # Determine direction
        if signal.p_up > 0.5:
            side = Side.UP
            our_p = signal.p_up
            market_p = market_price_up
        else:
            side = Side.DOWN
            our_p = 1 - signal.p_up
            market_p = market_price_down

        edge = our_p - market_p
        size = self.kelly_size(our_p, market_p)

        if size < 0.50:  # minimum $0.50 trade
            return TradeDecision(
                action="skip", side=None, order_type=OrderType.MAKER,
                size_usd=0, limit_price=None, confidence=signal.confidence,
                reason=f"Size too small: ${size:.2f}"
            )

        # MODE A: High conviction -> aggressive taker
        if edge > 0.05 and signal.confidence > 0.6:
            return TradeDecision(
                action=f"buy_{side.value}",
                side=side,
                order_type=OrderType.TAKER,
                size_usd=size,
                limit_price=None,  # market order
                confidence=signal.confidence,
                reason=f"High conviction: edge={edge:.1%}, p={our_p:.3f} vs market={market_p:.3f}"
            )

        # MODE B: Moderate conviction -> post limit (maker)
        # We post slightly above current best bid to get filled
        # but still earn maker rebate
        limit_price = market_p - 0.01  # 1 cent improvement
        limit_price = max(0.01, min(0.99, limit_price))

        return TradeDecision(
            action=f"buy_{side.value}",
            side=side,
            order_type=OrderType.MAKER,
            size_usd=size,
            limit_price=limit_price,
            confidence=signal.confidence,
            reason=f"Maker post: edge={edge:.1%}, limit@{limit_price:.3f}"
        )

    def on_fill(self, side: Side, size: float, price: float, market_id: str):
        """Record a filled order."""
        self.positions.append(Position(
            side=side, size=size, entry_price=price,
            entry_time=time.time(), market_id=market_id,
        ))
        self.total_trades += 1
        logger.info(f"FILL: {side.value} ${size:.2f} @ {price:.3f}")

    def on_resolution(self, market_id: str, outcome_up: bool):
        """Handle market resolution."""
        to_remove = []
        for i, pos in enumerate(self.positions):
            if pos.market_id != market_id:
                continue

            won = (pos.side == Side.UP and outcome_up) or \
                  (pos.side == Side.DOWN and not outcome_up)

            if won:
                pnl = pos.size * (1 - pos.entry_price) / pos.entry_price
                # Add maker rebate
                pnl += pos.size * config.MAKER_REBATE
                self.winning_trades += 1
            else:
                pnl = -pos.size
                # Subtract taker fee (worst case)
                pnl -= pos.size * config.TAKER_FEE

            self.bankroll += pnl
            self.daily_pnl += pnl
            self.total_pnl += pnl
            to_remove.append(i)

            outcome_str = "WIN" if won else "LOSS"
            logger.info(
                f"RESOLVED {outcome_str}: {pos.side.value} "
                f"pnl=${pnl:+.2f} bankroll=${self.bankroll:.2f}"
            )

        for i in sorted(to_remove, reverse=True):
            self.positions.pop(i)

    def reset_daily(self):
        """Reset daily P&L tracker."""
        self.daily_pnl = 0.0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def stats(self) -> dict:
        return {
            'bankroll': self.bankroll,
            'total_pnl': self.total_pnl,
            'total_trades': self.total_trades,
            'win_rate': self.win_rate,
            'daily_pnl': self.daily_pnl,
            'open_positions': len(self.positions),
        }
