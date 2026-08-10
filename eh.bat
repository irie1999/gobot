@echo off
REM ============================================================
REM eh.bat - inject the "E/H compare" tab into the newest daily report
REM   Usage:  .\dailyfast --no-serve      (generate the report first)
REM           .\eh                        (then inject)
REM
REM   NOTE: .\daily / .\dailyfast now inject the tab automatically (LSS_EH_TAB=0
REM         disables it). This bat is for re-injecting with different options,
REM         e.g. .\eh --exclude-months 2026-03
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM   Why a separate step:
REM     .\dailyfast REGENERATES signals_holdout_all_both_*.html, so the injected
REM     tab is lost on every run. Re-run .\eh after each .\dailyfast.
REM     Keeping it out of dailyfast also keeps the morning run fast (E/H needs
REM     5-min bars for every signal day) and never touches the order list.
REM
REM   Input: oos_raw_fold*.csv (made by run_oos_folds.py). NOT regenerated daily.
REM   The original html is saved as <name>.html.bak. Re-running replaces the tab.
REM
REM   Pass-through: .\eh --exclude-months 2026-03   /   .\eh --html eh_report.html
REM ============================================================
cd /d "%~dp0"

REM Prefer the report's own trade log: same window as the P&L tab, current month
REM included. Fall back to the rolling-OOS raw data (no current month).
set "EHSRC=lss_trades.csv"
if not exist "lss_trades.csv" set "EHSRC=oos_raw_fold*.csv"
if not exist "lss_trades.csv" if not exist "oos_raw_fold01_*.csv" (
  echo [error] neither lss_trades.csv nor oos_raw_fold*.csv found.
  echo         Run .\dailyfast first, or: python run_oos_folds.py --workers 8 --lss-only
  exit /b 1
)
echo [source] %EHSRC%

set "TARGET="
REM The tab lives in the lss pane file. signals_holdout_all_both_*.html is only an
REM iframe wrapper and has no ho-outer-nav, so injecting there fails.
for /f "delims=" %%f in ('dir /b /o-d signals_holdout_all_lss_*.html 2^>nul') do (
  set "TARGET=%%f"
  goto :found
)
:found
if not defined TARGET (
  echo [error] signals_holdout_all_lss_*.html not found.
  echo         Run .\dailyfast --no-serve first.
  exit /b 1
)
echo [target] %TARGET%
python analyze_overnight_lss.py --raw "%EHSRC%" --workers 8 --require-open-bar --inject-html "%TARGET%" %*
