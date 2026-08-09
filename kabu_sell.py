"""
kabu_sell.py — kabuステーションで指定銘柄を売る (返済/現物売り)
==============================================================

kabu_buy.py で建てた信用ロングを返済売りで決済する (現物売りも可)。
発注前に建玉を表示して、約定済みかを目視確認できる。

安全設計:
  - デフォルト dry-run。--execute 時のみ実発注。
  - --execute でも接続先は既定デモ(18081)。本番は --prod を明示。
  - 発注前に対象建玉 (数量・取得単価・評価損益) を表示。

使い方:
  python kabu_sell.py --symbol 2586 --prod                    # dry-run (建玉確認)
  python kabu_sell.py --symbol 2586 --prod --execute          # 信用返済 成行売り(全量)
  python kabu_sell.py --symbol 2586 --qty 100 --prod --execute
  python kabu_sell.py --symbol 2586 --type limit --price 300 --prod --execute
  python kabu_sell.py --symbol 2586 --genbutsu --prod --execute   # 現物売り
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta

from kabu_api import (KabuClient, CASH_GENBUTSU, CASH_MARGIN_CLOSE,
                      EXCHANGE_SOR, EXCHANGE_TOKYO_PLUS)

JST = timezone(timedelta(hours=9))


def _show_positions(cli: KabuClient, symbol: str, genbutsu: bool) -> int:
    """対象建玉を表示し、保有数量の合計を返す。"""
    try:
        product = 1 if genbutsu else 2   # 1=現物 2=信用
        positions = [p for p in cli.get_positions(product=product)
                     if str(p.get("Symbol", "")) == str(symbol)]
    except Exception as e:
        print(f"⚠ 建玉取得エラー: {e}")
        return 0
    if not positions:
        print(f"⚠ {symbol} の{'現物' if genbutsu else '信用'}建玉が見つかりません。")
        return 0
    total = 0
    print(f"保有建玉 ({symbol}):")
    for p in positions:
        leaves = int(p.get("LeavesQty") or 0)
        total += leaves
        print(f"  HoldID={p.get('HoldID')} Side={p.get('Side')} "
              f"数量={leaves} 取得単価={p.get('Price')} "
              f"評価損益={p.get('ProfitLoss')}")
    print(f"  合計保有数量: {total} 株")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="kabuステーションで指定銘柄を売る")
    ap.add_argument("--symbol", default="2586", help="銘柄コード (既定2586=フルッタフルッタ)")
    ap.add_argument("--qty", type=int, default=None,
                    help="売却株数 (未指定なら保有全量)")
    ap.add_argument("--type", choices=["market", "limit", "moo"], default="market",
                    help="注文方法: market=成行(既定) / limit=指値 / moo=寄成")
    ap.add_argument("--price", type=float, default=None,
                    help="指値価格 (--type limit のとき必須)")
    ap.add_argument("--genbutsu", action="store_true",
                    help="現物売り (未指定は信用返済)")
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

    cash_margin = CASH_GENBUTSU if args.genbutsu else CASH_MARGIN_CLOSE
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode = "★実発注★" if args.execute else "dry-run (発注なし)"
    type_label = {"market": "成行", "limit": "指値", "moo": "寄成"}[args.type]
    kind = "現物売" if args.genbutsu else "信用返済"

    now = datetime.now(JST)
    print("=" * 60)
    print(f"売り注文  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode}  /  接続先: {env_label}")
    print(f"銘柄: {args.symbol}  {type_label}  {kind}")
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

    # 建玉を表示 (約定確認)
    held = _show_positions(cli, args.symbol, args.genbutsu)
    print()

    qty = args.qty if args.qty is not None else held
    if qty <= 0:
        print("✗ 売却数量が 0 です。建玉がまだ約定していない可能性があります。")
        print("  買い注文が約定してから再実行してください。")
        return 1
    if held and qty > held:
        print(f"⚠ 指定数量 {qty} が保有 {held} を超えています。{held} 株に丸めます。")
        qty = held

    price = cli.get_current_price(args.symbol)
    if price is not None:
        print(f"現在値: {price:,.0f} 円  概算約定額: {price * qty:,.0f} 円 ({qty}株)\n")

    # 発注
    res = cli.send_sell(args.symbol, qty=qty, price=args.price,
                        cash_margin=cash_margin, order_type=args.type)

    if res.get("Result") == 0:
        if args.execute:
            print(f"\n✓ 発注成功 OrderId={res.get('OrderId')}")
        else:
            print("\ndry-run のため実発注していません。--execute で発注します。")
        return 0

    print(f"\n✗ 発注失敗: {res}")
    if res.get("Code") == 100378:
        print("  → 取引時間外で成行が弾かれています。ザラ場(9:00-11:30/12:30-15:00)に"
              "再実行するか、--type moo (寄成) を使ってください。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
