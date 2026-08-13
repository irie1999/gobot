@echo off
REM ============================================================
REM hvar.bat - run the report WITH the H setting sweep turned on.
REM
REM   Usage:  .\hvar                 (365-day window, no order server)
REM           .\hvar --days 180
REM
REM   The daily run keeps LSS_H_VARIANT_TAB=0 because pricing TEN variants
REM   (limit position x auction-only x stop delay x stop anchor x sizing) off
REM   the 5-minute bars is the single biggest cost of the P&L tab, and
REM   CLAUDE.md 18.36 already settled the settings (walk-forward lost to
REM   "change nothing" in all three blocks). This bat is the way to re-measure.
REM
REM   Do this roughly monthly, or whenever a rule changes.
REM
REM   Where to look: "long-stock short" tab -> P&L tab -> the collapsed block
REM     "H setting comparison (limit position x auction-only)".
REM   Read it in this order:
REM     1. the "walk-forward selection" row at the bottom. If it is BELOW the
REM        current H row, stop there - changing settings has negative value.
REM     2. the first-half / second-half columns. Same sign (check mark) or it
REM        is noise.
REM     3. only then the diff/month and t.
REM
REM   Uses dailyfast (lighter, same comparison blocks) and --no-serve so it
REM   never fights the watcher for the kabu token.
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM ============================================================
cd /d "%~dp0"
set "LSS_H_VARIANT_TAB=1"
call dailyfast.bat --days 365 --no-serve %*
set "LSS_H_VARIANT_TAB="
