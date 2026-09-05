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
from _open_html import open_html
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
# conservative: 2026-06-25 scan, 1000-6000円, MAX_HOLD=7, tm=3.0 (2R)
# aggressive  : 2026-06-25 scan, 1000-6000円, MAX_HOLD=7, tm=1.5 (1.33R)

_STOP_WATCHLIST_CONSERVATIVE: list[tuple[str, str, str]] = [
    # ── MACD Walk-forward選定 (2026-06-25) ──
    ("1975.T", "朝日工業社",                         "MACD"),  # folds=2 16取引 PF3.98 WR69%
    ("4205.T", "日本ゼオン",                         "MACD"),  # folds=2 10取引 PF10.0 WR90%
    ("7322.T", "三十三フィナンシャルグループ",       "MACD"),  # folds=2 14取引 PF8.38 WR86%
    ("2503.T", "キリンホールディングス",             "MACD"),  # folds=2 14取引 PF8.20 WR71%
    ("1961.T", "三機工業",                           "MACD"),  # folds=2 27取引 PF5.78 WR77%
    ("3964.T", "オークネット",                       "MACD"),  # folds=2 18取引 PF8.09 WR92%
    ("6914.T", "オプテックスグループ",               "MACD"),  # folds=2 22取引 PF2.26 WR73%
    ("2579.T", "コカ・コーラ ボトラーズジャパンHD", "MACD"),  # folds=2 17取引 PF2.62 WR74%
    ("6473.T", "ジェイテクト",                       "MACD"),  # folds=2 20取引 PF5.79 WR75%
    ("7245.T", "大同メタル工業",                     "MACD"),  # folds=2 28取引 PF2.04 WR66%
    # ── A7 Walk-forward選定 (2026-06-25) ──
    ("6752.T", "パナソニック ホールディングス",      "A7"),    # folds=2  8取引 PF10.0 WR92%
    ("7003.T", "三井Ｅ＆Ｓ",                         "A7"),    # folds=2 12取引 PF4.69 WR75%
    ("4506.T", "住友ファーマ",                       "A7"),    # folds=2  8取引 PF8.65 WR92%
    ("7520.T", "エコス",                             "A7"),    # folds=2 12取引 PF9.87 WR70%
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),    # folds=2  8取引 PF10.0 WR100%
    ("2288.T", "丸大食品",                           "A7"),    # folds=2  7取引 PF10.0 WR100%
    ("1885.T", "東亜建設工業",                       "A7"),    # folds=2 15取引 PF3.24 WR67%
    ("8544.T", "京葉銀行",                           "A7"),    # folds=2 14取引 PF5.01 WR71%
    ("4043.T", "トクヤマ",                           "A7"),    # folds=2 12取引 PF4.08 WR69%
    ("7322.T", "三十三フィナンシャルグループ",       "A7"),    # folds=2 15取引 PF6.63 WR73%
    # ── RSI2 Walk-forward選定 (2026-06-25) ──
    ("4631.T", "ＤＩＣ",                             "RSI2"),  # folds=2 10取引 PF10.0 WR100%
    ("7921.T", "ＴＡＫＡＲＡ ＆ ＣＯＭＰＡＮＹ",    "RSI2"),  # folds=2  8取引 PF6.33 WR80%
    ("8366.T", "滋賀銀行",                           "RSI2"),  # folds=2  7取引 PF7.11 WR71%
    ("7970.T", "信越ポリマー",                       "RSI2"),  # folds=2 11取引 PF7.61 WR77%
    ("5482.T", "愛知製鋼",                           "RSI2"),  # folds=2 11取引 PF3.98 WR58%
    ("1930.T", "北陸電気工事",                       "RSI2"),  # folds=2  6取引 PF1.91 WR63%
]

