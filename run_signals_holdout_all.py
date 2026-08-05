"""
run_signals_holdout_all.py — nikkei_analysis.py のシグナル・損益タブと
完全同一フォーマットで、複数ホールドアウト設定を1画面で確認する。

WATCHLISTの優先順:
  1. ホールドアウト専用CSV  walkforward_{strat}_holdout{N}d_*.csv
  2. 標準WF CSV            walkforward_{strat}_*.csv  (holdoutなし)
  3. フォールバック         check_signals_stop/breakout の WATCHLIST

使い方:
  python run_signals_holdout_all.py
  python run_signals_holdout_all.py --workers 8
  python run_signals_holdout_all.py --no-browser
  python run_signals_holdout_all.py --date 2026-06-09
  python run_signals_holdout_all.py --min-score 60
  python run_signals_holdout_all.py --days 180   # 最初に表示する期間 (デフォルト180)
  python run_signals_holdout_all.py --short --force  # 当日キャッシュを無視して再生成

当日キャッシュ:
  同一パラメータ(--short/--symbol/--date)の出力HTMLが当日分すでに存在すれば、
  重いバックテストをスキップしてそのファイルを開いて即終了する。
  フィルター(--max-price/--min-price/--days 等)を変えた場合や強制再計算したい
  場合は --force を付ける。
"""
from __future__ import annotations

import argparse
import copy as _copy
import csv
import importlib as _importlib
import os
import re
import sys

# OpenBLAS/MKLのスレッド数を1に制限（メモリ不足エラー回避）
# importより前に設定する必要がある
for _blas_env in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_blas_env, "1")
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

_JST_TZ = timezone(timedelta(hours=9))

def _report_date():
    """レポート基準日: 15時以降なら当日、それ以前なら前営業日（深夜実行時に翌日付けになるのを防ぐ）"""
    now   = datetime.now(_JST_TZ)
    today = now.date()
    wd    = today.weekday()  # 0=Mon...6=Sun
    if wd == 5:  # 土
        return today - timedelta(days=1)
    if wd == 6:  # 日
        return today - timedelta(days=2)
    if now.hour >= 15:
        return today  # 引け後 → 当日
    if wd == 0:  # 月
        return today - timedelta(days=3)  # 前金曜
    return today - timedelta(days=1)

# ── 引数先読み ────────────────────────────────────────────────────────────────
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--workers",    type=int,   default=1)
_pre.add_argument("--no-browser", action="store_true")
_pre.add_argument("--date",       type=str,   default=None)
_pre.add_argument("--min-score",  type=int,   default=0)
_pre.add_argument("--wf-dir",     type=Path,  default=Path("walkforward_results"))
_pre.add_argument("--auto-scan",  action="store_true")
_pre.add_argument("--max-price",  type=float, default=10000.0)
_pre.add_argument("--min-price",  type=float, default=0.0,
                  help="最新終値の下限 (円/株). 低位株除外 (例: 1000)")
_pre.add_argument("--days",       type=int,   default=180,
                  help="損益タブで最初に表示する期間 (30/60/90/120/150/180)")
_pre.add_argument("--days-from-base", action="store_true",
                  help="表示期間を『選定基準月の翌月〜今日』にする(基準月スイープのOOS全体を表示)。"
                       "基準月は --output-suffix の base<YYYY-MM> か --long-base から判定")
_pre.add_argument("--symbol",     type=str,   default=None,
                  help="指定銘柄の期間別取引詳細を追加表示 (例: 8050.T)")
_pre.add_argument("--short",      action="store_true",
                  help="ショート戦略(A7_S/RSI2_S/MACD_S/DON_S/MOM_S/GAP_S/VOL_S)で出力")
_pre.add_argument("--force",      action="store_true", default=True,
                  help="当日の生成済みHTMLがあっても無視して再生成する(既定ON)")
_pre.add_argument("--no-force",    dest="force", action="store_false",
                  help="当日キャッシュがあれば再生成しない")
_pre.add_argument("--entry-days", type=int, default=None,
                  help="取引明細をエントリー日ベースで絞り込む日数 (例: 7=直近1週間エントリーのみ)")
_pre.add_argument("--both",       action="store_true",
                  help="ロング+ショート両方を実行して1つのHTMLにまとめる")
_pre.add_argument("--mirror",     action="store_true",
                  help="ロングミラー(指値空売り・同日決済/日計り)で評価。"
                       "ロング銘柄・戦略はそのままに、約定損益を符号反転し max_hold=0 に固定")
_pre.add_argument("--long-stop-short", dest="long_stop_short", action="store_true",
                  help="ロング銘柄を逆指値の空売り(stop_sell)で評価(下ブレイク継続狙い)")
_pre.add_argument("--long-daytrade", dest="long_daytrade", action="store_true",
                  help="ロングデイトレ(逆指値買い=上ブレイク・同日決済)で評価。lssの鏡像。"
                       "1月などロング有利な相場を取るための検証・銘柄確認用(当面 発注は分析専用)")
_pre.add_argument("--ldt-proposal", type=str, default=None,
                  help="ロングデイトレの選定ファイル(scan_long_daytrade が出力する "
                       "long_watchlist_proposal_*.py / long_proposal_*.py)を銘柄リストとして使う。"
                       "'auto' で最新の long_watchlist_proposal_*.py を自動検出")
_pre.add_argument("--lss-proposal", type=str, default=None,
                  help="lss専用の選定ファイル(scan_lss_universe が出力する lss_watchlist_proposal_*.py)を"
                       "銘柄リストとして使う。指定するとWF選定の代わりに SELECTED を全設定共通で使用し、"
                       "取引明細メインの軽量lssタブになる。'auto'で最新の提案ファイルを自動検出")
_pre.add_argument("--lss-top", type=int, default=0,
                  help="提案ファイル(TRAIN期待値順に整列済み)から使う上位ペア数(0=全部)。"
                       "既存の提案をそのまま使い、価格フィルタ後の上位N本だけ採用する(再スキャン不要)。推奨150")
_pre.add_argument("--long-base", type=str, default=None,
                  help="ロングのWF選定CSV参照as-ofを指定日に固定(例 2025-12-31)。各ホールドアウトが"
                       "walkforward_<戦略>_holdout{N}d_<日付>.csv を拾い、2025-12基準で6ホールドアウト"
                       "選定になる。表示(直近N日)は今日のまま=2026がOOS。lssと同じ土俵(2025-12選定→2026検証)")
_pre.add_argument("--no-mirror", action="store_true",
                  help="--both でロングミラー(却下済み・重い5分足スイープ)を生成しない")
_pre.add_argument("--no-short", action="store_true",
                  help="--both でショート(WF選定ショート)を生成しない")
_pre.add_argument("--no-long", action="store_true",
                  help="--both でロングを生成しない(lss単独の調査を高速化)。lssの②BT軸分析は"
                       "rec_scoreベースなので影響なし。BT30フィルタタブのみrec_scoreにフォールバック")
_pre.add_argument("--bt-max", type=float, default=None,
                  help="ロングBTスコアの上限グローバル絞り込み(この値未満の銘柄だけ"
                       "レポート全体を集計)。未指定なら全銘柄を集計し、取引明細の"
                       "『BT40以下』ボタンで切り替える運用(mirror/lssではロングBTで判定)")
_pre.add_argument("--bt-min", type=float, default=None,
                  help="ロングBTスコアの下限グローバル絞り込み(この値以上の銘柄だけ集計)")
_pre.add_argument("--mirror-sm", type=float, default=None,
                  help="mirror/lss の損切ATR倍率(sm)を一括上書き。同日TP/SL最適化で"
                       "見つけた最適幅を損益/取引明細に適用する(例 0.3)")
_pre.add_argument("--mirror-tm", type=float, default=None,
                  help="mirror/lss の利確ATR倍率(tm)を一括上書き(例 0.3)")
_pre.add_argument("--daily-tpsl-sweep", action="store_true",
                  help="mirror/lss に日足版の同日TP/SLスイープタブも出す(既定OFF。"
                       "日足は同日の当たり判定が近似で不正確なため、通常は5分足版のみ)")
_pre.add_argument("--no-5m", action="store_true",
                  help="mirror/lss の取引明細を5分足で集計しない(日足近似のまま)。"
                       "既定は5分足で集計(正確)")
_pre.add_argument("--no-news", action="store_true",
                  help="ニュース取得(スコア計算＋ニュース・情報タブ)をスキップして高速化する")
_pre.add_argument("--no-risk", action="store_true",
                  help="決算日/リスクチェック(kabutan等へのWeb取得)をスキップして高速化する。"
                       "503連発で遅い初回に有効。同日キャッシュがある2回目は元から速い")
_pre.add_argument("--lss-tpsl", action="store_true",
                  help="--lss-proposal 使用時でも『㉔ 5分足TP/SL最適化』タブ(損切/利確の"
                       "最適ATR幅)を出す。初回は計算、以降はキャッシュ表示(SAMEDAY5M_RESWEEP=1で再計算)")
_pre.add_argument("--tpsl-wide", action="store_true",
                  help="㉔ 5分足TP/SL のグリッドを広げる(利確ATR tm を 1.5/2.0/3.0 まで)。"
                       "現行★が端(tm=1.0)にあるので、さらに伸ばすと良いか検証用。別キャッシュ")
_pre.add_argument("--price-ranges", type=str, default=None,
                  help="複数の株価上限をカンマ区切りで指定 (例: 6000,10000). --bothと組み合わせて使用")
_pre.add_argument("--output-suffix", type=str, default="",
                  help="出力HTMLファイル名にサフィックスを付ける (内部用・--bothから自動設定)")
_pre.add_argument("--default-tab", type=str, default="long",
                  choices=["long", "short", "mirror", "lss"],
                  help="統合レポートを開いたとき最初に表示する方向タブ (既定 long)。"
                       "lss検証スイープでは lss を指定すると『ロング銘柄ショート』が最初に開く")
_pre.add_argument("--rolling", type=int, default=0,
                  help="ローリング逆指値: 未約定時に終値で注文価格を更新する最大回数 (例: --rolling 2)")
_pre.add_argument("--wf-until", type=str, default=None,
                  help="WF歴史検証の最新基準日 (YYYY-MM-DD). 省略時は6ヶ月前を自動設定")
_pre.add_argument("--wf-periods", type=int, default=4,
                  help="WF歴史検証の期間数・6ヶ月間隔 (デフォルト: 4期間 = 約2年分). 0を指定するとスキップ")
_pre.add_argument("--no-wf-history", action="store_true", default=True,
                  help="WF歴史検証タブをスキップ（高速化・既定ON）")
_pre.add_argument("--wf-history", dest="no_wf_history", action="store_false",
                  help="WF歴史検証タブを計算する")
_pre.add_argument("--no-preoos", action="store_true", default=True,
                  help="OOS前BTスコア計算をスキップ（高速化・既定ON）")
_pre.add_argument("--preoos", dest="no_preoos", action="store_false",
                  help="OOS前BTスコア計算を行う")
_pre.add_argument("--wf-universe", type=str, default=None,
                  help="WF歴史検証で使うユニバースファイル (例: symbols_all.py=N225, symbols_listed_prime.py=プライム全体). デフォルト: 自動検出")
_pre.add_argument("--oos-until", type=str, default=None,
                  help="OOS検証終了日 (YYYY-MM-DD). 指定時のみOOS検証HTMLを生成する")
_pre.add_argument("--oos-days",  type=int, default=365,
                  help="OOS検証期間（日数）. デフォルト365日")
_pre.add_argument("--max-holds", type=str, default=None,
                  help="比較する最大保有日数をカンマ区切りで指定 (例: 7,15,20). --bothと組み合わせて使用")
_pre.add_argument("--recalc-analysis", action="store_true",
                  help="保有期間比較・押し目買い比較・寄り確認など日々変わらない構造分析を"
                       "強制再計算する (既定: 最新キャッシュを日付跨ぎで再利用。--force では再計算しない)")
_pre.add_argument("--minute-dir", default=None,
                  help="⑭寄り確認タブ用の5分足CSVディレクトリ ({code}.csv)。"
                       "無指定はyfinance(約60日)")
_pre.add_argument("--open-confirm", action="store_true",
                  help="⑭寄り確認タブを計算する(5分足を読むので重い)。"
                       "未指定でもキャッシュがあれば表示。既定OFFで日次レポートを軽量化")
_pre.add_argument("--no-analysis", action="store_true",
                  help="重い解析タブ(詳細分析タブ群・約定タイミング・期間別パネル30/60/…日)"
                       "をスキップして高速化。相場環境・トレンド期間・エントリー分析・"
                       "銘柄詳細の相場コンテキストタブは計算する(シグナル+損益+相場は残る)")
_pre.add_argument("--forward-days", type=int, default=0,
                  help="過去検証(--date)で損益タブを『基準日〜基準日+N日』だけ集計する。"
                       "現在まで走らせないので軽量。例: --forward-days 365 で基準月+1年。"
                       "(--date 指定が前提。当時BTスコアは先読みなしのまま)")
_pre.add_argument("--back-days", type=int, default=0,
                  help="--forward-days と併用。基準日より前 N 日も表示する(選定期間=in-sample "
                       "の成績を並べて崖を可視化)。例: --back-days 365 --forward-days 365 で "
                       "基準月の前後1年ずつ。基準日前は当時BTスコアが選定期間と重なる(in-sample)点に注意。")
_pre.add_argument("--fill-timing", action="store_true",
                  help="⑮約定タイミングタブを計算する。未指定でもキャッシュがあれば表示")
_pre.add_argument("--serve", action="store_true", default=True,
                  help="レポート生成後に発注サーバ(order_server.py)を起動する (既定ON)")
_pre.add_argument("--serve-execute", action="store_true", default=True,
                  help="--serve 時に実発注で起動する (既定ON)")
_pre.add_argument("--serve-prod", action="store_true", default=True,
                  help="--serve 時に本番口座(18080)で起動する (既定ON)")
_pre.add_argument("--serve-genbutsu", action="store_true",
                  help="--serve 時にロングを現物で起動する (未指定なら信用新規)")
# ── 安全解除フラグ (既定=本番自動発注ON のため、止める/デモにする手段を用意) ──
_pre.add_argument("--no-serve", dest="serve", action="store_false",
                  help="発注サーバを起動しない(レポート生成のみ)")
_pre.add_argument("--demo", action="store_true",
                  help="デモ口座(18081)・実発注で起動 (本番=18080 を回避)")
_pre.add_argument("--dry", action="store_true",
                  help="発注サーバを dry-run で起動 (実発注しない)")
_pre.add_argument("--no-minute-update", action="store_true",
                  help="5分足の自動更新(1日1回)を行わない")
_args, _ = _pre.parse_known_args()
# --demo / --dry の反映
if getattr(_args, "demo", False):
    _args.serve_prod = False
if getattr(_args, "dry", False):
    _args.serve_execute = False


def _maybe_serve_orders():
    """--serve 指定時、レポート生成後に発注サーバを前面で起動する。
    Ctrl+C で停止するまでブロックする。"""
    if not getattr(_args, "serve", False):
        return
    import subprocess as _sp2
    from pathlib import Path as _P2
    _srv = _P2(__file__).resolve().parent / "order_server.py"
    _cmd = [sys.executable, str(_srv)]
    if _args.serve_execute:
        _cmd.append("--execute")
    if _args.serve_prod:
        _cmd.append("--prod")
    if _args.serve_genbutsu:
        _cmd.append("--genbutsu")
    _is_prod_live = _args.serve_execute and _args.serve_prod
    print("\n" + "=" * 65)
    if _is_prod_live:
        print("⚠⚠⚠  本番口座(18080)・実発注モードで発注サーバを起動します  ⚠⚠⚠")
        print("⚠  接続時に本番の自動発注(利確補完など)が走ります。誤発注に注意。")
        print("⚠  止める: --no-serve / デモ: --demo / 実発注しない: --dry")
    else:
        _lbl = ("デモ(18081)" if not _args.serve_prod else "本番(18080)")
        _ex = "実発注" if _args.serve_execute else "dry-run(発注なし)"
        print(f"発注サーバを起動します（{_lbl} / {_ex}）")
    print("  " + " ".join(_cmd))
    print("  停止するには Ctrl+C")
    print("=" * 65)
    try:
        _sp2.run(_cmd)
    except KeyboardInterrupt:
        pass


def _auto_update_minute():
    """stock_5min の5分足を最新化する(1日1回・自動・非致命)。

    J-Quants サブスク終了に備え、yfinance で「既存より後のバーだけ」安全に追記する
    daily_fetch_minute.run_update を使う(時刻を保持し、既存の完璧データは書き換えない)。
    ランキング検証(backtest_intraday_ranking)が最新5分足を使えるよう、日次コマンドの
    中で stock_5min 全銘柄を自動更新する。--no-minute-update で無効化。"""
    _mk = Path(".holdout_bt_cache")
    _mk.mkdir(exist_ok=True)
    _flag = _mk / f".minute_updated_{_report_date()}"
    if _flag.exists():
        return                                  # 本日更新済み → スキップ
    try:
        from daily_fetch_minute import run_update
    except Exception as _e:
        print(f"  ⚠ 5分足自動更新スキップ (import失敗: {_e})", flush=True)
        return
    print("=" * 65)
    print("5分足(stock_5min)を自動更新中 (yfinance・本日初回のみ)...", flush=True)
    print("=" * 65)
    try:
        _res = run_update(source="yfinance", verbose=True)
        _flag.write_text("ok", encoding="utf-8")   # 成功時のみフラグ
        print(f"  5分足更新完了: +{_res.get('bars', 0):,}バー / "
              f"{_res.get('updated', 0)}銘柄", flush=True)
    except Exception as _e:
        print(f"  ⚠ 5分足自動更新スキップ ({_e})", flush=True)


# 日次コマンドの一部として5分足を自動更新(1日1回)。--no-minute-update で無効化。
if not getattr(_args, "no_minute_update", False):
    _auto_update_minute()


def _data_freshness_line() -> str:
    """レポートが「今使える最新の確定データ」で作られたかを1行に要約する。
    実行の最後に表示し、鮮度を消極的判定(=警告が出ない)ではなく明示的に伝える。
    高流動の基準銘柄(トヨタ/ソニー/SBG)の日足最新バーと、期待される最新確定バー
    (_expected_latest_bar_date: 平日15:40以降=当日)を突き合わせる。"""
    if getattr(_args, "date", None):
        return f"📅 データ基準: 指定日 {_args.date} で生成(過去再現モード)"
    try:
        from datetime import date as _dcls
        from backtest_limit_entry import (fetch as _rf,
                                          _expected_latest_bar_date as _expf)
        exp = _expf()
        act = None
        for _s in ("7203.T", "6758.T", "9984.T"):   # トヨタ/ソニー/SBG(高流動・欠損稀)
            try:
                _d = _rf(_s, 30)
                if _d is not None and len(_d) > 0:
                    _lb = _d.index[-1]
                    act = _lb.date() if hasattr(_lb, "date") else _lb
                    break
            except Exception:
                continue
        if act is None:
            return "ℹ データ鮮度: 基準銘柄を取得できず未確認(ネットワーク?)"
        if isinstance(exp, _dcls) and act < exp:
            return (f"⚠ データ未確定で生成: 日足最新バー {act} < 期待 {exp} "
                    f"(引け直後で当日バー未配信の可能性 → 引け後しばらくして再実行推奨)")
        return f"✅ 最新データで生成: 日足最新バー {act}(期待どおり最新の確定データを使用)"
    except Exception as _e:
        return f"ℹ データ鮮度: 確認スキップ({_e})"


