"""
test_close_order.py — 信用返済注文のHoldID手動テスト

kabuステーション PC アプリで確認した建玉番号を --hold-id に渡して
実際に注文が通るか確認するデバッグスクリプト。

使い方:
  python test_close_order.py --symbol 8086 --hold-id <建玉番号> --prod
  python test_close_order.py --symbol 8086 --hold-id <建玉番号> --prod --execute
"""

import argparse
import sys
from kabu_api import KabuClient, CASH_MARGIN_CLOSE

def main():
    ap = argparse.ArgumentParser(description="信用返済 HoldID テスト")
    ap.add_argument("--symbol",  required=True, help="銘柄コード (例: 8086)")
    ap.add_argument("--hold-id", required=True, help="kabu PC アプリで確認した建玉番号")
    ap.add_argument("--qty",     type=int, default=100, help="数量 (デフォルト: 100)")
    ap.add_argument("--prod",    action="store_true", help="本番口座(18080)")
    ap.add_argument("--execute", action="store_true", help="実発注 (なければ dry-run)")
    ap.add_argument("--moo",     action="store_true", help="翌日寄成(MOO)で発注 (なければ成行)")
    args = ap.parse_args()

    env = "本番(18080)" if args.prod else "デモ(18081)"
    mode = "★実発注★" if args.execute else "[dry-run]"
    order_type = "翌日寄成(MOO)" if args.moo else "成行"
    print(f"\n{mode} 信用返済テスト ({env})")
    print(f"  銘柄: {args.symbol}  HoldID: {args.hold_id}  数量: {args.qty}  発注: {order_type}")

    close_pos = [{"HoldID": args.hold_id, "Qty": args.qty}]

    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    cli.connect()
    print(f"  接続成功 ({cli.env_label})")

    if args.moo:
        res = cli.send_sell(args.symbol, qty=args.qty,
                            cash_margin=CASH_MARGIN_CLOSE,
                            order_type="moo",
                            close_positions=close_pos)
    else:
        res = cli.send_sell(args.symbol, qty=args.qty,
                            cash_margin=CASH_MARGIN_CLOSE,
                            order_type="market",
                            close_positions=close_pos)

    print(f"\n結果: {res}")

if __name__ == "__main__":
    main()
