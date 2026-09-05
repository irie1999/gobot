"""
build_watchlist_wf.py  ―  WFスキャン結果(wf_strategies_*_ho*.csv)から
                          OOS堅牢なデイトレ WATCHLIST を確定する。

選定基準 (既定): ホールドアウト HO90 と HO180 の両方で
  - holdout_pnl > 0 (選定に使っていない期間=真の未使用OOSで黒字)
  - holdout_trades >= 3 (サンプル最低数)
の (銘柄 × 戦略) を採用する。HO30 はサンプルが少ないため任意条件。

出力:
  daytrade_watchlist.py    … WATCHLIST = {戦略: [(code, name), ...]} の Python
  画面に戦略別ランキング

使い方:
  python build_watchlist_wf.py
  python build_watchlist_wf.py --min-ho-trades 3 --require-ho30
  python build_watchlist_wf.py --date 2026-06-30
"""
from __future__ import annotations

import argparse
import csv
import glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
RESULTS = Path("walkforward_results")
STRATEGIES = ["Donchian", "GapReversal", "VWAP", "RSI", "Pivot",
              "OpenMomentum", "MACDBreak", "StochATR", "VolSurge"]
HOLDOUTS = [30, 90, 180]


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _load(strategy: str, ho: int, date: str) -> dict:
    """wf_strategies_<strategy>_ho<ho>_<date>.csv → {code: row}。"""
    path = RESULTS / f"wf_strategies_{strategy}_ho{ho}_{date}.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["symbol"]] = r
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).date().isoformat())
    ap.add_argument("--min-ho-trades", type=int, default=3,
                    help="HO各期間で必要な最小ホールドアウト取引数")
    ap.add_argument("--require-ho30", action="store_true",
                    help="HO30 もプラスを必須にする (より厳格)")
    ap.add_argument("--out", default="daytrade_watchlist.py")
    args = ap.parse_args()

    watchlist: dict[str, list] = {}
    print("=" * 92)
    print(f"OOS堅牢 WATCHLIST 選定  (date={args.date})")
    print(f"  基準: HO90 & HO180 で holdout_pnl>0 かつ holdout_trades>="
          f"{args.min_ho_trades}" + ("  + HO30も+" if args.require_ho30 else ""))
    print("=" * 92)

    for strat in STRATEGIES:
        data = {ho: _load(strat, ho, args.date) for ho in HOLDOUTS}
        if not any(data.values()):
            continue
        picks = []
        # HO180 を基準に銘柄を走査 (最も長い検証窓)
        for code, r180 in data[180].items():
            r90 = data[90].get(code)
            r30 = data[30].get(code)
            if not r90:
                continue

            def ok(r):
                return (r and _f(r.get("holdout_pnl")) > 0
                        and _f(r.get("holdout_trades")) >= args.min_ho_trades)

            if not (ok(r180) and ok(r90)):
                continue
            if args.require_ho30 and not ok(r30):
                continue
            picks.append({
                "code": code, "name": r180.get("name", ""),
                "price": _f(r180.get("latest_price")),
                "ho180": _f(r180.get("holdout_pnl")),
                "ho90": _f(r90.get("holdout_pnl")),
                "ho30": _f(r30.get("holdout_pnl")) if r30 else 0.0,
                # 合計holdout損益でランク
            })
        picks.sort(key=lambda x: -(x["ho180"] + x["ho90"]))
        if not picks:
            continue
        watchlist[strat] = [(p["code"], p["name"]) for p in picks]
        print(f"\n【{strat}】 採用 {len(picks)}銘柄")
        print(f"  {'銘柄':<9}{'名前':<22}{'株価':>7}"
              f"{'HO180':>10}{'HO90':>10}{'HO30':>10}")
        print("  " + "-" * 76)
        for p in picks:
            print(f"  {p['code']:<9}{p['name'][:20]:<22}{p['price']:>7,.0f}"
                  f"{p['ho180']:>+10,.0f}{p['ho90']:>+10,.0f}{p['ho30']:>+10,.0f}")

    # ── daytrade_watchlist.py 出力 ──
    total = sum(len(v) for v in watchlist.values())
    lines = [
        '"""daytrade_watchlist.py ― OOS堅牢なデイトレ WATCHLIST',
        f'build_watchlist_wf.py により {args.date} に自動生成。',
        'HO90 & HO180 の両ホールドアウトで holdout損益>0 の (銘柄×戦略)。',
        '"""',
        "",
        "# 戦略 -> [(コード, 名前), ...]",
        "WATCHLIST = {",
    ]
    for strat, syms in watchlist.items():
        lines.append(f"    {strat!r}: [")
        for code, name in syms:
            lines.append(f"        ({code!r}, {name!r}),")
        lines.append("    ],")
    lines.append("}")
    lines.append("")
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "=" * 92)
    print(f"合計 {total}銘柄(延べ) を {args.out} に出力しました。")
    print("  戦略別:", {k: len(v) for k, v in watchlist.items()})
    print("=" * 92)


if __name__ == "__main__":
    main()
