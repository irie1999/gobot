"""
kabu_buy.py — kabuステーションで指定銘柄を買う (単発エントリー)
==============================================================

逆指値シグナル運用とは別に、手動で 1 銘柄を成行/指値/寄成で買うためのスクリプト。
信用新規 (既定) / 現物 を切替可能。

安全設計:
  - デフォルト dry-run。--execute 時のみ実発注。
  - --execute でも接続先は既定デモ(18081)。本番は --prod を明示。
  - 発注前に現在値と概算約定額を表示。

使い方:
  python kabu_buy.py --symbol 2586                       # dry-run (フルッタフルッタ)
  python kabu_buy.py --symbol 2586 --qty 100 --prod --execute   # 本番で成行100株(信用)
  python kabu_buy.py --symbol 2586 --type limit --price 250 --prod --execute
  python kabu_buy.py --symbol 2586 --type moo --prod --execute  # 寄成 (時間外でも発注可)
  python kabu_buy.py --symbol 2586 --genbutsu               # 現物で買う
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta

from kabu_api import (KabuClient, CASH_GENBUTSU, CASH_MARGIN_OPEN,
                      EXCHANGE_SOR, EXCHANGE_TOKYO_PLUS)

JST = timezone(timedelta(hours=9))


def main() -> int:
    ap = argparse.ArgumentParser(description="kabuステーションで指定銘柄を買う")
    ap.add_argument("--symbol", default="2586", help="銘柄コード (既定2586=フルッタフルッタ)")
    ap.add_argument("--qty", type=int, default=100, help="株数 (既定100)")
    ap.add_argument("--type", choices=["market", "limit", "moo"], default="market",
                    help="注文方法: market=成行(既定) / limit=指値 / moo=寄成")
    ap.add_argument("--price", type=float, default=None,
                    help="指値価格 (--type limit のとき必須)")
    ap.add_argument("--genbutsu", action="store_true",
                    help="現物で買う (未指定は信用新規)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run)")
    ap.add_argument("--exchange", type=int, default=EXCHANGE_SOR,
                    choices=[EXCHANGE_SOR, EXCHANGE_TOKYO_PLUS],
                    help="発注の市場コード 9=SOR(既定) / 27=東証＋ "
                         "(1=東証は2026/02で廃止)")
    args = ap.parse_args()

    if args.type == "limit" and args.price is None:
        print("✗ --type limit には --price が必要です。")
        return 1

    cash_margin = CASH_GENBUTSU if args.genbutsu else CASH_MARGIN_OPEN
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode = "★実発注★" if args.execute else "dry-run (発注なし)"
    type_label = {"market": "成行", "limit": "指値", "moo": "寄成"}[args.type]
    kind = "現物" if args.genbutsu else "信用新規"

    now = datetime.now(JST)
    print("=" * 60)
    print(f"買い注文  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode}  /  接続先: {env_label}")
    print(f"銘柄: {args.symbol}  株数: {args.qty}  {type_label}  {kind}")
    if args.type == "limit":
        print(f"指値価格: {args.price:,.0f} 円")
    print("=" * 60)

    cli = KabuClient(prod=args.prod, dry_run=not args.execute,
                     order_exchange=args.exchange)
    try:
        cli.connect()
        print(f"✓ 接続成功 ({cli.env_label})  発注市場コード={args.exchange}"
              f"{'(SOR)' if args.exchange == EXCHANGE_SOR else '(東証＋)'}\n")
    except Exception as e:
        print(f"✗ 接続失敗: {e}")
        return 1

    # 現在値と概算額を表示
    price = cli.get_current_price(args.symbol)
    if price is not None:
        print(f"現在値: {price:,.0f} 円  概算約定額: {price * args.qty:,.0f} 円 "
              f"({args.qty}株)")
    else:
        print("⚠ 現在値を取得できませんでした (時間外/銘柄登録待ち等)。続行します。")
    print()

    # 発注 (SOR 固定。東証＋(27)は手数料がかかるため自動フォールバックしない)
    res = cli.send_buy(args.symbol, qty=args.qty, price=args.price,
                       cash_margin=cash_margin, order_type=args.type)

    if res.get("Result") == 0:
        if args.execute:
            print(f"\n✓ 発注成功 OrderId={res.get('OrderId')} "
                  f"(市場コード={cli.order_exchange})")
            print("  注文一覧/建玉は kabuステーション or get_orders()/get_positions() で確認できます。")
        else:
            print("\ndry-run のため実発注していません。--execute で発注します。")
        return 0

    print(f"\n✗ 発注失敗: {res}")
    if res.get("Code") == 100378:
        print("  → 市場コード(Exchange)起因の拒否です。SOR(9)で弾かれる場合は"
              "ザラ場(9:00-11:30/12:30-15:00)に再実行してください。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
