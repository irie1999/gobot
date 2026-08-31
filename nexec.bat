@echo off
REM ============================================================
REM nexec.bat - N (09:00 confirm) REAL ORDERS
REM
REM *** THIS PLACES REAL ORDERS. Use .\norder for paper only. ***
REM
REM   .\nexec              DRY RUN. Prints what it would send. No orders.
REM   .\nexec --go         REAL ORDERS on the LIVE account.
REM   .\nexec --go --budget 50    first day: small (1-2 names)
REM
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHAT N IS (18.54 / 18.55, differs from J in four ways)
REM   candidates  n_signals_<date>.csv  (prev-day return >= +1.753 pct,
REM                                      top 50 by 20-day turnover)
REM   pass        open >= prev close + 100bp        (J used +75bp)
REM   size        100 SHARES, FIXED                 (J used equal-yen)
REM   exit        CLOSING MOC ONLY. no stop, no target.   (18.55)
REM
REM *** WHY THERE IS NO WATCHER ***
REM   N holds no barriers, so nothing needs arming, and the MOC no longer
REM   conflicts with a stop order for the same position (in J they were
REM   mutually exclusive - only one order per position). An MOC placed in
REM   the morning fills at the close even if this PC dies. That is the whole
REM   exit path. Do NOT start .\watch or .\jwatch afterwards - they would
REM   touch N positions with J's rules (delay4 / sm0.5 / tm1.0).
REM
REM SEQUENCE
REM   0. build tonight's candidates (yfinance only, no kabu)
REM   1. 08:55  register + one warm read (skipping this costs 40-140s at 09:00)
REM   2. 09:00  poll every 10s; each name that opens and passes is sold with
REM             a PROTECTIVE LIMIT at open x 0.995 (never a market order -
REM             a limit sell fills at or above the limit, so a normal book
REM             behaves like a market order, and a broken book does not fill)
REM   3. 09:10  polling ends -> a CLOSING MOC is placed for every open short.
REM             The unprotected window is therefore at most 10 minutes.
REM   4. after the close, verify with:  .\fills
REM
REM SAFETY
REM   - dry run unless --go is passed
REM   - --budget is the allocation budget; --max-notional is a separate hard
REM     cap on total yen sent. Both in units of 10,000 yen.
REM   - the pre-send check refuses anything that is not exactly 100 shares
REM   - orders are journalled BEFORE they are sent (18.48), so a crash leaves
REM     a record rather than an unrecorded live order
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
if /i "%~1"=="--go"       set "NGO=--execute --prod" & shift & goto :parse
if /i "%~1"=="--budget"   set "NBUD=--budget %~2"    & shift & shift & goto :parse
set "NARGS=%NARGS% %~1"
shift
goto :parse
:parsed

echo ============================================================
if defined NGO (
  echo  N REAL ORDERS - LIVE ACCOUNT
  echo  *** this will send real orders at 09:00 ***
) else (
  echo  N DRY RUN - no orders will be sent
  echo  add --go to send real orders
)
echo    start this by 08:40; it waits for 08:55 and 09:00 by itself
echo    do NOT run .\norder, .\jorder, .\watch or the order server
echo    at the same time (kabu allows exactly one live token)
echo    do NOT start a watcher afterwards - N needs none
echo ============================================================
echo.
echo [0/2] building the candidate list (no kabu)
python n_paper.py --collect --no-mirror
if errorlevel 1 (
  echo.
  echo *** candidate list failed - stopping here ***
  goto :eof
)
echo.
echo [1/2] warm read at 08:55, then [2/2] poll from 09:00 to 09:10
python k_open_confirm.py --n-mode --poll %NGO% %NBUD%%NARGS%
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
echo .\nexec                    dry run (no orders)
echo .\nexec --go               REAL orders, live account
echo .\nexec --go --budget 50   first day: small
echo.
echo   N = prev-day +1.753 pct, open ^>= prev close +100bp, sell 100 shares,
echo       close with a closing MOC. No stop, no target, no watcher.
echo   Use .\norder for paper recording (that one cannot order at all).
goto :eof
