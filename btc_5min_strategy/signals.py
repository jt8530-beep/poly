"""
Signal generation for 5-min BTC Up/Down market.

Combines multiple signals into a single probability estimate:
1. Short-term momentum (2-min price slope)
2. RSI on micro-candles
3. Order book imbalance (Polymarket)
4. Volatility regime (adjust confidence)
5. VWAP deviation

The final output is P(Up) in [0, 1].
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional
import time


@dataclass
class PriceTick:
    timestamp: float    # unix epoch
    price: float
    volume: float = 0.0


@dataclass
class Signal:
    """Composite signal output."""
    p_up: float             # estimated probability BTC goes Up in this 5-min window
    confidence: float       # 0-1, how confident we are in the estimate
    momentum_score: float   # -1 to +1
    rsi: float              # 0-100
    vol_regime: str         # "low", "medium", "high"
    book_imbalance: float   # -1 to +1 (positive = more bids)
    timestamp: float = 0.0

    @property
    def edge_vs_market(self) -> float:
        """Edge vs 50/50 fair value (unsigned)."""
        return abs(self.p_up - 0.5)


class SignalEngine:
    """
    Generates trading signals from BTC price stream and Polymarket orderbook.
    """

    def __init__(
        self,
        momentum_lookback: int = 120,     # seconds
        rsi_period: int = 14,
        vol_lookback: int = 300,
        candle_interval: int = 5,         # 5-second micro-candles
    ):
        self.momentum_lookback = momentum_lookback
        self.rsi_period = rsi_period
        self.vol_lookback = vol_lookback
        self.candle_interval = candle_interval

        self.ticks: List[PriceTick] = []
        self.candles: List[dict] = []  # {open, high, low, close, volume, ts}
        self._last_candle_ts: float = 0

    def add_tick(self, tick: PriceTick):
        """Add a new price tick."""
        self.ticks.append(tick)
        # Keep only last 10 minutes of ticks
        cutoff = tick.timestamp - 600
        self.ticks = [t for t in self.ticks if t.timestamp >= cutoff]
        self._update_candles(tick)

    def _update_candles(self, tick: PriceTick):
        """Aggregate ticks into micro-candles."""
        candle_ts = int(tick.timestamp // self.candle_interval) * self.candle_interval
        if candle_ts > self._last_candle_ts:
            # New candle
            self.candles.append({
                'open': tick.price,
                'high': tick.price,
                'low': tick.price,
                'close': tick.price,
                'volume': tick.volume,
                'ts': candle_ts,
            })
            self._last_candle_ts = candle_ts
            # Keep last 120 candles (10 min of 5s candles)
            if len(self.candles) > 120:
                self.candles = self.candles[-120:]
        else:
            # Update current candle
            c = self.candles[-1]
            c['high'] = max(c['high'], tick.price)
            c['low'] = min(c['low'], tick.price)
            c['close'] = tick.price
            c['volume'] += tick.volume

    def compute_momentum(self) -> float:
        """
        Linear regression slope of price over momentum_lookback window.
        Returns normalized score in [-1, 1].
        """
        now = time.time()
        cutoff = now - self.momentum_lookback
        recent = [t for t in self.ticks if t.timestamp >= cutoff]
        if len(recent) < 10:
            return 0.0

        prices = np.array([t.price for t in recent])
        times = np.array([t.timestamp - recent[0].timestamp for t in recent])

        # Linear regression
        if times[-1] == 0:
            return 0.0
        slope = np.polyfit(times, prices, 1)[0]

        # Normalize: slope per second, relative to price
        avg_price = np.mean(prices)
        if avg_price == 0:
            return 0.0
        norm_slope = (slope / avg_price) * 60  # % change per minute

        # Clip to [-1, 1] (±0.5% per minute saturates)
        return float(np.clip(norm_slope / 0.005, -1.0, 1.0))

    def compute_rsi(self) -> float:
        """RSI on micro-candle closes."""
        if len(self.candles) < self.rsi_period + 1:
            return 50.0  # neutral

        closes = np.array([c['close'] for c in self.candles[-(self.rsi_period + 1):]])
        deltas = np.diff(closes)

        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if len(losses) > 0 else 1e-10

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100 - 100 / (1 + rs))

    def compute_volatility_regime(self) -> tuple[str, float]:
        """
        Estimate current volatility regime.
        Returns (regime_label, annualized_vol).
        """
        now = time.time()
        cutoff = now - self.vol_lookback
        recent = [t for t in self.ticks if t.timestamp >= cutoff]
        if len(recent) < 20:
            return "medium", 0.0

        prices = np.array([t.price for t in recent])
        returns = np.diff(np.log(prices))
        if len(returns) == 0:
            return "medium", 0.0

        # Average tick interval
        avg_dt = (recent[-1].timestamp - recent[0].timestamp) / len(returns)
        if avg_dt <= 0:
            avg_dt = 1.0

        # Annualize (seconds per year ~31.5M)
        vol_per_sec = float(np.std(returns)) / np.sqrt(avg_dt)
        ann_vol = vol_per_sec * np.sqrt(31_536_000)

        if ann_vol < 0.30:
            regime = "low"
        elif ann_vol < 0.70:
            regime = "medium"
        else:
            regime = "high"

        return regime, float(ann_vol)

    def compute_book_imbalance(self, bids: List[tuple], asks: List[tuple]) -> float:
        """
        Order book imbalance from Polymarket CLOB.
        bids/asks: list of (price, size) tuples.
        Returns score in [-1, 1]: positive = more bid pressure (bullish).
        """
        if not bids and not asks:
            return 0.0

        bid_depth = sum(size for _, size in bids[:5])  # top 5 levels
        ask_depth = sum(size for _, size in asks[:5])
        total = bid_depth + ask_depth

        if total == 0:
            return 0.0
        return float((bid_depth - ask_depth) / total)

    def generate_signal(
        self,
        market_price_up: float = 0.50,     # current Polymarket price for "Up"
        bids: Optional[List[tuple]] = None,
        asks: Optional[List[tuple]] = None,
    ) -> Signal:
        """
        Combine all sub-signals into a final P(Up) estimate.

        Weighting scheme:
        - Momentum:       40%  (strongest short-term predictor)
        - RSI:            20%  (mean-reversion at extremes)
        - Book imbalance: 20%  (market microstructure)
        - Market price:   20%  (respect market wisdom)
        """
        momentum = self.compute_momentum()
        rsi = self.compute_rsi()
        vol_regime, ann_vol = self.compute_volatility_regime()
        book_imb = self.compute_book_imbalance(bids or [], asks or [])

        # Convert RSI to directional score [-1, 1]
        # RSI > 70 -> bearish signal, RSI < 30 -> bullish signal
        rsi_score = -(rsi - 50) / 50.0  # inverted: high RSI = overbought = down
        rsi_score = np.clip(rsi_score, -1, 1)

        # Weighted combination -> raw score in [-1, 1]
        raw_score = (
            0.40 * momentum +
            0.20 * rsi_score +
            0.20 * book_imb +
            0.20 * (market_price_up - 0.5) * 2  # market price mapped to [-1,1]
        )

        # Convert to probability
        # Use sigmoid-like mapping: p_up = 0.5 + 0.5 * tanh(raw_score * scale)
        scale = 2.0  # controls how aggressive the mapping is
        p_up = 0.5 + 0.5 * np.tanh(raw_score * scale)

        # Confidence: lower in high-vol regime (signals are less reliable)
        if vol_regime == "low":
            confidence = 0.8
        elif vol_regime == "medium":
            confidence = 0.6
        else:
            confidence = 0.35

        # Shrink p_up toward 0.5 based on confidence
        # Higher confidence -> trust signal more
        p_up_adjusted = 0.5 + (p_up - 0.5) * confidence

        return Signal(
            p_up=float(np.clip(p_up_adjusted, 0.01, 0.99)),
            confidence=confidence,
            momentum_score=momentum,
            rsi=rsi,
            vol_regime=vol_regime,
            book_imbalance=book_imb,
            timestamp=time.time(),
        )