_STOP_WATCHLIST_AGGRESSIVE: list[tuple[str, str, str]] = [
    # ── RSI2 Walk-forward選定 (2026-06-25, aggressive) ──
    ("4631.T", "ＤＩＣ",                             "RSI2"),  # folds=2 10取引 PF10.0 WR100%
    ("9960.T", "東テク",                             "RSI2"),  # folds=2 16取引 PF5.71 WR73%
    ("2540.T", "養命酒製造",                         "RSI2"),  # folds=2 10取引 PF10.0 WR70%
    ("8392.T", "大分銀行",                           "RSI2"),  # folds=2  7取引 PF10.0 WR100%
    ("5482.T", "愛知製鋼",                           "RSI2"),  # folds=2 11取引 PF2.63 WR64%
    ("7970.T", "信越ポリマー",                       "RSI2"),  # folds=2 11取引 PF7.66 WR77%
    ("7921.T", "ＴＡＫＡＲＡ ＆ ＣＯＭＰＡＮＹ",    "RSI2"),  # folds=2  8取引 PF5.89 WR80%
    ("2270.T", "雪印メグミルク",                     "RSI2"),  # folds=2 14取引 PF2.63 WR65%
    ("3036.T", "アルコニックス",                     "RSI2"),  # folds=2 11取引 PF3.77 WR73%
    ("6770.T", "アルプスアルパイン",                 "RSI2"),  # folds=2 10取引 PF5.73 WR83%
    # ── MACD Walk-forward選定 (2026-06-25, aggressive) ──
    ("1975.T", "朝日工業社",                         "MACD"),  # folds=2 16取引 PF4.14 WR75%
    ("7988.T", "ニフコ",                             "MACD"),  # folds=2 12取引 PF8.15 WR75%
    ("8795.T", "Ｔ＆Ｄホールディングス",             "MACD"),  # folds=2 14取引 PF5.85 WR79%
    ("7322.T", "三十三フィナンシャルグループ",       "MACD"),  # folds=2 14取引 PF7.86 WR86%
    ("6473.T", "ジェイテクト",                       "MACD"),  # folds=2 20取引 PF7.48 WR75%
    ("4205.T", "日本ゼオン",                         "MACD"),  # folds=2 10取引 PF10.0 WR90%
    ("2503.T", "キリンホールディングス",             "MACD"),  # folds=2 14取引 PF8.95 WR79%
    ("5036.T", "日本ビジネスシステムズ",             "MACD"),  # folds=2 10取引 PF6.07 WR83%
    ("3964.T", "オークネット",                       "MACD"),  # folds=2 18取引 PF7.40 WR92%
    ("7172.T", "ジャパンインベストメントアドバイザー","MACD"),  # folds=2 11取引 PF6.67 WR89%
    # ── A7 Walk-forward選定 (2026-06-25, aggressive) ──
    ("7003.T", "三井Ｅ＆Ｓ",                         "A7"),    # folds=2 12取引 PF5.73 WR81%
    ("6995.T", "東海理化電機製作所",                 "A7"),    # folds=2  7取引 PF10.0 WR100%
    ("6752.T", "パナソニック ホールディングス",      "A7"),    # folds=2  8取引 PF10.0 WR92%
    ("8522.T", "名古屋銀行",                         "A7"),    # folds=2 17取引 PF2.80 WR73%
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),    # folds=2  8取引 PF10.0 WR100%
    ("8387.T", "四国銀行",                           "A7"),    # folds=2  9取引 PF10.0 WR92%
    ("4506.T", "住友ファーマ",                       "A7"),    # folds=2  8取引 PF7.50 WR92%
    ("4229.T", "群栄化学工業",                       "A7"),    # folds=2  7取引 PF6.28 WR90%
    ("7483.T", "ドウシシャ",                         "A7"),    # folds=2  9取引 PF7.69 WR75%
    ("8544.T", "京葉銀行",                           "A7"),    # folds=2 14取引 PF5.23 WR79%
]

