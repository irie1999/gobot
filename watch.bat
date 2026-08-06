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
REM   --all-dates is ENABLED BY DEFAULT (orders are placed the evening before, so the
REM   order CSV is dated yesterday). Same-morning orders still work (today is included).
REM
REM   Keep it running until the close (15:30). Do NOT close it early (missed close = risk).
REM   Do NOT run the order server at the same time (one kabu token = 401 conflict).
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Usage:
REM     .\watch                 (real, production, all-dates on, poll 5s, close 15:20)
REM     .\watch --poll 3        (faster polling near the open)
REM ============================================================
cd /d "%~dp0"
REM --all-dates is ON by default here: orders are placed the EVENING BEFORE, so
REM their CSV is dated yesterday. Without --all-dates the watcher would pick up
REM nothing. If you ever place orders same-morning, that still works (today is a
REM subset of all-dates). Extra options pass through, e.g. .\watch --poll 3
REM
REM --stop-delay-bars 2 (delay2) is ON by default: for the entry 5min bar AND the
REM next one the watcher places NO stop, then arms the stop from the grid after
REM that (09:00 fill -> stop armed 09:10). Avoids getting wicked out by the opening
REM bounce on the tight 0.1ATR stop. Target/close stay active during the no-stop
REM window. To disable for one run: .\watch --stop-delay-bars 0
REM   240d BT40+ net(realistic): base +1,791,907 / delay1 +2,358,023 /
REM   delay2 +2,436,051 (peak; delay3/4 fall back to ~+2,361,000).
REM   delay2 wins in all 3 fill models and in 7 of 9 months.
REM
REM --entry-cutoff 13:00: from 13:00 on, unfilled lss entry stops are cancelled --
REM lss exits same day, so a late fill has no time to reach target while the stop
REM stays fully live. 240d BT40+: +26,299 realistic / +49,936 conservative on top
REM of delay2. The benefit GROWS as the fill model gets more pessimistic, which is
REM the point: late fills are exactly what the backtest prices worst (2026-08-06
REM 5632 real -9,600 vs sim -4,552). To disable for one run: .\watch --entry-cutoff ""
python lss_exit_watcher.py --execute --prod --all-dates --budget-cap 4000000 --stop-delay-bars 2 --entry-cutoff 13:00 %*
