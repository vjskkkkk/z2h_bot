"""
live/greeks.py
==============
Black-Scholes Greeks calculator. Runs locally — no API needed.
Calculates: Delta, Gamma, Theta, Vega, IV + buy/sell decision engine.
"""

import math
from scipy.stats import norm
from scipy.optimize import brentq
import numpy as np


# ── BLACK-SCHOLES CORE ───────────────────────────────────────

def bs_price(S, K, T, r, sigma, option_type="CE"):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0) if option_type == "CE" else max(K - S, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_delta(S, K, T, r, sigma, option_type="CE"):
    if T <= 0 or sigma <= 0:
        return 1.0 if (option_type == "CE" and S > K) else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return norm.cdf(d1) if option_type == "CE" else norm.cdf(d1) - 1


def bs_gamma(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def bs_theta(S, K, T, r, sigma, option_type="CE"):
    """Returns theta per calendar day."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    term1 = -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
    if option_type == "CE":
        term2 = -r * K * math.exp(-r * T) * norm.cdf(d2)
    else:
        term2 = r * K * math.exp(-r * T) * norm.cdf(-d2)
    return (term1 + term2) / 365


def bs_vega(S, K, T, r, sigma):
    """Returns vega per 1% move in IV."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return S * norm.pdf(d1) * math.sqrt(T) * 0.01


def implied_volatility(market_price, S, K, T, r, option_type="CE"):
    """Solve for IV using Brent's method. Returns decimal or None."""
    if T <= 0 or market_price <= 0:
        return None
    try:
        intrinsic = max(S - K, 0) if option_type == "CE" else max(K - S, 0)
        if market_price < intrinsic:
            return None
        iv = brentq(
            lambda sigma: bs_price(S, K, T, r, sigma, option_type) - market_price,
            0.001, 10.0, xtol=1e-6, maxiter=200
        )
        return iv
    except Exception:
        return None


# ── FULL GREEKS BUNDLE ───────────────────────────────────────

def calculate_greeks(spot, strike, ltp, days_to_expiry, option_type="CE", r=0.065):
    """Returns full dict of Greeks + derived signals."""
    T  = max(days_to_expiry / 365, 1/365)
    iv = implied_volatility(ltp, spot, strike, T, r, option_type) or 0.20

    delta     = bs_delta(spot, strike, T, r, iv, option_type)
    gamma     = bs_gamma(spot, strike, T, r, iv)
    theta     = bs_theta(spot, strike, T, r, iv, option_type)
    vega      = bs_vega(spot, strike, T, r, iv)
    theta_pct = abs(theta / ltp * 100) if ltp > 0 else 0

    moneyness = "ATM"
    if option_type == "CE":
        if spot > strike * 1.002:   moneyness = "ITM"
        elif spot < strike * 0.998: moneyness = "OTM"
    else:
        if spot < strike * 0.998:   moneyness = "ITM"
        elif spot > strike * 1.002: moneyness = "OTM"

    return {
        "iv":        round(iv * 100, 2),
        "delta":     round(delta, 3),
        "gamma":     round(gamma, 5),
        "theta":     round(theta, 2),
        "vega":      round(vega, 2),
        "theta_pct": round(theta_pct, 2),
        "moneyness": moneyness,
        "T":         round(T * 365, 1),
        "iv_raw":    iv,
    }


# ── IV PERCENTILE ────────────────────────────────────────────

def iv_percentile(current_iv_raw, iv_history):
    """
    What % of past days had IV lower than today?
    >80 = expensive (sell). <30 = cheap (buy).
    """
    if not iv_history:
        return 50
    below = sum(1 for v in iv_history if v < current_iv_raw)
    return round(below / len(iv_history) * 100, 1)


# ── BUY vs SELL DECISION ENGINE ─────────────────────────────

def get_trade_recommendation(greeks, vix, iv_pct_rank, signal_direction):
    """
    Scores buy vs sell conditions and recommends:
    BUY | SELL_NAKED | SELL_SPREAD | SKIP
    """
    warnings   = []
    iv         = greeks["iv"]
    delta      = abs(greeks["delta"])
    theta_pct  = greeks["theta_pct"]
    days_left  = greeks["T"]
    instrument = "PE" if signal_direction == "BEARISH" else "CE"

    # ── SELL SCORE ───────────────────────────────────────────
    sell_score = 0
    sell_reasons = []
    if iv_pct_rank >= 80:
        sell_score += 3
        sell_reasons.append(f"IV rank {iv_pct_rank}% (very expensive)")
    elif iv_pct_rank >= 65:
        sell_score += 2
        sell_reasons.append(f"IV rank {iv_pct_rank}% (elevated)")
    if vix >= 22:
        sell_score += 3
        sell_reasons.append(f"VIX {vix} (very high)")
    elif vix >= 18:
        sell_score += 2
        sell_reasons.append(f"VIX {vix} (elevated)")
    if theta_pct >= 2.5:
        sell_score += 2
        sell_reasons.append(f"Theta {theta_pct:.1f}%/day (fast decay)")
    elif theta_pct >= 1.5:
        sell_score += 1
        sell_reasons.append(f"Theta {theta_pct:.1f}%/day (moderate decay)")
    if delta <= 0.30:
        sell_score += 2
        sell_reasons.append(f"Delta {delta:.2f} (OTM — safer to sell)")
    elif delta <= 0.40:
        sell_score += 1
        sell_reasons.append(f"Delta {delta:.2f} (slightly OTM)")
    if days_left <= 3:
        sell_score += 2
        sell_reasons.append(f"{days_left:.0f}d to expiry (theta peak)")
    elif days_left <= 7:
        sell_score += 1
        sell_reasons.append(f"{days_left:.0f}d to expiry (theta building)")

    # ── BUY SCORE ────────────────────────────────────────────
    buy_score = 0
    buy_reasons = []
    if iv_pct_rank <= 25:
        buy_score += 3
        buy_reasons.append(f"IV rank {iv_pct_rank}% (very cheap)")
    elif iv_pct_rank <= 40:
        buy_score += 2
        buy_reasons.append(f"IV rank {iv_pct_rank}% (below average)")
    if vix <= 13:
        buy_score += 2
        buy_reasons.append(f"VIX {vix} (low — vol expansion likely)")
    elif vix <= 16:
        buy_score += 1
        buy_reasons.append(f"VIX {vix} (moderate)")
    if delta >= 0.50:
        buy_score += 2
        buy_reasons.append(f"Delta {delta:.2f} (ITM — highly responsive)")
    elif delta >= 0.40:
        buy_score += 1
        buy_reasons.append(f"Delta {delta:.2f} (near ATM)")
    if theta_pct <= 1.0:
        buy_score += 2
        buy_reasons.append(f"Theta only {theta_pct:.1f}%/day (slow decay)")
    elif theta_pct <= 1.5:
        buy_score += 1
        buy_reasons.append(f"Theta {theta_pct:.1f}%/day (manageable)")
    if days_left >= 5:
        buy_score += 1
        buy_reasons.append(f"{days_left:.0f}d to expiry (time buffer)")

    # ── DECISION ─────────────────────────────────────────────
    if sell_score > buy_score and sell_score >= 5:

        if iv_pct_rank >= 80 and vix >= 20 and days_left <= 3 and delta <= 0.30:
            action     = "SELL_NAKED"
            confidence = "HIGH"
            reason     = " | ".join(sell_reasons[:3])
            warnings.append("💰 Collect full premium — no hedge cost")
            warnings.append("⚠️ No protection — use strict mental SL at 2× premium received")
            if vix > 25:
                warnings.append("🚨 VIX very high — gap risk significant on naked sells")
        else:
            action     = "SELL_SPREAD"
            confidence = "HIGH" if sell_score >= 7 else "MEDIUM"
            reason     = " | ".join(sell_reasons[:3])
            warnings.append("🛡️ Spread limits max loss to spread width minus credit received")
            if days_left > 7:
                warnings.append("⚠️ Far from expiry — theta accrual slow initially")
            if delta > 0.40:
                warnings.append("⚠️ Sell strike too close to ATM — widen spread")

    elif buy_score > sell_score and buy_score >= 4:
        action     = "BUY"
        confidence = "HIGH" if buy_score >= 6 else "MEDIUM"
        reason     = " | ".join(buy_reasons[:3])
        if theta_pct > 2.0:
            warnings.append(f"⚠️ Theta eating {theta_pct:.1f}%/day — need quick move")
        if delta < 0.35:
            warnings.append("⚠️ Low delta — needs large directional move to profit")
        if days_left <= 2:
            warnings.append("🚨 Near expiry — extreme theta risk for buyers")

    else:
        action     = "SKIP"
        confidence = "LOW"
        reason     = (f"Mixed signals — Buy score {buy_score} vs Sell score {sell_score}. "
                     f"IV rank {iv_pct_rank}%, VIX {vix}. Wait for clearer setup.")
        warnings.append("🔍 Neither buy nor sell conditions dominant enough")

    return {
        "action":      action,
        "instrument":  instrument,
        "reason":      reason,
        "confidence":  confidence,
        "warnings":    warnings,
        "buy_score":   buy_score,
        "sell_score":  sell_score,
        "iv_pct_rank": iv_pct_rank,
    }


# ── SPREAD STRUCTURE CALCULATOR ──────────────────────────────

def get_spread_strikes(atm_strike, direction, option_chain):
    """
    BEARISH: Bear Call Spread — Sell OTM CE (ATM+50), Buy OTM CE (ATM+100)
    BULLISH: Bull Put Spread  — Sell OTM PE (ATM-50), Buy OTM PE (ATM-100)
    Both collect a net credit. Max profit = credit. Max loss = width - credit.
    """
    if direction == "BEARISH":
        opt_type    = "CE"
        sell_strike = atm_strike + 50
        buy_strike  = atm_strike + 100
    else:
        opt_type    = "PE"
        sell_strike = atm_strike - 50
        buy_strike  = atm_strike - 100

    sell_ltp = next((o["ltp"] for o in option_chain
                     if o["strike"] == sell_strike and o["type"] == opt_type), 0)
    buy_ltp  = next((o["ltp"] for o in option_chain
                     if o["strike"] == buy_strike  and o["type"] == opt_type), 0)

    net_credit   = round(sell_ltp - buy_ltp, 2)
    spread_width = abs(sell_strike - buy_strike)
    max_loss     = round(spread_width - net_credit, 2)

    return {
        "opt_type":    opt_type,
        "sell_strike": sell_strike,
        "buy_strike":  buy_strike,
        "sell_ltp":    sell_ltp,
        "buy_ltp":     buy_ltp,
        "net_credit":  net_credit,
        "max_profit":  net_credit,
        "max_loss":    max_loss,
        "rr_ratio":    round(net_credit / max_loss, 2) if max_loss > 0 else 0,
    }
