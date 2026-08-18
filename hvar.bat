@echo off
REM ============================================================
REM hvar.bat - run the report WITH the H setting sweep turned on.
REM
REM   Usage:  .\hvar                 (365-day window, no order server)
REM           .\hvar --days 180
REM
REM   The daily run keeps LSS_H_VARIANT_TAB=0 because CLAUDE.md 18.36 already
REM   settled the settings (walk-forward lost to "change nothing" in all three
REM   blocks), so there is nothing to decide from it daily. This bat re-measures.
REM
REM   CORRECTION (2026-08-15, measured): this used to say the variant sweep was
REM   "the single biggest cost of the P&L tab". It is not - it is 1.7s, 0.5% of
REM   a 351s run. The filter scan was 303.3s (91.8%) and is now off by default.
REM   Do not guess where the time goes; the run prints a phase-time summary.
REM
REM   Do this roughly monthly, or whenever a rule changes.
REM
REM   Where to look: "long-stock short" tab -> P&L tab -> the collapsed block
REM     "H setting comparison (limit position x auction-only)".
REM   Read it in this order:
REM     1. the "walk-forward selection" row at the bottom. If it is BELOW the
REM        current H row, stop there - changing settings has negative value.
REM     2. the first-half / second-half columns. Same sign (check mark) or it
REM        is noise.
REM     3. only then the diff/month and t.
REM
REM   Uses dailyfast (lighter, same comparison blocks) and --no-serve so it
REM   never fights the watcher for the kabu token.
REM
REM   --no-h-tab: dailyfast always passes --h-tab, and that runs the WHOLE pass
REM   a second time (lss again, only the order type differs) - about 2x the wall
REM   clock. Every block this bat exists to read (the setting-audit board, the
REM   current/E/H/J/K comparison, concentration) lives in the "long-stock short"
REM   pane, so the H pane is pure cost here. Ordering still happens from .\daily.
REM
REM   The run now ends with a phase-time summary (slowest first). Read it before
REM   trying to make anything faster - do not guess where the time goes.
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM ============================================================
cd /d "%~dp0"
REM ============================================================
REM   POOL: the SELECTED list (lss_proposal_cumul.py) - the default base
REM         everywhere since 2026-08-18. The wide pool drops 29%% of the
REM         selected pairs, so settings were being swept on a population that
REM         does not match what gets ordered. Override for one run with
REM         set LSS_BASE_POOL=lss_proposal_full.py
REM   Same-window paired test over 11 months (compare_pools.py):
REM     no-selection minus selection = +124,013 yen/month
REM     t=+2.92 / 95%% CI +29,408..+218,617 / wins 10 of 11 months
REM     primary metric (mean/sigma) also flips 1.56 -> 1.72
REM     first half +145,225 (6-0) / second half +98,558 (4-1)
REM   Selection picks pairs by PAST performance, which mean-reverts; it also
REM   forces START_DATES, which shortens the measurable window by 2 months.
REM   RESEARCH ONLY. daily.bat (the live order list) still uses the cumulative
REM   selection - do not switch that until the remaining checks pass
REM   (regime over 730 days, budget-constrained re-measure, walk-forward).
REM   Override for one run:  .\hvar --lss-proposal lss_proposal_cumul.py
REM   COST: ~5 minutes instead of ~40s (reads 653k symbol-days of 5-min bars).
REM   Regenerate the pool when the universe changes:  python make_full_proposal.py
REM ============================================================
REM Keep the research run from clobbering lss_trades.csv, which .\fills reconciles
REM against the live H orders (CLAUDE.md 18.29).
set "LSS_TRADES_CSV=lss_trades_hvar.csv"
REM Keep the research run from clobbering holdout_selected_symbols.py, which the
REM LIVE order path reads (kabu_send_lss._load_symbols). Switching this bat to the
REM no-selection pool overwrote it with 8,106 pairs on 2026-08-16 - the next
REM morning's order list would silently have been a different universe.
set "LSS_SELECTED_OUT=holdout_selected_symbols_hvar.py"
set "LSS_H_VARIANT_TAB=1"
call dailyfast.bat --days 365 --no-serve --no-h-tab %*
set "LSS_H_VARIANT_TAB="
set "LSS_TRADES_CSV="
set "LSS_SELECTED_OUT="
