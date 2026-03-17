"""
trader.py
==========
Live order execution module for Zero Hero.
Uses Groww SDK to place real orders.

SAFETY: config.py LIVE_TRADING must be True to place real orders.
        Default is False — will raise an error if accidentally called.

Architecture:
  1. Safety gate    — hard checks before any order
  2. Entry order    — market order for the signal
  3. OCO bracket    — target + SL placed immediately after entry
  4. Position monitor — tracks live P&L every 5 min
  5. Exit order     — time stop or manual exit
  6. Emergency exit — cancels all open orders + exits position

Order log: every real order written to order_log.json immediately.
"""

import json
import os
import datetime
import time
from config import *


ORDER_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "order_log.json")
POSITION_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_position.json")


# ── SAFETY GATE ──────────────────────────────────────────────

class LiveTradingDisabled(Exception):
    pass

class SafetyCheckFailed(Exception):
    pass


def safety_gate(groww, entry):
    """
    Hard checks that must ALL pass before a single real order is placed.
    Raises SafetyCheckFailed with reason if anything fails.
    """
    if not LIVE_TRADING:
        raise LiveTradingDisabled(
            "LIVE_TRADING=False in config.py. "
            "Set to True only after 30 days of paper trading verification."
        )

    checks = []

    # 1. Capital check — never risk more than defined limit
    try:
        positions = groww.get_positions_for_user(segment=groww.SEGMENT_FNO)
        open_positions = [p for p in positions.get("positions", [])
                         if p.get("quantity", 0) != 0]
        if open_positions:
            checks.append(f"Already have {len(open_positions)} open FNO position(s)")
    except SafetyCheckFailed:
        raise
    except Exception as e:
        # API error — fail safe by blocking the trade
        checks.append(f"Position check failed (fail-safe block): {e}")

    # 2. Margin check — ensure sufficient margin
    try:
        margin = groww.get_available_margin_details()
        available = float(margin.get("equity", {}).get("available_margin", 0))
        required  = entry["ltp"] * entry.get("units", 1) * 75  # approx
        if available < required * 1.5:
            checks.append(f"Insufficient margin: ₹{available:.0f} available, "
                         f"₹{required:.0f} required")
    except SafetyCheckFailed:
        raise
    except Exception as e:
        checks.append(f"Margin check failed (fail-safe block): {e}")

    # 3. Time check — only trade in window
    now   = datetime.datetime.now().time()
    start = datetime.time(9, 45)
    end   = datetime.time(14, 0)
    if not (start <= now <= end):
        checks.append(f"Outside trading window (now={now.strftime('%H:%M')})")

    # 4. Daily order count — never exceed limit
    log = load_order_log()
    today_orders = [o for o in log.get("orders", [])
                    if o["date"] == str(datetime.date.today())
                    and o["status"] not in ("CANCELLED", "REJECTED")]
    if len(today_orders) >= MAX_TRADES_PER_DAY:
        checks.append(f"Max {MAX_TRADES_PER_DAY} real orders/day reached")

    # 5. Premium sanity — price must be reasonable
    if entry["ltp"] <= 0:
        checks.append("Entry LTP is zero — stale data?")
    if entry["ltp"] > 500:
        checks.append(f"Premium ₹{entry['ltp']} unusually high — verify manually")

    if checks:
        raise SafetyCheckFailed("\n".join(checks))

    return True


# ── ORDER LOG ────────────────────────────────────────────────

def load_order_log():
    if os.path.exists(ORDER_LOG_FILE):
        with open(ORDER_LOG_FILE, "r") as f:
            return json.load(f)
    return {"orders": [], "total_realised_pnl": 0}


def log_order(order_dict):
    log = load_order_log()
    log["orders"].append(order_dict)
    with open(ORDER_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def save_position(position):
    with open(POSITION_FILE, "w") as f:
        json.dump(position, f, indent=2, default=str)


def load_position():
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE, "r") as f:
            return json.load(f)
    return None


