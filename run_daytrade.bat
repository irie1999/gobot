@echo off
REM ORB デイトレボット 自動起動バッチ (10銘柄監視版)
REM タスクスケジューラから平日 08:55 に呼び出される

cd /d "%~dp0"

REM 日付別のログファイル名
set LOGDIR=dry_run_logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set LOGFILE=%LOGDIR%\dry_run_%date:~0,4%-%date:~5,2%-%date:~8,2%.log

echo ================================================== > "%LOGFILE%"
echo %date% %time% ORB 10銘柄監視ボット 開始 >> "%LOGFILE%"
echo ================================================== >> "%LOGFILE%"

REM ドライラン実行 (Python出力を全てログに保存)
python kabu_daytrade_bot.py --dry-run --budget 600000 >> "%LOGFILE%" 2>&1

echo. >> "%LOGFILE%"
echo ================================================== >> "%LOGFILE%"
echo %date% %time% 終了 >> "%LOGFILE%"
echo ================================================== >> "%LOGFILE%"
