"""
scheduler.py (v5 — Natenberg improvements)

Changes vs v4:
  - 2-bar signal confirmation: scheduler calls check_and_update_pending()
    before entering any trade. Signal must persist for SIGNAL_CONFIRM_BARS
    consecutive 5-min scans.
  - Spot price passed to update_trade() for thesis-invalidation SL.
  - Banner updated to v5.
"""

import time
import datetime
import schedule
import traceback

from config import (
    TRADE_START, TRADE_END, TIME_STOP,
    UNDERLYINGS, SIGNAL_CONFIRM_BARS,
)
from engine import (
    run_zero_hero_check, get_groww_client, get_spot_price,
    parse_option_chain,  get_atm_strike,   get_best_expiry,
    get_days_to_expiry,  compute_pcr,      get_india_vix,
    enrich_with_sentiment,
)
from paper_trader import (
    load_log, save_log, can_trade, enter_trade,
    update_trade, get_daily_summary, reset_daily,
    check_and_update_pending,
)
from telegram_bot import (
    notify_signal_detected, notify_trade_entered,
    notify_trade_update,   notify_trade_closed,
    notify_daily_summary,  notify_error, send_message,
)


def get_current_ltp(groww, trading_symbol):
    try:
        resp = groww.get_ltp(
            segment=groww.SEGMENT_FNO,
            exchange_trading_symbols=f"NSE_{trading_symbol}"
        )
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
    print(f"\n[{now.strftime('%H:%M:%S')}] ══════════════ Scanning ══════════════")

    try:
        groww = get_groww_client()
        log   = load_log()
        log   = reset_daily(log)

        # ── Monitor open trade ─────────────────────────────────────────
        if log.get("open_trade"):
            symbol     = log["open_trade"]["symbol"]
            underlying = log["open_trade"].get("underlying", "NIFTY")
            price      = get_current_ltp(groww, symbol)
            # Also fetch live spot for thesis-invalidation check
            spot       = get_spot_price(groww, underlying)
            print(f"  [OPEN TRADE] {symbol} @ ₹{price}  |  {underlying} spot ₹{spot:,.0f}")
            if price > 0:
                log, event = update_trade(log, price, current_spot=spot)
                if event:
                    if isinstance(event, dict) and event.get("type") == "CLOSED":
                        notify_trade_closed(event)
                    elif isinstance(event, str):
                        notify_trade_update(event)
            return

        # ── Entry window check ─────────────────────────────────────────
        start = datetime.datetime.strptime(TRADE_START, "%H:%M").time()
        end   = datetime.datetime.strptime(TRADE_END,   "%H:%M").time()
        if not (start <= now.time() <= end):
            print(f"  Outside entry window ({TRADE_START}–{TRADE_END})")
            return

        ok, reason = can_trade(log)
        if not ok:
            print(f"  {reason}")
            return

        # ── Fetch VIX once ─────────────────────────────────────────────
        vix = get_india_vix(groww)

        # ── Loop through all underlyings ───────────────────────────────
        for underlying in UNDERLYINGS:
            print(f"\n  ── {underlying} ──────────────────────────────────")

            result = run_zero_hero_check(groww, underlying)

            # ── 2-bar confirmation ─────────────────────────────────────
            confirm_action, log = check_and_update_pending(log, result)

            if result["go"]:
                pending = log.get("pending_signal", {})
                count   = pending.get("count", 1) if pending else 1
                print(f"  [{underlying}] Signal={result['signal']} "
                      f"Confirmation={count}/{SIGNAL_CONFIRM_BARS}")

            if confirm_action == "PENDING":
                pending = log.get("pending_signal", {})
                count   = pending.get("count", 1) if pending else 1
                print(f"  [{underlying}] ⏳ Signal pending confirmation "
                      f"({count}/{SIGNAL_CONFIRM_BARS} bars)")
                continue   # wait for next scan

            if confirm_action != "ENTER":
                # NONE or RESET
                if result["go"] is False:
                    failed = [k for k, v in result["checklist"].items()
                              if not v["passed"]]
                    print(f"  [{underlying}] Failed: {failed}")
                continue

            # ── Signal confirmed — fetch sentiment chain and enter ──────
            expiry      = get_best_expiry(groww, underlying)
            spot        = get_spot_price(groww, underlying)
            atm         = get_atm_strike(spot, underlying)
            days_to_exp = get_days_to_expiry(expiry) if expiry else 7

            from config import EXPIRY_EXCHANGE
            _exchange_str = EXPIRY_EXCHANGE.get(underlying, "NSE")
            exchange      = getattr(groww, f"EXCHANGE_{_exchange_str}", _exchange_str)

            chain_resp   = groww.get_option_chain(
                exchange=exchange, underlying=underlying, expiry_date=expiry
            )
            option_chain = parse_option_chain(chain_resp)
            pcr          = compute_pcr(option_chain)

            result = enrich_with_sentiment(
                result, option_chain, spot, atm, days_to_exp, vix, pcr
            )

            print(f"  [{underlying}] ✅ CONFIRMED | Signal={result['signal']} "
                  f"Sentiment={result.get('sentiment', {}).get('overall', '–')}")

            # Iron Condor opportunity
            ic = result.get("iron_condor_opportunity", {})
            if ic.get("detected"):
                s = ic["structure"]
                print(f"  [{underlying}] 🦅 Iron Condor: "
                      f"Credit ₹{s['net_credit']} | Max loss ₹{s['max_loss']}")

            if result["go"]:
                entry = result["entry"]
                # Notify with extra context
                notify_signal_detected(result)
                send_message(
                    f"📊 *Entry details*\n"
                    f"DTE: `{entry.get('dte', '?')}` | "
                    f"IV Rank: `{entry.get('iv_rank', '?')}%` | "
                    f"Confirmation: `{SIGNAL_CONFIRM_BARS} bars`"
                )
                log, trade_or_msg = enter_trade(log, entry)
                if isinstance(trade_or_msg, dict):
                    notify_trade_entered(trade_or_msg)
                else:
                    print(f"  [{underlying}] {trade_or_msg}")
                break   # one trade at a time
            else:
                failed = [k for k, v in result["checklist"].items()
                          if not v["passed"]]
                print(f"  [{underlying}] Blocked post-sentiment: {failed}")

        print(f"\n[{now.strftime('%H:%M:%S')}] ══════════════ Scan complete ══════════")

    except Exception as e:
        notify_error("run_scan()", str(e))
        print(traceback.format_exc())


