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
REM Base month = 2026-06 (lss_proposal_2026-06.py). Generate it first (SAME method as
REM current: all prices=default max-price, all other args default):
REM   python scan_lss_universe.py --base-month 2026-06 --out lss_proposal_2026-06.py --source local
REM (or run .\lss_base_sweep.bat which regenerates all base months the same way, latest fixes).
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 %*
