"""
check_price_limit.py ― 銘柄の日足から「値幅(ストップ高/安)」の推移を確認する
================================================================================
利確指値が値幅制限でキャップされた/キャンセルされた原因を調べるための診断。
各営業日の『基準値段(前日終値)→制限値幅→ストップ高/安』を一覧表示する。

使い方:
  python check_price_limit.py 6136.T                 # 直近20営業日
  python check_price_limit.py 6136.T --days 40
  python check_price_limit.py 6136.T --target 3995   # 目標がどの日に値幅超だったか判定
"""
from __future__ import annotations

import argparse

from backtest_limit_entry import fetch, tick_size

# 東証 制限値幅テーブル: 基準値段(前日終値) → 片側の値幅
_TSE_LIMIT_TABLE = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150),
    (1500, 300), (2000, 400), (3000, 500), (5000, 700), (7000, 1000),
    (10000, 1500), (15000, 3000), (20000, 4000), (30000, 5000),
    (50000, 7000), (70000, 10000), (100000, 15000), (150000, 30000),
]


def _width(base: float) -> float:
    for th, w in _TSE_LIMIT_TABLE:
        if base < th:
            return w
    return 1_000_000.0


def _floor_tick(p: float) -> int:
    t = tick_size(float(p))
    return int(p // t) * t


def _ceil_tick(p: float) -> int:
    import math
    t = tick_size(float(p))
    return int(math.ceil(p / t)) * t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--days", type=int, default=20, help="表示する直近営業日数")
    ap.add_argument("--target", type=float, default=0.0,
                    help="利確目標。指定するとその日にストップ高を超えていたか判定")
    args = ap.parse_args()

    sym = args.symbol if args.symbol.endswith(".T") else f"{args.symbol}.T"
    df = fetch(sym, 120)
    if df is None or df.empty:
        print(f"✗ {sym} の日足が取得できません")
        return
    df = df.tail(args.days + 1)
    closes = df["close"].values
    idx = df.index

    print("=" * 84)
    print(f"{sym} 値幅(ストップ高/安)の推移  (基準値段=前日終値)")
    if args.target:
        print(f"  利確目標 {args.target:,.0f}円 が『ストップ高』を超える日 = その日は目標に指値できずキャップされる")
    print("=" * 84)
    print(f"  {'日付':<12}{'終値':>9}{'前日終値':>10}{'値幅':>7}"
          f"{'ストップ高':>11}{'ストップ安':>11}"
          + ("  目標可否" if args.target else ""))
    for i in range(1, len(df)):
        d = idx[i].date() if hasattr(idx[i], "date") else idx[i]
        cl = float(closes[i])
        base = float(closes[i - 1])          # 前日終値 = 基準値段
        w = _width(base)
        upper = _floor_tick(base + w)        # ストップ高(呼値内)
        lower = _ceil_tick(base - w)         # ストップ安(呼値内)
        line = (f"  {str(d):<12}{cl:>9,.0f}{base:>10,.0f}{w:>7,.0f}"
                f"{upper:>11,.0f}{lower:>11,.0f}")
        if args.target:
            if args.target > upper:
                line += f"  ✗超過(目標>{upper:,.0f}→上限に指値)"
            else:
                line += f"  ○可(目標≤{upper:,.0f})"
        print(line)

    print("\n読み方:")
    print("  ・利確目標 > その日のストップ高 → 目標に指値できず『ストップ高』にキャップされる")
    print("  ・株価が下落して前日終値(基準)が下がると値幅も下がり、ストップ高が目標を下回りうる")
    print("  ・株価が戻って基準が上がれば、目標がストップ高以下に収まり本来目標に指値し直せる")


if __name__ == "__main__":
    main()
