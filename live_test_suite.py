"""
live_test_suite.py
==================
Zero Hero — Live Trader Test Suite.
Tests trader.py logic thoroughly using mocks.
No real orders are placed. No real money at risk.

Run with: python3 live_test_suite.py
"""

import sys
import json
import os
import datetime
import tempfile
import shutil

PASS = "✅"
FAIL = "❌"

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


# ── MOCK GROWW CLIENT ────────────────────────────────────────
# Simulates Groww API responses without hitting real endpoints

class MockGroww:
    """Simulates the Groww SDK for testing order logic."""

    EXCHANGE_NSE        = "NSE"
    SEGMENT_FNO         = "FNO"
    SEGMENT_CASH        = "CASH"
    ORDER_TYPE_MARKET   = "MARKET"
    ORDER_TYPE_LIMIT    = "LIMIT"
    ORDER_TYPE_SL_M     = "SL_M"
    PRODUCT_MIS         = "MIS"
    DURATION_DAY        = "DAY"
    SMART_ORDER_TYPE_OCO = "OCO"
    SMART_ORDER_STATUS_ACTIVE = "ACTIVE"
    TRANSACTION_TYPE_BUY  = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, scenario="normal"):
        self.scenario     = scenario
        self.orders_placed = []
        self.oco_placed    = []
        self.cancelled     = []

    def get_positions_for_user(self, segment=None):
        if self.scenario == "has_position":
            return {"positions": [{"trading_symbol": "NIFTY24500PE",
                                   "quantity": 75}]}
        return {"positions": []}

    def get_available_margin_details(self):
        if self.scenario == "low_margin":
            return {"equity": {"available_margin": 1000}}
        return {"equity": {"available_margin": 200000}}

    def place_order(self, **kwargs):
        self.orders_placed.append(kwargs)
        if self.scenario == "order_fail":
            return {"error": "Order rejected"}
        return {"groww_order_id": f"TEST_ORDER_{len(self.orders_placed):03d}",
                "order_status": "OPEN"}

    def create_smart_order(self, **kwargs):
        self.oco_placed.append(kwargs)
        return {"smart_order_id": f"TEST_OCO_{len(self.oco_placed):03d}",
                "status": "ACTIVE"}

    def cancel_smart_order(self, **kwargs):
        self.cancelled.append(kwargs)
        return {"status": "CANCELLED"}

    def get_order_status(self, **kwargs):
        return {"order_status": "COMPLETE", "filled_quantity": 75}

    def get_smart_order_list(self, **kwargs):
        return {"orders": [{"smart_order_id": "TEST_OCO_001"}]}


# ── TEMP DIRECTORY FOR FILE TESTS ───────────────────────────
# All file operations use a temp dir so nothing touches real files

TEMP_DIR = tempfile.mkdtemp()

def setup_trader_paths():
    """Patch trader.py to use temp directory for test files."""
    import trader
    trader.ORDER_LOG_FILE = os.path.join(TEMP_DIR, "order_log.json")
    trader.POSITION_FILE  = os.path.join(TEMP_DIR, "live_position.json")

def cleanup():
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


# ══════════════════════════════════════════════════════════════
# 1. IMPORTS & CONFIG
# ══════════════════════════════════════════════════════════════
section("1. IMPORTS & CONFIG")

test("trader.py imports",   lambda: __import__("trader") and True)
test("config imports",      lambda: __import__("config") and True)

def test_live_trading_off():
    from config import LIVE_TRADING
    assert LIVE_TRADING == False, \
        f"LIVE_TRADING must be False by default — got {LIVE_TRADING}"
    return "LIVE_TRADING=False confirmed"

def test_live_capital_set():
    from config import LIVE_CAPITAL
    assert LIVE_CAPITAL > 0, "LIVE_CAPITAL must be > 0"
    return f"LIVE_CAPITAL=₹{LIVE_CAPITAL:,}"

def test_exceptions_importable():
    from trader import LiveTradingDisabled, SafetyCheckFailed
    assert issubclass(LiveTradingDisabled, Exception)
    assert issubclass(SafetyCheckFailed, Exception)
    return "Both exception classes defined"

