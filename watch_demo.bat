@echo off
REM ============================================================
REM watch_demo.bat - DEMO account watcher with 300man budget cap.
REM   Same as watch.bat but:
REM     - DEMO mode (port 18081, no --prod)
REM     - budget-cap 3,000,000 (cancel unfilled orders above 300man notional)
REM   Use for trial runs before switching to production.
REM
REM   Usage:
REM     .\watch_demo              (demo, 300man cap, delay1 on)
REM     .\watch_demo --poll 3     (faster polling near the open)
REM ============================================================
cd /d "%~dp0"
python lss_exit_watcher.py --execute --all-dates --budget-cap 3000000 --stop-delay-bars 1 %*
