"""
nifty_backtest.py  (Groww Historical API)
==========================================
Same engine as sensex_backtest.py — parameterised for NIFTY/NSE.

Key differences vs SENSEX:
  - Exchange  : NSE  (not BSE)
  - Lot size  : 75   (not 20)
  - ATM step  : 50   (not 100)
  - Expiry    : Tuesday weekly (changed from Thursday, effective Sep 2025)

Fixes vs v1:
  - Section headers correctly say NIFTY
  - Detects expiry day and automatically steps back to the previous
    trading session so the backtest always runs on a normal trading day
  - Retry with backoff on 429 rate-limit errors
  - Increased pause between contract fetches to reduce 429s

Run:   python3 nifty_backtest.py
Deps:  only 'requests' — already installed if you run the bot.
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

UNDERLYING    = "NIFTY"
EXCHANGE      = "NSE"
SEGMENT_CASH  = "CASH"
SEGMENT_FNO   = "FNO"
TRADE_START   = datetime.time(9, 45)
TRADE_END     = datetime.time(14, 0)
TIME_STOP     = datetime.time(14, 45)
N_STRIKES     = 5
BASE_URL      = "https://api.groww.in/v1"

# NIFTY expires on Tuesday (weekday=1) — changed from Thursday effective Sep 2025
EXPIRY_WEEKDAY = 1


# ── Helpers ───────────────────────────────────────────────────

def sep(char="─", n=62): print(char * n)
def header(t): sep("═"); print(f"  {t}"); sep("═")
def section(t): print(f"\n{'─'*62}\n  {t}\n{'─'*62}")


def groww_get(endpoint, params, token, retries=3):
    """Authenticated GET with retry on 429 rate-limit."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-API-VERSION": "1.0",
        "Accept":        "application/json",
    }
    for attempt in range(retries):
        resp = requests.get(f"{BASE_URL}{endpoint}", params=params,
                            headers=headers, timeout=15)
        if resp.status_code == 429:
            wait = 2 ** attempt   # 1s, 2s, 4s
            print(f"    ⏳ Rate limited — waiting {wait}s before retry...")
            _time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "SUCCESS":
            raise ValueError(f"API error: {data}")
        return data["payload"]
    raise Exception(f"Failed after {retries} retries (persistent 429)")


def last_non_expiry_trading_day():
    """
    Step back from today until we find a weekday that is:
      - Not today (we want historical data)
      - Not a weekend
      - Not NIFTY expiry day (Tuesday, post Sep-2025)
    This ensures the backtest runs on a normal mid-week session.
    """
    d = datetime.date.today() - datetime.timedelta(days=1)
    while True:
        if d.weekday() < 5 and d.weekday() != EXPIRY_WEEKDAY:
            return d
        d -= datetime.timedelta(days=1)


def ts_to_time(ts_str):
    return datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S").time()


def build_groww_symbol(exchange, underlying, expiry_str, strike, opt_type):
    """NSE-NIFTY-20Mar26-23500-CE"""
    dt  = datetime.datetime.strptime(expiry_str, "%Y-%m-%d")
    exp = dt.strftime("%d%b%y")
    return f"{exchange}-{underlying}-{exp}-{strike}-{opt_type}"


# ── Step 1: Find active expiry ────────────────────────────────

