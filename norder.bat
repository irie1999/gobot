@echo off
REM ============================================================
REM norder.bat - N (09:00 confirm) PAPER RECORDING
REM
REM *** THIS PLACES NO ORDERS AT ALL. NOT ONE YEN. ***
REM   n_open_confirm.py is a copy of k_open_confirm.py with the order calls
REM   PHYSICALLY REMOVED (send_sell / send_moc are gone, dry_run is pinned to
REM   True, and --execute makes it exit at startup). Verified with an AST scan.
REM   If you want to place real J orders, use .\jorder instead.
REM
REM   Usage:  .\norder            (start it by 08:40; it waits by itself)
REM           .\norder --now      (skip the waits, read once - for testing)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHAT IT DOES, IN ORDER (one kabu token, so strictly sequential)
REM   0. build tonight's candidate list (yfinance only, no kabu)
REM        prev-day return >= +1.753 percent, price band 1,000-6,000 yen,
REM        sorted by 20-day turnover, top 50 (kabu registration cap, 18.44)
REM        -> n_signals_<date>.csv
REM   1. waits until 08:47, registers the 50 names and does ONE WARM READ.
REM        Skipping this makes the 09:00 read take 40-140 seconds (18.44).
REM   2. waits until 09:00, then polls every 10s until 09:30:
REM        for each name that opens, records the opening price and the gap.
REM        A name passes when open >= prev close + 100bp (18.54).
REM        Late opens (09:02-09:06) are picked up as they come.
REM        -> k_paper_<date>.csv
REM
REM WHY N USES +100bp AND J USED +75bp
REM   Different strategies. N is the gap-up short found on 2026-08-25:
REM   11.6 years, 388k stock-days, passes on both TRAIN and TEST (18.54).
REM   J is stopped (18.51).
REM
REM AFTER THE CLOSE (15:40 or later, no kabu needed)
REM   python n_paper.py --close
REM     fills in the closing price and prints the paper P&L, so you can
REM     compare it against the "New method N" tab in the report.
REM
REM WHAT THIS IS FOR
REM   The only unmeasured factor left in N is the execution cost. The report
REM   assumes the fill happens exactly at the daily open with zero slippage.
REM   This script records what the board actually shows at 09:00, so the two
REM   can be compared day by day. No money is at risk.
REM ============================================================
cd /d "%~dp0"
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)
echo ============================================================
echo  N + MIRROR + J PAPER RECORDING - no orders, reads the board only
echo    start this by 08:40; it waits for 08:47 and 09:00 by itself
echo    do NOT run .\jorder, .\watch or the order server at the same time
echo    (kabu allows exactly one live token)
echo ============================================================
echo.
echo [0/2] building the candidate list (no kabu)
REM   THREE methods share ONE board read (kabu allows exactly one live token):
REM     N       prev-day return >= +1.753 pct, open >= prev close +100bp, SELL
REM     mirror  prev-day return <= -1.753 pct, open <= prev close -100bp, BUY
REM     J       kept for the record only (18.51 stopped it)
REM   gap_bp is stored for every name that opens, so all three are scored
REM   afterwards from the same data.
REM
REM   *** THE 50-NAME CAP DOES NOT BIND HERE. ***
REM   n_open_confirm rotates the register in 50-name batches, and the board's
REM   OpeningPrice never moves once a name has opened. So reading 250 names
REM   over a few minutes still gives EXACTLY the right selections. Speed only
REM   matters when real orders are placed. Each method is still scored on its
REM   own top-50 by turnover, which is what could actually be traded.
python k_open_confirm.py --collect
python n_paper.py --collect --merge-j
if errorlevel 1 (
  echo.
  echo *** candidate list failed - stopping here ***
  goto :eof
)
echo.
echo [1/2] warm read at 08:47, then [2/2] poll from 09:00
python n_open_confirm.py --prod --poll %*
echo.
echo ============================================================
echo  done. after the close run:  python n_paper.py --close
echo    scores all three from the same board read:
echo      N       gap ^>= +100bp  sell
echo      mirror  gap ^<= -100bp  buy
echo      J       gap ^>= +75bp   (record only)
echo ============================================================
goto :eof

:help
echo .\norder [--now] [--gap-bp 100] [--poll-until 09:30]
echo   Records the 09:00 board for the N method. PLACES NO ORDERS.
echo   Runs: collect -^> 08:47 warm read -^> 09:00 poll.
echo   After the close:  python n_paper.py --close
echo   To place real J orders use .\jorder instead.
goto :eof
