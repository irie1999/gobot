@echo off
rem ── 日次メインコマンド（これ1つを実行すればOK） ──
rem レポート生成 → ブラウザ表示 → 発注サーバ(本番・実発注)起動 まで一括。
rem order_server が約定監視＋📌保有タブ自動更新も行うため、他のコマンドは不要。
rem ダブルクリック、またはコマンドプロンプトで run_daily.bat と打つだけ。
cd /d "%~dp0"

rem Python の解決（タスクスケジューラ/環境差対策。py ランチャ優先）
rem ★動かない場合は次行のremを外してフルパスを設定:
rem set "PYEXE=C:\Users\to732\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not defined PYEXE ( where py >nul 2>&1 && set "PYEXE=py" )
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE ( echo [ERROR] Python not found. Set PYEXE in run_daily.bat & pause & exit /b 1 )

"%PYEXE%" run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,10000 --no-wf-history --no-preoos --force --serve --serve-execute --serve-prod

rem run_signals_holdout_all は --serve で発注サーバを前面起動したまま待機する。
rem 終了(Ctrl+C)後、ここで画面を保持。
echo.
echo ============================================================
echo  DONE / stopped. Press any key to close.
echo ============================================================
pause >nul
