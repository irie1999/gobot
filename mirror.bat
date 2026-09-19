@echo off
REM ============================================================
REM mirror.bat - LONG-MIRROR short (limit sell on a rise = fade) for comparison
REM   Usage: .\mirror
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM   lss    = stop-sell BELOW the previous close  (break-down continuation)
REM   mirror = limit-sell AT the previous close on a RISE (fade / mean reversion)
REM   Same signals, same candidate pool, opposite trigger direction.
REM
REM   Env is copied from dailyfast.bat so the two are comparable. The only
REM   differences: --mirror instead of the lss pane, and a separate trades CSV
REM   so the production lss_trades.csv is never overwritten.
REM
REM   The old rejection ("daily +1.91M -> 5min -2.40M") predates v16/v17,
REM   delay1, BT30 and liquidity ordering, and was never recorded in CLAUDE.md.
REM ============================================================
cd /d "%~dp0"
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
set "LSS_STOP_DELAY_BARS=1"
set "LSS_BT_TAB_MIN=30"
if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
REM --- separate CSV: never clobber the production lss_trades.csv ---
set "LSS_TRADES_CSV=mirror_trades.csv"
REM --- same cumulative proposal as daily.bat so the candidate pool matches ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py lss_proposal_2025-12.py lss_proposal_2026-01.py lss_proposal_2026-02.py lss_proposal_2026-03.py lss_proposal_2026-04.py lss_proposal_2026-05.py lss_proposal_2026-06.py lss_proposal_2026-07.py --out lss_proposal_cumul.py
python run_signals_holdout_all.py --mirror --no-symbol-detail --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_cumul.py --force --no-news --no-risk --no-browser --no-serve --workers 8 %*
