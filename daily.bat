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

REM lss (long-candidate short) ONLY. Long/short are skipped (--no-long --no-short)
REM because we only trade lss; this avoids their heavy PnL backtests.
REM   - lss trade-detail BT30 filter falls back to lss's own BT (long BT cache is
REM     not built when long is skipped). The signal/order tab BT is unaffected
REM     (it always uses lss's own BT).
REM   - no --lss-top cap: every WF-test-passing/shortable/in-range stock signals;
REM     pick BT30+ by BT desc, invest as budget allows.
REM   - --workers 8: no-top makes ~3200 pairs; parallel reads local cache only.
REM     Override with:  .\daily --workers 4
python run_signals_holdout_all.py --both --no-long --no-short --min-price 1000 --price-ranges 6000,0 --lss-proposal auto --long-base 2025-12-31 --no-mirror --force --no-news --workers 8 %*
