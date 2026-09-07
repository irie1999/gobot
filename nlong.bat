@echo off
REM ============================================================
REM nlong.bat - run the report with a LONG window FOR N ONLY
REM
REM   .\nlong 700        N over 700 days, 3 tabs, H pane only  (DEFAULT)
REM   .\nlong 700 all    N over 700 days, all 7 tabs
REM   .\nlong 4200       11.5 years, 3 tabs
REM
REM   *** 3 TABS IS THE DEFAULT BECAUSE 7 RAN THE MACHINE OUT OF MEMORY. ***
REM   On 2026-09-08 all 7 variants over 700 days took the whole PC down -
REM   VS Code died with 'oom' and PowerToys crashed too. Each variant holds
REM   its own detail HTML, and the lss pane already carries H/J/L/K on top.
REM   Add "all" only when you actually need the other four, and close other
REM   apps first.
REM
REM WHY THIS EXISTS (2026-09-07)
REM   `.\dailyfast --days 2000` CRASHED THE MACHINE.
REM   --days is the window for the 5-MINUTE tabs (lss / H / J / L). The
REM   5-min data starts 2024-07 (J-Quants intraday add-on is a 2-year
REM   rolling window on every plan, CLAUDE.md 18.6), so anything past
REM   ~800 days holds no bars at all - but the loader still tries to read
REM   it: 1,300 names x 2,000 days x 60 bars ~= 150 MILLION rows.
REM   run_signals_holdout_all.py now clamps --days to 800 and points here.
REM
REM   N is different: it reads DAILY bars only, never 5-min, never the lss
REM   backtest. So N alone can look back years. Its window is a separate
REM   env var, LSS_NEWGAP_DAYS, and that is all this .bat sets.
REM
REM WHICH N TABS
REM   DEFAULT (3)          1 N / 2 mirror / 3 no 50-name cap
REM   with "all" (7 more)  4 no budget      a DIAGNOSTIC, not a rule (18.10)
REM                        5 no price band  1,000-6,000 removed
REM                        6 lower band only
REM                        7 upper band only
REM   plus  n_report_YYYYMMDD.txt   the same numbers as text, and
REM         n_days.csv              one row per session, for analyze_count_axes
REM   The scan is shared, so tabs 2-7 add almost no scan time - but each one
REM   still runs its own budget sim over the whole window.
REM   N also opens FIRST here (only when LSS_NEWGAP_DAYS is set).
REM
REM WHAT IS TURNED OFF
REM   LSS_HEAVY_BLOCKS=0   order-rank / budget sweep / per-strategy LOO /
REM                        filter scan. Those re-run the budget sim dozens
REM                        of times and are not what you came here for.
REM   LSS_PREOPEN_TAB=0    pre-open market variables (18.34b: nothing found)
REM   --days stays at 180  the 5-min tabs stay small and fast
REM
REM FIRST RUN AT A NEW DAY COUNT IS SLOW - THAT IS NORMAL
REM   The N disk cache key contains the day count, so a new count is a new
REM   file and the whole scan re-runs. 7000 days took ~160s per pane.
REM   The second run at the same count reads the pickle in ~8s.
REM   It does NOT re-download: check_daily_span measured the daily cache at
REM   a 25.7-year median on 2026-09-07, so the bars are already local.
REM
REM READ THE PERIOD LINE BEFORE TRUSTING ANY NUMBER
REM   The N tab prints what it ACTUALLY got:
REM       period 2015-03-02 - 2026-09-05 (11.5 years / 2,832 sessions)
REM           ...requested: --days 4,200
REM   If the two disagree the header turns red. Judging on a window that
REM   is not there is how analyze_gap_edge printed "FAIL" on empty data
REM   (CLAUDE.md 18.53).
REM
REM HOW FAR BACK EACH NUMBER REACHES, AND HOW MANY NAMES SURVIVE
REM   Measured 2026-09-07 with check_daily_span.py (1,529 cached names):
REM     days   reaches    years   names that reach it
REM     2000   2021-03      5.5   96.5 pct
REM     4200   2015-03     11.5   87.3 pct   (the 18.54 TRAIN window)
REM     7000   2007-06     19.0   77.2 pct   (18.58: Lehman and 2011)
REM     9000   2002-01     24.6   60.0 pct
REM    11000   1996-07     30.0    0.0 pct   NOTHING EXISTS THERE
REM   THE HARD WALL IS 2000-01-04 - that is where Yahoo Finance starts for
REM   TSE names. About 9,750 days. THERE IS NO CAP IN THE CODE.
REM   Past 7000 you are fighting three walls, none of which raise an error:
REM     1 yfinance coverage for TSE names stops at 2000-01-04
REM     2 survivorship - the universe is the 1,540 names listed TODAY, so a
REM       25-year window measures "firms that survived 25 years"
REM     3 tick sizes. 18.55 prices execution at 1 yen = 4.4bp on the CURRENT
REM       tick table. Before the 2014/2015 refinements the ticks were much
REM       coarser, so the real cost back then was several times that and the
REM       net edge is overstated for the old part of the window.
REM
REM SURVIVORSHIP
REM   The name list is the 1,540 stocks listed TODAY. The longer the
REM   window, the more it contains names that were not listed back then
REM   and omits ones that were delisted. 18.58 saw +315 yen/trade over 19
REM   years vs +802 over the recent 13 months - the level moves a lot.
REM   Long windows answer "does it break", not "what does it earn".
REM
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM   AND NO ANGLE BRACKETS, PIPE OR AMPERSAND, NOT EVEN INSIDE REM. cmd still
REM   parses redirection on REM lines. A placeholder written with angle
REM   brackets ate the parser on 2026-09-07: it printed
REM   "'ntains' is not recognized" and RAN THE WHOLE BATCH TWICE.
REM ============================================================
cd /d "%~dp0"

