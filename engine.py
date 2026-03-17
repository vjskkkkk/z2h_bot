"""
zero_hero/engine.py  (v2 — corrected for actual Groww SDK signatures)
=======================================================================
Correct API signatures per official Groww docs:
  - get_ltp(segment=..., exchange_trading_symbols=...)
  - get_option_chain(exchange=..., underlying=..., expiry_date=...)
  - get_ohlc(segment=..., exchange_trading_symbols=...)
"""

import datetime
from growwapi import GrowwAPI
from config import *


def get_groww_client():
    from auth import get_access_token
    return GrowwAPI(get_access_token())

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


def is_expiry_day():
    return datetime.date.today().weekday() == 3


def is_within_trading_window():
    now   = datetime.datetime.now().time()
    start = datetime.time(9, 45)
    end   = datetime.time(14, 0)
    return start <= now <= end


def get_days_to_expiry(expiry_date_str):
    """Returns calendar days between today and expiry. Minimum 0."""
    expiry = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    return max((expiry - datetime.date.today()).days, 0)


def get_nearest_expiry(groww):
    try:
        today = datetime.date.today()
        for delta in [0, 1]:
            month = ((today.month - 1 + delta) % 12) + 1
            year  = today.year + ((today.month - 1 + delta) // 12)
            resp     = groww.get_expiries(exchange=groww.EXCHANGE_NSE,
                                          underlying_symbol=UNDERLYING,
                                          year=year, month=month)
            expiries = resp.get("expiries", [])
            for exp in sorted(expiries):
                if datetime.datetime.strptime(exp, "%Y-%m-%d").date() >= today:
                    return exp
        return None
    except Exception as e:
        print(f"[EXPIRY] Error: {e}")
        return None


def get_spot_price(groww):
    try:
        resp = groww.get_ltp(
            segment=groww.SEGMENT_CASH,
            exchange_trading_symbols="NSE_NIFTY"
        )
        if isinstance(resp, dict):
            for key in resp:
                val = resp[key]
                if isinstance(val, (int, float)):
                    return float(val)
        return 0.0
    except Exception as e:
        print(f"[SPOT] Error: {e}")
        return 0.0


def get_atm_strike(spot, underlying="NIFTY"):
    from config import ATM_ROUNDING
    rounding = ATM_ROUNDING.get(underlying, 50)
    return round(spot / rounding) * rounding


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
    Heavy PE OI near ATM = support wall = bullish signal.
    Volume used as proxy for fresh OI buildup.
    """
    nearby = [o for o in option_chain
              if abs(o["strike"] - atm_strike) <= n_strikes * 50]

    ce_nearby = [o for o in nearby if o["type"] == "CE"]
    pe_nearby = [o for o in nearby if o["type"] == "PE"]

    ce_oi  = sum(o["oi"] for o in ce_nearby)
    pe_oi  = sum(o["oi"] for o in pe_nearby)
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


def find_first_itm_strike(option_chain, atm_strike, direction):
    target_strike = atm_strike - 50 if direction == "BEARISH" else atm_strike + 50
    opt_type = "PE" if direction == "BEARISH" else "CE"
    for o in option_chain:
        if o["strike"] == target_strike and o["type"] == opt_type:
            return o["trading_symbol"], o["ltp"]
    return None, 0


def check_price_action(groww, direction, spot):
    try:
        resp = groww.get_ohlc(
            segment=groww.SEGMENT_CASH,
            exchange_trading_symbols="NSE_NIFTY"
        )
        day_open = resp.get("NSE_NIFTY", {}).get("open", 0)
        if day_open == 0:
            return False, "No OHLC data"
        confirmed = spot < day_open if direction == "BEARISH" else spot > day_open
        arrow = "below" if direction == "BEARISH" else "above"
        return confirmed, f"Spot ₹{spot:,.0f} {'✅' if confirmed else '❌'} {arrow} open ₹{day_open:,.0f}"
    except Exception as e:
        return False, f"OHLC error: {e}"

# ── MAIN ─────────────────────────────────────────────────────

def run_zero_hero_check(groww):
    checklist = {}

    # Stage 1: Pre-market
    vix = get_india_vix(groww)
    checklist["vix"] = {
        "passed": vix <= VIX_MAX,
        "detail": f"India VIX = {vix} ({'✅ OK' if vix <= VIX_MAX else '❌ Too high'})"
    }
    checklist["expiry"] = {
        "passed": not is_expiry_day(),
        "detail": "❌ Expiry day" if is_expiry_day() else "✅ Not expiry day"
    }
    in_window = is_within_trading_window()
    checklist["time_window"] = {
        "passed": in_window,
        "detail": f"Time {datetime.datetime.now().strftime('%H:%M')} — {'✅ In window' if in_window else '❌ Outside 9:45–14:00'}"
    }

    if not all(v["passed"] for v in checklist.values()):
        return {"signal": "NO_SIGNAL", "go": False, "checklist": checklist, "entry": {}}

    # Fetch data
    expiry       = get_nearest_expiry(groww)
    spot         = get_spot_price(groww)
    atm          = get_atm_strike(spot)
    chain_resp   = groww.get_option_chain(exchange=groww.EXCHANGE_NSE, underlying=UNDERLYING, expiry_date=expiry)
    option_chain = parse_option_chain(chain_resp)
    pcr          = compute_pcr(option_chain)

    print(f"  Spot: ₹{spot:,.0f} | ATM: {atm} | Expiry: {expiry} | PCR: {pcr} | Strikes: {len(option_chain)}")

    # Stage 2: Signal
    bear_ok, bear_detail = check_oi_dominance(option_chain, "BEARISH", atm)
    bull_ok, bull_detail = check_oi_dominance(option_chain, "BULLISH", atm)

    if bear_ok and not bull_ok:
        signal = "BEARISH"
        oi_detail = bear_detail
    elif bull_ok and not bear_ok:
        signal = "BULLISH"
        oi_detail = bull_detail
    else:
        signal = "NO_SIGNAL"
        oi_detail = f"No dominant side | {bear_detail}"

    checklist["oi_dominance"] = {"passed": signal != "NO_SIGNAL", "detail": f"{signal} | {oi_detail}"}

    pcr_ok = (signal == "BEARISH" and pcr < PCR_BEARISH_THRESHOLD) or \
             (signal == "BULLISH" and pcr > PCR_BULLISH_THRESHOLD)
    checklist["pcr"] = {
        "passed": pcr_ok,
        "detail": f"PCR={pcr} — {'✅ confirms ' + signal if pcr_ok else '❌ no confirmation'}"
    }

    if signal == "NO_SIGNAL" or not pcr_ok:
        return {"signal": signal, "go": False, "checklist": checklist, "entry": {}}

    # Stage 3: Entry
    price_ok, price_detail = check_price_action(groww, signal, spot)
    checklist["price_action"] = {"passed": price_ok, "detail": price_detail}

    symbol, ltp = find_first_itm_strike(option_chain, atm, signal)
    checklist["itm_strike"] = {
        "passed": symbol is not None and ltp > 0,
        "detail": f"Symbol: {symbol} | LTP: ₹{ltp}"
    }

    all_passed = all(v["passed"] for v in checklist.values())
    entry_info = {}
    if all_passed and symbol:
        entry_info = {
            "trading_symbol": symbol, "ltp": ltp, "direction": signal,
            "atm_strike": atm, "expiry": expiry, "spot": spot, "pcr": pcr, "vix": vix
        }

    return {"signal": signal, "go": all_passed, "checklist": checklist, "entry": entry_info}


# ── SENTIMENT INTEGRATION (appended to existing engine.py) ──
# Call this after the existing run_zero_hero_check to enrich result
from sentiment import get_full_sentiment, get_iron_condor_strikes

def enrich_with_sentiment(result, option_chain, spot, atm, days_to_exp, vix, pcr):
    """
    Adds sentiment layer to an existing result dict.
    Upgrades recommendation if range-bound conditions detected.
    Confirms or weakens directional signal based on skew + FII + breadth.
    """
    if not result.get("go") and result.get("signal") == "NO_SIGNAL":
        # Even on no-signal, check for Iron Condor opportunity
        from sentiment import compute_range_bound_score, compute_iv_skew
        skew   = compute_iv_skew(option_chain, spot, atm, days_to_exp)
        range_b = compute_range_bound_score(
                    vix, pcr, abs(skew["skew"]), spot, option_chain, atm)
        if range_b["score"] >= 8:
            ic = get_iron_condor_strikes(atm, option_chain)
            result["iron_condor_opportunity"] = {
                "detected": True,
                "structure": ic,
                "detail":   range_b["detail"],
                "reasons":  range_b["reasons"],
            }
        return result

    sentiment = get_full_sentiment(
        option_chain, spot, atm, days_to_exp, vix, pcr
    )
    result["sentiment"] = sentiment

    # Sentiment confirmation check
    signal = result.get("signal", "NO_SIGNAL")
    overall = sentiment["overall"]

    if signal == "BEARISH" and overall == "BULLISH":
        result["sentiment_conflict"] = True
        result["checklist"]["sentiment"] = {
            "passed": False,
            "detail": f"⚠️ Sentiment conflict — signal BEARISH but sentiment BULLISH "
                     f"(skew:{sentiment['skew']['bias']} breadth:{sentiment['breadth']['breadth']} "
                     f"FII:{sentiment['fii']['bias']})"
        }
        result["go"] = False
    elif signal == "BULLISH" and overall == "BEARISH":
        result["sentiment_conflict"] = True
        result["checklist"]["sentiment"] = {
            "passed": False,
            "detail": f"⚠️ Sentiment conflict — signal BULLISH but sentiment BEARISH"
        }
        result["go"] = False
    else:
        result["sentiment_conflict"] = False
        result["checklist"]["sentiment"] = {
            "passed": True,
            "detail": f"✅ Sentiment {overall} confirms {signal} | "
                     f"Skew:{sentiment['skew']['detail']} | "
                     f"Breadth:{sentiment['breadth']['breadth']} | "
                     f"FII:{sentiment['fii']['bias']}"
        }

    # Range-bound override — if market is range-bound, suggest Iron Condor
    range_b = sentiment["range_bound"]
    if range_b["score"] >= 8:
        ic = get_iron_condor_strikes(atm, option_chain)
        result["iron_condor_opportunity"] = {
            "detected": True,
            "structure": ic,
            "detail":   range_b["detail"],
        }

    # Parity flag — note if one side mispriced
    parity = sentiment["parity"]
    if parity["mispriced"] != "NONE":
        result["parity_note"] = parity["detail"]

    return result
