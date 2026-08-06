@echo off
REM ============================================================
REM sweep_oos.bat - lss OOS累積フォールドスイープ
REM   Usage: .\sweep_oos
REM   daily.bat と同一引数で各フォールドを自動実行し、
REM   oos_sweep_YYYY-MM-DD.csv と oos_raw_YYYY-MM-DD.csv を生成。
REM ============================================================
cd /d "%~dp0"
REM --- daily.bat と同一の環境変数 ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM delay2 = live watcher --stop-delay-bars 2 (CLAUDE.md 18.9)
set "LSS_STOP_DELAY_BARS=2"
set "LSS_BT_TAB_MIN=40"
REM --- OOSスイープ実行 ---
python sweep_lss_oos_monthly.py --workers 8 %*
