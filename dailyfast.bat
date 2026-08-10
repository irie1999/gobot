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
REM --- delay1 ON: matches watch.bat --stop-delay-bars 1 (see CLAUDE.md 18.9) ---
set "LSS_STOP_DELAY_BARS=1"
REM --- BT threshold for the "BTxx-and-above" tabs and the budget floor.
REM     30 = the pool floor = effectively no BT filter (2026-08-08, CLAUDE.md 18.24).
REM     Keep this in sync with daily.bat.
set "LSS_BT_TAB_MIN=30"
REM --- H (limit sell at prev close -5 ticks) is the entry method under evaluation.
REM     Keep the report's H tab on the SAME setting the live orders use, or the
REM     screen you read every morning shows a different H than what you send
REM     (CLAUDE.md 18.9: backtest and live must always match).
REM     -5 ticks won on 10-month OOS AND was picked by walk-forward every month
REM     from Nov onward (99.5% of the fixed best), so the choice is not a
REM     10-month hindsight pick. Zara-ba fills are kept (LSS_H_AUCTION_ONLY unset)
REM     because walk-forward kept choosing them: +94,324 over 10 months.
REM     Live order: python lss_budget_cap.py --entry-mode limit --limit-ticks -5
REM                 --budget-multiple 1.0   (1.0 because H fills at the open all at
REM                 once, so over-subscribe cannot be cancelled in time)
if not defined LSS_H_LIMIT_TICKS set "LSS_H_LIMIT_TICKS=-5"
REM --- as-of BT ON: score every PAST trade with the BT it had AT SIGNAL TIME.
REM     Without this, 93.7% of the trades in the PnL tab were scored with TODAY's BT.
REM     BT is built from the last 365 days, and the PnL tab shows the last 180 days,
REM     so a stock that made money since February got a high BT and the budget tab's
REM     BT-descending order picked it knowing the outcome (lookahead). See CLAUDE.md.
REM     Today's SIGNAL list is unaffected - only how past results are scored.
REM     To compare for one run: set LSS_ASOF_BT=0 before calling.
if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
REM --- dump every settled trade so .\fills can reconcile real fills against the test.
REM     Without it .\fills skips its section 3 and the daily divergence never accumulates.
if not defined LSS_TRADES_CSV set "LSS_TRADES_CSV=lss_trades.csv"
REM --- rebuild the CUMULATIVE lss proposal (union of ALL bases) ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py lss_proposal_2025-12.py lss_proposal_2026-01.py lss_proposal_2026-02.py lss_proposal_2026-03.py lss_proposal_2026-04.py lss_proposal_2026-05.py lss_proposal_2026-06.py lss_proposal_2026-07.py --out lss_proposal_cumul.py
python run_signals_holdout_all.py --both --no-long --no-short --no-symbol-detail --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
