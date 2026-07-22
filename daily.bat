@echo off
REM ============================================================
REM daily.bat - morning report: long + short + lss, order server
REM   Usage (swingtrade folder):  .\daily   (PowerShell)  /  daily  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Directions: long + short + lss (mirror skipped). lss is the default tab.
REM     - long / short: WF-holdout selection fixed to 2026-06 as-of via --long-base
REM       (--long-base now applies to BOTH long and short so they share the base).
REM     - lss: MERGED proposal = union of 2025-12 + 2026-03 + 2026-06 (fresh 3 bases).
REM       Rebuilt every run so it is always current. OOS-verified: the merge beats any
REM       single base in the 400man BT-descending tab (bigger pool -> BT-descending
REM       picks higher-BT orders). To refresh the base set, edit the merge line below
REM       (drop the oldest, add the newest). Do NOT add pre-Dec bases (stale = weak).
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
REM --- rebuild the merged lss proposal (union of the 3 fresh bases) ---
python merge_lss_proposals.py lss_proposal_2025-12.py lss_proposal_2026-03.py lss_proposal_2026-06.py --out lss_proposal_merged3.py
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_merged3.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
