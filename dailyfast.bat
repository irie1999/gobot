@echo off
REM ============================================================
REM dailyfast.bat - the fast form of daily.bat (same order list, far less work)
REM   Usage (swingtrade folder):  .\dailyfast
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Same selection / same order list as .\daily.
REM   What it drops (none of it changes which orders you place):
REM     --no-symbol-detail  the per-symbol P&L tab. That tab rebuilds the WHOLE P&L
REM                         once PER SIGNAL SYMBOL (69x on a busy day) and is by far
REM                         the biggest cost in a run, in both time and memory.
REM     --no-long --no-short  the long / short panes. H is the tab you order from.
REM     --price-ranges 6000   one pane (1,000-6,000) instead of two.
REM
REM   Everything that matters for ordering stays: signal tab, P&L tab, budget tabs,
REM   and the tenkan tab (now auto-built from unfilled signals, so it includes today).
REM
REM   TWO direction tabs: "long-stock short" (the tab you have always read - all the
REM   comparison blocks live there) and "H limit short" (the method actually ordered:
REM   limit sell at prev close -5 ticks). Same signals / names / exits; only the order
REM   type differs. --default-tab lss opens on the one you are used to; click over to
REM   H to place orders. The stop-sell order buttons are hard-blocked, so a misclick
REM   on the first tab cannot send anything (LSS_H_ONLY in order_server).
REM   COST: it runs the whole pass TWICE. Add --no-lss for one run to drop the
REM   stop-sell tab and halve the time (you lose the comparison blocks with it).
REM
REM   --no-tenkan / --no-market keep it light:
REM     --no-tenkan  the long-conversion tab. CLAUDE.md 18.26b settled it as
REM                  "record only, never part of the order rules", and building it
REM                  re-simulates several hundred trades off 5-minute bars.
REM     --no-market  the regime / trend-period / entry-analysis tabs. They pull the
REM                  Nikkei and emit 200k+ characters of HTML, and none of it feeds
REM                  the H order decision.
REM   Add them back for one run if you actually need those tabs.
REM
REM   When you DO want the full report, run .\daily instead.
REM   Middle ground: .\daily --symbol-detail-limit 20   (keeps the tab, top-20 by BT)
REM ============================================================
cd /d "%~dp0"
REM --- help: -h / --help / /?  The Japanese text lives in daily_help.py, because a
REM     .bat is read in CP932 and multibyte comments break cmd parsing (18.10.1). ---
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)

