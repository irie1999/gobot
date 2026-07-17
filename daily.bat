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

python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal auto --lss-top 700 --long-base 2025-12-31 --no-mirror --force --no-news %*
