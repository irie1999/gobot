@echo off
rem ── フォワードテスト 日次記録（バックテストと実運用の乖離を実測） ──
rem 毎営業日の引け後(15:35頃)にタスクスケジューラから実行する想定。
rem 今日のシグナルを記録し、既存の保留分を実データで約定/決済評価する。
rem ※無人で繰り返すため pause は入れない。
cd /d "%~dp0"

if not defined PYEXE ( where py >nul 2>&1 && set "PYEXE=py" )
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE ( echo [ERROR] Python not found. Set PYEXE in run_forward_test.bat & exit /b 1 )

rem conservative(既定)モードを記録。aggressiveも記録したい場合は下行のremを外す。
"%PYEXE%" forward_test.py --record
rem "%PYEXE%" forward_test.py --record --aggressive
