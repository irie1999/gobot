@echo off
REM ============================================================
REM  .\ldt = LDT report (Long DayTrade: stop-buy on upside break, same-day exit)
REM
REM  RULES FOR THIS FILE (learned the hard way, 2026-08-08):
REM   1. ASCII ONLY. cmd.exe reads .bat in the OEM codepage (932 on a Japanese
REM      Windows), so UTF-8 Japanese becomes mojibake, and some CP932 second
REM      bytes are 0x7C or 0x26, which cmd treats as operators. It then splits
REM      the REM line and tries to run the fragment as a command.
REM   2. No redirection or pipe characters in REM lines either. REM does NOT
REM      protect them: "REM foo (gt) bar" really creates a file named bar.
REM      So: no greater-than, no less-than, no pipe, no ampersand, no caret.
REM   Every other .bat here follows both rules. Keep it that way.
REM
REM  Why a .bat at all:
REM    Running run_signals_holdout_all.py with bare python applies NONE of the
REM    env that daily.bat sets. Actually observed on the first LDT run:
REM      LSS_ASOF_BT         default OFF, so past trades get ranked by TODAY's
REM                          BT. That is lookahead. Win rate came out at 79 pct.
REM      LSS_STOP_DELAY_BARS default 0, disagreeing with live (watch.bat is 1)
REM      LSS_BT_TAB_MIN      default 30, a different budget-sim BT floor
REM    All of them move the P and L tab a lot. This file pins the same ground
REM    as daily.bat. ALWAYS measure LDT through .\ldt, never bare python.
REM
REM  Usage:
REM    .\ldt                  180d display window, 365d backtest
REM    .\ldt --days 365       widen the display window
REM    .\ldt --no-browser
REM
REM  Needs long_watchlist_proposal_*.py (from scan_long_daytrade.py). Without it
REM  it falls back to the existing WATCHLIST, which is a weaker selection.
REM ============================================================
cd /d "%~dp0"
REM --- clear heavy research flags ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM --- match live (watch.bat). Applies to LDT too, see backtest_limit_entry.py
REM     line 1282: _sd_bars uses _LSS_STOP_DELAY_BARS when _dt_long is set.
set "LSS_STOP_DELAY_BARS=1"
REM --- same BT floor as lss so the two reports are comparable
set "LSS_BT_TAB_MIN=40"
REM --- as-of BT: without this the PnL tab ranks past trades by TODAY's BT.
REM     On lss that inflated the win rate from 41 pct to 55-66 pct. Same trap.
if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
REM --- dump settled trades to a SEPARATE csv so lss and ldt never mix
if not defined LSS_TRADES_CSV set "LSS_TRADES_CSV=ldt_trades.csv"
REM --- ALWAYS --no-serve. LDT is analysis only. Without it the script starts the
REM     LIVE order server against the PRODUCTION account (18080, long margin new).
REM     It also fights .\daily and .\watch for the single kabu token (401).
REM     Happened once on 2026-08-08. Do not remove this flag.
python run_signals_holdout_all.py --long-daytrade --ldt-proposal auto --min-price 1000 --max-price 6000 --force --days 365 --no-news --no-risk --workers 8 --no-serve %*
