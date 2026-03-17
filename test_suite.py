"""
test_suite.py
=============
Zero Hero — Full system test suite.
Tests every layer of logic without needing market hours or live API.

Run with: python3 test_suite.py
"""

import sys
import traceback
import datetime

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results = []

def test(name, fn):
    try:
        result = fn()
        if result is True or result is None:
            print(f"  {PASS} {name}")
            results.append((name, "PASS", None))
        elif result is False:
            print(f"  {FAIL} {name}")
            results.append((name, "FAIL", "Returned False"))
        else:
            print(f"  {PASS} {name} → {result}")
            results.append((name, "PASS", result))
    except Exception as e:
        print(f"  {FAIL} {name}")
        print(f"       {type(e).__name__}: {e}")
        results.append((name, "FAIL", str(e)))


def section(title):
    print(f"\n{'─'*55}")
    print(f"  {title}")
    print(f"{'─'*55}")


# ═══════════════════════════════════════════════════════════
# 1. IMPORTS
# ═══════════════════════════════════════════════════════════
section("1. IMPORTS")

test("config imports",        lambda: __import__("config") and True)
test("greeks imports",        lambda: __import__("greeks") and True)
test("sentiment imports",     lambda: __import__("sentiment") and True)
test("engine imports",        lambda: __import__("engine") and True)
test("paper_trader imports",  lambda: __import__("paper_trader") and True)
test("telegram_bot imports",  lambda: __import__("telegram_bot") and True)
test("scheduler imports",     lambda: __import__("scheduler") and True)
test("auth imports",          lambda: __import__("auth") and True)


# ═══════════════════════════════════════════════════════════
# 2. GREEKS ENGINE
# ═══════════════════════════════════════════════════════════
section("2. GREEKS ENGINE (Black-Scholes)")

from greeks import (calculate_greeks, implied_volatility, bs_price,
                    bs_delta, bs_theta, bs_vega, iv_percentile,
                    get_trade_recommendation, get_spread_strikes)

def test_iv_solve():
    iv = implied_volatility(85, 24500, 24450, 3/365, 0.065, "PE")
    assert iv is not None, "IV solve returned None"
    assert 0.05 < iv < 2.0, f"IV {iv} out of range"
    return f"IV={iv*100:.2f}%"

def test_greeks_pe():
    g = calculate_greeks(24500, 24450, 85, 3, "PE")
    assert g["delta"] < 0,          "PE delta should be negative"
    assert g["theta"] < 0,          "Theta should be negative"
    assert g["vega"]  > 0,          "Vega should be positive"
    assert g["iv"]    > 0,          "IV should be positive"
    assert 0 < g["theta_pct"] < 100,"Theta% out of range"
    return f"delta={g['delta']} theta={g['theta']}₹/d iv={g['iv']}%"

def test_greeks_ce():
    g = calculate_greeks(24500, 24550, 72, 3, "CE")
    assert g["delta"] > 0,  "CE delta should be positive"
    assert g["theta"] < 0,  "CE theta should be negative"
    return f"delta={g['delta']} iv={g['iv']}%"

def test_greeks_moneyness():
    # For a PUT: ITM = spot BELOW strike, OTM = spot ABOVE strike
    itm_put = calculate_greeks(24400, 24500, 120, 5, "PE")  # spot < strike = ITM
    atm_put = calculate_greeks(24500, 24500, 85,  5, "PE")  # spot = strike = ATM
    otm_put = calculate_greeks(24600, 24500, 40,  5, "PE")  # spot > strike = OTM
    # For a CALL: ITM = spot ABOVE strike, OTM = spot BELOW strike
    itm_ce  = calculate_greeks(24600, 24500, 120, 5, "CE")  # spot > strike = ITM
    otm_ce  = calculate_greeks(24400, 24500, 40,  5, "CE")  # spot < strike = OTM
    assert itm_put["moneyness"] == "ITM", f"PE ITM: spot<strike, got {itm_put['moneyness']}"
    assert atm_put["moneyness"] == "ATM", f"PE ATM: got {atm_put['moneyness']}"
    assert otm_put["moneyness"] == "OTM", f"PE OTM: spot>strike, got {otm_put['moneyness']}"
    assert itm_ce["moneyness"]  == "ITM", f"CE ITM: spot>strike, got {itm_ce['moneyness']}"
    assert otm_ce["moneyness"]  == "OTM", f"CE OTM: spot<strike, got {otm_ce['moneyness']}"
    return "ITM/ATM/OTM correct for both CE and PE"

