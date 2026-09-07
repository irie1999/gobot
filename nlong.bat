@echo off
REM ============================================================
REM nlong.bat - run the report with a LONG window FOR N ONLY
REM
REM   .\nlong            N over 2000 days (about 8 years)
REM   .\nlong 4200       N over 4200 days (about 11.5 years)
REM   .\nlong 7000       N over 7000 days (about 19 years)
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
REM WHAT IS TURNED OFF
REM   LSS_HEAVY_BLOCKS=0   order-rank / budget sweep / per-strategy LOO /
REM                        filter scan. Those re-run the budget sim dozens
REM                        of times and are not what you came here for.
REM   LSS_PREOPEN_TAB=0    pre-open market variables (18.34b: nothing found)
REM   --days stays at 180  the 5-min tabs stay small and fast
REM
REM FIRST RUN IS SLOW - THAT IS NORMAL
REM   The N disk cache key contains the day count
REM   (ng_v1_<days>_<names>_<latest bar>.pkl), so a NEW day count is a NEW
REM   file: 1,540 names get re-fetched from yfinance. Expect tens of
REM   minutes. The second run at the same day count is ~0.2s.
REM
REM READ THE PERIOD LINE BEFORE TRUSTING ANY NUMBER
REM   The N tab prints what it ACTUALLY got:
REM       period 2015-03-02 - 2026-09-05 (11.5 years / 2,832 sessions)
REM         <- --days 4,200 requested
REM   If the two disagree the header turns red. Judging on a window that
REM   is not there is how analyze_gap_edge printed "FAIL" on empty data
REM   (CLAUDE.md 18.53).
REM
REM SURVIVORSHIP
REM   The name list is the 1,540 stocks listed TODAY. The longer the
REM   window, the more it contains names that were not listed back then
REM   and omits ones that were delisted. 18.58 saw +315 yen/trade over 19
REM   years vs +802 over the recent 13 months - the level moves a lot.
REM
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM ============================================================
cd /d "%~dp0"

set "NDAYS=2000"
if not "%~1"=="" set "NDAYS=%~1"

echo ============================================================
echo  N over %NDAYS% days  (daily bars only - no 5-min, no lss)
echo    the 5-min tabs stay at --days 180
echo    heavy analysis blocks are OFF
echo    first run at a new day count refetches 1,540 names - be patient
echo ============================================================
echo.

set "LSS_NEWGAP_DAYS=%NDAYS%"
set "LSS_HEAVY_BLOCKS=0"
set "LSS_PREOPEN_TAB=0"

call "%~dp0dailyfast.bat" --days 180 --no-serve %2 %3 %4 %5 %6 %7 %8 %9
