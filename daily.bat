@echo off
REM ============================================================
REM daily.bat - 朝の発注用(6月基準・lss・高速・発注サーバ起動)
REM   Usage (swingtradeフォルダ):  .\daily   (PowerShell)  /  daily  (cmd)
REM   速度優先で最小限(発注に必要なものだけ):
REM     --price-ranges 6000 : 6,000円のみ(無制限は出さない=1パス。発注は6000円のみ)
REM     --no-short          : ショートは発注に不要(ロングはlssのBT参照用に残す)
REM     --no-analysis       : 重い5分足スイープ等の分析タブを省略(発注リスト・400万円タブは残る)
REM     --no-serve なし     : レポート後に発注サーバを起動(朝の発注/監視トグル用)
REM   起動時に LSS_CLOSESTOP_RESWEEP / LSS_GUARD_ONLY を必ずクリア:
REM     これらが残っていると毎回『終値損切り比較』(5分足フルロード=数百秒)が走って激遅になるため。
REM   基準月=2026-06。ガードは現行3%(検証済)。
REM   追加オプションは末尾に透過:  .\daily --short   /   .\daily --price-ranges 0
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM
REM   ※ 深掘り調査(⑦終値比較/㉓指値ガード)をしたい時だけ、別ターミナルで
REM     $env:LSS_CLOSESTOP_RESWEEP="1" (または LSS_GUARD_ONLY="1") を付けて手動実行する。
REM   ※ 新しい取引日の1本目はBTキャッシュ再構築で少し時間がかかる(不可避)。以降は速い。
REM ============================================================
cd /d "%~dp0"
REM --- 残った重い調査フラグを無効化(daily は常に軽く動かす) ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --no-short --default-tab lss --force --no-news --no-risk --workers 8 %*
