@echo off
REM ============================================================
REM pxsweep.bat - price-cap sweep INCLUDING names above 6,000 yen
REM   Usage:  .\pxsweep                (365 days)
REM           .\pxsweep --days 730
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHY A SEPARATE BAT
REM   The normal report caps the universe at 6,000 yen (--price-ranges 6000),
REM   so the in-report price sweep can only go DOWN from there. To see whether
REM   expensive names are better we need a WIDER pool, which means a different
REM   run - and a different run must never touch the live order files (18.24
REM   says do not compare across runs; 18.16 says the selected-symbols file is
REM   read by the live order path).
REM
REM   Raising --price-ranges rewrites holdout_selected_symbols.py with a
REM   10,000-yen universe. If that leaked, TOMORROW'S ORDER LIST would silently
REM   include 9,000-yen names. Every shared file is redirected below.
REM
REM WHAT TO LOOK AT (decide before running - 18.38 checklist #6)
REM   The audit board row "建値の上限". Judge on:
REM     1. mean/sigma  (NOT total yen - price has no edge, proven 4x:
REM                     18.13 / 18.24 / 18.31 / 18.38)
REM     2. the concentration table: what % of the budget one name takes
REM     3. how many names per day - fewer names means higher sigma, since
REM        94%% of the daily variance is idiosyncratic (18.30)
REM   A higher cap that keeps mean/sigma AND cuts the names-per-day is the only
REM   result that would justify a smaller watch list.
REM ============================================================
cd /d "%~dp0"

REM --- isolate every file the live J path touches -------------
set "LSS_SELECTED_OUT=holdout_selected_symbols_px.py"
set "LSS_SIGNALS_OUT="
set "LSS_TRADES_CSV=lss_trades_px.csv"

REM --- the sweep itself ---------------------------------------
REM 8000/10000 are only reachable because the pool below is wider.
set "LSS_EQ_PRICE_MAXES=2000,3000,4000,6000,8000"
set "LSS_H_VARIANT_TAB=1"

call dailyfast.bat --days 365 --no-serve --no-h-tab --price-ranges 10000 --output-suffix _px %*

REM --- put the shell back the way it was ----------------------
set "LSS_SELECTED_OUT="
set "LSS_SIGNALS_OUT="
set "LSS_TRADES_CSV="
set "LSS_EQ_PRICE_MAXES="
set "LSS_H_VARIANT_TAB="
