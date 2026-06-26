"""
show_positions.py — kabu station 保有建玉 + 損切り/利確 表示
=============================================================

kabu station API から実建玉を取得し、my_positions.csv の損切り・利確価格と
照合して現在状況をコンソールに表示します。

使い方:
  python show_positions.py           # デモ口座 (18081)
  python show_positions.py --prod    # 本番口座 (18080)
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from kabu_api import KabuClient

_POS_CSV = "my_positions.csv"


def load_csv_positions(csv_path: str) -> dict:
    """my_positions.csv から symbol → {stop_price, target_price, ...} を返す。"""
    result: dict = {}
    p = Path(csv_path)
    if not p.exists():
        return result
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") not in ("holding", "filled"):
                continue
            sym = str(row.get("symbol", "")).strip()
            if not sym:
                continue
            try:
                stop   = float(row["stop_price"])   if row.get("stop_price")   else None
                target = float(row["target_price"]) if row.get("target_price") else None
                fill   = float(row["fill_price"])   if row.get("fill_price")   else None
                qty    = int(row["qty"])             if row.get("qty")          else None
            except (ValueError, KeyError):
                stop = target = fill = qty = None
            result[sym] = {
                "stop":     stop,
                "target":   target,
                "fill":     fill,
                "qty":      qty,
                "strategy": row.get("strategy", ""),
                "name":     row.get("name", ""),
                "side":     row.get("side", "long"),
                "record_date": row.get("record_date", ""),
            }
    return result


def _pct(price: float, ref: float) -> str:
    if ref and ref > 0:
        return f"{(price - ref) / ref * 100:+.1f}%"
    return "—"


def _pnl_str(current: float | None, fill: float | None, qty: int | None, side: str) -> str:
    if current is None or fill is None or qty is None:
        return "—"
    diff = (current - fill) if side == "long" else (fill - current)
    pnl = diff * qty
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{pnl:,.0f}円"


def main():
    ap = argparse.ArgumentParser(description="kabu station 保有建玉 + 損切り/利確 表示")
    ap.add_argument("--prod",    action="store_true", help="本番口座 (18080) を使用")
    ap.add_argument("--csv",     default=_POS_CSV,    help=f"CSVパス (default: {_POS_CSV})")
    ap.add_argument("--product", type=int, default=2, help="product: 0=全部 1=現物 2=信用 (default:2)")
    args = ap.parse_args()

    # kabu station 接続
    cli = KabuClient(prod=args.prod, dry_run=True)
    try:
        cli.connect()
    except Exception as e:
        print(f"[ERROR] kabu station 接続失敗: {e}")
        print("  → kabu station が起動しているか確認してください。")
        sys.exit(1)

    # 建玉取得
    try:
        positions = cli.get_positions(product=args.product)
    except Exception as e:
        print(f"[ERROR] 建玉取得失敗: {e}")
        sys.exit(1)

    if not positions:
        print("現在の保有建玉はありません。")
        return

    # CSV の損切り/利確データ
    csv_data = load_csv_positions(args.csv)

    # 現在値取得 & 表示
    print()
    print("=" * 80)
    print("  kabu station 保有建玉  損切り・利確 一覧")
    print("=" * 80)

    total_pnl = 0.0
    has_pnl   = False

    for pos in positions:
        sym  = str(pos.get("Symbol", ""))
        name = pos.get("SymbolName") or pos.get("Name") or csv_data.get(sym, {}).get("name", "")
        side_code = pos.get("Side", "")          # "2"=買(ロング) "1"=売(ショート)
        side_str  = "ロング" if side_code == "2" else "ショート"
        qty       = pos.get("LeavesQty") or pos.get("Qty") or 0
        avg_price = pos.get("AveragePrice") or pos.get("Price") or 0.0
        profit    = pos.get("Profit")            # 評価損益 (kabu が計算済みの場合)
        current   = pos.get("CurrentPrice")      # 現在値 (kabu board 取得済みの場合)

        # 現在値がなければ取得
        if current is None:
            try:
                current = cli.get_current_price(sym)
            except Exception:
                current = None

        cv = csv_data.get(sym, {})
        stop   = cv.get("stop")
        target = cv.get("target")
        fill   = cv.get("fill") or avg_price or None
        strategy = cv.get("strategy", "")
        record_date = cv.get("record_date", "")

        print()
        print(f"  {sym}  {name}  [{side_str}]  {strategy or '—'}")
        print(f"  ─────────────────────────────────────────────")
        print(f"  約定値   : {fill:>9,.1f} 円   ({record_date})" if fill else "  約定値   : —")
        if current is not None:
            pct_now = _pct(current, fill) if fill else "—"
            print(f"  現在値   : {current:>9,.1f} 円   ({pct_now})")
        else:
            print(f"  現在値   : 取得失敗")
        if stop is not None:
            dist_stop = _pct(stop, fill) if fill else "—"
            print(f"  損切り   : {stop:>9,.1f} 円   (約定比 {dist_stop})")
        else:
            print(f"  損切り   : 未設定  ← my_positions.csv に stop_price を入力してください")
        if target is not None:
            dist_tgt = _pct(target, fill) if fill else "—"
            print(f"  利確目標 : {target:>9,.1f} 円   (約定比 {dist_tgt})")
        else:
            print(f"  利確目標 : 未設定")

        # 損益
        if profit is not None:
            total_pnl += float(profit)
            has_pnl    = True
            sign = "+" if float(profit) >= 0 else ""
            print(f"  評価損益 : {sign}{profit:,.0f} 円  (×{qty}株)")
        elif current is not None and fill:
            side_long = (side_code == "2")
            diff = (current - fill) if side_long else (fill - current)
            pnl_est = diff * qty
            total_pnl += pnl_est
            has_pnl    = True
            sign = "+" if pnl_est >= 0 else ""
            print(f"  評価損益 : {sign}{pnl_est:,.0f} 円  (×{qty}株, 推定)")

    print()
    print("=" * 80)
    if has_pnl:
        sign = "+" if total_pnl >= 0 else ""
        print(f"  合計評価損益: {sign}{total_pnl:,.0f} 円")
    print("=" * 80)
    print()
    print("  ※ 損切り・利確価格は my_positions.csv の stop_price / target_price を参照。")
    print("  ※ 損切りチェック: python close_stop_guard.py")
    print()


if __name__ == "__main__":
    main()