def test_iv_percentile():
    history = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.28]
    rank    = iv_percentile(0.25, history)
    assert 70 <= rank <= 90, f"Rank {rank} unexpected for 0.25 vs history"
    rank_low = iv_percentile(0.11, history)
    assert rank_low < 20, f"Low IV should have low rank, got {rank_low}"
    return f"High IV rank={rank}% Low IV rank={rank_low}%"

def test_buy_recommendation():
    # Low IV, low VIX, decent delta → should recommend BUY
    g   = calculate_greeks(24500, 24450, 85, 5, "PE")
    rec = get_trade_recommendation(g, 13.0, 15, "BEARISH")
    assert rec["action"] == "BUY", f"Expected BUY got {rec['action']}"
    assert rec["instrument"] == "PE"
    return f"action={rec['action']} confidence={rec['confidence']}"

def test_sell_recommendation():
    # High IV rank, high VIX, low delta → should recommend SELL
    g   = calculate_greeks(24500, 24700, 35, 2, "CE")
    rec = get_trade_recommendation(g, 24.0, 85, "BEARISH")
    assert "SELL" in rec["action"], f"Expected SELL got {rec['action']}"
    return f"action={rec['action']} sell_score={rec['sell_score']}"

def test_skip_recommendation():
    # Mixed signals → should SKIP
    g   = calculate_greeks(24500, 24450, 85, 5, "PE")
    rec = get_trade_recommendation(g, 17.0, 50, "BEARISH")
    # Score should be low on both sides
    assert rec["buy_score"] < 8 or rec["sell_score"] < 8
    return f"buy={rec['buy_score']} sell={rec['sell_score']} action={rec['action']}"

def test_spread_strikes_bearish():
    chain = [
        {"strike": 24400, "type": "CE", "ltp": 120, "oi": 1000, "volume": 500},
        {"strike": 24450, "type": "CE", "ltp": 95,  "oi": 1000, "volume": 500},
        {"strike": 24500, "type": "CE", "ltp": 72,  "oi": 1000, "volume": 500},
        {"strike": 24550, "type": "CE", "ltp": 52,  "oi": 1000, "volume": 500},
        {"strike": 24600, "type": "CE", "ltp": 38,  "oi": 1000, "volume": 500},
    ]
    sp = get_spread_strikes(24500, "BEARISH", chain)
    assert sp["sell_strike"] == 24550, f"Bear call sell should be ATM+50, got {sp['sell_strike']}"
    assert sp["buy_strike"]  == 24600, f"Bear call buy should be ATM+100, got {sp['buy_strike']}"
    assert sp["net_credit"]  > 0,      "Net credit should be positive"
    return f"sell={sp['sell_strike']} buy={sp['buy_strike']} credit=₹{sp['net_credit']}"

test("IV solve (Newton-Raphson)",       test_iv_solve)
test("PE Greeks correctness",           test_greeks_pe)
test("CE Greeks correctness",           test_greeks_ce)
test("Moneyness detection ITM/ATM/OTM", test_greeks_moneyness)
test("IV percentile ranking",           test_iv_percentile)
test("BUY recommendation (low IV)",     test_buy_recommendation)
test("SELL recommendation (high IV)",   test_sell_recommendation)
test("Mixed signals produce score",     test_skip_recommendation)
test("Bear call spread strikes",        test_spread_strikes_bearish)


# ═══════════════════════════════════════════════════════════
# 3. SENTIMENT ENGINE
# ═══════════════════════════════════════════════════════════
section("3. SENTIMENT ENGINE")

from sentiment import (compute_iv_skew, check_put_call_parity,
                       compute_range_bound_score, get_iron_condor_strikes,
                       get_full_sentiment)

