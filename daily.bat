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
REM --- BT threshold = 30 (2026-08-08: lowered from 40). 30 equals the budget-sim
REM     candidate pool floor (_BUD_MIN_BT is hard-clamped to >=30), so this is
REM     effectively NO BT filter. One env drives every "BTxx-and-above" place:
REM     the detail filter tabs AND the 400man budget floor (max(_BUD_MIN_BT, this)).
REM     Why: BT has no discriminative power (CLAUDE.md 18.12) and the rolling-OOS
REM     tier sweep is non-monotonic with BT30 best (18.24). BT40 also HALVES the
REM     measurable signal: 9 months to t=2 at BT30 vs 29 months at BT40.
REM     Set to 40 or 50 to revert.
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
REM     --bt-min defaults to 30 = the SAME floor the budget tab uses. Live used to
REM     default to 0, so the report scored a strategy we did not actually trade
REM     (found 2026-08-12). Pass --bt-min 0 to disable.
if not defined LSS_H_LIMIT_TICKS set "LSS_H_LIMIT_TICKS=-5"
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
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
goto :eof

:help
python daily_help.py daily
goto :eof