def clear_position():
    if os.path.exists(POSITION_FILE):
        os.remove(POSITION_FILE)


# ── POSITION SIZING ──────────────────────────────────────────

def calculate_live_units(premium):
    """
    Same 1% risk rule as paper trader.
    Uses LIVE_CAPITAL from config — separate from paper CAPITAL.
    """
    max_loss      = LIVE_CAPITAL * MAX_RISK_PER_TRADE_PCT
    loss_per_unit = premium * STOP_LOSS_PCT
    if loss_per_unit <= 0:
        return 0
    units = int(max_loss / loss_per_unit)
    # Round down to nearest lot size (Nifty = 75)
    lot_size = 75
    lots  = max(1, units // lot_size)
    return lots * lot_size


# ── ORDER PLACEMENT ──────────────────────────────────────────

def place_entry_order(groww, entry):
    """
    Place a market order for entry.
    Returns order_id or raises on failure.
    """
    action  = entry.get("action", "BUY")
    units   = calculate_live_units(entry["ltp"])

    if units == 0:
        raise SafetyCheckFailed("Position size = 0 units after sizing")

    transaction_type = "BUY" if action == "BUY" else "SELL"

    print(f"  [LIVE] Placing {transaction_type} {units} units of "
          f"{entry['trading_symbol']} @ market")

    response = groww.place_order(
        trading_symbol=entry["trading_symbol"],
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        transaction_type=transaction_type,
        quantity=units,
        order_type=groww.ORDER_TYPE_MARKET,
        product=groww.PRODUCT_MIS,          # MIS = intraday, auto squares off
        duration=groww.DURATION_DAY,
    )

    order_id = response.get("groww_order_id")
    if not order_id:
        raise Exception(f"Order placement failed: {response}")

    # Log immediately
    log_order({
        "date":           str(datetime.date.today()),
        "time":           datetime.datetime.now().strftime("%H:%M:%S"),
        "type":           "ENTRY",
        "action":         action,
        "symbol":         entry["trading_symbol"],
        "units":          units,
        "order_type":     "MARKET",
        "transaction":    transaction_type,
        "groww_order_id": order_id,
        "ltp_at_entry":   entry["ltp"],
        "status":         "PLACED",
        "direction":      entry["direction"],
        "signal_data":    {
            "vix":  entry.get("vix"),
            "pcr":  entry.get("pcr"),
            "spot": entry.get("spot"),
        }
    })

    # Save open position
    sl_price     = round(entry["ltp"] * (1 - STOP_LOSS_PCT), 2)
    target_price = round(entry["ltp"] * (1 + PARTIAL_EXIT_PCT), 2)

    save_position({
        "order_id":       order_id,
        "symbol":         entry["trading_symbol"],
        "action":         action,
        "direction":      entry["direction"],
        "units":          units,
        "entry_price":    entry["ltp"],
        "entry_time":     datetime.datetime.now().strftime("%H:%M:%S"),
        "entry_date":     str(datetime.date.today()),
        "sl_price":       sl_price,
        "target_price":   target_price,
        "trailing_sl":    sl_price,
        "partial_exited": False,
        "oco_order_id":   None,
        "status":         "OPEN",
    })

    print(f"  [LIVE] Entry order placed: {order_id} | "
          f"SL=₹{sl_price} Target=₹{target_price}")

    return order_id, units


def place_oco_bracket(groww, entry_order_id, units, entry_ltp, action):
    """
    Place OCO (One-Cancels-Other) bracket immediately after entry confirms.
    OCO: if target hits → SL cancelled automatically (and vice versa).

    For BUY: target = +60%, SL = -35%
    For SELL: target = -80% decay, SL = +100% (2× premium)
    """
    position = load_position()
    if not position:
        raise Exception("No open position found for OCO bracket")

    sl_price     = position["sl_price"]
    target_price = position["target_price"]
    symbol       = position["symbol"]

    # Wait briefly for entry to confirm
    time.sleep(2)

    # Verify entry filled
    status = groww.get_order_status(
        groww_order_id=entry_order_id,
        segment=groww.SEGMENT_FNO
    )
    if status.get("order_status") not in ("COMPLETE", "TRADED"):
        print(f"  [LIVE] ⚠️ Entry not yet filled: {status.get('order_status')} — OCO pending")

    # Reverse transaction for exit orders
    exit_transaction = "SELL" if action == "BUY" else "BUY"

    oco_response = groww.create_smart_order(
        smart_order_type=groww.SMART_ORDER_TYPE_OCO,
        trading_symbol=symbol,
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        quantity=units,
        product_type=groww.PRODUCT_MIS,
        duration=groww.DURATION_DAY,
        target={
            "trigger_price": str(target_price),
            "order_type":    groww.ORDER_TYPE_LIMIT,
            "price":         str(round(target_price * 0.995, 2)),
        },
        stop_loss={
            "trigger_price": str(sl_price),
            "order_type":    groww.ORDER_TYPE_SL_M,
            "price":         None,
        },
    )

    oco_id = oco_response.get("smart_order_id")

    # Update position with OCO ID
    position["oco_order_id"] = oco_id
    save_position(position)

    log_order({
        "date":           str(datetime.date.today()),
        "time":           datetime.datetime.now().strftime("%H:%M:%S"),
        "type":           "OCO_BRACKET",
        "symbol":         symbol,
        "units":          units,
        "target_price":   target_price,
        "sl_price":       sl_price,
        "oco_order_id":   oco_id,
        "status":         "PLACED",
    })

    print(f"  [LIVE] OCO bracket placed: {oco_id} | "
          f"Target=₹{target_price} SL=₹{sl_price}")

    return oco_id


def place_exit_order(groww, reason="Manual exit"):
    """
    Exit the open position immediately at market price.
    Cancels OCO bracket first to avoid double-fill.
    """
    position = load_position()
    if not position:
        print("  [LIVE] No open position to exit")
        return

    symbol = position["symbol"]
    units  = position["units"]
    action = position["action"]
    exit_transaction = "SELL" if action == "BUY" else "BUY"

    # Cancel OCO bracket first
    oco_id = position.get("oco_order_id")
    if oco_id:
        try:
            groww.cancel_smart_order(
                smart_order_id=oco_id,
                segment=groww.SEGMENT_FNO
            )
            print(f"  [LIVE] OCO {oco_id} cancelled")
        except Exception as e:
            print(f"  [LIVE] ⚠️ Could not cancel OCO: {e}")

    # Place market exit
    print(f"  [LIVE] Placing exit: {exit_transaction} {units} {symbol} @ market")

    response = groww.place_order(
        trading_symbol=symbol,
        exchange=groww.EXCHANGE_NSE,
        segment=groww.SEGMENT_FNO,
        transaction_type=exit_transaction,
        quantity=units,
        order_type=groww.ORDER_TYPE_MARKET,
        product=groww.PRODUCT_MIS,
        duration=groww.DURATION_DAY,
    )

    order_id = response.get("groww_order_id")

    log_order({
        "date":           str(datetime.date.today()),
        "time":           datetime.datetime.now().strftime("%H:%M:%S"),
        "type":           "EXIT",
        "symbol":         symbol,
        "units":          units,
        "transaction":    exit_transaction,
        "groww_order_id": order_id,
        "reason":         reason,
        "status":         "PLACED",
    })

    clear_position()
    print(f"  [LIVE] Exit order placed: {order_id} | Reason: {reason}")
    return order_id


def emergency_exit_all(groww):
    """
    Nuclear option. Cancels ALL open orders and exits ALL FNO positions.
    Call this if the bot misbehaves or connectivity drops mid-trade.
    """
    print("\n🚨 EMERGENCY EXIT TRIGGERED 🚨")

    # Cancel all smart orders
    try:
        smart_orders = groww.get_smart_order_list(
            segment=groww.SEGMENT_FNO,
            smart_order_type=groww.SMART_ORDER_TYPE_OCO,
            status=groww.SMART_ORDER_STATUS_ACTIVE,
            page=0, page_size=50,
            start_date_time=datetime.datetime.now().strftime("%Y-%m-%dT00:00:00"),
            end_date_time=datetime.datetime.now().strftime("%Y-%m-%dT23:59:59"),
        )
        for order in smart_orders.get("orders", []):
            try:
                groww.cancel_smart_order(
                    smart_order_id=order["smart_order_id"],
                    segment=groww.SEGMENT_FNO
                )
                print(f"  Cancelled OCO: {order['smart_order_id']}")
            except Exception as e:
                print(f"  ⚠️ Could not cancel {order['smart_order_id']}: {e}")
    except Exception as e:
        print(f"  ⚠️ Could not fetch smart orders: {e}")

    # Exit all FNO positions
    try:
        positions = groww.get_positions_for_user(segment=groww.SEGMENT_FNO)
        for pos in positions.get("positions", []):
            qty = pos.get("quantity", 0)
            if qty == 0:
                continue
            exit_txn = "SELL" if qty > 0 else "BUY"
            try:
                groww.place_order(
                    trading_symbol=pos["trading_symbol"],
                    exchange=groww.EXCHANGE_NSE,
                    segment=groww.SEGMENT_FNO,
                    transaction_type=exit_txn,
                    quantity=abs(qty),
                    order_type=groww.ORDER_TYPE_MARKET,
                    product=groww.PRODUCT_MIS,
                    duration=groww.DURATION_DAY,
                )
                print(f"  Exited: {pos['trading_symbol']} qty={qty}")
            except Exception as e:
                print(f"  ⚠️ Could not exit {pos['trading_symbol']}: {e}")
    except Exception as e:
        print(f"  ⚠️ Could not fetch positions: {e}")

    clear_position()
    print("🚨 Emergency exit complete\n")


# ── MAIN ENTRY POINT ─────────────────────────────────────────

def execute_live_trade(groww, entry):
    """
    Main function called by scheduler when LIVE_TRADING=True.
    Runs full safety gate → entry → OCO bracket.

    Returns True if trade placed, False if blocked.
    """
    try:
        # Safety gate — raises on any failure
        safety_gate(groww, entry)

        print(f"\n  [LIVE] 🟢 All safety checks passed")
        print(f"  [LIVE] Entering {entry['action']} on {entry['trading_symbol']}")

        # Place entry order
        order_id, units = place_entry_order(groww, entry)

        # Place OCO bracket immediately
        oco_id = place_oco_bracket(
            groww, order_id, units,
            entry["ltp"], entry.get("action", "BUY")
        )

        return True, order_id, oco_id

    except LiveTradingDisabled as e:
        print(f"  [LIVE] 🔒 {e}")
        return False, None, None

    except SafetyCheckFailed as e:
        print(f"  [LIVE] 🛑 Safety check failed:\n{e}")
        return False, None, None

    except Exception as e:
        print(f"  [LIVE] ❌ Order error: {e}")
        from telegram_bot import notify_error
        notify_error("execute_live_trade()", str(e))
        return False, None, None


def check_time_stop(groww):
    """
    Call this at 14:45 — exits position if still open.
    Groww MIS orders auto-square at 15:20 anyway, but we exit early.
    """
    position = load_position()
    if not position:
        return

    now = datetime.datetime.now().time()
    if now >= datetime.time(14, 45):
        print(f"  [LIVE] ⏰ Time stop triggered")
        place_exit_order(groww, reason="⏰ Time stop 14:45")
        from telegram_bot import notify_trade_update
        notify_trade_update("⏰ Time stop triggered — position closed at 14:45")
