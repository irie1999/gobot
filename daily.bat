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
REM   Pass-through options at the end (fastest first):
REM     .\daily --no-long --no-short --no-symbol-detail --price-ranges 6000
REM                                     (lss only, 1 pane, no per-symbol tab = FASTEST)
REM     .\daily --no-symbol-detail      (skip the per-symbol P&L tab: it re-runs the whole
REM                                      P&L build once PER SIGNAL SYMBOL - 69x on a busy
REM                                      day. This is by far the biggest cost in a run.)
REM     .\daily --symbol-detail-limit 20  (keep the tab, top-20 by BT only)
REM     .\daily --no-long --no-short    (lss only, for quick ordering)
REM     .\daily --price-ranges 6000     (6000 only = one pane instead of two)
REM   Notes:
REM     - The per-symbol detail tab dominates runtime AND memory. Drop it first when slow.
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
REM --- delay2 ON by default: the engine skips the stop on the entry 5min bar AND the
REM     next one, arming it from the grid after that. MUST match watch.bat
REM     (--stop-delay-bars 2) or the report shows stop-outs the live run avoids.
REM     Report BT/ranking reflect delay2. NOTE: the P&L tab is OPTIMISTIC here (the
REM     engine fills the stop at the line); the realistic verdict is
REM     compare_lss_rules.py net-real (240d BT40+: delay2 +2,436,051 = peak).
REM     Changing this invalidates the BT cache (version token sd<N>) -> first run slow.
REM     To disable for one run: set LSS_STOP_DELAY_BARS=0 before calling, or edit here.
REM     Honor an externally set value so you can A/B without editing this file:
REM       $env:LSS_STOP_DELAY_BARS="1"; .\dailyfast   (PowerShell)
if not defined LSS_STOP_DELAY_BARS set "LSS_STOP_DELAY_BARS=2"
REM --- BT threshold = 40 for ALL "BTxx-and-above" places: the detail filter tabs
REM     (BT40+ list / BT40+ x entry-day) AND the 400man x BT-descending budget floor
REM     (max(_BUD_MIN_BT=30, _BT_TAB_MIN)=40). One env drives them all. Set to 50 to revert.
REM     (Note: after the #9 revert the old LSS_BUDGET_FLOOR_BT env is no longer read; use this.)
set "LSS_BT_TAB_MIN=40"
REM --- rebuild the CUMULATIVE lss proposal (union of ALL bases 2025-09 .. 2026-06) ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py lss_proposal_2025-12.py lss_proposal_2026-01.py lss_proposal_2026-02.py lss_proposal_2026-03.py lss_proposal_2026-04.py lss_proposal_2026-05.py lss_proposal_2026-06.py lss_proposal_2026-07.py --out lss_proposal_cumul.py
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
