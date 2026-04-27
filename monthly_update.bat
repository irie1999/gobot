@echo off
REM =====================================================
REM 月次 winners 自動ローテーション
REM 月初に過去30日のwinnersを再抽出して daytrade_donchian_winners.py を更新
REM
REM 【使い方】
REM   1. 手動: ダブルクリック
REM   2. 自動: タスクスケジューラで月初1日 8:00 に実行
REM
REM 【ログ】
REM   logs\monthly_update_YYYY-MM-DD.log
REM =====================================================

cd /d "%~dp0"

if not exist "logs" mkdir logs

for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
set "today=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%"
set "logfile=logs\monthly_update_%today%.log"

echo ============================================== >> "%logfile%"
echo  月次ローテーション開始: %date% %time% >> "%logfile%"
echo ============================================== >> "%logfile%"

REM 過去30日のwinnersを抽出 (200,000円予算、PF≥2.0で厳格化)
python daytrade_donchian_compare.py --universe n225 --days 30 --extract-winners --target-preset ultra_freq_v2 --budget 200000 --min-pf 2.0 --min-trades 12 --no-browser >> "%logfile%" 2>&1

echo. >> "%logfile%"
echo  完了: %date% %time% >> "%logfile%"
echo ============================================== >> "%logfile%"
