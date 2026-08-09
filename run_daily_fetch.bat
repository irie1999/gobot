@echo off
REM 毎日の5分足データ自動取得
REM タスクスケジューラから 15:35 に実行

cd /d "%~dp0"
echo %date% %time% daily fetch starting... >> daily_fetch_log.txt

python daily_fetch_minute.py

echo %date% %time% daily fetch finished >> daily_fetch_log.txt
