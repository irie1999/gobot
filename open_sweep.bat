@echo off
REM ============================================================
REM open_sweep.bat - reopen the 4 base-month sweep reports (no re-scan).
REM   Opens the NEWEST report for each base month (2024-06/12, 2025-06/12).
REM   Run lss_base_sweep.bat first to generate them; then use this to reopen anytime.
REM   Usage (swingtrade folder):  .\open_sweep.bat
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"

call :openlatest _base2024-12
call :openlatest _base2025-06
call :openlatest _base2025-12
goto :eof

REM ---- :openlatest <suffix> : open the newest _both html with that suffix ----
:openlatest
for /f "delims=" %%f in ('dir /b /a-d /o-d "signals_holdout_all_both_*%~1.html" 2^>nul') do (
  echo Opening %%f
  start "" "%%f"
  goto :eof
)
echo   (not found: *%~1.html  -- run lss_base_sweep.bat first)
goto :eof
