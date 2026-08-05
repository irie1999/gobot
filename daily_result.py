r"""daily_result.py — その日の実取引結果(約定・決済・損益)を kabu から集計。

watcher で運用した日の「実際に約定した取引」を kabu の注文履歴(get_orders)から拾い、
銘柄ごとに 新規売り(信用) と 返済買い を突き合わせて実現損益を計算する。読み取り専用
(get_orders / get_positions のみ)なので発注は一切しない = 安全。

使い方:
  python daily_result.py --prod                 # 本番口座の今日の結果(実運用はこれ)
  python daily_result.py                         # デモ口座(既定)
  python daily_result.py --prod --date 2026-08-04  # 指定日
  python daily_result.py --prod --all            # 未決済(建玉が残っている)も表示

損益の符号: lss は「売ってから買い戻す」ので  実現損益 = 売却額 - 買戻額 (プラス=利益)。
手数料は往復 FEE_PCT_ONE_WAY×2 を概算で差し引く(実際の証券会社プランで多少ズレる)。
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from kabu_api import KabuClient, SIDE_SELL, SIDE_BUY
import backtest_limit_entry as ble

JST = timezone(timedelta(hours=9))
FEE = ble.FEE_PCT_ONE_WAY

ap = argparse.ArgumentParser(description="その日の実取引結果を kabu から集計(読み取り専用)")
ap.add_argument("--prod", action="store_true", help="本番口座(18080)。既定はデモ(18081)")
ap.add_argument("--date", type=str, default=None, help="対象日 YYYY-MM-DD(既定=今日JST)")
ap.add_argument("--all", action="store_true", help="未決済(建玉が残る銘柄)も表示")
args = ap.parse_args()

target = args.date or datetime.now(JST).strftime("%Y-%m-%d")
target_int = int(target.replace("-", ""))   # kabu ExecutionDay は yyyyMMdd(int)


def _num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _exec_date_int(det: dict) -> int | None:
    """約定明細から約定日(yyyyMMdd int)を取り出す。ExecutionDay 優先、無ければ RecvTime。"""
    ed = det.get("ExecutionDay")
    if ed:
        try:
            return int(ed)
        except Exception:
            pass
    rt = det.get("RecvTime") or ""
    # 例 "2026-08-04T09:05:12.34+09:00"
    if len(rt) >= 10 and rt[4] == "-":
        try:
            return int(rt[:10].replace("-", ""))
        except Exception:
            return None
    return None


def main() -> int:
    cli = KabuClient(prod=args.prod, dry_run=True)   # dry_run は発注のみに影響(読取は無関係)
    print(f"接続先: {'本番(18080)' if args.prod else 'デモ(18081)'} / 対象日: {target}")
    try:
        cli.connect()
    except Exception as e:
        print(f"✗ kabu 接続失敗: {e}")
        return 1

    try:
        orders = cli.get_orders()
    except Exception as e:
        print(f"✗ get_orders 失敗: {e}")
        return 1

    # 銘柄ごとに 売り約定額 / 買い約定額 / 株数 を集計(その日の約定のみ)
    agg = defaultdict(lambda: {"name": "", "sell_amt": 0.0, "sell_qty": 0,
                               "buy_amt": 0.0, "buy_qty": 0})
    n_exec = 0
    for o in orders:
        sym = str(o.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        side = o.get("Side")
        nm = o.get("SymbolName", "") or ""
        for det in (o.get("Details") or []):
            # RecType 8 = 約定。無い実装向けに ExecPrice/ExecQty があるレコードも拾う。
            rectype = int(det.get("RecType") or 0)
            price = _num(det.get("Price"))
            qty = int(_num(det.get("Qty")))
            if rectype != 8 or price <= 0 or qty <= 0:
                continue
            if _exec_date_int(det) != target_int:
                continue
            n_exec += 1
            a = agg[sym]
            a["name"] = a["name"] or nm
            if side == SIDE_SELL:
                a["sell_amt"] += price * qty
                a["sell_qty"] += qty
            elif side == SIDE_BUY:
                a["buy_amt"] += price * qty
                a["buy_qty"] += qty

    if not agg:
        print(f"\n{target} の約定はありませんでした(約定明細0件)。")
        print("※ --prod を付け忘れていないか / 対象日が営業日か を確認。")
        return 0

    # 決済済み(売り株数==買い株数)= ラウンドトリップ。未決済は残玉あり。
    rows_closed, rows_open = [], []
    tot_pnl = wins = losses = 0
    for sym, a in agg.items():
        closed_qty = min(a["sell_qty"], a["buy_qty"])
        if closed_qty > 0:
            avg_sell = a["sell_amt"] / a["sell_qty"] if a["sell_qty"] else 0.0
            avg_buy = a["buy_amt"] / a["buy_qty"] if a["buy_qty"] else 0.0
            gross = (avg_sell - avg_buy) * closed_qty        # 売り-買い(short)
            fee = (avg_sell + avg_buy) * closed_qty * FEE    # 往復手数料 概算
            pnl = gross - fee
            tot_pnl += pnl
            wins += 1 if pnl > 0 else 0
            losses += 1 if pnl <= 0 else 0
            rows_closed.append((pnl, sym, a["name"], closed_qty, avg_sell, avg_buy, gross, fee))
        if a["sell_qty"] != a["buy_qty"]:
            rows_open.append((sym, a["name"], a["sell_qty"], a["buy_qty"]))

    rows_closed.sort(reverse=True)   # 損益降順

    print(f"\n約定明細 {n_exec}件 / 銘柄 {len(agg)}")
    print("=" * 92)
    print(f"{'銘柄':>6} {'名称':<14} {'株数':>5} {'平均売':>9} {'平均買':>9} "
          f"{'粗損益':>10} {'手数料':>8} {'実現損益':>10}")
    print("-" * 92)
    for pnl, sym, nm, qty, asell, abuy, gross, fee in rows_closed:
        mark = "＋" if pnl > 0 else "－"
        print(f"{sym:>6} {nm[:14]:<14} {qty:>5} {asell:>9,.0f} {abuy:>9,.0f} "
              f"{gross:>+10,.0f} {fee:>8,.0f} {pnl:>+10,.0f} {mark}")
    print("-" * 92)
    n = wins + losses
    wr = (wins / n * 100) if n else 0.0
    print(f"  決済 {n}件  勝ち {wins} / 負け {losses}  勝率 {wr:.0f}%  "
          f"実現損益合計 {tot_pnl:>+,.0f}円")

    if rows_open:
        print("\n【未決済(株数が売り≠買い=建玉が残っている可能性)】")
        for sym, nm, sq, bq in rows_open:
            print(f"  {sym} {nm[:14]} 売り約定{sq}株 / 買い約定{bq}株 → 残 {sq-bq:+d}株")
        if not args.all:
            print("  ※ watcher がまだ決済していない可能性。引け後に再実行してください。")

    # 参考: 現在の建玉(信用)
    try:
        pos = cli.get_positions(product=2)
        held = [p for p in pos if int(_num(p.get("LeavesQty", 0))) > 0]
        if held:
            print(f"\n【現在の信用建玉 {len(held)}件(未決済)】")
            for p in held:
                s = str(p.get("Symbol", "")).removesuffix(".T")
                print(f"  {s} {p.get('SymbolName','')[:14]} "
                      f"残{int(_num(p.get('LeavesQty',0)))}株 @約定{_num(p.get('Price',0)):,.0f}")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
