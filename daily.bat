@echo off
REM ============================================================
REM daily.bat - 朝の発注用(6月基準・lssのみ・最速・発注サーバ起動)
REM   Usage (swingtradeフォルダ):  .\daily   (PowerShell)  /  daily  (cmd)
REM   発注に必要な最小限だけ(9:00の発注に間に合う速度を最優先):
REM     --no-long     : ロング方向を廃止(★最大の時短)。発注リストはlssのBT(rec_score)で
REM                     出るのでロード不要。予算タブ/BTフィルタはrec_scoreに自動フォールバック。
REM     --no-short    : ショートも発注に不要。
REM     --no-analysis : 重い5分足スイープ等の分析タブを省略(発注リスト・400万円タブは残る)。
REM     --price-ranges 6000 : 6,000円のみ(無制限は出さない=1パス)。
REM     --no-serve なし     : レポート後に発注サーバを起動(朝の発注/監視トグル用)。
REM   起動時に LSS_CLOSESTOP_RESWEEP / LSS_GUARD_ONLY を必ずクリア(残ると『終値損切り比較』の
REM     5分足フルロード=数百秒が毎回走って激遅になるため)。
REM   基準月=2026-06。ガードは現行3%(検証済)。
REM   追加オプションは末尾に透過:  .\daily --price-ranges 0   等
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM
REM   ※ 深掘り調査(⑦終値比較/㉓指値ガード/5分足スイープ)や long/short の日別成績を見たい時は
REM     別コマンドで(--no-long/--no-short/--no-analysis を外す、RESWEEP/GUARD_ONLY を付ける等)。
REM   ※ 新しい取引日の1本目はlssのBTキャッシュ再構築で少し時間がかかる(不可避)。以降は速い。
REM   ※ 9:00に間に合わせるため、8:45頃までに一度回しておくのが安全。
REM ============================================================
cd /d "%~dp0"
REM --- 残った重い調査フラグを無効化(daily は常に軽く動かす) ---
set "LSS_CLOSESTOP_RESWEEP="
set "LSS_GUARD_ONLY="
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000 --no-analysis --lss-proposal lss_proposal_2026-06.py --long-base 2026-06-30 --no-mirror --no-short --no-long --default-tab lss --force --no-news --no-risk --workers 8 %*
