@echo off
REM ============================================================
REM watcher.bat - lss intraday OCO + budget-cap + close exit watcher
REM   Usage (swingtrade folder):
REM     .\watcher                    LIVE  : 本番実行(デフォルト) — real stop/target/close + budget cancel on PROD
REM     .\watcher --no-prod          DEMO  : デモ口座で実決済(デバッグ用)
REM     dry-run したい場合は直接実行: python lss_exit_watcher.py --budget-cap 4000000 --stop-delay-bars 1
REM
REM   Always applied:
REM     --execute             : 実際に決済発注する(デフォルトON)
REM     --prod                : 本番口座(18080)(デフォルトON)
REM     --budget-cap 4000000  : once filled lss short notional reaches 400man, cancel the
REM                             remaining un-triggered lss stop-sell orders (over-subscribe cap).
REM     --stop-delay-bars 1   : delay1 = skip the stop on the fill 5min bar, enable from the
REM                             next grid (avoids the open first-bar wick; take-profit/close stay on).
REM
REM   Rules (see CLAUDE.md 18.4 / 18.7):
REM     - Keep running until 15:30 (do NOT stop early = missed close exit).
REM     - Do NOT run together with the order server (single kabu token -> 401 clashes).
REM       Morning: place orders from the HTML, then switch to this watcher.
REM     - On days this watcher runs, do NOT also run close_lss_guard.py (double-exit).
REM   Pass-through: anything after .\watcher is forwarded (e.g. --poll 3).
REM ============================================================
python lss_exit_watcher.py --execute --prod --budget-cap 4000000 --stop-delay-bars 1 %*
