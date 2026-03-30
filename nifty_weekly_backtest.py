"""
nifty_weekly_backtest.py
========================
Runs the Zero Hero NIFTY strategy across every valid trading day
last week (Mon, Wed–Fri, skipping Tuesday expiry).

Output: one clean P&L table. No bar-by-bar noise.
"""

import datetime
import sys
import time as _time
import requests

from auth import get_access_token
from growwapi import GrowwAPI
from engine import (
    compute_pcr, check_oi_dominance, find_first_itm_strike,
    get_atm_strike, get_india_vix,
)
from config import (
    VIX_MAX, PCR_BEARISH_THRESHOLD, PCR_BULLISH_THRESHOLD,
    STOP_LOSS_PCT, PARTIAL_EXIT_PCT, TRAIL_STOP_PCT,
    LOT_SIZES, ATM_ROUNDING,
)
from paper_trader import calc_units, _round_to_lot

UNDERLYING   = "NIFTY"
EXCHANGE     = "NSE"
SEGMENT_CASH = "CASH"
SEGMENT_FNO  = "FNO"
TRADE_START  = datetime.time(9, 45)
TRADE_END    = datetime.time(14, 0)
TIME_STOP    = datetime.time(14, 45)
N_STRIKES    = 5
BASE_URL     = "https://api.groww.in/v1"


# ── API ───────────────────────────────────────────────────────

def groww_get(endpoint, params, token, retries=4):
    headers = {
        "Authorization": f"Bearer {token}",
        "X-API-VERSION": "1.0",
        "Accept":        "application/json",
    }
    for attempt in range(retries):
        try:
            resp = requests.get(f"{BASE_URL}{endpoint}", params=params,
                                headers=headers, timeout=15)
            if resp.status_code == 429:
                wait = 2 ** attempt
                _time.sleep(wait)
                continue
            if resp.status_code == 500:
                return None   # server error on this contract — skip silently
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "SUCCESS":
                return None
            return data["payload"]
        except Exception:
            _time.sleep(1)
    return None


def ts_to_time(ts_str):
    return datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").time()


def build_sym(strike, opt_type, expiry_str):
    dt  = datetime.datetime.strptime(expiry_str, "%Y-%m-%d")
    exp = dt.strftime("%d%b%y")
    return f"{EXCHANGE}-{UNDERLYING}-{exp}-{strike}-{opt_type}"


# ── Date helpers ──────────────────────────────────────────────

def last_week_trading_days():
    """
    Return Mon–Fri of last week, excluding Tuesday (NIFTY expiry post Sep-2025).
    If today is Mon, 'last week' = the Mon–Fri 7 days ago.
    """
    today     = datetime.date.today()
    # Go back to last Monday
    days_since_monday = today.weekday()
    last_monday = today - datetime.timedelta(days=days_since_monday + 7)
    days = []
    for i in range(5):   # Mon=0 … Fri=4
        d = last_monday + datetime.timedelta(days=i)
        if d.weekday() == 1:   # skip Tuesday (NIFTY expiry post Sep 2025)
            continue
        days.append(d)
    return days


# ── Per-day data fetch ────────────────────────────────────────

