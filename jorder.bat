@echo off
REM ============================================================
REM jorder.bat - J (09:00 confirm) SMALL-LOT LIVE ORDERING
REM   Usage:  .\jorder                 (budget 50 (man-yen), production account)
REM           .\jorder --budget 100     (raise the budget)
REM           .\jorder --dry-run        (print the schedule and exit)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM *** THIS PLACES REAL ORDERS ON THE PRODUCTION ACCOUNT ***
REM   For a paper run with no orders at all, use .\mtest instead.
REM
REM WHAT IT DOES, IN ORDER (one kabu token, so strictly sequential)
REM   0. collect today's signal candidates (yfinance only, no kabu)
REM   1. log pre-open quotes 08:00-08:45   -> preopen_board_<date>.csv
REM        This is the opening-price study (18.35). It keeps running daily.
REM   2. 08:47 warm read (skipping this makes 09:00 take 40-140s)
REM   3. 09:00 onward, poll every 10s until 09:10:
REM        for each name that opens, if open >= prev close + 50bp,
REM        size it and place a PROTECTIVE LIMIT SELL at open x (1 - 50bp).
REM        Names that open late (09:02-09:06) are picked up as they come.
REM
REM DEFAULTS ON PURPOSE
REM   budget 50 and the same value as a hard notional cap. Start small:
REM   slippage is the last unmeasured factor and only real fills can show it.
REM
REM WHY THE POLL STOPS AT 09:10 (not 09:30)
REM   kabu allows exactly one live token, so the watcher cannot start until
REM   this script exits. J is delay4 = the stop is armed 20 min after entry
REM   (09:20 for a 09:00 fill). Polling to 09:30 would leave the position
REM   unprotected past its arming time. Late opens are 93% done by 09:06
REM   (measured, 18.44), so 09:10 is enough. morning_test enforces this.
REM
REM AFTER IT FINISHES - DO THIS IMMEDIATELY
REM   python lss_exit_watcher.py --execute --prod --all-dates --stop-delay-bars 4
REM   Without the watcher the position is held to the close with no stop.
REM   J is delay4, so the watcher MUST use --stop-delay-bars 4 (18.9: the
REM   backtest and the live side must always match).
REM   Do not start it while this script is still running (one token only).
REM
REM AFTER THE CLOSE
REM   .\fills     -> real fills vs the test. This is where real slippage
REM                  finally becomes measurable (18.37).
REM ============================================================
cd /d "%~dp0"
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)
echo ============================================================
echo  J SMALL-LOT LIVE ORDERING - production account
echo    default budget 50 (man-yen), hard cap the same
echo    start this by 07:50 so the pre-open log covers 08:00-08:45
echo    do NOT run .\watch or the order server at the same time
echo ============================================================
python morning_test.py --prod --execute --stop-delay-bars 4 %*
goto :eof

:help
echo .\jorder [--budget 50] [--max-notional 50] [--dry-run]
echo   Places REAL J orders on the production account, small lot by default.
echo   Runs: collect -^> pre-open quote log -^> warm read -^> 09:00 poll+order.
echo   After it ends, start the watcher with --stop-delay-bars 4.
echo   For a no-order paper run use .\mtest instead.
goto :eof
