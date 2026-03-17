# Zero Hero Trading Bot — Setup Guide
# =====================================

# ── REQUIREMENTS ─────────────────────────────────────────────
# requirements.txt — install with: pip3 install -r requirements.txt

growwapi>=1.0.0
requests>=2.28.0
schedule>=1.2.0


# ── DIRECTORY STRUCTURE ──────────────────────────────────────
# /home/claude/zero_hero/
#   ├── config.py          ← YOUR KEYS GO HERE
#   ├── engine.py          ← Zero Hero strategy logic
#   ├── paper_trader.py    ← Virtual ₹10,000 portfolio
#   ├── telegram_bot.py    ← Notifications
#   ├── scheduler.py       ← Main loop (run this)
#   ├── requirements.txt
#   └── logs/
#       └── zero_hero.log

# ── STEP-BY-STEP SETUP ON YOUR VPS ──────────────────────────

# 1. SSH into your VPS
#    ssh user@your-vps-ip

# 2. Create the project directory
#    mkdir -p /home/claude/zero_hero/logs
#    cd /home/claude/zero_hero

# 3. Upload all .py files (or copy-paste them)

# 4. Install dependencies
#    pip3 install -r requirements.txt --break-system-packages

# 5. Edit config.py with your actual keys
#    nano config.py
#    → Fill in GROWW_API_KEY, GROWW_API_SECRET
#    → Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# 6. Test the connection
#    python3 -c "from engine import get_groww_client; g = get_groww_client(); print('Groww connected ✅')"

# 7. Run the bot manually first to verify
#    python3 scheduler.py

# 8. Once working, run it with PM2 so it restarts automatically
#    pm2 start scheduler.py --name "zero-hero" --interpreter python3
#    pm2 save
#    pm2 logs zero-hero

# ── GETTING YOUR TELEGRAM CHAT ID ────────────────────────────
# 1. Open Telegram, search for your bot, send /start
# 2. Open: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
# 3. Find "chat" → "id" in the response — that's your TELEGRAM_CHAT_ID

# ── GROWW API AUTH NOTE ───────────────────────────────────────
# Groww uses OAuth2. The API key + secret are used to generate
# an access token. Check the latest Groww SDK docs for the
# exact token generation flow:
# https://groww.in/trade-api/docs/python-sdk
# Once you have the access token, set GROWW_API_KEY = "your_access_token"

# ── IMPORTANT DISCLAIMER ─────────────────────────────────────
# This is a PAPER TRADING simulation only.
# No real orders are placed. No real money is at risk.
# Do not connect real broker order placement until you have
# validated the strategy with at least 30 days of paper trades.
