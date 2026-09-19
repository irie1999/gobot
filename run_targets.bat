@echo off
rem ── ザラ場中の利確指値 発注（約定済み建玉に利確指値を置く） ──
rem 9:00〜15:00 に15〜30分おきにタスクスケジューラから実行する想定。
rem 約定からなるべく早く利確指値を板に出して、同日の目標タッチを拾うのが目的。
rem ※繰り返し無人実行するため pause は入れない（入れると多重起動・ハングの原因）。
cd /d "%~dp0"

if not defined PYEXE (
  where py >nul 2>&1 && set "PYEXE=py"
)
if not defined PYEXE (
  where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  echo [ERROR] Python not found. Set PYEXE full path in run_targets.bat
  exit /b 1
)

rem 利確指値のみ発注（決済・損切りはしない）。本番・実発注。
"%PYEXE%" close_stop_guard.py --targets --kabu --prod --execute
