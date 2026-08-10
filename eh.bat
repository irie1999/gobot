@echo off
REM ============================================================
REM eh.bat - inject the "E/H compare" tab into the newest daily report
REM   Usage:  .\dailyfast --no-serve      (generate the report first)
REM           .\eh                        (then inject)
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

if not exist "oos_raw_fold01_*.csv" (
  echo [error] oos_raw_fold*.csv not found.
  echo         Run: python run_oos_folds.py --workers 8 --lss-only
  exit /b 1
)

set "TARGET="
for /f "delims=" %%f in ('dir /b /o-d signals_holdout_all_both_*.html 2^>nul') do (
  set "TARGET=%%f"
  goto :found
)
:found
if not defined TARGET (
  echo [error] signals_holdout_all_both_*.html not found.
  echo         Run .\dailyfast --no-serve first.
  exit /b 1
)
echo [target] %TARGET%
python analyze_overnight_lss.py --raw "oos_raw_fold*.csv" --workers 8 --require-open-bar --inject-html "%TARGET%" %*
