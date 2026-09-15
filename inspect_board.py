"""
inspect_board.py  ―  kabu board(時価/値幅)の全フィールドをダンプする診断ツール

利確指値の上限値(3200等)が board のどのフィールドに入っているかを確認する。
order_server の 429 ストームと切り離して単独実行する。

使い方:
  python inspect_board.py 5726            # デモ(18081)
  python inspect_board.py 5726 --prod     # 本番(18080)
"""
import argparse
import json
import time

from kabu_api import KabuClient, EXCHANGE_TOSHO


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--prod", action="store_true")
    args = ap.parse_args()

    cli = KabuClient(prod=args.prod, dry_run=True)
    cli.connect()
    print(f"接続: {cli.env_label}")

    # register を 429 backoff つきで確実に
    for i in range(5):
        try:
            cli.register(args.symbol, EXCHANGE_TOSHO)
            break
        except Exception as e:
            print(f"  register retry {i+1}: {e}")
            time.sleep(1.5 * (i + 1))
    time.sleep(0.5)

    board = None
    for i in range(5):
        try:
            board = cli.get_board(args.symbol)
            break
        except Exception as e:
            print(f"  get_board retry {i+1}: {e}")
            time.sleep(1.5 * (i + 1))

    if not board:
        print("board 取得失敗")
        return

    print(f"\n=== {args.symbol} board 全フィールド ===")
    print(json.dumps(board, ensure_ascii=False, indent=2))

    # 値幅関連だけ抜粋
    print("\n=== 値幅/価格 関連の候補フィールド ===")
    for k in sorted(board.keys()):
        kl = k.lower()
        if any(w in kl for w in ("limit", "price", "upper", "lower",
                                  "high", "low", "cap", "値")):
            print(f"  {k} = {board[k]}")


if __name__ == "__main__":
    main()
