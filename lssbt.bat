@echo off
REM ============================================================
REM lssbt.bat - lss (long-candidate short) ONLY, minimal compute, for one base month.
REM   Skips long / short / mirror and the heavy 5min sweep. Just the lss signal +
REM   PnL tabs. Use to compare base months quickly.
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Usage:  .\lssbt <base-month>
REM     .\lssbt 2025-12     -> lss_proposal_2025-12.py, unlimited budget tab
REM     .\lssbt 2026-03     -> lss_proposal_2026-03.py
REM   Output: signals_holdout_all_both_<today>_base<base-month>.html
REM
REM   Notes:
REM     - Reads lss_proposal_<base-month>.py (must exist locally).
REM     - --long-base is NOT needed (long/short are skipped; lss uses the proposal).
REM     - price-ranges 0 = unlimited only (400man budget). Add 6000,0 for both tabs.
REM     - Extra options pass through, e.g. .\lssbt 2025-12 --no-browser
REM ============================================================
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: .\lssbt ^<base-month^>   e.g.  .\lssbt 2025-12
  exit /b 1
)
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
python run_signals_holdout_all.py --both --no-long --no-short --no-mirror --lss-proposal lss_proposal_%~1.py --min-price 1000 --price-ranges 0 --no-analysis --default-tab lss --force --no-news --no-risk --workers 8 --output-suffix _base%~1 %2 %3 %4 %5 %6
