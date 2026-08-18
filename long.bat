@echo off
REM ============================================================
REM long.bat - LONG side report (multi-day swing), RESEARCH ONLY
REM   Usage:  .\long                 (365 days)
REM           .\long --days 180
REM           .\long --no-browser
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHAT THIS IS
REM   The original swing strategy: stop-buy entries (MACD/A7/RSI2) and
REM   breakouts (DON/VOL/MOM), held for up to MAX_HOLD days. This is NOT the
REM   same-day short (lss/H/J) that is currently traded live.
REM
REM *** ZERO IMPACT ON J - THAT IS THE WHOLE POINT OF THIS FILE ***
REM   The long path shares three output files with the live order path.
REM   Every one of them is redirected or disabled below. Do not run the long
REM   report with a bare `python run_signals_holdout_all.py` - it WILL clobber
REM   them.
REM
REM     holdout_selected_symbols.py   read by kabu_send_lss._load_symbols
REM                                   -> LSS_SELECTED_OUT (renamed)
REM     signals_latest.json           read by kabu_send_signals
REM                                   -> LSS_SIGNALS_OUT="" (write disabled)
REM     lss_trades.csv                reconciled by .\fills
REM                                   -> LSS_TRADES_CSV (renamed)
REM
REM   Also cleared: the research env vars a PowerShell session may still hold
REM   from earlier sweeps, so the long run starts from a known state.
REM
REM   NOT shared, so left alone:
REM     signal_score_cache.json   long has its own file (lss uses _lss.json)
REM     bt_v18*.pkl               additive cache, the lss run asks for it
REM     signal_list_<date>.csv    never overwritten if it already exists
REM ============================================================
cd /d "%~dp0"

REM --- isolate every file the live J path touches -------------
set "LSS_SELECTED_OUT=holdout_selected_symbols_long.py"
set "LSS_SIGNALS_OUT="
set "LSS_TRADES_CSV=lss_trades_long.csv"

REM --- clear research flags that may linger in the shell -------
set "LSS_BUDGET_SWEEP="
set "LSS_OOS_BUDGET_CSV="
set "LSS_OOS_BUDGET_BT_TIERS="
set "LSS_OOS_BUDGET_DAYS="
set "LSS_DROP_ARTIFACT_DAYS="
set "LSS_NO_UNIVERSE_PRICE_FILTER="
set "LSS_BASE_POOL="
set "LSS_SIGNAL_POOL="
set "LSS_IMPL_PROPOSAL="
set "LSS_H_VARIANT_TAB="
set "LSS_EQ_METHOD="
set "LSS_PRIORITY="

python run_signals_holdout_all.py --min-price 1000 --max-price 6000 --days 365 --force --no-tenkan --no-market --no-news --no-risk --no-symbol-detail --workers 8 --output-suffix _long %*

REM --- put the shell back the way it was ----------------------
set "LSS_SELECTED_OUT="
set "LSS_SIGNALS_OUT="
set "LSS_TRADES_CSV="
