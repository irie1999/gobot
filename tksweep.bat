@echo off
REM ============================================================
REM tksweep.bat - optimize the tenkan (long-conversion) rules:
REM   cutoff time  x  exit rule (stop/target ATR + time backstop)
REM   with a TRAIN/TEST split so a lucky cell cannot be mistaken for an edge.
REM
REM   Usage:  .\tksweep                 (365-day window, 120-day holdout)
REM           .\tksweep --days 240
REM           .\tksweep --by-month
REM
REM   Why a .bat: PowerShell line continuation is a backtick, not ^, and the
REM   exit-rule string contains / and @ which are easy to mis-quote. Every
REM   multi-line command in this repo goes through a .bat (CLAUDE.md 18.10.1).
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Read the output bottom-up:
REM     1. the TRAIN/TEST grid  - is the TRAIN-best cell also positive in TEST?
REM     2. if TRAIN-best and TEST-best differ, the ranking did NOT reproduce
REM        -> do not adopt anything (CLAUDE.md 18.28)
REM     3. every number is vs PURE lss (no conversion at all). Negative means
REM        you would have been better off not converting.
REM
REM   Speed: --bt-min 30 makes the tool run 420 extra days of backtest per pair.
REM   BT30 is effectively no filter (18.24), so --bt-min 0 gives almost the same
REM   population much faster. Pass it if this is too slow.
REM ============================================================
cd /d "%~dp0"

set "TK_CUTOFFS=09:05,09:10,09:30,10:00,11:00,11:30,12:30,13:00,14:00,14:30"
set "TK_EXITS=0/0@11:30,0/0@15:25,0.1/1.0@15:25,0.3/1.5@15:25,0.5/2.0@15:25,0.3/1.5@13:00,0.5/1.0@15:25"

echo [tksweep] cutoffs : %TK_CUTOFFS%
echo [tksweep] exits   : %TK_EXITS%
echo [tksweep] 10 cutoffs x 7 exits = 70 cells, TRAIN/TEST split at 120 days
echo.

python analyze_tenkan_cutoff.py --days 365 --bt-min 30 --workers 8 --holdout-days 120 --min-price 1000 --max-price 6000 --stop-delay-bars 1 --cutoffs "%TK_CUTOFFS%" --tk-exits "%TK_EXITS%" %*
