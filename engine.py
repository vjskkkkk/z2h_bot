"""
zero_hero/engine.py (v3 — multi-underlying: NIFTY / BANKNIFTY / SENSEX)
=========================================================================
All public functions that previously hardcoded "NIFTY" or groww.EXCHANGE_NSE
now accept an optional `underlying` parameter that defaults to the value of
config.UNDERLYING (i.e. "NIFTY"), so every existing call-site continues to
work without modification.

Correct API signatures per official Groww docs:
  - get_ltp(segment=..., exchange_trading_symbols=...)
  - get_option_chain(exchange=..., underlying=..., expiry_date=...)
  - get_ohlc(segment=..., exchange_trading_symbols=...)
"""

import datetime
from growwapi import GrowwAPI
from config import (
    UNDERLYING, ATM_ROUNDING, LTP_SYMBOLS, EXPIRY_EXCHANGE,
    VIX_MAX, PCR_BEARISH_THRESHOLD, PCR_BULLISH_THRESHOLD,
)


def get_groww_client():
    from auth import get_access_token
    return GrowwAPI(get_access_token())


# ── HELPERS ──────────────────────────────────────────────────

def get_india_vix(groww):
    try:
        resp = groww.get_ltp(
            segment=groww.SEGMENT_CASH,
            exchange_trading_symbols="NSE_INDIAVIX"
        )
        if isinstance(resp, dict):
            for key in resp:
                val = resp[key]
                if isinstance(val, (int, float)):
                    return float(val)
        return 999.0
    except Exception as e:
        print(f"[VIX] Error: {e}")
        return 999.0


# CHANGED: accepts `underlying` parameter.
# Expiry weekday differs by index:  NIFTY=Thu(3), BANKNIFTY=Wed(2), SENSEX=Fri(4)
_EXPIRY_WEEKDAY = {
    "NIFTY":     3,   # Thursday
    "BANKNIFTY": 2,   # Wednesday
    "SENSEX":    4,   # Friday
}

def is_expiry_day(underlying=None):
    """Return True if today is the weekly expiry day for `underlying`."""
    if underlying is None:
        underlying = UNDERLYING
    expiry_weekday = _EXPIRY_WEEKDAY.get(underlying, 3)
    return datetime.date.today().weekday() == expiry_weekday


def is_within_trading_window():
    now   = datetime.datetime.now().time()
    start = datetime.time(9, 45)
    end   = datetime.time(14, 0)
    return start <= now <= end


def get_days_to_expiry(expiry_date_str):
    """Returns calendar days between today and expiry. Minimum 0."""
    expiry = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    return max((expiry - datetime.date.today()).days, 0)


