@echo off
REM ============================================================
REM hsweep.bat - sweep H's limit price (prev close +/- N ticks).
REM
REM   Usage:  .\hsweep                  (offsets -3..+5, 120-day holdout)
REM           .\hsweep --offsets -5,0,5,10
REM           .\hsweep --holdout-days 180
REM
REM   H is "sell limit at the previous close". Where that limit sits is H's
REM   only defining parameter and it has never been swept:
REM     raise it  -> sell higher, but fewer fills
REM     lower it  -> more fills, but sell cheaper (drifts toward the current lss)
REM
REM   Reads lss_trades.csv (written by .\daily) for the (symbol, date) universe,
REM   so run .\daily first. Excludes tenkan rows (not lss).
REM
REM   This is WITHOUT the budget cap. If an offset looks good:
REM       set LSS_H_LIMIT_TICKS=N
REM       .\daily
REM   and compare H in the E/H tab under the real 4M budget (CLAUDE.md 18.10).
REM
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM   Env must match the live/report settings, same as daily.bat.
REM ============================================================
cd /d "%~dp0"

set "LSS_STOP_DELAY_BARS=1"

if not exist lss_trades.csv (
  echo [error] lss_trades.csv not found. Run .\daily first.
  exit /b 1
)

python sweep_h_limit.py --trades lss_trades.csv --workers 8 --holdout-days 120 --min-price 1000 --max-price 6000 %*
