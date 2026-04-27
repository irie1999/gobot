@echo off
REM =====================================================
REM kabu_donchian_bot ドライラン自動起動スクリプト
REM
REM 【使い方】
REM   1. このファイルを kabu_donchian_bot.py と同じフォルダに置く
REM   2. ダブルクリック or Windowsタスクスケジューラから実行
REM
REM 【ログ】
REM   logs\YYYY-MM-DD.log にbotの出力を保存
REM
REM 【注意】
REM   - kabu STATION が起動&ログイン済みであること
REM   - .env が正しく設定されていること
REM =====================================================

REM スクリプトのフォルダに移動 (Task Scheduler対策)
cd /d "%~dp0"

REM ログフォルダ作成 (なければ)
if not exist "logs" mkdir logs

REM 日付ベースのログファイル名
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
set "today=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%"
set "logfile=logs\%today%_dryrun.log"

REM 起動メッセージ
echo ============================================== >> "%logfile%"
echo  起動: %date% %time% >> "%logfile%"
echo  モード: ドライラン (本番環境想定) >> "%logfile%"
echo ============================================== >> "%logfile%"

REM bot 実行 (標準出力&エラーをログに追記)
REM 同時保有2ポジ (max-concurrent 2) で運用
python kabu_donchian_bot.py --dry-run --budget 200000 --max-risk 1000 --max-concurrent 2 --margin --poll 30 >> "%logfile%" 2>&1

REM 終了メッセージ
echo. >> "%logfile%"
echo ============================================== >> "%logfile%"
echo  終了: %date% %time% >> "%logfile%"
echo ============================================== >> "%logfile%"
