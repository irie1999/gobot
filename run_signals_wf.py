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

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from run_signals import _run_group, build_combined_html
from _signal_funds import collect_fund_rows, fund_html as _fund_html, filter_items

JST = timezone(timedelta(hours=9))

# ── Walk-forward 選定 WATCHLIST (2026-05-19, budget≤50万, conservative) ──
# check_signals_stop.py / check_signals_breakout.py は変更しない
# このスクリプト実行時のみ以下のWATCHLISTを使用する

_STOP_WATCHLIST: list[tuple[str, str, str]] = [
    # ── MACD Walk-forward選定 ──
    ("6310.T", "井関農機",                       "MACD"),  # folds=2 19取引 +137,919 PF5.22 WR71%
    ("6376.T", "日機装",                         "MACD"),  # folds=2 18取引 +115,052 PF4.89 WR72%
    ("1893.T", "五洋建設",                       "MACD"),  # folds=2 18取引 +105,291 PF4.98 WR75%
    ("2264.T", "森永乳業",                       "MACD"),  # folds=2 12取引 +119,918 PF4.82 WR76%
    ("5715.T", "古河機械金属",                   "MACD"),  # folds=2 17取引 +155,479 PF3.99 WR76%
    ("4205.T", "日本ゼオン",                     "MACD"),  # folds=2 12取引  +58,878 PF7.27 WR83%
    ("9042.T", "阪急阪神ホールディングス",       "MACD"),  # folds=2 14取引 +105,338 PF2.89 WR70%
    ("6143.T", "ソディック",                     "MACD"),  # folds=2 19取引  +93,610 PF5.02 WR73%
    ("1938.T", "日本リーテック",                 "MACD"),  # folds=2 20取引 +101,638 PF2.94 WR67%
    ("1417.T", "ミライト・ワン",                 "MACD"),  # folds=3 14取引  +84,230 PF2.60 WR71%
    # ── A7 Walk-forward選定 ──
    ("6103.T", "オークマ",                       "A7"),    # folds=2 12取引 +194,787 PF5.66 WR85%
    ("1814.T", "大末建設",                       "A7"),    # folds=2 12取引 +138,041 PF8.45 WR93%
    ("4506.T", "住友ファーマ",                   "A7"),    # folds=2 12取引 +175,506 PF5.11 WR74%
    ("5803.T", "フジクラ",                       "A7"),    # folds=2 13取引 +194,887 PF5.73 WR73%
    ("6963.T", "ローム",                         "A7"),    # folds=2 11取引 +180,594 PF6.03 WR81%
    ("8227.T", "しまむら",                       "A7"),    # folds=2 10取引 +207,110 PF5.01 WR78%
    ("5831.T", "しずおかフィナンシャルグループ", "A7"),    # folds=2 10取引  +78,891 PF8.35 WR93%
    ("1885.T", "東亜建設工業",                   "A7"),    # folds=2 15取引 +126,628 PF5.48 WR70%
    ("4229.T", "群栄化学工業",                   "A7"),    # folds=2  8取引  +99,240 PF5.42 WR78%
    ("7525.T", "リックス",                       "A7"),    # folds=2 15取引  +92,297 PF4.61 WR72%
    # ── RSI2 Walk-forward選定 ──
    ("7011.T", "三菱重工業",                     "RSI2"),  # folds=2 13取引 +133,875 PF2.53 WR77%
    ("9948.T", "アークス",                       "RSI2"),  # folds=2 12取引  +94,833 PF5.33 WR74%
    ("9069.T", "センコーグループホールディングス","RSI2"),  # folds=2 11取引  +59,500 PF3.43 WR68%
    ("3036.T", "アルコニックス",                 "RSI2"),  # folds=2 10取引  +69,849 PF4.98 WR72%
    ("4658.T", "日本空調サービス",               "RSI2"),  # folds=2 12取引  +55,311 PF5.62 WR67%
    ("8344.T", "山形銀行",                       "RSI2"),  # folds=2  9取引  +49,558 PF5.12 WR81%
    ("8370.T", "紀陽銀行",                       "RSI2"),  # folds=2 12取引  +66,860 PF2.01 WR67%
    ("6754.T", "アンリツ",                       "RSI2"),  # folds=2 10取引  +50,817 PF4.84 WR69%
    ("9882.T", "イエローハット",                 "RSI2"),  # folds=3  9取引  +33,269 PF7.28 WR81%
    ("3612.T", "ワールド",                       "RSI2"),  # folds=2 10取引  +33,758 PF4.81 WR67%
]