set "NDAYS=2000"
if not "%~1"=="" set "NDAYS=%~1"

echo ============================================================
echo  N over %NDAYS% days  (daily bars only - no 5-min, no lss)
echo    the 5-min tabs stay at --days 180
echo    heavy analysis blocks are OFF
echo.
echo    N TABS ONLY: N / mirror / no-50-cap    (add "all" for 7)
echo    the 4M-yen / H / J / L / K tabs are NOT built
echo    plus n_report_YYYYMMDD.txt and n_days.csv
echo    N opens first.
echo.
echo    A NEW day count re-runs the scan (7000 days: ~160s per pane).
echo    It does NOT re-download - the daily bars are already local.
echo    Same day count again reads the cache in ~8s.
echo ============================================================
if %NDAYS% GTR 7000 (
  echo.
  echo  *** past 7000 days you are outside what we have ever run ***
  echo      yfinance thins out before ~2000, the universe is the names
  echo      listed TODAY, and 18.55 prices execution on the CURRENT tick
  echo      table - ticks were much coarser before 2014/2015, so the old
  echo      part of the window looks better than it was.
  echo      Read it as "does it break", not as a level.
)
echo.

set "LSS_NEWGAP_DAYS=%NDAYS%"
set "LSS_HEAVY_BLOCKS=0"
set "LSS_PREOPEN_TAB=0"

REM every N variant on
REM *** N TABS ONLY. *** The 4M-yen / H / J / L / K detail tabs are not
REM   built at all. That block is exactly where the MemoryError kept
REM   happening, and this .bat exists to look at N. Set it to 0 if you
REM   want the usual tabs back.
if not defined LSS_NEWGAP_ONLY set "LSS_NEWGAP_ONLY=1"
set "LSS_NEWGAP_MIRROR=1"
set "LSS_NEWGAP_NOCAP=1"
set "LSS_NEWGAP_CAP=1"
REM the other four only with "all" - they are what ran the PC out of memory
if /i "%~2"=="all" (
  set "LSS_NEWGAP_ALL=1"
  set "LSS_NEWGAP_NOPX=1"
  set "LSS_NEWGAP_PXSPLIT=1"
  echo    *** all 7 N tabs - close other apps first, this needs RAM ***
) else (
  set "LSS_NEWGAP_ALL=0"
  set "LSS_NEWGAP_NOPX=0"
  set "LSS_NEWGAP_PXSPLIT=0"
)
REM NOT LSS_NEWGAP_WBMATRIX. The watch x budget grid is 4x4 = 16 budget sims
REM and measured 80 seconds of the 82s that the no-50-cap tab took over a
REM 19-year window. watch and budget are a "how many can I place TODAY"
REM question, so a 19-year window adds nothing. The report skips it by
REM itself past 1500 days UNLESS this var is set - so leave it unset here.
REM Force it back on with:  set LSS_NEWGAP_WBMATRIX=1
REM text mirror of the tabs, so the numbers can be pasted without the HTML
if not defined LSS_NEWGAP_TXT set "LSS_NEWGAP_TXT=auto"
REM one row per session (date, cand, watched, hit, built, used, pnl, missed)
if not defined LSS_NEWGAP_DAYS_CSV set "LSS_NEWGAP_DAYS_CSV=n_days.csv"

REM *** %2 may be the word "all", which is OURS - it must NOT reach python.
REM   Without this, run_signals_holdout_all.py sees a stray positional arg.
set "PASS=%3 %4 %5 %6 %7 %8 %9"
if /i not "%~2"=="all" set "PASS=%2 %3 %4 %5 %6 %7 %8 %9"
REM *** --no-lss: BUILD ONLY THE H PANE. ***
REM   The lss pane is what kept dying. Its traceback was
REM     nikkei_analysis.py  _eh_pane += (...123 lines...)  MemoryError
REM   and it died BEFORE the N tabs even started - so trimming N tabs
REM   never helped it. Meanwhile the H pane finished in 45s at 47MB with
REM   all 7 N variants. The N tabs are IDENTICAL in both panes, so the
REM   lss pane adds nothing here and costs a whole second report.
REM   Roughly half the time and half the memory.
REM   If you ever need the lss pane, run .\dailyfast directly instead -
REM   this .bat is for looking at N, and N is the same in both panes.
call "%~dp0dailyfast.bat" --days 180 --no-serve --no-lss %PASS%