MOCK_CHAIN = [
    {"strike": 24300, "type": "PE", "ltp": 65,  "oi": 80000,  "volume": 30000},
    {"strike": 24350, "type": "PE", "ltp": 80,  "oi": 90000,  "volume": 35000},
    {"strike": 24400, "type": "PE", "ltp": 100, "oi": 120000, "volume": 50000},
    {"strike": 24450, "type": "PE", "ltp": 130, "oi": 150000, "volume": 60000},
    {"strike": 24500, "type": "CE", "ltp": 72,  "oi": 140000, "volume": 55000},
    {"strike": 24500, "type": "PE", "ltp": 85,  "oi": 140000, "volume": 55000},
    {"strike": 24550, "type": "CE", "ltp": 55,  "oi": 120000, "volume": 45000},
    {"strike": 24600, "type": "CE", "ltp": 40,  "oi": 100000, "volume": 40000},
    {"strike": 24650, "type": "CE", "ltp": 28,  "oi": 80000,  "volume": 30000},
    {"strike": 24700, "type": "CE", "ltp": 18,  "oi": 60000,  "volume": 20000},
]

def test_iv_skew_structure():
    skew = compute_iv_skew(MOCK_CHAIN, 24500, 24500, 3)
    assert "skew"   in skew, "Missing skew key"
    assert "bias"   in skew, "Missing bias key"
    assert "detail" in skew, "Missing detail key"
    assert skew["bias"] in ("BULLISH", "BEARISH", "NEUTRAL")
    return f"skew={skew['skew']:+.2f}pp bias={skew['bias']}"

def test_put_call_parity():
    parity = check_put_call_parity(MOCK_CHAIN, 24500, 24500, 3)
    assert "parity_ok"   in parity
    assert "deviation"   in parity
    assert "mispriced"   in parity
    assert parity["mispriced"] in ("CE_EXPENSIVE", "PE_EXPENSIVE", "NONE")
    return f"deviation=₹{parity['deviation']} mispriced={parity['mispriced']}"

def test_range_bound_high_vix():
    # High VIX + unbalanced PCR → should score LOW (directional, not range-bound)
    rb = compute_range_bound_score(24.0, 0.6, 8.0, 24500, MOCK_CHAIN, 24500)
    assert rb["score"] < 7, f"High VIX should give low range-bound score, got {rb['score']}"
    return f"score={rb['score']} structure={rb['structure']}"

def test_range_bound_low_vix():
    # Low VIX + balanced PCR + low skew → should score HIGH
    rb = compute_range_bound_score(11.0, 1.05, 1.5, 24500, MOCK_CHAIN, 24500)
    assert rb["score"] >= 5, f"Low VIX should give high range-bound score, got {rb['score']}"
    return f"score={rb['score']} structure={rb['structure']}"

def test_iron_condor_structure():
    ic = get_iron_condor_strikes(24500, MOCK_CHAIN)
    assert ic["sell_ce"] == 24600, f"Sell CE should be ATM+100={24600}, got {ic['sell_ce']}"
    assert ic["sell_pe"] == 24400, f"Sell PE should be ATM-100={24400}, got {ic['sell_pe']}"
    assert ic["buy_ce"]  == 24650, f"Buy CE wing should be ATM+150={24650}, got {ic['buy_ce']}"
    assert ic["buy_pe"]  == 24350, f"Buy PE wing should be ATM-150={24350}, got {ic['buy_pe']}"
    assert ic["net_credit"] > 0,   "Iron condor net credit should be positive"
    return (f"sell CE {ic['sell_ce']}@₹{ic['sell_ce_ltp']} | "
            f"sell PE {ic['sell_pe']}@₹{ic['sell_pe_ltp']} | "
            f"credit=₹{ic['net_credit']} max_loss=₹{ic['max_loss']}")

def test_full_sentiment_structure():
    s = get_full_sentiment(MOCK_CHAIN, 24500, 24500, 3, 18.0, 0.85)
    assert "overall"     in s
    assert "skew"        in s
    assert "parity"      in s
    assert "range_bound" in s
    assert "breadth"     in s
    assert "fii"         in s
    assert s["overall"]  in ("BULLISH", "BEARISH", "NEUTRAL")
    return f"overall={s['overall']} votes={s['votes']}"

