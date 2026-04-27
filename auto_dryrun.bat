@echo off
REM =====================================================
REM auto_dryrun.bat ― PC起動時の自動ドライラン
REM
REM 【動作】
REM 1. PC起動&ログイン後に自動実行 (Windowsスタートアップフォルダ登録)
REM 2. kabu STATIONの起動を最大3分待機
REM 3. API接続テスト (失敗時はリトライ)
REM 4. bot をドライラン起動
REM 5. bot 内部で9:00まで自動待機 → 15:00自動終了
REM 6. bot終了後、5分足データを日次更新 (1800+銘柄、約30-60分)
REM
REM 【セットアップ】
REM 1. Win+R → shell:startup → スタートアップフォルダ
REM 2. このファイルのショートカット作成 → スタートアップフォルダに配置
REM 3. PC再起動でテスト
REM
REM 【ログ】
REM   logs\YYYY-MM-DD_auto.log
REM
REM 【手動停止】
REM   Ctrl+C または タスクマネージャで python.exe 終了
REM =====================================================

REM コードページをUTF-8に
chcp 65001 > nul

REM スクリプトのフォルダに移動
cd /d "%~dp0"

REM ログフォルダ作成
if not exist "logs" mkdir logs

REM 日付ベースのログファイル名
for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set "dt=%%a"
set "today=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%"
set "logfile=logs\%today%_auto.log"

REM 起動メッセージ
echo ============================================== >> "%logfile%"
echo  自動ドライラン開始: %date% %time% >> "%logfile%"
echo  プロジェクト: %CD% >> "%logfile%"
echo ============================================== >> "%logfile%"

REM kabu STATION の起動を待つ (最大3分)
echo [1/4] kabu STATION 起動待機中... >> "%logfile%"
echo [1/4] kabu STATION 起動待機中... (最大3分)
set retry=0
:wait_kabu
set /a retry+=1
python -c "import requests, os; from pathlib import Path; [os.environ.update({k.strip():v.strip()}) for line in Path('.env').read_text(encoding='utf-8').splitlines() if '=' in line for k,v in [line.split('=',1)]]; exit(0 if requests.post(os.environ['KABU_BASE_URL']+'/kabusapi/token', json={'APIPassword':os.environ['KABU_API_PASSWORD']}, timeout=5).status_code == 200 else 1)" >> "%logfile%" 2>&1
if %errorlevel% equ 0 (
    echo [1/4] ✓ kabu STATION 接続OK >> "%logfile%"
    echo [1/4] ✓ kabu STATION 接続OK
    goto kabu_ready
)
if %retry% geq 18 (
    echo [1/4] ✗ kabu STATION 起動タイムアウト (3分超過) >> "%logfile%"
    echo [1/4] ✗ kabu STATION が起動していません。
    echo        手動でkabu STATIONを起動&ログインしてください。
    timeout /t 30 /nobreak > nul
    exit /b 1
)
timeout /t 10 /nobreak > nul
goto wait_kabu

:kabu_ready
echo [2/4] 接続確認完了 >> "%logfile%"

REM bot 起動 (--dry-run、本番環境のリアル価格使用)
echo [3/4] bot 起動 (ドライラン) >> "%logfile%"
echo [3/4] bot 起動 (ドライラン)
echo. >> "%logfile%"

python kabu_donchian_bot.py --dry-run --margin --max-concurrent 2 --max-risk 1000 --budget 200000 --poll 30 >> "%logfile%" 2>&1

REM bot終了後、5分足データの日次更新 (1800+銘柄、並列5、約30-60分)
echo. >> "%logfile%"
echo ============================================== >> "%logfile%"
echo [4/4] 5分足データ日次更新開始: %date% %time% >> "%logfile%"
echo ============================================== >> "%logfile%"
echo.
echo [4/4] 5分足データ日次更新中... (1800+銘柄、約30-60分)
python daily_fetch_minute.py --workers 5 >> "%logfile%" 2>&1
echo [4/4] ✓ 5分足データ日次更新完了: %date% %time% >> "%logfile%"
echo [4/4] ✓ 5分足データ日次更新完了

REM 終了メッセージ
echo. >> "%logfile%"
echo ============================================== >> "%logfile%"
echo  自動ドライラン終了: %date% %time% >> "%logfile%"
echo ============================================== >> "%logfile%"

REM 終了通知 (任意で30秒待機)
echo.
echo 全処理 終了しました。日次レポート&データ更新ログはログファイルに保存。
echo   ログ: %logfile%
echo.
timeout /t 30 /nobreak > nul
