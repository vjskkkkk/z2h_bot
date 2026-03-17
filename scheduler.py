"""
scheduler.py  (v3 — with sentiment enrichment)
"""

import time, datetime, schedule, traceback
from growwapi import GrowwAPI
from config import GROWW_API_KEY, TRADE_START, TRADE_END, TIME_STOP, UNDERLYINGS
from engine import (run_zero_hero_check, get_groww_client, get_spot_price,
                    parse_option_chain, get_atm_strike, get_nearest_expiry,
                    get_days_to_expiry, compute_pcr, get_india_vix,
                    enrich_with_sentiment)
from paper_trader import (load_log, save_log, can_trade, enter_trade,
                           update_trade, get_daily_summary, reset_daily)
from telegram_bot import (notify_signal_detected, notify_trade_entered,
                           notify_trade_update, notify_trade_closed,
                           notify_daily_summary, notify_error)


def get_current_ltp(groww, trading_symbol):
    try:
        resp = groww.get_ltp(segment=groww.SEGMENT_FNO,
                             exchange_trading_symbols=f"NSE_{trading_symbol}")
        if isinstance(resp, dict):
            for key in resp:
                val = resp[key]
                if isinstance(val, dict) and "ltp" in val:
                    return float(val["ltp"])
        return 0
    except Exception as e:
        print(f"[LTP] {e}")
        return 0


def is_market_hours():
    now = datetime.datetime.now().time()
    return datetime.time(9, 40) <= now <= datetime.time(15, 35)


def run_scan():
    now = datetime.datetime.now()
    print(f"\n[{now.strftime('%H:%M:%S')}] ── Scanning ──")
    try:
        groww = get_groww_client()
        log   = load_log()
        log   = reset_daily(log)

        # Monitor open trade
        if log.get("open_trade"):
            symbol = log["open_trade"]["symbol"]
            price  = get_current_ltp(groww, symbol)
            print(f"  Open: {symbol} @ ₹{price}")
            if price > 0:
                log, event = update_trade(log, price)
                if event:
                    if isinstance(event, dict) and event.get("type") == "CLOSED":
                        notify_trade_closed(event)
                    elif isinstance(event, str):
                        notify_trade_update(event)
            return

        # Entry window check
        start = datetime.datetime.strptime(TRADE_START, "%H:%M").time()
        end   = datetime.datetime.strptime(TRADE_END,   "%H:%M").time()
        if not (start <= now.time() <= end):
            print(f"  Outside entry window")
            return

        ok, reason = can_trade(log)
        if not ok:
            print(f"  {reason}")
            return

        # Run core Zero Hero checklist
        result = run_zero_hero_check(groww)

        # Fetch market data for sentiment (reuse what engine already fetched)
        expiry       = get_nearest_expiry(groww)
        spot         = get_spot_price(groww)
        atm          = get_atm_strike(spot)
        days_to_exp  = get_days_to_expiry(expiry) if expiry else 7
        vix          = get_india_vix(groww)
        chain_resp   = groww.get_option_chain(
                           exchange=groww.EXCHANGE_NSE,
                           underlying="NIFTY",
                           expiry_date=expiry)
        option_chain = parse_option_chain(chain_resp)
        pcr          = compute_pcr(option_chain)

        # Enrich with sentiment layer
        result = enrich_with_sentiment(
            result, option_chain, spot, atm, days_to_exp, vix, pcr
        )

        print(f"  Signal={result['signal']} GO={result['go']} "
              f"Action={result.get('recommendation',{}).get('action','–')} "
              f"Sentiment={result.get('sentiment',{}).get('overall','–')}")

        # Iron Condor opportunity (even without directional signal)
        ic = result.get("iron_condor_opportunity", {})
        if ic.get("detected"):
            s = ic["structure"]
            print(f"  🦅 Iron Condor opportunity: "
                  f"Credit ₹{s['net_credit']} | Max loss ₹{s['max_loss']}")

        if result["go"]:
            notify_signal_detected(result)
            log, trade_or_msg = enter_trade(log, result["entry"])
            if isinstance(trade_or_msg, dict):
                notify_trade_entered(trade_or_msg)
            else:
                print(f"  {trade_or_msg}")
        else:
            failed = [k for k, v in result["checklist"].items() if not v["passed"]]
            print(f"  Failed: {failed}")

    except Exception as e:
        notify_error("run_scan()", str(e))
        print(traceback.format_exc())


def run_daily_summary():
    print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Daily summary")
    try:
        groww = get_groww_client()
        log   = load_log()
        if log.get("open_trade"):
            symbol = log["open_trade"]["symbol"]
            price  = get_current_ltp(groww, symbol)
            if price > 0:
                log, event = update_trade(log, price)
                if event and isinstance(event, dict):
                    notify_trade_closed(event)
        notify_daily_summary(get_daily_summary(log))
    except Exception as e:
        notify_error("daily_summary()", str(e))


schedule.every(5).minutes.do(run_scan)
schedule.every().day.at("15:30").do(run_daily_summary)

if __name__ == "__main__":
    print("=" * 55)
    print("  ZERO HERO BOT v3.1 — FULL SENTIMENT LAYER")
    print(f"  Capital: ₹{__import__('config').CAPITAL:,}")
    print(f"  Scan: every 5 min | {TRADE_START}–{TRADE_END}")
    print("=" * 55)
    if is_market_hours():
        run_scan()
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── LIVE TRADING INTEGRATION ─────────────────────────────────
# This function replaces the paper trade block in run_scan()
# when LIVE_TRADING=True. The rest of scheduler.py is unchanged.

def execute_trade_decision(groww, result, log):
    """
    Routes to paper trader or live trader based on LIVE_TRADING flag.
    Called from run_scan() after a valid signal is confirmed.
    """
    from config import LIVE_TRADING

    if LIVE_TRADING:
        from trader import execute_live_trade, check_time_stop
        from telegram_bot import send_message

        send_message(
            f"🔴 *LIVE TRADE EXECUTING*\n"
            f"Signal: `{result['signal']}`\n"
            f"Action: `{result['recommendation']['action']}`\n"
            f"Symbol: `{result['entry']['trading_symbol']}`\n"
            f"Premium: `₹{result['entry']['ltp']}`\n\n"
            f"_Placing real order now..._"
        )

        success, order_id, oco_id = execute_live_trade(groww, result["entry"])

        if success:
            send_message(
                f"✅ *LIVE ORDER PLACED*\n"
                f"Entry ID: `{order_id}`\n"
                f"OCO ID:   `{oco_id}`\n"
                f"SL and target bracket active."
            )
        else:
            send_message("🛑 *Live order blocked by safety gate* — check terminal logs")

    else:
        # Paper trading (default)
        from paper_trader import enter_trade
        from telegram_bot import notify_signal_detected, notify_trade_entered
        notify_signal_detected(result)
        log, trade_or_msg = enter_trade(log, result["entry"])
        if isinstance(trade_or_msg, dict):
            notify_trade_entered(trade_or_msg)
        return log

    return log