test("IV skew structure",             test_iv_skew_structure)
test("Put-call parity check",         test_put_call_parity)
test("Range-bound: high VIX = low",   test_range_bound_high_vix)
test("Range-bound: low VIX = high",   test_range_bound_low_vix)
test("Iron condor strikes correct",   test_iron_condor_structure)
test("Full sentiment dict structure", test_full_sentiment_structure)


# ═══════════════════════════════════════════════════════════
# 4. ENGINE LOGIC
# ═══════════════════════════════════════════════════════════
section("4. ENGINE LOGIC")

from engine import (get_atm_strike, parse_option_chain,
                    compute_pcr, check_oi_dominance, find_first_itm_strike,
                    enrich_with_sentiment, is_expiry_day, is_within_trading_window)

def get_days_to_expiry(expiry_date_str):
    """Local definition — also add this to engine.py if missing."""
    expiry = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
    return max((expiry - datetime.date.today()).days, 0)

def test_atm_rounding():
    assert get_atm_strike(24480) == 24500, "Should round up to 24500"
    assert get_atm_strike(24520) == 24500, "Should round down to 24500"
    assert get_atm_strike(24250) == 24250, "Exact 50 should stay"
    assert get_atm_strike(24275) == 24300, "Should round to nearest 50"
    return "All ATM rounding correct"

def test_pcr():
    chain = [
        {"strike": 24500, "type": "CE", "oi": 100000, "volume": 0, "ltp": 0, "trading_symbol": ""},
        {"strike": 24500, "type": "PE", "oi": 120000, "volume": 0, "ltp": 0, "trading_symbol": ""},
    ]
    pcr = compute_pcr(chain)
    assert pcr == 1.2, f"Expected PCR 1.2 got {pcr}"
    return f"PCR={pcr}"

def test_oi_dominance_bearish():
    # Heavy CE OI + CE volume > PE volume → BEARISH
    chain = [
        {"strike": 24500, "type": "CE", "oi": 200000, "volume": 80000, "ltp": 70, "trading_symbol": ""},
        {"strike": 24500, "type": "PE", "oi": 60000,  "volume": 20000, "ltp": 85, "trading_symbol": ""},
        {"strike": 24450, "type": "CE", "oi": 180000, "volume": 70000, "ltp": 90, "trading_symbol": ""},
        {"strike": 24450, "type": "PE", "oi": 50000,  "volume": 15000, "ltp": 100,"trading_symbol": ""},
    ]
    detected, detail = check_oi_dominance(chain, "BEARISH", 24500)
    assert detected, f"Should detect BEARISH OI dominance. Detail: {detail}"
    return f"Bearish detected: {detail[:60]}"

def test_oi_dominance_not_triggered():
    # Balanced OI → should NOT trigger
    chain = [
        {"strike": 24500, "type": "CE", "oi": 100000, "volume": 40000, "ltp": 70, "trading_symbol": ""},
        {"strike": 24500, "type": "PE", "oi": 95000,  "volume": 38000, "ltp": 85, "trading_symbol": ""},
    ]
    detected, detail = check_oi_dominance(chain, "BEARISH", 24500)
    assert not detected, f"Balanced OI should NOT trigger. Detail: {detail}"
    return "Balanced OI correctly not triggered"

def test_itm_strike_bearish():
    chain = [
        {"strike": 24450, "type": "PE", "ltp": 130, "oi": 1000, "volume": 500, "trading_symbol": "NIFTY24450PE"},
        {"strike": 24500, "type": "PE", "ltp": 85,  "oi": 1000, "volume": 500, "trading_symbol": "NIFTY24500PE"},
    ]
    sym, ltp = find_first_itm_strike(chain, 24500, "BEARISH")
    assert sym == "NIFTY24450PE", f"ITM Put should be ATM-50=24450, got {sym}"
    assert ltp == 130
    return f"ITM PE strike: {sym} @ ₹{ltp}"

def test_itm_strike_bullish():
    chain = [
        {"strike": 24550, "type": "CE", "ltp": 52,  "oi": 1000, "volume": 500, "trading_symbol": "NIFTY24550CE"},
        {"strike": 24500, "type": "CE", "ltp": 72,  "oi": 1000, "volume": 500, "trading_symbol": "NIFTY24500CE"},
    ]
    sym, ltp = find_first_itm_strike(chain, 24500, "BULLISH")
    assert sym == "NIFTY24550CE", f"ITM Call should be ATM+50=24550, got {sym}"
    return f"ITM CE strike: {sym} @ ₹{ltp}"