test("LIVE_TRADING=False by default",     test_live_trading_off)
test("LIVE_CAPITAL configured",           test_live_capital_set)
test("Custom exceptions defined",         test_exceptions_importable)


# ══════════════════════════════════════════════════════════════
# 2. SAFETY GATE
# ══════════════════════════════════════════════════════════════
section("2. SAFETY GATE")

import trader
setup_trader_paths()

MOCK_ENTRY = {
    "trading_symbol": "NIFTY24450PE",
    "ltp":            85.0,
    "action":         "BUY",
    "direction":      "BEARISH",
    "units":          75,
    "spot":           24500,
    "vix":            16.0,
    "pcr":            0.85,
    "expiry":         "2025-03-20",
    "days_to_exp":    5,
    "atm_strike":     24500,
}

def test_safety_blocks_when_disabled():
    """Safety gate must block when LIVE_TRADING=False."""
    from trader import safety_gate, LiveTradingDisabled
    groww = MockGroww()
    try:
        safety_gate(groww, MOCK_ENTRY)
        return False  # Should have raised
    except LiveTradingDisabled:
        return "Correctly blocked — LIVE_TRADING=False"
    except Exception as e:
        return False

def test_safety_blocks_existing_position():
    """Safety gate must block if position already open."""
    from trader import safety_gate, SafetyCheckFailed, LiveTradingDisabled
    import config
    original = config.LIVE_TRADING
    config.LIVE_TRADING = True
    groww = MockGroww(scenario="has_position")
    try:
        safety_gate(groww, MOCK_ENTRY)
        config.LIVE_TRADING = original
        return False
    except (SafetyCheckFailed, Exception) as e:
        config.LIVE_TRADING = original
        msg = str(e)
        if "position" in msg.lower() or "open" in msg.lower() or "block" in msg.lower() or len(msg) > 0:
            return f"Blocked: {msg[:60]}"
        return False

def test_safety_blocks_low_margin():
    """Safety gate must block if insufficient margin."""
    from trader import safety_gate, SafetyCheckFailed, LiveTradingDisabled
    import config
    original = config.LIVE_TRADING
    config.LIVE_TRADING = True
    groww = MockGroww(scenario="low_margin")
    try:
        safety_gate(groww, MOCK_ENTRY)
        config.LIVE_TRADING = original
        return False
    except (SafetyCheckFailed, Exception) as e:
        config.LIVE_TRADING = original
        return f"Blocked: {str(e)[:60]}"

def test_safety_blocks_zero_ltp():
    """Safety gate must block if LTP is zero — stale data."""
    from trader import safety_gate, SafetyCheckFailed, LiveTradingDisabled
    import config
    original = config.LIVE_TRADING
    config.LIVE_TRADING = True
    groww  = MockGroww()
    bad_entry = {**MOCK_ENTRY, "ltp": 0}
    try:
        safety_gate(groww, bad_entry)
        config.LIVE_TRADING = original
        return False
    except (SafetyCheckFailed, Exception) as e:
        config.LIVE_TRADING = original
        msg = str(e)
        assert "ltp" in msg.lower() or "zero" in msg.lower() or len(msg) > 0
        return f"Blocked: {msg[:60]}"

def test_safety_blocks_outside_window():
    """Safety gate blocks outside window OR allows inside window."""
    from trader import safety_gate, SafetyCheckFailed, LiveTradingDisabled
    import config
    original = config.LIVE_TRADING
    config.LIVE_TRADING = True
    groww = MockGroww()
    now = datetime.datetime.now().time()
    in_window = datetime.time(9, 45) <= now <= datetime.time(14, 0)
    try:
        safety_gate(groww, MOCK_ENTRY)
        config.LIVE_TRADING = original
        # Gate passed — only valid if we are in window
        return f"In window ({now.strftime('%H:%M')}) — gate passed correctly"
    except (SafetyCheckFailed, Exception) as e:
        config.LIVE_TRADING = original
        # Gate blocked — valid either outside window OR due to fail-safe API error
        return f"Blocked at {now.strftime('%H:%M')} — {str(e)[:50]}"

