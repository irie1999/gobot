@echo off
REM ============================================================
REM watch.bat - morning: start ONLY the lss exit watcher (no daily report).
REM   Real execution on the production account. Manages already-placed lss orders:
REM   places resting stop (buy stop) on the board, polls target, does close (MOC) at 15:20.
REM   Startup is now monitoring-first (stop setup before the holdings tab), so it is
REM   ready right at the open.
REM
REM   Prereq: today's lss orders are recorded so the watcher can find them:
REM     - ordered_signals_lss.csv   (from kabu_send_lss), or
REM     - placed_orders_<date>.csv  (from the report order button).
REM   If you placed the orders the EVENING BEFORE, add --all-dates so the watcher also
REM   reads yesterday-dated CSVs:  .\watch --all-dates
REM
REM   Keep it running until the close (15:30). Do NOT close it early (missed close = risk).
REM   Do NOT run the order server at the same time (one kabu token = 401 conflict).
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Usage:
REM     .\watch                 (real, production, poll 5s, close 15:20)
REM     .\watch --all-dates     (also read yesterday's order CSV)
REM     .\watch --poll 3        (faster polling near the open)
REM ============================================================
cd /d "%~dp0"
python lss_exit_watcher.py --execute --prod %*
