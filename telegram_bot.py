"""
zero_hero/telegram_bot.py
==========================
Sends structured Telegram messages for signals, entries, exits, and summaries.
"""

import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_message(text, parse_mode="Markdown"):
    """Send a message to your Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": parse_mode,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[TELEGRAM] Failed to send: {e}")
        return False


def notify_signal_detected(result):
    """Alert when all conditions are met and a paper trade is being entered."""
    entry = result["entry"]
    checks = result["checklist"]

    check_lines = "\n".join(
        f"{'✅' if v['passed'] else '❌'} {k}: {v['detail']}"
        for k, v in checks.items()
    )

    msg = (
        f"🚨 *ZERO HERO SIGNAL DETECTED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Signal:  `{result['signal']}`\n"
        f"🎯 Symbol:  `{entry.get('trading_symbol', 'N/A')}`\n"
        f"💵 Premium: ₹{entry.get('ltp', 0):.2f}\n"
        f"📊 Spot:    ₹{entry.get('spot', 0):,.0f}\n"
        f"📉 PCR:     {entry.get('pcr', 0)}\n"
        f"🌡 VIX:     {entry.get('vix', 0)}\n"
        f"📅 Expiry:  {entry.get('expiry', 'N/A')}\n\n"
        f"*Checklist:*\n{check_lines}\n\n"
        f"_📝 Paper trade being entered now..._"
    )
    send_message(msg)


def notify_trade_entered(trade):
    """Confirm paper trade entry with full position details."""
    msg = (
        f"📥 *PAPER TRADE ENTERED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID:    `{trade['id']}`\n"
        f"📌 Symbol:      `{trade['symbol']}`\n"
        f"📐 Direction:   `{trade['direction']}`\n"
        f"💵 Entry Price: ₹{trade['entry_price']:.2f}\n"
        f"📦 Units:       {trade['units']}\n"
        f"💸 Capital Used: ₹{trade['cost']:,.2f}\n\n"
        f"🛑 Stop-Loss:   ₹{trade['sl_price']:.2f} (-35%)\n"
        f"🎯 Partial Target: ₹{trade['target_price']:.2f} (+60%)\n"
        f"⏰ Time Stop:   2:45 PM\n\n"
        f"_This is a PAPER trade. No real money involved._"
    )
    send_message(msg)


def notify_trade_update(event_text):
    """Send an update about a mid-trade event (partial exit, trailing SL moved)."""
    msg = f"🔄 *Trade Update*\n{event_text}"
    send_message(msg)


def notify_trade_closed(event):
    """Send trade closure notification with P&L."""
    trade = event["trade"]
    pnl   = event["pnl"]
    emoji = "✅ PROFIT" if pnl > 0 else "❌ LOSS"

    msg = (
        f"{'🏆' if pnl > 0 else '💥'} *PAPER TRADE CLOSED — {emoji}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Trade ID:   `{trade['id']}`\n"
        f"📌 Symbol:     `{trade['symbol']}`\n"
        f"💵 Entry:      ₹{trade['entry_price']:.2f}\n"
        f"🚪 Exit:       ₹{trade['exit_price']:.2f}\n"
        f"📦 Units:      {trade['units']}\n"
        f"💰 Net P&L:   *₹{pnl:.2f}*\n"
        f"📋 Reason:     {event['reason']}\n"
        f"⏱ Duration:  {trade['entry_time']} → {trade['exit_time']}"
    )
    send_message(msg)


def notify_no_signal(reason):
    """Quietly log why no signal was found (optional — can disable to reduce noise)."""
    # Uncomment if you want verbose no-signal notifications
    # send_message(f"🔍 Scan complete — No signal: {reason}")
    pass


def notify_daily_summary(summary_text):
    """Send end-of-day P&L summary."""
    send_message(summary_text)


def notify_error(context, error):
    """Send an error alert."""
    send_message(f"⚠️ *Error in Zero Hero Bot*\nContext: {context}\nError: `{error}`")


def notify_iron_condor(ic_data, atm):
    """Send Iron Condor opportunity alert."""
    s = ic_data["structure"]
    msg = (
        f"🦅 *IRON CONDOR OPPORTUNITY*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Market appears range-bound\n\n"
        f"*Structure (paper trade)*\n"
        f"  Sell CE {s['sell_ce']} @ ₹{s['sell_ce_ltp']}\n"
        f"  Buy  CE {s['buy_ce']}  @ ₹{s['buy_ce_ltp']}\n"
        f"  Sell PE {s['sell_pe']} @ ₹{s['sell_pe_ltp']}\n"
        f"  Buy  PE {s['buy_pe']}  @ ₹{s['buy_pe_ltp']}\n\n"
        f"💰 Net credit: `₹{s['net_credit']}`\n"
        f"🛑 Max loss:   `₹{s['max_loss']}`\n"
        f"📊 R/R ratio:  `{s['rr_ratio']}x`\n\n"
        f"_{ic_data['detail']}_"
    )
    send_message(msg)


def notify_sentiment_block(sentiment):
    """Append sentiment summary to any signal message."""
    if not sentiment:
        return ""
    skew    = sentiment.get("skew", {})
    breadth = sentiment.get("breadth", {})
    fii     = sentiment.get("fii", {})
    parity  = sentiment.get("parity", {})

    return (
        f"\n\n🧠 *Sentiment Layer*\n"
        f"  Overall:  `{sentiment.get('overall','–')}`\n"
        f"  IV Skew:  `{skew.get('detail','–')}`\n"
        f"  Breadth:  `{breadth.get('detail','–')}`\n"
        f"  FII Flow: `{fii.get('detail','–')}`\n"
        f"  Parity:   `{parity.get('detail','–')}`"
    )
