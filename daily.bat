@echo off
REM ============================================================
REM daily.bat - short command for the morning lss report (+ order server)
REM   Usage (in the swingtrade folder):  .\daily      (PowerShell)  /  daily  (cmd)
REM   Extra options pass through:  .\daily --no-analysis   /   .\daily --lss-tpsl
REM   (the trailing %* forwards whatever options you type to python)
REM   ASCII-only on purpose to avoid Shift-JIS codepage mojibake.
REM ============================================================

REM move to the folder that contains this .bat (= swingtrade)
cd /d "%~dp0"

REM Main tab = lss (long-candidate short). Long/short are kept too but LIGHT:
REM they run with --no-analysis (detail tabs skipped, no 5-min) so you still get
REM their per-day (daily) performance grid without the heavy analysis.
REM   - --default-tab lss: opens on the lss tab (long/short are secondary).
REM   - no --lss-top cap: every WF-test-passing/shortable/in-range stock signals;
REM     pick BT30+ by BT desc, invest as budget allows.
REM   - --workers 8: no-top makes ~3200 pairs; parallel reads local cache only.
REM     Override with:  .\daily --workers 4
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal auto --long-base 2025-12-31 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
