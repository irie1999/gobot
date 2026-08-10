@echo off
REM ============================================================
REM ehm.bat - compare current lss / E / H month by month, all through the
REM           REPORT path (the authoritative one, decided 2026-08-10).
REM
REM   Usage:  .\ehm                 (365-day window, the default)
REM           .\ehm --days 730      (extra options pass through to dailyfast)
REM
REM   Why a .bat and not two shell lines:
REM       In PowerShell `set FOO=bar` is Set-Variable, NOT an environment
REM       variable, so the report never sees it and the CSV is silently not
REM       written (hit 2026-08-10). cmd's `set` inside a .bat is reliable.
REM       Same rule as CLAUDE.md 18.10.1: always go through a .bat.
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   What it does:
REM     1. LSS_OOS_BUDGET_CSV / _DAYS so the report dumps its own monthly P&L
REM        for the three sims, all built by the SAME _run_budget_sim
REM        (sim_type = the plain budget one, plus its _E and _H variants)
REM     2. runs dailyfast (lss pane only, no long/short, no per-symbol tab)
REM     3. prints the 3-way monthly table + paired t-tests
REM
REM   run_oos_folds is NOT needed: per-symbol START_DATES already make each
REM   month row use only the pairs selected up to the previous month, i.e. the
REM   same pool a rolling fold would use (CLAUDE.md 18.29).
REM ============================================================
cd /d "%~dp0"

set "EHM_DAYS=365"
set "LSS_OOS_BUDGET_CSV=eh_months.csv"
set "LSS_OOS_BUDGET_DAYS=%EHM_DAYS%"
if exist eh_months.csv del eh_months.csv

call "%~dp0dailyfast.bat" --no-serve --no-browser --days %EHM_DAYS% %*

set "LSS_OOS_BUDGET_CSV="
set "LSS_OOS_BUDGET_DAYS="

if not exist eh_months.csv echo [error] eh_months.csv was not written. Look for an
if not exist eh_months.csv echo         "[OOS budget CSV]" line in the log above.
if not exist eh_months.csv exit /b 1

python compare_eh_months.py --csv eh_months.csv --drop-current
