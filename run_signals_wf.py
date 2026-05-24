"""
run_signals_wf.py  ―  Walk-forward選定銘柄 シグナルレポート
==============================================================
2026-05-19 scan_walkforward.py --budget 500000 で選定した
Walk-forward銘柄（5,000円以下・conservative）のシグナルを確認する。

既存の check_signals_stop.py / check_signals_breakout.py の
WATCHLIST は変更しない。このスクリプト内でのみ新WATCHLISTを使用。

【使い方】
  python run_signals_wf.py                    # 全期間(365日) HTMLレポート
  python run_signals_wf.py --days 90          # 直近90日
  python run_signals_wf.py --date 2026-05-19  # 任意日シグナル確認
  python run_signals_wf.py --signal-only      # シグナルのみ表示
  python run_signals_wf.py --no-browser       # HTML生成のみ
  python run_signals_wf.py --aggressive       # 積極利確モード

【出力】
  signals_wf_YYYY-MM-DD.html
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop            as _stop
import check_signals_breakout        as _brk
import check_signals_short           as _short
import check_signals_short_breakout  as _sbrk
from run_signals import _run_group, build_combined_html, _auto_update_regime_cache
from _signal_funds import collect_fund_rows, fund_html as _fund_html, filter_items

JST = timezone(timedelta(hours=9))

# ── Walk-forward 選定 WATCHLIST ──────────────────────────────────────────
# check_signals_stop.py / check_signals_breakout.py は変更しない
# このスクリプト実行時のみ以下のWATCHLISTを使用する
# conservative: 2026-05-24 scan, budget≤50万, tm=3.0 (2R)
# aggressive  : 2026-05-24 scan, budget≤50万, tm=2.0 (1.33R)

_STOP_WATCHLIST_CONSERVATIVE: list[tuple[str, str, str]] = [
    # ── MACD Walk-forward選定 (2026-05-24) ──
    ("6376.T", "日機装",                             "MACD"),  # folds=2 18取引 PF5.89 WR76%
    ("1893.T", "五洋建設",                           "MACD"),  # folds=2 18取引 PF4.98 WR75%
    ("2264.T", "森永乳業",                           "MACD"),  # folds=2 13取引 PF4.73 WR70%
    ("5715.T", "古河機械金属",                       "MACD"),  # folds=2 17取引 PF4.29 WR76%
    ("4205.T", "日本ゼオン",                         "MACD"),  # folds=2 12取引 PF7.27 WR83%
    ("8267.T", "イオン",                             "MACD"),  # folds=2 12取引 PF6.14 WR58%
    ("6143.T", "ソディック",                         "MACD"),  # folds=2 19取引 PF5.02 WR73%
    ("3105.T", "日清紡ホールディングス",             "MACD"),  # folds=2 18取引 PF6.71 WR83%
    ("6794.T", "フォスター電機",                     "MACD"),  # folds=2 12取引 PF4.54 WR70%
    ("6644.T", "大崎電気工業",                       "MACD"),  # folds=3 17取引 PF4.84 WR77%
    # ── A7 Walk-forward選定 (2026-05-24) ──
    ("7003.T", "三井Ｅ＆Ｓ",                         "A7"),    # folds=2 14取引 PF5.22 WR82%
    ("6103.T", "オークマ",                           "A7"),    # folds=2 11取引 PF5.36 WR82%
    ("4506.T", "住友ファーマ",                       "A7"),    # folds=2 12取引 PF5.11 WR74%
    ("6963.T", "ローム",                             "A7"),    # folds=2 11取引 PF6.03 WR81%
    ("1814.T", "大末建設",                           "A7"),    # folds=2 12取引 PF8.04 WR89%
    ("8227.T", "しまむら",                           "A7"),    # folds=2 10取引 PF4.89 WR78%
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),    # folds=2 10取引 PF8.45 WR93%
    ("1885.T", "東亜建設工業",                       "A7"),    # folds=2 15取引 PF5.74 WR71%
    ("1662.T", "石油資源開発",                       "A7"),    # folds=2 12取引 PF4.65 WR76%
    ("2540.T", "養命酒製造",                         "A7"),    # folds=2 15取引 PF3.26 WR58%
    # ── RSI2 Walk-forward選定 (2026-05-24) ──
    ("9948.T", "アークス",                           "RSI2"),  # folds=2 12取引 PF5.33 WR74%
    ("7011.T", "三菱重工業",                         "RSI2"),  # folds=2 12取引 PF2.45 WR75%
    ("9069.T", "センコーグループホールディングス",   "RSI2"),  # folds=2 11取引 PF3.64 WR68%
    ("8344.T", "山形銀行",                           "RSI2"),  # folds=2  9取引 PF5.43 WR81%
    ("3036.T", "アルコニックス",                     "RSI2"),  # folds=2 10取引 PF4.98 WR72%
    ("6754.T", "アンリツ",                           "RSI2"),  # folds=2 10取引 PF4.84 WR69%
    ("8358.T", "スルガ銀行",                         "RSI2"),  # folds=2 10取引 PF2.27 WR66%
]

_STOP_WATCHLIST_AGGRESSIVE: list[tuple[str, str, str]] = [
    # ── MACD Walk-forward選定 (2026-05-24, aggressive) ──
    ("9024.T", "西武ホールディングス",               "MACD"),  # folds=3 14取引 PF6.27 WR83%
    ("6323.T", "ローツェ",                           "MACD"),  # folds=2 22取引 PF3.85 WR86%
    ("4390.T", "ＩＰＳ",                             "MACD"),  # folds=2 13取引 PF4.78 WR75%
    ("6264.T", "マルマエ",                           "MACD"),  # folds=2 26取引 PF4.04 WR75%
    ("1515.T", "日鉄鉱業",                           "MACD"),  # folds=3 28取引 PF3.10 WR73%
    ("5832.T", "ちゅうぎんフィナンシャルグループ",   "MACD"),  # folds=2 20取引 PF3.49 WR79%
    ("1975.T", "朝日工業社",                         "MACD"),  # folds=3 20取引 PF4.76 WR79%
    ("4413.T", "ボードルア",                         "MACD"),  # folds=2 17取引 PF2.69 WR70%
    ("8020.T", "兼松",                               "MACD"),  # folds=2 18取引 PF5.09 WR80%
    ("4523.T", "エーザイ",                           "MACD"),  # folds=2 14取引 PF4.48 WR76%
    # ── A7 Walk-forward選定 (2026-05-24, aggressive) ──
    ("7003.T", "三井Ｅ＆Ｓ",                         "A7"),    # folds=2 17取引 PF4.76 WR82%
    ("6963.T", "ローム",                             "A7"),    # folds=2 14取引 PF7.77 WR92%
    ("1814.T", "大末建設",                           "A7"),    # folds=2 16取引 PF7.78 WR91%
    ("6331.T", "三菱化工機",                         "A7"),    # folds=2 12取引 PF7.73 WR89%
    ("8227.T", "しまむら",                           "A7"),    # folds=2 11取引 PF4.94 WR81%
    ("4506.T", "住友ファーマ",                       "A7"),    # folds=2 12取引 PF4.63 WR74%
    ("3132.T", "マクニカホールディングス",           "A7"),    # folds=2 15取引 PF5.14 WR83%
    ("2540.T", "養命酒製造",                         "A7"),    # folds=2 15取引 PF4.91 WR68%
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),    # folds=2 11取引 PF7.92 WR93%
    ("6103.T", "オークマ",                           "A7"),    # folds=2 13取引 PF4.60 WR81%
    # ── RSI2 Walk-forward選定 (2026-05-24, aggressive) ──
    ("7003.T", "三井Ｅ＆Ｓ",                         "RSI2"),  # folds=2 15取引 PF5.10 WR80%
    ("8061.T", "西華産業",                           "RSI2"),  # folds=2 11取引 PF7.18 WR89%
    ("3064.T", "ＭｏｎｏｔａＲＯ",                   "RSI2"),  # folds=2  7取引 PF4.39 WR60%
    ("8344.T", "山形銀行",                           "RSI2"),  # folds=2  9取引 PF7.23 WR89%
    ("8919.T", "カチタス",                           "RSI2"),  # folds=3 13取引 PF2.61 WR76%
    ("6788.T", "日本トリム",                         "RSI2"),  # folds=2 15取引 PF4.37 WR67%
    ("9882.T", "イエローハット",                     "RSI2"),  # folds=2 10取引 PF7.24 WR83%
    ("8600.T", "トモニホールディングス",             "RSI2"),  # folds=2  9取引 PF7.72 WR92%
    ("9948.T", "アークス",                           "RSI2"),  # folds=2 13取引 PF2.06 WR77%
    ("9716.T", "乃村工藝社",                         "RSI2"),  # folds=2 13取引 PF4.86 WR82%
]

_BRK_WATCHLIST_CONSERVATIVE: list[tuple[str, str, str]] = [
    # ── DON Walk-forward選定 (2026-05-24) ──
    ("8386.T", "百十四銀行",                         "DON"),  # folds=3 22取引 PF6.48 WR81%
    ("7609.T", "ダイトロン",                         "DON"),  # folds=2 16取引 PF5.89 WR85%
    ("1980.T", "ダイダン",                           "DON"),  # folds=3 15取引 PF3.35 WR74%
    ("1893.T", "五洋建設",                           "DON"),  # folds=2 23取引 PF4.80 WR68%
    ("1938.T", "日本リーテック",                     "DON"),  # folds=2 19取引 PF3.09 WR70%
    ("1975.T", "朝日工業社",                         "DON"),  # folds=3 17取引 PF5.20 WR81%
    ("8715.T", "アニコムホールディングス",           "DON"),  # folds=2 18取引 PF5.40 WR80%
    ("8544.T", "京葉銀行",                           "DON"),  # folds=3 26取引 PF2.66 WR68%
    ("5482.T", "愛知製鋼",                           "DON"),  # folds=2 18取引 PF2.64 WR61%
    ("3197.T", "すかいらーくホールディングス",       "DON"),  # folds=2 18取引 PF2.08 WR63%
    # ── VOL Walk-forward選定 (2026-05-24) ──
    ("4072.T", "電算システムホールディングス",       "VOL"),  # folds=2  9取引 PF4.75 WR50%
    ("6310.T", "井関農機",                           "VOL"),  # folds=2 15取引 PF6.82 WR82%
    ("7013.T", "ＩＨＩ",                             "VOL"),  # folds=3 11取引 PF5.10 WR72%
    ("8157.T", "都築電気",                           "VOL"),  # folds=2 11取引 PF4.86 WR81%
    ("3946.T", "トーモク",                           "VOL"),  # folds=2 13取引 PF7.30 WR76%
    ("1515.T", "日鉄鉱業",                           "VOL"),  # folds=2 19取引 PF3.47 WR74%
    ("3099.T", "三越伊勢丹ホールディングス",         "VOL"),  # folds=2 13取引 PF3.49 WR68%
    ("4680.T", "ラウンドワン",                       "VOL"),  # folds=2  6取引 PF6.67 WR67%
    ("5602.T", "栗本鐵工所",                         "VOL"),  # folds=2 11取引 PF7.09 WR83%
    ("3197.T", "すかいらーくホールディングス",       "VOL"),  # folds=2 10取引 PF5.04 WR70%
    # ── MOM Walk-forward選定 (2026-05-24) ──
    ("3659.T", "ネクソン",                           "MOM"),  # folds=2 10取引 PF7.55 WR78%
    ("9412.T", "スカパーＪＳＡＴ",                   "MOM"),  # folds=2 21取引 PF5.86 WR72%
    ("6752.T", "パナソニックホールディングス",       "MOM"),  # folds=3 18取引 PF5.19 WR66%
    ("1975.T", "朝日工業社",                         "MOM"),  # folds=2 14取引 PF2.57 WR65%
    ("7013.T", "ＩＨＩ",                             "MOM"),  # folds=2 20取引 PF3.90 WR69%
    ("1938.T", "日本リーテック",                     "MOM"),  # folds=2 20取引 PF2.56 WR67%
    ("6644.T", "大崎電気工業",                       "MOM"),  # folds=2 17取引 PF4.88 WR79%
    ("8802.T", "三菱地所",                           "MOM"),  # folds=2 15取引 PF1.90 WR67%
    ("3388.T", "明治電機工業",                       "MOM"),  # folds=2 12取引 PF3.92 WR44%
    ("7327.T", "第四北越フィナンシャルグループ",     "MOM"),  # folds=2 19取引 PF2.05 WR64%
]

_BRK_WATCHLIST_AGGRESSIVE: list[tuple[str, str, str]] = [
    # ── DON Walk-forward選定 (2026-05-24, aggressive) ──
    ("5803.T", "フジクラ",                           "DON"),  # folds=3 35取引 PF2.57 WR74%
    ("8386.T", "百十四銀行",                         "DON"),  # folds=3 32取引 PF6.48 WR86%
    ("7013.T", "ＩＨＩ",                             "DON"),  # folds=2 20取引 PF4.06 WR70%
    ("1980.T", "ダイダン",                           "DON"),  # folds=2 21取引 PF3.41 WR77%
    ("1975.T", "朝日工業社",                         "DON"),  # folds=3 21取引 PF6.00 WR84%
    ("1803.T", "清水建設",                           "DON"),  # folds=2 26取引 PF2.93 WR68%
    ("5482.T", "愛知製鋼",                           "DON"),  # folds=3 23取引 PF2.75 WR73%
    ("4506.T", "住友ファーマ",                       "DON"),  # folds=3 22取引 PF2.66 WR66%
    ("8715.T", "アニコムホールディングス",           "DON"),  # folds=3 26取引 PF5.40 WR83%
    ("2726.T", "パルグループホールディングス",       "DON"),  # folds=2 14取引 PF2.12 WR52%
    # ── VOL Walk-forward選定 (2026-05-24, aggressive) ──
    ("7013.T", "ＩＨＩ",                             "VOL"),  # folds=2 16取引 PF5.48 WR80%
    ("1975.T", "朝日工業社",                         "VOL"),  # folds=3 12取引 PF5.14 WR82%
    ("6254.T", "野村マイクロ・サイエンス",           "VOL"),  # folds=2 20取引 PF2.05 WR70%
    ("6310.T", "井関農機",                           "VOL"),  # folds=2 18取引 PF5.41 WR80%
    ("3099.T", "三越伊勢丹ホールディングス",         "VOL"),  # folds=2 14取引 PF3.53 WR76%
    ("3946.T", "トーモク",                           "VOL"),  # folds=2 15取引 PF7.37 WR79%
    ("6323.T", "ローツェ",                           "VOL"),  # folds=2 17取引 PF2.52 WR77%
    ("4072.T", "電算システムホールディングス",       "VOL"),  # folds=2 11取引 PF4.14 WR58%
    ("3197.T", "すかいらーくホールディングス",       "VOL"),  # folds=2 11取引 PF7.15 WR82%
    ("1515.T", "日鉄鉱業",                           "VOL"),  # folds=2 22取引 PF2.36 WR73%
    # ── MOM Walk-forward選定 (2026-05-24, aggressive) ──
    ("1515.T", "日鉄鉱業",                           "MOM"),  # folds=2 28取引 PF3.48 WR76%
    ("7242.T", "カヤバ",                             "MOM"),  # folds=2 21取引 PF5.41 WR78%
    ("9412.T", "スカパーＪＳＡＴ",                   "MOM"),  # folds=2 24取引 PF3.19 WR70%
    ("7013.T", "ＩＨＩ",                             "MOM"),  # folds=3 28取引 PF3.78 WR72%
    ("3659.T", "ネクソン",                           "MOM"),  # folds=3 13取引 PF6.75 WR75%
    ("6754.T", "アンリツ",                           "MOM"),  # folds=2 23取引 PF2.24 WR71%
    ("1964.T", "中外炉工業",                         "MOM"),  # folds=2 13取引 PF4.56 WR77%
    ("6140.T", "旭ダイヤモンド工業",                 "MOM"),  # folds=2 18取引 PF2.03 WR72%
    ("8011.T", "三陽商会",                           "MOM"),  # folds=2 12取引 PF4.51 WR78%
    ("1938.T", "日本リーテック",                     "MOM"),  # folds=2 24取引 PF1.85 WR70%
]

import os as _os_wf
_WF_MODE = _os_wf.getenv("TRADING_MODE", "conservative").lower()
_STOP_WATCHLIST = _STOP_WATCHLIST_AGGRESSIVE if _WF_MODE == "aggressive" else _STOP_WATCHLIST_CONSERVATIVE
_BRK_WATCHLIST  = _BRK_WATCHLIST_AGGRESSIVE  if _WF_MODE == "aggressive" else _BRK_WATCHLIST_CONSERVATIVE


def _merge_items(con_items: list, agg_items: list) -> list:
    """
    マージルール:
      - 共通銘柄でシグナルが両方 → aggressive を採用
      - 共通銘柄でシグナルが片方 → そのモードを採用
      - 片方のみの銘柄        → そのモードを採用
    """
    con_map = {(it["symbol"], it["strategy"]): it for it in con_items}
    agg_map = {(it["symbol"], it["strategy"]): it for it in agg_items}
    result = []
    for key in set(con_map) | set(agg_map):
        con_it = con_map.get(key)
        agg_it = agg_map.get(key)
        if con_it and agg_it:
            # 共通銘柄: aggressive優先、ただしaggシグナルなし&conシグナルあり → con採用
            if not agg_it.get("today_sig") and con_it.get("today_sig"):
                result.append(con_it)
            else:
                result.append(agg_it)
        elif con_it:
            result.append(con_it)
        else:
            result.append(agg_it)
    return result


def _run_both(sig_date, workers):
    """Conservative と Aggressive を順番に実行してアイテムリストを返す。"""
    from concurrent.futures import ThreadPoolExecutor

    # --- Conservative ---
    _stop.STRATEGY_PARAMS = _stop.STRATEGY_PARAMS_CONSERVATIVE
    _brk.STRATEGY_PARAMS  = _brk.STRATEGY_PARAMS_CONSERVATIVE
    _stop.WATCHLIST = _STOP_WATCHLIST_CONSERVATIVE
    _brk.WATCHLIST  = _BRK_WATCHLIST_CONSERVATIVE
    with ThreadPoolExecutor(max_workers=4) as pool:
        con_stop = filter_items(pool.submit(_run_group, _stop, sig_date, workers).result())
        con_brk  = filter_items(pool.submit(_run_group, _brk,  sig_date, workers).result())

    # --- Aggressive ---
    _stop.STRATEGY_PARAMS = _stop.STRATEGY_PARAMS_AGGRESSIVE
    _brk.STRATEGY_PARAMS  = _brk.STRATEGY_PARAMS_AGGRESSIVE
    _stop.WATCHLIST = _STOP_WATCHLIST_AGGRESSIVE
    _brk.WATCHLIST  = _BRK_WATCHLIST_AGGRESSIVE
    with ThreadPoolExecutor(max_workers=4) as pool:
        agg_stop = filter_items(pool.submit(_run_group, _stop, sig_date, workers).result())
        agg_brk  = filter_items(pool.submit(_run_group, _brk,  sig_date, workers).result())

    return _merge_items(con_stop, agg_stop), _merge_items(con_brk, agg_brk)


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward選定銘柄 シグナルレポート")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true")
    mode_group.add_argument("--conservative", action="store_true")
    mode_group.add_argument("--merged",       action="store_true",
                            help="conservative+aggressiveを統合（共通銘柄は両方シグナル→agg、片方→そのモード）")
    parser.add_argument("--funds", action="store_true",
                        help="必要資金集計をHTMLに表示")
    args = parser.parse_args()
    _auto_update_regime_cache(args.workers)

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    from concurrent.futures import ThreadPoolExecutor

    if args.merged:
        mode = "merged"
        print(f"WF版シグナル統合 開始 (merged) 確認日: {date_label}", flush=True)
        print(f"  Conservative ({len(_STOP_WATCHLIST_CONSERVATIVE)+len(_BRK_WATCHLIST_CONSERVATIVE)}銘柄) +"
              f" Aggressive ({len(_STOP_WATCHLIST_AGGRESSIVE)+len(_BRK_WATCHLIST_AGGRESSIVE)}銘柄)", flush=True)
        stop_items, brk_items = _run_both(sig_date, args.workers)
        with ThreadPoolExecutor(max_workers=2) as pool:
            short_items = filter_items(pool.submit(_run_group, _short, sig_date, args.workers).result())
            sbrk_items  = filter_items(pool.submit(_run_group, _sbrk,  sig_date, args.workers).result())
    else:
        # ── monkey-patch: このプロセス内でのみ新WATCHLISTを使用 ──
        _stop.WATCHLIST = _STOP_WATCHLIST
        _brk.WATCHLIST  = _BRK_WATCHLIST
        mode = _stop.TRADING_MODE
        n_total = len(_STOP_WATCHLIST) + len(_BRK_WATCHLIST) + len(_short.WATCHLIST) + len(_sbrk.WATCHLIST)
        print(f"WF版シグナル統合 開始 ({n_total}銘柄) 確認日: {date_label}  モード: {mode}", flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            stop_items  = filter_items(pool.submit(_run_group, _stop,  sig_date, args.workers).result())
            brk_items   = filter_items(pool.submit(_run_group, _brk,   sig_date, args.workers).result())
            short_items = filter_items(pool.submit(_run_group, _short, sig_date, args.workers).result())
            sbrk_items  = filter_items(pool.submit(_run_group, _sbrk,  sig_date, args.workers).result())

    # シグナル表示
    all_sigs = []
    for it in stop_items + brk_items + short_items + sbrk_items:
        sig = it.get("today_sig")
        if not sig:
            continue
        mod = _short if it["strategy"] in ("MACD_S", "A7_S", "RSI2_S") else \
              _sbrk  if it["strategy"] in ("DON_S", "MOM_S", "GAP_S") else \
              _brk   if it["strategy"] in ("DON", "VOL", "MOM") else _stop
        score, rank = mod.calc_recommend_score(it["period_results"])
        all_sigs.append((sig, it["symbol"], it["name"], it["strategy"], score, rank))
    all_sigs.sort(key=lambda x: x[4], reverse=True)

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"\n【シグナル ({date_label})】 {len(all_sigs)}件")
    for sig, sym, name, strat, score, rank in all_sigs:
        print(f"  {sym} {name} [{strat}] {rank}{score}点  "
              f"逆指値:{sig['order_price']:,.0f}  損切:{sig['stop_price']:,.0f}  "
              f"目標:{sig['target_price']:,.0f}")

    if args.signal_only:
        return

    # HTML生成
    fund_block = ""
    if args.funds:
        rows = collect_fund_rows([stop_items, brk_items, short_items, sbrk_items], args.days)
        fund_block = _fund_html(rows, args.days)

    _cmd = f"python run_signals_wf.py --{mode}"
    stop_html  = _stop.build_html(stop_items,   args.days, date_label, run_cmd=_cmd)
    brk_html   = _brk.build_html(brk_items,     args.days, date_label, run_cmd=_cmd)
    short_html = _short.build_html(short_items,  args.days, date_label, run_cmd=_cmd)
    sbrk_html  = _sbrk.build_html(sbrk_items,   args.days, date_label, run_cmd=_cmd)
    combined   = build_combined_html(stop_html, brk_html, short_html, sbrk_html, fund_html_block=fund_block)

    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    out = Path(f"signals_wf{mode_suffix}_{today_str}.html")
    out.write_text(combined, encoding="utf-8")
    print(f"\nHTML: {out}", flush=True)

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
