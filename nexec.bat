@echo off
REM ============================================================
REM nexec.bat - N (09:00 confirm) REAL ORDERS
REM
REM   .\nexec              DRY RUN against the LIVE book. Sends nothing.
REM   .\nexec --go         REAL ORDERS on the live account.
REM   .\nexec --go --budget 50    first day: small (1-2 names)
REM
REM   *** THE DRY RUN READS THE LIVE BOOK (--prod) ON PURPOSE. ***
REM   Only --go adds --execute. A dry run against the demo book would show
REM   different prices and different names, so it would not tell you what
REM   the real run is about to do (2026-08-31 review, item 6).
REM
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHAT N IS (18.54 / 18.55, differs from J in five ways)
REM   candidates  n_signals_<date>.csv  (prev-day return >= +1.753 pct,
REM                                      top 50 by 20-day turnover)
REM   pass        open >= prev close + 100bp, NO UPPER GAP LIMIT,
REM               and the 09:00 open must itself be 1,000-6,000 yen
REM   size        100 SHARES, FIXED
REM   entry       protective limit 2 TICKS below the open (never a market
REM               order, and never a wide percentage - N only has ~21.9bp)
REM   exit        CLOSING MOC ONLY. no stop, no target, NO WATCHER.
REM
REM HOW THE EXIT IS MADE SAFE (this is the part that took two rounds)
REM   J placed all orders, kept partial fills, then swept get_positions() for
REM   a closing MOC - and left the rest to the watcher. N has no watcher, so
REM   that shape leaves two holes: a partial fill can complete AFTER the MOC
REM   quantity was decided, and sweeping positions touches OTHER strategies'
REM   shorts (a margin close cancels their existing closing order).
REM   N instead settles only its own orders:
REM     1. cancel every one of our new-sell orders, filled-in-part included
REM     2. wait until every one of them reaches a terminal state
REM        -> only then is the filled quantity final
REM     3. place one closing MOC per symbol for exactly that quantity
REM     4. confirm each MOC was accepted; shout loudly if any was not
REM
REM SEQUENCE
REM   0. build tonight's candidates (yfinance only, no kabu)
REM   1. 08:55  register + one warm read (skipping this costs 40-140s at 09:00)
REM   2. 09:00  poll every 10s, sell each name that opens and passes
REM   3. 09:10  polling ends -> the settle sequence above runs
REM   4. after the close:  .\fills
REM
REM AFTERWARDS
REM   Do NOT start .\watch or .\jwatch. They read ordered_signals_lss.csv;
REM   N writes ordered_signals_n.csv precisely so they cannot see it. If one
REM   did see an N position it would arm J's rules, and for a position with
REM   no ATR it falls back to an emergency 1 percent stop.
REM ============================================================
cd /d "%~dp0"

set "NGO="
set "NARGS="
set "NBUD=--budget 200"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="-h"     goto :help
if /i "%~1"=="--help" goto :help
if /i "%~1"=="/?"     goto :help
if /i "%~1"=="--go"       set "NGO=--execute"      & shift & goto :parse
if /i "%~1"=="--budget"   set "NBUD=--budget %~2"  & shift & shift & goto :parse
set "NARGS=%NARGS% %~1"
shift
goto :parse
:parsed

echo ============================================================
if defined NGO (
  echo  N REAL ORDERS - LIVE ACCOUNT
  echo  *** this will send real orders at 09:00 ***
) else (
  echo  N DRY RUN - live book, nothing is sent
  echo  add --go to send real orders
)
echo    start this by 08:40; it waits for 08:55 and 09:00 by itself
echo    do NOT run .\norder, .\jorder, .\watch or the order server
echo    at the same time (kabu allows exactly one live token)
echo    do NOT start a watcher afterwards - N needs none, and a watcher
echo    would arm J's rules on N positions
echo ============================================================
echo.
echo [0/2] building the candidate list (no kabu)
python n_paper.py --collect --no-mirror
if errorlevel 1 (
  echo.
  echo *** candidate list FAILED - stopping here ***
  exit /b 1
)
echo.
echo [1/2] warm read at 08:55, then [2/2] poll from 09:00 to 09:10
python k_open_confirm.py --n-mode --prod --poll %NGO% %NBUD%%NARGS%
if errorlevel 1 (
  echo.
  echo *** THE ORDER SCRIPT EXITED WITH AN ERROR ***
  echo     Orders may or may not have been sent. Check kabu station now,
  echo     and if there are open shorts make sure each one has a closing
  echo     MOC. Do NOT assume the run was clean.
  exit /b 1
)
echo.
echo ============================================================
echo  done. after the close (15:40 or later):
echo.
echo      .\fills
echo.
echo  and for the paper comparison:
echo      python n_paper.py --close --budget 400 --seq-sides n
echo ============================================================
goto :eof

:help
echo .\nexec                    dry run against the live book (sends nothing)
echo .\nexec --go               REAL orders, live account
echo .\nexec --go --budget 50   first day: small
echo.
echo   N = prev-day +1.753 pct, open ^>= prev close +100bp, sell 100 shares
echo       at 2 ticks below the open, close with a closing MOC.
echo       No stop, no target, no watcher, no upper gap limit.
echo   Use .\norder for paper recording (that one cannot order at all).
goto :eof
