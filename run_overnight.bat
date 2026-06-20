@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================================
echo  夜間スキャン開始: %DATE% %TIME%
echo ============================================================

echo.
echo [%TIME%] ① WF歴史検証 2025-12-20 基準（データDL + WFスキャン）
python run_signals_holdout_all.py --both --min-price 1000 --max-price 6000 --wf-until 2025-12-20 --workers 1 --force --no-browser
echo [%TIME%] ① 完了

echo.
echo [%TIME%] ② WF歴史検証 2025-06-20 基準（データDL済み・高速）
python run_signals_holdout_all.py --both --min-price 1000 --max-price 6000 --wf-until 2025-06-20 --workers 2 --force --no-browser
echo [%TIME%] ② 完了

echo.
echo [%TIME%] ③ 本日の通常シグナル
python run_signals_holdout_all.py --both --min-price 1000 --max-price 6000 --force --no-browser
echo [%TIME%] ③ 完了

echo.
echo ============================================================
echo  全完了: %DATE% %TIME%
echo ============================================================
pause
