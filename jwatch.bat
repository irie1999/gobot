@echo off
REM ============================================================
REM jwatch.bat - restart the J exit watcher by hand (recovery path)
REM   Usage:  .\jwatch          (production, J settings)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHEN TO USE THIS
REM   .\jorder starts the watcher by itself. Use this ONLY if that watcher
REM   died and you need to bring it back mid-session. That is exactly what
REM   happened on 2026-08-19 (18.46): it died 2 seconds after starting and
REM   8 positions sat unguarded for 6h20m, then got force-closed the next
REM   morning for 17,600 yen in fees.
REM
REM *** DO NOT USE .\watch FOR J ***
REM   watch.bat is the OLD lss watcher: --stop-delay-bars 1 and a budget cap.
REM   J is delay4. Restarting with .\watch would arm the stop at 09:05 instead
REM   of 09:20, i.e. the live side would stop matching the backtest (18.9).
REM   This file is the same command morning_test.py starts, nothing more.
REM
REM IF IT SAYS "another lss_exit_watcher is running (lock)"
REM   The lock is a heartbeat file that goes stale after 180 seconds. If the
REM   old process really is dead, wait 3 minutes and run this again. Do not
REM   delete the lock by hand - if the old process is actually alive you would
REM   end up with two watchers closing the same position twice.
REM
REM WHAT IT PICKS UP ON RESTART
REM   - open positions are read from kabu, so it does not matter that this
REM     process did not place them
REM   - .lss_watcher_seen.json restores when each position was first seen, so
REM     the delay4 window is NOT restarted from zero
REM   - MOC already sitting on the board (placed by k_open_confirm on exit) is
REM     left alone; the watcher swaps it for the stop when the delay expires
REM
REM *** DO NOT CLOSE THIS WINDOW BEFORE 15:30 *** (18.4)
REM ============================================================
cd /d "%~dp0"
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)
echo ============================================================
echo  J EXIT WATCHER - restart by hand (production account)
echo    delay4 / entry-cutoff 09:15 - same as .\jorder starts
echo    *** DO NOT CLOSE THIS WINDOW BEFORE 15:30 ***
echo    do NOT run .\watch, .\jorder or the order server at the same time
echo ============================================================
python lss_exit_watcher.py --execute --prod --all-dates --stop-delay-bars 4 --entry-cutoff 09:15 %*
goto :eof

:help
echo .\jwatch
echo   Restarts the J exit watcher (delay4, entry-cutoff 09:15) on the
echo   production account. Use only when the watcher .\jorder started has died.
echo   Do NOT use .\watch for J - it is delay1 and would not match the backtest.
echo   "lock" message: wait 3 minutes (the lock goes stale) and run again.
goto :eof
