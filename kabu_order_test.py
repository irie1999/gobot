"""
kabu_order_test.py — 実発注が通るかの安全な確認テスト
=======================================================

「実際に発注できるか」を、約定リスクなしで確認するためのスクリプト。

仕組み:
  1. 指定銘柄の現在値を取得
  2. 現在値より十分高い価格で逆指値買い (trigger_price = 現在値 × 1.5) を実発注
     → 逆指値買いは「trigger 以上で成行」なので、はるか上の価格なら即約定しない
  3. 注文一覧 (/orders) に出ているか確認
  4. その注文を取消 (/cancelorder)
  5. 取消後に注文一覧を再確認

これで「発注 → 約定せず板に乗る → 取消」までを実際の API で確認できる。
ポジションは持たない (1株も買わない)。

使い方:
  python kabu_order_test.py --prod                 # 本番(18080) で確認 (要 --execute)
  python kabu_order_test.py --prod --execute       # 実際に発注→取消を行う
  python kabu_order_test.py --prod --execute --symbol 7203 --qty 100

安全設計:
  - --execute を付けないと dry-run (内容表示のみ。実発注しない)
  - 既定 qty=100。trigger は現在値×1.5 なので即約定しない
"""

from __future__ import annotations

import argparse
import sys
import time

from kabu_api import KabuClient, CASH_GENBUTSU, CASH_MARGIN_OPEN


def main() -> int:
    ap = argparse.ArgumentParser(description="kabu 実発注の安全確認テスト")
    ap.add_argument("--prod", action="store_true", help="本番(18080)。未指定はデモ(18081)")
    ap.add_argument("--execute", action="store_true", help="実際に発注→取消する (未指定は dry-run)")
    ap.add_argument("--symbol", default="7203", help="テスト銘柄コード (既定: 7203 トヨタ)")
    ap.add_argument("--qty", type=int, default=100, help="株数 (既定100)")
    ap.add_argument("--margin", action="store_true", help="信用新規でテスト (既定: 現物)")
    args = ap.parse_args()

    cash_margin = CASH_MARGIN_OPEN if args.margin else CASH_GENBUTSU
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode = "★実発注→取消★" if args.execute else "dry-run (発注なし)"

    print("=" * 60)
    print(f"kabu 実発注テスト  接続先: {env_label}  /  {mode}")
    print(f"銘柄: {args.symbol}  株数: {args.qty}  "
          f"{'信用新規' if args.margin else '現物'}")
    print("=" * 60)

    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    try:
        cli.connect()
        print(f"✓ 接続成功 ({cli.env_label})\n")
    except Exception as e:
        print(f"✗ 接続失敗: {e}")
        return 1

    # 1) 現在値取得
    print("[1] 現在値を取得...")
    price = cli.get_current_price(args.symbol)
    if price is None:
        print(f"  ✗ {args.symbol} の現在値を取得できませんでした。")
        print("    銘柄コードを確認するか、市場が開いているか確認してください。")
        return 1
    print(f"  {args.symbol} 現在値: {price:,.0f} 円")

    # 2) 約定しない高さで逆指値買いを発注 (現在値×1.5)
    trigger = round(price * 1.5)
    print(f"\n[2] 逆指値買いを発注 (trigger=現在値×1.5={trigger:,} 円)")
    print("    ※ 現在値よりはるかに高いので即約定しません")
    res = cli.send_stop_buy(args.symbol, qty=args.qty,
                            trigger_price=trigger, cash_margin=cash_margin)
    # 取引時間外だと発火後=成行が弾かれる(Code 100378)。指値発火でリトライ。
    if res.get("Result") != 0 and res.get("Code") == 100378:
        print("  ↻ 成行が市場に拒否されました(時間外?)。発火後=指値で再試行します。")
        res = cli.send_stop_buy(args.symbol, qty=args.qty, trigger_price=trigger,
                                cash_margin=cash_margin, after_hit_price=trigger)
    order_id = res.get("OrderId")
    if res.get("Result") != 0:
        print(f"  ✗ 発注失敗: {res}")
        print("    → 取引時間外の可能性が高いです。ザラ場(9:00-11:30/12:30-15:00)に"
              "再実行してください。")
        return 1
    print(f"  ✓ 発注OK OrderId={order_id}")

    if not args.execute:
        print("\ndry-run のためここまで。--execute で実発注→取消を確認できます。")
        return 0

    # 3) 注文一覧で確認
    print("\n[3] 注文一覧で発注を確認...")
    time.sleep(1.0)  # 反映待ち
    try:
        orders = cli.get_orders()
        mine = [o for o in orders if o.get("ID") == order_id]
        if mine:
            o = mine[0]
            print(f"  ✓ 注文を確認: ID={o.get('ID')} "
                  f"State={o.get('State')} OrderQty={o.get('OrderQty')} "
                  f"Price={o.get('Price')}")
        else:
            print(f"  ⚠ OrderId={order_id} が一覧に見つかりません "
                  f"(反映遅延の可能性。一覧件数={len(orders)})")
    except Exception as e:
        print(f"  ⚠ 注文一覧取得エラー: {e}")

    # 4) 取消
    print(f"\n[4] テスト注文を取消 (OrderId={order_id})...")
    try:
        cres = cli.cancel_order(order_id)
        if cres.get("Result") == 0:
            print(f"  ✓ 取消成功")
        else:
            print(f"  ⚠ 取消レスポンス: {cres}")
            print("    → kabuステーションの注文一覧から手動で取消してください。")
    except Exception as e:
        print(f"  ✗ 取消エラー: {e}")
        print("    → kabuステーションの注文一覧から手動で取消してください。")
        return 1

    # 5) 取消後の確認
    print("\n[5] 取消を確認...")
    time.sleep(1.0)
    try:
        orders = cli.get_orders()
        mine = [o for o in orders if o.get("ID") == order_id]
        if mine:
            st = mine[0].get("State")
            # State: 5=終了(取消完了含む)
            print(f"  注文状態 State={st} (5=終了/取消済み)")
        print("  ✓ テスト完了。ポジションは持っていません。")
    except Exception as e:
        print(f"  ⚠ 確認エラー: {e}")

    print("\n" + "=" * 60)
    print("✓ 発注→確認→取消 が一通り成功しました。実発注が可能です。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
