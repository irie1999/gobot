"""
run_short.py  ―  逆指値ショートシグナル専用レポーター
========================================================
check_signals_short.py（MACD_S / A7_S / RSI2_S / DON_S / VOL_S / MOM_S 空売り）
を1コマンドで実行し、HTMLレポートを生成する。

【使い方】
  python run_short.py                     # 全期間(365日) HTMLレポート (conservative)
  python run_short.py --days 90           # 直近90日
  python run_short.py --date 2026-04-08   # 任意日シグナル確認
  python run_short.py --signal-only       # シグナルのみ表示（HTML生成をスキップ）
  python run_short.py --no-browser        # HTML生成のみ（ブラウザ起動しない）
  python run_short.py --aggressive        # 積極利確モード (tm=1.5 目標+4.5%)
  python run_short.py --budget 500000     # 予算50万円 → 株価5,000円以下に絞込
  python run_short.py --max-price 3000    # 株価3,000円以下に絞込
  python run_short.py --workers 8         # 並列数

※ デフォルトは conservative モード (tm=3.0 目標+9%) + budget=500,000円 (≤5,000円/株)
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_short as _short

JST = timezone(timedelta(hours=9))


def _run_group(mod, sig_date, workers: int) -> list[dict]:
    """モジュールの WATCHLIST 全銘柄のバックテスト + シグナル確認を並列実行。"""
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in mod.WATCHLIST}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass
            if done % 8 == 0 or done == len(mod.WATCHLIST):
                print(f"  {done}/{len(mod.WATCHLIST)} 完了", flush=True)

    order = {(s, st): i for i, (s, _, st) in enumerate(mod.WATCHLIST)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    print(f"  シグナル確認中...", flush=True)
    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)
    return all_items


def main() -> None:
    parser = argparse.ArgumentParser(description="逆指値ショートシグナル専用レポーター")
    parser.add_argument("--days",        type=int,   default=365)
    parser.add_argument("--date",        type=str,   default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true",
                        help="シグナルのみ表示（HTML生成をスキップ）")
    parser.add_argument("--workers",     type=int,   default=_short._DEFAULT_WORKERS)
    parser.add_argument("--budget",      type=float, default=500_000,
                        help="予算上限 (円, デフォルト500000 → 株価5,000円以下に絞込)")
    parser.add_argument("--max-price",   type=float, default=None,
                        help="最大株価フィルター (円, --budget より優先)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, 目標+4.5%)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, 目標+9%, デフォルト)")
    args = parser.parse_args()

    # 予算フィルター設定 (モジュール変数に反映)
    if args.max_price is not None:
        _short._MAX_PRICE = args.max_price
    elif args.budget is not None:
        _short._MAX_PRICE = args.budget / 100  # FIXED_QTY=100株

    # --aggressive フラグ対応 (import 後のモード切替)
    if args.aggressive:
        os.environ["TRADING_MODE"] = "aggressive"
        _short.STRATEGY_PARAMS = _short.STRATEGY_PARAMS_AGGRESSIVE
        _short.TRADING_MODE    = "aggressive"

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label       = args.date if args.date else "本日"
    price_filter_str = (f"株価≤{_short._MAX_PRICE:,.0f}円"
                        if _short._MAX_PRICE is not None else "なし")

    print(f"逆指値ショート 開始 ({len(_short.WATCHLIST)}銘柄) "
          f"シグナル確認日: {date_label}  モード: {_short.TRADING_MODE}  "
          f"株価フィルター: {price_filter_str}", flush=True)
    print("⚠ 空売りは損失無限大リスクがあります。逆日歩・規制を確認してください。", flush=True)

    short_items = _run_group(_short, sig_date, args.workers)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 85)
    print(f"  逆指値ショートシグナル  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 85)

    # シグナルをスコア降順で表示
    signals_today = [
        (item, _short.calc_recommend_score(item["period_results"]))
        for item in short_items if item["today_sig"]
    ]
    signals_today.sort(key=lambda x: x[1][0], reverse=True)

    print(f"\n【ショートシグナル ({date_label})】 {len(signals_today)}件")
    if signals_today:
        print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<8} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値(売)':>10} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 115)
        for item, (score, rank) in signals_today:
            sig   = item["today_sig"]
            price = float(sig["order_price"])
            gyaku = " ⚠逆日歩高" if price <= 300 else (" ⚠逆日歩注意" if price <= 500 else "")
            print(f"  {item['symbol']:<12} {item['name']:<20} {item['strategy']:<8}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>10,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点{gyaku}")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    show_days = args.days
    print(f"\nHTMLレポート生成中...", flush=True)
    html = _short.build_html(short_items, show_days, date_label)

    date_suffix  = args.date if args.date else today
    mode_suffix  = f"_{_short.TRADING_MODE}" if _short.TRADING_MODE != "conservative" else ""
    out_path     = Path(f"signals_short{mode_suffix}_{date_suffix}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