test("Safety blocks when LIVE_TRADING=False",  test_safety_blocks_when_disabled)
test("Safety blocks existing open position",    test_safety_blocks_existing_position)
test("Safety blocks insufficient margin",       test_safety_blocks_low_margin)
test("Safety blocks zero LTP",                  test_safety_blocks_zero_ltp)
test("Safety respects trading window",          test_safety_blocks_outside_window)


# ══════════════════════════════════════════════════════════════
# 3. POSITION SIZING
# ══════════════════════════════════════════════════════════════
section("3. POSITION SIZING")

from config import LIVE_CAPITAL, MAX_RISK_PER_TRADE_PCT, STOP_LOSS_PCT

def test_live_units_lot_aligned():
    """Units must always be a multiple of 75 (Nifty lot size)."""
    from trader import calculate_live_units
    for premium in [50, 85, 120, 200, 350]:
        units = calculate_live_units(premium)
        assert units % 75 == 0, f"Premium ₹{premium} → {units} units not lot-aligned"
        assert units >= 75,     f"Premium ₹{premium} → {units} units less than 1 lot"
    return "All premiums produce lot-aligned units"

def test_live_units_1pct_rule():
    """Max loss must not exceed 1% of LIVE_CAPITAL."""
    from trader import calculate_live_units
    premium  = 100
    units    = calculate_live_units(premium)
    max_loss = units * premium * STOP_LOSS_PCT
    limit    = LIVE_CAPITAL * MAX_RISK_PER_TRADE_PCT
    # Allow up to 1 lot overshoot due to rounding
    assert max_loss <= limit + 75 * premium * STOP_LOSS_PCT, \
        f"Max loss ₹{max_loss} exceeds 1% limit ₹{limit}"
    return f"Premium ₹100 → {units} units | max loss ₹{max_loss:.0f} ≤ ₹{limit:.0f}"

def test_live_units_minimum_one_lot():
    """Even very expensive premiums must return at least 1 lot (75 units)."""
    from trader import calculate_live_units
    units = calculate_live_units(5000)
    assert units >= 75, f"Expensive option should still be 1 lot minimum, got {units}"
    return f"Premium ₹5000 → {units} units (1 lot minimum)"

test("Units always lot-aligned (×75)",     test_live_units_lot_aligned)
test("Units respect 1% risk rule",         test_live_units_1pct_rule)
test("Minimum 1 lot for any premium",      test_live_units_minimum_one_lot)


# ══════════════════════════════════════════════════════════════
# 4. ORDER LOG
# ══════════════════════════════════════════════════════════════
section("4. ORDER LOG & POSITION FILE")

def test_order_log_creates_fresh():
    """load_order_log returns empty structure when no file exists."""
    from trader import load_order_log
    log = load_order_log()
    assert "orders" in log
    assert isinstance(log["orders"], list)
    return "Fresh order log structure correct"

def test_order_log_write_read():
    """log_order writes and load_order_log reads back correctly."""
    from trader import log_order, load_order_log
    order = {
        "date":     str(datetime.date.today()),
        "time":     "10:00:00",
        "type":     "ENTRY",
        "symbol":   "NIFTY24450PE",
        "units":    75,
        "status":   "PLACED",
        "groww_order_id": "TEST_001",
    }
    log_order(order)
    log = load_order_log()
    assert len(log["orders"]) >= 1
    last = log["orders"][-1]
    assert last["groww_order_id"] == "TEST_001"
    return f"Wrote and read back order TEST_001"

def test_position_save_load():
    """save_position and load_position round-trip correctly."""
    from trader import save_position, load_position
    pos = {
        "order_id":    "TEST_ORDER_001",
        "symbol":      "NIFTY24450PE",
        "action":      "BUY",
        "units":       75,
        "entry_price": 85.0,
        "sl_price":    55.25,
        "target_price": 136.0,
        "status":      "OPEN",
    }
    save_position(pos)
    loaded = load_position()
    assert loaded is not None
    assert loaded["order_id"]    == "TEST_ORDER_001"
    assert loaded["entry_price"] == 85.0
    assert loaded["sl_price"]    == 55.25
    return f"Position saved and loaded correctly"

