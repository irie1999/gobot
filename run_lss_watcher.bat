@echo off
REM ============================================================
REM run_lss_watcher.bat - launch the lss intraday OCO exit watcher
REM   Called by Task Scheduler (daily 8:45 / StartWhenAvailable).
REM   Duplicate launches are blocked by a lock inside lss_exit_watcher.py,
REM   so even if this runs twice only one instance actually works.
REM   Output is appended to lss_watcher.log.
REM   (ASCII-only on purpose to avoid codepage issues.)
REM ============================================================

REM Move to the folder that contains this .bat (= swingtrade)
cd /d "%~dp0"

echo. >> lss_watcher.log
echo ==== %date% %time% START ==== >> lss_watcher.log

REM Live account, real settlement. The script waits/retries if kabu is not logged in yet.
python lss_exit_watcher.py --execute --prod >> lss_watcher.log 2>&1

echo ==== %date% %time% END ==== >> lss_watcher.log
