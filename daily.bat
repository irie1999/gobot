@echo off
REM ============================================================
REM daily.bat - morning report: long + short + lss + H, order server
REM   Usage (swingtrade folder):  .\daily   (PowerShell)  /  daily  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Directions: long + short + lss + H (mirror skipped). lss is the default tab
REM   (the one you have always read; all comparison blocks live there). Orders are
REM   placed from the "H limit short" tab - stop-sell buttons are hard-blocked.
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
REM                                     (H only, 1 pane, no per-symbol tab = FASTEST)
REM     .\daily --no-symbol-detail      (skip the per-symbol P&L tab: it re-runs the whole
REM                                      P&L build once PER SIGNAL SYMBOL - 69x on a busy
REM                                      day. This is by far the biggest cost in a run.)
REM     .\daily --symbol-detail-limit 20  (keep the tab, top-20 by BT only)
REM     .\daily --no-long --no-short    (H only, for quick ordering)
REM     .\daily --price-ranges 6000     (6000 only = one pane instead of two)
REM   Notes:
REM     - The per-symbol detail tab dominates runtime AND memory. Drop it first when slow.
REM     - Long/short add time; use --no-long --no-short when you only need the order list.
REM     - First run of a new trading day rebuilds the BT cache (slower once).
REM     - Run once by ~8:45 so the order list is ready before 9:00.
REM ============================================================
cd /d "%~dp0"
REM --- help: -h / --help / /?  The Japanese text lives in daily_help.py, because a
REM     .bat is read in CP932 and multibyte comments break cmd parsing (18.10.1). ---
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)

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
REM --- BT filter: OFF (2026-08-12, user decision).
REM     BT was measured 8 times and showed zero discriminative power every time:
REM     win rate flat across all tiers, non-monotonic totals, BT-descending order
REM     inside the random band, and walk-forward threshold selection reaching only
REM     24.9% of the fixed best (1/10 winning months). The one time it looked strong
REM     it was lookahead (see the as-of BT fix).
REM     Turning it off also removes a real inconsistency: the signal tab (= what you
REM     actually order) never filtered by BT, while the P&L/budget tabs floored at 30.
REM     The most liquid name of the day was ranked #1 to order yet was absent from
REM     the evaluation. Now: signal tab = what you order = what is evaluated.
REM     To restore the old floor for one run: set LSS_NO_BT_FILTER=0 & set LSS_BT_TAB_MIN=30
if not defined LSS_NO_BT_FILTER set "LSS_NO_BT_FILTER=1"
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
REM     No --bt-min: the live order script does not filter by BT at all, matching
REM     the report above (BT filter OFF). Signal tab = what you order = what is
REM     evaluated. Do NOT re-add --bt-min 30 from an older note.
if not defined LSS_H_LIMIT_TICKS set "LSS_H_LIMIT_TICKS=-5"
REM --- H setting sweep: OFF for the daily run (2026-08-13).
REM     The "H setting comparison" table prices TEN variants (limit position x
REM     auction-only x stop delay x sizing) off the 5-minute bars, and that is
REM     the single biggest cost of the P&L tab. CLAUDE.md 18.36 settled the
REM     settings: walk-forward lost to "change nothing" in all three blocks,
REM     so there is nothing to decide from it on a daily basis.
REM     The other three comparison blocks (current/E/H, order rank, per-strategy,
REM     pre-open market) stay ON - they are cheap and they are what you read.
REM     Re-measure roughly monthly, or whenever a rule changes:
REM       set LSS_H_VARIANT_TAB=1 & .\daily --days 365
if not defined LSS_H_VARIANT_TAB set "LSS_H_VARIANT_TAB=0"
REM --- as-of BT ON: score every PAST trade with the BT it had AT SIGNAL TIME.
REM     Without this, 93.7% of the trades in the PnL tab were scored with TODAY's BT
REM     (measured 2026-08-07: frozen 866 / today 12,849). BT is built from the last
REM     365 days and the PnL tab shows the last 180, so a stock that made money since
REM     February got a high BT and the budget tab's BT-descending order picked it
REM     knowing the outcome (lookahead). See CLAUDE.md.
REM     Today's SIGNAL list is unaffected - only how past results are scored.
REM     Costs one extra backtest per pair (window+400d), so the PnL tab is ~2x slower.
REM     To compare for one run: set LSS_ASOF_BT=0 before calling.
if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
REM --- dump every settled trade so .\fills can reconcile real fills against the test.
REM     Cheap (one CSV write). Without it .\fills skips its section 3 and the daily
REM     divergence (real vs test) never accumulates - that number decides whether the
REM     strategy is profitable after slippage. See CLAUDE.md 18.12 / A1.
if not defined LSS_TRADES_CSV set "LSS_TRADES_CSV=lss_trades.csv"
REM --- rebuild the CUMULATIVE lss proposal (union of ALL bases 2025-09 .. 2026-06) ---
python merge_lss_proposals.py lss_proposal_2025-09.py lss_proposal_2025-10.py lss_proposal_2025-11.py lss_proposal_2025-12.py lss_proposal_2026-01.py lss_proposal_2026-02.py lss_proposal_2026-03.py lss_proposal_2026-04.py lss_proposal_2026-05.py lss_proposal_2026-06.py lss_proposal_2026-07.py --out lss_proposal_cumul.py
REM ============================================================
REM   POOL (2026-08-16): run the P&L on the WIDE pool (no selection) and filter
REM   the ORDER LIST back to the selected pool. Selected is a subset of
REM   no-selection, so one run can produce both tabs:
REM     J = implemented  : selected pool  + watch 50 (what kabu can do today)
REM     K = ideal        : no selection   + watch unlimited
REM   LSS_SIGNAL_POOL keeps the signal tab (= what you actually order) on the
REM   selected pool, so the live order list is UNCHANGED by this. The run prints
REM     [order list] <file> filtered: N -> M pairs
REM   Check that line every morning; if it is missing the order list widened.
REM   COST: the wide pool reads ~653k symbol-days of 5-min bars, so a cold run
REM   takes minutes instead of ~40s. The E/H cache makes repeat runs much faster.
REM   To go back to the old behaviour for one run:
REM     .\daily --lss-proposal lss_proposal_cumul.py
REM ============================================================
if not exist "lss_proposal_full.py" (
  echo [pool] lss_proposal_full.py not found - generating it now...
  python make_full_proposal.py
)
if not defined LSS_SIGNAL_POOL set "LSS_SIGNAL_POOL=lss_proposal_cumul.py"
if not defined LSS_IMPL_PROPOSAL set "LSS_IMPL_PROPOSAL=lss_proposal_cumul.py"
python run_signals_holdout_all.py --both --h-tab --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_full.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
goto :eof

:help
python daily_help.py daily
goto :eof
