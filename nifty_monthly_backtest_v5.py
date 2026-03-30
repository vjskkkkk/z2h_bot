"""
nifty_monthly_backtest_v5.py
=============================
Monthly NIFTY backtest with all v5 Natenberg improvements.
Change BACKTEST_YEAR / BACKTEST_MONTH to run any month.
"""

import datetime, sys, calendar
sys.path.insert(0, "/home/claude")  # find backtest_core

from auth import get_access_token
from backtest_core import run_day, print_summary

# ── Change these ──────────────────────────────────────────────
BACKTEST_YEAR  = 2026
BACKTEST_MONTH = 2      # February

UNDERLYING  = "NIFTY"
EXCHANGE    = "NSE"
EXPIRY_WDAY = 1   # Tuesday (changed from Thursday, effective Sep 2025)

def get_trading_days(year, month):
    _, n_days = calendar.monthrange(year, month)
    return [datetime.date(year, month, d)
            for d in range(1, n_days + 1)
            if datetime.date(year, month, d).weekday() < 5
            and datetime.date(year, month, d).weekday() != EXPIRY_WDAY]

def main():
    from config import MIN_DAYS_TO_EXPIRY, OI_VOL_DOMINANCE_RATIO, SIGNAL_CONFIRM_BARS, STOP_LOSS_PCT
    month_name = datetime.date(BACKTEST_YEAR, BACKTEST_MONTH, 1).strftime("%B %Y")

    print("\n" + "═"*76)
    print(f"  ZERO HERO v5 — NIFTY MONTHLY BACKTEST  |  {month_name}")
    print(f"  Gates: DTE≥{MIN_DAYS_TO_EXPIRY} | Vol ratio {OI_VOL_DOMINANCE_RATIO}x | "
          f"{SIGNAL_CONFIRM_BARS}-bar confirm | SL {STOP_LOSS_PCT*100:.0f}% | Thesis SL")
    print(f"  Expiry day excluded: Tuesday (post Sep-2025 NSE change)")
    print("═"*76)

    token = get_access_token()
    days  = get_trading_days(BACKTEST_YEAR, BACKTEST_MONTH)

    print(f"\n  Trading days (ex-Thu) : {len(days)}")
    print(f"  Estimated time        : ~{len(days)//2} minutes\n")

    results   = []
    prev_week = None

    for i, d in enumerate(days):
        wk = d.isocalendar()[1]
        if wk != prev_week:
            if prev_week is not None:
                print()
            print(f"  Week of {d.strftime('%d %b')}:")
            prev_week = wk

        print(f"    [{i+1:02d}/{len(days)}] {d.strftime('%a %d %b')} ... ",
              end="", flush=True)

        r = run_day(token, EXCHANGE, UNDERLYING, d)
        r["date"] = d
        results.append(r)

        if r.get("went_live"):
            emoji = "✅" if r["pnl"] > 0 else "❌"
            print(f"{r['signal_time']} {r['direction'][:4]:4}  "
                  f"DTE:{r.get('dte','?')}  "
                  f"entry ₹{r['entry']:,.0f}  exit ₹{r['exit_price']:,.0f}  "
                  f"{emoji} ₹{r['pnl']:+,.0f}")
        else:
            print(f"— {r.get('reason', '–')}")

    print_summary(results, UNDERLYING, month_name, days)

if __name__ == "__main__":
    main()
