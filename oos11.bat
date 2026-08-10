@echo off
REM ============================================================
REM oos11.bat - single-split OOS report: merge bases up to 2025-11 only,
REM             so EVERY month from 2025-12 onward is out-of-sample.
REM   Usage:  .\oos11
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM   Why this exists:
REM     .\daily merges bases 2025-09 .. 2026-07 (cumulative). Per-symbol START_DATES
REM     already make each month OOS for the pairs first seen that month (18.29), but
REM     a single hard cut is easier to read in the HTML: with bases capped at 2025-11,
REM     nothing selected after that date can influence any month from 2025-12 on.
REM
REM   What is identical to .\daily (do NOT change these - 18.10.1):
REM     LSS_STOP_DELAY_BARS=1 / LSS_BT_TAB_MIN=30 / LSS_ASOF_BT=1
REM     price range 1000-6000 / order rank = liquidity / fee 0 / v17 split guard
REM
REM   What is different:
REM     merge      : 2025-09 + 2025-10 + 2025-11 only
REM     --long-base: 2025-11-30 (long/short panes use the same as-of; lss unaffected)
REM     --days 365 : window must cover 2025-12 -> today
REM     --no-serve : never start the order server (this is research, not ordering)
REM     LSS_TRADES_CSV redirected so the production lss_trades.csv used by .\fills
REM       is NOT overwritten (18.29).
REM     --output-suffix _oos11 so signals_holdout_all_*_oos11_*.html is a separate file
REM       and the morning report is not clobbered.
REM
REM   ORDER LIST IS MEANINGLESS HERE. Never order from this report - the pool is
REM   capped at 2025-11 and misses every stock selected since. Use .\daily for that.
REM ============================================================
cd /d "%~dp0"

REM --- clear heavy research flags (same as daily.bat) ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="

REM --- must match daily.bat / watch.bat exactly ---
set "LSS_STOP_DELAY_BARS=1"
set "LSS_BT_TAB_MIN=30"
set "LSS_ASOF_BT=1"

REM --- keep the production trade log intact (.\fills reads lss_trades.csv) ---
set "LSS_TRADES_CSV=lss_trades_oos11.csv"

REM --- merge ONLY the bases up to 2025-11 ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py --out lss_proposal_cumul_to202511.py
if errorlevel 1 (
  echo [error] merge failed. Check that lss_proposal_2025-09/10/11.py exist.
  exit /b 1
)

REM --- how many pairs did we keep vs the full cumulative pool? ---
if exist lss_proposal_cumul.py python diff_proposals.py lss_proposal_cumul.py lss_proposal_cumul_to202511.py

python run_signals_holdout_all.py --both --no-long --no-short --no-symbol-detail --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_cumul_to202511.py --long-base 2025-11-30 --no-mirror --default-tab lss --force --no-news --no-risk --no-serve --days 365 --output-suffix _oos11 --workers 8 %*
