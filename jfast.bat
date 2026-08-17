@echo off
REM ============================================================
REM jfast.bat - measure J with the SELECTED pool as the base (2026-08-17)
REM   Usage:  .\jfast --days 365 --no-serve
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHY THIS EXISTS
REM   dailyfast runs the P&L on the WIDE pool (lss_proposal_full.py) so that one
REM   run can also produce L (no selection x watch50) and K (no selection x watch
REM   unlimited). That has a side effect measured on 2026-08-17:
REM
REM     [E/H] 653/3,508 pairs (19%%) of the selected pool are NOT in the base
REM
REM   Those pairs fall out of the wide pool for price range / not-shortable /
REM   missing 5-min bars. J's candidate list is therefore ~19%% SHORTER than the
REM   real order list, so the watch50 cut reaches DEEPER than 50 and J builds
REM   names that live would never even register (they show as red #NN with a
REM   stop mark in the J detail).
REM
REM   Running the base on the selected pool removes that gap, so watch50 stops
REM   exactly at 50 and the red marks should go to zero. That is the number to
REM   judge J on.
REM
REM COST
REM   L and K tabs become meaningless (base == selected, so "no selection" no
REM   longer exists in this run). K was rejected anyway (CLAUDE.md 18.45), and L
REM   only exists to split "selection effect" from "read-more effect", which is
REM   not the question here.
REM
REM   Do NOT pass --lss-proposal on the command line to do this: dailyfast
REM   already passes one and the duplicate breaks the run. This .bat sets
REM   LSS_BASE_POOL instead, which dailyfast honors.
REM ============================================================
cd /d "%~dp0"
for %%a in (%*) do (
  if /i "%%~a"=="-h"     goto :help
  if /i "%%~a"=="--help" goto :help
  if /i "%%~a"=="/?"     goto :help
)
set "LSS_BASE_POOL=lss_proposal_cumul.py"
echo [jfast] base pool = %LSS_BASE_POOL%  (J is measured on the real order list)
echo [jfast] L / K tabs are meaningless in this run - judge J only.
call "%~dp0dailyfast.bat" %*
goto :eof

:help
echo .\jfast [args passed to dailyfast]
echo   Runs dailyfast with the SELECTED pool (lss_proposal_cumul.py) as the base,
echo   so J's watch50 cuts at exactly 50 and the red rank marks go to zero.
echo   Example:  .\jfast --days 365 --no-serve
goto :eof
