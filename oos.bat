@echo off
REM ============================================================
REM oos.bat - single-split OOS report. Merge proposal bases up to YYYY-MM only,
REM           so EVERY month after that is out-of-sample.
REM
REM   Usage:  .\oos 2026-01          (bases 2025-09..2026-01 -> OOS from 2026-02)
REM           .\oos 2025-11          (bases 2025-09..2025-11 -> OOS from 2025-12)
REM           .\oos                  (default 2025-11)
REM           .\oos 2026-01 --no-browser        (up to 8 extra options pass through)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM   Why this exists:
REM     .\daily merges ALL bases cumulatively. Per-symbol START_DATES already make
REM     each month OOS for the pairs first seen that month (18.29), but a single hard
REM     cut is far easier to read in the HTML: with bases capped at YYYY-MM, nothing
REM     selected after that date can influence any later month.
REM
REM   Output: signals_holdout_all_*_oosYYYYMM_<date>.html   (daily report untouched)
REM
REM   What is identical to .\daily (do NOT change - 18.10.1):
REM     LSS_STOP_DELAY_BARS=1 / LSS_BT_TAB_MIN=30 / LSS_ASOF_BT=1
REM     LSS_H_LIMIT_TICKS=-5 (H's limit offset)
REM     price range 1000-6000 / order rank = liquidity / fee 0 / v17 split guard
REM
REM   What is different:
REM     merge      : only bases <= the month you pass (merge_lss_proposals --until)
REM     --days 365 : window must cover the whole OOS stretch
REM     --no-serve : never start the order server (this is research, not ordering)
REM     no --long-base : long/short panes are disabled here, so it is unused
REM     LSS_TRADES_CSV redirected so the production lss_trades.csv used by .\fills
REM       is NOT overwritten (18.29).
REM
REM   ORDER LIST IS MEANINGLESS HERE. Never order from this report - the pool is
REM   frozen at the cutoff and misses every stock selected since. Use .\daily.
REM ============================================================
cd /d "%~dp0"

set "CUT=%~1"
if "%CUT%"=="" set "CUT=2025-11"
set "TAG=%CUT:-=%"
REM --- lower bound MUST match daily.bat, which merges from 2025-09 only.
REM     Without it the glob also picks up lss_proposal_2024-12 / 2025-03 / 2025-06
REM     and the pool stops being comparable to the daily report (hit 2026-08-10:
REM     3 bases vs 8 bases, +140k swing in one month that looked like a real effect).
REM     Override for one run:  set OOS_SINCE=2024-12
if not defined OOS_SINCE set "OOS_SINCE=2025-09"
echo [oos] bases %OOS_SINCE% .. %CUT%   (everything after %CUT% is out-of-sample)

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
REM --- H's limit offset must match daily.bat too, or the H tab in this OOS report
REM     evaluates a different entry method than the one under evaluation.
if not defined LSS_H_LIMIT_TICKS set "LSS_H_LIMIT_TICKS=-5"

REM --- keep the production trade log intact (.\fills reads lss_trades.csv) ---
set "LSS_TRADES_CSV=lss_trades_oos%TAG%.csv"

REM --- merge ONLY the bases up to the cutoff (auto-collected by --until) ---
python merge_lss_proposals.py --since %OOS_SINCE% --until %CUT% --out lss_proposal_cumul_to%TAG%.py
if errorlevel 1 echo [error] merge failed. Check that lss_proposal_YYYY-MM.py files exist.
if errorlevel 1 exit /b 1

REM --- how many pairs did we keep vs the full cumulative pool? ---
if exist lss_proposal_cumul.py python diff_proposals.py lss_proposal_cumul.py lss_proposal_cumul_to%TAG%.py

python run_signals_holdout_all.py --both --no-long --no-short --no-symbol-detail --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_cumul_to%TAG%.py --no-mirror --default-tab lss --force --no-news --no-risk --no-serve --days 365 --output-suffix _oos%TAG% --workers 8 %2 %3 %4 %5 %6 %7 %8 %9
