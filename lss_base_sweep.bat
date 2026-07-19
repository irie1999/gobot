@echo off
REM ============================================================
REM lss_base_sweep.bat - RE-SCAN + report for each selection base month.
REM   For each base month it (1) re-selects lss as-of that month with the CURRENT
REM   scan_lss_universe (=latest fixes: realistic entry min(trigger,open)+-3% guard),
REM   (2) builds a .\daily-equivalent report tagged _base<YYYY-MM>.
REM   HEAVY: each base month runs a full lss universe scan. Expect a long run.
REM   -> This produces the "latest-fix version" of every base-month proposal+report.
REM   Usage (swingtrade folder):  .\lss_base_sweep.bat
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"

call :one 2024-12 2024-12-31
call :one 2025-06 2025-06-30
call :one 2025-12 2025-12-31
call :one 2026-06 2026-06-30

echo.
echo ============================================================
echo DONE. 4 reports generated (open with .\open_sweep.bat):
echo   signals_holdout_all_both_*_base2024-12.html
echo   signals_holdout_all_both_*_base2025-06.html
echo   signals_holdout_all_both_*_base2025-12.html
echo   signals_holdout_all_both_*_base2026-06.html  (= .\daily uses this proposal)
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
