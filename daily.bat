@echo off
REM ============================================================
REM daily.bat - 毎朝のlssレポート生成(発注サーバ付き)の短縮コマンド
REM   長いフルコマンドをこれ1つにまとめたもの。
REM   使い方(swingtradeフォルダで):  daily          または  .\daily.bat
REM   追加オプションもそのまま渡せる:  daily --no-analysis   /   daily --lss-tpsl
REM   (末尾の %* が、打ったオプションをそのまま python に渡します)
REM ============================================================

REM このbatのあるフォルダ(=swingtrade)に移動
cd /d "%~dp0"

python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal auto --lss-top 700 --long-base 2025-12-31 --no-mirror --force --no-news %*
