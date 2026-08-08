@echo off
REM ============================================================
REM sweep_oos.bat - lss OOS cumulative-fold sweep
REM   Usage: .\sweep_oos
REM   Runs each fold with the same arguments as daily.bat and writes
REM   oos_sweep_YYYY-MM-DD.csv and oos_raw_YYYY-MM-DD.csv.
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM ============================================================
cd /d "%~dp0"
REM --- same environment as daily.bat ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM Keep these identical to daily.bat, or the sweep measures a different strategy.
REM   delay1 = live watcher --stop-delay-bars 1 (CLAUDE.md 18.9)
REM   BT 30  = the pool floor = effectively no BT filter (CLAUDE.md 18.24)
set "LSS_STOP_DELAY_BARS=1"
set "LSS_BT_TAB_MIN=30"
REM --- run the OOS sweep ---
python sweep_lss_oos_monthly.py --workers 8 %*
