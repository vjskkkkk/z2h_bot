"""
zero_hero/engine.py (v5 — Natenberg improvements)
==================================================
Changes vs v3:
  1. get_best_expiry() replaces get_nearest_expiry() — evaluates up to
     EXPIRY_CANDIDATES upcoming expiries and picks the first one that
     falls within MIN_DAYS_TO_EXPIRY..MAX_DAYS_TO_EXPIRY.
     get_nearest_expiry() kept as a backwards-compatible alias.

  2. run_zero_hero_check() — new checklist gates:
       - DTE gate: skip if selected expiry has < MIN_DAYS_TO_EXPIRY DTE
       - VIX_MIN gate: skip directional buy if VIX too low (IC territory)
       - VIX_MAX lowered to 18 (in config)
       - IV rank gate: only buy when ATM IV is in bottom IV_BUY_MAX_RANK%
         of recent history (stored in iv_history.json)
       - Volume dominance ratio: CE/PE volume must exceed the other by
         OI_VOL_DOMINANCE_RATIO, not just be greater

  3. check_oi_dominance() — volume ratio applied
  4. update_iv_history() — records ATM IV after each scan for rank calc
  5. get_atm_iv() — extracts ATM IV from the live option chain
"""

import datetime
import json
import os
from growwapi import GrowwAPI
from config import (
    UNDERLYING, ATM_ROUNDING, LTP_SYMBOLS, EXPIRY_EXCHANGE,
    VIX_MAX, VIX_MIN,
    PCR_BEARISH_THRESHOLD, PCR_BULLISH_THRESHOLD,
    OI_VOL_DOMINANCE_RATIO,
    IV_BUY_MAX_RANK, IV_HISTORY_DAYS, IV_HISTORY_FILE,
    MIN_DAYS_TO_EXPIRY, MAX_DAYS_TO_EXPIRY, EXPIRY_CANDIDATES,
)


def get_groww_client():
    from auth import get_access_token
    return GrowwAPI(get_access_token())


# ── VIX ───────────────────────────────────────────────────────

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


# ── EXPIRY DAY DETECTION ──────────────────────────────────────

# Revised expiry days effective 1 Sep 2025 (NSE/BSE circular):
#   NIFTY     weekly/monthly → Tuesday (was Thursday)
#   BANKNIFTY weekly/monthly → Tuesday (was Wednesday)
#   SENSEX    weekly/monthly → Thursday (was Friday/Tuesday)
_EXPIRY_WEEKDAY = {
    "NIFTY":     1,   # Tuesday  (changed from Thursday, effective Sep 2025)
    "BANKNIFTY": 1,   # Tuesday  (changed from Wednesday, effective Sep 2025)
    "SENSEX":    3,   # Thursday (changed from Friday/Tuesday, effective Sep 2025)
}

def is_expiry_day(underlying=None):
    if underlying is None:
        underlying = UNDERLYING
    return datetime.date.today().weekday() == _EXPIRY_WEEKDAY.get(underlying, 3)


def is_within_trading_window():
    now = datetime.datetime.now().time()
    return datetime.time(9, 45) <= now <= datetime.time(14, 0)


def get_days_to_expiry(expiry_date_str):
    expiry = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    return max((expiry - datetime.date.today()).days, 0)


# ── EXPIRY SELECTION (NEW) ────────────────────────────────────

