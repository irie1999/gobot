@echo off
REM ============================================================
REM tenkan.bat - today's TENKAN result (what the unfilled lss orders would have paid)
REM   Usage (swingtrade folder):  .\tenkan   (PowerShell)  /  tenkan  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Run AFTER .\fills. With no arguments it auto-picks today's orders_<yyyyMMdd>.csv
REM   (written by .\fills) and simulates the long-conversion on every SELL order that
REM   did NOT fill: buy at the first bar from 09:09, sell at the last bar up to 11:30.
REM
REM   Saves two files every run (no flag needed):
REM     tenkan_<yyyyMMdd>.csv   - per-symbol detail for that day
REM     tenkan_daily_log.csv    - one row per day, appended. Re-running a day REPLACES
REM                               its row, so it never duplicates. This is the history
REM                               that answers "how much did tenkan add on days lss
REM                               missed?" (CLAUDE.md 18.5.2)
REM
REM   READ-ONLY. Never places an order. Needs 5min data (stock_5min), not kabu.
REM
REM   Options:
REM     .\tenkan --date 2026-08-04            (a past day; needs orders_20260804.csv)
REM     .\tenkan --symbols 3036,3132,6237     (explicit list, no orders CSV needed)
REM     .\tenkan --all                        (include filled sells too = reference)
REM     .\tenkan --no-save                    (print only, do not write CSVs)
REM ============================================================
cd /d "%~dp0"
python tenkan_today.py %*
