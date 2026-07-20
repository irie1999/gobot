@echo off
REM ============================================================
REM lss_merge_ladder.bat - 各基準月(単独) と 累積マージ の標準HTMLレポートを一括生成。
REM   目的: 「古いbaseほどOOSが長い」を、いつもの run_signals_holdout_all のHTMLで
REM         それぞれ見比べる。単独6本 + 累積マージ5本 = 計11本のHTMLを出す。
REM   前提: lss_proposal_2025-03 / 2025-06 / 2025-09 / 2025-12 / 2026-03 / 2026-06 が
REM         すべて始値モデル(7/19以降)で選定済みであること。
REM   比較の公平性: BTランキング(ロング参照)を揃えるため、全レポートで
REM         --long-base 2026-06-30 に固定。--no-short で高速化。無制限(--price-ranges 0)。
REM   表示期間: --days 520(=2025-03以降を全レポート共通で表示)。これで古いbaseほど
REM         OOS月数が多い(base月より後が全部OOS)のを同じ窓で読み比べられる。重いが必要。
REM   Usage (swingtrade フォルダ):  .\lss_merge_ladder.bat
REM   ASCII-only on purpose (avoid Shift-JIS mojibake).
REM ============================================================
cd /d "%~dp0"

echo ==================== 累積マージ proposal を生成(union) ====================
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py --mode union --out lss_proposal_cum_2.py
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py --mode union --out lss_proposal_cum_3.py
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py lss_proposal_2025-12.py --mode union --out lss_proposal_cum_4.py
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py lss_proposal_2025-12.py lss_proposal_2026-03.py --mode union --out lss_proposal_cum_5.py
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py lss_proposal_2025-12.py lss_proposal_2026-03.py lss_proposal_2026-06.py --mode union --out lss_proposal_cum_6.py

echo.
echo ==================== K-of-N 投票 proposal を生成(6窓中K窓以上) ====================
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py lss_proposal_2025-12.py lss_proposal_2026-03.py lss_proposal_2026-06.py --min-votes 4 --out lss_proposal_vote4.py
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py lss_proposal_2025-12.py lss_proposal_2026-03.py lss_proposal_2026-06.py --min-votes 5 --out lss_proposal_vote5.py
python merge_lss_proposals.py lss_proposal_2025-03.py lss_proposal_2025-06.py lss_proposal_2025-09.py lss_proposal_2025-12.py lss_proposal_2026-03.py lss_proposal_2026-06.py --min-votes 6 --out lss_proposal_vote6.py

echo.
echo ==================== 単独 基準月レポート(6本) ====================
call :rep lss_proposal_2025-03.py _s2025-03
call :rep lss_proposal_2025-06.py _s2025-06
call :rep lss_proposal_2025-09.py _s2025-09
call :rep lss_proposal_2025-12.py _s2025-12
call :rep lss_proposal_2026-03.py _s2026-03
call :rep lss_proposal_2026-06.py _s2026-06

echo.
echo ==================== 累積マージレポート(5本) ====================
call :rep lss_proposal_cum_2.py _cum2_0306
call :rep lss_proposal_cum_3.py _cum3_to0925
call :rep lss_proposal_cum_4.py _cum4_to1225
call :rep lss_proposal_cum_5.py _cum5_to0326
call :rep lss_proposal_cum_6.py _cum6_all

echo.
echo ==================== K-of-N 投票レポート(3本) ====================
call :rep lss_proposal_vote4.py _vote4
call :rep lss_proposal_vote5.py _vote5
call :rep lss_proposal_vote6.py _vote6

echo.
echo ============================================================
echo DONE. 14本のHTMLを生成しました。ファイル名の suffix で見分けます:
echo   単独:   signals_holdout_all_both_*_s2025-03.html ... _s2026-06.html
echo   累積:   signals_holdout_all_both_*_cum2_0306.html ... _cum6_all.html
echo   投票:   signals_holdout_all_both_*_vote4.html / _vote5.html / _vote6.html
echo   各HTML -> lssタブ -> 1,000〜無制限 -> 400万円 -> 2026/07行(および各月)を比較。
echo   単独: 古いbase(2025-03)ほどOOS月数が多い(2025-04以降が全部OOS)=長期検証向き。
echo   投票: 繰り返し選ばれた頑健核。OOSは7月のみだが本物度が高い。
echo ============================================================
goto :eof

REM ---- :rep <proposal.py> <output-suffix> ----
REM 全レポート共通: 無制限 / lssのみ表示(--no-long --no-short) / 高速。
REM   --no-long: ロングタブを出さず、ロング計算もスキップ(=速い)。BTランキングは既存の
REM   ロング参照キャッシュ bt_v3_<バー>.pkl から読むので同一。※そのキャッシュが必要なので、
REM   新しい取引日は最初に一度 .\daily 等(ロング込み)を回してキャッシュを作っておくこと。
:rep
echo -------------------- report: %1  (suffix %2) --------------------
python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 0 --days 520 --lss-proposal %1 --long-base 2026-06-30 --no-mirror --no-short --no-long --default-tab lss --force --no-news --no-risk --workers 8 --output-suffix %2
goto :eof