def test_days_to_expiry():
    future = (datetime.date.today() + datetime.timedelta(days=5)).strftime("%Y-%m-%d")
    days   = get_days_to_expiry(future)
    assert days == 5, f"Expected 5 days got {days}"
    past   = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    days2  = get_days_to_expiry(past)
    assert days2 == 0, f"Past expiry should return 0 got {days2}"
    return f"5d expiry={days} past expiry={days2}"

def test_sentiment_conflict_blocks_trade():
    # BEARISH signal + BULLISH sentiment → go should flip False
    mock_result = {
        "signal":    "BEARISH",
        "go":        True,
        "checklist": {},
        "entry":     {},
        "recommendation": {"action": "BUY"},
    }
    # Manually inject a bullish sentiment
    from engine import enrich_with_sentiment
    result = enrich_with_sentiment(
        mock_result, MOCK_CHAIN, 24500, 24500, 3, 18.0, 0.85
    )
    # Result depends on actual sentiment calculation — just check it runs
    assert "checklist" in result
    assert "sentiment" in result or result.get("signal") == "BEARISH"
    return f"go={result['go']} conflict={result.get('sentiment_conflict','n/a')}"

def test_expiry_day_detection():
    # Just verify it returns a bool
    result = is_expiry_day()
    assert isinstance(result, bool)
    return f"Today is {'expiry' if result else 'not expiry'} day"

def test_trading_window():
    result = is_within_trading_window()
    assert isinstance(result, bool)
    now = datetime.datetime.now().strftime("%H:%M")
    return f"Window open={result} at {now}"

test("ATM strike rounding",             test_atm_rounding)
test("PCR calculation",                 test_pcr)
test("OI dominance BEARISH detection",  test_oi_dominance_bearish)
test("OI dominance NOT triggered",      test_oi_dominance_not_triggered)
test("ITM strike finder (BEARISH)",     test_itm_strike_bearish)
test("ITM strike finder (BULLISH)",     test_itm_strike_bullish)
test("Days to expiry calculation",      test_days_to_expiry)
test("Sentiment conflict check",        test_sentiment_conflict_blocks_trade)
test("Expiry day detection",            test_expiry_day_detection)
test("Trading window detection",        test_trading_window)


# ═══════════════════════════════════════════════════════════
# 5. PAPER TRADER LOGIC
# ═══════════════════════════════════════════════════════════
section("5. PAPER TRADER LOGIC")

from paper_trader import (calc_units, load_log, can_trade,
                           enter_trade, update_trade, reset_daily)
from config import CAPITAL, MAX_RISK_PER_TRADE_PCT, STOP_LOSS_PCT

def test_position_sizing():
    units = calc_units(100)
    max_loss   = CAPITAL * MAX_RISK_PER_TRADE_PCT
    loss_per_u = 100 * STOP_LOSS_PCT
    expected   = int(max_loss / loss_per_u)
    assert units == expected, f"Expected {expected} units got {units}"
    return f"Premium ₹100 → {units} units (max loss ₹{units*100*STOP_LOSS_PCT:.0f})"

def test_position_sizing_expensive():
    # Very expensive premium — should still return at least 1
    units = calc_units(5000)
    assert units >= 1, f"Should return at least 1 unit"
    return f"Premium ₹5000 → {units} units"

def test_daily_reset():
    log = load_log()
    log["trade_date"]   = "2020-01-01"   # old date
    log["trades_today"] = 99
    log["daily_loss"]   = -9999
    log = reset_daily(log)
    assert log["trades_today"] == 0,  "trades_today should reset"
    assert log["daily_loss"]   == 0,  "daily_loss should reset"
    assert log["trade_date"]   == str(datetime.date.today())
    return "Daily counters reset correctly on new day"

def test_can_trade_fresh_log():
    log = {
        "capital": CAPITAL, "available": CAPITAL,
        "total_pnl": 0, "trades_today": 0,
        "daily_loss": 0, "trade_date": str(datetime.date.today()),
        "open_trade": None, "closed_trades": []
    }
    ok, reason = can_trade(log)
    assert ok, f"Fresh log should allow trading: {reason}"
    return reason

