@echo off
REM ============================================================
REM  .\ldt = LDT (Long DayTrade: 逆指値買い=上ブレイク・同日決済) のレポート
REM
REM  なぜ .bat が要るか (2026-08-08):
REM    run_signals_holdout_all.py を素の python で叩くと、daily.bat が渡している
REM    環境変数が **一切効かない**。実際に事故った組み合わせ:
REM      LSS_ASOF_BT       既定OFF → 過去の取引を『今日のBT』で並べる = 先読み
REM      LSS_STOP_DELAY_BARS 既定0 → live(watch.bat = 1)と食い違う
REM    どちらも損益タブの金額を大きく動かす。だから daily.bat と同じ土俵を
REM    このファイルで固定する。**LDT を測るときは必ず .\ldt を使うこと。**
REM
REM  使い方:
REM    .\ldt                      直近180日表示 / 365日バックテスト
REM    .\ldt --days 365           表示窓を伸ばす
REM    .\ldt --no-browser
REM
REM  前提: long_watchlist_proposal_*.py (scan_long_daytrade.py が出力) が必要。
REM        無ければ既存WATCHLISTにフォールバックする(選定の質は落ちる)。
REM ============================================================
cd /d "%~dp0"
REM --- clear heavy research flags ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
set "LSS_ENTRY_DELAY_BARS="
set "LSS_BUDGET_MIN_BT="
set "LSS_MONTH_FROM="
set "LSS_REALISTIC_ENTRY="
REM --- live (watch.bat) と揃える。LDT にも適用される
REM     (backtest_limit_entry.py:1282 `_sd_bars = _LSS_STOP_DELAY_BARS if (_dt_long or ...)`)
set "LSS_STOP_DELAY_BARS=1"
REM --- BT閾値は lss と同じ 40 に揃える(比較可能にするため)
set "LSS_BT_TAB_MIN=40"
REM --- ★ as-of BT: これが無いと過去の取引が『今日のBT』で並び、先読みになる。
REM     lss では勝率が 55-66% → 41% に落ちた。LDT でも同じことが起きる。
if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
REM --- 実約定との突合用に決済トレードを吐く (lss とはファイルを分ける)
if not defined LSS_TRADES_CSV set "LSS_TRADES_CSV=ldt_trades.csv"
python run_signals_holdout_all.py --long-daytrade --ldt-proposal auto --min-price 1000 --max-price 6000 --force --days 365 --no-news --no-risk --workers 8 %*
