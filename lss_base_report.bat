@echo off
REM ============================================================
REM lss_base_report.bat - REGENERATE the 4 base-month reports ONLY
REM   (reuse the existing lss_proposal_*.py - NO heavy re-scan).
REM   Use this when you only changed report options (e.g. --days-from-base).
REM   Requires lss_proposal_2024-06.py ... _2025-12.py (from lss_base_sweep.bat).
REM   Usage (swingtrade folder):  .\lss_base_report.bat
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"

call :one 2024-06 2024-06-30
call :one 2024-12 2024-12-31
call :one 2025-06 2025-06-30
call :one 2025-12 2025-12-31

echo.
echo ============================================================
echo DONE. 4 reports regenerated (report only, no re-scan).
echo ============================================================
goto :eof

REM ---- :one <base-month YYYY-MM> <long-base YYYY-MM-DD> ----
:one
set "BM=%~1"
set "LB=%~2"
if not exist "lss_proposal_%BM%.py" (
  echo   [skip] lss_proposal_%BM%.py not found - run lss_base_sweep.bat first for %BM%.
  goto :eof
)
echo ==================== base-month %BM% : report only ====================
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal lss_proposal_%BM%.py --long-base %LB% --days-from-base --default-tab lss --no-mirror --no-short --no-long --force --no-news --no-serve --output-suffix _base%BM%
goto :eof