def run_daily_summary():
    print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Daily summary")
    try:
        groww = get_groww_client()
        log   = load_log()
        if log.get("open_trade"):
            symbol     = log["open_trade"]["symbol"]
            underlying = log["open_trade"].get("underlying", "NIFTY")
            price      = get_current_ltp(groww, symbol)
            spot       = get_spot_price(groww, underlying)
            if price > 0:
                log, event = update_trade(log, price, current_spot=spot)
                if event and isinstance(event, dict):
                    notify_trade_closed(event)
        notify_daily_summary(get_daily_summary(log))
    except Exception as e:
        notify_error("daily_summary()", str(e))


schedule.every(5).minutes.do(run_scan)
schedule.every().day.at("15:30").do(run_daily_summary)

if __name__ == "__main__":
    print("=" * 62)
    print("  ZERO HERO BOT v5.0 — NATENBERG IMPROVEMENTS")
    print(f"  Underlyings  : {', '.join(UNDERLYINGS)}")
    print(f"  Capital      : ₹{__import__('config').CAPITAL:,}")
    print(f"  VIX range    : {__import__('config').VIX_MIN}–{__import__('config').VIX_MAX}")
    print(f"  Min DTE      : {__import__('config').MIN_DAYS_TO_EXPIRY}")
    print(f"  Confirmation : {SIGNAL_CONFIRM_BARS} bars")
    print(f"  IV buy max   : {__import__('config').IV_BUY_MAX_RANK}th percentile")
    print(f"  SL           : {__import__('config').STOP_LOSS_PCT*100:.0f}%")
    print(f"  Scan         : every 5 min | {TRADE_START}–{TRADE_END}")
    print("=" * 62)

    if is_market_hours():
        run_scan()

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── LIVE TRADING INTEGRATION ──────────────────────────────────

def execute_trade_decision(groww, result, log):
    from config import LIVE_TRADING
    if LIVE_TRADING:
        from trader       import execute_live_trade
        underlying = result.get("underlying", "UNKNOWN")
        entry      = result["entry"]
        send_message(
            f"🔴 *LIVE TRADE EXECUTING*\n"
            f"Underlying: `{underlying}`\n"
            f"Signal: `{result['signal']}`\n"
            f"Symbol: `{entry['trading_symbol']}`\n"
            f"Premium: `₹{entry['ltp']}`\n"
            f"DTE: `{entry.get('dte','?')}` | IV Rank: `{entry.get('iv_rank','?')}%`\n"
            f"_Placing real order now..._"
        )
        success, order_id, oco_id = execute_live_trade(groww, entry)
        if success:
            send_message(
                f"✅ *LIVE ORDER PLACED*\n"
                f"Entry ID: `{order_id}`\n"
                f"OCO ID: `{oco_id}`"
            )
        else:
            send_message("🛑 *Live order blocked by safety gate*")
    else:
        notify_signal_detected(result)
        log, trade_or_msg = enter_trade(log, result["entry"])
        if isinstance(trade_or_msg, dict):
            notify_trade_entered(trade_or_msg)
    return log