def find_active_expiry(token, trade_date):
    section("1. Finding active NIFTY expiry for trade date")
    expiries = []
    for offset in [0, 1]:
        m = ((trade_date.month - 1 + offset) % 12) + 1
        y = trade_date.year + ((trade_date.month - 1 + offset) // 12)
        payload  = groww_get("/historical/expiries", {
            "exchange":          EXCHANGE,
            "underlying_symbol": UNDERLYING,
            "year": y, "month": m,
        }, token)
        expiries += payload.get("expiries", [])

    future = sorted(e for e in expiries
                    if datetime.datetime.strptime(e, "%Y-%m-%d").date() >= trade_date)
    if not future:
        print("  ❌ No valid expiry found.")
        sys.exit(1)

    expiry      = future[0]
    expiry_date = datetime.datetime.strptime(expiry, "%Y-%m-%d").date()
    days_to_exp = (expiry_date - trade_date).days

    print(f"  ✅ Trade date  : {trade_date}  ({trade_date.strftime('%A')})")
    print(f"  ✅ Expiry      : {expiry}  ({days_to_exp} calendar days away)")

    if days_to_exp == 0:
        # Should not happen because last_non_expiry_trading_day() skips Tuesdays,
        # but guard anyway in case of a special expiry.
        print(f"  ⚠️  Trade date is expiry day — chain will be 0-DTE.")
        print(f"     Results may be unreliable. Consider running on a different date.")

    return expiry, days_to_exp


# ── Step 2: Spot candles ──────────────────────────────────────

def fetch_spot_candles(token, trade_date):
    section("2. Fetching NIFTY spot candles (5-min)")
    date_str = trade_date.strftime("%Y-%m-%d")
    payload  = groww_get("/historical/candles", {
        "exchange":        EXCHANGE,
        "segment":         SEGMENT_CASH,
        "groww_symbol":    f"{EXCHANGE}-{UNDERLYING}",
        "start_time":      f"{date_str} 09:15:00",
        "end_time":        f"{date_str} 15:30:00",
        "candle_interval": "5minute",
    }, token)

    candles = payload.get("candles", [])
    if not candles:
        print("  ❌ No spot candles — possible market holiday?")
        sys.exit(1)

    bars = [{"time": ts_to_time(c[0]), "open": c[1],
              "high": c[2], "low": c[3], "close": c[4], "vol": c[5]}
            for c in candles]

    d_open  = bars[0]["open"]
    d_high  = max(b["high"]  for b in bars)
    d_low   = min(b["low"]   for b in bars)
    d_close = bars[-1]["close"]
    print(f"  ✅ {len(bars)} bars  |  O:{d_open:,.0f}  H:{d_high:,.0f}  "
          f"L:{d_low:,.0f}  C:{d_close:,.0f}  ({(d_close/d_open-1)*100:+.2f}%)")
    return bars


# ── Step 3: Option candles ────────────────────────────────────

def fetch_option_candles(token, trade_date, expiry, spot_bars):
    section("3. Fetching NIFTY option candles from Groww (NSE FNO)")

    all_spots  = [b["close"] for b in spot_bars]
    mid_spot   = (max(all_spots) + min(all_spots)) / 2
    rounding   = ATM_ROUNDING[UNDERLYING]
    centre_atm = round(mid_spot / rounding) * rounding
    strike_range = [centre_atm + i * rounding
                    for i in range(-(N_STRIKES + 2), N_STRIKES + 3)]

    print(f"  Centre ATM : {centre_atm:,}  |  ±{N_STRIKES+2} strikes = {len(strike_range)} levels")

    contracts_payload = groww_get("/historical/contracts", {
        "exchange":          EXCHANGE,
        "underlying_symbol": UNDERLYING,
        "expiry_date":       expiry,
    }, token)
    available = set(contracts_payload.get("contracts", []))
    print(f"  Contracts available for expiry: {len(available)}")

    date_str = trade_date.strftime("%Y-%m-%d")
    wanted   = []
    for strike in strike_range:
        for opt_type in ["CE", "PE"]:
            sym = build_groww_symbol(EXCHANGE, UNDERLYING, expiry, strike, opt_type)
            if sym in available:
                wanted.append((strike, opt_type, sym))

    if not wanted:
        print("  ❌ No matching contracts in strike range — check symbol format.")
        # Print a sample of available symbols to help debug
        sample = list(available)[:5]
        print(f"  Sample available symbols: {sample}")
        sys.exit(1)
    print(f"  Fetching candles for {len(wanted)} contracts...")

    contract_data = {}
    for i, (strike, opt_type, sym) in enumerate(wanted):
        try:
            payload = groww_get("/historical/candles", {
                "exchange":        EXCHANGE,
                "segment":         SEGMENT_FNO,
                "groww_symbol":    sym,
                "start_time":      f"{date_str} 09:15:00",
                "end_time":        f"{date_str} 15:30:00",
                "candle_interval": "5minute",
            }, token)
            bars = []
            for c in payload.get("candles", []):
                bars.append({
                    "time":   ts_to_time(c[0]),
                    "open":   c[1], "high": c[2],
                    "low":    c[3], "close": c[4],
                    "volume": c[5],
                    "oi":     c[6] if c[6] is not None else 0,
                    "strike": strike,
                    "type":   opt_type,
                })
            contract_data[sym] = bars
        except Exception as e:
            print(f"    ⚠️  {sym}: {e}")
        # Longer pause to avoid 429s — NIFTY has more contracts than SENSEX
        _time.sleep(0.5)

    print(f"  ✅ Candles loaded for {len(contract_data)}/{len(wanted)} contracts")
    return contract_data, wanted


def chain_at_bar(bar_time, contract_data, wanted):
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
                "strike":         strike,
                "type":           opt_type,
                "oi":             match["oi"],
                "volume":         match["volume"],
                "ltp":            match["close"],
                "trading_symbol": sym,
            })
    return chain


