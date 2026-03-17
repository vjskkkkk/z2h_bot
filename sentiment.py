"""
sentiment.py
=============
Additional market context signals for Zero Hero.
All free, no extra API keys needed.

Computes:
1. IV Skew         — OTM PE IV vs OTM CE IV → directional bias
2. Put-Call Parity — Fair value check → mispricing flag
3. Range-bound     — Butterfly/Iron Condor opportunity score
4. NSE Breadth     — Advance/Decline ratio from NSE public data
5. FII net flow    — From NSE public endpoint (EOD, updates ~6 PM)
"""

import math
import requests
import datetime
from greeks import calculate_greeks, bs_price


# ── 1. IV SKEW ───────────────────────────────────────────────

def compute_iv_skew(option_chain, spot, atm_strike, days_to_expiry, r=0.065):
    """
    IV Skew = OTM PE IV minus OTM CE IV.

    Positive skew → puts more expensive → market fears downside → bearish
    Negative skew → calls more expensive → market chasing upside → bullish
    Near zero    → balanced → neutral

    Uses 2 strikes OTM on each side for robustness.
    """
    T = max(days_to_expiry / 365, 1/365)

    otm_ce_strike = atm_strike + 100   # 2 strikes OTM call
    otm_pe_strike = atm_strike - 100   # 2 strikes OTM put

    ce_ltp = next((o["ltp"] for o in option_chain
                   if o["strike"] == otm_ce_strike and o["type"] == "CE"), 0)
    pe_ltp = next((o["ltp"] for o in option_chain
                   if o["strike"] == otm_pe_strike and o["type"] == "PE"), 0)

    if ce_ltp <= 0 or pe_ltp <= 0:
        return {"skew": 0, "bias": "NEUTRAL", "detail": "Insufficient OTM data"}

    try:
        from greeks import implied_volatility
        ce_iv = implied_volatility(ce_ltp, spot, otm_ce_strike, T, r, "CE") or 0
        pe_iv = implied_volatility(pe_ltp, spot, otm_pe_strike, T, r, "PE") or 0
    except Exception:
        return {"skew": 0, "bias": "NEUTRAL", "detail": "IV solve failed"}

    skew = round((pe_iv - ce_iv) * 100, 2)   # in percentage points

    if skew > 3:
        bias   = "BEARISH"
        detail = f"Put skew {skew:+.1f}pp — market pricing downside protection"
    elif skew < -3:
        bias   = "BULLISH"
        detail = f"Call skew {skew:+.1f}pp — market chasing upside"
    else:
        bias   = "NEUTRAL"
        detail = f"Skew {skew:+.1f}pp — balanced IV"

    return {
        "skew":       skew,
        "ce_iv":      round(ce_iv * 100, 2),
        "pe_iv":      round(pe_iv * 100, 2),
        "bias":       bias,
        "detail":     detail,
    }


# ── 2. PUT-CALL PARITY CHECK ─────────────────────────────────

def check_put_call_parity(option_chain, spot, atm_strike, days_to_expiry, r=0.065):
    """
    Put-Call Parity: C - P = S - K * e^(-rT)
    If actual difference deviates significantly, one side is mispriced.

    Returns which side (CE or PE) appears cheap/expensive.
    Traders should buy the cheap side and sell the expensive side.
    """
    T = max(days_to_expiry / 365, 1/365)

    ce_ltp = next((o["ltp"] for o in option_chain
                   if o["strike"] == atm_strike and o["type"] == "CE"), 0)
    pe_ltp = next((o["ltp"] for o in option_chain
                   if o["strike"] == atm_strike and o["type"] == "PE"), 0)

    if ce_ltp <= 0 or pe_ltp <= 0:
        return {"parity_ok": True, "detail": "No ATM data for parity check"}

    actual_diff   = round(ce_ltp - pe_ltp, 2)
    theoretical   = round(spot - atm_strike * math.exp(-r * T), 2)
    deviation     = round(actual_diff - theoretical, 2)
    deviation_pct = round(abs(deviation) / ((ce_ltp + pe_ltp) / 2) * 100, 1)

    parity_ok = deviation_pct < 5.0   # within 5% is acceptable

    if abs(deviation) < 2:
        detail = f"Parity OK (diff ₹{actual_diff} vs theory ₹{theoretical})"
        mispriced = "NONE"
    elif deviation > 0:
        detail = f"CE overpriced by ₹{deviation:.1f} (or PE cheap)"
        mispriced = "CE_EXPENSIVE"
    else:
        detail = f"PE overpriced by ₹{abs(deviation):.1f} (or CE cheap)"
        mispriced = "PE_EXPENSIVE"

    return {
        "parity_ok":    parity_ok,
        "actual_diff":  actual_diff,
        "theoretical":  theoretical,
        "deviation":    deviation,
        "deviation_pct": deviation_pct,
        "mispriced":    mispriced,
        "detail":       detail,
    }


