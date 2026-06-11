"""
kabu_send_signals.py — 今日の逆指値シグナルを kabuステーションに発注する
========================================================================

run_signals / check_signals_* と同じロジックで本日のシグナルを収集し
(`forward_test.collect_today_signals` を再利用)、各シグナルの逆指値注文価格
(order_price) をそのまま kabuステーションの **逆指値買い** 注文として発注する。

損切りは CLAUDE.md §16 の既定 (close 方式) で運用する想定のため、ここでは
損切り逆指値は同時に出さない (引け判定は close_stop_guard.py が担当)。
ザラ場で損切り逆指値も置く intraday 運用にしたい場合は --with-stop を付ける。

【安全設計】
  - デフォルト dry-run。--execute 時のみ実発注。
  - --execute でも接続先は既定デモ(18081)。本番は --prod を明示。
  - --budget で 1 銘柄あたりの投入上限を指定すると、高すぎる銘柄をスキップ。

使い方:
  python kabu_send_signals.py                  # dry-run: 発注内容を表示
  python kabu_send_signals.py --execute        # デモ口座に逆指値買いを発注
  python kabu_send_signals.py --execute --prod # 本番口座に発注 (要明示)
  python kabu_send_signals.py --with-stop      # 損切り逆指値も同時発注 (intraday運用)
  python kabu_send_signals.py --budget 600000  # 1銘柄60万円以下のみ
  python kabu_send_signals.py --aggressive     # aggressive モードでシグナル収集
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

# ── TRADING_MODE を import 前に設定 (各スクリプトと同じ作法) ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

from kabu_api import KabuClient, CASH_GENBUTSU  # noqa: E402

JST = timezone(timedelta(hours=9))
FIXED_QTY = 100   # backtest_limit_entry.FIXED_QTY と同じ前提


def main() -> int:
    ap = argparse.ArgumentParser(
        description="今日の逆指値シグナルを kabuステーションに発注する")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--with-stop", action="store_true",
                    help="損切り逆指値も同時に出す (intraday 運用)")
    ap.add_argument("--budget", type=float, default=None,
                    help="1銘柄あたりの投入上限(円)。order_price×100 がこれを超える銘柄はスキップ")
    ap.add_argument("--margin", action="store_true",
                    help="信用新規で発注する (未指定なら現物)")
    ap.add_argument("--aggressive", action="store_true",
                    help="aggressive モードでシグナル収集")
    ap.add_argument("--conservative", action="store_true",
                    help="conservative モードでシグナル収集 (既定)")
    args = ap.parse_args()

    # collect_today_signals は import 時に TRADING_MODE を読むので遅延 import
    from forward_test import collect_today_signals

    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"
    cash_margin = 2 if args.margin else CASH_GENBUTSU  # 2=信用新規

    now = datetime.now(JST)
    print("=" * 60)
    print(f"逆指値シグナル発注  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label}  /  接続先: {env_label}  /  "
          f"{'信用新規' if args.margin else '現物'}")
    print("=" * 60)

    print("本日のシグナルを収集中...")
    signals = collect_today_signals()
    if not signals:
        print("本日のシグナルなし。終了します。")
        return 0

    # 予算フィルター
    if args.budget:
        kept = []
        for s in signals:
            cost = s["order_price"] * FIXED_QTY
            if cost > args.budget:
                print(f"  skip {s['symbol']} {s['name']}: "
                      f"必要資金 {cost:,.0f}円 > 予算 {args.budget:,.0f}円")
                continue
            kept.append(s)
        signals = kept

    if not signals:
        print("予算条件を満たすシグナルなし。終了します。")
        return 0
    print(f"発注対象シグナル: {len(signals)} 件\n")

    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    if args.execute:
        try:
            cli.connect()
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            return 1

    ok = 0
    for s in signals:
        sym, name = s["symbol"], s["name"]
        order_p, stop_p = s["order_price"], s["stop_price"]
        print(f"・{sym} {name} [{s['strategy']}/{s['family']}] "
              f"逆指値買い @≥{order_p:.0f} 損切り {stop_p:.0f}")
        res = cli.send_stop_buy(sym, qty=FIXED_QTY, trigger_price=order_p,
                                cash_margin=cash_margin)
        if res.get("Result") == 0:
            ok += 1
        if args.with_stop:
            cli.send_stop_sell(sym, qty=FIXED_QTY, trigger_price=stop_p,
                               cash_margin=cash_margin)

    print(f"\nエントリー発注完了: {ok}/{len(signals)} 件成功")
    if not args.execute:
        print("dry-run のため実発注していません。--execute で発注します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