_BRK_WATCHLIST: list[tuple[str, str, str]] = [
    # ── DON Walk-forward選定 ──
    ("1515.T", "日鉄鉱業",                       "DON"),  # folds=2 20取引 +228,174 PF4.40 WR78%
    ("8386.T", "百十四銀行",                     "DON"),  # folds=2 21取引 +126,913 PF6.01 WR85%
    ("1980.T", "ダイダン",                       "DON"),  # folds=3 15取引 +113,748 PF3.35 WR74%
    ("1938.T", "日本リーテック",                 "DON"),  # folds=2 19取引 +122,294 PF3.17 WR70%
    ("1975.T", "朝日工業社",                     "DON"),  # folds=2 17取引 +123,746 PF5.20 WR81%
    ("1893.T", "五洋建設",                       "DON"),  # folds=2 23取引 +100,845 PF4.80 WR68%
    ("5482.T", "愛知製鋼",                       "DON"),  # folds=2 18取引 +104,280 PF2.76 WR61%
    ("2325.T", "ＮＪＳ",                         "DON"),  # folds=2 11取引 +109,178 PF4.27 WR71%
    ("3197.T", "すかいらーくホールディングス",   "DON"),  # folds=2 17取引  +86,597 PF2.05 WR61%
    ("1861.T", "熊谷組",                         "DON"),  # folds=2 18取引  +66,468 PF3.24 WR74%
    # ── VOL Walk-forward選定 (ラウンドワン→鴻池運輸に交換) ──
    ("4072.T", "電算システムホールディングス",   "VOL"),  # folds=2  9取引 +229,385 PF4.75 WR50%
    ("6310.T", "井関農機",                       "VOL"),  # folds=2 14取引 +140,412 PF9.05 WR93%
    ("7013.T", "ＩＨＩ",                         "VOL"),  # folds=3 11取引 +164,925 PF5.10 WR72%
    ("8157.T", "都築電気",                       "VOL"),  # folds=2 11取引 +124,801 PF4.86 WR81%
    ("1515.T", "日鉄鉱業",                       "VOL"),  # folds=2 19取引 +167,393 PF3.32 WR74%
    ("9025.T", "鴻池運輸",                       "VOL"),  # folds=2 11取引  +34,406 PF3.92 WR67%
    ("3099.T", "三越伊勢丹ホールディングス",     "VOL"),  # folds=2 13取引 +104,039 PF3.33 WR68%
    ("3946.T", "トーモク",                       "VOL"),  # folds=2 13取引  +92,386 PF5.53 WR69%
    ("5602.T", "栗本鐵工所",                     "VOL"),  # folds=2 11取引  +61,539 PF5.11 WR72%
    ("1861.T", "熊谷組",                         "VOL"),  # folds=2 11取引  +51,610 PF7.38 WR87%
    # ── MOM Walk-forward選定 (オンワードHD除外) ──
    ("3659.T", "ネクソン",                       "MOM"),  # folds=2 11取引 +150,525 PF5.11 WR81%
    ("9412.T", "スカパーＪＳＡＴ",               "MOM"),  # folds=2 19取引 +162,878 PF5.34 WR72%
    ("6752.T", "パナソニック　ホールディングス", "MOM"),  # folds=3 18取引 +139,580 PF5.09 WR66%
    ("7013.T", "ＩＨＩ",                         "MOM"),  # folds=2 19取引 +168,680 PF4.50 WR73%
    ("1938.T", "日本リーテック",                 "MOM"),  # folds=2 20取引  +98,024 PF2.57 WR67%
    ("1961.T", "三機工業",                       "MOM"),  # folds=3 20取引  +90,061 PF3.72 WR66%
    ("6644.T", "大崎電気工業",                   "MOM"),  # folds=2 17取引  +68,391 PF4.57 WR72%
    ("5831.T", "しずおかフィナンシャルグループ", "MOM"),  # folds=2 16取引  +58,874 PF3.13 WR69%
    ("7327.T", "第四北越フィナンシャルグループ", "MOM"),  # folds=2 19取引  +38,756 PF2.16 WR64%
]


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
    parser.add_argument("--funds", action="store_true",
                        help="必要資金集計をHTMLに表示")
    args = parser.parse_args()

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    # ── monkey-patch: このプロセス内でのみ新WATCHLISTを使用 ──
    _stop.WATCHLIST = _STOP_WATCHLIST
    _brk.WATCHLIST  = _BRK_WATCHLIST

    date_label = args.date if args.date else "本日"
    n_total = len(_STOP_WATCHLIST) + len(_BRK_WATCHLIST)
    mode = _stop.TRADING_MODE
    print(f"WF版シグナル統合 開始 ({n_total}銘柄) 確認日: {date_label}  モード: {mode}", flush=True)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_stop = pool.submit(_run_group, _stop, sig_date, args.workers)
        fut_brk  = pool.submit(_run_group, _brk,  sig_date, args.workers)
        stop_items = filter_items(fut_stop.result())
        brk_items  = filter_items(fut_brk.result())

    # シグナル表示
    all_sigs = [
        (it["today_sig"], it["symbol"], it["name"], it["strategy"],
         it.get("score", 0), it.get("rank", "-"))
        for it in stop_items + brk_items
        if it.get("today_sig")
    ]
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
        rows = collect_fund_rows([stop_items, brk_items], args.days)
        fund_block = _fund_html(rows, args.days)

    stop_html = _stop.build_html(stop_items, args.days, date_label)
    brk_html  = _brk.build_html(brk_items,  args.days, date_label)
    combined  = build_combined_html(stop_html, brk_html, fund_html_block=fund_block)

    mode_suffix = "_aggressive" if mode == "aggressive" else ""
    out = Path(f"signals_wf{mode_suffix}_{today_str}.html")
    out.write_text(combined, encoding="utf-8")
    print(f"\nHTML: {out}", flush=True)

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