# ── Step 4: Replay ────────────────────────────────────────────

def replay(spot_bars, contract_data, wanted, vix):
    section("4. Signal replay — 09:45 to 14:00")

    day_open = spot_bars[0]["open"]
    print(f"  Day open: ₹{day_open:,.2f}  |  VIX: {vix}  "
          f"|  Lot: {LOT_SIZES[UNDERLYING]}")
    print()
    print(f"  {'Time':<6} {'Spot':>9} {'ATM':>7}  "
          f"{'OI_SIG':<9} {'PCR':>6}  {'PA':^4}  {'GO':^5}")
    sep()

    signals_fired = []
    vix_ok        = vix <= VIX_MAX

    for bar in spot_bars:
        t = bar["time"]
        if not (TRADE_START <= t <= TRADE_END):
            continue

        spot  = bar["close"]
        atm   = get_atm_strike(spot, UNDERLYING)
        chain = chain_at_bar(t, contract_data, wanted)
        if not chain:
            continue

        pcr = compute_pcr(chain)

        bear_ok, _ = check_oi_dominance(chain, "BEARISH", atm)
        bull_ok, _ = check_oi_dominance(chain, "BULLISH", atm)

        if bear_ok and not bull_ok:
            oi_signal = "BEARISH"
        elif bull_ok and not bear_ok:
            oi_signal = "BULLISH"
        else:
            oi_signal = "NONE"

        if oi_signal == "BEARISH":
            pcr_ok = pcr < PCR_BEARISH_THRESHOLD
            pa_ok  = spot < day_open
        elif oi_signal == "BULLISH":
            pcr_ok = pcr > PCR_BULLISH_THRESHOLD
            pa_ok  = spot > day_open
        else:
            pcr_ok = pa_ok = False

        go = vix_ok and oi_signal != "NONE" and pcr_ok and pa_ok

        if go:
            sym, ltp = find_first_itm_strike(chain, atm, oi_signal, UNDERLYING)
            if sym and ltp > 0:
                units = _round_to_lot(calc_units(ltp), UNDERLYING)
                signals_fired.append({
                    "time": t, "spot": spot, "atm": atm,
                    "direction": oi_signal, "symbol": sym,
                    "ltp": ltp, "units": units, "pcr": pcr,
                })

        oi_str  = oi_signal if oi_signal != "NONE" else "·"
        pcr_str = f"{pcr:.2f}{'✅' if pcr_ok else '❌'}"
        pa_str  = "✅" if pa_ok else "❌"
        go_str  = "✅ GO" if go else "·"
        print(f"  {t.strftime('%H:%M'):<6} ₹{spot:>8,.0f} {atm:>7,}  "
              f"{oi_str:<9} {pcr_str:>7}  {pa_str:^4}  {go_str}")

    return signals_fired


# ── Step 5: Simulate ──────────────────────────────────────────