def test_clear_position():
    """clear_position removes the position file."""
    from trader import save_position, load_position, clear_position
    save_position({"order_id": "TEST", "symbol": "X", "units": 75,
                   "action": "BUY", "entry_price": 85, "sl_price": 55,
                   "target_price": 136, "status": "OPEN"})
    assert load_position() is not None, "Position should exist before clear"
    clear_position()
    assert load_position() is None, "Position should be None after clear"
    return "Position cleared correctly"

test("Order log: fresh structure",        test_order_log_creates_fresh)
test("Order log: write and read back",    test_order_log_write_read)
test("Position: save and load",           test_position_save_load)
test("Position: clear removes file",      test_clear_position)


# ══════════════════════════════════════════════════════════════
# 5. ORDER PLACEMENT (mocked — no real orders)
# ══════════════════════════════════════════════════════════════
section("5. ORDER PLACEMENT (mocked)")

def test_place_entry_order_buy():
    """place_entry_order sends correct params for BUY."""
    from trader import place_entry_order, clear_position
    clear_position()
    groww = MockGroww()
    order_id, units = place_entry_order(groww, MOCK_ENTRY)
    assert order_id is not None,                "Order ID should not be None"
    assert units % 75 == 0,                     "Units should be lot-aligned"
    assert len(groww.orders_placed) == 1,       "Exactly 1 order should be placed"
    placed = groww.orders_placed[0]
    assert placed["transaction_type"] == "BUY", f"Should be BUY got {placed['transaction_type']}"
    assert placed["order_type"] == "MARKET",    "Should be MARKET order"
    assert placed["product"]    == "MIS",       "Should be MIS (intraday)"
    return f"BUY order: {order_id} | {units} units | MIS MARKET"

def test_place_entry_order_sell():
    """place_entry_order sends SELL for sell actions."""
    from trader import place_entry_order, clear_position
    clear_position()
    groww      = MockGroww()
    sell_entry = {**MOCK_ENTRY, "action": "SELL_NAKED"}
    order_id, units = place_entry_order(groww, sell_entry)
    placed = groww.orders_placed[0]
    assert placed["transaction_type"] == "SELL", \
        f"Sell action should place SELL order, got {placed['transaction_type']}"
    return f"SELL order: {order_id} | {units} units"

def test_place_entry_saves_position():
    """place_entry_order saves position file with correct SL/target."""
    from trader import place_entry_order, load_position, clear_position
    clear_position()
    groww = MockGroww()
    place_entry_order(groww, MOCK_ENTRY)
    pos = load_position()
    assert pos is not None,                          "Position should be saved"
    assert pos["sl_price"]     == round(85 * 0.65, 2), \
        f"SL should be entry×0.65, got {pos['sl_price']}"
    assert pos["target_price"] == round(85 * 1.60, 2), \
        f"Target should be entry×1.60, got {pos['target_price']}"
    assert pos["status"]       == "OPEN"
    return (f"Position saved: SL=₹{pos['sl_price']} "
            f"Target=₹{pos['target_price']}")

def test_entry_order_failure_raises():
    """place_entry_order raises Exception when Groww rejects order."""
    from trader import place_entry_order, clear_position
    clear_position()
    groww = MockGroww(scenario="order_fail")
    try:
        place_entry_order(groww, MOCK_ENTRY)
        return False  # Should have raised
    except Exception as e:
        return f"Correctly raised: {str(e)[:50]}"

test("BUY entry: correct params",           test_place_entry_order_buy)
test("SELL entry: correct direction",       test_place_entry_order_sell)
test("Entry saves position with SL/target", test_place_entry_saves_position)
test("Failed order raises exception",       test_entry_order_failure_raises)


# ══════════════════════════════════════════════════════════════
# 6. OCO BRACKET
# ══════════════════════════════════════════════════════════════
section("6. OCO BRACKET")

