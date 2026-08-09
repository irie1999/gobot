"""
set_profit_orders.py — 保有銘柄の利確指値注文を毎日自動設定
============================================================

my_positions.csv の保有銘柄に対して kabuステーション に
利確指値注文を設定するスクリプト。

【運用フロー（intraday 運用時）】
  ① 引け後 or 翌朝寄り前に実行
    python set_profit_orders.py              # dry-run（確認のみ）
    python set_profit_orders.py --execute    # デモ口座に発注
    python set_profit_orders.py --execute --prod  # 本番口座

  ② ザラ場中に目標価格に到達 → 指値約定で利確

【close 方式との使い分け】
  - close（既定）: set_profit_orders.py は不要。
    close_stop_guard.py が引け前に目標価格到達を判定して MOC 発注する。
  - intraday: このスクリプトで日中に指値を置き、自動約定を狙う。
    ただしザラ場の一時的な高値ヒゲで約定することがあり、より積極的な利確になる。

【重複防止】
  - 既に同銘柄の有効な指値売り注文がある場合はスキップ
  - target_price が 0.5% 以上変わった場合は取消→再発注
  - --refresh で全て強制取消→再設定

【データソース】
  my_positions.csv の target_price 列

使い方:
  python set_profit_orders.py                          # dry-run
  python set_profit_orders.py --execute                # デモ口座
  python set_profit_orders.py --execute --prod         # 本番口座
  python set_profit_orders.py --refresh --execute      # 全て再設定
  python set_profit_orders.py --log other.csv          # 別のCSV
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from kabu_api import KabuClient, CASH_GENBUTSU, CASH_MARGIN_CLOSE


# ── ポジション読み込み ─────────────────────────────────────────────────────────

def load_positions(csv_path: str) -> list[dict]:
    """保有中ポジションを CSV から読む。target_price が未設定の場合スキップ。"""
    positions = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") not in ("holding", "filled"):
                continue

            sym = str(row.get("symbol", "")).strip()
            name = str(row.get("name", "")).strip()
            side = str(row.get("side", "long")).lower()

            def _fv(k):
                try:
                    v = float(row.get(k) or 0)
                    return 0.0 if v != v else v
                except Exception:
                    return 0.0

            target_p = _fv("target_price")
            qty      = int(_fv("qty") or 100)
            cm       = int(_fv("cash_margin") or 1)

            if target_p <= 0:
                print(f"  ✗ {sym} {name}: 目標価格が見つかりません → スキップ")
                continue

            positions.append({
                "symbol":      sym,
                "name":        name,
                "target_price": target_p,
                "qty":         qty,
                "side":        side,
                "cash_margin": cm,
            })

    return positions


# ── 既存注文チェック ──────────────────────────────────────────────────────────

def get_active_limit_orders(orders: list[dict]) -> dict[str, dict]:
    """有効な指値注文（売り/買い）を {symbol: {order_id, price, side}} で返す。

    OrderState: 1=待機, 2=処理中, 4=訂正取消中 → 有効
    FrontOrderType: 20=指値
    """
    result: dict[str, dict] = {}
    for o in orders:
        state = o.get("OrderState", 0)
        if state not in (1, 2, 4):
            continue
        if o.get("FrontOrderType") != 20:  # 指値以外はスキップ
            continue
        sym = str(o.get("Symbol", ""))
        result[sym] = {
            "order_id": o.get("ID", ""),
            "price":    float(o.get("Price", 0)),
            "side":     o.get("Side", ""),   # "1"=売, "2"=買
        }
    return result


# ── メイン ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="保有銘柄の利確指値注文を自動設定")
    ap.add_argument("--execute",  action="store_true", help="実際に発注する（デフォルト: dry-run）")
    ap.add_argument("--prod",     action="store_true", help="本番口座(18080)を使う")
    ap.add_argument("--refresh",  action="store_true", help="既存の指値を取消して再設定")
    ap.add_argument("--log",      default="my_positions.csv", help="CSVパス")
    args = ap.parse_args()

    dry_run = not args.execute
    cli = KabuClient(prod=args.prod, dry_run=dry_run)
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}{'kabu接続中' if args.execute else 'kabu未接続'} ({env_label})…")

    if args.execute:
        try:
            cli.connect()
        except Exception as e:
            print(f"✗ 接続失敗: {e}")
            sys.exit(1)

    csv_path = Path(args.log)
    if not csv_path.exists():
        print(f"✗ ファイルが見つかりません: {csv_path}")
        sys.exit(1)

    positions = load_positions(str(csv_path))
    if not positions:
        print("対象ポジションなし。終了します。")
        return

    # 既存の有効な指値注文を取得（execute 時のみ）
    active_limits: dict[str, dict] = {}
    if args.execute:
        try:
            orders = cli.get_orders()
            active_limits = get_active_limit_orders(orders)
            print(f"既存の有効指値注文: {len(active_limits)}件\n")
        except Exception as e:
            print(f"⚠ 注文一覧取得失敗: {e}（重複チェックなしで進みます）\n")
    else:
        print("dry-run のため既存注文チェックをスキップ\n")

    set_count = 0
    skip_count = 0

    for pos in positions:
        sym      = pos["symbol"]
        name     = pos["name"]
        target_p = pos["target_price"]
        qty      = pos["qty"]
        side     = pos["side"]
        cm       = pos["cash_margin"]

        send_cm = CASH_MARGIN_CLOSE if cm == 3 else CASH_GENBUTSU

        existing = active_limits.get(sym)

        if existing and not args.refresh:
            diff = abs(existing["price"] - target_p) / max(target_p, 1)
            if diff < 0.005:
                print(f"  ✓ {sym} {name}: 既存の指値 @{existing['price']:,.0f}円 → スキップ")
                skip_count += 1
                continue
            else:
                print(f"  ↻ {sym} {name}: 価格変更 @{existing['price']:,.0f} → @{target_p:,.0f}円 → 取消して再発注")
                cli.cancel_order(existing["order_id"])

        elif existing and args.refresh:
            print(f"  ↻ {sym} {name}: --refresh → 既存取消して再設定")
            cli.cancel_order(existing["order_id"])

        # 発注: ロングは指値売り、ショートは指値買い（踏み上げ終了時の利確）
        if side == "short":
            print(f"  → {sym} {name}: 利確指値買い @{target_p:,.0f}円 x{qty}株")
            cli.send_buy(sym, qty=qty, price=target_p, cash_margin=send_cm,
                         order_type="limit")
        else:
            print(f"  → {sym} {name}: 利確指値売り @{target_p:,.0f}円 x{qty}株")
            cli.send_sell(sym, qty=qty, price=target_p, cash_margin=send_cm,
                          order_type="limit")

        set_count += 1

    print(f"\n{'─'*40}")
    print(f"設定: {set_count}件 / スキップ: {skip_count}件 / 合計: {len(positions)}件")
    if dry_run:
        print("※ dry-run です。実際の発注には --execute を追加してください。")


if __name__ == "__main__":
    main()
