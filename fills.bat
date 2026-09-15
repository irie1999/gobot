@echo off
REM ============================================================
REM fills.bat - today's ACTUAL trade results from kabu (live account)
REM   Usage (swingtrade folder):  .\fills   (PowerShell)  /  fills  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Runs verify_fills.py against the LIVE account (--prod) and auto-saves
REM   two CSVs named by date (--save):
REM     fills_<yyyyMMdd>.csv   - round-trip trades with REAL pnl and fill times
REM     orders_<yyyyMMdd>.csv  - every order placed today (filled or not)
REM
REM   Two sections are printed:
REM     (1) all orders  - incl. unfilled / cancelled / expired -> shows fill rate
REM     (2) results     - realized pnl of completed round trips
REM   Compare these against the report/backtest expectation to measure the
REM   live-vs-backtest gap (CLAUDE.md 18.7 forward test).
REM
REM   READ-ONLY. Never places an order.
REM
REM   TIMING: run after the close (15:30+), once the MOC buybacks have settled.
REM
REM   IMPORTANT: kabu allows ONE active token. If the order server or the
REM   lss watcher is running you may get 401 (token tug-of-war). Stop one of
REM   them for a moment, or wait and retry.
REM
REM   Pass-through options:
REM     .\fills --date 20260728        (a past day)
REM     .\fills --no-date              (do not filter by date)
REM     .\fills --expected exp.csv     (diff against expected values)
REM     .\fills --fee 0.001            (apply a one-way fee rate; default 0)
REM ============================================================
cd /d "%~dp0"
python verify_fills.py --prod --save %*