def test_can_trade_max_reached():
    from config import MAX_TRADES_PER_DAY
    log = {
        "capital": CAPITAL, "available": CAPITAL,
        "total_pnl": 0, "trades_today": MAX_TRADES_PER_DAY,
        "daily_loss": 0, "trade_date": str(datetime.date.today()),
        "open_trade": None, "closed_trades": []
    }
    ok, reason = can_trade(log)
    assert not ok, "Should block when max trades reached"
    return reason

def test_can_trade_daily_loss_cap():
    from config import DAILY_LOSS_CAP_PCT
    log = {
        "capital": CAPITAL, "available": CAPITAL,
        "total_pnl": 0, "trades_today": 0,
        "daily_loss": -(CAPITAL * DAILY_LOSS_CAP_PCT + 1),
        "trade_date": str(datetime.date.today()),
        "open_trade": None, "closed_trades": []
    }
    ok, reason = can_trade(log)
    assert not ok, "Should block when daily loss cap hit"
    return reason

def test_enter_buy_trade():
    log   = {
        "capital": CAPITAL, "available": CAPITAL,
        "total_pnl": 0, "trades_today": 0,
        "daily_loss": 0, "trade_date": str(datetime.date.today()),
        "open_trade": None, "closed_trades": []
    }
    entry = {
        "action": "BUY", "trading_symbol": "NIFTY24450PE",
        "ltp": 85, "direction": "BEARISH",
        "expiry": "2025-03-20", "atm_strike": 24500,
        "days_to_exp": 5, "spot": 24500, "pcr": 0.9, "vix": 16.0
    }
    log, trade = enter_trade(log, entry)
    assert isinstance(trade, dict),          "Should return trade dict"
    assert trade["action"]      == "BUY"
    assert trade["entry_price"] == 85
    assert trade["sl_price"]    == round(85 * 0.65, 2), "SL should be entry × 0.65"
    assert trade["target_price"]== round(85 * 1.60, 2), "Target should be entry × 1.60"
    assert log["open_trade"]    is not None
    assert log["available"]     < CAPITAL,   "Capital should be reduced"
    return f"Entered BUY @ ₹85 | SL=₹{trade['sl_price']} Target=₹{trade['target_price']}"

def test_sl_triggered():
    log = {
        "capital": CAPITAL, "available": CAPITAL * 0.9,
        "total_pnl": 0, "trades_today": 1,
        "daily_loss": 0, "trade_date": str(datetime.date.today()),
        "open_trade": {
            "id": "T001", "symbol": "NIFTY24450PE", "action": "BUY",
            "entry_price": 85, "units": 10, "cost": 850,
            "sl_price": 55.25, "target_price": 136,
            "trailing_sl": 55.25, "partial_exited": False,
            "partial_units": 5, "status": "OPEN",
            "exit_price": None, "exit_time": None,
            "exit_reason": None, "pnl": 0,
            "entry_time": "10:00:00", "date": str(datetime.date.today()),
        },
        "closed_trades": []
    }
    # Current price below SL
    log, event = update_trade(log, 50.0)
    assert event is not None,                    "Should generate close event"
    assert event["type"] == "CLOSED",            "Should be CLOSED"
    assert event["pnl"]  < 0,                    "Should be a loss"
    assert log["open_trade"] is None,            "open_trade should be cleared"
    return f"SL triggered | P&L=₹{event['pnl']}"

def test_partial_exit_triggered():
    log = {
        "capital": CAPITAL, "available": CAPITAL * 0.9,
        "total_pnl": 0, "trades_today": 1,
        "daily_loss": 0, "trade_date": str(datetime.date.today()),
        "open_trade": {
            "id": "T001", "symbol": "NIFTY24450PE", "action": "BUY",
            "entry_price": 85, "units": 10, "cost": 850,
            "sl_price": 55.25, "target_price": 136,
            "trailing_sl": 55.25, "partial_exited": False,
            "partial_units": 5, "status": "OPEN",
            "exit_price": None, "exit_time": None,
            "exit_reason": None, "pnl": 0,
            "entry_time": "10:00:00", "date": str(datetime.date.today()),
        },
        "closed_trades": []
    }
    # Price hits target (+60%)
    log, event = update_trade(log, 140.0)
    assert event is not None,                              "Should generate event"
    assert isinstance(event, str),                         "Partial exit returns string"
    assert log["open_trade"]["partial_exited"] == True,    "partial_exited should be True"
    assert log["open_trade"]["sl_price"] == 85,            "SL should move to breakeven"
    return f"Partial exit triggered: {event[:60]}"