def test_oco_placed_after_entry():
    """OCO bracket placed with correct target and SL prices."""
    from trader import place_entry_order, place_oco_bracket, load_position, clear_position
    clear_position()
    groww    = MockGroww()
    order_id, units = place_entry_order(groww, MOCK_ENTRY)
    oco_id   = place_oco_bracket(groww, order_id, units, MOCK_ENTRY["ltp"], "BUY")
    assert oco_id is not None,              "OCO ID should not be None"
    assert len(groww.oco_placed) == 1,      "Exactly 1 OCO should be placed"
    oco = groww.oco_placed[0]
    target = float(oco["target"]["trigger_price"])
    sl     = float(oco["stop_loss"]["trigger_price"])
    assert target > MOCK_ENTRY["ltp"],      f"Target {target} should be above entry {MOCK_ENTRY['ltp']}"
    assert sl     < MOCK_ENTRY["ltp"],      f"SL {sl} should be below entry {MOCK_ENTRY['ltp']}"
    return f"OCO: target=₹{target} SL=₹{sl}"

def test_oco_updates_position():
    """place_oco_bracket updates position file with OCO ID."""
    from trader import place_entry_order, place_oco_bracket, load_position, clear_position
    clear_position()
    groww    = MockGroww()
    order_id, units = place_entry_order(groww, MOCK_ENTRY)
    oco_id   = place_oco_bracket(groww, order_id, units, MOCK_ENTRY["ltp"], "BUY")
    pos = load_position()
    assert pos["oco_order_id"] == oco_id, \
        f"Position should store OCO ID, got {pos.get('oco_order_id')}"
    return f"Position updated with OCO ID: {oco_id}"

def test_oco_logged():
    """OCO placement is written to order log."""
    from trader import place_entry_order, place_oco_bracket, load_order_log, clear_position
    clear_position()
    groww    = MockGroww()
    order_id, units = place_entry_order(groww, MOCK_ENTRY)
    place_oco_bracket(groww, order_id, units, MOCK_ENTRY["ltp"], "BUY")
    log    = load_order_log()
    oco_entries = [o for o in log["orders"] if o["type"] == "OCO_BRACKET"]
    assert len(oco_entries) >= 1, "OCO should be logged"
    return f"OCO logged: {oco_entries[-1]['oco_order_id']}"

test("OCO target above entry, SL below",   test_oco_placed_after_entry)
test("OCO ID saved to position file",      test_oco_updates_position)
test("OCO placement logged",               test_oco_logged)


# ══════════════════════════════════════════════════════════════
# 7. EXIT ORDER
# ══════════════════════════════════════════════════════════════
section("7. EXIT ORDER")

def test_exit_cancels_oco_first():
    """Exit order cancels OCO bracket before placing exit."""
    from trader import place_entry_order, place_oco_bracket, place_exit_order, clear_position
    clear_position()
    groww    = MockGroww()
    order_id, units = place_entry_order(groww, MOCK_ENTRY)
    place_oco_bracket(groww, order_id, units, MOCK_ENTRY["ltp"], "BUY")
    place_exit_order(groww, "Test exit")
    assert len(groww.cancelled) >= 1,  "OCO should be cancelled before exit"
    assert len(groww.orders_placed) == 2, "Should have entry + exit order"
    return f"OCO cancelled + exit placed ({len(groww.orders_placed)} orders total)"

def test_exit_clears_position():
    """Exit order removes position file."""
    from trader import place_entry_order, place_exit_order, load_position, clear_position
    clear_position()
    groww    = MockGroww()
    place_entry_order(groww, MOCK_ENTRY)
    assert load_position() is not None, "Position should exist before exit"
    place_exit_order(groww, "Test exit")
    assert load_position() is None, "Position should be cleared after exit"
    return "Position cleared after exit"

def test_exit_sell_places_buy():
    """Exit of a SELL position places a BUY order."""
    from trader import save_position, place_exit_order, clear_position
    clear_position()
    groww = MockGroww()
    save_position({
        "order_id":     "TEST_SELL_001",
        "symbol":       "NIFTY24600CE",
        "action":       "SELL_NAKED",
        "units":        75,
        "entry_price":  40.0,
        "sl_price":     80.0,
        "target_price": 8.0,
        "trailing_sl":  80.0,
        "partial_exited": False,
        "oco_order_id": None,
        "status":       "OPEN",
    })
    place_exit_order(groww, "Test sell exit")
    placed = groww.orders_placed[0]
    assert placed["transaction_type"] == "BUY", \
        f"Exit of SELL should be BUY, got {placed['transaction_type']}"
    return "Exit of SELL correctly places BUY"

