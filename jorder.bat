@echo off
REM ============================================================
REM jorder.bat - J (09:00 confirm) SMALL-LOT LIVE ORDERING
REM   Usage:  .\jorder                 (budget 60 (man-yen), production account)
REM           .\jorder --budget 100     (raise the budget)
REM           .\jorder --dry-run        (print the schedule and exit)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM *** THIS PLACES REAL ORDERS ON THE PRODUCTION ACCOUNT ***
REM   For a paper run with no orders at all, add --no-order:
REM       .\jorder --budget 300 --no-order
REM   --no-order BEATS --execute, so nothing is ordered even though this
REM   script hardcodes --execute. Everything else runs the same way, so the
REM   two runs are directly comparable.
REM   .\mtest does the same thing (it just never passes --execute).
REM   For the N method (gap-up short, 18.54) use .\norder instead.
REM
REM WHAT IT DOES, IN ORDER (one kabu token, so strictly sequential)
REM   0. collect today's signal candidates (yfinance only, no kabu)
REM   1. log pre-open quotes 08:00-08:45   -> preopen_board_<date>.csv
REM        This is the opening-price study (18.35). It keeps running daily.
REM   2. 08:47 warm read (skipping this makes 09:00 take 40-140s)
REM   3. 09:00 onward, poll every 10s until 09:10:
REM        for each name that opens, if open >= prev close + 75bp,
REM        size it and place a PROTECTIVE LIMIT SELL at open x (1 - 50bp).
REM        (+75bp is the pass threshold, changed from +50bp on 2026-08-21;
REM         the -50bp on the limit is unrelated - it is the fill protection.)
REM        Per-name cap is half the budget (--max-yen-pct, default 50).
REM        Names that open late (09:02-09:06) are picked up as they come.
REM
REM DEFAULTS ON PURPOSE
REM   budget 60 (man-yen) and the same value as a hard notional cap.
REM   60 is the SMALLEST amount that can still buy one lot of a 6,000-yen name,
REM   which is the top of the price band. At 50 those names silently size to
REM   zero lots, so the sample would lose every high-priced stock - bad when
REM   the whole point is measuring slippage. Start small otherwise: slippage
REM   is the last unmeasured factor and only real fills can show it.
REM
REM WHY THE POLL STOPS AT 09:10 (not 09:30)
REM   kabu allows exactly one live token, so the watcher cannot start until
REM   this script exits. J is delay4 = the stop is armed 20 min after entry
REM   (09:20 for a 09:00 fill). Polling to 09:30 would leave the position
REM   unprotected past its arming time. Late opens are 93% done by 09:06
REM   (measured, 18.44), so 09:10 is enough. morning_test enforces this.
REM
REM   4. the exit watcher starts AUTOMATICALLY right after ordering ends,
REM      with --stop-delay-bars 4 (J is delay4; 18.9 says the backtest and the
REM      live side must always match) and --entry-cutoff 09:15 as a safety net
REM      that sweeps any entry limit that never filled. It runs until 15:30.
REM      *** DO NOT CLOSE THE WINDOW BEFORE 15:30 *** - closing it early means
REM      missed exits (18.4). Use .\jorder --no-watch to start it by hand.
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
echo    (add --no-order for a paper run with no orders at all)
echo    default budget 60 (man-yen), hard cap the same
echo    start this by 07:50 so the pre-open log covers 08:00-08:45
echo    the exit watcher starts by itself after ordering and runs to 15:30
echo    *** DO NOT CLOSE THIS WINDOW BEFORE 15:30 ***
echo    do NOT run .\watch or the order server at the same time
echo ============================================================
python morning_test.py --prod --execute --stop-delay-bars 4 %*
goto :eof

:help
echo .\jorder [--budget 60] [--max-notional 60] [--dry-run] [--no-order]
echo   Places REAL J orders on the production account, small lot by default.
echo   --no-order turns it into a paper run (beats the hardcoded --execute).
echo   Runs: collect -^> pre-open quote log -^> warm read -^> 09:00 poll+order.
echo   The exit watcher (--stop-delay-bars 4) starts automatically and runs
echo   until 15:30. Add --no-watch to start it by hand instead.
echo   For a no-order paper run use .\mtest instead.
goto :eof
