@echo off
REM ============================================================
REM chk.bat - verify tonight's order records BEFORE the open.
REM   Usage:  .\chk            (today)   /   .\chk --date 2026-08-13
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Reads ordered_signals_lss.csv ONLY. It does NOT touch kabu, so it is
REM   safe to run while the order server or the watcher is up (no token fight).
REM
REM   Why: on day 1 (2026-08-13) six defects were found, ALL of them only
REM   AFTER real money was on the line. The worst one - the order button not
REM   sending atr - left all four positions with NO stop loss. One look at
REM   the CSV right after ordering would have caught it while the orders
REM   could still be re-sent.
REM
REM   Checks: order type is a LIMIT sell / atr,sm,tm present / short side
REM   ordering (stop > limit > target) / widths match ATRxsm and ATRxtm /
REM   qty / duplicate names / total vs budget.
REM ============================================================
cd /d "%~dp0"
python check_orders.py %*
