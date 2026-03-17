# =============================================================
# ZERO HERO TRADING BOT — CONFIG
# Fill in your actual keys. Never share or commit this file.
# =============================================================

import os

# --- Groww API ---
GROWW_TOTP_TOKEN  = "GROWW_TOTP_TOKEN"
GROWW_TOTP_SECRET = "GROWW_TOTP_SECRET"
GROWW_API_KEY     = GROWW_TOTP_TOKEN   # backwards compatibility alias

# --- Telegram ---
TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "TELEGRAM_CHAT_ID"   # Your personal chat ID with the bot

# --- Strategy Settings ---
UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]
UNDERLYING  = "NIFTY"     # default single-underlying (backwards compat)
EXCHANGE    = "NSE"

CAPITAL = 500000          # ₹5L — good
MAX_RISK_PER_TRADE_PCT = 0.01   # 1% = ₹5,000 per trade — professional standard
DAILY_LOSS_CAP_PCT     = 0.03   # 3% = ₹15,000 max day loss
MAX_TRADES_PER_DAY     = 3

ATM_ROUNDING = {
    "NIFTY":     50,
    "BANKNIFTY": 100,
    "SENSEX":    100,
}

LTP_SYMBOLS = {
    "NIFTY":     "NSE_NIFTY",
    "BANKNIFTY": "NSE_BANKNIFTY",
    "SENSEX":    "BSE_SENSEX",
}

EXPIRY_EXCHANGE = {
    "NIFTY":     "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX":    "BSE",
}

# Lot sizes per underlying (used for position sizing)
LOT_SIZES = {
    "NIFTY":     75,
    "BANKNIFTY": 35,
    "SENSEX":    20,
}

# --- Zero Hero Signal Thresholds ---
OI_CHANGE_PCT_THRESHOLD  = 10000   # 10,000% OI change required
OI_CHANGE_ABS_THRESHOLD  = 50000   # Minimum 50,000 new contracts
MIN_STRIKES_WITH_SPIKE   = 2       # Spike must appear on 2+ strikes
PCR_BEARISH_THRESHOLD    = 0.7     # PCR below this = bearish confirmation
PCR_BULLISH_THRESHOLD    = 1.2     # PCR above this = bullish confirmation
VIX_MAX                  = 22.0    # Skip if VIX above this

# --- Trade Management ---
STOP_LOSS_PCT      = 0.35   # Exit if premium drops 35%
PARTIAL_EXIT_PCT   = 0.60   # Take 50% profit at +60%
TRAIL_STOP_PCT     = 0.20   # Trail remaining with 20% trailing SL
TIME_STOP          = "14:45"  # Hard exit time
TRADE_START        = "09:45"
TRADE_END          = "14:00"  # No new entries after this

# --- Log File ---
TRADES_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log.json")

# =============================================================
# LIVE TRADING — DO NOT CHANGE UNTIL 30 DAYS OF PAPER TRADING
# =============================================================
LIVE_TRADING = False      # ← The master switch. False = paper only.
LIVE_CAPITAL = 50000      # ← Real capital allocated for live trading
                          #   (separate from paper CAPITAL = ₹5,00,000)

# Live order safety limits
MAX_LIVE_LOSS_PER_DAY = 1500    # ₹ — hard stop for the day in live mode
MAX_LIVE_TRADES_DAY   = 2       # Even more conservative in live mode
