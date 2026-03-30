"""
backtest_core.py
================
Shared logic for nifty_monthly_backtest_v5.py and sensex_monthly_backtest_v5.py.
Implements all Natenberg-based improvements from engine/paper_trader v5:

  1. Smart expiry selection  — skips expiries with DTE < MIN_DAYS_TO_EXPIRY
  2. Volume dominance ratio  — OI_VOL_DOMINANCE_RATIO (1.5x required)
  3. 2-bar confirmation      — signal must fire on 2 consecutive bars
  4. Tighter SL              — 25% (was 35%)
  5. Thesis-invalidation SL  — exit if spot recrosses day open
  6. DTE stored on trade     — shown in summary table
"""

import datetime
import time as _time
import requests

from config import (
    PCR_BEARISH_THRESHOLD, PCR_BULLISH_THRESHOLD,
    STOP_LOSS_PCT, PARTIAL_EXIT_PCT, TRAIL_STOP_PCT,
    LOT_SIZES, ATM_ROUNDING,
    MIN_DAYS_TO_EXPIRY, MAX_DAYS_TO_EXPIRY, EXPIRY_CANDIDATES,
    OI_VOL_DOMINANCE_RATIO,
    SIGNAL_CONFIRM_BARS,
)
from engine import get_atm_strike, compute_pcr
from paper_trader import calc_units, _round_to_lot

BASE_URL     = "https://api.groww.in/v1"
TRADE_START  = datetime.time(9, 45)
TRADE_END    = datetime.time(14, 0)
TIME_STOP    = datetime.time(14, 45)
N_STRIKES    = 5


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
                _time.sleep(2 ** attempt)
                continue
            if resp.status_code in (500, 502, 503):
                return None
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


def build_sym(exchange, underlying, expiry_str, strike, opt_type):
    """Zero-pads day: 06Feb26, not 6Feb26."""
    dt  = datetime.datetime.strptime(expiry_str, "%Y-%m-%d")
    exp = dt.strftime("%d%b%y")
    return f"{exchange}-{underlying}-{exp}-{strike}-{opt_type}"


# ── Smart expiry selection (v5) ───────────────────────────────