def get_nearest_expiry(token, trade_date):
    expiries = []
    for offset in [0, 1]:
        m = ((trade_date.month - 1 + offset) % 12) + 1
        y = trade_date.year + ((trade_date.month - 1 + offset) // 12)
        payload = groww_get("/historical/expiries", {
            "exchange": EXCHANGE, "underlying_symbol": UNDERLYING,
            "year": y, "month": m,
        }, token)
        if payload:
            expiries += payload.get("expiries", [])
    future = sorted(e for e in expiries
                    if datetime.datetime.strptime(e, "%Y-%m-%d").date() >= trade_date)
    return future[0] if future else None


def fetch_spot_bars(token, trade_date):
    date_str = trade_date.strftime("%Y-%m-%d")
    payload  = groww_get("/historical/candles", {
        "exchange": EXCHANGE, "segment": SEGMENT_CASH,
        "groww_symbol": f"{EXCHANGE}-{UNDERLYING}",
        "start_time": f"{date_str} 09:15:00",
        "end_time":   f"{date_str} 15:30:00",
        "candle_interval": "5minute",
    }, token)
    if not payload:
        return []
    return [{"time": ts_to_time(c[0]), "open": c[1], "high": c[2],
              "low": c[3], "close": c[4]} for c in payload.get("candles", [])]


def fetch_contracts(token, expiry):
    payload = groww_get("/historical/contracts", {
        "exchange": EXCHANGE, "underlying_symbol": UNDERLYING,
        "expiry_date": expiry,
    }, token)
    return set(payload.get("contracts", [])) if payload else set()


def fetch_option_bars(token, trade_date, expiry, spot_bars):
    all_spots  = [b["close"] for b in spot_bars]
    mid        = (max(all_spots) + min(all_spots)) / 2
    step       = ATM_ROUNDING[UNDERLYING]
    centre     = round(mid / step) * step
    strikes    = [centre + i * step for i in range(-(N_STRIKES+2), N_STRIKES+3)]

    available  = fetch_contracts(token, expiry)
    date_str   = trade_date.strftime("%Y-%m-%d")

    wanted         = []
    contract_data  = {}

    for strike in strikes:
        for opt_type in ["CE", "PE"]:
            sym = build_sym(strike, opt_type, expiry)
            if sym in available:
                wanted.append((strike, opt_type, sym))

    for i, (strike, opt_type, sym) in enumerate(wanted):
        payload = groww_get("/historical/candles", {
            "exchange": EXCHANGE, "segment": SEGMENT_FNO,
            "groww_symbol": sym,
            "start_time": f"{date_str} 09:15:00",
            "end_time":   f"{date_str} 15:30:00",
            "candle_interval": "5minute",
        }, token)
        if payload:
            bars = []
            for c in payload.get("candles", []):
                bars.append({
                    "time":   ts_to_time(c[0]),
                    "close":  c[4],
                    "volume": c[5],
                    "oi":     c[6] if c[6] is not None else 0,
                    "strike": strike,
                    "type":   opt_type,
                })
            contract_data[sym] = bars
        _time.sleep(0.5)

    return contract_data, wanted


def chain_at(bar_time, contract_data, wanted):
    chain = []
    for (strike, opt_type, sym) in wanted:
        bars  = contract_data.get(sym, [])
        match = None
        for b in bars:
            if b["time"] <= bar_time:
                match = b
            else:
                break
        if match:
            chain.append({
                "strike": strike, "type": opt_type,
                "oi": match["oi"], "volume": match["volume"],
                "ltp": match["close"], "trading_symbol": sym,
            })
    return chain


# ── Single-day signal + simulation ───────────────────────────

def run_day(token, trade_date, vix):
    """
    Returns a result dict:
      signal_time, direction, symbol, entry, exit_price,
      exit_reason, units, pnl, went_live (bool)
    or None if no signal fired.
    """
    expiry    = get_nearest_expiry(token, trade_date)
    if not expiry:
        return {"error": "No expiry found"}

    spot_bars = fetch_spot_bars(token, trade_date)
    if not spot_bars:
        return {"error": "No spot data (holiday?)"}

    contract_data, wanted = fetch_option_bars(token, trade_date, expiry, spot_bars)
    if not contract_data:
        return {"error": "No option data"}

    day_open      = spot_bars[0]["open"]
    vix_ok        = vix <= VIX_MAX
    first_signal  = None

    # ── Find first valid signal bar ───────────────────────────
    for bar in spot_bars:
        t = bar["time"]
        if not (TRADE_START <= t <= TRADE_END):
            continue

        spot  = bar["close"]
        atm   = get_atm_strike(spot, UNDERLYING)
        chain = chain_at(t, contract_data, wanted)
        if not chain:
            continue

        pcr = compute_pcr(chain)
        bear_ok, _ = check_oi_dominance(chain, "BEARISH", atm)
        bull_ok, _ = check_oi_dominance(chain, "BULLISH", atm)

        if bear_ok and not bull_ok:
            oi_sig = "BEARISH"
        elif bull_ok and not bear_ok:
            oi_sig = "BULLISH"
        else:
            oi_sig = "NONE"

        if oi_sig == "BEARISH":
            pcr_ok = pcr < PCR_BEARISH_THRESHOLD
            pa_ok  = spot < day_open
        elif oi_sig == "BULLISH":
            pcr_ok = pcr > PCR_BULLISH_THRESHOLD
            pa_ok  = spot > day_open
        else:
            pcr_ok = pa_ok = False

        if vix_ok and oi_sig != "NONE" and pcr_ok and pa_ok:
            sym, ltp = find_first_itm_strike(chain, atm, oi_sig, UNDERLYING)
            if sym and ltp > 0:
                units = _round_to_lot(calc_units(ltp), UNDERLYING)
                first_signal = {
                    "time": t, "spot": spot, "atm": atm,
                    "direction": oi_sig, "symbol": sym,
                    "ltp": ltp, "units": units, "pcr": pcr,
                    "expiry": expiry,
                }
                break

    if not first_signal:
        return {"went_live": False, "reason": "No signal"}

    # ── Simulate the trade ────────────────────────────────────
    sig   = first_signal
    entry = sig["ltp"]
    units = sig["units"]
    sym   = sig["symbol"]
    sl    = round(entry * (1 - STOP_LOSS_PCT),    2)
    tgt   = round(entry * (1 + PARTIAL_EXIT_PCT), 2)

    opt_bars = contract_data.get(sym, [])
    if not opt_bars:
        return {"went_live": False, "reason": f"No bars for {sym}"}

    spot_by_time = {b["time"]: b["close"] for b in spot_bars}
    partial_hit  = False
    partial_pnl  = 0.0
    remaining    = units
    half         = units // 2
    trail_sl     = sl
    outcome      = None

    for ob in opt_bars:
        t   = ob["time"]
        ltp = ob["close"]
        if t < sig["time"]:
            continue

        if t >= TIME_STOP:
            rem       = round(remaining * (ltp - entry), 2)
            outcome   = ("TIME_STOP", ltp, round(partial_pnl + rem, 2))
            break

        if ltp <= trail_sl:
            rem     = round(remaining * (ltp - entry), 2)
            outcome = ("SL_HIT", ltp, round(partial_pnl + rem, 2))
            break

        if not partial_hit and ltp >= tgt:
            partial_pnl = round(half * (ltp - entry), 2)
            remaining   = units - half
            trail_sl    = entry
            partial_hit = True

        if partial_hit:
            new_trail = round(ltp * (1 - TRAIL_STOP_PCT), 2)
            if new_trail > trail_sl:
                trail_sl = new_trail

        if partial_hit and ltp <= trail_sl:
            rem     = round(remaining * (ltp - entry), 2)
            outcome = ("TRAIL_SL", ltp, round(partial_pnl + rem, 2))
            break

    if not outcome:
        outcome = ("NO_EXIT", entry, 0.0)

    _, exit_price, total_pnl = outcome

    return {
        "went_live":   True,
        "date":        trade_date,
        "signal_time": sig["time"].strftime("%H:%M"),
        "direction":   sig["direction"],
        "symbol":      sym,
        "expiry":      expiry,
        "entry":       entry,
        "exit_price":  exit_price,
        "exit_reason": outcome[0],
        "units":       units,
        "pnl":         total_pnl,
        "partial_hit": partial_hit,
        "partial_pnl": partial_pnl,
        "capital_at_risk": round(units * entry * STOP_LOSS_PCT),
    }


# ── Main ──────────────────────────────────────────────────────

def main():
    print("\n" + "═"*65)
    print("  ZERO HERO — NIFTY WEEKLY BACKTEST")
    print("  Strategy: OI dominance + PCR + Price Action | 1% risk/trade")
    print("═"*65)

    token = get_access_token()
    groww = GrowwAPI(token)
    vix   = get_india_vix(groww)

    days    = last_week_trading_days()
    week_str = f"{days[0].strftime('%d %b')} – {days[-1].strftime('%d %b %Y')}"
    print(f"\n  Week     : {week_str}  (Thu expiry day excluded)")
    print(f"  Live VIX : {vix}  ({'✅ within limit' if vix <= VIX_MAX else '❌ above limit — all days would skip'})")
    print(f"  Days     : {', '.join(d.strftime('%a %d %b') for d in days)}\n")

    results    = []
    day_num    = 0

    for d in days:
        day_num += 1
        print(f"  [{day_num}/{len(days)}] {d.strftime('%A %d %b')} — fetching...", end="", flush=True)
        r = run_day(token, d, vix)
        r["date"] = d
        results.append(r)
        if r.get("went_live"):
            print(f" signal {r['signal_time']} {r['direction'][:4]}  "
                  f"₹{r['pnl']:+,.0f}")
        else:
            print(f" {r.get('reason', 'no signal')}")

    # ── Summary table ─────────────────────────────────────────
    print("\n" + "═"*65)
    print("  WEEKLY P&L SUMMARY — NIFTY")
    print("═"*65)
    print(f"  {'Date':<14} {'Day':<5} {'Signal':>7} {'Dir':<9} {'Entry':>7} "
          f"{'Exit':>7} {'Exit type':<12} {'Units':>5} {'P&L':>9}")
    print("  " + "─"*63)

    total_pnl        = 0
    total_risk       = 0
    trades_taken     = 0
    wins             = 0
    losses           = 0

    for r in results:
        date_str = r["date"].strftime("%d %b %Y")
        day_name = r["date"].strftime("%a")

        if not r.get("went_live"):
            reason = r.get("reason", "–")[:18]
            print(f"  {date_str:<14} {day_name:<5} {'–':>7}  {'–':<9} "
                  f"{'–':>7} {'–':>7} {reason:<12}")
            continue

        pnl_str  = f"₹{r['pnl']:+,.0f}"
        emoji    = "✅" if r["pnl"] > 0 else "❌"
        exit_lbl = {
            "TIME_STOP": "Time stop",
            "SL_HIT":    "SL hit",
            "TRAIL_SL":  "Trail SL",
            "NO_EXIT":   "No exit",
        }.get(r["exit_reason"], r["exit_reason"])

        print(f"  {date_str:<14} {day_name:<5} {r['signal_time']:>7}  "
              f"{r['direction']:<9} ₹{r['entry']:>6,.0f} ₹{r['exit_price']:>6,.0f} "
              f"{exit_lbl:<12} {r['units']:>5}  {emoji} {pnl_str}")

        total_pnl  += r["pnl"]
        total_risk += r["capital_at_risk"]
        trades_taken += 1
        if r["pnl"] > 0:
            wins += 1
        else:
            losses += 1

    print("  " + "─"*63)

    if trades_taken == 0:
        print("  No trades taken this week.")
    else:
        win_rate   = round(wins / trades_taken * 100)
        avg_rr     = round(total_pnl / total_risk, 2) if total_risk > 0 else 0
        week_emoji = "🟢" if total_pnl > 0 else "🔴"

        print(f"\n  {'Trades taken':<22}: {trades_taken}  "
              f"({wins} wins / {losses} losses  —  {win_rate}% win rate)")
        print(f"  {'Total capital risked':<22}: ₹{total_risk:,.0f}")
        print(f"  {'Total P&L':<22}: {week_emoji} ₹{total_pnl:+,.0f}")
        print(f"  {'Return on risk':<22}: {avg_rr:+.2f}x  "
              f"({'profit' if avg_rr > 0 else 'loss'} vs capital risked)")

    print("\n" + "═"*65 + "\n")


if __name__ == "__main__":
    main()
