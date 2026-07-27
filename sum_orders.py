"""sum_orders.py — kabuの注文一覧(/orders)を取得して、注文中の株数・必要資金を集計し、
同一銘柄の重複注文を検出する。発注サーバの発注額が正しいか(二重発注が無いか)を確認する用。

使い方(あなたの機械・本番口座。KABU_API_PASSWORD 設定済み):
  python sum_orders.py --prod            # 本番(18080)の注文中を集計
  python sum_orders.py                   # デモ(18081)
  python sum_orders.py --prod --all      # 約定済・終了も含めて全部表示

照会のみ(発注しない)。dry_run=True でも get_orders は実データを読む。
※ 発注サーバ/watcher と同時に叩くとトークンの取り合いで401になることがある。
  401 が出たら、片方(発注サーバ or watcher)を一瞬止めてから実行するか、少し待って再試行。
"""
from __future__ import annotations
import argparse
from collections import defaultdict

from kabu_api import KabuClient

ap = argparse.ArgumentParser(description="kabu 注文中の株数・必要資金を集計(重複検出つき)")
ap.add_argument("--prod", action="store_true", help="本番(18080)。未指定はデモ(18081)")
ap.add_argument("--all", action="store_true", help="終了/約定済も含めて全注文を表示")
args = ap.parse_args()

_SIDE = {"1": "売", "2": "買"}
# kabu State/OrderState: 1待機 2処理中 3処理済 4訂正取消送信 5終了
_STATE = {1: "待機", 2: "処理中", 3: "処理済", 4: "取消送信", 5: "終了"}


def _trigger_price(o: dict) -> float:
    """注文の基準価格。逆指値は Details/ReverseLimitOrder のトリガー、無ければ Price。"""
    p = float(o.get("Price") or 0)
    if p > 0:
        return p
    # Details 配列の中から価格らしきものを拾う(逆指値のトリガー/指値)
    for d in (o.get("Details") or []):
        for k in ("TriggerPrice", "Price"):
            v = float(d.get(k) or 0)
            if v > 0:
                return v
    return 0.0


def _is_active(o: dict) -> bool:
    """注文中(未約定で有効)か。State/OrderState=5(終了)や全約定は除外。"""
    st = int(o.get("State") or o.get("OrderState") or 0)
    if st == 5:                      # 終了(約定完了/失効/取消済)
        return False
    qty = float(o.get("Qty") or 0)
    cum = float(o.get("CumQty") or 0)
    return (qty - cum) > 0           # 未約定数量が残っている


def main():
    cli = KabuClient(prod=args.prod, dry_run=True)
    cli.connect()
    orders = cli.get_orders()
    print(f"[取得] 全注文 {len(orders)}件 / 接続先 {'本番18080' if args.prod else 'デモ18081'}\n")

    rows = []
    per_sym = defaultdict(lambda: {"n": 0, "qty": 0.0, "notional": 0.0, "ids": []})
    total_notional = 0.0
    total_qty = 0.0
    for o in orders:
        active = _is_active(o)
        if not active and not args.all:
            continue
        sym = str(o.get("Symbol") or "")
        name = str(o.get("SymbolName") or "")
        side = _SIDE.get(str(o.get("Side")), str(o.get("Side")))
        qty = float(o.get("Qty") or 0)
        cum = float(o.get("CumQty") or 0)
        rem = qty - cum
        st = int(o.get("State") or o.get("OrderState") or 0)
        px = _trigger_price(o)
        note = px * rem
        oid = str(o.get("ID") or "")
        rows.append((sym, name, side, int(qty), int(cum), _STATE.get(st, st), px, note, oid))
        if active:
            e = per_sym[(sym, name)]
            e["n"] += 1
            e["qty"] += rem
            e["notional"] += note
            e["ids"].append(oid[-6:])
            total_notional += note
            total_qty += rem

    # 明細
    print(f"{'コード':>6} {'銘柄':<12}{'売買':>4}{'発注':>6}{'約定':>6}{'状態':>6}{'価格':>9}{'必要資金':>12}  OrderId")
    for sym, name, side, qty, cum, st, px, note, oid in rows:
        print(f"{sym:>6} {name[:12]:<12}{side:>4}{qty:>6}{cum:>6}{st:>6}{px:>9,.0f}{note:>12,.0f}  {oid}")

    # 銘柄別サマリー + 重複検出
    print("\n── 銘柄別(注文中) ──")
    dups = []
    for (sym, name), e in sorted(per_sym.items(), key=lambda x: -x[1]["notional"]):
        mark = ""
        if e["n"] > 1:
            mark = f"  ⚠重複{e['n']}件(ID末尾 {', '.join(e['ids'])})"
            dups.append((sym, name, e))
        print(f"  {sym} {name[:12]:<12} 注文{e['n']}件 {int(e['qty'])}株 "
              f"必要資金 {e['notional']:>11,.0f}円{mark}")

    print(f"\n[合計] 注文中 {int(total_qty)}株 / 必要資金 {total_notional:,.0f}円")
    if dups:
        print("\n🚨 同一銘柄の重複注文があります(二重発注の可能性):")
        for sym, name, e in dups:
            print(f"   {sym} {name} … {e['n']}件・計{int(e['qty'])}株。"
                  f"不要な分は kabu かレポートで取消してください(OrderId末尾 {', '.join(e['ids'])})。")
    else:
        print("\n重複注文はありません。")


if __name__ == "__main__":
    main()
