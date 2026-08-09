@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================================
echo  夜間スキャン開始: %DATE% %TIME%
echo ============================================================

echo.
echo [%TIME%] ① WF歴史検証 2024-06-19 基準（最古・データDL込み）
python run_wf_history_scan.py --wf-until 2024-06-19 --min-price 1000 --max-price 10000 --workers 1 --no-browser
echo [%TIME%] ① 完了

echo.
echo [%TIME%] ② WF歴史検証 2024-12-19 基準（DL済み・速い）
python run_wf_history_scan.py --wf-until 2024-12-19 --min-price 1000 --max-price 10000 --workers 1 --no-browser
echo [%TIME%] ② 完了

echo.
echo [%TIME%] ③ 本日の通常シグナル（クロス期間WF検証タブ付き）
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,10000 --force --no-browser
echo [%TIME%] ③ 完了

echo.
echo ============================================================
echo  全完了: %DATE% %TIME%
echo ============================================================
pause
