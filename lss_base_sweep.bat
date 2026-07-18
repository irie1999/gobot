@echo off
REM ============================================================
REM lss_base_sweep.bat - run the daily-equivalent report for 4 selection base months.
REM   For each base month it (1) re-selects lss as-of that month, (2) builds a
REM   .\daily-equivalent report tagged with _base<YYYY-MM> so the 4 do not overwrite.
REM   HEAVY: each base month runs a full lss universe scan. Expect a long run.
REM   Usage (swingtrade folder):  .\lss_base_sweep.bat
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"

call :one 2024-12 2024-12-31
call :one 2025-06 2025-06-30
call :one 2025-12 2025-12-31

echo.
echo ============================================================
echo DONE. 4 reports generated (open each in a browser):
echo   signals_holdout_all_both_*_base2024-12.html
echo   signals_holdout_all_both_*_base2025-06.html
echo   signals_holdout_all_both_*_base2025-12.html
echo ============================================================
goto :eof

REM ---- :one <base-month YYYY-MM> <long-base YYYY-MM-DD> ----
:one
set "BM=%~1"
set "LB=%~2"
echo.
echo ==================== base-month %BM% : lss selection ====================
python scan_lss_universe.py --base-month %BM% --out lss_proposal_%BM%.py --max-price 6000 --source local
echo ==================== base-month %BM% : report ====================
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal lss_proposal_%BM%.py --long-base %LB% --days-from-base --default-tab lss --no-mirror --no-short --no-long --force --no-news --no-serve --output-suffix _base%BM%
goto :eof
