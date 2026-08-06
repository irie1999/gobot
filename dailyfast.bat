@echo off
REM ============================================================
REM dailyfast.bat - the fast form of daily.bat (same order list, far less work)
REM   Usage (swingtrade folder):  .\dailyfast
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Same lss selection / same BT ranking / same order list as .\daily.
REM   What it drops (none of it changes which orders you place):
REM     --no-symbol-detail  the per-symbol P&L tab. That tab rebuilds the WHOLE P&L
REM                         once PER SIGNAL SYMBOL (69x on a busy day) and is by far
REM                         the biggest cost in a run, in both time and memory.
REM     --no-long --no-short  the long / short panes. lss is the tab you order from.
REM     --price-ranges 6000   one pane (1,000-6,000) instead of two.
REM
REM   Everything that matters for ordering stays: signal tab, P&L tab, budget tabs,
REM   and the tenkan tab (now auto-built from unfilled signals, so it includes today).
REM
REM   When you DO want the full report, run .\daily instead.
REM   Middle ground: .\daily --symbol-detail-limit 20   (keeps the tab, top-20 by BT)
REM ============================================================
cd /d "%~dp0"
REM --- clear heavy research flags so the run stays light ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM --- delay2 ON: MUST match watch.bat --stop-delay-bars 2 (see CLAUDE.md 18.9) ---
REM     Honor an externally set value so you can A/B without editing this file:
REM       $env:LSS_STOP_DELAY_BARS="1"; .\dailyfast   (PowerShell)
if not defined LSS_STOP_DELAY_BARS set "LSS_STOP_DELAY_BARS=2"
REM --- BT threshold for the "BTxx-and-above" tabs and the budget floor ---
set "LSS_BT_TAB_MIN=40"
REM --- rebuild the CUMULATIVE lss proposal (union of ALL bases) ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py lss_proposal_2025-12.py lss_proposal_2026-01.py lss_proposal_2026-02.py lss_proposal_2026-03.py lss_proposal_2026-04.py lss_proposal_2026-05.py lss_proposal_2026-06.py lss_proposal_2026-07.py --out lss_proposal_cumul.py
python run_signals_holdout_all.py --both --no-long --no-short --no-symbol-detail --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
