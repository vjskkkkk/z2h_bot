"""
live/paper_trader.py (v5 — Natenberg improvements)

Changes vs v3:
  - update_trade() now also checks for spot-recross thesis invalidation:
    if the underlying crosses back through day_open, the directional
    thesis is broken and we exit immediately (separate from premium SL).
  - Pending signal state stored in log for 2-bar confirmation (read by scheduler).
  - SL tightened to 25% (in config).
"""

import json
import datetime
import os

from config import (
    CAPITAL, MAX_RISK_PER_TRADE_PCT, DAILY_LOSS_CAP_PCT,
    MAX_TRADES_PER_DAY, STOP_LOSS_PCT, PARTIAL_EXIT_PCT,
    TRAIL_STOP_PCT, TIME_STOP, TRADES_LOG_FILE,
    LOT_SIZES, UNDERLYING,
    SIGNAL_CONFIRM_BARS,
)


def load_log():
    if os.path.exists(TRADES_LOG_FILE):
        with open(TRADES_LOG_FILE, "r") as f:
            return json.load(f)
    return {
        "capital":        CAPITAL,
        "available":      CAPITAL,
        "total_pnl":      0,
        "trades_today":   0,
        "daily_loss":     0,
        "trade_date":     str(datetime.date.today()),
        "open_trade":     None,
        "closed_trades":  [],
        # 2-bar confirmation state
        "pending_signal": None,   # dict with direction, symbol, count
    }


def save_log(log):
    os.makedirs(os.path.dirname(TRADES_LOG_FILE), exist_ok=True)
    with open(TRADES_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2, default=str)


def reset_daily(log):
    today = str(datetime.date.today())
    if log["trade_date"] != today:
        log["trade_date"]     = today
        log["trades_today"]   = 0
        log["daily_loss"]     = 0
        log["pending_signal"] = None   # reset pending signal at day start
    return log


def can_trade(log):
    log = reset_daily(log)
    if log["open_trade"]:
        return False, "❌ Trade already open"
    if log["trades_today"] >= MAX_TRADES_PER_DAY:
        return False, f"❌ Max {MAX_TRADES_PER_DAY} trades/day"
    if log["daily_loss"] <= -(log["capital"] * DAILY_LOSS_CAP_PCT):
        return False, "❌ Daily loss cap hit"
    if log["available"] <= 0:
        return False, "❌ No capital"
    return True, "✅ OK"


def calc_units(premium):
    """Raw units from 1% risk rule — no lot rounding (test suite expects this)."""
    max_loss = CAPITAL * MAX_RISK_PER_TRADE_PCT
    loss_pu  = premium * STOP_LOSS_PCT
    if loss_pu <= 0:
        return 0
    return int(max_loss / loss_pu)


