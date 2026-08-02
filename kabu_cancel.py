"""
kabu_cancel.py — kabuステーションの注文を取消す
================================================

OrderId または銘柄コードで注文を特定し、未約定分を取消す。
取消の前に約定状態 (約定済数量) を表示するので、既約定の注文を
誤って成行で出し直して二重発注する事故を防げる。

安全設計:
  - デフォルト dry-run。--execute 時のみ実際に取消。
  - 既に全約定している注文は取消しない (警告のみ)。

使い方:
  python kabu_cancel.py --order-id 20260612A02N92056464 --prod          # dry-run
  python kabu_cancel.py --order-id 20260612A02N92056464 --prod --execute
  python kabu_cancel.py --symbol 2586 --prod --execute   # 銘柄の未約定注文を全取消
"""

from __future__ import annotations

import argparse
import sys

from kabu_api import KabuClient

# kabu 注文 State: 1=待機 2=処理中 3=処理済 4=訂正取消送信中 5=終了
STATE_DONE = 5


def main() -> int:
    ap = argparse.ArgumentParser(description="kabuステーションの注文を取消す")
    ap.add_argument("--order-id", default=None, help="取消す注文ID")
    ap.add_argument("--symbol", default=None,
                    help="この銘柄の未約定注文を全て取消す (--order-id の代わり)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080) (未指定ならデモ18081)")
    ap.add_argument("--execute", action="store_true",
                    help="実際に取消す (未指定なら dry-run)")
    args = ap.parse_args()

    if not args.order_id and not args.symbol:
        print("✗ --order-id か --symbol のどちらかを指定してください。")
        return 1

    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode = "★実取消★" if args.execute else "dry-run (取消なし)"
    print("=" * 60)
    print(f"注文取消  接続先: {env_label}  /  {mode}")
    print("=" * 60)

    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    try:
        cli.connect()
        print(f"✓ 接続成功 ({cli.env_label})\n")
    except Exception as e:
        print(f"✗ 接続失敗: {e}")
        return 1

    try:
        orders = cli.get_orders()
    except Exception as e:
        print(f"✗ 注文一覧取得エラー: {e}")
        return 1

    # 対象注文を絞り込み
    if args.order_id:
        targets = [o for o in orders if o.get("ID") == args.order_id]
        if not targets:
            print(f"✗ OrderId={args.order_id} が見つかりません。")
            return 1
    else:
        targets = [o for o in orders
                   if str(o.get("Symbol", "")) == str(args.symbol)
                   and o.get("State") != STATE_DONE]
        if not targets:
            print(f"対象なし: {args.symbol} に未約定の注文はありません。")
            return 0

    cancelled = 0
    for o in targets:
        oid = o.get("ID")
        state = o.get("State")
        order_qty = o.get("OrderQty")
        cum_qty = o.get("CumQty")     # 約定済数量
        print(f"・OrderId={oid} Symbol={o.get('Symbol')} "
              f"State={state}(5=終了) 注文数={order_qty} 約定済={cum_qty} "
              f"Price={o.get('Price')}")

        if state == STATE_DONE:
            print("  → 既に終了(全約定 or 取消済)。取消しません。")
            continue
        if cum_qty and float(cum_qty) > 0:
            print(f"  ⚠ 一部約定済 ({cum_qty}株)。未約定分のみ取消されます。"
                  "成行で出し直す際は約定済の分を二重に買わないよう注意。")

        res = cli.cancel_order(oid)
        if res.get("Result") == 0:
            print(f"  ✓ 取消{'(dry-run)' if not args.execute else ''}成功")
            cancelled += 1
        else:
            print(f"  ✗ 取消失敗: {res}")

    print(f"\n取消{'予定' if not args.execute else ''}: {cancelled} 件")
    if not args.execute:
        print("dry-run のため実際には取消していません。--execute で取消します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
