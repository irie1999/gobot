"""
build_watchlist_wf.py  ―  WFスキャン結果(wf_strategies_*_ho*.csv)から
                          OOS堅牢なデイトレ WATCHLIST を確定する。

★重要な原則 (2026-06-30 改修): 直近ホールドアウト期間(HO日数)は銘柄選定に
  一切使わない。選定は WF の TRAIN/TEST 成績 (= 選定窓 base=今日-HO日 以前の
  データのみ) だけで行い、holdout_* (直近HO日の成績) は『答え合わせ用の
  純OOS指標』として表示するだけ。フィルタには絶対に使わない。

選定基準 (既定): 各戦略について HO90 と HO180 の CSV 両方に出現し、両方で
  - folds_passed >= 2 (= CSV に載っている時点で WF fold を通過済み)
  - total_test_pnl > 0  (選定窓内の TEST=擬似未来で黒字。直近HO日は含まない)
  - avg_test_pf >= --min-test-pf  (既定 1.0)
  - total_test_trades >= --min-test-trades  (既定 3)
を満たす (銘柄 × 戦略) を採用する。HO90/HO180 の二窓で生き残る = 選定時期に
依存しない堅牢さ。ランクは TEST 損益 (HO180+HO90) 降順。
holdout_* はテーブルに OOS 検証列として併記するだけ (選定には不使用)。

出力:
  daytrade_watchlist.py    … WATCHLIST = {戦略: [(code, name), ...]} の Python
  画面に戦略別ランキング (TEST=選定指標 と HO=純OOS答え合わせ を並記)

使い方:
  python build_watchlist_wf.py
  python build_watchlist_wf.py --min-test-pf 1.2 --min-test-trades 5
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
    ap.add_argument("--min-test-pf", type=float, default=1.0,
                    help="選定窓 TEST の最小平均PF (既定 1.0)")
    ap.add_argument("--min-test-trades", type=int, default=3,
                    help="選定窓 TEST の最小取引数 (既定 3)")
    ap.add_argument("--out", default="daytrade_watchlist.py")
    args = ap.parse_args()

    watchlist: dict[str, list] = {}
    print("=" * 100)
    print(f"OOS堅牢 WATCHLIST 選定  (date={args.date})")
    print(f"  選定基準(直近HO日は不使用): HO90 & HO180 両CSV出現 / "
          f"total_test_pnl>0 / avg_test_pf>={args.min_test_pf} / "
          f"test_trades>={args.min_test_trades}")
    print(f"  ※ HO* 列は『選定に使っていない直近HO日』の純OOS答え合わせ (表示のみ)")
    print("=" * 100)

    for strat in STRATEGIES:
        data = {ho: _load(strat, ho, args.date) for ho in HOLDOUTS}
        if not any(data.values()):
            continue
        picks = []
        # HO180 を基準に銘柄を走査 (最も長い OOS 検証窓)
        for code, r180 in data[180].items():
            r90 = data[90].get(code)
            r30 = data[30].get(code)
            if not r90:
                continue

            # ── 選定は『選定窓内 TEST』のみで判定。直近HO日は一切見ない ──
            def selectable(r):
                return (r
                        and _f(r.get("total_test_pnl")) > 0
                        and _f(r.get("avg_test_pf")) >= args.min_test_pf
                        and _f(r.get("total_test_trades")) >= args.min_test_trades)

            if not (selectable(r180) and selectable(r90)):
                continue
            picks.append({
                "code": code, "name": r180.get("name", ""),
                "price": _f(r180.get("latest_price")),
                # 選定指標 (clean): TEST 損益・PF
                "test180": _f(r180.get("total_test_pnl")),
                "test90": _f(r90.get("total_test_pnl")),
                "pf180": _f(r180.get("avg_test_pf")),
                # 答え合わせ (純OOS, 表示のみ): holdout 損益
                "ho180": _f(r180.get("holdout_pnl")),
                "ho90": _f(r90.get("holdout_pnl")),
                "ho30": _f(r30.get("holdout_pnl")) if r30 else 0.0,
            })
        # ランクは選定指標(TEST 損益)で。holdout では並べない
        picks.sort(key=lambda x: -(x["test180"] + x["test90"]))
        if not picks:
            continue
        watchlist[strat] = [(p["code"], p["name"]) for p in picks]
        # 参考: 採用銘柄の holdout 合計 (選定後の純OOS実績がプラスかを目視)
        ho_sum = sum(p["ho180"] for p in picks)
        print(f"\n【{strat}】 採用 {len(picks)}銘柄  "
              f"(純OOS HO180合計 {ho_sum:+,.0f})")
        print(f"  {'銘柄':<9}{'名前':<20}{'株価':>7}"
              f" │選定 {'TEST180':>9}{'TEST90':>9}{'PF':>6}"
              f" │OOS {'HO180':>9}{'HO90':>9}")
        print("  " + "-" * 92)
        for p in picks:
            print(f"  {p['code']:<9}{p['name'][:18]:<20}{p['price']:>7,.0f}"
                  f" │    {p['test180']:>+9,.0f}{p['test90']:>+9,.0f}"
                  f"{p['pf180']:>6.2f}"
                  f" │    {p['ho180']:>+9,.0f}{p['ho90']:>+9,.0f}")

    # ── daytrade_watchlist.py 出力 ──
    total = sum(len(v) for v in watchlist.values())
    lines = [
        '"""daytrade_watchlist.py ― OOS堅牢なデイトレ WATCHLIST',
        f'build_watchlist_wf.py により {args.date} に自動生成。',
        '選定は WF TRAIN/TEST (選定窓=今日-HO日 以前) のみで実施。',
        '直近ホールドアウト期間(HO日)は銘柄選定に不使用 (純OOS答え合わせ専用)。',
        'HO90 & HO180 両CSVで total_test_pnl>0 / avg_test_pf>=1.0 の (銘柄×戦略)。',
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