# ── 3. RANGE-BOUND / BUTTERFLY SCORE ─────────────────────────

def compute_range_bound_score(vix, pcr, skew_abs, spot, option_chain, atm_strike):
    """
    Score how range-bound the market is (0-10).
    High score → Iron Condor / Butterfly opportunity.
    Low score  → directional trade more appropriate.

    Factors:
    - Low VIX = low expected move
    - PCR near 1.0 = balanced sentiment
    - Low IV skew = no directional fear
    - Tight OI distribution around ATM
    """
    score = 0
    reasons = []

    # VIX component
    if vix <= 12:
        score += 3
        reasons.append(f"VIX {vix} (very low — small expected move)")
    elif vix <= 15:
        score += 2
        reasons.append(f"VIX {vix} (low)")
    elif vix <= 18:
        score += 1
        reasons.append(f"VIX {vix} (moderate)")

    # PCR near 1.0 = balanced
    pcr_distance = abs(pcr - 1.0)
    if pcr_distance <= 0.1:
        score += 3
        reasons.append(f"PCR {pcr} (perfectly balanced)")
    elif pcr_distance <= 0.2:
        score += 2
        reasons.append(f"PCR {pcr} (near balanced)")
    elif pcr_distance <= 0.3:
        score += 1

    # Low skew = no directional fear
    if skew_abs <= 2:
        score += 2
        reasons.append(f"Skew {skew_abs:.1f}pp (balanced IV)")
    elif skew_abs <= 4:
        score += 1

    # OI concentrated at ATM (vs spread out)
    nearby_oi  = sum(o["oi"] for o in option_chain
                     if abs(o["strike"] - atm_strike) <= 50)
    total_oi   = sum(o["oi"] for o in option_chain) or 1
    atm_concentration = nearby_oi / total_oi
    if atm_concentration >= 0.3:
        score += 2
        reasons.append(f"OI concentrated at ATM ({atm_concentration:.0%})")
    elif atm_concentration >= 0.2:
        score += 1

    # Iron Condor structure levels
    if score >= 8:
        structure = "IRON_CONDOR"
        detail    = "Strong range-bound signal — sell OTM CE + OTM PE"
    elif score >= 6:
        structure = "BUTTERFLY"
        detail    = "Moderate range-bound — butterfly spread appropriate"
    else:
        structure = "DIRECTIONAL"
        detail    = "Market not range-bound enough for neutral strategies"

    return {
        "score":     score,
        "structure": structure,
        "detail":    detail,
        "reasons":   reasons,
    }


def get_iron_condor_strikes(atm_strike, option_chain):
    """
    Iron Condor: Sell OTM CE + OTM PE, Buy further OTM CE + PE as wings.
    Returns full structure with credits and max loss.
    """
    sell_ce = atm_strike + 100
    buy_ce  = atm_strike + 150
    sell_pe = atm_strike - 100
    buy_pe  = atm_strike - 150

    def ltp(strike, otype):
        return next((o["ltp"] for o in option_chain
                     if o["strike"] == strike and o["type"] == otype), 0)

    sell_ce_ltp = ltp(sell_ce, "CE")
    buy_ce_ltp  = ltp(buy_ce,  "CE")
    sell_pe_ltp = ltp(sell_pe, "PE")
    buy_pe_ltp  = ltp(buy_pe,  "PE")

    net_credit  = round((sell_ce_ltp - buy_ce_ltp) + (sell_pe_ltp - buy_pe_ltp), 2)
    wing_width  = 50
    max_loss    = round(wing_width - net_credit, 2)

    return {
        "sell_ce": sell_ce, "sell_ce_ltp": sell_ce_ltp,
        "buy_ce":  buy_ce,  "buy_ce_ltp":  buy_ce_ltp,
        "sell_pe": sell_pe, "sell_pe_ltp": sell_pe_ltp,
        "buy_pe":  buy_pe,  "buy_pe_ltp":  buy_pe_ltp,
        "net_credit": net_credit,
        "max_loss":   max_loss,
        "rr_ratio":   round(net_credit / max_loss, 2) if max_loss > 0 else 0,
    }