# ── --both モード: ロング+ショートを統合HTMLに ───────────────────────────────
if _args.both and not _args.short:
    import subprocess as _sp
    _bd   = _args.date or str(_report_date())
    # --output-suffix(例 _base2024-06)を統合HTMLと各方向HTMLの両方に波及させて、
    # 基準月スイープで4つのレポートが上書きされないようにする(空なら従来どおり)。
    _BASE_SUFFIX = _args.output_suffix or ""
    _bout = Path(f"signals_holdout_all_both_{_bd}{_BASE_SUFFIX}.html")

    if _bout.exists() and not _args.force:
        print(f"[CACHE] 当日生成済み(both): {_bout.resolve()}")
        print(f"        再生成するには --force を付けてください。")
        if not _args.no_browser:
            from _open_html import open_html
            open_html(_bout.resolve())
        _maybe_serve_orders()
        sys.exit(0)

    # 株価範囲リストを構築
    _min_p_val = _args.min_price if _args.min_price and _args.min_price > 0 else 0.0
    if _args.price_ranges:
        _price_list = [int(x.strip()) for x in _args.price_ranges.split(",") if x.strip()]
    else:
        _price_list = [int(_args.max_price) if _args.max_price < 100000 else 10000]
    _multi_price = len(_price_list) > 1

    # --both / --short / --no-browser / --price-ranges / --output-suffix は渡さず、
    # --max-holds / --force はサブプロセスに伝播させる
    # --no-analysis は各方向で個別に付け直す(ロング/ショート=省略で高速 /
    # ロングミラー・ロング銘柄ショート=フル実行で同日TP/SL最適化タブを出す)ため除外。
    _base_cargs = [a for a in sys.argv[1:]
                   if a not in ("--both", "--short", "--mirror", "--long-stop-short",
                                "--no-analysis", "--no-mirror", "--no-short",
                                "--no-browser", "--price-ranges", "--output-suffix",
                                "--serve", "--serve-execute", "--serve-prod", "--serve-genbutsu")
                   and not a.startswith("--price-ranges=")
                   and not a.startswith("--output-suffix=")]
    # --max-price も除去（後で各ループで付け直す）
    _base_cargs_no_price = []
    _skip_next = False
    for _a in _base_cargs:
        if _skip_next:
            _skip_next = False
            continue
        # --max-price は後で付け直す。--lss-proposal/--long-base は各方向にだけ付けるので共通からは除く
        if _a in ("--max-price", "--lss-proposal", "--long-base"):
            _skip_next = True
            continue
        if (_a.startswith("--max-price=") or _a.startswith("--lss-proposal=")
                or _a.startswith("--long-base=")):
            continue
        _base_cargs_no_price.append(_a)
    if "--force" not in _base_cargs_no_price:
        _base_cargs_no_price.append("--force")
    _base_cargs_no_price.append("--no-browser")
    # serve は既定ONのため、サブプロセスでは必ず無効化(発注サーバは親が最後に1回だけ起動)
    _base_cargs_no_price.append("--no-serve")

    # 各価格範囲 × ロング/ショートを生成（逐次実行でメモリ使用を抑える）
    # --max-holds はサブプロセスに渡り、損益タブ内の比較セクションとして生成される
    _generated: dict = {}  # (direction, max_p) -> Path
    # 生成する方向: (dir_key, ラベル, 追加引数, 出力ファイル接頭辞)
    # ロング/ショートは --no-analysis で高速化(詳細分析タブ群を省略)。
    # ロングミラー/ロング銘柄ショートはフル実行(→「同日TP/SL最適化」タブが出る)。
    # lss専用選定ファイル(scan_lss_universe の提案)を解決。'auto'=最新を自動検出。
    # 指定時は lss を「新選定・取引明細メインの軽量タブ」で生成する(既存lssを置き換え)。
    _lss_proposal_path = None
    _lp_arg = getattr(_args, "lss_proposal", None)
    if _lp_arg:
        if _lp_arg == "auto":
            _cands = sorted(Path(".").glob("lss_watchlist_proposal_*.py"))
            _lss_proposal_path = str(_cands[-1]) if _cands else None
            if _lss_proposal_path:
                print(f"[lss] 提案ファイル自動検出: {_lss_proposal_path}")
            else:
                print("[lss] lss_watchlist_proposal_*.py が見つからず → 既存WATCHLISTでlss生成")
        else:
            _lss_proposal_path = _lp_arg
    _lss_dargs = ["--long-stop-short"]
    if _lss_proposal_path:
        _lss_dargs += ["--lss-proposal", _lss_proposal_path]

    # long を指定as-of(例2025-12-31)のWFスキャンCSVで単一選定する場合、その方向にだけ付与
    _long_base = getattr(_args, "long_base", None)
    _long_dargs = ["--no-analysis"]
    _short_dargs = ["--short", "--no-analysis"]
    if _long_base:
        _long_dargs += ["--long-base", _long_base]
        _short_dargs += ["--long-base", _long_base]   # short も同じas-of(6月基準)に揃える
        print(f"[long/short] 単一as-of選定: base={_long_base}")

    # スキップ指定(--no-mirror / --no-short)を除いた生成対象。longは常に生成。
    _skip_dirs = set()
    if getattr(_args, "no_mirror", False):
        _skip_dirs.add("mirror")
    if getattr(_args, "no_short", False):
        _skip_dirs.add("short")
    if getattr(_args, "no_long", False):
        _skip_dirs.add("long")
    _DIRECTIONS = [d for d in [
        ("long",   "ロング",             _long_dargs,                    "signals_holdout_all"),
        ("short",  "ショート",           _short_dargs,                   "signals_holdout_all_short"),
        ("mirror", "ロングミラー",       ["--mirror"],                   "signals_holdout_all_mirror"),
        ("lss",    "ロング銘柄ショート", _lss_dargs,                     "signals_holdout_all_lss"),
    ] if d[0] not in _skip_dirs]
    # 最初に開く方向タブ(--default-tab)。生成されない方向を指定したら、生成される
    # 先頭の方向にフォールバック(--no-long 等で long 自体を省くケースに対応)。
    _default_dir = getattr(_args, "default_tab", "long")
    _avail_dirs = [d[0] for d in _DIRECTIONS]
    if _default_dir not in _avail_dirs:
        _default_dir = _avail_dirs[0] if _avail_dirs else "long"
    if _skip_dirs:
        print(f"[both] スキップ: {', '.join(sorted(_skip_dirs))}")
    for _mp in _price_list:
        _mp_suffix = f"_p{_mp}" if _multi_price else ""
        # _mp==0 は「価格無制限」。サブプロセスには十分大きい上限を渡す。
        _mp_cap = 1_000_000 if _mp == 0 else _mp
        _cargs_mp = _base_cargs_no_price + ["--max-price", str(_mp_cap),
                                            "--output-suffix", _mp_suffix + _BASE_SUFFIX]
        for _dk, _dlabel, _dargs, _dprefix in _DIRECTIONS:
            _df = Path(f"{_dprefix}_{_bd}{_mp_suffix}{_BASE_SUFFIX}.html")
            print("=" * 65)
            print(f"=== {_dlabel}シグナル生成中 ({'価格無制限' if _mp == 0 else f'〜{_mp:,}円'}) ===")
            print("=" * 65)
            _sp.run([sys.executable, __file__] + _cargs_mp + _dargs)
            if not _df.exists():
                print(f"[ERROR] {_dlabel}HTMLの生成に失敗しました (max-price={_mp})")
                sys.exit(1)
            _generated[(_dk, _mp)] = _df

    # ── 最初の価格範囲をデフォルト表示 ──────────────────────────────────────
    _first_mp   = _price_list[0]
    _min_p_disp = int(_min_p_val) if _min_p_val > 0 else None

    def _price_label(mp: int) -> str:
        if mp == 0 or mp >= 100000:
            return (f"{_min_p_disp:,}〜無制限" if _min_p_disp else "無制限")
        parts = []
        if _min_p_disp:
            parts.append(f"{_min_p_disp:,}〜")
        parts.append(f"{mp:,}円")
        return "".join(parts)

    # ── ナビゲーションHTML生成 ────────────────────────────────────────────
    _nav_btns = ""
    _frames   = ""

    # ロング/ショート/ロングミラー/ロング銘柄ショート ボタン(スキップ分は出さない)
    for _dir, _lbl_prefix, _dir_cls in [
        ("long",   "📈 ロング",           "lb"),
        ("short",  "📉 ショート",         "sb"),
        ("mirror", "🪞 ロングミラー",     "mb"),
        ("lss",    "🔻 ロング銘柄ショート", "xb"),
    ]:
        if _dir in _skip_dirs:
            continue
        _is_active = _dir == _default_dir
        _nav_btns += (
            f'  <button class="ls-btn {_dir_cls}{" active" if _is_active else ""}" '
            f'onclick="switchLs(\'{_dir}\')">{_lbl_prefix}</button>\n'
        )

    # 価格帯ボタン（複数の場合のみ、セパレータを挟んで同じ行に追加）
    if _multi_price:
        _nav_btns += '  <span class="nav-sep"></span>\n'
        for _i, _mp in enumerate(_price_list):
            _nav_btns += (
                f'  <button class="pr-btn{" active" if _i==0 else ""}" data-price="{_mp}" '
                f'onclick="switchPr({_mp})">{_price_label(_mp)}</button>\n'
            )
    else:
        _nav_btns += (
            f'  <span style="font-size:0.68rem;font-weight:600;color:#fbbf24;'
            f'background:#292418;border:1px solid #854d0e;border-radius:4px;'
            f'padding:1px 6px;margin-left:8px;align-self:center">'
            f'株価 {_price_label(_price_list[0])}</span>\n'
        )

    # 📌 保有タブ（実際に約定した建玉。close_stop_guard --holdings-html が生成）
    _nav_btns += '  <span class="nav-sep"></span>\n'
    _nav_btns += '  <button class="hold-btn" onclick="switchHoldings(event)">📌 保有</button>\n'
    # 🔻 監視に切替 / 🚀 発注に切替: 発注サーバ⇄watcher の双方向トグル(kabuトークンを渡す)。
    _nav_btns += ('  <button class="handoff-btn" onclick="handoffWatcher()" '
                  'title="発注サーバを停止して lss_exit_watcher --execute --prod を起動します">'
                  '🔻 監視に切替</button>\n')
    _nav_btns += ('  <button class="handoff-btn handoff-order" onclick="handoffOrder()" '
                  'title="watcherを停止して発注サーバ(order_server --execute)を起動します">'
                  '🚀 発注に切替</button>\n')
    _holdings_path = Path("holdings_latest.html")
    if not _holdings_path.exists():
        _holdings_path.write_text(
            "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
            "<style>body{background:#0f172a;color:#94a3b8;font-family:sans-serif;padding:30px}"
            "h2{color:#e2e8f0}pre{color:#fbbf24;background:#1e293b;padding:12px;border-radius:8px}</style>"
            "</head><body><h2>📌 保有銘柄</h2>"
            "<p>まだ約定確認をしていません。引け後に次を実行すると、実際に約定した保有銘柄と"
            "タイムカット日が表示されます（このタブを再読み込み）:</p>"
            "<pre>python close_stop_guard.py --holdings-html --kabu --prod</pre>"
            "</body></html>", encoding="utf-8")

    import time as _time_mod
    _cache_bust = int(_time_mod.time())
    for _dir in ("long", "short", "mirror", "lss"):
        if _dir in _skip_dirs:
            continue
        for _i, _mp in enumerate(_price_list):
            _frame_id = f"ls-{_dir}-{_mp}"
            _active_fr = " active" if _dir == _default_dir and _i == 0 else ""
            _src = _generated[(_dir, _mp)].name
            # 遅延ロード: 全iframeを data-src(素のファイル名)にし、表示時にJSが
            # ?v=Date.now() を付けて読み込む。file:// はクエリを無視して古いiframeを
            # キャッシュするため、生成時刻の固定トークンでは reload/複製で古い版が出る。
            # ロード毎に一意トークンにすることで必ず最新のhtmlを読む。
            _frames += f'<iframe id="{_frame_id}" class="ls-frame{_active_fr}" data-src="{_src}"></iframe>\n'
    _frames += '<iframe id="holdings-frame" class="hold-frame" data-src="holdings_latest.html"></iframe>\n'

    _bout.write_text(f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ホールドアウト全設定 ロング+ショート統合 {_bd} [保有{__import__('backtest_limit_entry').MAX_HOLD}日]</title>
<style>
body{{margin:0;padding:0;background:#0f172a;font-family:sans-serif}}
.ls-nav{{display:flex;gap:0;align-items:flex-end;border-bottom:2px solid #1e293b;background:#0f172a;
  position:sticky;top:0;z-index:9999;padding:8px 16px 0}}
.ls-btn{{padding:11px 28px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:1.05rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.ls-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.ls-btn.active.lb{{color:#34d399;border-bottom:2px solid #34d399;background:#0f172a}}
.ls-btn.active.sb{{color:#f87171;border-bottom:2px solid #f87171;background:#0f172a}}
.ls-btn.active.mb{{color:#fbbf24;border-bottom:2px solid #fbbf24;background:#0f172a}}
.ls-btn.active.xb{{color:#a78bfa;border-bottom:2px solid #a78bfa;background:#0f172a}}
.pr-btn{{padding:9px 20px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:0.92rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.pr-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.pr-btn.active{{background:#0f172a;border-bottom:2px solid #fbbf24;color:#fbbf24}}
.nav-sep{{width:1px;background:#334155;margin:8px 8px 4px;align-self:stretch}}
.hold-btn{{padding:9px 20px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:0.92rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.hold-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.hold-btn.active{{background:#0f172a;border-bottom:2px solid #60a5fa;color:#60a5fa}}
.handoff-btn{{padding:9px 16px;margin-left:6px;background:#3f1d2b;border:1px solid #f472b6;
  border-radius:6px;color:#f9a8d4;cursor:pointer;font-size:0.86rem;font-weight:700;
  align-self:center;transition:all .15s}}
.handoff-btn:hover:not(:disabled){{background:#4c1d3a;color:#fbcfe8}}
.handoff-btn:disabled{{opacity:.6;cursor:default}}
.handoff-order{{background:#14312b;border-color:#34d399;color:#6ee7b7}}
.handoff-order:hover:not(:disabled){{background:#12463a;color:#a7f3d0}}
.ls-frame,.hold-frame{{display:none;width:100%;border:none;height:calc(100vh - 54px)}}
.ls-frame.active,.hold-frame.active{{display:block}}
</style>
</head>
<body>
<div class="ls-nav">
{_nav_btns}</div>
{_frames}
<script>
var _curDir = '{_default_dir}';
var _curPr  = {_first_mp};
function _ensureLoaded(f) {{
  // data-src だけの iframe を初回表示時に読み込む (遅延ロード)。
  // ?v=Date.now() を毎回付けて file:// のキャッシュ(古いiframe)を確実に回避する。
  if (f && f.dataset && f.dataset.src) {{
    var s = f.dataset.src;
    f.src = s + (s.indexOf('?') >= 0 ? '&' : '?') + 'v=' + Date.now();
    f.removeAttribute('data-src');
  }}
}}
function _showFrame() {{
  document.querySelectorAll('.ls-frame,.hold-frame').forEach(f => f.classList.remove('active'));
  document.querySelectorAll('.hold-btn').forEach(b => b.classList.remove('active'));
  var f = document.getElementById('ls-' + _curDir + '-' + _curPr);
  if (f) {{ _ensureLoaded(f); f.classList.add('active'); }}
}}
function switchLs(dir) {{
  _curDir = dir;
  document.querySelectorAll('.ls-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  _showFrame();
}}
function switchPr(pr) {{
  _curPr = pr;
  document.querySelectorAll('.pr-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  _showFrame();
}}
function switchHoldings(ev) {{
  document.querySelectorAll('.ls-frame,.hold-frame').forEach(f => f.classList.remove('active'));
  document.querySelectorAll('.hold-btn').forEach(b => b.classList.remove('active'));
  ev.target.classList.add('active');
  var hf = document.getElementById('holdings-frame');
  _ensureLoaded(hf);
  hf.classList.add('active');
}}
function handoffWatcher() {{
  if (!confirm('発注サーバを停止して、監視(lss_exit_watcher --execute --prod)に切り替えます。\\n'
    + '・新しいウィンドウで watcher が起動します\\n'
    + '・発注サーバは停止します(以降このレポートの🚀発注ボタンは使えません)\\n'
    + '・出した注文は kabu 側に残ります\\n'
    + '・保有/損益は📌保有タブを再読込すれば更新されます\\n\\nよろしいですか？')) return;
  var b = document.querySelector('.handoff-btn');
  if (b) {{ b.disabled = true; b.textContent = '⏳ 切替中...'; }}
  fetch('http://127.0.0.1:8765/handoff-watcher').then(r => r.text()).then(t => {{
    alert(t);
    if (b) b.textContent = '✅ 監視に切替済み';
  }}).catch(e => {{
    alert('切替リクエスト失敗: ' + e + '\\n(発注サーバが起動していない可能性があります。'
      + '手動で lss_exit_watcher を起動してください)');
    if (b) {{ b.disabled = false; b.textContent = '🔻 監視に切替'; }}
  }});
}}
function handoffOrder() {{
  if (!confirm('監視(watcher)を停止して、発注サーバに切り替えます。\\n'
    + '・新しいウィンドウで発注サーバが起動します\\n'
    + '・watcherは停止します(日中の自動決済監視も止まります)\\n'
    + '・レポートの🚀発注ボタンが再び使えます\\n\\nよろしいですか？')) return;
  var b = document.querySelector('.handoff-order');
  if (b) {{ b.disabled = true; b.textContent = '⏳ 切替中...'; }}
  fetch('http://127.0.0.1:8766/handoff-order').then(r => r.text()).then(t => {{
    alert(t);
    if (b) b.textContent = '✅ 発注に切替済み';
  }}).catch(e => {{
    alert('切替リクエスト失敗: ' + e + '\\n(watcherが起動していない可能性があります。'
      + '発注サーバは daily で起動できます)');
    if (b) {{ b.disabled = false; b.textContent = '🚀 発注に切替'; }}
  }});
}}
// 初期アクティブ(long×先頭価格帯)を、ページ読み込み/リロードごとに最新トークンで読み込む
_showFrame();
</script>
</body>
</html>""", encoding="utf-8")
    print(f"\n統合レポート生成完了: {_bout.resolve()}")
    print(_data_freshness_line())
    if not _args.no_browser:
        from _open_html import open_html
        # file:// は同名ファイルを作り直しても、既に開いているタブを自動で読み直さない
        # (手動リロードするまで古い内容のまま)。トップURLに一意トークンを付けて
        # 「別URL＝新規ナビゲーション」にし、必ず最新をディスクから読ませる。
        open_html(_bout.resolve().as_uri() + f"?v={_cache_bust}")
    _maybe_serve_orders()
    sys.exit(0)

# ── ロング/ショートの戦略セット ──────────────────────────────────────────────
if _args.short:
    _STOP_STRATS = ["A7_S", "RSI2_S", "MACD_S"]      # ショート逆指値系
    _BRK_STRATS  = ["DON_S", "MOM_S", "GAP_S"]       # ショートBRK系
else:
    # VOL/MACD はトレンドフィルタ版(VOLTF/MACDTF, +MA50)を採用。
    # WF比較(compare_filter_variants)で全12設定 PF↑・DD↓・PnL↑ を確認済み。
    # falling knife(MA50割れ, 例:東洋エンジ)のシグナルを除外する。
    _STOP_STRATS = ["MACDTF", "A7", "RSI2"]
    _BRK_STRATS  = ["DON", "VOLTF", "MOM"]

JST   = _JST_TZ
TODAY = _report_date()

# ── 当日キャッシュ: 生成済みHTMLがあれば再計算をスキップ ──────────────────────
# 重いバックテストに入る前に、同一パラメータの出力ファイルが既に存在すれば
# それを開いて即終了する。--force で強制再生成。
_cache_date    = _args.date or str(TODAY)
if _args.mirror:
    _cache_short = "_mirror"
elif _args.long_stop_short:
    _cache_short = "_lss"
elif getattr(_args, "long_daytrade", False):
    _cache_short = "_ldt"
elif _args.short:
    _cache_short = "_short"
else:
    _cache_short = ""
# サイジング/保有日数の設定をキャッシュ名に反映 (設定を変えたら別キャッシュになる)
# これが無いと VOL_PARITY や MAX_HOLD_OVERRIDE を変えても古い結果を読んでしまう
def _settings_sig() -> str:
    parts = []
    if os.environ.get("VOL_PARITY", "0").lower() in ("1", "true", "yes", "on"):
        parts.append("vp" + os.environ.get("RISK_PER_TRADE", "20000"))
    mho = os.environ.get("MAX_HOLD_OVERRIDE")
    if mho:
        parts.append("mh" + mho)
    return ("_" + "_".join(parts)) if parts else ""
_cache_settings = _settings_sig()
_cache_short   = _cache_short + _cache_settings
_cache_symbol  = ""
if _args.symbol:
    _s = _args.symbol.upper()
    if not _s.endswith(".T"):
        _s += ".T"
    _cache_symbol = f"_{_s.replace('.', '')}"
_out_suffix_arg = _args.output_suffix if _args.output_suffix else ""
_cached_out = Path(f"signals_holdout_all{_cache_short}{_cache_symbol}_{_cache_date}{_out_suffix_arg}.html")
if _cached_out.exists() and not _args.force:
    print(f"[CACHE] 当日生成済み: {_cached_out.resolve()}")
    print(f"        再生成するには --force を付けてください。")
    if not _args.no_browser:
        from _open_html import open_html
        open_html(_cached_out.resolve())
    sys.exit(0)

_PNL_PERIODS  = [30, 60, 90, 120, 150, 180, 270, 365]
# --days-from-base: 選定基準月の翌月〜今日 を表示期間にする(基準月スイープのOOS全体)。
if getattr(_args, "days_from_base", False):
    import re as _re_dfb
    from datetime import date as _dt_date
    _bm_m = _re_dfb.search(r"base(\d{4})-(\d{2})", (_args.output_suffix or ""))
    if not _bm_m and getattr(_args, "long_base", None):
        _bm_m = _re_dfb.search(r"(\d{4})-(\d{2})", str(_args.long_base))
    if _bm_m:
        _by, _bmo = int(_bm_m.group(1)), int(_bm_m.group(2))
        # OOS開始 = 基準月の翌月1日(基準月=TRAIN終端なので、その翌月からがOOS)
        _oos_start = (_dt_date(_by + 1, 1, 1) if _bmo == 12 else _dt_date(_by, _bmo + 1, 1))
        _dfb = (TODAY - _oos_start).days
        if _dfb > 0:
            _args.days = _dfb
            print(f"[days-from-base] 表示期間を基準月{_by}-{_bmo:02d}の翌月〜今日={_dfb}日 に設定")
# --forward-days/--back-days 指定時は表示窓=基準日の前後ぶん(since=基準日-back)。それ以外は --days。
if _args.forward_days > 0 or _args.back_days > 0:
    _disp_days = _args.forward_days + _args.back_days
else:
    _disp_days = _args.days
# --days/--forward-days が既定リスト外なら、その値を期間リストに追加して初期表示にする。
if _disp_days and _disp_days not in _PNL_PERIODS:
    _PNL_PERIODS = sorted(set(_PNL_PERIODS) | {_disp_days})
_DEFAULT_DAYS = _disp_days if _disp_days in _PNL_PERIODS else 180

HOLDOUT_CONFIGS = [
    (30,  "HO30d",  "#3b82f6", "#60a5fa"),
    (60,  "HO60d",  "#06b6d4", "#67e8f9"),
    (90,  "HO90d",  "#10b981", "#6ee7b7"),
    (120, "HO120d", "#84cc16", "#bef264"),
    (150, "HO150d", "#f59e0b", "#fcd34d"),
    (180, "HO180d", "#ef4444", "#fca5a5"),
]

# ── TRADING_MODE を import 前に設定 ───────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

# ── CSV ヘルパー ──────────────────────────────────────────────────────────────
def _float(v, default=0.0) -> float:
    try:    return float(v)
    except: return default

def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))

_SCAN_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

def _csv_scan_date(p: Path) -> str:
    """CSVファイル名からスキャン日付 YYYY-MM-DD を返す(取れなければ空)。
    末尾に -15d / -7d 等の派生サフィックスが付くファイル名
    (例: walkforward_A7_holdout120d_2026-06-08-15d.csv) でも
    正しく日付部分を抽出する。複数一致時は最後(最も右)の日付を採用。"""
    m = _SCAN_DATE_RE.findall(p.stem)
    return m[-1] if m else ""


def _find_csv(strategy: str, holdout_days: int, wf_dir: Path,
              mode: str = "conservative",
              base_date: str | None = None) -> tuple[Path | None, str]:
    """holdout CSV を探す。base_date 指定時は『base_date 以後にスキャンされたCSVは
    使わない』(=未来の選定で過去検証を汚染しない)。該当が無ければ標準CSVに fallback。
    base_date=None なら従来どおり最新を採用。"""
    mode_suffix = f"_{mode}" if mode != "conservative" else ""

    def _pick(cands: list) -> Path | None:
        # cands は日付降順(新しい順)。base_date 指定時は base_date 以前のみ。
        # 日付が読めないファイルは(未来の派生CSVが漏れ込むのを防ぐため)採用しない。
        if base_date:
            ok = [c for c in cands
                  if _csv_scan_date(c) and _csv_scan_date(c) <= base_date]
            return ok[0] if ok else None
        return cands[0] if cands else None

    cands = sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_holdout{holdout_days}d_*.csv"),
                   key=lambda c: _csv_scan_date(c), reverse=True)
    hit = _pick(cands)
    if hit:
        return hit, "holdout"
    fallback = sorted([f for f in wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_*.csv")
                       if "holdout" not in f.name],
                      key=lambda c: _csv_scan_date(c), reverse=True)
    hit = _pick(fallback)
    if hit:
        return hit, "standard"
    return None, "none"

def _load_wl_from_csv(csv_path: Path, max_price: float, strategy: str,
                       per_strategy: int = 10, min_price: float = 0.0) -> list[tuple]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    filtered = [r for r in rows if (
        _float(r.get("total_test_pnl", 0)) > 0
        and _float(r.get("max_drawdown_pct", 999)) <= 15.0
        and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
        and (min_price <= 0 or _float(r.get("latest_price", 0)) >= min_price)
    )]
    filtered.sort(key=_composite_score, reverse=True)
    return [(r.get("symbol", ""), r.get("name", ""), strategy)
            for r in filtered[:per_strategy] if r.get("symbol")]


def _filter_wl_by_price(wl: list, min_price: float, max_price: float) -> list:
    """フォールバック(現行WATCHLIST)を最新終値で価格フィルタする。
    CSV経路の latest_price フィルタ相当。価格指定が無ければ素通し。
    価格取得に失敗した銘柄は安全側で除外する。"""
    _has_max = bool(max_price) and 0 < max_price < 100000
    _has_min = bool(min_price) and min_price > 0
    if not _has_max and not _has_min:
        return list(wl)
    import backtest_limit_entry as _ble
    out: list = []
    dropped = 0
    for item in wl:
        code = item[0] if item else ""
        try:
            df = _ble.fetch(code, 400)
            px = float(df["close"].iloc[-1]) if df is not None and len(df) else None
        except Exception:
            px = None
        if px is None or (_has_max and px > max_price) or (_has_min and px < min_price):
            dropped += 1
            continue
        out.append(item)
    if dropped:
        print(f"  [価格フィルタ] 現行WATCHLIST {len(wl)}→{len(out)}件 "
              f"(min={min_price:g}/max={max_price:g} で {dropped}件除外)")
    return out

# ── PNL_CONFIGS 構築 ──────────────────────────────────────────────────────────
print("=" * 65)
print(f"run_signals_holdout_all: {TODAY}")
print("=" * 65)

wf_dir = _args.wf_dir
wf_dir.mkdir(exist_ok=True)

# 選定CSVを探す基準日: --date 指定時はその日(=それ以後にスキャンしたCSVは使わない)。
# 未指定なら今日 → 既存CSVは全て今日以前なので従来どおり最新を採用(挙動不変)。
_wl_base_date = _args.date or str(TODAY)
# --long-base 指定かつ通常ロング(short/mirror/lss以外)のときは、CSV参照のas-ofだけを
# その日付に固定する。これで各ホールドアウトが walkforward_<戦略>_holdout{N}d_<base>.csv
# (例2025-12-31版)を拾う=2025-12基準で6ホールドアウト選定。表示(直近N日)は今日のまま
# =2026がOOS。lssと同じ土俵(2025-12選定→2026検証)になる。
_long_base_asof = getattr(_args, "long_base", None)
if _long_base_asof and not (_args.mirror or _args.long_stop_short):
    # long と short の両方に適用(mirror/lssは除く)。short もWFホールドアウトCSVの
    # as-ofを同じ日付に固定=long/short とも同一基準月(例2026-06)で選定。
    _wl_base_date = _long_base_asof
    print(f"  [{'short' if _args.short else 'long'}] CSV参照as-ofを {_wl_base_date} に固定"
          f"(同一基準月で6ホールドアウト選定 / 表示は今日=OOS)")
elif _args.date:
    print(f"  [選定CSV] base_date={_wl_base_date} 以前にスキャンしたCSVのみ使用"
          f"(未来の選定で汚染しない)")

# period_days → list of config dicts
_period_configs: dict[int, list[dict]] = {d: [] for d in _PNL_PERIODS}

for holdout_days, ho_label, col_con, col_agg in HOLDOUT_CONFIGS:
    for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
        mode_short = "con" if mode == "conservative" else "agg"
        stop_wl: list[tuple] = []
        brk_wl:  list[tuple] = []
        has_data = False
        for strat in _STOP_STRATS:
            p, _ = _find_csv(strat, holdout_days, wf_dir, mode, base_date=_wl_base_date)
            if p:
                stop_wl.extend(_load_wl_from_csv(p, _args.max_price, strat, min_price=_args.min_price))
                has_data = True
        for strat in _BRK_STRATS:
            p, _ = _find_csv(strat, holdout_days, wf_dir, mode, base_date=_wl_base_date)
            if p:
                brk_wl.extend(_load_wl_from_csv(p, _args.max_price, strat, min_price=_args.min_price))
                has_data = True
        if stop_wl or brk_wl:   # 実際にアイテムがある場合のみ登録
            _period_configs[holdout_days].append({
                "label": f"{ho_label}/{mode_short}",
                "color": color,
                "mode":  mode,
                "sm_tm": None,
                "stop_wl": stop_wl,
                "brk_wl":  brk_wl,
            })

# フォールバック: CSV なし → 現行 WATCHLIST を全期間で共通使用
import check_signals_stop     as _stop
import check_signals_breakout as _brk
if _args.short:
    import check_signals_short          as _fb_stop_mod
    import check_signals_short_breakout as _fb_brk_mod
else:
    _fb_stop_mod = _stop
    _fb_brk_mod  = _brk

_using_fallback = all(len(v) == 0 for v in _period_configs.values())
if _using_fallback:
    print("[INFO] WFスキャンCSVなし → 現行WATCHLISTを全期間で使用")
    _fb_cfg = {
        "label":   "現行WL con",
        "color":   "#3b82f6",
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": _filter_wl_by_price(list(_fb_stop_mod.WATCHLIST),
                                       _args.min_price, _args.max_price),
        "brk_wl":  _filter_wl_by_price(list(_fb_brk_mod.WATCHLIST),
                                       _args.min_price, _args.max_price),
    }
    for days in _PNL_PERIODS:
        _period_configs[days] = [_fb_cfg]

# ── lss専用選定(scan_lss_universe の提案)で上書き ──────────────────────────
# --lss-proposal 指定時は WF選定/フォールバックを捨て、提案 SELECTED を全設定共通の
# 単一設定として使う(既存lssタブを置き換え=取引明細メインの軽量lss)。フラグ未指定なら
# この節は完全にスキップされ、既存挙動は一切変わらない。
_lss_proposal_file = getattr(_args, "lss_proposal", None)
_lss_source_bases: dict = {}   # merge時の (code,strat)->基準月ラベル。import後に _na へ流す
_lss_start_dates: dict = {}    # merge時の (code,strat)->OOS開始日。最新基準月のみ銘柄の遡及防止。
if _args.long_stop_short and _lss_proposal_file:
    _CANON_STOP = {"MACDTF", "A7", "RSI2"}
    _CANON_BRK  = {"DON", "VOLTF", "MOM"}
    _lss_exclude = {s.strip().upper() for s in os.environ.get("LSS_EXCLUDE_STRATS", "").split(",") if s.strip()}
    if _lss_exclude:
        _CANON_STOP -= _lss_exclude
        _CANON_BRK  -= _lss_exclude
        print(f"[lss] 戦略除外(LSS_EXCLUDE_STRATS): {_lss_exclude}")
    _sel = []
    try:
        _pns: dict = {}
        exec(Path(_lss_proposal_file).read_text(encoding="utf-8"), _pns)
        _sel = list(_pns.get("SELECTED", []))
        _lss_source_bases = dict(_pns.get("SOURCE_BASES") or {})
        _lss_start_dates = dict(_pns.get("START_DATES") or {})
    except Exception as _e:
        print(f"[lss] 提案ファイル読み込み失敗 {_lss_proposal_file}: {_e} → 既存選定のまま")
    if _sel:
        # 提案はTRAIN期待値の高い順に整列済み。その順序を保ったまま:
        #   1) レポート対応6戦略(MACDTF/A7/RSI2/DON/VOLTF/MOM)だけ残す(MACD/VOL除外)
        #   2) 価格フィルタ(この価格タブの max_price。無制限タブは素通し)を先に適用
        #      → 値がさが上位に偏るので、先に価格で絞らないと予算タブが空になる
        #   3) その上で上位N本(--lss-top)だけ採用
        # 空売り不可(非貸借/取扱なし)銘柄を自動除外する。check_shortable.py が
        # kabu の MarginSell 照会で作る not_shortable.py(NOT_SHORTABLE=[...])を読む。
        # ファイルが無ければ除外しない(後方互換)。月1で check_shortable を回して更新する。
        _not_short: set = set()
        try:
            _nsp = Path(__file__).resolve().parent / "not_shortable.py"
            if _nsp.exists():
                _nsns: dict = {}
                exec(_nsp.read_text(encoding="utf-8"), _nsns)
                _not_short = {str(x).upper().removesuffix(".T").split(".")[0]
                              for x in _nsns.get("NOT_SHORTABLE", [])}
        except Exception as _e:
            print(f"[lss] not_shortable.py 読み込み失敗: {_e} → 除外なしで続行")

        _canon, _dropped, _ns_dropped = [], 0, 0
        for _tup in _sel:
            if not _tup or len(_tup) < 3:
                continue
            _c, _n, _s = _tup[0], _tup[1], _tup[2]
            if str(_c).upper().removesuffix(".T").split(".")[0] in _not_short:
                _ns_dropped += 1
                continue   # 空売り不可 → lss から自動除外
            if _s in _CANON_STOP or _s in _CANON_BRK:
                _canon.append((_c, _n, _s))
            else:
                _dropped += 1
        _canon = _filter_wl_by_price(_canon, _args.min_price, _args.max_price)
        _lss_top = getattr(_args, "lss_top", 0) or 0
        if _lss_top > 0:
            _canon = _canon[:_lss_top]
        _p_stop = [t for t in _canon if t[2] in _CANON_STOP]
        _p_brk  = [t for t in _canon if t[2] in _CANON_BRK]
        _lss_cfg = {
            "label": "lss選定", "color": "#f472b6", "mode": "conservative",
            "sm_tm": None, "stop_wl": _p_stop, "brk_wl": _p_brk,
        }
        for days in _PNL_PERIODS:
            _period_configs[days] = [_lss_cfg]
        print(f"[lss] 新選定で上書き: {Path(_lss_proposal_file).name} → "
              f"価格≤{_args.max_price:,.0f}円で {len(_canon)}ペア"
              + (f"(上位{_lss_top}) " if _lss_top > 0 else " ")
              + f"[stop {len(_p_stop)}/brk {len(_p_brk)}]"
              + (f" 非対応{_dropped}除外" if _dropped else "")
              + (f" 空売不可{_ns_dropped}除外" if _ns_dropped else ""))

# ── ロングデイトレ専用選定(scan_long_daytrade の提案)で上書き ────────────────
# --long-daytrade + --ldt-proposal 指定時は WF選定/フォールバックを捨て、提案 SELECTED を
# 全設定共通の単一設定として使う(lss の --lss-proposal と同じ経路の鏡像)。ロングなので
# 空売り不可(not_shortable)の除外は行わない。
_ldt_proposal_file = None
if getattr(_args, "long_daytrade", False):
    _ldt_arg = getattr(_args, "ldt_proposal", None)
    if _ldt_arg == "auto":
        _cands = sorted(Path(".").glob("long_watchlist_proposal_*.py"))
        _ldt_proposal_file = str(_cands[-1]) if _cands else None
        if _ldt_proposal_file:
            print(f"[ldt] 提案ファイル自動検出: {_ldt_proposal_file}")
        else:
            print("[ldt] long_watchlist_proposal_*.py が見つからず → 既存WATCHLISTでロングデイトレ生成")
    elif _ldt_arg:
        _ldt_proposal_file = _ldt_arg
if getattr(_args, "long_daytrade", False) and _ldt_proposal_file:
    _CANON_STOP = {"MACDTF", "A7", "RSI2"}
    _CANON_BRK  = {"DON", "VOLTF", "MOM"}
    _sel = []
    try:
        _pns: dict = {}
        exec(Path(_ldt_proposal_file).read_text(encoding="utf-8"), _pns)
        _sel = list(_pns.get("SELECTED", []))
        _lss_source_bases = dict(_pns.get("SOURCE_BASES") or {})
        _lss_start_dates = dict(_pns.get("START_DATES") or {})
    except Exception as _e:
        print(f"[ldt] 提案ファイル読み込み失敗 {_ldt_proposal_file}: {_e} → 既存選定のまま")
    if _sel:
        _canon, _dropped = [], 0
        for _tup in _sel:
            if not _tup or len(_tup) < 3:
                continue
            _c, _n, _s = _tup[0], _tup[1], _tup[2]
            if _s in _CANON_STOP or _s in _CANON_BRK:
                _canon.append((_c, _n, _s))
            else:
                _dropped += 1
        _canon = _filter_wl_by_price(_canon, _args.min_price, _args.max_price)
        _ldt_top = getattr(_args, "lss_top", 0) or 0
        if _ldt_top > 0:
            _canon = _canon[:_ldt_top]
        _p_stop = [t for t in _canon if t[2] in _CANON_STOP]
        _p_brk  = [t for t in _canon if t[2] in _CANON_BRK]
        _ldt_cfg = {
            "label": "ロングデイトレ選定", "color": "#34d399", "mode": "conservative",
            "sm_tm": None, "stop_wl": _p_stop, "brk_wl": _p_brk,
        }
        for days in _PNL_PERIODS:
            _period_configs[days] = [_ldt_cfg]
        print(f"[ldt] 新選定で上書き: {Path(_ldt_proposal_file).name} → "
              f"価格≤{_args.max_price:,.0f}円で {len(_canon)}ペア"
              + (f"(上位{_ldt_top}) " if _ldt_top > 0 else " ")
              + f"[stop {len(_p_stop)}/brk {len(_p_brk)}]"
              + (f" 非対応{_dropped}除外" if _dropped else ""))

# シグナルタブ用: 全設定を重複なしで結合
_seen_cfg_labels: set = set()
_all_configs: list[dict] = []
for cfgs in _period_configs.values():
    for cfg in cfgs:
        if cfg["label"] not in _seen_cfg_labels:
            _seen_cfg_labels.add(cfg["label"])
            _all_configs.append(cfg)

n_items_total = sum(len(c["stop_wl"]) + len(c["brk_wl"]) for c in _all_configs)
print(f"設定数: {len(_all_configs)}件 / アイテム合計: {n_items_total}件")

# ── holdout選定銘柄をエクスポート (デイトレ5分足の同日ショート検証用) ──────────
# 全設定の (銘柄 × 戦略) を重複なしで holdout_selected_symbols.py に出力。
# con/agg はエントリー(em=0)が同一なので (code, strat) で重複排除。
# デイトレ側 verify_sameday_short_intraday.py --symbols-file で読み込む。
try:
    _sel_seen: set = set()
    _sel: list[tuple] = []
    for _c in _all_configs:
        for _code, _name, _strat in list(_c["stop_wl"]) + list(_c["brk_wl"]):
            _k = (_code, _strat)
            if _code and _k not in _sel_seen:
                _sel_seen.add(_k)
                _sel.append((_code, _name, _strat))
    _sel_suffix = "_short" if _args.short else ""
    _sel_path = Path(f"holdout_selected_symbols{_sel_suffix}.py")
    _sel_lines = [
        '"""holdout_selected_symbols.py — run_signals_holdout_all が選定した',
        f'(銘柄, 名前, 戦略) の重複なし一覧。{TODAY} 自動生成。',
        'デイトレ5分足の同日ショート検証用:',
        '  verify_sameday_short_intraday.py --symbols-file holdout_selected_symbols.py',
        '"""',
        "SELECTED = [",
    ]
    for _code, _name, _strat in _sel:
        _sel_lines.append(f"    ({_code!r}, {_name!r}, {_strat!r}),")
    _sel_lines.append("]")
    _sel_path.write_text("\n".join(_sel_lines), encoding="utf-8")
    print(f"[export] holdout選定 {len(_sel)}ペア → {_sel_path}")
except Exception as _e:
    print(f"[export] holdout_selected_symbols 出力失敗: {_e}")

# ── ローリング逆指値: バックテストエンジンにモジュール定数を注入 ──────────────
import backtest_limit_entry as _bte
if _args.rolling > 0:
    _bte.ROLLING_ENTRY = _args.rolling
    print(f"[ROLLING] ローリング逆指値 有効: 最大{_args.rolling}回更新")

# ── ロングミラー / ロング銘柄ショート の評価フックを注入 ─────────────────────
# どちらもロング銘柄・ロング戦略・ロングWATCHLISTをそのまま使い、約定エンジンの
# 挙動だけ差し替える(唯一の約定ロジック run_limit_backtest に効くので全タブへ波及)。
if _args.mirror:
    _bte._MIRROR_PNL = True      # 損益を符号反転(指値空売りの鏡写し)
    _bte._MAX_HOLD_FORCE = 0     # 同日決済(日計り)。環境変数と違いキャッシュ名を汚さない
    print("[MIRROR] ロングミラー(指値空売り・同日決済): 全トレードpnl反転 / max_hold=0")
if _args.long_stop_short:
    _bte._ENTRY_TYPE_FORCE = "stop_sell"   # ロング銘柄を逆指値の空売りで評価
    _bte._MAX_HOLD_FORCE = 0     # 同日決済(日計り)。ミラーと揃える
    print("[LSS] ロング銘柄を逆指値空売り(stop_sell)・同日決済で評価(下ブレイク継続)")
if getattr(_args, "long_daytrade", False):
    # ロングデイトレ: 逆指値買い(上ブレイク継続)を本物のロングで同日決済。lssの鏡像。
    _bte._ENTRY_TYPE_FORCE = "stop"   # 逆指値買い(前日終値+atr*em, em=0→前日終値)
    _bte._MAX_HOLD_FORCE = 0          # 同日決済(日計り)
    _bte._LONG_DAYTRADE = True        # 5分足同日決済ブロックを long_* 関数で評価(pnl反転なし)
    print("[LDT] ロングデイトレ(逆指値買い=上ブレイク)・同日決済で評価(本物のロング)")
# 同日TP/SL最適幅の適用(mirror/lss のみ)。損益/取引明細/月別すべてがこの幅で計算される。
# ※同日スイープ自体は build_sameday_tpsl_sweep_html 内で一時的にこの上書きを外すので、
#   グリッドは常に本来の総当り結果を示す(適用値に引きずられない)。
if (_args.mirror or _args.long_stop_short or getattr(_args, "long_daytrade", False)):
    # 5分足スイープで確定した最適な同日TP/SL幅(モード別)をデフォルトにする。
    # --mirror-sm / --mirror-tm を明示指定すれば上書き。
    #   ミラー(指値空売り): 損切ATR0.5 / 利確ATR0.1 (long基準。5分足★最良)
    #   ロング銘柄ショート(逆指値空売り): 損切ATR0.1 / 利確ATR1.0 (short基準。5分足★最良)
    #   ロングデイトレ(逆指値買い): 損切ATR0.1 / 利確ATR1.0 (lssの鏡像。要フォワード検証)
    _MIRROR_OPT = (0.5, 0.1)
    _LSS_OPT    = (0.1, 1.0)
    _LDT_OPT    = (0.1, 1.0)
    if _args.mirror:
        _def_sm, _def_tm = _MIRROR_OPT
    elif _args.long_stop_short:
        _def_sm, _def_tm = _LSS_OPT
    else:
        _def_sm, _def_tm = _LDT_OPT
    _bte._SM_FORCE = _args.mirror_sm if _args.mirror_sm is not None else _def_sm
    _bte._TM_FORCE = _args.mirror_tm if _args.mirror_tm is not None else _def_tm
    _sm_src = "指定" if _args.mirror_sm is not None else "5分足最適(既定)"
    _tm_src = "指定" if _args.mirror_tm is not None else "5分足最適(既定)"
    print(f"[TP/SL適用] 損切ATR={_bte._SM_FORCE}({_sm_src}) / "
          f"利確ATR={_bte._TM_FORCE}({_tm_src}) を全体に適用", flush=True)
    # mirror/lss の取引明細・KPI・月別を5分足ベースで集計する(--no-5m で無効化)。
    if not getattr(_args, "no_5m", False):
        try:
            import daytrade_data as _dtd0
            _bte._INTRADAY_5M_SOURCE = "local" if (
                _dtd0.DATA_DIR.exists() and any(_dtd0.DATA_DIR.glob("*.pkl"))) else "auto"
        except Exception:
            _bte._INTRADAY_5M_SOURCE = "auto"
        _bte._INTRADAY_5M = True
        # 約定モデルの切替(A/B用): LSS_REALISTIC_ENTRY=0 で旧・楽観モデル(常にトリガー約定)。
        # 既定(未設定/1)は現実的モデル(min(トリガー,始値)+-3%ガード)。約定値の寄与を測る用。
        import os as _os_re
        if _os_re.environ.get("LSS_REALISTIC_ENTRY") == "0":
            _bte._INTRADAY_5M_REALISTIC_ENTRY = False
            print("[約定モデル] 現実的約定=OFF: 常にトリガー約定(旧・楽観モデル)で計算します", flush=True)
        # 約定遅延(検証用): LSS_ENTRY_DELAY_BARS=N で寄りからN本(=N×5分)待ってから約定。
        # 400万タブ含む全タブに反映される。既定0=即約定(通常運用)。
        try:
            _edly = int(_os_re.environ.get("LSS_ENTRY_DELAY_BARS", "0") or "0")
        except Exception:
            _edly = 0
        if _edly > 0:
            _bte._INTRADAY_5M_ENTRY_DELAY = _edly
            print(f"[約定遅延] 寄りから{_edly}本(={_edly*5}分)待ってから約定で計算します", flush=True)
        # 5分足ロード日数を表示窓に合わせる(既定400=ライブ)。基準月スイープ等で表示窓が
        # 長いと、5分足を400日しか読まず古い月がlssから欠落するため、窓+60日に拡張する。
        _bte._INTRADAY_5M_DAYS = max(400, _disp_days + 60)
        print(f"[5分足集計] mirror/lss の同日決済を5分足で集計(ソース={_bte._INTRADAY_5M_SOURCE} / "
              f"5分足ロード{_bte._INTRADAY_5M_DAYS}日)。5分足の無い取引は除外されます。", flush=True)

# mirror/lss は「検証専用」モード: 実発注用の signals_latest.json や共有スコア
# キャッシュを上書きしない(発注サーバが誤モードのシグナルを掴むのを防ぐ)。
_analysis_only = bool(_args.mirror or _args.long_stop_short
                      or getattr(_args, "long_daytrade", False))

class _AnalysisOnlySkip(Exception):
    """分析専用モードで JSON/キャッシュ書き出しを飛ばすための番兵。"""

# ── nikkei_analysis をインポートして PNL_CONFIGS を注入 ───────────────────────
# nikkei_analysis は import 時に _stop/_brk を reload する。
# WATCHLIST が上書きされないよう、reload 後に元に戻す。
_orig_stop_wl = list(_stop.WATCHLIST)
_orig_brk_wl  = list(_brk.WATCHLIST)

import nikkei_analysis as _na

# proposalマージ時の選定基準月ラベルをレポートへ流す(かぶり銘柄の由来を明示)。
if _lss_source_bases:
    _na._LSS_SRC_BASES = _lss_source_bases
    print(f"[lss] 基準月ラベル {len(_lss_source_bases)}件をシグナル/明細に表示")
if _lss_start_dates:
    _na._LSS_START_DATES = _lss_start_dates
    print(f"[lss] OOS開始日制限 {len(_lss_start_dates)}件 (最新基準月のみ銘柄の遡及防止)")

# reload で上書きされた WATCHLIST を元に戻す
_stop.WATCHLIST[:] = _orig_stop_wl
_brk.WATCHLIST[:]  = _orig_brk_wl

# nikkei_analysis にホールドアウト設定を注入
_na._SIGNALS_AVAILABLE = True
_na._PNL_CONFIGS[:] = _all_configs
# 損益タブの予算フィルタ: 約定値(entry_p)で「100株買えた取引だけ」に絞る。
# 選定時の latest_price フィルタは範囲内でも、約定時に急騰した銘柄(例:選定時5,000円→
# 約定時20,000円)は 100株買えないので明細/月別集計から除外する。
_na._PNL_ENTRY_MAX_PRICE = _args.max_price if (_args.max_price and _args.max_price < 100000) else 0.0
_na._PNL_ENTRY_MIN_PRICE = _args.min_price if (_args.min_price and _args.min_price > 0) else 0.0
# 損益タブの月別グリッドを全期間表示する窓(日)。--days が365超なら過去まで表示。
# 365以下(既定/ライブ)なら None 相当で従来どおり(挙動不変)。
_na._BT_WINDOW_DAYS = max(_PNL_PERIODS) if max(_PNL_PERIODS) > 365 else None
# --forward-days/--back-days: 損益タブを『基準日-back 〜 基準日+forward』で打ち切る(軽量化)。
if (_args.forward_days > 0 or _args.back_days > 0) and _args.date:
    from datetime import date as _fwd_date_cls
    try:
        _fwd_base = _fwd_date_cls.fromisoformat(_args.date)
        # 表示の最古日(=warmupの起点)。基準日より前も出す場合はその分さかのぼる。
        _na._REPORT_START = _fwd_base - timedelta(days=_args.back_days)
        _na._REPORT_END = _fwd_base + timedelta(days=_args.forward_days)
        _na._BT_WINDOW_DAYS = _disp_days   # full_trade_log を確実に回す
        print(f"  [forward] 損益タブ集計期間: {_na._REPORT_START} 〜 {_na._REPORT_END} "
              f"(基準日 {_fwd_base} / 前{_args.back_days}日+後{_args.forward_days}日) "
              f"※現在まで走らせない軽量モード", flush=True)
    except ValueError:
        print(f"[WARN] --date {_args.date} が不正。--forward-days/--back-days を無視します。")
# ショートモード: トレンド別成績テーブルの表示順・凡例を反転。
# ミラー(指値空売り)/ロング銘柄ショート も建玉は「売り」なのでショート視点で色付けする。
# ロングデイトレは本物のロング(買い建玉)なのでショート視点にはしない。
_na._IS_SHORT_MODE = _args.short or _args.mirror or _args.long_stop_short
# mirror/lss(ロング銘柄を空売り評価する分析専用)はシグナルタブの発注ボタンが
# ロング逆指値買いの値を送るため、発注を無効化して誤発注を防ぐ。
_na._ANALYSIS_ONLY = _analysis_only
# lss(--long-stop-short)は『信用新規売りの逆指値』として正しく発注できるようにする
# (side=short・エントリーのみ・同日引け買戻し)。mirror(--mirror)は却下済で発注不可のまま。
# ロングデイトレ(--long-daytrade)は同日決済レポートの骨格(月別/BT50/予算400万タブ)を
# lss と共有するため _LSS_ORDER_MODE を立て、向きだけ _LSS_LONG=True で反転する
# (発注は当面 分析専用=ロング用の同日決済watcherが未整備のため)。
_na._LSS_ORDER_MODE = bool((_args.long_stop_short or getattr(_args, "long_daytrade", False))
                           and not _args.mirror)
_na._LSS_LONG = bool(getattr(_args, "long_daytrade", False) and not _args.mirror)
# lss の損切/利確ATR倍率(既定0.1/1.0)をレポートの表示計算へ渡す(実際の注文内容に合わせる)。
_na._LSS_SM = _bte._SM_FORCE if getattr(_bte, "_SM_FORCE", None) is not None else 0.1
_na._LSS_TM = _bte._TM_FORCE if getattr(_bte, "_TM_FORCE", None) is not None else 1.0

# 日足版の同日TP/SLスイープは「近似」で不正確(同日の当たり判定が日足高安のみ。
# 実測ではミラーが日足+191万→5分足-240万と真逆になった)。既定OFF、--daily-tpsl-sweep
# で明示的に出したときだけ表示する。通常は下の5分足版(正確)のみ。
_na._SAMEDAY_SWEEP_TAB = (_analysis_only and not getattr(_args, "no_analysis", False)
                          and getattr(_args, "daily_tpsl_sweep", False))
_na._SAMEDAY_SWEEP_INVERTED = bool(_args.mirror)   # mirror=符号反転 / lss=そのまま
# 5分足版TP/SLスイープ(正確・本命)。5分足が無ければタブ内に注意書き(グレースフル)。
# ただし --lss-proposal(新選定・取引明細メインの軽量lss)ではスイープ格子は省略する
# (取引明細の5分足集計自体は _INTRADAY_5M で維持されるので同日損益は正確なまま)。
# ロングデイトレ(long_daytrade)は 5分足TP/SLスイープ格子(short前提の計算)を出さない。
# 取引明細の5分足集計は _INTRADAY_5M + _LONG_DAYTRADE で正しく維持される。
_na._SAMEDAY_5M_TAB = (_analysis_only and not getattr(_args, "no_analysis", False)
                       and not getattr(_args, "long_daytrade", False)
                       and (not getattr(_args, "lss_proposal", None)
                            or getattr(_args, "lss_tpsl", False)))

# 重い長系の詳細分析(保有日数比較/損切り幅/寄り付き方向/下落深さ/em比較/約定
# タイミング/期間別パネル)を飛ばすか。--no-analysis 指定時に加え、mirror/lss は
# これらが全てロング前提で無意味なので常にスキップ(=同日スイープだけ回して高速化)。
_skip_heavy_analysis = getattr(_args, "no_analysis", False) or _analysis_only

# ── バックテストキャッシュ (5期間 × 同一銘柄の重複実行を防ぐ) ─────────────────
# 全設定統合(180)+6期間+シグナルタブで同一(銘柄,戦略,モード)が何度も呼ばれるため、
# 1プロセス内で結果をメモ化して重複計算を防ぐ。
# さらにディスクにも永続化し、当日内なら中断・再実行でも再計算しない。
import pickle as _pickle
import atexit as _atexit
import time as _time

_bt_cache: dict[tuple, dict | None] = {}

_bt_cache_dir  = Path(".holdout_bt_cache")
_bt_cache_dir.mkdir(exist_ok=True)
# BTキャッシュのキーは「期待される最新確定バー日付」を使う。
# 15:00 JST を跨ぐ(前営業日→当日)と自動で別ファイル=再計算になり、
# 「引け前に作ったキャッシュ(現在値=前日)が引け後も再利用され、現在値が更新されない」
# 問題を防ぐ。--date 指定(過去日再現)時はその日付を使う。
try:
    from backtest_limit_entry import _expected_latest_bar_date as _exp_bar_date
    _bt_bar_tok = str(_args.date) if _args.date else str(_exp_bar_date())
except Exception:
    _bt_bar_tok = _cache_date
# ── ラグ対策: BTキャッシュのタグを「実データの最新バー」で確定する ──────────────
# _expected_latest_bar_date() は時計(平日15:40以降=今日)で期待日を返すが、yfinance の
# 日足確定には数十分ラグがあり、15:40〜(引け直後)に実行するとまだ当日バーが配信されて
# いないことがある。その状態で作った BTキャッシュ(中身は前営業日まで)が「今日版」として
# ファイル名にタグ付けされ、当日中ずっと使い回されると、当日バーで初めて立つシグナルが
# 一日中出力・凍結されない(→ 損益タブが凍結BTを引けず別ロジックの仮値を表示する)。
# 対策: 起動時に流動性の高い基準銘柄で「実際に取れている最新バー日付」を確認し、それが
# 期待日より古ければ、キャッシュタグを実バー日付に落とす。当日バーが配信された後の実行で
# 初めて今日タグの新規キャッシュが作られ、シグナルが正しく出力・凍結される。
# --date(過去日再現)時は対象外。通常(データ確定済み)は実バー==期待日で挙動は不変。
# _data_asof = このキャッシュが実際に使うデータの最新バー日付(鮮度スタンプ)。
# キャッシュ再利用時に「保存時データ < 今使えるデータ」なら自動で作り直す判定に使う。
_data_asof = str(_args.date) if _args.date else None
if not _args.date:
    try:
        from datetime import date as _date_cls2
        from backtest_limit_entry import fetch as _ref_fetch
        _expected_bar = _exp_bar_date()
        _actual_bar = None
        for _ref_sym in ("7203.T", "6758.T", "9984.T"):   # トヨタ/ソニー/SBG (高流動・欠損稀)
            try:
                _ref_df = _ref_fetch(_ref_sym, 30)
                if _ref_df is not None and len(_ref_df) > 0:
                    _lb = _ref_df.index[-1]
                    _actual_bar = _lb.date() if hasattr(_lb, "date") else _lb
                    break
            except Exception:
                continue
        if _actual_bar is not None:
            # 実際に取れているデータの最新日(期待日を超えない=trim済み)を鮮度スタンプに。
            if isinstance(_expected_bar, _date_cls2):
                _data_asof = str(min(_actual_bar, _expected_bar))
            else:
                _data_asof = str(_actual_bar)
            if isinstance(_expected_bar, _date_cls2) and _actual_bar < _expected_bar:
                print(f"[BTキャッシュ] データ未確定検出: 実最新バー {_actual_bar} < 期待 "
                      f"{_expected_bar} → キャッシュタグを実バー {_actual_bar} に調整"
                      f"(ラグ中の暫定キャッシュが『今日版』として固定される不具合を回避)",
                      flush=True)
                _bt_bar_tok = str(_actual_bar)
        else:
            _data_asof = str(_expected_bar)
    except Exception as _tok_e:
        print(f"[BTキャッシュ] 実バー確認をスキップ (期待日でタグ付け): {_tok_e}", flush=True)
# 約定ロジックのバージョン。約定/決済モデルを変えたらここを上げると、旧BTキャッシュを
# 自動で無効化(ファイル名が変わる)して作り直す。バー日付だけだとコード変更が反映されない。
#   v2: 現実的約定モデル(min(トリガー,始値)+-3%指値ガード)を導入(2026-07-18)
#   v3: 不約定(nofill_log)を保持(予算シミュの注文時枠消費用)。結果dictに新フィールド追加(2026-07-20)
#   v4: ENTRY_EXPIRE 1→0(翌営業日のみ有効=当日限り。実運用の当日限り注文に一致)(2026-07-21)
#   v5: lss約定価格を opens[ei](約定バー始値)→opens[0](その日の始値)に修正。ザラ場約定は
#       トリガー近辺で約定するのに約定バー始値だと過小評価だった(実約定と乖離)(2026-07-22)
#   v6: 寄り値を日足始値優先に。5分足の寄り(opens[0])が稀に壊れる(例:実2,869なのに5分足2,819
#       始まり)ため、信頼できる日足始値で寄りギャップ判定(2026-07-22)
#   v7: 寄り約定(ギャップで始値約定)時は約定バーから損切り/利確を見る。建玉は寄りから開いて
#       いるので約定バー内のヒットも計上(ザラ場約定は従来通り次バーから)(2026-07-22)
#   v8: 5分約定判定を実発注の呼値グリッドに合わせる。損切=ライン直上ティック(ceil,早すぎる
#       損切り回避)、利確=一つ上で判定、トリガー=最近接ティック。BTと実注文の損切/利確が一致
#       (例:シーイーシー損切ライン2,232→注文2,235で高値2,232では損切りせずタイムカット)(2026-07-23)
#   v9: lssトリガーに「前日終値-1ティック」を反映(逆指値売りは現在値以上だと即約定で弾かれる)。
#       engine生値は前日終値そのままで-1tickが無く、損切/利確が実注文より1ティック高かった。
#       トリガーを-1tickにして損切=トリガー+atr*sm(ceil)/利確=-atr*tm(ceil)で再計算し実注文に一致
#       (例:応用地質 損切2,835→2,830で寄り高値2,830を正しく損切り)(2026-07-23)
#   v10: 損切/利確のリスク幅は「前日終値」基準に測る(§18検証モデルに一致)。トリガーだけ
#        -1tick(約定用)、損切=前日終値+atr*sm(ceil)/利確=前日終値-atr*tm(ceil)。v9は-1tickを
#        損切りにも反映して1ティック不必要にタイトだった。緩めて寄りヒゲで刈られにくくする
#        (例:応用地質 損切2,835/トリガー2,825で寄り高値2,830はタイムカット)(2026-07-23)
#   v11: 寄り(09:00-09:05)の5分足バー欠落による約定取りこぼしを日足安値で救済。J-Quants
#        5分足は寄りバーが欠けることがあり(例:7180は先頭09:05, 日足始値1,575で寄ったのに
#        5分足最安値1,582)、トリガーを寄りで割った約定を no_entry と誤判定していた。日足安値
#        ≤トリガー なら寄り欠落バーで約定とみなし、決済はbar0から判定(=正しく損切り計上)(2026-07-23)
#   v12: タイムカット(引け成行MOC)の決済価格を公式終値(日足の確定終値)に。従来は5分足の最終足
#        終値を使い、引けの寄成/引成ギャップや当日5分足の未同期で実際の引け値とズレていた
#        (例:アルコニックス 5分足最終足2,557 vs 公式終値2,573)。day_close優先・無ければ従来通り(2026-07-24)
#   v13: 同バー損切り(悲観=損切り優先)。約定した5分足の中で損切りラインに触れていれば損切り
#        扱いに(旧は約定バーを飛ばし後の目標達成を誤計上。例:4092は約定5,710→同足5,770で損切り
#        なのに+41,901円と計上)。short_exit_5m を常に約定バー(ei)から判定(2026-07-24)
#   v14: 指値ガードを 3%→2% に試したが、本番エンジンの月別損益で2%は3%より約-1%(微減)と判明。
#        3%へ差し戻し(2026-07-31)。v13(3%)のキャッシュに戻すのでバージョンも v13 に戻す。
_BT_LOGIC_VER = "v13"
# lss損切り遅延フラグ(delay1等)を使う場合はBTキャッシュを別管理(ON/OFFで衝突しないよう
# 版トークンに sd<N> を付与)。env LSS_STOP_DELAY_BARS=1 で有効化(既定0=現行と同一キー)。
_LSS_STOP_DELAY = int(os.environ.get("LSS_STOP_DELAY_BARS", "0") or "0")
if _LSS_STOP_DELAY > 0:
    _BT_LOGIC_VER = f"{_BT_LOGIC_VER}sd{_LSS_STOP_DELAY}"
# 指値ガード(深ギャップ約定不可の閾値)を env で上書き可能に(A/B検証用)。既定=3%。
# 既定と異なる時だけ BTキャッシュキーに g<‰> を付与し、別ガードの結果を混同しない。
#   例: set LSS_GAP_LIMIT=0.02 & .\daily  → 2%版を別キャッシュで生成。
#   比較は「BT40以上(2877)フィルタの総損益」を 2% と 3% で突き合わせる(=BT40全取引で深ギャップ検証)。
_LSS_GAP_ENV = os.environ.get("LSS_GAP_LIMIT", "").strip()
if _LSS_GAP_ENV:
    try:
        _gv = float(_LSS_GAP_ENV)
        if _gv > 0:
            _bte._INTRADAY_5M_ENTRY_GAP_LIMIT = _gv
            print(f"[gap] 指値ガードを env で {_gv*100:.2f}% に上書き", flush=True)
            if abs(_gv - 0.03) > 1e-9:
                _BT_LOGIC_VER = f"{_BT_LOGIC_VER}g{int(round(_gv * 1000))}"
    except ValueError:
        print(f"[gap] LSS_GAP_LIMIT='{_LSS_GAP_ENV}' を解釈できず → 既定3%のまま", flush=True)
_bt_cache_file = _bt_cache_dir / f"bt{_cache_short}_{_BT_LOGIC_VER}_{_bt_bar_tok}.pkl"
# 鮮度スタンプ: このキャッシュが「どのデータ日付で作られたか」を隣に記録する。
_bt_asof_file  = _bt_cache_file.with_suffix(".pkl.asof")
# 同一 最新確定バー日付 の間だけ再利用（--force はHTML再生成のみ強制、BTキャッシュは
# バー日付が進めば自動で作り直す）。
# さらに鮮度スタンプで「保存時データ < 今使えるデータ」なら自動で破棄・再生成する。
# これにより、同じトークン名のまま古いデータで作られたキャッシュ(例: 修正前に引け直後の
# 未確定データで作られた『今日版』)が残っていても、手動削除なしに自動で作り直される。
if _bt_cache_file.exists():
    _bt_stale = False
    if not _args.date and _data_asof:
        try:
            _saved_asof = (_bt_asof_file.read_text(encoding="utf-8").strip()
                           if _bt_asof_file.exists() else "")
        except Exception:
            _saved_asof = ""
        if (not _saved_asof) or (_saved_asof < _data_asof):
            print(f"[BTキャッシュ] 鮮度スタンプ {_saved_asof or '無し'} < 現在データ "
                  f"{_data_asof} → 古いキャッシュを破棄して再生成します", flush=True)
            _bt_stale = True
    if not _bt_stale:
        try:
            with open(_bt_cache_file, "rb") as _bf:
                _bt_cache = _pickle.load(_bf)
            print(f"[BTキャッシュ] {len(_bt_cache)}件をディスクから復元")
        except Exception:
            _bt_cache = {}

_bt_cache_dirty = {"n": 0}

def _save_bt_cache():
    if _bt_cache_dirty["n"] == 0:
        return
    try:
        # ワーカースレッドが書き込み中でも落ちないよう、反復前にスナップショットを取る
        # (dict changed size during iteration 対策)。dict(...) は原子的でスレッド安全。
        _snap = dict(_bt_cache)
        with open(_bt_cache_file, "wb") as _bf:
            _pickle.dump(_snap, _bf, protocol=_pickle.HIGHEST_PROTOCOL)
        # 鮮度スタンプを更新(このキャッシュがどのデータ日付で作られたか)。
        try:
            _bt_asof_file.write_text(str(_data_asof or _bt_bar_tok), encoding="utf-8")
        except Exception:
            pass
        print(f"[BTキャッシュ] {len(_snap)}件を保存 ({_bt_cache_file})")
    except Exception as _e:
        print(f"[BTキャッシュ] 保存失敗: {_e}")

# 中断(Ctrl-C)・正常終了どちらでも保存し、途中までの計算を次回再利用する
_atexit.register(_save_bt_cache)

def _make_cached_bt(orig_fn):
    _mod_globals = orig_fn.__globals__  # 対象モジュールのグローバル名前空間
    def wrapper(symbol, name, strategy, max_hold=None):
        mode = os.environ.get("TRADING_MODE", "conservative")
        mh_key = ""
        if max_hold is not None:
            mh_key = f"|mh{max_hold}"
            # sm/tm をキーに含めることで、mode切替後に古いキャッシュが返るのを防ぐ
            try:
                params = _mod_globals.get("STRATEGY_PARAMS", {}).get(strategy)
                if params and len(params) >= 4:
                    mh_key += f"|{params[2]:.2f}_{params[3]:.2f}"
            except Exception:
                pass
        key  = f"{symbol}|{strategy}|{mode}{_cache_settings}{mh_key}"
        # ミラー/空売り/同日決済(max_hold=0)の状態をキーに含める。
        # これが無いと、旧バージョン(例: lss が同日決済でなかった頃)の
        # bt_{mirror,lss}_*.pkl キャッシュを誤って再利用し、保有日数や損益が
        # 古い挙動のまま表示されてしまう。フラグが立つ mirror/lss のみ付与する
        # ので、ロング/ショートの既存キャッシュキーは不変(再計算を強制しない)。
        _mhf = getattr(_bte, "_MAX_HOLD_FORCE", None)
        if getattr(_bte, "_MIRROR_PNL", False) or getattr(_bte, "_ENTRY_TYPE_FORCE", None) or _mhf is not None:
            key += (f"|MIR{int(bool(getattr(_bte, '_MIRROR_PNL', False)))}"
                    f"|ET{getattr(_bte, '_ENTRY_TYPE_FORCE', None) or ''}"
                    f"|MHF{_mhf if _mhf is not None else ''}")
            # 5分足集計の有無・幅もキーに含める(日足近似の旧キャッシュ誤再利用を防ぐ)
            if getattr(_bte, "_INTRADAY_5M", False):
                key += (f"|5M{getattr(_bte, '_INTRADAY_5M_SOURCE', '')}"
                        f"|SM{getattr(_bte, '_SM_FORCE', '')}|TM{getattr(_bte, '_TM_FORCE', '')}")
                # 約定モデルOFF(旧・楽観)のときだけキーを分ける。既定(現実的)は従来キーのまま
                # =温まった既存キャッシュを維持。A/B比較でキャッシュが混ざらないようにする。
                if not getattr(_bte, "_INTRADAY_5M_REALISTIC_ENTRY", True):
                    key += "|RE0"
                # 約定遅延ありのときだけキーを分ける(既定0は従来キー=温存)。
                _edly_k = int(getattr(_bte, "_INTRADAY_5M_ENTRY_DELAY", 0) or 0)
                if _edly_k > 0:
                    key += f"|DLY{_edly_k}"
        if key not in _bt_cache:
            _bt_cache[key] = orig_fn(symbol, name, strategy, max_hold)
            _bt_cache_dirty["n"] += 1
            # 100件ごとに途中保存 (長時間実行の中断対策)
            if _bt_cache_dirty["n"] % 100 == 0:
                _save_bt_cache()
        return _bt_cache[key]
    return wrapper

# ロング側 (check_signals_stop / breakout)
_stop.backtest_one = _make_cached_bt(_stop.backtest_one)
_brk.backtest_one  = _make_cached_bt(_brk.backtest_one)

# ショート側 (check_signals_short / short_breakout)。
# nikkei_analysis の _mod_for() はショート戦略をこれらに振り分けるため、
# ここをラップしないとショート実行でキャッシュが全く効かず7〜8倍重くなる。
for _mod_attr in ("_short", "_sbrk"):
    _m = getattr(_na, _mod_attr, None)
    if _m is not None and hasattr(_m, "backtest_one"):
        _m.backtest_one = _make_cached_bt(_m.backtest_one)

# ── ロングBT参照 & BTフィルタ ────────────────────────────────────────────────
# 取引明細の「BT40以下」ボタンや、任意の --bt-max/--bt-min グローバル絞り込みは
# “ロングの”BTで判定する。BTを現モード(mirror/lss)で測ると符号が反転して意味が
# 逆になるため。ロングBTは同日に先行実行されたロング子プロセスの BTキャッシュ
# (bt_{bar}.pkl)から読み出す(--both はロング→ショート→mirror→lss の順なので
# mirror/lss 実行時には存在する)。
# ※ ロングデイトレ(long_daytrade)は本物のロングなので現モードBT自体がロングBT。
#   外部のスイングロングBTで上書きせず、native BT(_eff_long_bt の rec_score)を使う。
if _analysis_only and not getattr(_args, "long_daytrade", False):
    _long_bt_ref: dict = {}
    _long_cache_file = _bt_cache_dir / f"bt_{_BT_LOGIC_VER}_{_bt_bar_tok}.pkl"   # ロング(_cache_short="")のキャッシュ(版付き)
    if _long_cache_file.exists():
        try:
            with open(_long_cache_file, "rb") as _lbf:
                _long_bt_raw = _pickle.load(_lbf)
            for _k, _v in _long_bt_raw.items():
                if not _v or not isinstance(_v, dict):
                    continue
                _pr = _v.get("period_results")
                if not _pr:
                    continue
                _parts = str(_k).split("|")
                if len(_parts) < 2:
                    continue
                _ksym, _kstrat = _parts[0], _parts[1]
                try:
                    _sc = _na._mod_for(_kstrat).calc_recommend_score(_pr)[0]
                except Exception:
                    continue
                _kk = (_ksym, _kstrat)
                # 同一(sym,strat)が複数mode(con/agg)で入るので最大(=最良ロングBT)を採用
                if _kk not in _long_bt_ref or _sc > _long_bt_ref[_kk]:
                    _long_bt_ref[_kk] = _sc
            print(f"[BTフィルタ] ロングBT参照 {len(_long_bt_ref)}件を {_long_cache_file.name} から読込"
                  f"(取引明細の『BT40以下』ボタンで使用)")
        except Exception as _e:
            print(f"[BTフィルタ] ロングBT参照の読込失敗: {_e} → 現モードBTで代替判定")
    else:
        print(f"[BTフィルタ] ロングBTキャッシュ {_long_cache_file.name} が無い → 現モードBTで代替判定"
              f"(--both ならロングを先に実行すれば正確になります)")
    _na._LONG_BT_REF = _long_bt_ref

# 任意のグローバルBT絞り込み(--bt-max / --bt-min)。既定は無効(=全銘柄を集計し、
# 取引明細の『BT40以下』ボタンで切り替える運用)。明示指定時のみレポート全体を絞る。
if (_args.bt_max or 0) > 0 or (_args.bt_min or 0) > 0:
    _na._PNL_BT_MAX = float(_args.bt_max or 0)
    _na._PNL_BT_MIN = float(_args.bt_min or 0)
    print(f"[BTフィルタ] グローバル絞り込み: ロングBT "
          f"{(format(_args.bt_min,'.0f')+'≤') if _args.bt_min else ''}"
          f"{('<'+format(_args.bt_max,'.0f')) if _args.bt_max else ''} の銘柄だけを集計")

# ── シグナルスコアキャッシュ読み込み ─────────────────────────────────────────
# 初回発信時のBTスコアを保存し、以後の実行でも同じスコアを表示する。
# キャッシュキー: "{symbol}::{strategy}::{signal_date}"
import json as _json

# lss は専用キャッシュに凍結BTを保存(long/short と (sym,strat,date) キーが衝突するため分離)。
# これで『シグナルが出た時点のBTスコアを凍結し、以降の再計算で上書きしない』=発注時BTと一致。
_score_cache_path = Path("signal_score_cache_ldt.json"
                         if getattr(_args, "long_daytrade", False)
                         else ("signal_score_cache_lss.json"
                               if getattr(_args, "long_stop_short", False)
                               else "signal_score_cache.json"))
_score_cache: dict = {}
if _score_cache_path.exists():
    try:
        _score_cache = _json.loads(_score_cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass

# キャッシュから (sym, strat) → 最新signal_date & bt_score を取得
_cached_latest: dict[tuple, dict] = {}
for _ck, _cv in _score_cache.items():
    _parts = _ck.split("::")
    if len(_parts) == 3:
        _csym, _cstrat, _csigdate = _parts
        _existing = _cached_latest.get((_csym, _cstrat))
        if _existing is None or _csigdate > _existing["signal_date"]:
            _cached_latest[(_csym, _cstrat)] = {"signal_date": _csigdate,
                                                  "bt_score": _cv.get("bt_score", 0)}

# キャッシュにあるスコアを注入 (signal_date は後で検証)
_na._FROZEN_BT_SCORES.clear()
for (_csym, _cstrat), _info in _cached_latest.items():
    _na._FROZEN_BT_SCORES[(_csym, _cstrat)] = _info["bt_score"]

# シグナル発生時BTスコアを (sym, strat, signal_date_str) → score で注入
# P&Lタブで「シグナル発生時のBTスコア」を正確に表示するために使用
_na._SIGNAL_DATE_BT_SCORES.clear()
for _ck, _cv in _score_cache.items():
    _ck_parts = _ck.split("::")
    if len(_ck_parts) == 3:
        _csym2, _cstrat2, _csigdate2 = _ck_parts
        _na._SIGNAL_DATE_BT_SCORES[(_csym2, _cstrat2, _csigdate2)] = _cv.get("bt_score", 0)

# ── target_date 解決 ─────────────────────────────────────────────────────────
target_date = None
if _args.date:
    from datetime import date as _date_cls
    try:
        target_date = _date_cls.fromisoformat(_args.date)
    except ValueError:
        pass

date_str = _args.date or str(TODAY)

# レポート種別ラベル(タイトル/見出し用)
_bt_flt_lbl = ""
if _na._PNL_BT_MAX > 0 or _na._PNL_BT_MIN > 0:
    _lo = f"BT{_na._PNL_BT_MIN:.0f}≤" if _na._PNL_BT_MIN > 0 else ""
    _hi = f"<{_na._PNL_BT_MAX:.0f}" if _na._PNL_BT_MAX > 0 else ""
    _bt_flt_lbl = f"・ロング{_lo}{_hi}"
if _args.mirror:
    _mode_label_ja = f"（ロングミラー・指値空売り/同日決済{_bt_flt_lbl}）"
elif _args.long_stop_short:
    _mode_label_ja = f"（ロング銘柄ショート・逆指値空売り/同日決済{_bt_flt_lbl}）"
elif getattr(_args, "long_daytrade", False):
    _mode_label_ja = f"（ロングデイトレ・逆指値買い/同日決済{_bt_flt_lbl}）"
elif _args.short:
    _mode_label_ja = "（ショート）"
else:
    _mode_label_ja = ""

# ── 日経相場環境・トレンド・エントリー分析タブ ─────────────────────────────────
print("日経平均データ取得中 (市場分析)...", flush=True)
_market_tab1_html = _market_tab2_html = _market_tab3_html = ""
try:
    import pandas as _pd
    _na_years  = 5
    _na_end    = target_date if _args.date else TODAY  # 基準日でN225データを揃える
    _na_close  = _na.fetch_n225(_na_years, end_date=_na_end)
    _na_close  = _na_close[_na_close.index <= _pd.Timestamp(_na_end)]
    if not _na_close.empty:
        _na_trend     = _na.label_trend(_na_close)
        _na_r         = _na.get_regime(_na_close)
        _na_ref       = target_date or TODAY
        _na_periods   = _na.extract_periods(_na_close, _na_trend, _na_ref)
        _na_up_p      = _na.extract_up_periods(_na_close, _na_trend, _na_ref)
        _na_all_stats = {
            "up":   _na.calc_stats([p for p in _na_periods if p["trend"] == "up"]),
            "down": _na.calc_stats([p for p in _na_periods if p["trend"] == "down"]),
        }
        print("参考指標取得中...", flush=True)
        _na_indicators = _na.fetch_market_indicators(years=1, end_date=_na_end)
        import traceback as _tb_mkt
        # タブごとに個別 try: 1つ失敗しても他のタブを生かす + 例外を必ず表示
        try:
            _market_tab1_html = _na._tab1_signal_html(
                _na_r, _na_ref, indicators=_na_indicators, periods=_na_periods)
        except Exception as _e1:
            print(f"[WARN] 相場環境タブ失敗: {_e1}\n{_tb_mkt.format_exc()}", flush=True)
        # 相場環境/トレンド期間/エントリー分析は日経データ1回で作れる軽い相場コンテキスト。
        # --no-analysis でも計算する(スキップするのは重い期間別パネル/詳細分析/約定タイミング)。
        try:
            _market_tab2_html = _na._tab2_trend_html(
                _na_close, _na_trend, _na_periods, _na_years)
        except Exception as _e2t:
            print(f"[WARN] トレンド期間タブ失敗: {_e2t}\n{_tb_mkt.format_exc()}", flush=True)
        try:
            _market_tab3_html = _na._tab3_timing_html(
                _na_close, _na_up_p, _na_all_stats)
        except Exception as _e3t:
            print(f"[WARN] エントリー分析タブ失敗: {_e3t}\n{_tb_mkt.format_exc()}", flush=True)
        print(f"市場分析タブ生成完了 (相場環境={len(_market_tab1_html)}字 "
              f"/ トレンド期間={len(_market_tab2_html)}字 "
              f"/ エントリー分析={len(_market_tab3_html)}字)", flush=True)
except Exception as _me:
    import traceback as _tb_mkt2
    print(f"[WARN] 市場分析スキップ: {_me}\n{_tb_mkt2.format_exc()}", flush=True)

_market_tab_btns = ""
if _market_tab1_html:
    _market_tab_btns += (
        "\n  <button class=\"ho-outer-btn\" onclick=\"switchHoTab('mkt1')\">&#x1F4CA; 相場環境</button>")
if _market_tab2_html:
    _market_tab_btns += (
        "\n  <button class=\"ho-outer-btn\" onclick=\"switchHoTab('mkt2')\">&#x1F4C8; トレンド期間</button>")
if _market_tab3_html:
    _market_tab_btns += (
        "\n  <button class=\"ho-outer-btn\" onclick=\"switchHoTab('mkt3')\">&#x23F1; エントリー分析</button>")

# ── 日経バナーは銘柄に依存しないので先に取得 ─────────────────────────────────
try:
    from signal_risk_check import (
        precompute_all       as _precompute_risks,
        render_nikkei_banner as _render_nikkei_banner,
    )
    _nikkei_banner = _render_nikkei_banner()
except Exception as _re:
    print(f"[WARN] リスクチェックスキップ: {_re}", flush=True)
    _precompute_risks = None
    _nikkei_banner = ""

# ── 工程タイミング計測 ────────────────────────────────────────────────────────
_T0 = _time.time()
def _phase(msg: str):
    print(f"  [⏱ {_time.time() - _T0:6.1f}s] {msg}", flush=True)

# ── シグナルタブ HTML (パス1: バッジなし) ─────────────────────────────────────
print("シグナル収集中...", flush=True)
_na._PNL_CONFIGS[:] = _all_configs
_sig_html = _na._tab4_signals_html(
    workers=_args.workers,
    min_score=_args.min_score,
    target_date=target_date,
)
_phase("シグナルタブ完了")

# ── キャッシュ更新・signal_date 検証 ─────────────────────────────────────────
# 新規シグナル保存 & signal_dateが変わった銘柄のスコアを更新
_needs_regen = False
for _sig in _na._last_signals:
    _ssym   = _sig.get("symbol", "")
    _sstrat = _sig.get("strategy", "")
    _ssigdt = str(_sig.get("signal_date", ""))
    _skey   = f"{_ssym}::{_sstrat}::{_ssigdt}"
    _cached = _cached_latest.get((_ssym, _sstrat))

    if _skey not in _score_cache:
        # 新規シグナル: 現在のBTスコアで保存
        _real_bt = _sig.get("rec_score", 0)
        _score_cache[_skey] = {"bt_score": _real_bt, "first_seen": str(TODAY)}
        if _cached and _cached["signal_date"] != _ssigdt:
            # signal_dateが変わった → 古い凍結スコアを使っているので再生成が必要
            _na._FROZEN_BT_SCORES[(_ssym, _sstrat)] = _real_bt
            _needs_regen = True

# signal_dateが変わった銘柄があれば HTML を再生成 (スコア更新のみ、まだバッジなし)
if _needs_regen:
    print("シグナル再生成中 (signal_date更新あり)...", flush=True)
    _sig_html = _na._tab4_signals_html(
        workers=_args.workers,
        min_score=_args.min_score,
        target_date=target_date,
    )

# ── リスク警告・決算日: シグナルが出た銘柄のみ事前計算 ────────────────────────
# _last_signals はここで確定しているので、シグナル銘柄のみに絞り込む
_sig_sym_map: dict[str, str] = {}
for _sig in _na._last_signals:
    _s = _sig.get("symbol", "")
    _n = _sig.get("name", "")
    if _s and _s not in _sig_sym_map:
        _sig_sym_map[_s] = _n

if getattr(_args, "no_risk", False):
    print("決算日/リスクチェック: スキップ (--no-risk)", flush=True)
elif _precompute_risks and _sig_sym_map:
    try:
        _precompute_risks(
            list(_sig_sym_map.items()),
            workers=_args.workers,
            target_date=target_date,
        )
        # バッジ付きで再生成
        _sig_html = _na._tab4_signals_html(
            workers=_args.workers,
            min_score=_args.min_score,
            target_date=target_date,
        )
    except Exception as _re2:
        print(f"[WARN] リスクチェック失敗: {_re2}", flush=True)
elif not _sig_sym_map:
    print("[INFO] シグナルなし — リスクチェックスキップ", flush=True)

# キャッシュ保存: lss は専用キャッシュ(signal_score_cache_lss.json)にBTを凍結して保存。
# mirror は却下済み・分析専用なのでスキップ。long/short は従来通り共有キャッシュ。
if getattr(_args, "mirror", False):
    print("[INFO] mirror は分析専用: signal_score_cache 保存スキップ", flush=True)
else:
    try:
        _score_cache_path.write_text(
            _json.dumps(_score_cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[INFO] シグナルスコアキャッシュ: {len(_score_cache)}件保存", flush=True)
    except Exception as _e:
        print(f"[WARN] キャッシュ保存失敗: {_e}", flush=True)

# ── シグナルを JSON へエクスポート (position_server の /signals が読む) ─────────
# ロング → signals_latest.json / ショート → signals_latest_short.json
# position_server.py からワンクリック登録できるよう、確定シグナルを永続化する。
# ※ mirror/lss は分析専用モード。実発注用の signals_latest.json を上書きすると
#   発注サーバが誤ったモードのシグナルを掴むため、JSON書き出しをスキップする。
if _analysis_only:
    print("[INFO] mirror/lss は分析専用: signals_latest.json 書き出しスキップ", flush=True)
try:
    if _analysis_only:
        raise _AnalysisOnlySkip()
    _sig_export = []
    for _s in _na._last_signals:
        try:
            _qty = _na._calc_qty(_s.get("order_p", 0), _s.get("stop_p", 0)) \
                   if _s.get("order_p") else 100
        except Exception:
            _qty = 100
        _sig_export.append({
            "symbol":      _s.get("symbol", ""),
            "name":        _s.get("name", ""),
            "strategy":    _s.get("strategy", ""),
            "order_p":     _s.get("order_p", 0),
            "stop_p":      _s.get("stop_p", 0),
            "target_p":    _s.get("target_p", 0),
            "signal_date": str(_s.get("signal_date", "")),
            "score":       _s.get("score", 0),
            "rank":        _s.get("rank", ""),
            "qty":         _qty,
        })
    _sig_out = Path("signals_latest_short.json" if _args.short else "signals_latest.json")

    # 同日に複数の価格レンジで実行した場合はマージ（symbol+strategy でデdup）
    _existing: list[dict] = []
    if _sig_out.exists():
        try:
            _prev = _json.loads(_sig_out.read_text(encoding="utf-8"))
            if str(_prev.get("signal_date", "")) == str(_cache_date):
                _existing = _prev.get("signals", [])
        except Exception:
            pass
    # 既存シグナルに今回の分を追加（symbol+strategy が重複しないよう優先: 今回分）
    _existing_keys = {(s["symbol"], s["strategy"]) for s in _sig_export}
    _merged = _sig_export + [s for s in _existing
                              if (s["symbol"], s["strategy"]) not in _existing_keys]
    _merged.sort(key=lambda s: s.get("score", 0), reverse=True)

    _sig_payload = _json.dumps({
        "generated_at": str(TODAY),
        "signal_date":  _cache_date,
        "mode":         "short" if _args.short else "long",
        "signals":      _merged,
    }, ensure_ascii=False, indent=2)
    _sig_out.write_text(_sig_payload, encoding="utf-8")
    # 日付付きバックアップも保存（翌日の約定登録時に参照できるよう）
    _sig_dated = Path(f"signals_{_cache_date}{'_short' if _args.short else ''}.json")
    _sig_dated.write_text(_sig_payload, encoding="utf-8")
    print(f"[INFO] シグナルJSON書き出し: {_sig_out.name} + {_sig_dated.name} ({len(_merged)}件, うち今回{len(_sig_export)}件)", flush=True)
except _AnalysisOnlySkip:
    pass
except Exception as _e:
    print(f"[WARN] シグナルJSON書き出し失敗: {_e}", flush=True)

# ── ロング転換トレードレコード生成 (lss モード + oos_raw_fold*.csv 存在時) ──────
# _na._EXTRA_TRADES に転換 trade dict を注入して、損益タブの月別/日別カードに混合表示。
# filled=0 (lss未約定) の OOS 銘柄を 9:09 成行買い → 11:30 成行売り でシミュレーション。
_na._EXTRA_TRADES = []
if getattr(_args, "long_stop_short", False):
    try:
        import glob as _lrv_glob
        import pickle as _lrv_pickle
        from datetime import time as _lrv_dtime
        from pathlib import Path as _lrv_Path

        _lrv_csvs = sorted(_lrv_glob.glob("oos_raw_fold*.csv"))
        print(f"[転換] OOS CSV: {len(_lrv_csvs)}件 / CWD={os.getcwd()}", flush=True)
        if _lrv_csvs:
            print(f"  例: {[str(c) for c in _lrv_csvs[:3]]}", flush=True)
            import pandas as _lrv_pd
            _lrv_all_df = _lrv_pd.concat(
                [_lrv_pd.read_csv(f) for f in _lrv_csvs], ignore_index=True)
            _lrv_all_df["entry_date"] = _lrv_pd.to_datetime(
                _lrv_all_df["entry_date"]).dt.date

            _LRV_DIR_1M = _lrv_Path(
                os.environ.get("MINUTE_1M_DIR",
                               _lrv_Path.home() / ".jquants_cache" / "minute"))
            _LRV_DIR_5M: "_lrv_Path | None" = None
            _env5m = os.environ.get("MINUTE_5M_DIR", "").strip()
            if _env5m:
                _LRV_DIR_5M = _lrv_Path(_env5m)
            if _LRV_DIR_5M is None or not _LRV_DIR_5M.exists():
                _here5m = _lrv_Path(__file__).resolve().parent
                for _c5m in [_here5m / "data" / "stock_5min",
                              _here5m.parent / "stock_5min",
                              _here5m.parent / "stock_5min" / "data" / "stock_5min"]:
                    try:
                        if _c5m.exists() and any(_c5m.glob("*.pkl")):
                            _LRV_DIR_5M = _c5m
                            break
                    except Exception:
                        pass

            _lrv_1m_exists = _LRV_DIR_1M.exists()
            _lrv_5m_exists = _LRV_DIR_5M.exists() if _LRV_DIR_5M else False
            print(f"[転換] 1分足DIR: {_LRV_DIR_1M} (存在: {_lrv_1m_exists})", flush=True)
            print(f"[転換] 5分足DIR: {_LRV_DIR_5M} (存在: {_lrv_5m_exists})", flush=True)
            if not _lrv_1m_exists and not _lrv_5m_exists:
                print("[転換] [WARN] 1分足・5分足データディレクトリが見つかりません。転換トレード生成スキップ。", flush=True)
            _lrv_cache: dict = {}

            def _lrv_yf2jq_e(sym: str) -> str:
                return sym.upper().replace(".T", "") + "0"

            def _lrv_load_pkl_e(pkl_path: "_lrv_Path") -> "_lrv_pd.DataFrame | None":
                if not pkl_path.exists():
                    return None
                try:
                    with open(pkl_path, "rb") as _lf:
                        _ldf = _lrv_pickle.load(_lf)
                    _ldf.columns = [c.lower() for c in _ldf.columns]
                    try:
                        import pytz as _lpytz
                        if _ldf.index.tzinfo is None:
                            _ldf.index = _ldf.index.tz_localize("Asia/Tokyo")
                        else:
                            _ldf.index = _ldf.index.tz_convert("Asia/Tokyo")
                    except ImportError:
                        pass
                    return _ldf
                except Exception:
                    return None

            def _lrv_bars_e(sym: str) -> "_lrv_pd.DataFrame | None":
                if sym in _lrv_cache:
                    return _lrv_cache[sym]
                _code = _lrv_yf2jq_e(sym)
                _df2 = _lrv_load_pkl_e(_LRV_DIR_1M / f"{_code}_1m.pkl")
                if _df2 is None and _LRV_DIR_5M:
                    _df2 = _lrv_load_pkl_e(_LRV_DIR_5M / f"{_code}.pkl")
                _lrv_cache[sym] = _df2
                return _df2

            _LRV_BUY_T_E  = _lrv_dtime(9, 9)
            _LRV_SELL_T_E = _lrv_dtime(11, 30)
            _LRV_SLIP_E   = 0.0005
            _LRV_FEE_E    = 0.001
            _LRV_QTY_E    = 100

            def _lrv_sim_e(sym: str, d) -> "tuple | None":
                """Returns (pnl, entry_p, exit_p) or None if data missing.

                買い: BUY_T_E(09:09)以降の最初バーのOPEN
                      1分足=09:09, 5分足=09:10 バー → 厳密一致不要
                売り: SELL_T_E(11:30)以前の最後バーのOPEN (前場引け相当)
                      11:30バーがあればそれ、なければ最終前場バー(11:25等)のOPEN
                """
                _df3 = _lrv_bars_e(sym)
                if _df3 is None:
                    return None
                try:
                    _day3 = _df3[_df3.index.date == d]
                except Exception:
                    return None
                if len(_day3) < 3:
                    return None
                _bar_ts = [_t3.time() for _t3 in _day3.index]
                # 買い: 09:09以降の最初バー
                _bp = None
                for _i3, _bt3 in enumerate(_bar_ts):
                    if _bt3 >= _LRV_BUY_T_E:
                        _bp = float(_day3.iloc[_i3]["open"])
                        break
                # 売り: 11:30以前の最後バー(後場バーを除く)
                _sp = None
                for _i3 in range(len(_bar_ts) - 1, -1, -1):
                    if _bar_ts[_i3] <= _LRV_SELL_T_E:
                        _sp = float(_day3.iloc[_i3]["open"])
                        break
                if not _bp or not _sp:
                    return None
                _be2 = _bp * (1 + _LRV_SLIP_E)
                _se2 = _sp * (1 - _LRV_SLIP_E)
                return ((_se2 - _be2) * _LRV_QTY_E
                        - (_be2 + _se2) * _LRV_QTY_E * _LRV_FEE_E,
                        _be2, _se2)

            _lrv_unfl_df = _lrv_all_df[_lrv_all_df["filled"] == 0].copy()
            _lrv_total_rows = len(_lrv_all_df)
            _lrv_unfl_cnt = len(_lrv_unfl_df)
            print(f"[転換] OOS全レコード: {_lrv_total_rows}件 / 未約定(filled=0): {_lrv_unfl_cnt}件", flush=True)
            if _lrv_unfl_cnt > 0 and "bt_score" in _lrv_unfl_df.columns:
                _sc_vals = _lrv_unfl_df["bt_score"].dropna()
                _sc_ge40 = (_sc_vals >= 40).sum()
                _sc_ge30 = (_sc_vals >= 30).sum()
                print(f"[転換] bt_score分布: BT≥40={_sc_ge40}件 / BT≥30={_sc_ge30}件 / 全体={len(_sc_vals)}件", flush=True)
            _lrv_extra = []
            _lrv_no_data_e = 0
            for _, _rw in _lrv_unfl_df.iterrows():
                _res = _lrv_sim_e(_rw["symbol"], _rw["entry_date"])
                if _res is None:
                    _lrv_no_data_e += 1
                    continue
                _pnl3, _ep3, _xp3 = _res
                _sc3 = float(_rw.get("bt_score", 0) or 0)
                if _sc3 >= 80:   _rk3 = "★★★"
                elif _sc3 >= 60: _rk3 = "★★"
                elif _sc3 >= 40: _rk3 = "★"
                else:            _rk3 = "△"
                _ed3 = _rw["entry_date"]
                _ed3_str = _ed3.strftime("%m/%d")
                _lrv_extra.append({
                    "label":          "転換ロング",
                    "color":          "#60a5fa",
                    "symbol":         str(_rw.get("symbol", "")),
                    "name":           str(_rw.get("name", "")),
                    "strategy":       "転換",
                    "score":          _sc3,
                    "rank":           _rk3,
                    "is_wf":          False,
                    "wf_score":       0,
                    "rec_score":      _sc3,
                    "preoos_score":   _sc3,
                    "signal_dt_raw":  _ed3,
                    "bt_type":        "",
                    "entry_d_raw":    _ed3,
                    "exit_d_raw":     _ed3,
                    "entry_time":     "09:09",
                    "pnl":            _pnl3,
                    "reason":         "転換決済",
                    "entry_dt":       _ed3_str,
                    "exit_dt":        _ed3_str,
                    "entry_p":        _ep3,
                    "exit_p":         _xp3,
                    "qty":            _LRV_QTY_E,
                    "hold_days":      0,
                    "days_neg":       0,
                    "days_to_fill":   0,
                    "order_limit":    0,
                    "order_stop":     0,
                    "order_target":   0,
                })
            _na._EXTRA_TRADES = _lrv_extra
            print(f"転換トレードレコード生成: {len(_lrv_extra)}件 "
                  f"(データ不足スキップ: {_lrv_no_data_e}件)", flush=True)
    except Exception as _lrv_exc:
        import traceback as _lrv_tb
        print(f"[WARN] 転換トレードレコード生成エラー: {_lrv_exc}\n"
              f"{_lrv_tb.format_exc()}", flush=True)

# ── 損益タブ HTML: 全設定統合 (_DEFAULT_DAYS) + 期間別 ──────────────────────
# 全設定統合: _all_configs でデフォルト期間を一括集計 → デフォルト表示
_na._PNL_CONFIGS[:] = _all_configs
print(f"損益集計中 (全設定統合・直近{_DEFAULT_DAYS}日 / {len(_all_configs)}設定)...", flush=True)
_all_period_html = _na._tab5_pnl_html(_DEFAULT_DAYS, _args.workers, entry_days=_args.entry_days, skip_timing9=True)
# ⑭寄り確認用に、この全設定統合ペインの取引セット(=損益タブの取引明細と同一)を退避
_oc_report_trades = list(getattr(_na, "_LAST_KPI_TRADES", []) or [])
_phase(f"損益タブ({_DEFAULT_DAYS}日/全設定統合)完了")

# トレンド期間タブの一覧表に損益列を追加（損益計算で得たトレードを使って再生成）
if _market_tab2_html:
    try:
        _mkt_trades = getattr(_na, "_LAST_KPI_TRADES", None)
        if _mkt_trades:
            _market_tab2_html = _na._tab2_trend_html(
                _na_close, _na_trend, _na_periods, _na_years, trades=_mkt_trades)
            print("トレンド期間タブに損益列を追加", flush=True)
    except Exception as _e2:
        print(f"[WARN] トレンド期間タブ損益列スキップ: {_e2}", flush=True)

# ── 最大保有日数比較セクション（⑪⑫タブ）日付跨ぎキャッシュ付き ──────────────────
# 保有期間比較・押し目買い比較は長期窓の構造分析で日々ほとんど変わらないため、
# 日付が変わっても最新の既存キャッシュを再利用する (再計算しない)。
# 強制再計算は --recalc-analysis のみ。--force(シグナル/HTML更新)では再計算しない。
import pickle as _mhpk
from pathlib import Path as _MHP
_mh_cache_dir  = _MHP(".holdout_bt_cache")
_mh_cache_dir.mkdir(exist_ok=True)
# short/設定ごとに分けるため suffix (_cache_short = _short + 設定sig) を含める
_ANALYSIS_STALE_DAYS = 30   # これより古い場合のみ自動リフレッシュ

def _latest_analysis_cache(prefix: str):
    """prefix にマッチする最新キャッシュ(日付跨ぎ)を返す。
    無い / _ANALYSIS_STALE_DAYS より古い / --recalc-analysis なら None。"""
    if getattr(_args, "recalc_analysis", False):
        return None
    # 日付(YYYY...)始まりのみに限定。prefix="openconfirm" が
    # "openconfirm_short_*" を誤って拾う(ロングがショートのキャッシュを読む)のを防ぐ。
    cands = sorted(_mh_cache_dir.glob(f"{prefix}_[0-9]*.pkl"), reverse=True)
    if not cands:
        return None
    newest = cands[0]
    # ファイル名末尾の YYYY-MM-DD を取り出して鮮度判定
    try:
        _dstr = newest.stem.rsplit("_", 1)[-1]
        _fdate = datetime.strptime(_dstr, "%Y-%m-%d").date()
        if (TODAY - _fdate).days > _ANALYSIS_STALE_DAYS:
            print(f"[{prefix}] 最新キャッシュ {newest.name} が"
                  f"{_ANALYSIS_STALE_DAYS}日超で古いため再計算", flush=True)
            return None
    except Exception:
        pass
    return newest

_mh_prefix     = f"maxhold_cmp{_cache_short}"
_mh_cache_file = _mh_cache_dir / f"{_mh_prefix}_{TODAY}.pkl"
# --force 時はキャッシュを使わず再計算する(コード修正=HTML構造の変更を反映するため)。
# --no-force なら日付跨ぎでpklを再利用して高速化。
_mh_reuse      = None if _args.force else _latest_analysis_cache(_mh_prefix)

# 各分析は独立タブ。⑪=保有日数比較のみ / ⑯損切り幅 ⑰寄り付き方向 ⑱下落深さ
# ⑲逆張りショート ⑳保有日数カーブ はそれぞれ別スロットに入れる。
_fade_html = _curve_html = _stopw_html = _opendir_html = _drop_html = _emcmp_html = ""
if _skip_heavy_analysis:
    print("詳細分析タブ群: スキップ"
          + (" (mirror/lss は同日スイープのみ)" if _analysis_only else " (--no-analysis)"), flush=True)
    _mh_html = _mh_cmp_html = ""   # 空にしてスロット未置換(タブは空表示)にする
elif _mh_reuse is not None:
    try:
        _mh_cached   = _mhpk.loads(_mh_reuse.read_bytes())
        _mh_html     = _mh_cached.get("conservative", "")
        _mh_cmp_html = _mh_cached.get("con_agg", "")
        _fade_html   = _mh_cached.get("fade", "")
        _curve_html  = _mh_cached.get("curve", "")
        _stopw_html  = _mh_cached.get("stopwidth", "")
        _opendir_html = _mh_cached.get("opendir", "")
        _drop_html   = _mh_cached.get("drop", "")
        _emcmp_html  = _mh_cached.get("emcmp", "")
        print(f"[最大保有日数比較] キャッシュ再利用(日付跨ぎ可・--no-force): {_mh_reuse.name}", flush=True)
    except Exception:
        _mh_html = _mh_cmp_html = None
else:
    _mh_html = _mh_cmp_html = None

if _mh_html is None:
    _hold_list = [7, 10, 15, 20]
    _na._PNL_CONFIGS[:] = _all_configs
    print(f"最大保有日数比較中 ({_hold_list}, conservative)...", flush=True)
    _mh_html = _na.build_max_hold_comparison_html(_hold_list, _DEFAULT_DAYS, _args.workers, compare_modes=False) or ""
    print(f"最大保有日数比較中 ({_hold_list}, con+agg)...", flush=True)
    _mh_cmp_html = _na.build_max_hold_comparison_html(_hold_list, _DEFAULT_DAYS, _args.workers, compare_modes=True) or ""
    # ⑯ 損切り幅別 成績（独立タブ）
    print("損切り幅別 成績 検証中...", flush=True)
    try:
        _stopw_html = _na.build_stop_width_html(_DEFAULT_DAYS, _args.workers) or ""
    except Exception as _swe:
        print(f"[損切り幅別] 失敗: {_swe}", flush=True)
    # ⑰ 寄り付き方向 予測精度（独立タブ）
    print("寄り付き方向 予測精度 検証中...", flush=True)
    try:
        _opendir_html = _na.build_open_direction_accuracy_html(_DEFAULT_DAYS, _args.workers) or ""
    except Exception as _ode:
        print(f"[寄り付き方向] 失敗: {_ode}", flush=True)
    # ⑱ 下落深さ別 成績（独立タブ）
    print("下落深さ別 成績 検証中...", flush=True)
    try:
        _drop_html = _na.build_drop_entry_html(_DEFAULT_DAYS, _args.workers) or ""
    except Exception as _dee:
        print(f"[下落深さ別] 失敗: {_dee}", flush=True)
    # ⑲ ブレイク逆張りショート(N日手仕舞い)（独立タブ）
    print("ブレイク逆張りショート(N日手仕舞い)検証中...", flush=True)
    try:
        _fade_html = _na.build_fade_short_html(_DEFAULT_DAYS, _args.workers) or ""
    except Exception as _fse:
        print(f"[逆張りショート] 失敗: {_fse}", flush=True)
    # ⑳ 保有日数に対する含み損の変化（独立タブ）
    print("保有日数に対する含み損の変化 検証中...", flush=True)
    try:
        _curve_html = _na.build_holdday_curve_html(_DEFAULT_DAYS, _args.workers) or ""
    except Exception as _hce:
        print(f"[含み損カーブ] 失敗: {_hce}", flush=True)
    # ㉑ 逆指値em比較＋直近取引がどうなるか（独立タブ）
    print("逆指値em比較 検証中...", flush=True)
    try:
        _emcmp_html = _na.build_em_comparison_html(_DEFAULT_DAYS, _args.workers) or ""
    except Exception as _eme:
        print(f"[em比較] 失敗: {_eme}", flush=True)
    try:
        _mh_cache_file.write_bytes(_mhpk.dumps({
            "conservative": _mh_html, "con_agg": _mh_cmp_html,
            "fade": _fade_html, "curve": _curve_html, "stopwidth": _stopw_html,
            "opendir": _opendir_html, "drop": _drop_html, "emcmp": _emcmp_html}))
        print(f"[最大保有日数比較] キャッシュ保存: {_mh_cache_file.name}", flush=True)
    except Exception as _mhe:
        print(f"[最大保有日数比較] キャッシュ保存失敗: {_mhe}", flush=True)

if _mh_html:
    _all_period_html = _all_period_html.replace("<!-- MAXHOLD_SLOT -->", _mh_html, 1)
if _mh_cmp_html:
    _all_period_html = _all_period_html.replace("<!-- MAXHOLD_CMP_SLOT -->", _mh_cmp_html, 1)
if _stopw_html:
    _all_period_html = _all_period_html.replace("<!-- STOPWIDTH_SLOT -->", _stopw_html, 1)
if _opendir_html:
    _all_period_html = _all_period_html.replace("<!-- OPENDIR_SLOT -->", _opendir_html, 1)
if _drop_html:
    _all_period_html = _all_period_html.replace("<!-- DROP_SLOT -->", _drop_html, 1)
if _fade_html:
    _all_period_html = _all_period_html.replace("<!-- FADESHORT_SLOT -->", _fade_html, 1)
if _curve_html:
    _all_period_html = _all_period_html.replace("<!-- HOLDCURVE_SLOT -->", _curve_html, 1)
if _emcmp_html:
    _all_period_html = _all_period_html.replace("<!-- EMCMP_SLOT -->", _emcmp_html, 1)
_phase("最大保有日数比較完了")

# ── ⑬ 押し目指値買い vs 逆指値ブレイク買い 比較 (詳細分析タブ) ──────────────────
# 「約定後すぐ押す」を活かせるか= 押し目を指値で安く買う方が良いかを本レポートの
# 選定銘柄(=_all_configs)の実トレードで比較。BT70以上のフィルタ表も併記。
# ショートモードでは押し目買いの概念が無いためスキップ。
# mirror/lss(指値空売り/逆指値空売り)も「押し目買い」は非該当なのでスキップ(重い)。
if _analysis_only:
    print("押し目買い比較タブ: スキップ (mirror/lss は非該当)", flush=True)
if not _args.short and not _analysis_only:
    _na._PNL_CONFIGS[:] = _all_configs
    _pb_list = [0.3, 0.5, 1.0]
    _pb_prefix = f"pullback_cmp{_cache_short}"
    _pb_cache_file = _mh_cache_dir / f"{_pb_prefix}_{TODAY}.pkl"
    _pb_html = None
    # 日付跨ぎで最新キャッシュを再利用 (再計算は --recalc-analysis のみ)
    _pb_reuse = _latest_analysis_cache(_pb_prefix)
    if _pb_reuse is not None:
        try:
            _pb_html = _mhpk.loads(_pb_reuse.read_bytes()).get("html", "")
            print(f"[押し目買い比較] キャッシュ再利用(日付跨ぎ可): {_pb_reuse.name}", flush=True)
        except Exception:
            _pb_html = None
    if _pb_html is None:
        print(f"押し目買い比較中 ({_pb_list}, conservative)...", flush=True)
        try:
            _pb_html = _na.build_pullback_comparison_html(
                _pb_list, _DEFAULT_DAYS, _args.workers) or ""
        except Exception as _pbe:
            print(f"[押し目買い比較] 失敗: {_pbe}", flush=True)
            _pb_html = ""
        if _pb_html:
            try:
                _pb_cache_file.write_bytes(_mhpk.dumps({"html": _pb_html}))
                print(f"[押し目買い比較] キャッシュ保存: {_pb_cache_file.name}", flush=True)
            except Exception:
                pass
    if _pb_html:
        _all_period_html = _all_period_html.replace(
            "<!-- PULLBACK_CMP_SLOT -->", _pb_html, 1)
    _phase("押し目買い比較完了")

# ── ⑭ 寄り後確認エントリー検証 (詳細分析タブ) 日付跨ぎキャッシュ ─────────────────
# 5分足が必要な重い分析なので、構造分析扱いで日付跨ぎ再利用 (--recalc-analysis で再計算)。
# ショートはロング専用のため即ノート表示。
# データ源(yfinance / pkl何個)をキャッシュキーに含める → pkl検出時に自動で再計算
# (yfinance時の古い結果を使い回さない)。
import analyze_open_confirm_entry as _oc
try:
    # ショート/mirror/lss はロング専用の寄り後確認が非該当 → 5分足探索もスキップ
    _oc_dirs = [] if (_args.short or _analysis_only) else _oc.locate_5m_dirs()
except Exception:
    _oc_dirs = []
_oc_src_tok = (f"pkl{len(_oc_dirs)}" if _oc_dirs
               else ("short" if _args.short else "yf"))
_oc_prefix    = f"openconfirmv12_{_oc_src_tok}{_cache_short}"   # v12: 5分足を信頼源ティア優先(quarantine/J-Quants>minute_5m)で読み込み・混ぜない
_oc_cache_file = _mh_cache_dir / f"{_oc_prefix}_{TODAY}.pkl"
# 本日分キャッシュがあれば再利用(=同日2回目以降スキップ)。無ければ再計算
# (=5分足自動更新後の最新データを反映)。翌日は自動的に作り直す。
_oc_html = None
if _oc_cache_file.exists():
    try:
        _oc_html = _mhpk.loads(_oc_cache_file.read_bytes()).get("html", "")
        print(f"[寄り確認] 本日分キャッシュ再利用(2回目以降スキップ): {_oc_cache_file.name}", flush=True)
    except Exception:
        _oc_html = None
# キャッシュが無い場合のみ計算(=一度計算したら以降スキップ)。①②寄り確認 + ③フェア版。
# mirror/lss はロング専用の寄り後確認が非該当 → 計算せずスキップ(最大の時短ポイント)。
if _analysis_only:
    print("寄り確認タブ: スキップ (mirror/lss は非該当)", flush=True)
    _oc_html = ""
elif _oc_html is None:
    print(f"寄り確認+フェア版 検証中 (初回のみ・5分足源: {_oc_src_tok})...", flush=True)
    try:
        _oc_html = _oc.build_html(minute_dir=_args.minute_dir,
                                  is_short=_args.short,
                                  trades=None if _args.short else _oc_report_trades) or ""
    except Exception as _oce:
        import traceback as _octb
        print(f"[寄り確認] 失敗: {_oce}\n{_octb.format_exc()}", flush=True)
        _oc_html = ""
    # ③ フェア版(後知恵なし・全シグナル)を追記
    try:
        import analyze_fair_market_entry as _fair
        _oc_html += _fair.build_fair_html(is_short=_args.short,
                                          workers=_args.workers,
                                          minute_dir=_args.minute_dir,
                                          days=0) or ""   # 全期間(5分足が無い日は自動除外)
    except Exception as _fe:
        import traceback as _fetb
        print(f"[フェア版] 失敗: {_fe}\n{_fetb.format_exc()}", flush=True)
    if _oc_html:
        try:
            _oc_cache_file.write_bytes(_mhpk.dumps({"html": _oc_html}))
            print(f"[寄り確認] キャッシュ保存(以降スキップ): {_oc_cache_file.name}", flush=True)
        except Exception:
            pass
if _oc_html:
    _all_period_html = _all_period_html.replace(
        "<!-- OPENCONFIRM_SLOT -->", _oc_html, 1)
_phase("寄り確認検証完了")

# ── ㉓ 同日決済(日計り)向け 損切/利確幅スイープ (mirror/lss 専用・詳細分析) ────────
# 同日決済なのにスイング用の広い幅(sm1.5/tm3.0)を使っている現状を、当たる幅
# (≈0.3〜1ATR)の総当りで最適化するための調査タブ。--no-analysis 時は省略。
if getattr(_na, "_SAMEDAY_SWEEP_TAB", False):
    # 日付跨ぎキャッシュ: 一度計算したら翌日以降も再利用(最適な同日幅は日々ほとんど
    # 変わらない構造分析なので)。--recalc-analysis で強制再計算。キャッシュキーは
    # mirror/lss × 価格帯で分離(異なる銘柄セットの結果を取り違えないため)。
    _sd_mp        = int(_args.max_price) if (_args.max_price and _args.max_price < 100000) else 0
    # v2: グリッドを 0.1〜1.0 に拡張したのでキャッシュキーを更新(旧0.3始まりを無効化)
    _sd_prefix    = f"samedaytpsl2{_cache_short}_mp{_sd_mp}"
    _sd_reuse     = _latest_analysis_cache(_sd_prefix)
    _sd_html = None
    if _sd_reuse is not None:
        try:
            _sd_html = _mhpk.loads(_sd_reuse.read_bytes()).get("html", "")
            print(f"[同日TP/SL] キャッシュ再利用(日付跨ぎ・翌日以降もスキップ): {_sd_reuse.name}", flush=True)
        except Exception:
            _sd_html = None
    if _sd_html is None:
        print("同日TP/SLスイープ 検証中 (mirror/lss・初回のみ)...", flush=True)
        try:
            _sd_html = _na.build_sameday_tpsl_sweep_html(
                _DEFAULT_DAYS, _args.workers,
                inverted=getattr(_na, "_SAMEDAY_SWEEP_INVERTED", True)) or ""
        except Exception as _sde:
            import traceback as _sdtb
            print(f"[同日TP/SL] 失敗: {_sde}\n{_sdtb.format_exc()}", flush=True)
            _sd_html = ""
        if _sd_html:
            try:
                _sd_cache_file = _mh_cache_dir / f"{_sd_prefix}_{_cache_date}.pkl"
                _sd_cache_file.write_bytes(_mhpk.dumps({"html": _sd_html}))
                print(f"[同日TP/SL] キャッシュ保存(以降スキップ): {_sd_cache_file.name}", flush=True)
            except Exception:
                pass
    if _sd_html:
        _all_period_html = _all_period_html.replace(
            "<!-- SAMEDAY_TPSL_SLOT -->", _sd_html, 1)
    _phase("同日TP/SLスイープ完了")

# ── ㉔ 同日TP/SL最適化【5分足・正確】 (mirror/lss 専用・詳細分析) ──────────────────
# 日足スイープの近似を5分足の実際の値動きで正確化。5分足ソースをキャッシュキーに
# 含める(local有無で結果が変わるため)。日付跨ぎキャッシュで翌日以降もスキップ。
if getattr(_na, "_SAMEDAY_5M_TAB", False):
    try:
        import daytrade_data as _dtd
        _5m_src = "local" if (_dtd.DATA_DIR.exists() and any(_dtd.DATA_DIR.glob("*.pkl"))) else "auto"
    except Exception:
        _5m_src = "auto"
    _s5_mp     = int(_args.max_price) if (_args.max_price and _args.max_price < 100000) else 0
    # v2: グローバル解除バグ修正+グリッド変更に伴いキャッシュキー更新(旧の誤グリッド無効化)
    # --tpsl-wide は広いグリッド(別結果)なのでキャッシュキーも分ける(狭/広で混ざらない)。
    _tpsl_wide = getattr(_args, "tpsl_wide", False)
    _wide_tm = [0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 1.5, 2.0, 3.0] if _tpsl_wide else None
    _s5_prefix = f"sameday5m2{_cache_short}_mp{_s5_mp}_{_5m_src}{'_wide' if _tpsl_wide else ''}"
    _s5_reuse  = _latest_analysis_cache(_s5_prefix)
    _s5_html = None
    if _s5_reuse is not None:
        try:
            _s5_html = _mhpk.loads(_s5_reuse.read_bytes()).get("html", "")
            print(f"[5分足TP/SL] キャッシュ再利用(日付跨ぎ): {_s5_reuse.name}", flush=True)
        except Exception:
            _s5_html = None
    if _s5_html is None:
        print(f"5分足TP/SLスイープ 検証中 (mirror/lss・初回のみ・5分足源={_5m_src})...", flush=True)
        try:
            _s5_html = _na.build_sameday_5m_sweep_html(
                _DEFAULT_DAYS, _args.workers,
                is_mirror=bool(_args.mirror), source=_5m_src,
                tm_list=_wide_tm) or ""
        except Exception as _s5e:
            import traceback as _s5tb
            print(f"[5分足TP/SL] 失敗: {_s5e}\n{_s5tb.format_exc()}", flush=True)
            _s5_html = ""
        if _s5_html:
            try:
                (_mh_cache_dir / f"{_s5_prefix}_{_cache_date}.pkl").write_bytes(
                    _mhpk.dumps({"html": _s5_html}))
                print(f"[5分足TP/SL] キャッシュ保存(以降スキップ): {_s5_prefix}_{_cache_date}.pkl", flush=True)
            except Exception:
                pass
    if _s5_html:
        _all_period_html = _all_period_html.replace(
            "<!-- SAMEDAY_5M_SLOT -->", _s5_html, 1)
    _phase("5分足TP/SLスイープ完了")

# ── ⑮ 逆指値の約定率・約定タイミング (詳細分析タブ) 日付跨ぎキャッシュ ─────────────
# em=0で約定挙動はcon/agg共通・5分足不要の軽いバックテスト。日付跨ぎ再利用。
_ft_prefix    = f"filltimingv3{_cache_short}"   # v2: BTフィルタ追加
_ft_reuse     = _latest_analysis_cache(_ft_prefix)
_ft_html = None
if _skip_heavy_analysis:
    print("約定タイミングタブ: スキップ"
          + (" (mirror/lss)" if _analysis_only else " (--no-analysis)"), flush=True)
    _ft_html = ""
elif _ft_reuse is not None:
    try:
        _ft_html = _mhpk.loads(_ft_reuse.read_bytes()).get("html", "")
        print(f"[約定タイミング] キャッシュ再利用(日付跨ぎ可): {_ft_reuse.name}", flush=True)
    except Exception:
        _ft_html = None
if _ft_html is None:
    print("約定タイミング集計中 (初回のみ・以降スキップ)...", flush=True)
    try:
        import analyze_fill_timing as _ft
        _ft_html = _ft.build_html(is_short=_args.short, workers=_args.workers) or ""
    except Exception as _fte:
        import traceback as _fttb
        print(f"[約定タイミング] 失敗: {_fte}\n{_fttb.format_exc()}", flush=True)
        _ft_html = ""
    if _ft_html:
        try:
            (_mh_cache_dir / f"{_ft_prefix}_{TODAY}.pkl").write_bytes(
                _mhpk.dumps({"html": _ft_html}))
        except Exception:
            pass
if _ft_html:
    _all_period_html = _all_period_html.replace(
        "<!-- FILLTIMING_SLOT -->", _ft_html, 1)
_phase("約定タイミング集計完了")

# 期間別: 各期間のconfigs（⑨Rolling/em比較はスキップして高速化）
# preoos_cutoff_days=days を渡してOOS前BTスコア別成績タブを追加
_period_pane_htmls: dict[int, str] = {}
_skip_period_panes = _skip_heavy_analysis
if _skip_period_panes:
    print("期間別パネル(30/60/…日): スキップ。全設定パネルのみ表示", flush=True)
for days in _PNL_PERIODS:
    if _skip_period_panes:
        _period_pane_htmls[days] = ('<p style="color:#64748b;padding:20px">'
                                    '--no-analysis のためスキップ（「全設定」タブを見てください）</p>')
        continue
    cfgs = _period_configs.get(days) or _all_configs
    _na._PNL_CONFIGS[:] = cfgs
    print(f"損益集計中 (直近{days}日 / {len(cfgs)}設定)...", flush=True)
    _skip_preoos = getattr(_args, "no_preoos", False)
    _period_pane_htmls[days] = _na._tab5_pnl_html(
        days, _args.workers, entry_days=_args.entry_days, skip_timing9=True,
        preoos_cutoff_days=None if _skip_preoos else days)
    _phase(f"損益タブ({days}日)完了")

# ── 銘柄詳細タブ HTML (シグナル銘柄ごと) ──────────────────────────────────────
# _last_signals はシグナルタブ生成時に _na 側で設定される
_signal_stocks: list[tuple] = []
_seen_sym: set = set()
# 銘柄 → 本日シグナルが出た戦略の集合 (銘柄詳細タブを「チップのBT=本日の戦略」に絞る)
_sym_today_strats: dict[str, list[str]] = {}
for _sig in _na._last_signals:
    _s = _sig.get("symbol", "")
    if not _s:
        continue
    _st = _sig.get("strategy", "")
    if _st:
        _lst = _sym_today_strats.setdefault(_s, [])
        if _st not in _lst:
            _lst.append(_st)
    if _s not in _seen_sym:
        _seen_sym.add(_s)
        _signal_stocks.append((_s, _sig.get("name", ""), _sig.get("rec_score") or 0))

_sym_tab_nav   = ""
_sym_tab_panes = ""
# 銘柄詳細は「本日シグナルが出た銘柄」だけの相場コンテキストなので --no-analysis でも計算する
# (重い期間別パネル/詳細分析/約定タイミングだけをスキップする)。
for _i, (_sym, _sname, _bt) in enumerate(_signal_stocks):
    _tid     = f"sym_{_sym.replace('.','_')}"
    _active  = "active" if _i == 0 else ""
    _display = "block"  if _i == 0 else "none"
    _short   = _sname[:8] if len(_sname) > 8 else _sname
    _sym_tab_nav += (
        f'<button class="sym-tab-btn {_active}" onclick="switchSymTab(\'{_tid}\')">'
        f'<span style="font-size:0.8rem;font-weight:700">{_sym}</span>'
        f'<br><span style="font-size:0.68rem;color:#94a3b8">{_short}</span>'
        f'<br><span style="font-size:0.7rem;color:#fbbf24">BT:{_bt}</span>'
        f'</button>\n'
    )
    _na._PNL_CONFIGS[:] = _all_configs
    # 本日シグナルが出た戦略(=チップのBT)だけに絞る。取引明細・KPIとも一致させる。
    _today_strats = _sym_today_strats.get(_sym) or None
    print(f"銘柄詳細生成中: {_sym} {_sname} (戦略={_today_strats or 'all'})...", flush=True)
    _sym_pnl = _na._tab5_pnl_html(365, _args.workers, symbol_filter=[_sym],
                                  strategy_filter=_today_strats, skip_timing9=True)
    _sym_tab_panes += (
        f'<div id="{_tid}" class="sym-tab-pane" style="display:{_display}">'
        f'{_sym_pnl}</div>\n'
    )

# 後片付け
_na._PNL_CONFIGS[:] = _all_configs

# ── ニュースモデル スコアテーブル HTML ────────────────────────────────────────
# news_model.json が存在する場合のみ、シグナル銘柄のニューススコアを表示する
_news_score_table_html = ""
if _signal_stocks and not getattr(_args, "no_news", False):
    try:
        _model_path = Path("news_model.json")
        if _model_path.exists():
            print("ニュースモデル スコア計算中...", flush=True)
            from fetch_signal_news import load_and_apply_model as _lam
            from datetime import date as _date_cls
            _today_date = _date_cls.fromisoformat(str(TODAY))
            _ns_rows = ""
            for _ns_sym, _ns_name, _ns_bt in _signal_stocks:
                try:
                    _ns_result = _lam(_ns_sym, _ns_name, _today_date, _ns_bt, skip_news=False)
                    _ns_sent   = _ns_result.get("news_sentiment", 0.0)
                    _ns_cnt    = _ns_result.get("news_count", 0)
                    _ns_pred   = _ns_result.get("predicted_win_prob", 0.5)
                    _ns_score  = _ns_result.get("news_score", 0.0)
                    # 感情スコアの色
                    _sent_clr  = "#4ade80" if _ns_sent > 0.1 else "#f87171" if _ns_sent < -0.1 else "#94a3b8"
                    # 予測勝率の色
                    _pred_clr  = "#4ade80" if _ns_pred >= 0.65 else "#facc15" if _ns_pred >= 0.50 else "#f87171"
                    # BTスコアの色
                    _bt_clr    = "#4ade80" if _ns_bt >= 60 else "#facc15" if _ns_bt >= 40 else "#f87171"
                    _ns_rows += (
                        f'<tr>'
                        f'<td><strong>{_ns_sym}</strong></td>'
                        f'<td style="color:#cbd5e1">{_ns_name}</td>'
                        f'<td style="color:{_bt_clr};font-weight:700">{_ns_bt}</td>'
                        f'<td style="color:{_sent_clr};font-weight:700">{_ns_sent:+.2f}</td>'
                        f'<td style="color:#94a3b8">{_ns_cnt}</td>'
                        f'<td style="color:{_pred_clr};font-weight:700">{_ns_pred*100:.1f}%</td>'
                        f'</tr>\n'
                    )
                except Exception as _ns_e:
                    _ns_rows += (
                        f'<tr>'
                        f'<td><strong>{_ns_sym}</strong></td>'
                        f'<td>{_ns_name}</td>'
                        f'<td>{_ns_bt}</td>'
                        f'<td colspan="3" style="color:#64748b">スコア取得失敗: {_ns_e}</td>'
                        f'</tr>\n'
                    )
            _news_score_table_html = f"""
<div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:16px;margin:0 0 20px">
  <h3 style="color:#93c5fd;font-size:0.95rem;margin:0 0 10px">
    ニュースモデル スコア付きシグナル
    <span style="font-size:0.72rem;color:#64748b;font-weight:normal;margin-left:8px">
      (news_model.json から予測)
    </span>
  </h3>
  <table style="width:100%;border-collapse:collapse;font-size:0.82rem">
    <thead>
      <tr style="border-bottom:1px solid #334155">
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">コード</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">銘柄名</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">BTスコア</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">ニュース感情</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">記事数</th>
        <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-size:0.72rem">予測勝率</th>
      </tr>
    </thead>
    <tbody>{_ns_rows}</tbody>
  </table>
  <p style="color:#64748b;font-size:0.72rem;margin-top:8px">
    予測勝率: モデル訓練済み (news_model.json) のロジスティック回帰による。
    緑≥65%, 黄≥50%, 赤&lt;50%
  </p>
</div>"""
            print(f"ニュースモデルスコア: {len(_signal_stocks)}銘柄 完了", flush=True)
    except Exception as _nst_e:
        print(f"[WARN] ニュースモデルスコア取得失敗: {_nst_e}", flush=True)

# 日経バナー + ニュースモデルスコアをシグナルHTMLの先頭に追加
if _nikkei_banner or _news_score_table_html:
    _sig_html = _nikkei_banner + _news_score_table_html + _sig_html

# ── ニュース・情報タブ HTML ────────────────────────────────────────────────────
if getattr(_args, "no_news", False):
    print("ニュース取得: スキップ (--no-news)", flush=True)
    _news_html = ('<p style="color:#94a3b8;padding:24px">ニュース取得は --no-news で'
                  'スキップされました。ニュースを見るには --no-news を外して再生成してください。</p>')
    _news_tab_ok = False
else:
    try:
        from fetch_signal_news import build_news_html as _build_news_html
        _news_html = _build_news_html(_signal_stocks, workers=_args.workers)
        _news_tab_ok = True
    except Exception as _e:
        print(f"[WARN] ニュース取得スキップ: {_e}", flush=True)
        _news_html  = f'<p style="color:#ef4444;padding:24px">ニュース取得エラー: {_e}</p>'
        _news_tab_ok = False

# ── --symbol 指定時: 銘柄別期間別取引詳細タブ ────────────────────────────────
_sym_detail_tab_btn  = ""
_sym_detail_tab_pane = ""

if _args.symbol:
    _sym_arg = _args.symbol.upper()
    if not _sym_arg.endswith(".T"):
        _sym_arg += ".T"

    _sp_btns  = ""
    _sp_panes = ""
    print(f"指定銘柄 {_sym_arg} の期間別取引詳細生成中...", flush=True)
    for days in _PNL_PERIODS:
        cfgs = _period_configs.get(days) or _all_configs
        _na._PNL_CONFIGS[:] = cfgs
        active  = "active" if days == _DEFAULT_DAYS else ""
        display = "block"  if days == _DEFAULT_DAYS else "none"
        _sp_btns += (
            f'<button class="sp-period-btn {active}" '
            f'onclick="switchSpPeriod({days})">{days}日</button>\n'
        )
        print(f"  直近{days}日...", flush=True)
        _sp_html = _na._tab5_pnl_html(days, _args.workers, symbol_filter=[_sym_arg], skip_timing9=True)
        _sp_panes += (
            f'<div id="sp{days}" class="sp-period-pane" style="display:{display}">'
            f'{_sp_html}</div>\n'
        )

    _na._PNL_CONFIGS[:] = _all_configs

    _sym_detail_tab_btn = (
        f'\n  <button class="ho-outer-btn" onclick="switchHoTab(\'sym_detail\')">'
        f'📌 {_sym_arg}</button>'
    )
    _sym_detail_tab_pane = f"""
<div id="ho-sym_detail" class="ho-outer-pane">
  <p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 12px">
    <strong style="color:#e2e8f0">{_sym_arg}</strong> の期間別取引詳細
  </p>
  <div style="margin:0 0 16px">
    <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
    {_sp_btns}
  </div>
  {_sp_panes}
</div>"""

# ── 期間セレクターのHTML部品 ──────────────────────────────────────────────────
def _period_label(d: int) -> str:
    """期間ボタンの表記。長期は『(◯年)』を添えて分かりやすくする。"""
    if d >= 365:
        yrs = d / 365.0
        return f"{d}日 ({yrs:.0f}年)" if abs(yrs - round(yrs)) < 0.1 else f"{d}日 ({yrs:.1f}年)"
    return f"{d}日"

# デフォルトは「全設定」ボタン（全10設定の合算）
_period_btns = (
    '<button class="ho-period-btn active" data-days="all" '
    f"onclick=\"switchHoPeriod('all')\">全設定 ({_period_label(_DEFAULT_DAYS)})</button>\n"
)
_period_panes = (
    f'<div id="hdall" class="ho-period-pane" style="display:block">'
    f'{_all_period_html}</div>\n'
)
for days in _PNL_PERIODS:
    _period_btns += (
        f'<button class="ho-period-btn" '
        f'data-days="{days}" onclick="switchHoPeriod({days})">{_period_label(days)}</button>\n'
    )
    _period_panes += (
        f'<div id="hd{days}" class="ho-period-pane" style="display:none">'
        f'{_period_pane_htmls[days]}</div>\n'
    )

# ── WF歴史検証タブ（常時表示・複数基準日自動生成）────────────────────────────
from datetime import date as _date_cls, timedelta as _td_wfh

# 最新基準日: --wf-until 指定 or 自動（6ヶ月前）
if _args.wf_until:
    try:
        _wfh_latest = _date_cls.fromisoformat(_args.wf_until)
    except ValueError:
        print(f"[WARN] --wf-until の日付形式が不正: {_args.wf_until} → 6ヶ月前を使用")
        _wfh_latest = _date_cls.today() - _td_wfh(days=183)
else:
    _wfh_latest = _date_cls.today() - _td_wfh(days=183)

# 6ヶ月間隔で N 期間さかのぼる
_skip_wf_history = getattr(_args, "no_wf_history", False) or getattr(_args, "wf_periods", 4) == 0
_wfh_periods = max(1, getattr(_args, "wf_periods", 4))
_wfh_dates = []
_d = _wfh_latest
for _ in range(_wfh_periods):
    _wfh_dates.insert(0, _d)
    _d = _d - _td_wfh(days=183)

_wfh_max_price = getattr(_args, "max_price", 0.0) or 0.0
# --price-ranges 指定時はその最大値を WF検証の上限に使う
if getattr(_args, "price_ranges", None):
    _pr_vals = [float(x.strip()) for x in _args.price_ranges.split(",") if x.strip()]
    if _pr_vals:
        _wfh_max_price = max(_pr_vals)
_wfh_min_price = getattr(_args, "min_price", 0.0) or 0.0
_wfh_universe  = getattr(_args, "wf_universe", None)

if _skip_wf_history:
    print("\nWF歴史検証: スキップ (--no-wf-history または --wf-periods 0)", flush=True)
    _wfh_tab_btn  = ""
    _wfh_tab_pane = ""
else:
    print(f"\nWF歴史検証（クロス期間）: {[str(d) for d in _wfh_dates]}", flush=True)
    try:
        _wfh_body = _na._wf_multi_history_html(
            _wfh_dates, workers=_args.workers,
            max_price=_wfh_max_price,
            min_price=_wfh_min_price,
            universe_path=_wfh_universe,
            force=bool(_args.force),
        )
    except Exception as _wfh_e:
        print(f"[WARN] WF歴史検証エラー: {_wfh_e}")
        import traceback; traceback.print_exc()
        _wfh_body = f'<p style="color:#f87171;padding:20px">WF歴史検証エラー: {_wfh_e}</p>'
    _wfh_tab_btn  = '\n  <button class="ho-outer-btn" onclick="switchHoTab(\'wfh\')">📊 WF歴史検証</button>'
    _wfh_tab_pane = f'\n<div id="ho-wfh" class="ho-outer-pane">\n{_wfh_body}\n</div>'
    print("WF歴史検証タブ生成完了", flush=True)

# ── OOS検証タブ (--oos-until 指定時のみ) ─────────────────────────────────────
_oos_tab_btn  = ""
_oos_tab_pane = ""
if _args.oos_until:
    from datetime import date as _date_cls
    try:
        _oos_until_date = _date_cls.fromisoformat(_args.oos_until)
    except ValueError:
        print(f"[ERROR] --oos-until の日付形式が不正です: {_args.oos_until} (YYYY-MM-DD形式で指定してください)")
        _oos_until_date = None

    if _oos_until_date is not None:
        _oos_since = _oos_until_date - timedelta(days=_args.oos_days)
        print(f"\nOOS検証開始: 訓練前データ {_oos_since} 〜 {_oos_until_date} ({_args.oos_days}日間)")
        _na._PNL_CONFIGS[:] = _all_configs
        try:
            _oos_html_body = _na._oos_pnl_html(_oos_until_date, _args.oos_days, _args.workers)
        except Exception as _oos_e:
            print(f"[WARN] OOS検証エラー: {_oos_e}")
            _oos_html_body = f'<p style="color:#f87171;padding:20px">OOS検証エラー: {_oos_e}</p>'

        _oos_tab_btn = '\n  <button class="ho-outer-btn" onclick="switchHoTab(\'oos\')">🔬 OOS検証</button>'
        _oos_tab_pane = f"""
<div id="ho-oos" class="ho-outer-pane">
  <p style="color:#64748b;font-size:0.82rem;margin:8px 0 16px">
    <strong style="color:#38bdf8">OOS検証（訓練前データ）</strong>
    &nbsp;|&nbsp; 期間: {_oos_since} 〜 {_oos_until_date}（{_args.oos_days}日間）
    &nbsp;|&nbsp; WF訓練開始日より前の純粋OOSデータで検証
  </p>
  {_oos_html_body}
</div>"""
        print("OOS検証タブ生成完了")

# ── フル HTML ─────────────────────────────────────────────────────────────────
_extra_css = """
/* run_signals_holdout_all: outer tab overrides */
.ho-outer-nav {
  display:flex; gap:0; margin:16px 0 0;
  border-bottom:2px solid #1e293b; padding-bottom:0;
}
.ho-outer-btn {
  padding:9px 22px; background:#1e293b; border:none;
  border-radius:6px 6px 0 0; color:#94a3b8;
  cursor:pointer; font-size:0.9rem; transition:all .15s;
  border-bottom:2px solid transparent; margin-bottom:-2px;
}
.ho-outer-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.ho-outer-btn.active { background:#0f172a; color:#60a5fa;
  border-bottom:2px solid #60a5fa; font-weight:700; }
.ho-outer-pane { display:none; padding:12px 0; }
.ho-outer-pane.active { display:block; }

/* 期間セレクター */
.ho-period-btn {
  background:#1e293b; border:1px solid #334155; color:#94a3b8;
  padding:5px 14px; border-radius:4px; cursor:pointer;
  font-size:0.82rem; margin-right:4px; transition:all .2s;
}
.ho-period-btn:hover { color:#e2e8f0; border-color:#64748b; }
.ho-period-btn.active { background:#3b82f6; color:#fff;
  border-color:#3b82f6; font-weight:700; }

/* 銘柄別タブ */
.sym-tab-nav {
  display:flex; flex-wrap:wrap; gap:6px; margin:12px 0 16px;
  padding:10px; background:#0f172a; border-radius:8px;
}
.sym-tab-btn {
  padding:6px 14px; background:#1e293b; border:1px solid #334155;
  color:#e2e8f0; border-radius:6px; cursor:pointer;
  font-size:0.82rem; text-align:center; line-height:1.5;
  transition:all .2s; min-width:90px;
}
.sym-tab-btn:hover { background:#263349; border-color:#64748b; }
.sym-tab-btn.active { background:#1d4ed8; border-color:#3b82f6; }
.sym-tab-pane { display:none; }

/* 指定銘柄 期間セレクター */
.sp-period-btn {
  background:#1e293b; border:1px solid #334155; color:#94a3b8;
  padding:5px 14px; border-radius:4px; cursor:pointer;
  font-size:0.82rem; margin-right:4px; transition:all .2s;
}
.sp-period-btn:hover { color:#e2e8f0; border-color:#64748b; }
.sp-period-btn.active { background:#3b82f6; color:#fff;
  border-color:#3b82f6; font-weight:700; }
.sp-period-pane { display:none; }
"""

_extra_js = """
function switchHoTab(tab) {
  document.querySelectorAll('.ho-outer-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.ho-outer-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('ho-' + tab).classList.add('active');
  (event.target.closest('.ho-outer-btn') || event.target).classList.add('active');
  // 縦長タブ(損益)で下までスクロール→短いタブへ切替時に、スクロール位置が下のまま
  // 残ると内容より下の余白が見えて「空白」に見える。切替時は先頭へ戻す。
  window.scrollTo(0, 0);
}
function switchHoPeriod(days) {
  document.querySelectorAll('.ho-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.ho-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('hd' + days).style.display = 'block';
  (event.target.closest('.ho-period-btn') || event.target).classList.add('active');
}
function switchSymTab(tabId) {
  document.querySelectorAll('.sym-tab-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sym-tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tabId).style.display = 'block';
  (event.target.closest('.sym-tab-btn') || event.target).classList.add('active');
}
function switchSpPeriod(days) {
  document.querySelectorAll('.sp-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sp-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('sp' + days).style.display = 'block';
  (event.target.closest('.sp-period-btn') || event.target).classList.add('active');
}
"""

# 選定基準月(WF as-of)をヘッダに明示する。--output-suffix に base<YYYY-MM> があればそれ、
# 無ければ --long-base、それも無ければ「直近WF(自動)」。実行日(date_str)と混同しないため。
import re as _re_base
_sel_base_disp = "直近WF(自動)"
_m_base = _re_base.search(r"base(\d{4}-\d{2})", (_args.output_suffix or ""))
if _m_base:
    _sel_base_disp = _m_base.group(1)
elif getattr(_args, "lss_proposal", None):
    # lssは提案ファイルで選定。マージ提案なら MERGED_BASES(複数基準月)を優先表示。
    # 単一提案は BASE_MONTH変数 or 旧docstringコメントから基準月を読む。
    try:
        _pt = Path(_args.lss_proposal).read_text(encoding="utf-8")
        _mb = _re_base.search(r'MERGED_BASES\s*=\s*["\']([^"\']+)["\']', _pt)
        _bm = (_re_base.search(r'BASE_MONTH\s*=\s*["\']?(\d{4}-\d{2})', _pt)
               or _re_base.search(r'基準月\(TRAIN終端\):\s*(\d{4}-\d{2})', _pt))
        if _mb and _mb.group(1).strip():
            _sel_base_disp = "マージ " + _mb.group(1).strip()
        elif _bm:
            _sel_base_disp = _bm.group(1)
    except Exception:
        pass
elif getattr(_args, "long_base", None):
    _sel_base_disp = str(_args.long_base)[:7]

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ホールドアウト全設定 シグナル・損益{_mode_label_ja} {date_str} [保有{_bte.MAX_HOLD}日]</title>
<style>
{_na.CSS}
{_extra_css}
</style>
</head>
<body>
<h1>ホールドアウト全設定 シグナル・損益レポート{_mode_label_ja}</h1>
<p class="subtitle">
  <b style="color:#f472b6">選定基準月: {_sel_base_disp}</b> &nbsp;|&nbsp;
  実行日: {date_str} &nbsp;|&nbsp;
  保有期限: {(str(_bte.MAX_HOLD)+'日') if _bte.timecut_enabled() else 'タイムカットなし(目標/損切のみ)'} &nbsp;|&nbsp;
  設定数: {len(_all_configs)}件 &nbsp;|&nbsp;
  workers={_args.workers}
</p>

<div class="ho-outer-nav">
  <button class="ho-outer-btn active" onclick="switchHoTab('sig')">📋 シグナル</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('pnl')">💹 損益</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('sym')">📊 銘柄詳細（{len(_signal_stocks)}件）</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('news')">📰 ニュース・情報</button>{_market_tab_btns}{_sym_detail_tab_btn}{_wfh_tab_btn}{_oos_tab_btn}
</div>

<div id="ho-sig" class="ho-outer-pane active">
{_sig_html}
</div>

<div id="ho-pnl" class="ho-outer-pane">
  <div style="margin:12px 0 16px">
    <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
    {_period_btns}
  </div>
  {_period_panes}
</div>

<div id="ho-sym" class="ho-outer-pane">
  <p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 0">
    本日シグナルが出た {len(_signal_stocks)} 銘柄の過去365日取引履歴（BTスコア降順）
  </p>
  <div class="sym-tab-nav">
{_sym_tab_nav}
  </div>
{_sym_tab_panes}
</div>

<div id="ho-news" class="ho-outer-pane">
{_news_html}
</div>

<div id="ho-mkt1" class="ho-outer-pane">
{_market_tab1_html}
</div>

<div id="ho-mkt2" class="ho-outer-pane">
{_market_tab2_html}
</div>

<div id="ho-mkt3" class="ho-outer-pane">
{_market_tab3_html}
</div>
{_sym_detail_tab_pane}
{_wfh_tab_pane}
{_oos_tab_pane}
<script>
{_na.JS}
{_extra_js}
</script>
</body>
</html>"""

_sym_suffix    = f"_{_sym_arg.replace('.', '')}" if _args.symbol else ""
# モード接頭辞: mirror(ロングミラー) / lss(ロング銘柄ショート) / short(ショート)。
# --both ラッパーが参照するファイル名と一致させる必要がある。
if _args.mirror:
    _short_suffix = "_mirror"
elif _args.long_stop_short:
    _short_suffix = "_lss"
elif getattr(_args, "long_daytrade", False):
    _short_suffix = "_ldt"
elif _args.short:
    _short_suffix = "_short"
else:
    _short_suffix = ""
_output_suffix = _args.output_suffix if _args.output_suffix else ""
out_path = Path(f"signals_holdout_all{_short_suffix}{_sym_suffix}_{date_str}{_output_suffix}.html")
# 巨大HTMLを write_text で一括書き込みすると str→bytes 変換でピークメモリが約2倍
# になり MemoryError になることがある(Python3.14 / 銘柄詳細が多いとき)。
# 1MBずつ分割書き込みしてピークを抑える。
with open(out_path, "w", encoding="utf-8", newline="") as _f:
    for _i in range(0, len(html), 1_000_000):
        _f.write(html[_i:_i + 1_000_000])
del html   # 後続(--both統合ラッパー等)のためにメモリを解放
print(f"\nレポート生成完了: {out_path.resolve()}")
print(_data_freshness_line())

if not _args.no_browser:
    from _open_html import open_html
    open_html(out_path.resolve())

# 単独(非--both)実行でも発注サーバを起動 (--no-serve で無効・既定ON)
_maybe_serve_orders()

