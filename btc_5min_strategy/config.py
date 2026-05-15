"""
Configuration for Polymarket 5-min BTC Up/Down trading bot.

Fill in your credentials and adjust parameters before running.
"""

# =====================================================================
# POLYMARKET API CREDENTIALS
# =====================================================================
# Get these from: https://docs.polymarket.com/trading/quickstart
PRIVATE_KEY = ""          # Your Ethereum wallet private key (hex, with 0x prefix)
API_KEY = ""              # Polymarket CLOB API key
API_SECRET = ""           # Polymarket CLOB API secret
API_PASSPHRASE = ""       # Polymarket CLOB API passphrase
FUNDER_ADDRESS = ""       # Your deposit/funder wallet address

# =====================================================================
# MARKET CONFIGURATION
# =====================================================================
# Polymarket 5-min BTC market condition token IDs
# These change every 5 minutes; the bot auto-discovers them via API
CHAIN_ID = 137            # Polygon mainnet

# =====================================================================
# STRATEGY PARAMETERS
# =====================================================================

# -- Risk Management --
BANKROLL = 100.0                # Total capital allocated (USDC)
MAX_POSITION_PCT = 0.05         # Max 5% of bankroll per single bet
MAX_DAILY_LOSS_PCT = 0.10       # Stop trading if daily loss exceeds 10%
MAX_CONCURRENT_POSITIONS = 3    # Max open positions at once

# -- Signal Thresholds --
MIN_EDGE = 0.02                 # Minimum required edge (2%) to enter a trade
MOMENTUM_LOOKBACK_SECONDS = 120 # Look at last 2 min of BTC price for momentum
VOLATILITY_LOOKBACK_SECONDS = 300  # 5 min for vol estimation
RSI_PERIOD = 14                 # RSI lookback (on 5-second candles)
RSI_OVERBOUGHT = 72            # RSI overbought threshold -> lean "Down"
RSI_OVERSOLD = 28              # RSI oversold threshold -> lean "Up"

# -- Timing --
ENTRY_WINDOW_START = 10         # Start looking for trades 10s after market opens
ENTRY_WINDOW_END = 180          # Stop entering after 3 minutes into the 5-min window
EXIT_DEADLINE = 270             # Must exit by 4:30 into window (allow settlement)

# -- Market Making Parameters --
SPREAD_BPS = 300                # 3% spread for passive quotes (maker rebate capture)
REQUOTE_INTERVAL = 5            # Re-quote every 5 seconds
MIN_BOOK_IMBALANCE = 0.15      # Order book imbalance threshold for signal

# -- Maker/Taker Fees --
MAKER_REBATE = 0.0112          # +1.12% maker rebate
TAKER_FEE = 0.0112             # -1.12% taker fee

# =====================================================================
# DATA SOURCES
# =====================================================================
# BTC price feed (for signal generation)
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST_KLINES = "https://api.binance.com/api/v3/klines"

# Polymarket endpoints
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"

# =====================================================================
# LOGGING
# =====================================================================
LOG_LEVEL = "INFO"
LOG_FILE = "btc_5min_bot.log"
TRADE_LOG_FILE = "trades.csv"
