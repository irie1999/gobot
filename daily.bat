@echo off
REM ============================================================
REM daily.bat - morning report: long + short + lss, all 2026-06 base, order server
REM   Usage (swingtrade folder):  .\daily   (PowerShell)  /  daily  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Directions: long + short + lss (mirror skipped). lss is the default tab.
REM     - long / short: WF-holdout selection fixed to 2026-06 as-of via --long-base
REM       (--long-base now applies to BOTH long and short so they share the base).
REM     - lss: --lss-proposal lss_proposal_2026-06.py.
REM   --no-analysis : skip heavy 5min TP/SL sweep tabs (signal list + budget tab stay).
REM   --price-ranges 6000,0 : both 1000-6000 and unlimited tabs.
REM   (no --no-serve): start order server after the report (order/watch toggle).
REM   Clears LSS_CLOSESTOP_RESWEEP / LSS_GUARD_ONLY on start (if left set they force the
REM     heavy close-stop compare = hundreds of seconds every run).
REM   Guard = 3% (verified best for recent base).
REM
REM   Pass-through options at the end:
REM     .\daily --no-long --no-short    (lss only = fastest, for quick ordering)
REM     .\daily --price-ranges 6000     (6000 only = faster)
REM   Notes:
REM     - Long/short add time; use --no-long --no-short when you only need the order list.
REM     - First run of a new trading day rebuilds the BT cache (slower once).
REM     - Run once by ~8:45 so the order list is ready before 9:00.
REM ============================================================
cd /d "%~dp0"
REM --- clear heavy research flags so daily always runs light ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