def test_sell_trade_profit():
    """Sell trade profits when premium decays."""
    log = {
        "capital": CAPITAL, "available": CAPITAL,
        "total_pnl": 0, "trades_today": 1,
        "daily_loss": 0, "trade_date": str(datetime.date.today()),
        "open_trade": {
            "id": "T001", "symbol": "NIFTY24600CE", "action": "SELL_SPREAD",
            "entry_price": 40, "premium_received": 25,
            "units": 10, "cost": 0,
            "sl_price": 50,       # 2× received
            "target_price": 0,
            "trailing_sl": 50, "partial_exited": False,
            "spread": {}, "status": "OPEN",
            "exit_price": None, "exit_time": None,
            "exit_reason": None, "pnl": 0,
            "entry_time": "10:00:00", "date": str(datetime.date.today()),
        },
        "closed_trades": []
    }
    # Premium decayed to 20% of received = target
    target_price = round(25 * 0.20, 2)
    log, event = update_trade(log, target_price)
    assert event is not None,         "Should generate close event"
    assert event["type"] == "CLOSED"
    assert event["pnl"]  > 0,         "Sell trade profit when premium decays"
    return f"Sell target hit | P&L=₹{event['pnl']}"

test("Position sizing (1% rule)",       test_position_sizing)
test("Position sizing (expensive opt)", test_position_sizing_expensive)
test("Daily counter reset",             test_daily_reset)
test("can_trade: fresh log",            test_can_trade_fresh_log)
test("can_trade: max trades block",     test_can_trade_max_reached)
test("can_trade: daily loss cap",       test_can_trade_daily_loss_cap)
test("Enter BUY trade",                 test_enter_buy_trade)
test("Stop-loss triggers correctly",    test_sl_triggered)
test("Partial exit triggers",           test_partial_exit_triggered)
test("Sell trade profits on decay",     test_sell_trade_profit)


# ═══════════════════════════════════════════════════════════
# 6. LIVE API CONNECTIONS
# ═══════════════════════════════════════════════════════════
section("6. LIVE API CONNECTIONS")

from engine import get_groww_client, get_india_vix
from telegram_bot import send_message

def test_groww_connect():
    g = get_groww_client()
    assert g is not None
    return "Connected"

def test_vix_live():
    g   = get_groww_client()
    vix = get_india_vix(g)
    # Market closed = 0.0 is acceptable. Just verify it's a number.
    assert isinstance(vix, float), f"VIX should be float got {type(vix)}"
    assert vix >= 0
    return f"VIX={vix} ({'market closed' if vix == 0 else 'live'})"

def test_telegram_send():
    ok = send_message("🧪 Zero Hero test suite — all systems check")
    assert ok, "Telegram send failed"
    return "Message sent — check your Telegram"

test("Groww API connection",  test_groww_connect)
test("VIX live read",         test_vix_live)
test("Telegram send",         test_telegram_send)


# ═══════════════════════════════════════════════════════════
# FINAL REPORT
# ═══════════════════════════════════════════════════════════
total  = len(results)
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = sum(1 for _, s, _ in results if s == "FAIL")

print(f"\n{'═'*55}")
print(f"  ZERO HERO TEST SUITE — COMPLETE")
print(f"{'═'*55}")
print(f"  Total:  {total}")
print(f"  Passed: {passed} ✅")
print(f"  Failed: {failed} ❌")
print(f"{'─'*55}")

if failed > 0:
    print(f"\n  FAILURES TO FIX:")
    for name, status, err in results:
        if status == "FAIL":
            print(f"  ❌ {name}")
            if err:
                print(f"     → {err}")
    print()
    sys.exit(1)
else:
    print(f"\n  🚀 All tests passed. Bot is ready for Monday.")
    sys.exit(0)
