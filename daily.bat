@echo off
REM ============================================================
REM daily.bat - morning report: long + short + lss, order server
REM   Usage (swingtrade folder):  .\daily   (PowerShell)  /  daily  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Directions: long + short + lss (mirror skipped). lss is the default tab.
REM     - long / short: WF-holdout selection fixed to 2026-06 as-of via --long-base
REM       (--long-base now applies to BOTH long and short so they share the base).
REM     - lss: CUMULATIVE proposal = union of ALL bases 2025-09 .. 2026-06.
REM       Rebuilt every run so it is always current. OOS-verified: the cumulative merge
REM       is net positive on pure OOS and, being the largest pool, lets the 400man
REM       BT-descending tab always pick the highest-BT orders each day (breadth is this
REM       strategy's edge; more names -> more captured edge). To refresh, add the newest
REM       base to the merge line below (keep the older ones = cumulative).
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
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM --- delay1 ON by default: engine skips the stop on the entry 5min bar and arms it
REM     from the next grid (matches watch.bat --stop-delay-bars 1). Report BT/ranking
REM     reflect delay1. NOTE: the P&L tab under delay1 is OPTIMISTIC (engine fills the
REM     stop at the line); the realistic verdict is compare_lss_rules.py net-real.
REM     To disable for one run: set LSS_STOP_DELAY_BARS=0 before calling, or edit here.
set "LSS_STOP_DELAY_BARS=1"
REM --- budget tab (400man x BT-descending) invests down to BT40 (BT30-39 is marginal,
REM     BT<30 is the losing band). Detail "BT50+" tab is unaffected. Set to 50 to revert. ---
set "LSS_BUDGET_FLOOR_BT=40"
REM --- rebuild the CUMULATIVE lss proposal (union of ALL bases 2025-09 .. 2026-06) ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py lss_proposal_2025-12.py lss_proposal_2026-01.py lss_proposal_2026-02.py lss_proposal_2026-03.py lss_proposal_2026-04.py lss_proposal_2026-05.py lss_proposal_2026-06.py --out lss_proposal_cumul.py
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