_BRK_WATCHLIST_CONSERVATIVE: list[tuple[str, str, str]] = [
    # ── DON Walk-forward選定 (2026-06-25) ──
    ("3659.T", "ネクソン",                           "DON"),  # folds=2 18取引 PF7.53 WR80%
    ("1515.T", "日鉄鉱業",                           "DON"),  # folds=2 23取引 PF2.73 WR74%
    ("8386.T", "百十四銀行",                         "DON"),  # folds=2 35取引 PF2.33 WR66%
    ("8524.T", "北洋銀行",                           "DON"),  # folds=2 23取引 PF1.86 WR61%
    # ── VOL Walk-forward選定 (2026-06-25) ──
    ("1975.T", "朝日工業社",                         "VOL"),  # folds=2  7取引 PF6.20 WR88%
    ("3475.T", "グッドコムアセット",                 "VOL"),  # folds=2  6取引 PF10.0 WR88%
    ("8386.T", "百十四銀行",                         "VOL"),  # folds=2 11取引 PF4.20 WR66%
    ("1952.T", "新日本空調",                         "VOL"),  # folds=2  7取引 PF3.27 WR71%
    ("6473.T", "ジェイテクト",                       "VOL"),  # folds=2 10取引 PF6.06 WR88%
    ("3946.T", "トーモク",                           "VOL"),  # folds=2 10取引 PF2.15 WR62%
    ("6237.T", "イワキポンプ",                       "VOL"),  # folds=2  9取引 PF1.81 WR68%
    # ── MOM Walk-forward選定 (2026-06-25) ──
    ("7242.T", "カヤバ",                             "MOM"),  # folds=2 29取引 PF2.67 WR70%
    ("7157.T", "ライフネット生命保険",               "MOM"),  # folds=2 13取引 PF5.60 WR74%
    ("9702.T", "アイ・エス・ビー",                   "MOM"),  # folds=2 27取引 PF2.30 WR67%
    ("4554.T", "富士製薬工業",                       "MOM"),  # folds=2 25取引 PF1.92 WR57%
    ("4633.T", "サカタインクス",                     "MOM"),  # folds=2  6取引 PF6.20 WR83%
]

_BRK_WATCHLIST_AGGRESSIVE: list[tuple[str, str, str]] = [
    # ── DON Walk-forward選定 (2026-06-25, aggressive) ──
    ("3659.T", "ネクソン",                           "DON"),  # folds=2 18取引 PF6.71 WR80%
    ("1515.T", "日鉄鉱業",                           "DON"),  # folds=2 23取引 PF2.13 WR74%
    ("1961.T", "三機工業",                           "DON"),  # folds=2 37取引 PF1.89 WR73%
    ("8386.T", "百十四銀行",                         "DON"),  # folds=2 35取引 PF2.16 WR74%
    ("6971.T", "京セラ",                             "DON"),  # folds=2 29取引 PF1.65 WR66%
    ("8524.T", "北洋銀行",                           "DON"),  # folds=2 23取引 PF1.74 WR65%
    # ── MOM Walk-forward選定 (2026-06-25, aggressive) ──
    ("7242.T", "カヤバ",                             "MOM"),  # folds=2 29取引 PF6.30 WR90%
    ("4633.T", "サカタインクス",                     "MOM"),  # folds=2  6取引 PF10.0 WR100%
    # ── VOL Walk-forward選定 (2026-06-25, aggressive) ──
    ("7013.T", "ＩＨＩ",                             "VOL"),  # folds=2 11取引 PF5.91 WR82%
    ("3197.T", "すかいらーくホールディングス",       "VOL"),  # folds=2  7取引 PF6.05 WR83%
    ("3475.T", "グッドコムアセット",                 "VOL"),  # folds=2  6取引 PF9.44 WR88%
    ("1975.T", "朝日工業社",                         "VOL"),  # folds=2  7取引 PF5.94 WR88%
    ("1952.T", "新日本空調",                         "VOL"),  # folds=2  7取引 PF2.82 WR71%
    ("6473.T", "ジェイテクト",                       "VOL"),  # folds=2 10取引 PF6.29 WR88%
    ("1976.T", "明星工業",                           "VOL"),  # folds=2  7取引 PF6.08 WR88%
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
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
