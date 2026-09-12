@echo off
REM ============================================================
REM mshadow.bat - MIRROR (buy the gap-down) PAPER RECORDING
REM
REM *** THIS PLACES NO ORDERS AT ALL. NOT ONE YEN. ***
REM   m_shadow.py never imports or calls an order method. It builds the
REM   KabuClient with dry_run=True and, at startup, AST-scans its own source
REM   to prove there is no send_* / place_ / cancel_order reference anywhere.
REM   If that scan finds one, it exits before touching kabu.
REM
REM   Usage:  .\mshadow           (run it AFTER .\nexec has finished, 09:10+)
REM           .\mshadow close     (after 15:40 - paper P and L, no kabu)
REM           .\mshadow --now     (skip the 09:10 guard - for testing)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHY A SEPARATE PROCESS, AND WHY AFTER 09:10
REM   k_open_confirm.py is FROZEN as execution version N2 while the 18.66
REM   gate is being counted (20 fills across 8 sessions with a fill). Adding
REM   the mirror to it would change the version and restart that count.
REM   kabu allows exactly ONE live token, so this must not overlap .\nexec.
REM   That costs nothing here: an opening price does not move once the name
REM   has opened, so reading it late still gives the right decision (18.44).
REM   Speed only matters when you actually send an order, and this sends none.
REM
REM WHAT IT DOES
REM   0. builds tonight's MIRROR candidate list by calling
REM        python n_paper.py --collect --mirror --watch 0
REM      into m_signals_<date>.csv. The N list n_signals_<date>.csv written
REM      by .\nexec this morning is NOT touched.
REM        prev-day return  =  -1.753 pct or lower
REM        price band 1,000-6,000 yen, sorted by 20-day turnover
REM   1. reads the board in 50-name batches, rotating (unregister between
REM      batches; kabu caps total registrations at 50). Default is the top
REM      150 by turnover: only the top 50 could be traded, but recording the
REM      tail is the only way to measure later what widening would do.
REM        a name passes when open  =  prev close -100bp or lower
REM        -> m_paper_<date>.csv  (same columns as k_paper, so n_paper reads it)
REM
REM WHY THE MIRROR
REM   Same rule as N with every sign flipped. On TRAIN it scored about the
REM   same as N (monthly +47,839 vs +49,587, 18.55) and it is easier to run
REM   live: a long needs no borrow, no reverse day charge, no short-sale
REM   price rule. It has never been recorded forward, so this starts that.
REM
REM WHAT IT IS NOT
REM   Not a decision to trade the mirror, and not a switch away from N.
REM   It is a paper log. Nothing here changes what .\nexec does.
REM ============================================================
cd /d "%~dp0"
if /i "%~1"=="close" goto :close
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)
echo ============================================================
echo  MIRROR PAPER RECORDING - no orders, reads the board only
echo    run this AFTER .\nexec has finished (09:10 or later)
echo    do NOT run it while .\nexec is still going
echo    (kabu allows exactly one live token)
echo ============================================================
echo.
python m_shadow.py --prod %*
if errorlevel 1 (
  echo.
  echo *** mirror recording failed - nothing was ordered ***
  goto :eof
)
echo.
echo ============================================================
echo  done. after the close (15:40 or later) run:
echo.
echo      .\mshadow close
echo.
echo  it fills in the closing price and prints the paper P and L.
echo  no kabu needed - it only reads the CSVs written this morning.
echo ============================================================
goto :eof

:close
echo ============================================================
echo  MIRROR PAPER CLOSE - no kabu, no orders
echo ============================================================
python m_shadow.py --close --budget 400
goto :eof

:help
echo .\mshadow [--now] [--read-top 150] [--gap-bp 100]
echo .\mshadow close
echo   Records the 09:00 board for the MIRROR method. PLACES NO ORDERS.
echo   Run it AFTER .\nexec has finished, 09:10 or later.
echo   After the close:  .\mshadow close
goto :eof
