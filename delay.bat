@echo off
REM ============================================================
REM delay.bat - "would a longer stop-delay have saved today's stop-outs?"
REM   Usage (swingtrade folder):  .\delay   (PowerShell)  /  delay  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Run AFTER .\fills. Takes the symbols that ACTUALLY filled today, uses the
REM   REAL sell price and REAL fill time from fills_<yyyyMMdd>.csv, and replays
REM   each one on the 5min bars with the stop armed after 0/1/2/.../N bars.
REM     d0 = stop live immediately (base)
REM     d1 = stop armed 5 min after the fill  (current live setting)
REM     d2 = stop armed 10 min after the fill
REM   Prints per-symbol results, a delay summary, and the list of stop-outs that
REM   a longer delay would have avoided (and the ones it would have made worse).
REM
REM   READ-ONLY. Never places an order. Needs 5min data (stock_5min), not kabu.
REM
REM   WARNING: this is ONE day of data. Do NOT change the live --stop-delay-bars
REM   on the strength of it. The decision is made on the 240-day population:
REM     python compare_lss_rules.py --bt-min 40 --days 240 --min-price 1000 --max-price 6000
REM
REM   Options:
REM     .\delay --date 2026-08-06        (a past day; needs fills_20260806.csv)
REM     .\delay --max-delay 8            (sweep up to 8 bars = 40 min)
REM     .\delay --bars 6247              (dump that symbol's 5min bars vs the stop line)
REM ============================================================
cd /d "%~dp0"
python analyze_today_delay.py %*
