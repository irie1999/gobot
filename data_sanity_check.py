"""
data_sanity_check.py  ―  pkl データの異常値検出
==================================================================
5分足データに価格不連続 (株式分割・併合の調整漏れ) があるか検出。
症状: ATR > 10%, 連続バー間で価格±50%超ジャンプ

【使い方】
  python data_sanity_check.py                    # 全winnersを検査
  python data_sanity_check.py --watchlist daytrade_combined_watchlist.py
  python data_sanity_check.py --all              # 全prime
  python data_sanity_check.py --quarantine       # 異常銘柄を quarantine/ に隔離
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_data import load_intraday_batch

JST = timezone(timedelta(hours=9))


def check_one(sym, df, max_atr_pct=5.0, max_gap_pct=30.0):
    """1銘柄のデータ整合性チェック。

    戻り値:
        {sane: bool, issues: list[str], atr_pct: float, max_gap_pct: float}
    """
    if df is None or df.empty or len(df) < 50:
        return {"sane": False, "issues": ["データ不足"], "atr_pct": 0, "max_gap": 0}

    issues = []
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    # ATR チェック
    atr_arr = []
    for i in range(1, min(len(closes), 100)):
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i-1]),
            abs(lows[-i] - closes[-i-1]),
        )
        atr_arr.append(tr)
    atr = float(np.mean(atr_arr)) if atr_arr else 0
    cur = closes[-1]
    atr_pct = atr / cur * 100 if cur > 0 else 0
    if atr_pct > max_atr_pct:
        issues.append(f"ATR {atr_pct:.1f}% (>={max_atr_pct}%)")

    # バー間の最大ジャンプ
    max_gap = 0
    n_gaps = 0
    for i in range(1, len(closes)):
        if closes[i-1] <= 0:
            continue
        gap = abs(closes[i] - closes[i-1]) / closes[i-1] * 100
        if gap > max_gap:
            max_gap = gap
        if gap > max_gap_pct:
            n_gaps += 1
    if max_gap > max_gap_pct:
        issues.append(f"最大ジャンプ {max_gap:.1f}% ({n_gaps}回 >={max_gap_pct}%)")

    # 価格レンジ (最古/最新の差が異常)
    if len(closes) >= 10:
        oldest = float(np.mean(closes[:10]))
        newest = float(np.mean(closes[-10:]))
        if oldest > 0:
            range_pct = abs(newest - oldest) / oldest * 100
            if range_pct > 500:
                issues.append(f"価格レンジ {range_pct:.0f}% (株式分割疑い)")

    return {
        "sane": len(issues) == 0,
        "issues": issues,
        "atr_pct": atr_pct,
        "max_gap": max_gap,
    }


def main():
    parser = argparse.ArgumentParser(description="pkl データ整合性チェック")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--all", action="store_true",
                        help="prime 全銘柄を検査")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--quarantine", action="store_true",
                        help="異常銘柄を quarantine/ に隔離")
    parser.add_argument("--max-atr-pct", type=float, default=5.0)
    parser.add_argument("--max-gap-pct", type=float, default=30.0)
    args = parser.parse_args()

    # 対象選定
    if args.all:
        from symbols_listed_all import SYMBOLS
        targets = SYMBOLS
    else:
        from check_signals_daytrade import _load_watchlist
        watchlist = _load_watchlist(args.watchlist)
        targets = [(e[0], e[1]) for e in watchlist]

    print(f"検査対象: {len(targets)}銘柄")

    # データロード
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")

    # 検査
    sane_list = []
    insane_list = []
    for sym, name in targets:
        df = fetched.get(sym)
        if df is None:
            continue
        result = check_one(sym, df, args.max_atr_pct, args.max_gap_pct)
        if result["sane"]:
            sane_list.append((sym, name, result))
        else:
            insane_list.append((sym, name, result))

    # 結果表示
    print(f"\n{'='*70}")
    print(f"  正常: {len(sane_list)}銘柄 / 異常: {len(insane_list)}銘柄")
    print(f"{'='*70}")

    if insane_list:
        print(f"\n【異常銘柄一覧】 (ATR>{args.max_atr_pct}% or ジャンプ>{args.max_gap_pct}%)")
        print(f"{'コード':<10} {'銘柄':<18} {'ATR%':>7} {'最大Gap%':>10} {'症状'}")
        print("-" * 70)
        for sym, name, r in sorted(insane_list, key=lambda x: -x[2]["atr_pct"]):
            disp = name[:16]
            print(f"{sym:<10} {disp:<18} {r['atr_pct']:>6.1f}% "
                  f"{r['max_gap']:>9.1f}% {' / '.join(r['issues'])}")

    # 隔離
    if args.quarantine and insane_list:
        from daytrade_data import DATA_DIR, yf_to_jquants
        quarantine_dir = DATA_DIR.parent / "quarantine_5m"
        quarantine_dir.mkdir(exist_ok=True)
        for sym, name, _ in insane_list:
            code5 = yf_to_jquants(sym)
            src = DATA_DIR / f"{code5}.pkl"
            if src.exists():
                dst = quarantine_dir / f"{code5}.pkl"
                shutil.move(str(src), str(dst))
                print(f"  隔離: {sym} → {dst}")
        print(f"\n{len(insane_list)}銘柄を {quarantine_dir} に隔離")

    # CSV 出力
    today = datetime.now(JST).strftime("%Y-%m-%d")
    out_path = Path(f"data_sanity_{today}.csv")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("symbol,name,sane,atr_pct,max_gap_pct,issues\n")
        for sym, name, r in sane_list + insane_list:
            issues = " | ".join(r["issues"])
            f.write(f"{sym},{name},{r['sane']},{r['atr_pct']:.2f},"
                    f"{r['max_gap']:.2f},{issues}\n")
    print(f"\nCSV: {out_path}")


if __name__ == "__main__":
    main()