def get_best_expiry_historical(token, exchange, underlying, trade_date):
    """
    Evaluate up to EXPIRY_CANDIDATES upcoming expiries (as of trade_date)
    and return the first whose DTE falls in [MIN_DAYS_TO_EXPIRY, MAX_DAYS_TO_EXPIRY].
    Falls back to nearest future expiry if nothing fits the window.
    """
    all_expiries = []
    for offset in [0, 1]:
        m = ((trade_date.month - 1 + offset) % 12) + 1
        y = trade_date.year + ((trade_date.month - 1 + offset) // 12)
        payload = groww_get("/historical/expiries", {
            "exchange": exchange, "underlying_symbol": underlying,
            "year": y, "month": m,
        }, token)
        if payload:
            all_expiries += payload.get("expiries", [])

    future = sorted(
        e for e in all_expiries
        if datetime.datetime.strptime(e, "%Y-%m-%d").date() >= trade_date
    )
    if not future:
        return None, 0

    fallback = future[0]
    for exp in future[:EXPIRY_CANDIDATES]:
        dte = (datetime.datetime.strptime(exp, "%Y-%m-%d").date() - trade_date).days
        if MIN_DAYS_TO_EXPIRY <= dte <= MAX_DAYS_TO_EXPIRY:
            return exp, dte

    # Nothing in window — return nearest as fallback with its DTE
    dte = (datetime.datetime.strptime(fallback, "%Y-%m-%d").date() - trade_date).days
    return fallback, dte


# ── OI dominance with volume ratio (v5) ──────────────────────

def check_oi_dominance_v5(chain, signal_type, atm_strike, n_strikes=5):
    """
    Like engine.check_oi_dominance() but uses OI_VOL_DOMINANCE_RATIO.
    CE vol must exceed PE vol × ratio (not just > PE vol).
    """
    step   = 50   # fixed step for nearby filter
    nearby = [o for o in chain if abs(o["strike"] - atm_strike) <= n_strikes * step]

    ce_nearby = [o for o in nearby if o["type"] == "CE"]
    pe_nearby = [o for o in nearby if o["type"] == "PE"]
    ce_oi  = sum(o["oi"]     for o in ce_nearby)
    pe_oi  = sum(o["oi"]     for o in pe_nearby)
    ce_vol = sum(o["volume"] for o in ce_nearby)
    pe_vol = sum(o["volume"] for o in pe_nearby)

    total_oi = ce_oi + pe_oi
    if total_oi == 0:
        return False, "NONE"

    ce_pct = ce_oi / total_oi * 100
    pe_pct = pe_oi / total_oi * 100

    if signal_type == "BEARISH":
        detected = ce_pct >= 60 and ce_vol > (pe_vol * OI_VOL_DOMINANCE_RATIO)
    else:
        detected = pe_pct >= 60 and pe_vol > (ce_vol * OI_VOL_DOMINANCE_RATIO)

    direction = "BEARISH" if signal_type == "BEARISH" else "BULLISH"
    return detected, direction if detected else "NONE"


# ── Data fetchers ─────────────────────────────────────────────

def fetch_spot_bars(token, exchange, underlying, trade_date):
    date_str = trade_date.strftime("%Y-%m-%d")
    payload  = groww_get("/historical/candles", {
        "exchange": exchange, "segment": "CASH",
        "groww_symbol": f"{exchange}-{underlying}",
        "start_time": f"{date_str} 09:15:00",
        "end_time":   f"{date_str} 15:30:00",
        "candle_interval": "5minute",
    }, token)
    if not payload:
        return []
    return [{"time": ts_to_time(c[0]), "open": c[1],
              "close": c[4]} for c in payload.get("candles", [])]


def fetch_option_bars(token, exchange, underlying, trade_date, expiry, spot_bars):
    all_spots = [b["close"] for b in spot_bars]
    mid       = (max(all_spots) + min(all_spots)) / 2
    step      = ATM_ROUNDING[underlying]
    centre    = round(mid / step) * step
    strikes   = [centre + i * step for i in range(-(N_STRIKES+2), N_STRIKES+3)]
    date_str  = trade_date.strftime("%Y-%m-%d")

    cp = groww_get("/historical/contracts", {
        "exchange": exchange, "underlying_symbol": underlying,
        "expiry_date": expiry,
    }, token)
    available = set(cp.get("contracts", [])) if cp else set()
    if not available:
        return None, None, "No contracts"

    wanted        = []
    contract_data = {}

    for strike in strikes:
        for opt_type in ["CE", "PE"]:
            sym = build_sym(exchange, underlying, expiry, strike, opt_type)
            if sym in available:
                wanted.append((strike, opt_type, sym))

    if not wanted:
        sample = sorted(available)[:3]
        return None, None, f"Symbol mismatch. Sample: {sample}"

    for (strike, opt_type, sym) in wanted:
        payload = groww_get("/historical/candles", {
            "exchange": exchange, "segment": "FNO",
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

    return contract_data, wanted, None


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


# ── find_first_itm_strike ─────────────────────────────────────

def find_itm(chain, atm_strike, direction, underlying):
    step          = ATM_ROUNDING[underlying]
    target_strike = atm_strike - step if direction == "BEARISH" else atm_strike + step
    opt_type      = "PE"              if direction == "BEARISH" else "CE"
    for o in chain:
        if o["strike"] == target_strike and o["type"] == opt_type:
            return o["trading_symbol"], o["ltp"]
    return None, 0


# ── Single-day run with all v5 logic ─────────────────────────

def run_day(token, exchange, underlying, trade_date):
    """
    Returns result dict. All v5 gates applied:
      - DTE gate (MIN_DAYS_TO_EXPIRY)
      - OI volume dominance ratio
      - 2-bar confirmation
      - Thesis-invalidation SL (spot recrosses day_open)
      - 25% SL (from config)
    """
    # Smart expiry selection
    expiry, dte = get_best_expiry_historical(token, exchange, underlying, trade_date)
    if not expiry:
        return {"went_live": False, "reason": "No expiry"}

    # DTE gate
    if dte < MIN_DAYS_TO_EXPIRY:
        return {"went_live": False,
                "reason": f"DTE={dte} < {MIN_DAYS_TO_EXPIRY} (theta zone)"}

    spot_bars = fetch_spot_bars(token, exchange, underlying, trade_date)
    if not spot_bars:
        return {"went_live": False, "reason": "Holiday"}

    contract_data, wanted, err = fetch_option_bars(
        token, exchange, underlying, trade_date, expiry, spot_bars)
    if err:
        return {"went_live": False, "reason": err[:35]}
    if not contract_data:
        return {"went_live": False, "reason": "No option data"}

    day_open          = spot_bars[0]["open"]
    first_signal      = None
    confirm_count     = 0        # 2-bar confirmation counter
    pending_direction = None     # track what signal is building up

    for bar in spot_bars:
        t = bar["time"]
        if not (TRADE_START <= t <= TRADE_END):
            continue

        spot  = bar["close"]
        atm   = get_atm_strike(spot, underlying)
        chain = chain_at(t, contract_data, wanted)
        if not chain:
            continue

        pcr = compute_pcr(chain)
        bear_ok, _ = check_oi_dominance_v5(chain, "BEARISH", atm)
        bull_ok, _ = check_oi_dominance_v5(chain, "BULLISH", atm)

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

        signal_ok = oi_sig != "NONE" and pcr_ok and pa_ok

        if signal_ok:
            if oi_sig == pending_direction:
                confirm_count += 1
            else:
                # New direction — restart counter
                pending_direction = oi_sig
                confirm_count     = 1

            if confirm_count >= SIGNAL_CONFIRM_BARS:
                sym, ltp = find_itm(chain, atm, oi_sig, underlying)
                if sym and ltp > 0:
                    units = _round_to_lot(calc_units(ltp), underlying)
                    first_signal = {
                        "time": t, "direction": oi_sig, "atm": atm,
                        "symbol": sym, "ltp": ltp, "units": units,
                        "day_open": day_open, "dte": dte,
                    }
                    break
        else:
            # Signal broke — reset counter
            pending_direction = None
            confirm_count     = 0

    if not first_signal:
        return {"went_live": False, "reason": "No signal"}

    # ── Simulate trade ────────────────────────────────────────
    sig       = first_signal
    entry     = sig["ltp"]
    units     = sig["units"]
    sym       = sig["symbol"]
    day_open  = sig["day_open"]
    direction = sig["direction"]
    sl        = round(entry * (1 - STOP_LOSS_PCT),    2)
    tgt       = round(entry * (1 + PARTIAL_EXIT_PCT), 2)

    opt_bars = contract_data.get(sym, [])
    if not opt_bars:
        return {"went_live": False, "reason": "No bars for entry"}

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
        spot_now = spot_by_time.get(t, 0)

        if t < sig["time"]:
            continue

        # Time stop
        if t >= TIME_STOP:
            rem     = round(remaining * (ltp - entry), 2)
            outcome = ("TIME_STOP", ltp, round(partial_pnl + rem, 2))
            break

        # Thesis-invalidation SL (Natenberg: exit when direction breaks)
        if spot_now > 0 and day_open > 0:
            if direction == "BEARISH" and spot_now > day_open:
                rem     = round(remaining * (ltp - entry), 2)
                outcome = ("THESIS_BROKEN", ltp, round(partial_pnl + rem, 2))
                break
            elif direction == "BULLISH" and spot_now < day_open:
                rem     = round(remaining * (ltp - entry), 2)
                outcome = ("THESIS_BROKEN", ltp, round(partial_pnl + rem, 2))
                break

        # Premium SL
        if ltp <= trail_sl:
            rem     = round(remaining * (ltp - entry), 2)
            outcome = ("SL_HIT", ltp, round(partial_pnl + rem, 2))
            break

        # Partial exit
        if not partial_hit and ltp >= tgt:
            partial_pnl = round(half * (ltp - entry), 2)
            remaining   = units - half
            trail_sl    = entry
            partial_hit = True

        # Update trailing SL
        if partial_hit:
            new_trail = round(ltp * (1 - TRAIL_STOP_PCT), 2)
            if new_trail > trail_sl:
                trail_sl = new_trail

        # Trailing SL hit
        if partial_hit and ltp <= trail_sl:
            rem     = round(remaining * (ltp - entry), 2)
            outcome = ("TRAIL_SL", ltp, round(partial_pnl + rem, 2))
            break

    if not outcome:
        outcome = ("NO_EXIT", entry, 0.0)

    _, exit_price, total_pnl = outcome

    return {
        "went_live":       True,
        "date":            trade_date,
        "signal_time":     sig["time"].strftime("%H:%M"),
        "direction":       sig["direction"],
        "symbol":          sym,
        "expiry":          expiry,
        "dte":             dte,
        "entry":           entry,
        "exit_price":      exit_price,
        "exit_reason":     outcome[0],
        "units":           units,
        "pnl":             total_pnl,
        "partial_hit":     partial_hit,
        "partial_pnl":     partial_pnl,
        "capital_at_risk": round(units * entry * STOP_LOSS_PCT),
    }


# ── Summary printer (shared by both scripts) ──────────────────

EXIT_LABELS = {
    "TIME_STOP":    "Time stop",
    "SL_HIT":       "SL hit",
    "TRAIL_SL":     "Trail SL",
    "THESIS_BROKEN":"Thesis broken",
    "NO_EXIT":      "No exit",
}

def print_summary(results, underlying, month_name, days):
    print("\n\n" + "═"*76)
    print(f"  MONTHLY P&L TABLE — {underlying}  |  {month_name}")
    print("═"*76)
    print(f"  {'Date':<13} {'Day':<4} {'Time':>5}  {'Dir':<8}  "
          f"{'DTE':>4}  {'Entry':>7}  {'Exit':>7}  {'Exit type':<15}  {'Units':>5}  {'P&L':>9}")
    print("  " + "─"*73)

    total_pnl    = 0
    total_risk   = 0
    trades_taken = 0
    wins = losses = no_signal = holidays = no_data = 0

    for r in results:
        d        = r["date"]
        date_str = d.strftime("%d %b %Y")
        day_str  = d.strftime("%a")

        if not r.get("went_live"):
            reason = r.get("reason", "–")
            if reason == "Holiday":
                holidays += 1
            elif "No option" in reason or "contract" in reason or "mismatch" in reason:
                no_data += 1
            else:
                no_signal += 1
            print(f"  {date_str:<13} {day_str:<4} {'–':>5}  {'–':<8}  "
                  f"{'–':>4}  {'–':>7}  {'–':>7}  {reason[:30]:<30}")
            continue

        pnl_str  = f"₹{r['pnl']:+,.0f}"
        emoji    = "✅" if r["pnl"] > 0 else "❌"
        exit_lbl = EXIT_LABELS.get(r["exit_reason"], r["exit_reason"])

        print(f"  {date_str:<13} {day_str:<4} {r['signal_time']:>5}  "
              f"{r['direction']:<8}  {r.get('dte','-'):>4}  "
              f"₹{r['entry']:>6,.0f}  ₹{r['exit_price']:>6,.0f}  "
              f"{exit_lbl:<15}  {r['units']:>5}  {emoji} {pnl_str}")

        total_pnl    += r["pnl"]
        total_risk   += r["capital_at_risk"]
        trades_taken += 1
        if r["pnl"] > 0: wins   += 1
        else:            losses += 1

    print("  " + "─"*73)
    print(f"\n  {'Month':<25}: {month_name}")
    print(f"  {'Days scanned':<25}: {len(days)}  "
          f"({holidays} holiday, {no_signal} no-signal, {no_data} no-data)")
    print(f"  {'Trades taken':<25}: {trades_taken}")
    print(f"  {'v5 gates active':<25}: DTE≥{MIN_DAYS_TO_EXPIRY} | "
          f"Vol ratio {OI_VOL_DOMINANCE_RATIO}x | "
          f"{SIGNAL_CONFIRM_BARS}-bar confirm | "
          f"SL {STOP_LOSS_PCT*100:.0f}% | Thesis SL")

    if trades_taken > 0:
        win_rate  = round(wins / trades_taken * 100)
        avg_trade = round(total_pnl / trades_taken)
        avg_risk  = round(total_risk / trades_taken)
        rr        = round(total_pnl / total_risk, 2) if total_risk > 0 else 0
        m_emoji   = "🟢" if total_pnl > 0 else "🔴"

        print(f"  {'Win / Loss':<25}: {wins}W / {losses}L  ({win_rate}% win rate)")
        print(f"  {'Avg P&L per trade':<25}: ₹{avg_trade:+,.0f}")
        print(f"  {'Total capital risked':<25}: ₹{total_risk:,.0f}  "
              f"(avg ₹{avg_risk:,.0f}/trade)")
        print(f"  {'Total P&L':<25}: {m_emoji} ₹{total_pnl:+,.0f}")
        print(f"  {'Return on risk':<25}: {rr:+.2f}x")

        # Weekly breakdown
        print(f"\n  Weekly breakdown:")
        week_pnls = {}
        for r in results:
            if not r.get("went_live"):
                continue
            wk = r["date"].isocalendar()[1]
            week_pnls[wk] = week_pnls.get(wk, 0) + r["pnl"]
        for wk, wpnl in sorted(week_pnls.items()):
            e = "🟢" if wpnl > 0 else "🔴"
            print(f"    Week {wk}: {e} ₹{wpnl:+,.0f}")

    print("\n" + "═"*76 + "\n")