def _round_to_lot(units, underlying):
    lot_size = LOT_SIZES.get(underlying, 65)
    lots     = max(1, units // lot_size)
    return lots * lot_size


# ── 2-BAR CONFIRMATION ────────────────────────────────────────

def check_and_update_pending(log, result):
    """
    Natenberg: wait for confirmation before entering.

    Called by scheduler after each scan.  Returns:
      "ENTER"   — signal has confirmed for SIGNAL_CONFIRM_BARS bars → enter now
      "PENDING" — signal fired but not yet confirmed → wait
      "RESET"   — signal changed or disappeared → clear pending state
      "NONE"    — no signal this bar and no pending → nothing to do

    The pending state is keyed by (underlying, direction) so a direction
    flip or underlying change resets the counter.
    """
    log = reset_daily(log)

    current_go        = result.get("go", False)
    current_signal    = result.get("signal", "NO_SIGNAL")
    current_underlying = result.get("underlying", UNDERLYING)
    pending           = log.get("pending_signal")

    if not current_go:
        # No signal — clear any pending state for this underlying
        if pending and pending.get("underlying") == current_underlying:
            log["pending_signal"] = None
            save_log(log)
            return "RESET", log
        return "NONE", log

    # Signal is live this bar
    key = f"{current_underlying}_{current_signal}"

    if pending and pending.get("key") == key:
        # Same signal as last bar — increment counter
        pending["count"] += 1
        log["pending_signal"] = pending
        save_log(log)
        if pending["count"] >= SIGNAL_CONFIRM_BARS:
            return "ENTER", log
        else:
            return "PENDING", log
    else:
        # New signal or direction changed — start fresh counter
        log["pending_signal"] = {
            "key":        key,
            "underlying": current_underlying,
            "direction":  current_signal,
            "count":      1,
            "entry":      result.get("entry", {}),
        }
        save_log(log)
        return "PENDING", log


def enter_trade(log, entry):
    log        = reset_daily(log)
    action     = entry.get("action", "BUY")
    underlying = entry.get("underlying", UNDERLYING)
    premium    = entry["ltp"]
    units      = _round_to_lot(calc_units(premium), underlying)

    if units == 0:
        return log, "⚠️ Position size = 0"

    if action == "BUY":
        sl_price     = round(premium * (1 - STOP_LOSS_PCT),    2)
        target_price = round(premium * (1 + PARTIAL_EXIT_PCT), 2)
        cost         = round(units * premium, 2)
        trade = {
            "id":             f"T{len(log['closed_trades'])+1:03d}",
            "date":           str(datetime.date.today()),
            "entry_time":     datetime.datetime.now().strftime("%H:%M:%S"),
            "symbol":         entry["trading_symbol"],
            "underlying":     underlying,
            "action":         "BUY",
            "direction":      entry["direction"],
            "expiry":         entry["expiry"],
            "dte_at_entry":   entry.get("dte", 0),
            "iv_rank":        entry.get("iv_rank", 50),
            "day_open":       entry.get("day_open", 0),   # for thesis-invalidation SL
            "entry_price":    premium,
            "units":          units,
            "cost":           cost,
            "sl_price":       sl_price,
            "target_price":   target_price,
            "trailing_sl":    sl_price,
            "partial_exited": False,
            "partial_units":  units // 2,
            "status":         "OPEN",
            "exit_price":     None,
            "exit_time":      None,
            "exit_reason":    None,
            "pnl":            0,
        }
        log["available"] -= cost
    else:
        spread           = entry.get("spread", {})
        premium_received = (spread.get("net_credit", premium)
                            if action == "SELL_SPREAD" else premium)
        sl_price         = round(premium_received * 2, 2)
        trade = {
            "id":              f"T{len(log['closed_trades'])+1:03d}",
            "date":            str(datetime.date.today()),
            "entry_time":      datetime.datetime.now().strftime("%H:%M:%S"),
            "symbol":          entry["trading_symbol"],
            "underlying":      underlying,
            "action":          action,
            "direction":       entry["direction"],
            "expiry":          entry["expiry"],
            "day_open":        entry.get("day_open", 0),
            "entry_price":     premium,
            "premium_received": premium_received,
            "units":           units,
            "cost":            0,
            "sl_price":        sl_price,
            "target_price":    0,
            "trailing_sl":     sl_price,
            "partial_exited":  False,
            "spread":          spread,
            "status":          "OPEN",
            "exit_price":      None,
            "exit_time":       None,
            "exit_reason":     None,
            "pnl":             0,
        }

    log["open_trade"]     = trade
    log["trades_today"]  += 1
    log["pending_signal"] = None   # clear pending after entry
    save_log(log)
    return log, trade


def update_trade(log, current_price, current_spot=None):
    """
    current_spot: live spot price of the underlying (optional).
    If provided, checks thesis-invalidation (spot recrossed day_open).
    """
    trade = log.get("open_trade")
    if not trade:
        return log, None

    now       = datetime.datetime.now()
    time_stop = datetime.datetime.strptime(TIME_STOP, "%H:%M").time()
    action    = trade.get("action", "BUY")

    if now.time() >= time_stop:
        return _close_trade(log, current_price, "⏰ Time stop 14:45")

    if action == "BUY":
        # ── Thesis-invalidation SL (Natenberg: exit when thesis breaks) ──
        day_open  = trade.get("day_open", 0)
        direction = trade.get("direction", "")
        if current_spot and day_open > 0:
            if direction == "BEARISH" and current_spot > day_open:
                return _close_trade(log, current_price,
                                    f"🔄 Thesis invalidated — spot ₹{current_spot:,.0f} "
                                    f"recrossed above day open ₹{day_open:,.0f}")
            elif direction == "BULLISH" and current_spot < day_open:
                return _close_trade(log, current_price,
                                    f"🔄 Thesis invalidated — spot ₹{current_spot:,.0f} "
                                    f"recrossed below day open ₹{day_open:,.0f}")

        # ── Premium SL ────────────────────────────────────────────────────
        if current_price <= trade["sl_price"]:
            return _close_trade(log, current_price,
                                f"🛑 SL hit ₹{current_price:.2f}")

        # ── Trailing SL (after partial exit) ─────────────────────────────
        if trade["partial_exited"]:
            new_trail = round(current_price * (1 - TRAIL_STOP_PCT), 2)
            if new_trail > trade["trailing_sl"]:
                trade["trailing_sl"] = new_trail
                log["open_trade"]    = trade
            if current_price <= trade["trailing_sl"]:
                return _close_trade(log, current_price,
                                    f"📉 Trail SL ₹{trade['trailing_sl']:.2f}")

        # ── Partial exit ──────────────────────────────────────────────────
        if not trade["partial_exited"] and current_price >= trade["target_price"]:
            half     = trade["partial_units"]
            part_pnl = round(half * (current_price - trade["entry_price"]), 2)
            trade["partial_exited"] = True
            trade["pnl"]           += part_pnl
            trade["units"]         -= half
            trade["sl_price"]       = trade["entry_price"]
            trade["trailing_sl"]    = trade["entry_price"]
            log["available"]       += round(half * current_price, 2)
            log["open_trade"]       = trade
            save_log(log)
            return log, (f"💰 Partial exit {half} units @ ₹{current_price:.2f} | "
                         f"Locked ₹{part_pnl:.2f} | SL → breakeven")

    else:   # SELL trades
        premium_received = trade.get("premium_received", trade["entry_price"])
        if current_price >= trade["sl_price"]:
            return _close_trade(log, current_price,
                                f"🛑 Sell SL hit — premium ₹{current_price:.2f}")
        target_exit = round(premium_received * 0.20, 2)
        if current_price <= target_exit:
            return _close_trade(log, current_price,
                                f"🎯 Target — premium decayed to ₹{current_price:.2f}")

    save_log(log)
    return log, None


def _close_trade(log, exit_price, reason):
    trade  = log["open_trade"]
    units  = trade["units"]
    action = trade.get("action", "BUY")

    if action == "BUY":
        remaining_pnl = round(units * (exit_price - trade["entry_price"]), 2)
    else:
        premium_received = trade.get("premium_received", trade["entry_price"])
        remaining_pnl    = round(units * (premium_received - exit_price), 2)

    total_pnl = round(trade["pnl"] + remaining_pnl, 2)
    trade.update({
        "exit_price":  exit_price,
        "exit_time":   datetime.datetime.now().strftime("%H:%M:%S"),
        "exit_reason": reason,
        "pnl":         total_pnl,
        "status":      "CLOSED",
    })

    log["available"]    += round(units * exit_price, 2) if action == "BUY" else 0
    log["total_pnl"]    += total_pnl
    log["daily_loss"]   += min(0, total_pnl)
    log["open_trade"]    = None
    log["closed_trades"].append(trade)
    save_log(log)
    return log, {"type": "CLOSED", "trade": trade, "reason": reason, "pnl": total_pnl}


def get_daily_summary(log):
    today  = str(datetime.date.today())
    trades = [t for t in log["closed_trades"] if t["date"] == today]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    day_pnl     = sum(t["pnl"] for t in trades)
    buy_trades  = [t for t in trades if t.get("action") == "BUY"]
    sell_trades = [t for t in trades if t.get("action") in ("SELL_NAKED", "SELL_SPREAD")]

    lines = [
        f"📊 *Zero Hero Summary — {today}*",
        f"",
        f"💼 Capital  : ₹{log['capital']:,.0f}",
        f"💰 Available: ₹{log['available']:,.2f}",
        f"📈 Total P&L: ₹{log['total_pnl']:+,.2f}",
        f"",
        f"Today: {len(trades)} trades | ✅ {len(wins)} wins | ❌ {len(losses)} losses",
        f"Buy: {len(buy_trades)} | Sell: {len(sell_trades)}",
        f"Day P&L: ₹{day_pnl:+.2f}",
        f"",
    ]
    for t in trades:
        icon    = "✅" if t["pnl"] > 0 else "❌"
        act     = t.get("action", "BUY")
        und     = t.get("underlying", "")
        dte     = t.get("dte_at_entry", "?")
        iv_rank = t.get("iv_rank", "?")
        und_tag = f"[{und}] " if und else ""
        lines.append(
            f"{icon} {t['id']} {und_tag}[{act}] {t['symbol']} "
            f"DTE:{dte} IVRank:{iv_rank}% | "
            f"₹{t['entry_price']} → ₹{t.get('exit_price','?')} | "
            f"P&L ₹{t['pnl']:+.2f} | {t.get('exit_reason','')}"
        )
    return "\n".join(lines)
