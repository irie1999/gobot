@echo off
REM ============================================================
REM norder.bat - N (09:00 confirm) PAPER RECORDING - one command
REM
REM *** THIS PLACES NO ORDERS AT ALL. NOT ONE YEN. ***
REM   n_open_confirm.py is a copy of k_open_confirm.py with the order calls
REM   PHYSICALLY REMOVED (send_sell / send_moc are gone, dry_run is pinned to
REM   True, and --execute makes it exit at startup). Verified with an AST scan.
REM
REM   Usage:  .\norder            morning: collect -> 08:47 warm -> 09:00 poll
REM           .\norder close      after the close (15:40+): paper P&L
REM           .\norder mirror     morning, but also record the mirror side
REM           .\norder --now      skip the waits, read once (for testing)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHAT IT DOES (one kabu token, so strictly sequential)
REM   0. build tonight's candidate list (yfinance only, no kabu)
REM        prev-day return >= +1.753 percent, price band 1,000-6,000 yen,
REM        sorted by 20-day turnover, top 50 -> n_signals_<date>.csv
REM   1. waits until 08:47, registers the names, does ONE WARM READ.
REM        Skipping this makes the 09:00 read take 40-140 seconds (18.44).
REM   2. waits until 09:00, polls every 10s until 09:30. For each name that
REM        opens, records the opening price and the gap. A name passes when
REM        open >= prev close + 100bp (18.54). Late opens are picked up.
REM        -> k_paper_<date>.csv
REM
REM *** WHY N ONLY (changed 2026-08-30) ***
REM   This used to read N + mirror + J = 126 names = 3 registration batches.
REM     - J is STOPPED (18.51). Reading it bought nothing.
REM     - The mirror is NOT ADOPTED: under live open-order it is -3.10bp, and
REM       sharing the 4M budget costs 617,527 yen vs N alone (2026-08-30 audit).
REM   The ONLY thing this script can still measure that nothing else can is the
REM   SECOND-LEVEL OPEN ORDER (the last unknown in the -2.64bp deduction).
REM   That needs the FASTEST possible poll cycle, and every extra batch makes
REM   the detection groups coarser -- which UNDERSTATES the very penalty we are
REM   trying to measure. So: 50 names, 1 batch.
REM   Use ".\norder mirror" if you want the mirror recorded anyway (2 batches).
REM
REM WHAT THIS IS FOR
REM   The report assumes the fill happens exactly at the daily open with zero
REM   slippage. This records what the board actually showed at 09:00, and in
REM   what order names were detected, so the two can be compared day by day.
REM   No money is at risk.
REM ============================================================
cd /d "%~dp0"

set "NARGS="
set "NMIRROR=--no-mirror"
set "NMODE=morning"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="-h"     goto :help
if /i "%~1"=="--help" goto :help
if /i "%~1"=="/?"     goto :help
if /i "%~1"=="close"  set "NMODE=close" & shift & goto :parse
if /i "%~1"=="mirror" set "NMIRROR=" & shift & goto :parse
set "NARGS=%NARGS% %~1"
shift
goto :parse
:parsed

if /i "%NMODE%"=="close" goto :close

echo ============================================================
echo  N PAPER RECORDING - no orders, reads the board only
echo    start this by 08:40; it waits for 08:47 and 09:00 by itself
echo    do NOT run .\jorder, .\watch or the order server at the same time
echo    (kabu allows exactly one live token)
if defined NMIRROR echo    reading N only - 50 names - 1 batch - best open-order resolution
if not defined NMIRROR echo    reading N + mirror - 100 names - 2 batches
echo ============================================================
echo.
echo [0/2] building the candidate list (no kabu)
python n_paper.py --collect %NMIRROR%
if errorlevel 1 (
  echo.
  echo *** candidate list failed - stopping here ***
  goto :eof
)
echo.
echo [1/2] warm read at 08:47, then [2/2] poll from 09:00
python n_open_confirm.py --prod --poll%NARGS%
echo.
echo ============================================================
echo  done. after the close (15:40 or later) run:
echo.
echo      .\norder close
echo.
echo  it reads this morning's CSVs (no kabu needed) and prints:
echo    1. which names passed  (open ^>= prev close +100bp)
echo    2. the order the orders would have gone out in
echo         (detection group, then biggest gap first)
echo    3. the paper P^&L
echo ============================================================
goto :eof

:close
echo ============================================================
echo  N PAPER P^&L - reads this morning's CSVs, no kabu needed
echo ============================================================
python n_paper.py --close --budget 400 --seq-sides n%NARGS%
goto :eof

:help
echo .\norder            morning: collect -^> 08:47 warm read -^> 09:00 poll
echo .\norder close      after the close (15:40+): paper P^&L, N only
echo .\norder mirror     morning, but record the mirror side too (2 batches)
echo .\norder --now      skip the waits, read once (for testing)
echo.
echo   Records the 09:00 board for the N method. PLACES NO ORDERS.
echo   To place real J orders use .\jorder instead (J is stopped, 18.51).
goto :eof
