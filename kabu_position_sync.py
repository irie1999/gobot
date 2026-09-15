"""
kabu_position_sync.py — kabu約定結果を my_positions.csv に自動反映
================================================================

kabuステーション API から本日の約定済み注文を取得し、
my_positions.csv の pending 行（status が空 or "pending"）と照合して
fill_date / fill_price / status="holding" に更新する。

また、約定済みで CSV に登録がない建玉（手動発注など）を検出してリストアップする。

【運用フロー】
  ① 引け後（15:30 以降）に実行
    python kabu_position_sync.py              # dry-run（確認のみ）
    python kabu_position_sync.py --execute    # CSV を実際に更新
    python kabu_position_sync.py --execute --prod  # 本番口座

  ② 更新後、my_positions.csv の保有銘柄が正しいか確認

【照合ルール】
  - kabu の約定済みエントリー注文（買い建て or 売り建て）と CSV の symbol を照合
  - 同銘柄で status が "pending" または空の行を "holding" に更新
  - fill_date: 約定日、fill_price: 平均約定価格

使い方:
  python kabu_position_sync.py                          # dry-run
  python kabu_position_sync.py --execute                # デモ口座 + CSV更新
  python kabu_position_sync.py --execute --prod         # 本番口座 + CSV更新
  python kabu_position_sync.py --log other.csv          # 別のCSV
  python kabu_position_sync.py --days 3                 # 直近3日分の注文を対象
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

from kabu_api import KabuClient


TODAY = date.today()


# ── kabu 約定注文の取得 ──────────────────────────────────────────────────────

def _is_entry_order(order: dict) -> bool:
    """エントリー注文（新規建て）かどうかを判定する。

    CashMargin: 1=現物買, 2=信用新規 → 買いエントリー
    Side: "2"=買い（ロングエントリー）, "1"=売り（ショートエントリー）
    ただしCashMargin=1 の売り は現物決済なのでエントリーではない。
    """
    cm = int(order.get("CashMargin") or 0)
    side = str(order.get("Side", ""))
    # 現物買い or 信用新規 は エントリー
    if cm == 1 and side == "2":   # 現物買い
        return True
    if cm == 2:                    # 信用新規（ロング or ショート）
        return True
    return False


def get_filled_entries(cli: KabuClient, days: int = 1) -> list[dict]:
    """約定済みのエントリー注文一覧を返す。

    Returns list of dicts:
      symbol, name, side ("long"/"short"), fill_date, fill_price, qty, cash_margin
    """
    try:
        orders = cli.get_orders()
    except Exception as e:
        print(f"  ✗ 注文一覧取得失敗: {e}")
        return []

    cutoff = TODAY - timedelta(days=days)
    filled = []

    for o in orders:
        # 約定済みのみ (OrderState=5: 約定済み, 6: 取消済みは除く)
        state = int(o.get("OrderState") or 0)
        if state != 5:
            continue
        if not _is_entry_order(o):
            continue

        # 約定日フィルター
        recv_time = str(o.get("RecvTime") or "")
        order_date_str = recv_time[:10] if len(recv_time) >= 10 else ""
        try:
            order_date = date.fromisoformat(order_date_str)
        except ValueError:
            order_date = TODAY
        if order_date < cutoff:
            continue

        sym  = str(o.get("Symbol", "")).strip()
        name = str(o.get("SymbolName", ""))[:10]
        side_str = str(o.get("Side", ""))
        side = "short" if side_str == "1" else "long"
        cm = int(o.get("CashMargin") or 1)

        # 平均約定価格と数量を Details から集計
        details = o.get("Details") or []
        filled_qty = 0
        total_value = 0.0
        fill_date_str = order_date_str
        for d in details:
            if d.get("TransType") != 3:  # 3=約定
                continue
            q = int(d.get("Qty") or 0)
            p = float(d.get("Price") or 0)
            filled_qty += q
            total_value += q * p
            dt = str(d.get("TransactTime") or "")[:10]
            if dt:
                fill_date_str = dt

        if filled_qty <= 0:
            continue

        avg_price = total_value / filled_qty

        filled.append({
            "symbol":      sym,
            "name":        name,
            "side":        side,
            "fill_date":   fill_date_str or str(TODAY),
            "fill_price":  avg_price,
            "qty":         filled_qty,
            "cash_margin": cm,
        })

    return filled


# ── CSV 更新 ──────────────────────────────────────────────────────────────────

def update_csv(csv_path: str, fills: list[dict], dry_run: bool) -> None:
    """CSV を読み込んで約定情報を反映し、dry_run でなければ上書き保存する。"""
    p = Path(csv_path)
    if not p.exists():
        print(f"  ✗ CSV が見つかりません: {csv_path}")
        return

    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else []

    # fills を symbol でインデックス化（同銘柄複数約定は最新を使う）
    fill_map: dict[str, dict] = {}
    for fl in fills:
        sym = fl["symbol"]
        # 既にあれば fill_date が新しい方を採用
        if sym not in fill_map or fl["fill_date"] >= fill_map[sym]["fill_date"]:
            fill_map[sym] = fl

    updated_count = 0
    unmatched_fills = set(fill_map.keys())

    for row in rows:
        sym = str(row.get("symbol", "")).strip()
        status = str(row.get("status", "")).strip().lower()

        if sym not in fill_map:
            continue
        unmatched_fills.discard(sym)

        if status not in ("", "pending"):
            # すでに holding / filled / closed など → スキップ
            print(f"  ℹ {sym}: CSV status={status or '空'} → スキップ（既に更新済み）")
            continue

        fl = fill_map[sym]
        print(f"  ✓ {sym} {fl['name']}: pending → holding "
              f"約定日={fl['fill_date']} 約定価格={fl['fill_price']:.1f} "
              f"{fl['qty']}株")
        if not dry_run:
            row["status"]     = "holding"
            row["fill_date"]  = fl["fill_date"]
            row["fill_price"] = f"{fl['fill_price']:.1f}"
            row["qty"]        = str(fl["qty"])
            row["updated_date"] = str(TODAY)
        updated_count += 1

    # kabu にあって CSV にない約定
    if unmatched_fills:
        print(f"\n⚠ CSV に未登録の約定銘柄 ({len(unmatched_fills)}件):")
        for sym in sorted(unmatched_fills):
            fl = fill_map[sym]
            print(f"   {sym} {fl['name']} [{fl['side']}] "
                  f"@{fl['fill_price']:.0f}円 {fl['qty']}株 ({fl['fill_date']})")
        print("  → position_server で手動登録するか、CSV に行を追加してください。")

    if updated_count == 0:
        print("\n更新対象の pending 行なし。")
        return

    if dry_run:
        print(f"\n{updated_count}件を更新予定（dry-run のため変更しません）")
        return

    # 上書き保存
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n{updated_count}件を更新しました → {csv_path}")


# ── メイン ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="kabu約定結果を my_positions.csv に自動反映")
    ap.add_argument("--execute", action="store_true",
                    help="実際に CSV を更新する（デフォルト: dry-run）")
    ap.add_argument("--prod",    action="store_true",
                    help="本番口座(18080)を使う")
    ap.add_argument("--log",     default="my_positions.csv", help="CSVパス")
    ap.add_argument("--days",    type=int, default=1,
                    help="直近N日分の注文を対象にする（デフォルト: 1）")
    args = ap.parse_args()

    dry_run = not args.execute
    cli = KabuClient(prod=args.prod, dry_run=False)  # 照会は常に実行
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}kabu接続中 ({env_label})…")

    try:
        cli.connect()
    except Exception as e:
        print(f"✗ 接続失敗: {e}")
        sys.exit(1)

    print(f"直近 {args.days} 日の約定済みエントリー注文を取得中…")
    fills = get_filled_entries(cli, days=args.days)

    if not fills:
        print("約定済みエントリーなし。終了します。")
        return

    print(f"\n約定済みエントリー: {len(fills)} 件")
    for fl in fills:
        print(f"  {fl['symbol']} {fl['name']} [{fl['side']}] "
              f"@{fl['fill_price']:.1f}円 {fl['qty']}株 ({fl['fill_date']})")

    print(f"\n--- CSV 更新 ({args.log}) ---")
    update_csv(args.log, fills, dry_run)

    if dry_run:
        print("\ndry-run です。実際に更新するには --execute を追加してください。")


if __name__ == "__main__":
    main()
