@echo off
REM =====================================================
REM run_daytrade_menu.bat — デイトレ運用 メニューランチャー
REM
REM 使い方:
REM   run_daytrade_menu.bat              # メニュー表示
REM   run_daytrade_menu.bat scan         # WFスキャンのみ
REM   run_daytrade_menu.bat watchlist    # WATCHLIST 生成のみ
REM   run_daytrade_menu.bat report       # 統合レポート表示
REM   run_daytrade_menu.bat bot          # bot ドライラン
REM   run_daytrade_menu.bat verify       # WATCHLIST 検証
REM   run_daytrade_menu.bat forward      # フォワードテスト
REM   run_daytrade_menu.bat update       # データ更新のみ
REM =====================================================

chcp 65001 > nul
cd /d "%~dp0"

if "%1"=="scan" goto :scan
if "%1"=="watchlist" goto :watchlist
if "%1"=="report" goto :report
if "%1"=="bot" goto :bot
if "%1"=="verify" goto :verify
if "%1"=="forward" goto :forward
if "%1"=="update" goto :update
goto :menu

:menu
echo =====================================================
echo  デイトレ運用 メニュー
echo =====================================================
echo  1. データ更新 (1-30分)
echo  2. WFスキャン (1-2時間)
echo  3. WATCHLIST 生成
echo  4. 統合レポート表示 (即時)
echo  5. フォワードテスト
echo  6. WATCHLIST 検証
echo  7. bot ドライラン (本日終日)
echo  8. シグナルJSON エクスポート
echo  Q. 終了
echo.
set /p choice="選択: "
if "%choice%"=="1" goto :update
if "%choice%"=="2" goto :scan
if "%choice%"=="3" goto :watchlist
if "%choice%"=="4" goto :report
if "%choice%"=="5" goto :forward
if "%choice%"=="6" goto :verify
if "%choice%"=="7" goto :bot
if "%choice%"=="8" goto :export
goto :end

:update
python yfinance_update.py --days 60 --workers 5
pause
goto :menu

:scan
echo WFスキャン開始 (約1-2時間)
python scan_walkforward_daytrade.py --max-price 6000 --min-price 1000 --workers 4
pause
goto :menu

:watchlist
python build_watchlist_daytrade.py --combined --top 30 --max-price 6000 --min-price 1000
pause
goto :menu

:report
python run_signals_holdout_all_daytrade.py
pause
goto :menu

:forward
python forward_test_daytrade.py --days 30
pause
goto :menu

:verify
python verify_watchlist_daytrade.py
pause
goto :menu

:bot
echo bot ドライラン起動 (Ctrl+Cで停止)
python kabu_daytrade_multi_bot.py --dry-run --margin --max-concurrent 2 --max-risk 1000 --budget 300000 --poll 30 --regime-filter
pause
goto :menu

:export
python signals_export_daytrade.py
pause
goto :menu

:end
echo 終了