# CHANGED: accepts `underlying` parameter; uses EXPIRY_EXCHANGE dict for exchange.
def get_nearest_expiry(groww, underlying=None):
    if underlying is None:
        underlying = UNDERLYING
    _exchange_str = EXPIRY_EXCHANGE.get(underlying, "NSE")
    exchange = getattr(groww, f"EXCHANGE_{_exchange_str}", _exchange_str)
    try:
        today = datetime.date.today()
        for delta in [0, 1]:
            month = ((today.month - 1 + delta) % 12) + 1
            year  = today.year + ((today.month - 1 + delta) // 12)
            resp  = groww.get_expiries(
                exchange=exchange,
                underlying_symbol=underlying,
                year=year, month=month
            )
            expiries = resp.get("expiries", [])
            for exp in sorted(expiries):
                if datetime.datetime.strptime(exp, "%Y-%m-%d").date() >= today:
                    return exp
        return None
    except Exception as e:
        print(f"[EXPIRY] Error ({underlying}): {e}")
        return None


# CHANGED: accepts `underlying` parameter; uses LTP_SYMBOLS dict.
def get_spot_price(groww, underlying=None):
    if underlying is None:
        underlying = UNDERLYING
    ltp_symbol = LTP_SYMBOLS.get(underlying, f"NSE_{underlying}")
    try:
        resp = groww.get_ltp(
            segment=groww.SEGMENT_CASH,
            exchange_trading_symbols=ltp_symbol
        )
        if isinstance(resp, dict):
            for key in resp:
                val = resp[key]
                if isinstance(val, (int, float)):
                    return float(val)
        return 0.0
    except Exception as e:
        print(f"[SPOT] Error ({underlying}): {e}")
        return 0.0


def get_atm_strike(spot, underlying=None):
    if underlying is None:
        underlying = UNDERLYING
    rounding = ATM_ROUNDING.get(underlying, 50)
    return round(spot / rounding) * rounding


# ── OPTION CHAIN UTILITIES ───────────────────────────────────

def parse_option_chain(chain_resp):
    result = []
    for strike_str, sides in chain_resp.get("strikes", {}).items():
        try:
            strike = int(float(strike_str))
        except:
            continue
        for opt_type in ["CE", "PE"]:
            opt = sides.get(opt_type, {})
            if not opt:
                continue
            greeks = opt.get("greeks", {})
            result.append({
                "strike":         strike,
                "type":           opt_type,
                "oi":             opt.get("open_interest", 0),
                "volume":         opt.get("volume", 0),
                "ltp":            opt.get("ltp", 0),
                "trading_symbol": opt.get("trading_symbol", ""),
                "delta":          greeks.get("delta", 0),
                "gamma":          greeks.get("gamma", 0),
                "theta":          greeks.get("theta", 0),
                "vega":           greeks.get("vega", 0),
                "iv":             greeks.get("iv", 0),
            })
    return result


def compute_pcr(option_chain):
    total_ce = sum(o["oi"] for o in option_chain if o["type"] == "CE")
    total_pe = sum(o["oi"] for o in option_chain if o["type"] == "PE")
    if total_ce == 0:
        return 1.0
    return round(total_pe / total_ce, 3)


def check_oi_dominance(option_chain, signal_type, atm_strike, n_strikes=5):
    """
    Check OI concentration near ATM as directional signal.
    Heavy CE OI near ATM = resistance wall = bearish signal.
    Heavy PE OI near ATM = support wall  = bullish signal.
    Volume used as proxy for fresh OI buildup.
    """
    nearby = [o for o in option_chain
              if abs(o["strike"] - atm_strike) <= n_strikes * 50]

    ce_nearby = [o for o in nearby if o["type"] == "CE"]
    pe_nearby = [o for o in nearby if o["type"] == "PE"]

    ce_oi  = sum(o["oi"]     for o in ce_nearby)
    pe_oi  = sum(o["oi"]     for o in pe_nearby)
    ce_vol = sum(o["volume"] for o in ce_nearby)
    pe_vol = sum(o["volume"] for o in pe_nearby)

    total_oi = ce_oi + pe_oi
    if total_oi == 0:
        return False, "No OI data near ATM"

    ce_pct = round(ce_oi / total_oi * 100, 1)
    pe_pct = round(pe_oi / total_oi * 100, 1)
    detail = (f"Near-ATM — CE OI: {ce_oi:,} ({ce_pct}%) | PE OI: {pe_oi:,} ({pe_pct}%) | "
              f"CE Vol: {ce_vol:,} | PE Vol: {pe_vol:,}")

    if signal_type == "BEARISH":
        detected = ce_pct >= 60 and ce_vol > pe_vol
        return detected, detail
    else:
        detected = pe_pct >= 60 and pe_vol > ce_vol
        return detected, detail


def find_first_itm_strike(option_chain, atm_strike, direction, underlying=None):
    """
    Find the first ITM strike in the signal direction.
    Step size varies by underlying (matches ATM_ROUNDING).
    """
    if underlying is None:
        underlying = UNDERLYING
    step = ATM_ROUNDING.get(underlying, 50)
    target_strike = atm_strike - step if direction == "BEARISH" else atm_strike + step
    opt_type      = "PE"            if direction == "BEARISH" else "CE"
    for o in option_chain:
        if o["strike"] == target_strike and o["type"] == opt_type:
            return o["trading_symbol"], o["ltp"]
    return None, 0


# CHANGED: accepts `underlying` parameter; uses LTP_SYMBOLS for the correct symbol.
def check_price_action(groww, direction, spot, underlying=None):
    if underlying is None:
        underlying = UNDERLYING
    ltp_symbol = LTP_SYMBOLS.get(underlying, f"NSE_{underlying}")
    try:
        resp     = groww.get_ohlc(
            segment=groww.SEGMENT_CASH,
            exchange_trading_symbols=ltp_symbol
        )
        day_open = resp.get(ltp_symbol, {}).get("open", 0)
        if day_open == 0:
            return False, "No OHLC data"
        confirmed = spot < day_open if direction == "BEARISH" else spot > day_open
        arrow     = "below"         if direction == "BEARISH" else "above"
        return confirmed, f"Spot ₹{spot:,.0f} {'✅' if confirmed else '❌'} {arrow} open ₹{day_open:,.0f}"
    except Exception as e:
        return False, f"OHLC error: {e}"


# ── MAIN SIGNAL CHECK ────────────────────────────────────────

def run_zero_hero_check(groww, underlying=None):
    """
    Run the full Zero Hero checklist for `underlying`.

    Backwards compatible: run_zero_hero_check(groww) still works,
    defaulting to config.UNDERLYING ("NIFTY").
    """
    if underlying is None:
        underlying = UNDERLYING

    # Resolve the config string ("NSE" / "BSE") to the SDK constant on the
    # groww instance (groww.EXCHANGE_NSE / groww.EXCHANGE_BSE).
    # Falling back to the raw string keeps things working if a future SDK
    # version changes the constant name.
    _exchange_str = EXPIRY_EXCHANGE.get(underlying, "NSE")
    exchange = getattr(groww, f"EXCHANGE_{_exchange_str}", _exchange_str)

    checklist = {}

    # ── Stage 1: Pre-market gates ──────────────────────────
    vix = get_india_vix(groww)
    checklist["vix"] = {
        "passed": vix <= VIX_MAX,
        "detail": f"India VIX = {vix} ({'✅ OK' if vix <= VIX_MAX else '❌ Too high'})"
    }

    # CHANGED: pass underlying so expiry weekday is looked up correctly
    expiry_today = is_expiry_day(underlying)
    checklist["expiry"] = {
        "passed": not expiry_today,
        "detail": f"❌ Expiry day ({underlying})" if expiry_today else f"✅ Not expiry day ({underlying})"
    }

    in_window = is_within_trading_window()
    checklist["time_window"] = {
        "passed": in_window,
        "detail": f"Time {datetime.datetime.now().strftime('%H:%M')} — {'✅ In window' if in_window else '❌ Outside 9:45–14:00'}"
    }

    if not all(v["passed"] for v in checklist.values()):
        return {"signal": "NO_SIGNAL", "go": False, "checklist": checklist, "entry": {}, "underlying": underlying}

    # ── Fetch market data ──────────────────────────────────
    # CHANGED: all data fetches now pass `underlying` explicitly
    expiry       = get_nearest_expiry(groww, underlying)
    spot         = get_spot_price(groww, underlying)
    atm          = get_atm_strike(spot, underlying)

    # CHANGED: get_option_chain uses the exchange resolved from EXPIRY_EXCHANGE
    chain_resp   = groww.get_option_chain(
        exchange=exchange,
        underlying=underlying,
        expiry_date=expiry
    )
    option_chain = parse_option_chain(chain_resp)
    pcr          = compute_pcr(option_chain)

    print(f"  [{underlying}] Spot: ₹{spot:,.0f} | ATM: {atm} | Expiry: {expiry} | PCR: {pcr} | Strikes: {len(option_chain)}")

    # ── Stage 2: Signal ───────────────────────────────────
    bear_ok, bear_detail = check_oi_dominance(option_chain, "BEARISH", atm)
    bull_ok, bull_detail = check_oi_dominance(option_chain, "BULLISH", atm)

    if bear_ok and not bull_ok:
        signal    = "BEARISH"
        oi_detail = bear_detail
    elif bull_ok and not bear_ok:
        signal    = "BULLISH"
        oi_detail = bull_detail
    else:
        signal    = "NO_SIGNAL"
        oi_detail = f"No dominant side | {bear_detail}"

    checklist["oi_dominance"] = {"passed": signal != "NO_SIGNAL", "detail": f"{signal} | {oi_detail}"}

    pcr_ok = (signal == "BEARISH" and pcr < PCR_BEARISH_THRESHOLD) or \
             (signal == "BULLISH" and pcr > PCR_BULLISH_THRESHOLD)
    checklist["pcr"] = {
        "passed": pcr_ok,
        "detail": f"PCR={pcr} — {'✅ confirms ' + signal if pcr_ok else '❌ no confirmation'}"
    }

    if signal == "NO_SIGNAL" or not pcr_ok:
        return {"signal": signal, "go": False, "checklist": checklist, "entry": {}, "underlying": underlying}

    # ── Stage 3: Entry ────────────────────────────────────
    # CHANGED: pass underlying to price_action and find_first_itm_strike
    price_ok, price_detail = check_price_action(groww, signal, spot, underlying)
    checklist["price_action"] = {"passed": price_ok, "detail": price_detail}

    symbol, ltp = find_first_itm_strike(option_chain, atm, signal, underlying)
    checklist["itm_strike"] = {
        "passed": symbol is not None and ltp > 0,
        "detail": f"Symbol: {symbol} | LTP: ₹{ltp}"
    }

    all_passed = all(v["passed"] for v in checklist.values())
    entry_info = {}
    if all_passed and symbol:
        entry_info = {
            "trading_symbol": symbol,
            "ltp":            ltp,
            "direction":      signal,
            "atm_strike":     atm,
            "expiry":         expiry,
            "spot":           spot,
            "pcr":            pcr,
            "vix":            vix,
            "underlying":     underlying,   # NEW: carry underlying into entry dict
        }

    return {
        "signal":     signal,
        "go":         all_passed,
        "checklist":  checklist,
        "entry":      entry_info,
        "underlying": underlying,           # NEW: top-level field for scheduler
    }


# ── SENTIMENT INTEGRATION ────────────────────────────────────

from sentiment import get_full_sentiment, get_iron_condor_strikes


def enrich_with_sentiment(result, option_chain, spot, atm, days_to_exp, vix, pcr):
    """
    Adds sentiment layer to an existing result dict.
    Upgrades recommendation if range-bound conditions detected.
    Confirms or weakens directional signal based on skew + FII + breadth.

    NOTE: `option_chain` must be the chain already fetched for the same
    underlying that produced `result`.  The scheduler passes it explicitly,
    so there is no risk of stale data from a previous underlying in the loop.
    """
    if not result.get("go") and result.get("signal") == "NO_SIGNAL":
        # Even on no-signal, check for Iron Condor opportunity
        from sentiment import compute_range_bound_score, compute_iv_skew
        skew    = compute_iv_skew(option_chain, spot, atm, days_to_exp)
        range_b = compute_range_bound_score(
            vix, pcr, abs(skew["skew"]), spot, option_chain, atm)
        if range_b["score"] >= 8:
            ic = get_iron_condor_strikes(atm, option_chain)
            result["iron_condor_opportunity"] = {
                "detected": True,
                "structure": ic,
                "detail":    range_b["detail"],
                "reasons":   range_b["reasons"],
            }
        return result

    sentiment = get_full_sentiment(
        option_chain, spot, atm, days_to_exp, vix, pcr
    )
    result["sentiment"] = sentiment

    signal  = result.get("signal", "NO_SIGNAL")
    overall = sentiment["overall"]

    if signal == "BEARISH" and overall == "BULLISH":
        result["sentiment_conflict"] = True
        result["checklist"]["sentiment"] = {
            "passed": False,
            "detail": (f"⚠️ Sentiment conflict — signal BEARISH but sentiment BULLISH "
                       f"(skew:{sentiment['skew']['bias']} breadth:{sentiment['breadth']['breadth']} "
                       f"FII:{sentiment['fii']['bias']})")
        }
        result["go"] = False
    elif signal == "BULLISH" and overall == "BEARISH":
        result["sentiment_conflict"] = True
        result["checklist"]["sentiment"] = {
            "passed": False,
            "detail": "⚠️ Sentiment conflict — signal BULLISH but sentiment BEARISH"
        }
        result["go"] = False
    else:
        result["sentiment_conflict"] = False
        result["checklist"]["sentiment"] = {
            "passed": True,
            "detail": (f"✅ Sentiment {overall} confirms {signal} | "
                       f"Skew:{sentiment['skew']['detail']} | "
                       f"Breadth:{sentiment['breadth']['breadth']} | "
                       f"FII:{sentiment['fii']['bias']}")
        }

    # Range-bound override
    range_b = sentiment["range_bound"]
    if range_b["score"] >= 8:
        ic = get_iron_condor_strikes(atm, option_chain)
        result["iron_condor_opportunity"] = {
            "detected":  True,
            "structure": ic,
            "detail":    range_b["detail"],
        }

    # Parity flag
    parity = sentiment["parity"]
    if parity["mispriced"] != "NONE":
        result["parity_note"] = parity["detail"]

    return result
