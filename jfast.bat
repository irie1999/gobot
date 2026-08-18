@echo off
REM ============================================================
REM jfast.bat - NOW JUST AN ALIAS FOR dailyfast (2026-08-18)
REM
REM   .\jfast  ==  .\dailyfast
REM
REM WHY IT USED TO EXIST
REM   dailyfast ran the P&L on the WIDE pool (lss_proposal_full.py) so that one
REM   run could also produce L (no selection x watch50) and K (no selection x
REM   watch unlimited). jfast overrode that to the SELECTED pool so J would be
REM   measured on the list that actually gets ordered.
REM
REM WHY IT IS GONE
REM   The selected pool is now the DEFAULT base everywhere (daily / dailyfast /
REM   hvar). Both reasons for the wide pool expired:
REM     K  rejected outright - CLAUDE.md 18.45
REM     L  only splits "selection effect" from "read-more effect" = research
REM   and the wide pool DROPS 29%% of the selected pairs (price range /
REM   not-shortable / missing 5-min bars), so J was measured on the wrong
REM   population and watch50 cut deeper than 50.
REM
REM   Two bases also meant two report files that look identical. That cost half
REM   a day on 2026-08-16 (18.40b). One base, one file, no confusion.
REM
REM   Research the wide pool for one run:
REM     set LSS_BASE_POOL=lss_proposal_full.py
REM     .\dailyfast --days 365 --no-serve
REM     set LSS_BASE_POOL=
REM
REM   This file stays so that typing .\jfast keeps working.
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM ============================================================
cd /d "%~dp0"
echo [jfast] This is now the same as .\dailyfast - the selected pool
echo [jfast] (lss_proposal_cumul.py) is the DEFAULT base everywhere.
call "%~dp0dailyfast.bat" %*
