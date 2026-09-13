@echo off
REM ============================================================
REM backup_all.bat - back up EVERYTHING (code history + research data)
REM   Code is already on GitHub; this makes an extra offline copy so a
REM   PC failure loses nothing. Copies the Git-ignored research assets too.
REM
REM   Usage (swingtrade folder):
REM     .\backup_all.bat                 -> backup to  ..\gobot_backup
REM     .\backup_all.bat D:\backup\gobot -> backup to that path (USB etc.)
REM
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"

set "DEST=%~1"
if "%DEST%"=="" set "DEST=..\gobot_backup"

echo ============================================================
echo Backup destination: %DEST%
echo ============================================================
if not exist "%DEST%" mkdir "%DEST%"

REM --- 1) full Git history as a single file (restore with: git clone gobot_full.bundle) ---
echo [1/6] git bundle (full code history)...
git bundle create "%DEST%\gobot_full.bundle" --all
if errorlevel 1 echo   [warn] git bundle failed - is this a git repo?

REM --- 2) 5-min data (stock_5min, sibling folder) - IRREPLACEABLE, not in git ---
echo [2/6] stock_5min (5-min data)...
if exist "..\stock_5min" (
  robocopy "..\stock_5min" "%DEST%\stock_5min" /E /R:1 /W:1 /NFL /NDL /NP >nul
) else (
  echo   [skip] ..\stock_5min not found
)

REM --- 3) walk-forward scan results (CSV) ---
echo [3/6] walkforward_results...
if exist "walkforward_results" robocopy "walkforward_results" "%DEST%\walkforward_results" /E /R:1 /W:1 /NFL /NDL /NP >nul

REM --- 4) forward-test paper-trade logs (IRREPLACEABLE) ---
echo [4/6] forward_test logs + proposals...
copy /y forward_test_log*.csv "%DEST%\" >nul 2>&1
copy /y lss_proposal_*.py "%DEST%\" >nul 2>&1
copy /y watchlist_proposal_*.py "%DEST%\" >nul 2>&1
copy /y symbols_listed_*.py "%DEST%\" >nul 2>&1
copy /y symbols_all.py "%DEST%\" >nul 2>&1
copy /y not_shortable.py "%DEST%\" >nul 2>&1
copy /y my_positions.csv "%DEST%\" >nul 2>&1

REM --- 5) secrets (.env) - keep OFFLINE only, never upload to public places ---
echo [5/6] .env (secrets - keep offline)...
copy /y .env "%DEST%\env_backup.txt" >nul 2>&1

REM --- 6) daily-cache (rsi2) - regenerable, copy so you don't re-download ---
echo [6/6] .rsi2_cache (optional, regenerable)...
if exist ".rsi2_cache" robocopy ".rsi2_cache" "%DEST%\rsi2_cache" /E /R:1 /W:1 /NFL /NDL /NP >nul

echo.
echo ============================================================
echo DONE. Backup at: %DEST%
echo   - gobot_full.bundle : full code history (git clone gobot_full.bundle gobot)
echo   - stock_5min\       : 5-min data (irreplaceable)
echo   - forward_test_log*.csv / *proposal*.py / walkforward_results\
echo   - env_backup.txt    : your .env (SECRET - do not share/upload)
echo Tip: copy %DEST% to a USB drive or a second cloud for real off-PC safety.
echo ============================================================
