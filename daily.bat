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

REM lss (long-candidate short): no --lss-top cap. Every WF-test-passing,
REM shortable, in-price-range stock emits a signal; pick BT30+ by BT desc,
REM invest as budget allows. (Long/short sides unaffected.)
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal auto --long-base 2025-12-31 --no-mirror --force --no-news %*
