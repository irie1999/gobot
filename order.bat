@echo off
REM ============================================================
REM order.bat - 朝の発注専用(6月基準・lss・高速・発注サーバ起動)
REM   .\daily は日別成績確認用にロング/ショート+分析タブを出すので重い。
REM   こちらは発注に必要な最小限だけ:
REM     --no-short    : ショートは発注に不要(ロングはlssのBT参照用に残す)
REM     --no-analysis : 重い5分足TP/SLスイープ等の分析タブを省略(発注リスト・400万円タブは残る)
REM     --no-serve なし: レポート後に発注サーバを起動(朝の発注/監視トグル用)
REM   Usage (swingtradeフォルダ):  .\order
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --no-short --default-tab lss --force --no-news --no-risk --workers 8 %*
