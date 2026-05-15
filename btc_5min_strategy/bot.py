"""
Main bot loop for Polymarket 5-min BTC Up/Down auto-trading.

This is the execution engine that:
1. Connects to Binance WebSocket for real-time BTC price
2. Connects to Polymarket CLOB API for market data & order placement
3. Runs the strategy on each 5-minute window cycle
4. Manages the full lifecycle: signal -> decision -> order -> resolution

Usage:
    python bot.py                    # Run live (requires API keys in config.py)
    python bot.py --dry-run          # Paper trading mode (no real orders)
    python bot.py --dry-run --verbose
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from signals import SignalEngine, PriceTick
from strategy import TradingStrategy, Side, OrderType

# Setup logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_FILE),
    ]
)
logger = logging.getLogger("bot")


class PolymarketBTCBot:
    """
    Async bot that trades Polymarket 5-min BTC Up/Down markets.
    """

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.strategy = TradingStrategy()
        self.signal_engine = self.strategy.signal_engine
        self.running = False

        # Market state
        self.current_market_id: Optional[str] = None
        self.window_start_time: float = 0
        self.window_start_price: float = 0
        self.latest_btc_price: float = 0

        # Polymarket orderbook
        self.market_price_up: float = 0.50
        self.market_price_down: float = 0.50
        self.bids: list = []
        self.asks: list = []

        # Trade log
        self.trade_log_path = Path(config.TRADE_LOG_FILE)
        self._init_trade_log()

    def _init_trade_log(self):
        """Initialize CSV trade log."""
        if not self.trade_log_path.exists():
            with open(self.trade_log_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'market_id', 'side', 'order_type',
                    'size_usd', 'price', 'signal_p_up', 'momentum',
                    'rsi', 'vol_regime', 'outcome', 'pnl'
                ])

    def _log_trade(self, **kwargs):
        """Append trade to CSV log."""
        with open(self.trade_log_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                kwargs.get('market_id', ''),
                kwargs.get('side', ''),
                kwargs.get('order_type', ''),
                kwargs.get('size_usd', 0),
                kwargs.get('price', 0),
                kwargs.get('signal_p_up', 0),
                kwargs.get('momentum', 0),
                kwargs.get('rsi', 0),
                kwargs.get('vol_regime', ''),
                kwargs.get('outcome', ''),
                kwargs.get('pnl', 0),
            ])

    # =================================================================
    # MARKET DISCOVERY
    # =================================================================
    async def discover_active_market(self) -> Optional[str]:
        """
        Find the currently active 5-min BTC Up/Down market on Polymarket.

        Uses the Gamma API to search for active markets.
        In production, this queries:
        GET https://gamma-api.polymarket.com/markets?tag=crypto&active=true
        """
        if self.dry_run:
            # Simulate market discovery
            return f"sim_btc_5min_{int(time.time() // 300) * 300}"

        # Production: use py-clob-client-v2
        # from py_clob_client import ClobClient
        # client = ClobClient(config.POLYMARKET_CLOB_URL, chain_id=config.CHAIN_ID, ...)
        # markets = client.get_markets()
        # ... filter for active 5-min BTC market
        logger.info("Market discovery: would query Gamma API here")
        return None

    # =================================================================
    # PRICE FEED (Binance WebSocket)
    # =================================================================
    async def binance_price_feed(self):
        """
        Connect to Binance WebSocket for real-time BTC/USDT trades.
        Feeds ticks into the signal engine.
        """
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed; using simulated feed")
            await self._simulated_price_feed()
            return

        url = config.BINANCE_WS_URL
        logger.info(f"Connecting to Binance WS: {url}")

        async with websockets.connect(url) as ws:
            async for msg in ws:
                if not self.running:
                    break
                data = json.loads(msg)
                tick = PriceTick(
                    timestamp=data['T'] / 1000.0,
                    price=float(data['p']),
                    volume=float(data['q']),
                )
                self.latest_btc_price = tick.price
                self.signal_engine.add_tick(tick)

    async def _simulated_price_feed(self):
        """Simulated BTC price feed for dry-run testing."""
        import random
        price = 104000.0  # starting price
        while self.running:
            # Random walk with slight drift
            change = random.gauss(0, 15)  # ~$15 per tick std dev
            price += change
            price = max(price * 0.99, min(price * 1.01, price))

            tick = PriceTick(
                timestamp=time.time(),
                price=price,
                volume=random.uniform(0.01, 2.0),
            )
            self.latest_btc_price = tick.price
            self.signal_engine.add_tick(tick)
            await asyncio.sleep(0.5)  # 2 ticks per second

    # =================================================================
    # POLYMARKET ORDERBOOK FEED
    # =================================================================
    async def polymarket_book_feed(self):
        """
        Poll Polymarket orderbook for the active market.
        Updates market prices and order book state.
        """
        while self.running:
            if self.current_market_id is None:
                await asyncio.sleep(1)
                continue

            if self.dry_run:
                # Simulate orderbook around 50/50
                import random
                noise = random.gauss(0, 0.02)
                self.market_price_up = 0.50 + noise
                self.market_price_down = 1.0 - self.market_price_up
                # Simulate some book depth
                self.bids = [(self.market_price_up - 0.01 * i, 100 + i * 50)
                             for i in range(5)]
                self.asks = [(self.market_price_up + 0.01 * i, 100 + i * 50)
                             for i in range(5)]
            else:
                # Production: query CLOB API
                # book = client.get_order_book(self.current_market_id)
                # self.bids = [(o.price, o.size) for o in book.bids]
                # self.asks = [(o.price, o.size) for o in book.asks]
                pass

            await asyncio.sleep(config.REQUOTE_INTERVAL)

    # =================================================================
    # ORDER EXECUTION
    # =================================================================
    async def place_order(self, decision) -> bool:
        """
        Place order on Polymarket.

        Returns True if order was placed/filled successfully.
        """
        if decision.action == "skip":
            return False

        side_str = decision.side.value if decision.side else "unknown"
        logger.info(
            f"{'[DRY-RUN] ' if self.dry_run else ''}"
            f"ORDER: {decision.action} | {decision.order_type.value} | "
            f"${decision.size_usd:.2f} @ {decision.limit_price or 'market'} | "
            f"{decision.reason}"
        )

        if self.dry_run:
            # Simulate fill
            fill_price = decision.limit_price or self.market_price_up
            if decision.side == Side.DOWN:
                fill_price = decision.limit_price or self.market_price_down

            self.strategy.on_fill(
                side=decision.side,
                size=decision.size_usd,
                price=fill_price,
                market_id=self.current_market_id or "sim",
            )
            return True

        # Production: use py-clob-client-v2
        # from py_clob_client import ClobClient, OrderArgs, ...
        # order = client.create_and_post_order(OrderArgs(...))
        return False

    # =================================================================
    # MAIN TRADING LOOP
    # =================================================================
    async def trading_loop(self):
        """
        Main loop: one iteration per 5-minute window.

        Timeline of a 5-min window:
        [0s]     Window opens, record start price
        [10s]    Begin evaluating signals
        [10-180s] Look for entry opportunities
        [180-270s] Monitor position, consider exit
        [270s]   Force exit deadline
        [300s]   Resolution
        """
        while self.running:
            # 1. Discover the next active market
            self.current_market_id = await self.discover_active_market()
            if not self.current_market_id:
                logger.warning("No active market found. Waiting...")
                await asyncio.sleep(10)
                continue

            # 2. Calculate timing
            now = time.time()
            window_slot = int(now // 300) * 300  # current 5-min slot start
            self.window_start_time = window_slot
            elapsed = now - window_slot
            remaining = 300 - elapsed

            logger.info(
                f"\n{'='*60}\n"
                f"NEW WINDOW: market={self.current_market_id}\n"
                f"Elapsed: {elapsed:.0f}s | Remaining: {remaining:.0f}s\n"
                f"BTC: ${self.latest_btc_price:,.0f} | "
                f"Bankroll: ${self.strategy.bankroll:.2f}\n"
                f"{'='*60}"
            )

            if self.latest_btc_price > 0:
                self.window_start_price = self.latest_btc_price

            # 3. Trading phase: evaluate signal every few seconds
            traded_this_window = False
            while self.running:
                now = time.time()
                elapsed = now - self.window_start_time

                if elapsed >= 295:  # Window about to resolve
                    break

                if not traded_this_window and elapsed >= config.ENTRY_WINDOW_START:
                    # Generate signal
                    signal = self.signal_engine.generate_signal(
                        market_price_up=self.market_price_up,
                        bids=self.bids,
                        asks=self.asks,
                    )

                    # Make decision
                    decision = self.strategy.decide(
                        signal=signal,
                        elapsed_seconds=elapsed,
                        market_price_up=self.market_price_up,
                        market_price_down=self.market_price_down,
                    )

                    if decision.action != "skip":
                        success = await self.place_order(decision)
                        if success:
                            traded_this_window = True
                            self._log_trade(
                                market_id=self.current_market_id,
                                side=decision.side.value if decision.side else '',
                                order_type=decision.order_type.value,
                                size_usd=decision.size_usd,
                                price=decision.limit_price or self.market_price_up,
                                signal_p_up=signal.p_up,
                                momentum=signal.momentum_score,
                                rsi=signal.rsi,
                                vol_regime=signal.vol_regime,
                            )

                await asyncio.sleep(config.REQUOTE_INTERVAL)

            # 4. Wait for resolution
            await asyncio.sleep(max(0, 300 - (time.time() - self.window_start_time) + 2))

            # 5. Resolve
            if self.strategy.positions:
                # Determine outcome
                outcome_up = self.latest_btc_price >= self.window_start_price
                logger.info(
                    f"RESOLUTION: BTC {'UP' if outcome_up else 'DOWN'} | "
                    f"Start=${self.window_start_price:,.0f} "
                    f"End=${self.latest_btc_price:,.0f}"
                )
                self.strategy.on_resolution(
                    self.current_market_id or "sim",
                    outcome_up=outcome_up,
                )

            # Stats
            stats = self.strategy.stats
            logger.info(
                f"STATS: Bankroll=${stats['bankroll']:.2f} | "
                f"PnL=${stats['total_pnl']:+.2f} | "
                f"Trades={stats['total_trades']} | "
                f"WinRate={stats['win_rate']:.1%}"
            )

    # =================================================================
    # BOT LIFECYCLE
    # =================================================================
    async def run(self):
        """Start all async tasks."""
        self.running = True
        logger.info(
            f"{'='*60}\n"
            f"  Polymarket 5-Min BTC Bot Starting\n"
            f"  Mode: {'DRY-RUN' if self.dry_run else 'LIVE'}\n"
            f"  Bankroll: ${config.BANKROLL}\n"
            f"  Max position: {config.MAX_POSITION_PCT*100}% "
            f"(${config.BANKROLL * config.MAX_POSITION_PCT:.2f})\n"
            f"  Min edge: {config.MIN_EDGE*100}%\n"
            f"{'='*60}"
        )

        tasks = [
            asyncio.create_task(self.binance_price_feed()),
            asyncio.create_task(self.polymarket_book_feed()),
            asyncio.create_task(self.trading_loop()),
        ]

        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.running = False

    def stop(self):
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="Polymarket 5-min BTC Bot")
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help="Paper trading mode (default: True)")
    parser.add_argument('--live', action='store_true',
                        help="Enable live trading (requires API keys)")
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    dry_run = not args.live
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    bot = PolymarketBTCBot(dry_run=dry_run)
    asyncio.run(bot.run())


if __name__ == '__main__':
    main()