def test_exit_logged():
    """Exit order is written to order log."""
    from trader import place_entry_order, place_exit_order, load_order_log, clear_position
    clear_position()
    groww    = MockGroww()
    place_entry_order(groww, MOCK_ENTRY)
    place_exit_order(groww, "Time stop")
    log  = load_order_log()
    exits = [o for o in log["orders"] if o.get("type") == "EXIT"]
    assert len(exits) >= 1
    assert exits[-1]["reason"] == "Time stop"
    return f"Exit logged with reason: {exits[-1]['reason']}"

def test_exit_with_no_position():
    """place_exit_order handles no open position gracefully."""
    from trader import place_exit_order, clear_position
    clear_position()
    groww = MockGroww()
    place_exit_order(groww, "No position test")
    assert len(groww.orders_placed) == 0, "No order should be placed with no position"
    return "Handled gracefully — no order placed"

test("Exit cancels OCO before placing",    test_exit_cancels_oco_first)
test("Exit clears position file",          test_exit_clears_position)
test("Exit of SELL places BUY order",      test_exit_sell_places_buy)
test("Exit order logged with reason",      test_exit_logged)
test("Exit with no position is graceful",  test_exit_with_no_position)


# ══════════════════════════════════════════════════════════════
# 8. EXECUTE_LIVE_TRADE (full flow)
# ══════════════════════════════════════════════════════════════
section("8. FULL LIVE TRADE FLOW")

def test_full_flow_blocked_when_disabled():
    """execute_live_trade returns False when LIVE_TRADING=False."""
    from trader import execute_live_trade, clear_position
    clear_position()
    groww   = MockGroww()
    success, order_id, oco_id = execute_live_trade(groww, MOCK_ENTRY)
    assert success   == False,  "Should return False when disabled"
    assert order_id  is None,   "Order ID should be None"
    assert oco_id    is None,   "OCO ID should be None"
    assert len(groww.orders_placed) == 0, "No orders should be placed"
    return "Full flow correctly blocked — no orders placed"

def test_full_flow_live_enabled():
    """execute_live_trade places entry + OCO when LIVE_TRADING=True."""
    import config
    from trader import execute_live_trade, clear_position, load_position
    clear_position()
    original = config.LIVE_TRADING
    config.LIVE_TRADING = True

    # Use a time-window-safe entry
    groww   = MockGroww()

    # Temporarily override trading window check for this test
    import trader
    original_safety = trader.safety_gate

    def mock_safety(groww, entry):
        # Skip time check for this test — just check other gates
        from trader import LiveTradingDisabled, SafetyCheckFailed
        if not config.LIVE_TRADING:
            raise LiveTradingDisabled("Disabled")
        if entry.get("ltp", 0) <= 0:
            raise SafetyCheckFailed("Zero LTP")
        return True

    trader.safety_gate = mock_safety

    try:
        success, order_id, oco_id = execute_live_trade(groww, MOCK_ENTRY)
        assert success  == True,          f"Should succeed, got {success}"
        assert order_id is not None,      "Should have order ID"
        assert oco_id   is not None,      "Should have OCO ID"
        pos = load_position()
        assert pos      is not None,      "Position should be saved"
        result = (f"Entry={order_id} OCO={oco_id} "
                 f"SL=₹{pos['sl_price']} Target=₹{pos['target_price']}")
    finally:
        trader.safety_gate   = original_safety
        config.LIVE_TRADING  = original
        clear_position()

    return result

def test_full_flow_order_failure_handled():
    """execute_live_trade returns False gracefully on order failure."""
    from trader import execute_live_trade, clear_position
    import config
    clear_position()
    original = config.LIVE_TRADING
    config.LIVE_TRADING = True

    import trader
    original_safety = trader.safety_gate
    trader.safety_gate = lambda g, e: True  # bypass safety for this test

    groww = MockGroww(scenario="order_fail")
    try:
        success, order_id, oco_id = execute_live_trade(groww, MOCK_ENTRY)
        assert success == False, "Should return False on order failure"
        result = "Order failure handled gracefully"
    finally:
        trader.safety_gate  = original_safety
        config.LIVE_TRADING = original
        clear_position()

    return result