def simulate(signals_fired, spot_bars, contract_data, wanted):
    section("5. Trade simulation (actual Groww premium candles)")

    if not signals_fired:
        print("  No GO signals fired in the window.")
        return

    if len(signals_fired) > 1:
        print(f"  {len(signals_fired)} signals — trading only the first.")
        for s in signals_fired[1:]:
            print(f"    Suppressed: {s['time'].strftime('%H:%M')} "
                  f"{s['direction']} {s['symbol']}")
        print()

    sig   = signals_fired[0]
    entry = sig["ltp"]
    units = sig["units"]
    sym   = sig["symbol"]
    sl    = round(entry * (1 - STOP_LOSS_PCT),    2)
    tgt   = round(entry * (1 + PARTIAL_EXIT_PCT), 2)

    print(f"  Signal : {sig['time'].strftime('%H:%M')}  {sig['direction']}")
    print(f"  Symbol : {sym}")
    print(f"  Entry  : ₹{entry}  |  Units: {units}  |  Lot: {LOT_SIZES[UNDERLYING]}")
    print(f"  SL     : ₹{sl}  (-{STOP_LOSS_PCT*100:.0f}%)")
    print(f"  Target : ₹{tgt}  (+{PARTIAL_EXIT_PCT*100:.0f}%)  [50% partial exit]")
    print()

    opt_bars = contract_data.get(sym, [])
    if not opt_bars:
        print(f"  ⚠️  No candle data for {sym}.")
        return

    print(f"  {'Time':<6} {'Spot':>9} {'Prem':>7}  {'OI':>10}  Event")
    sep()

    spot_by_time = {b["time"]: b["close"] for b in spot_bars}
    partial_hit  = False
    partial_pnl  = 0.0
    remaining    = units
    half         = units // 2
    trail_sl     = sl
    outcome      = None

    for ob in opt_bars:
        t        = ob["time"]
        ltp      = ob["close"]
        oi       = ob["oi"]
        spot_now = spot_by_time.get(t, 0)

        if t < sig["time"]:
            continue

        event = ""

        if t >= TIME_STOP:
            rem_pnl   = round(remaining * (ltp - entry), 2)
            total_pnl = round(partial_pnl + rem_pnl, 2)
            event     = f"⏰ Time stop @ ₹{ltp}"
            print(f"  {t.strftime('%H:%M'):<6} ₹{spot_now:>8,.0f} "
                  f"  ₹{ltp:>6.2f}  {oi:>10,}  {event}")
            outcome = ("TIME_STOP", ltp, total_pnl)
            break

        if ltp <= trail_sl:
            rem_pnl   = round(remaining * (ltp - entry), 2)
            total_pnl = round(partial_pnl + rem_pnl, 2)
            event     = f"🛑 SL hit @ ₹{ltp}"
            print(f"  {t.strftime('%H:%M'):<6} ₹{spot_now:>8,.0f} "
                  f"  ₹{ltp:>6.2f}  {oi:>10,}  {event}")
            outcome = ("SL", ltp, total_pnl)
            break

        if not partial_hit and ltp >= tgt:
            partial_pnl = round(half * (ltp - entry), 2)
            remaining   = units - half
            trail_sl    = entry
            partial_hit = True
            event       = (f"💰 Partial exit {half} units @ ₹{ltp} "
                           f"| locked ₹{partial_pnl} | SL→breakeven")

        if partial_hit:
            new_trail = round(ltp * (1 - TRAIL_STOP_PCT), 2)
            if new_trail > trail_sl:
                trail_sl = new_trail

        if partial_hit and ltp <= trail_sl and not event:
            rem_pnl   = round(remaining * (ltp - entry), 2)
            total_pnl = round(partial_pnl + rem_pnl, 2)
            event     = f"📉 Trail SL hit @ ₹{ltp}"
            print(f"  {t.strftime('%H:%M'):<6} ₹{spot_now:>8,.0f} "
                  f"  ₹{ltp:>6.2f}  {oi:>10,}  {event}")
            outcome = ("TRAIL_SL", ltp, total_pnl)
            break

        trail_note = f"  trail_sl=₹{trail_sl:.2f}" if partial_hit else ""
        print(f"  {t.strftime('%H:%M'):<6} ₹{spot_now:>8,.0f} "
              f"  ₹{ltp:>6.2f}  {oi:>10,}  {event}{trail_note}")

    section("6. P&L Summary")
    if not outcome:
        print("  Trade open at end of available data.")
        return

    reason, exit_price, total_pnl = outcome
    emoji = "✅ PROFIT" if total_pnl > 0 else "❌ LOSS"
    print(f"  Result     : {emoji}")
    print(f"  Exit type  : {reason}")
    print(f"  Entry      : ₹{entry}  →  Exit: ₹{exit_price}")
    print(f"  Units      : {units}  (lot size {LOT_SIZES[UNDERLYING]})")
    print(f"  Net P&L    : ₹{total_pnl:+,.2f}")
    if partial_hit:
        print(f"  Partial locked : ₹{partial_pnl:+,.2f}")
    print(f"  Capital at risk was: ₹{round(units * entry * STOP_LOSS_PCT):,}")


# ── MAIN ─────────────────────────────────────────────────────

def main():
    header("ZERO HERO — NIFTY GENUINE BACKTEST (Groww Historical API)")

    section("0. Connecting to Groww")
    token = get_access_token()
    groww = GrowwAPI(token)
    vix   = get_india_vix(groww)
    print(f"  ✅ Connected  |  Current VIX: {vix}")

    # Skip expiry day (Tuesday post Sep-2025) — bot doesn't trade on expiry
    trade_date = last_non_expiry_trading_day()
    print(f"\n  📅 Backtesting on: {trade_date}  ({trade_date.strftime('%A')})")
    print(f"     (Automatically skipped Tuesday expiry days — post Sep 2025 change)")

    expiry, _             = find_active_expiry(token, trade_date)
    spot_bars             = fetch_spot_candles(token, trade_date)
    contract_data, wanted = fetch_option_candles(token, trade_date, expiry, spot_bars)
    signals_fired         = replay(spot_bars, contract_data, wanted, vix)
    simulate(signals_fired, spot_bars, contract_data, wanted)

    sep("═")
    print("  Backtest complete.")
    sep("═")


if __name__ == "__main__":
    main()