# ── 4. NSE MARKET BREADTH ────────────────────────────────────

def get_nse_breadth():
    """
    Fetch Nifty 50 advance/decline from NSE public endpoint.
    Returns advance_pct: >60% = bullish breadth, <40% = bearish breadth.
    """
    try:
        url     = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050"
        headers = {
            "User-Agent":      "Mozilla/5.0",
            "Accept":          "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.nseindia.com",
        }
        session = requests.Session()
        # NSE requires a cookie — get it first
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        resp = session.get(url, headers=headers, timeout=5)
        data = resp.json()

        advances  = data.get("advances",  0)
        declines  = data.get("declines",  0)
        unchanged = data.get("unchanged", 0)
        total     = advances + declines + unchanged or 50

        advance_pct = round(advances / total * 100, 1)

        if advance_pct >= 65:
            breadth = "BULLISH"
            detail  = f"{advances} advancing ({advance_pct}%) — broad rally"
        elif advance_pct <= 35:
            breadth = "BEARISH"
            detail  = f"{declines} declining ({100-advance_pct:.0f}%) — broad selloff"
        else:
            breadth = "NEUTRAL"
            detail  = f"Mixed — {advances} up, {declines} down"

        return {
            "advances":    advances,
            "declines":    declines,
            "advance_pct": advance_pct,
            "breadth":     breadth,
            "detail":      detail,
        }
    except Exception as e:
        return {
            "advances": 0, "declines": 0, "advance_pct": 50,
            "breadth": "NEUTRAL", "detail": f"Breadth unavailable: {e}"
        }


# ── 5. FII NET FLOW ──────────────────────────────────────────

def get_fii_net_flow():
    """
    Fetch today's FII net buy/sell from NSE public endpoint.
    Note: Updates ~6 PM EOD. During market hours returns previous day.
    Treat as background context, not a trigger signal.
    """
    try:
        url     = "https://www.nseindia.com/api/fiidiiTradeReact"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept":     "application/json",
            "Referer":    "https://www.nseindia.com",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        resp = session.get(url, headers=headers, timeout=5)
        data = resp.json()

        # Get equity row
        fii_row = next(
            (row for row in data if row.get("category") in ("FII/FPI", "FII")),
            None
        )
        if not fii_row:
            return {"net": 0, "bias": "NEUTRAL", "detail": "FII data not found"}

        net = float(str(fii_row.get("netVal", "0")).replace(",", ""))

        if net > 1000:
            bias   = "BULLISH"
            detail = f"FII net BUY ₹{net:,.0f}Cr — institutional accumulation"
        elif net < -1000:
            bias   = "BEARISH"
            detail = f"FII net SELL ₹{abs(net):,.0f}Cr — institutional distribution"
        else:
            bias   = "NEUTRAL"
            detail = f"FII net ₹{net:,.0f}Cr — no strong bias"

        return {"net": net, "bias": bias, "detail": detail}

    except Exception as e:
        return {"net": 0, "bias": "NEUTRAL", "detail": f"FII data unavailable: {e}"}


# ── MASTER SENTIMENT FUNCTION ────────────────────────────────

def get_full_sentiment(option_chain, spot, atm_strike, days_to_expiry,
                       vix, pcr, r=0.065):
    """
    Run all sentiment checks and return a unified context dict.
    Called once per signal from engine.py.
    """
    skew      = compute_iv_skew(option_chain, spot, atm_strike, days_to_expiry, r)
    parity    = check_put_call_parity(option_chain, spot, atm_strike, days_to_expiry, r)
    range_b   = compute_range_bound_score(
                    vix, pcr, abs(skew["skew"]),
                    spot, option_chain, atm_strike)
    breadth   = get_nse_breadth()
    fii       = get_fii_net_flow()

    # Overall sentiment vote
    votes     = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
    votes[skew["bias"]]    += 2   # IV skew = high weight
    votes[breadth["breadth"]] += 1
    votes[fii["bias"]]     += 1

    overall = max(votes, key=votes.get)

    return {
        "overall":    overall,
        "votes":      votes,
        "skew":       skew,
        "parity":     parity,
        "range_bound": range_b,
        "breadth":    breadth,
        "fii":        fii,
    }