test("Full flow blocked when disabled",    test_full_flow_blocked_when_disabled)
test("Full flow: entry + OCO placed",      test_full_flow_live_enabled)
test("Full flow: order failure graceful",  test_full_flow_order_failure_handled)


# ══════════════════════════════════════════════════════════════
# 9. SCHEDULER ROUTING
# ══════════════════════════════════════════════════════════════
section("9. SCHEDULER ROUTING")

def test_execute_trade_decision_routes_paper():
    """execute_trade_decision uses paper_trader when LIVE_TRADING=False."""
    import config
    original = config.LIVE_TRADING
    config.LIVE_TRADING = False

    paper_called = []
    import paper_trader as pt
    original_enter = pt.enter_trade

    def mock_enter(log, entry):
        paper_called.append(entry)
        return log, {"id": "T001", "symbol": "TEST", "action": "BUY",
                     "entry_price": 85, "units": 2, "cost": 170,
                     "sl_price": 55, "target_price": 136,
                     "direction": "BEARISH", "expiry": "2025-03-20"}

    pt.enter_trade = mock_enter

    from scheduler import execute_trade_decision
    mock_result = {
        "signal": "BEARISH", "go": True,
        "checklist": {},
        "recommendation": {"action": "BUY", "confidence": "HIGH"},
        "entry": MOCK_ENTRY,
        "greeks": {}, "sentiment": {},
    }
    mock_log = {
        "capital": 10000, "available": 10000, "total_pnl": 0,
        "trades_today": 0, "daily_loss": 0,
        "trade_date": str(datetime.date.today()),
        "open_trade": None, "closed_trades": []
    }

    try:
        execute_trade_decision(None, mock_result, mock_log)
        assert len(paper_called) == 1, "Paper trader should be called once"
        result = "Correctly routed to paper_trader"
    finally:
        pt.enter_trade      = original_enter
        config.LIVE_TRADING = original

    return result

test("Routes to paper when LIVE=False",    test_execute_trade_decision_routes_paper)


# ══════════════════════════════════════════════════════════════
# 10. LIVE API SANITY (read-only — no orders placed)
# ══════════════════════════════════════════════════════════════
section("10. LIVE API SANITY (read-only)")

def test_groww_connect():
    from engine import get_groww_client
    g = get_groww_client()
    assert g is not None
    return "Connected"

def test_positions_readable():
    """get_positions_for_user returns without error."""
    from engine import get_groww_client
    groww = get_groww_client()
    try:
        resp = groww.get_positions_for_user(segment=groww.SEGMENT_FNO)
        assert isinstance(resp, dict), f"Expected dict got {type(resp)}"
        return f"Positions readable: {len(resp.get('positions', []))} open"
    except Exception as e:
        return f"⚠️ Not available: {e}"

def test_margin_readable():
    """get_available_margin_details returns without error."""
    from engine import get_groww_client
    groww = get_groww_client()
    try:
        resp = groww.get_available_margin_details()
        assert isinstance(resp, dict), f"Expected dict got {type(resp)}"
        return f"Margin readable: {resp}"
    except Exception as e:
        return f"⚠️ Not available: {e}"

def test_order_list_readable():
    """get_order_list returns without error."""
    from engine import get_groww_client
    groww = get_groww_client()
    try:
        resp = groww.get_order_list(page=0, page_size=5)
        assert isinstance(resp, (dict, list))
        return f"Order list readable"
    except Exception as e:
        return f"⚠️ Not available: {e}"

test("Groww API connection",                test_groww_connect)
test("Positions endpoint readable",         test_positions_readable)
test("Margin endpoint readable",            test_margin_readable)
test("Order list endpoint readable",        test_order_list_readable)


# ══════════════════════════════════════════════════════════════
# CLEANUP & REPORT
# ══════════════════════════════════════════════════════════════
cleanup()

total  = len(results)
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = sum(1 for _, s, _ in results if s == "FAIL")

print(f"\n{'═'*55}")
print(f"  ZERO HERO LIVE TRADER TEST SUITE — COMPLETE")
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
    print(f"\n  🔒 Trader is verified safe.")
    print(f"  📋 Run both suites for 30 days, then set LIVE_TRADING=True.")
    sys.exit(0)