REM --- clear heavy research flags so the run stays light ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM --- delay1 ON: matches watch.bat --stop-delay-bars 1 (see CLAUDE.md 18.9) ---
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
REM     CLAUDE.md 18.36 settled the settings: walk-forward lost to "change
REM     nothing" in all three blocks, so there is nothing to decide from it on
REM     a daily basis. That is the reason - NOT cost.
REM     CORRECTION (2026-08-15, measured): this used to claim the sweep was
REM     "the single biggest cost of the P&L tab". Measured, it is 1.7s = 0.5%
REM     of a 351s run. The filter scan was 303.3s (91.8%); it is off by default
REM     now (LSS_FILTER_SCAN=1 to dig again). The run prints a phase-time
REM     summary at the end - read it instead of guessing.
REM     The other three comparison blocks (current/E/H, order rank, per-strategy,
REM     pre-open market) stay ON - they are cheap and they are what you read.
REM     Re-measure roughly monthly, or whenever a rule changes:
REM       set LSS_H_VARIANT_TAB=1 & .\daily --days 365
if not defined LSS_H_VARIANT_TAB set "LSS_H_VARIANT_TAB=0"
REM --- Sizing: 100 shares for EVERY name (user decision, 2026-08-11).
REM     ATR parity was tried and rolled back. It moved yen/trade +747 -> +775
REM     (+3.7%), which is INSIDE the noise band: reshuffling the order alone moves
REM     the 10-month total by sigma=124,660 yen (CLAUDE.md 18.24). It was also 2nd
REM     place in BOTH halves, i.e. it lost to flat 100 shares in each of them.
REM     The remaining argument was variance, not profit (18.30: 94% of the daily
REM     sigma is name-specific and FIXED_QTY=100 makes the position 100k..600k yen,
REM     a 6x spread). That claim is now measurable: the H comparison table prints
REM     "monthly sigma" and "mean/sigma" per variant. Turn this back on ONLY if the
REM     ATR-parity row actually shows a lower sigma there.
REM     Cost of turning it on: cheap names get sized UP to 10 lots, which raises the
REM     margin peak and crowds out other names because the budget is finite.
REM   set "LSS_SIZE_MODE=atr"
REM   set "LSS_SIZE_TARGET=1200"
REM --- as-of BT ON: score every PAST trade with the BT it had AT SIGNAL TIME.
REM     Without this, 93.7% of the trades in the PnL tab were scored with TODAY's BT.
REM     BT is built from the last 365 days, and the PnL tab shows the last 180 days,
REM     so a stock that made money since February got a high BT and the budget tab's
REM     BT-descending order picked it knowing the outcome (lookahead). See CLAUDE.md.
REM     Today's SIGNAL list is unaffected - only how past results are scored.
REM     To compare for one run: set LSS_ASOF_BT=0 before calling.
if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
REM --- BUDGET: must match `.\jorder --budget`. There is no auto-link between
REM     the report and the live script, so an unset value silently defaults to
REM     400 while the live side runs something else (that happened 2026-08-21).
REM     The 1-name cap follows this as a ratio (LSS_EQ_MAX_PCT, default 50%),
REM     so raising the budget raises the cap too - that is intended.
REM   *** 400 here vs 300 live is a DELIBERATE mismatch (user, 2026-08-22). ***
REM     Research stays on 400 so the numbers stay comparable across days.
REM     BEFORE applying any winner to the live side, re-check it at the live
REM     budget: a smaller budget runs out sooner, so "filter harder" is worth
REM     MORE at 300 than at 400 - the optimum can move.
REM       set LSS_BUDGET_MAN=300 & .\hvar
if not defined LSS_BUDGET_MAN set "LSS_BUDGET_MAN=400"
REM --- dump every settled trade so .\fills can reconcile real fills against the test.
REM     Without it .\fills skips its section 3 and the daily divergence never accumulates.
if not defined LSS_TRADES_CSV set "LSS_TRADES_CSV=lss_trades.csv"
REM --- rebuild the CUMULATIVE lss proposal (union of ALL bases) ---
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
REM --- BASE pool (the --lss-proposal the P&L tabs run on).
REM     Do NOT append a second --lss-proposal on the command line: this .bat
REM     already passes one, and the duplicate is what broke the run on
REM     2026-08-17. Override with the env var instead, or use .\jfast which
REM     sets it to the selected pool (see jfast.bat).
REM BASE POOL = the SELECTED list (2026-08-18). It used to be the wide pool
REM (lss_proposal_full.py) so that one run could also produce the L and K tabs
REM (no-selection). Both of those reasons are now gone:
REM   K  rejected outright - CLAUDE.md 18.45 (kabu cannot read 300+ names at
REM      09:00, and the decay eats the gain even if it could).
REM   L  only exists to split "selection effect" from "read-more effect", which
REM      is a research question, not a daily one.
REM Meanwhile the wide pool DROPS 29%% of the selected pairs (price range /
REM not-shortable / missing 5-min bars), so J was measured on a population that
REM does not match what actually gets ordered, and watch50 cut DEEPER than 50.
REM Having two bases also meant two report files that look identical - that
REM cost half a day on 2026-08-16 (18.40b).
REM   Research the wide pool with:  set LSS_BASE_POOL=lss_proposal_full.py
if not defined LSS_BASE_POOL set "LSS_BASE_POOL=lss_proposal_cumul.py"
python run_signals_holdout_all.py --both --h-tab --no-tenkan --no-market --no-long --no-short --no-symbol-detail --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal %LSS_BASE_POOL% --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
goto :eof

:help
python daily_help.py fast
goto :eof
