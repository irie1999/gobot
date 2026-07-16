@echo off
REM ============================================================
REM run_lss_watcher.bat — lss 日中OCO決済ウォッチャーを起動する
REM   タスクスケジューラ(8:45起動 / 起動漏れは次回PC起動時)から呼ばれる。
REM   多重起動は lss_exit_watcher.py 側の lock で防ぐので、重複起動されても
REM   1つだけが実際に動く。ログは lss_watcher.log に追記。
REM ============================================================

REM このバッチが置かれているフォルダ(=swingtrade)へ移動
cd /d "%~dp0"

REM 実行時刻をログに記録
echo. >> lss_watcher.log
echo ==== %date% %time% 起動 ==== >> lss_watcher.log

REM 本番口座・実決済で起動(kabu未ログインでもスクリプト側が接続を待つ)
python lss_exit_watcher.py --execute --prod >> lss_watcher.log 2>&1

echo ==== %date% %time% 終了 ==== >> lss_watcher.log
