@echo off
REM =====================================================
REM kabu_donchian_bot 本番運用 自動起動スクリプト
REM
REM ⚠️ 警告: --dry-run なし = 実発注!
REM
REM 【使い方】
REM   1. ドライランで5営業日以上動作確認後に使用
REM   2. 30万円委託保証金が入金済みであること確認
REM   3. リスク管理ルールを把握してから実行
REM
REM 【ログ】
REM   logs\YYYY-MM-DD_live.log
REM =====================================================

REM スクリプトのフォルダに移動
cd /d "%~dp0"

if not exist "logs" mkdir logs

for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
set "today=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%"
set "logfile=logs\%today%_live.log"

echo ============================================== >> "%logfile%"
echo  起動: %date% %time% >> "%logfile%"
echo  モード: 本番運用 (実発注!) >> "%logfile%"
echo  予算: 200,000 / リスク: 1,000 / 同時保有: 1 >> "%logfile%"
echo ============================================== >> "%logfile%"

REM 本番実行 (--dry-run なし)
python kabu_donchian_bot.py --budget 200000 --max-risk 1000 --max-concurrent 1 --margin --poll 30 >> "%logfile%" 2>&1

echo. >> "%logfile%"
echo ============================================== >> "%logfile%"
echo  終了: %date% %time% >> "%logfile%"
echo ============================================== >> "%logfile%"
