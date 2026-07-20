@echo off
REM ============================================================
REM daily.bat - 朝の発注用(6月基準・lss・高速・発注サーバ起動)
REM   Usage (swingtradeフォルダ):  .\daily   (PowerShell)  /  daily  (cmd)
REM   速度優先で最小限に統合(発注に必要なものだけ):
REM     --no-short    : ショートは発注に不要(ロングはlssのBT参照用に残す=long tabも見れる)
REM     --no-analysis : 重い5分足TP/SLスイープ等の分析タブを省略(発注リスト・400万円タブは残る)
REM     --no-serve なし: レポート後に発注サーバを起動(朝の発注/監視トグル用)
REM   基準月=2026-06 (lss_proposal_2026-06.py)。ガードは現行3%(検証済:新基準では3%が最良)。
REM   追加オプションは末尾に付けると透過:  .\daily --price-ranges 0   等
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM
REM   ※ 深掘り調査したい日(⑦終値比較/㉓指値ガード/5分足スイープ)は --no-analysis を外すか
REM     LSS_CLOSESTOP_RESWEEP=1 / LSS_GUARD_ONLY=1 を付けて別途実行。
REM   ※ ショートの日別成績も見たい日は --short を末尾に足す:  .\daily --short
REM   ※ 新しい取引日の1本目はBTキャッシュ再構築で少し時間がかかる(不可避)。以降は速い。
REM ============================================================
cd /d "%~dp0"
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --no-analysis --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --no-short --default-tab lss --force --no-news --no-risk --workers 8 %*
