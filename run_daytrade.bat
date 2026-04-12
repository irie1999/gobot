@echo off
REM ORB デイトレボット 自動起動バッチ
REM タスクスケジューラから呼び出される

cd /d "%~dp0"
echo %date% %time% ORB bot starting... >> bot_log.txt

REM ドライラン (本番にする時は --dry-run を削除)
python kabu_daytrade_bot.py --dry-run --symbol 8306 --budget 600000

echo %date% %time% ORB bot finished >> bot_log.txt