def get_best_expiry(groww, underlying=None):
    """
    Evaluate up to EXPIRY_CANDIDATES upcoming expiries and return the first
    one whose DTE falls within [MIN_DAYS_TO_EXPIRY, MAX_DAYS_TO_EXPIRY].

    Logic:
      - Collect all expiries for the current and next month.
      - Sort ascending (nearest first).
      - Skip any expiry with DTE < MIN_DAYS_TO_EXPIRY (theta spike zone).
      - Return the first expiry within the acceptable DTE window.
      - If nothing fits, fall back to the nearest expiry that is >= today
        (preserves old behaviour rather than returning None).

    This means on a Wednesday with a Tuesday expiry 1 day away, we skip that
    expiry and trade the following week's options (8 DTE) instead.
    """
    if underlying is None:
        underlying = UNDERLYING
    _exchange_str = EXPIRY_EXCHANGE.get(underlying, "NSE")
    exchange      = getattr(groww, f"EXCHANGE_{_exchange_str}", _exchange_str)
    today         = datetime.date.today()

    all_expiries = []
    try:
        for delta in [0, 1]:
            month = ((today.month - 1 + delta) % 12) + 1
            year  = today.year + ((today.month - 1 + delta) // 12)
            resp  = groww.get_expiries(
                exchange=exchange,
                underlying_symbol=underlying,
                year=year, month=month
            )
            all_expiries += resp.get("expiries", [])
    except Exception as e:
        print(f"[EXPIRY] Error ({underlying}): {e}")
        return None

    # Sort and keep only future expiries
    future = sorted(
        e for e in all_expiries
        if datetime.datetime.strptime(e, "%Y-%m-%d").date() >= today
    )

    # Try up to EXPIRY_CANDIDATES expiries for a valid DTE window
    fallback = future[0] if future else None
    for exp in future[:EXPIRY_CANDIDATES]:
        dte = (datetime.datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if MIN_DAYS_TO_EXPIRY <= dte <= MAX_DAYS_TO_EXPIRY:
            return exp

    # Nothing in window — return fallback (nearest) to avoid a hard failure
    return fallback


# Backwards-compatible alias — existing call-sites still work
def get_nearest_expiry(groww, underlying=None):
    return get_best_expiry(groww, underlying)


# ── SPOT PRICE ────────────────────────────────────────────────

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


# ── IV HISTORY (for IV rank gate) ────────────────────────────

def get_atm_iv(option_chain, atm_strike):
    """
    Extract the average IV of the ATM CE and ATM PE from the chain.
    Returns 0.0 if not available.
    """
    ce = next((o for o in option_chain
               if o["strike"] == atm_strike and o["type"] == "CE"), None)
    pe = next((o for o in option_chain
               if o["strike"] == atm_strike and o["type"] == "PE"), None)
    ivs = [o["iv"] for o in [ce, pe] if o and o.get("iv", 0) > 0]
    return round(sum(ivs) / len(ivs), 4) if ivs else 0.0


def update_iv_history(underlying, atm_iv):
    """
    Append today's ATM IV to the rolling history file.
    Keeps only the last IV_HISTORY_DAYS entries per underlying.
    """
    if atm_iv <= 0:
        return
    try:
        history = {}
        if os.path.exists(IV_HISTORY_FILE):
            with open(IV_HISTORY_FILE, "r") as f:
                history = json.load(f)
        entries = history.get(underlying, [])
        entries.append({
            "date": str(datetime.date.today()),
            "iv":   atm_iv,
        })
        # Keep only unique dates, most recent IV_HISTORY_DAYS
        seen  = set()
        dedup = []
        for e in reversed(entries):
            if e["date"] not in seen:
                seen.add(e["date"])
                dedup.append(e)
        history[underlying] = list(reversed(dedup))[-IV_HISTORY_DAYS:]
        with open(IV_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[IV_HISTORY] Error: {e}")


def get_iv_rank(underlying, current_iv):
    """
    Return IV rank (0–100): what % of the last IV_HISTORY_DAYS days had
    lower IV than today. Returns 50 (neutral) if insufficient history.
    """
    try:
        if not os.path.exists(IV_HISTORY_FILE):
            return 50
        with open(IV_HISTORY_FILE, "r") as f:
            history = json.load(f)
        entries = history.get(underlying, [])
        if len(entries) < 5:    # need at least 5 days to be meaningful
            return 50
        ivs   = [e["iv"] for e in entries]
        below = sum(1 for v in ivs if v < current_iv)
        return round(below / len(ivs) * 100, 1)
    except Exception as e:
        print(f"[IV_RANK] Error: {e}")
        return 50


# ── OPTION CHAIN UTILITIES ────────────────────────────────────

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
    OI dominance check with Natenberg volume ratio improvement.

    Original: CE vol > PE vol (any margin)
    Updated:  CE vol > PE vol × OI_VOL_DOMINANCE_RATIO
              (requires 1.5x volume dominance — fresh conviction, not stale OI)
    """
    step = 50   # use fixed step for nearby filter; ATM_ROUNDING varies
    nearby = [o for o in option_chain
              if abs(o["strike"] - atm_strike) <= n_strikes * step]

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
              f"CE Vol: {ce_vol:,} | PE Vol: {pe_vol:,} | Ratio req: {OI_VOL_DOMINANCE_RATIO}x")

    if signal_type == "BEARISH":
        # Heavy CE OI + CE vol decisively > PE vol
        detected = ce_pct >= 60 and ce_vol > (pe_vol * OI_VOL_DOMINANCE_RATIO)
        return detected, detail
    else:
        # Heavy PE OI + PE vol decisively > CE vol
        detected = pe_pct >= 60 and pe_vol > (ce_vol * OI_VOL_DOMINANCE_RATIO)
        return detected, detail


def find_first_itm_strike(option_chain, atm_strike, direction, underlying=None):
    if underlying is None:
        underlying = UNDERLYING
    step          = ATM_ROUNDING.get(underlying, 50)
    target_strike = atm_strike - step if direction == "BEARISH" else atm_strike + step
    opt_type      = "PE"            if direction == "BEARISH" else "CE"
    for o in option_chain:
        if o["strike"] == target_strike and o["type"] == opt_type:
            return o["trading_symbol"], o["ltp"]
    return None, 0


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
            return False, "No OHLC data", 0
        confirmed = spot < day_open if direction == "BEARISH" else spot > day_open
        arrow     = "below"         if direction == "BEARISH" else "above"
        return (confirmed,
                f"Spot ₹{spot:,.0f} {'✅' if confirmed else '❌'} {arrow} open ₹{day_open:,.0f}",
                day_open)
    except Exception as e:
        return False, f"OHLC error: {e}", 0


# ── MAIN SIGNAL CHECK ─────────────────────────────────────────

def run_zero_hero_check(groww, underlying=None):
    """
    Full Zero Hero checklist.  Backwards compatible: run_zero_hero_check(groww)
    defaults to config.UNDERLYING ("NIFTY").

    New gates vs v3:
      - DTE gate (MIN_DAYS_TO_EXPIRY)
      - VIX_MIN gate (too quiet → not suitable for directional buys)
      - IV rank gate (only buy cheap options)
      - Volume dominance ratio (inside check_oi_dominance)

    Entry dict now also carries:
      - day_open  (for spot-recross thesis invalidation in paper_trader)
      - iv_rank   (for logging / Telegram notification)
      - dte       (for logging)
    """
    if underlying is None:
        underlying = UNDERLYING

    _exchange_str = EXPIRY_EXCHANGE.get(underlying, "NSE")
    exchange      = getattr(groww, f"EXCHANGE_{_exchange_str}", _exchange_str)

    checklist = {}

    # ── Stage 1: Pre-market gates ──────────────────────────────
    vix = get_india_vix(groww)

    checklist["vix_max"] = {
        "passed": vix <= VIX_MAX,
        "detail": f"VIX={vix} ({'✅ ≤ {VIX_MAX}' if vix <= VIX_MAX else f'❌ > {VIX_MAX} — IV too expensive to buy'})"
    }
    checklist["vix_min"] = {
        "passed": vix >= VIX_MIN,
        "detail": f"VIX={vix} ({'✅ ≥ {VIX_MIN}' if vix >= VIX_MIN else f'❌ < {VIX_MIN} — market too quiet, use Iron Condor'})"
    }

    expiry_today = is_expiry_day(underlying)
    checklist["expiry_day"] = {
        "passed": not expiry_today,
        "detail": f"❌ Expiry day ({underlying})" if expiry_today else f"✅ Not expiry day ({underlying})"
    }

    in_window = is_within_trading_window()
    checklist["time_window"] = {
        "passed": in_window,
        "detail": f"Time {datetime.datetime.now().strftime('%H:%M')} — {'✅ In window' if in_window else '❌ Outside 9:45–14:00'}"
    }

    if not all(v["passed"] for v in checklist.values()):
        return {"signal": "NO_SIGNAL", "go": False, "checklist": checklist,
                "entry": {}, "underlying": underlying}

    # ── Fetch market data ───────────────────────────────────────
    expiry = get_best_expiry(groww, underlying)
    dte    = get_days_to_expiry(expiry) if expiry else 0

    # DTE gate — skip if too close to expiry (theta spike zone)
    checklist["dte"] = {
        "passed": dte >= MIN_DAYS_TO_EXPIRY,
        "detail": (f"DTE={dte} ✅ (expiry {expiry})" if dte >= MIN_DAYS_TO_EXPIRY
                   else f"DTE={dte} ❌ < {MIN_DAYS_TO_EXPIRY} — theta spike zone, skipping")
    }
    if not checklist["dte"]["passed"]:
        return {"signal": "NO_SIGNAL", "go": False, "checklist": checklist,
                "entry": {}, "underlying": underlying}

    spot = get_spot_price(groww, underlying)
    atm  = get_atm_strike(spot, underlying)

    chain_resp   = groww.get_option_chain(
        exchange=exchange, underlying=underlying, expiry_date=expiry
    )
    option_chain = parse_option_chain(chain_resp)
    pcr          = compute_pcr(option_chain)

    # Extract ATM IV and update rolling history
    atm_iv   = get_atm_iv(option_chain, atm)
    iv_rank  = get_iv_rank(underlying, atm_iv)
    update_iv_history(underlying, atm_iv)

    # IV rank gate — only buy when IV is cheap relative to recent history
    checklist["iv_rank"] = {
        "passed": iv_rank <= IV_BUY_MAX_RANK or iv_rank == 50,  # 50 = insufficient history → allow
        "detail": (f"IV rank={iv_rank}% ✅ (≤ {IV_BUY_MAX_RANK}% — cheap to buy)"
                   if iv_rank <= IV_BUY_MAX_RANK or iv_rank == 50
                   else f"IV rank={iv_rank}% ❌ > {IV_BUY_MAX_RANK}% — options expensive, skip")
    }

    print(f"  [{underlying}] Spot:₹{spot:,.0f} | ATM:{atm} | Expiry:{expiry} "
          f"| DTE:{dte} | PCR:{pcr} | IV:{atm_iv:.1%} | IVRank:{iv_rank}%")

    if not all(v["passed"] for v in checklist.values()):
        return {"signal": "NO_SIGNAL", "go": False, "checklist": checklist,
                "entry": {}, "underlying": underlying}

    # ── Stage 2: Signal ─────────────────────────────────────────
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

    checklist["oi_dominance"] = {
        "passed": signal != "NO_SIGNAL",
        "detail": f"{signal} | {oi_detail}"
    }

    pcr_ok = ((signal == "BEARISH" and pcr < PCR_BEARISH_THRESHOLD) or
              (signal == "BULLISH" and pcr > PCR_BULLISH_THRESHOLD))
    checklist["pcr"] = {
        "passed": pcr_ok,
        "detail": f"PCR={pcr} — {'✅ confirms ' + signal if pcr_ok else '❌ no confirmation'}"
    }

    if signal == "NO_SIGNAL" or not pcr_ok:
        return {"signal": signal, "go": False, "checklist": checklist,
                "entry": {}, "underlying": underlying}

    # ── Stage 3: Entry ──────────────────────────────────────────
    price_ok, price_detail, day_open = check_price_action(groww, signal, spot, underlying)
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
            "dte":            dte,
            "spot":           spot,
            "day_open":       day_open,   # NEW: for thesis-invalidation SL
            "pcr":            pcr,
            "vix":            vix,
            "iv_rank":        iv_rank,    # NEW: for logging
            "underlying":     underlying,
        }

    return {
        "signal":     signal,
        "go":         all_passed,
        "checklist":  checklist,
        "entry":      entry_info,
        "underlying": underlying,
    }


# ── SENTIMENT INTEGRATION ─────────────────────────────────────

from sentiment import get_full_sentiment, get_iron_condor_strikes


def enrich_with_sentiment(result, option_chain, spot, atm, days_to_exp, vix, pcr):
    """
    Sentiment layer. option_chain must be for the current underlying.
    """
    if not result.get("go") and result.get("signal") == "NO_SIGNAL":
        from sentiment import compute_range_bound_score, compute_iv_skew
        skew    = compute_iv_skew(option_chain, spot, atm, days_to_exp)
        range_b = compute_range_bound_score(
            vix, pcr, abs(skew["skew"]), spot, option_chain, atm)
        if range_b["score"] >= 8:
            ic = get_iron_condor_strikes(atm, option_chain)
            result["iron_condor_opportunity"] = {
                "detected": True, "structure": ic,
                "detail": range_b["detail"], "reasons": range_b["reasons"],
            }
        return result

    sentiment = get_full_sentiment(option_chain, spot, atm, days_to_exp, vix, pcr)
    result["sentiment"] = sentiment

    signal  = result.get("signal", "NO_SIGNAL")
    overall = sentiment["overall"]

    if signal == "BEARISH" and overall == "BULLISH":
        result["sentiment_conflict"] = True
        result["checklist"]["sentiment"] = {
            "passed": False,
            "detail": (f"⚠️ Conflict — BEARISH signal but BULLISH sentiment "
                       f"(skew:{sentiment['skew']['bias']} "
                       f"breadth:{sentiment['breadth']['breadth']} "
                       f"FII:{sentiment['fii']['bias']})")
        }
        result["go"] = False
    elif signal == "BULLISH" and overall == "BEARISH":
        result["sentiment_conflict"] = True
        result["checklist"]["sentiment"] = {
            "passed": False,
            "detail": "⚠️ Conflict — BULLISH signal but BEARISH sentiment"
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

    range_b = sentiment["range_bound"]
    if range_b["score"] >= 8:
        ic = get_iron_condor_strikes(atm, option_chain)
        result["iron_condor_opportunity"] = {
            "detected": True, "structure": ic, "detail": range_b["detail"],
        }

    parity = sentiment["parity"]
    if parity["mispriced"] != "NONE":
        result["parity_note"] = parity["detail"]

    return result
