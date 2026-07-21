@echo off
REM ============================================================
REM daily.bat - morning lss order report (June base, lss only, fast, order server)
REM   Usage (swingtrade folder):  .\daily   (PowerShell)  /  daily  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Flags (minimum needed to place orders, fast enough for 9:00):
REM     --no-long     : drop long direction (biggest speedup). Signal list uses lss BT
REM                     (rec_score); long BT ref falls back to rec_score automatically.
REM     --no-short    : short not needed for ordering.
REM     --no-analysis : skip heavy 5min TP/SL sweep tabs (signal list + budget tab stay).
REM     --price-ranges 6000,0 : both 1000-6000 and unlimited tabs (2 passes).
REM     (no --no-serve): start order server after the report (order/watch toggle).
REM   Clears LSS_CLOSESTOP_RESWEEP / LSS_GUARD_ONLY on start (if left set they force the
REM     heavy close-stop compare = hundreds of seconds every run).
REM   Base month = 2026-06. Guard = 3% (verified best for recent base).
REM
REM   Pass-through options at the end:
REM     .\daily --price-ranges 6000     (6000 only = faster, single pass)
REM     .\daily --price-ranges 0        (unlimited only)
REM     .\daily --short                 (also show short daily grid)
REM   Notes:
REM     - First run of a new trading day rebuilds the lss BT cache (slower once).
REM     - Run once by ~8:45 so the order list is ready before 9:00.
REM ============================================================
cd /d "%~dp0"
REM --- clear heavy research flags so daily always runs light ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --no-short --no-long --default-tab lss --force --no-news --no-risk --workers 8 %*
