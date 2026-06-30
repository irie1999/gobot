"""
build_watchlist_wf.py  ―  WFスキャン結果(wf_strategies_*_ho*.csv)から
                          OOS堅牢なデイトレ WATCHLIST を確定する。

★重要な原則 (2026-06-30 改修): 直近ホールドアウト期間(HO日数)は銘柄選定に
  一切使わない。選定は WF の TRAIN/TEST 成績 (= 選定窓 base=今日-HO日 以前の
  データのみ) だけで行い、holdout_* (直近HO日の成績) は『答え合わせ用の
  純OOS指標』として表示するだけ。フィルタには絶対に使わない。

選定方針 (既定): HO180 の CSV 1本だけで選定する。これにより直近180日が
まるごとクリーンOOS (どの選定窓も触っていない) になり、答え合わせ期間が最長。
  1. 候補 = HO180 CSV の合格銘柄 (= WF fold を 2/2 通過済み) のうち
       total_test_pnl > 0 / avg_test_pf >= --min-test-pf / test_trades >= 3
     を満たすもの (= 幅広い候補プール)
  2. その候補を『選定スコア』(TEST窓の PF/勝率/取引数 だけで算出。holdout不使用)
     で並べ、戦略あたり上位 --per-strategy 銘柄に厳選
holdout_* はテーブルに OOS 検証列として併記するだけ (選定には絶対に不使用)。

--strict-both を付けると HO90&HO180 両方合格を要求 (クリーンOOSは直近90日に縮む)。
--select-ho 90 で選定窓を直近90日除外に変更可。

出力:
  daytrade_watchlist.py    … WATCHLIST = {戦略: [(code, name), ...]} の Python
  画面に戦略別ランキング (Score=選定指標 と HO=純OOS答え合わせ を並記)

使い方:
  python build_watchlist_wf.py                          # HO180選定・上位10銘柄/戦略
  python build_watchlist_wf.py --per-strategy 6 --min-test-pf 1.5
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
              "OpenMomentum", "MACDBreak", "StochATR", "VolSurge", "StopShort",
              "ORB"]
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


def _test_score(r) -> float:
    """選定スコア (0〜100)。TEST窓のデータだけで算出。holdoutは一切使わない。
       PF 40点 + 勝率 40点 + 取引数 20点。"""
    pf = min(_f(r.get("avg_test_pf")), 10.0)
    wr = _f(r.get("avg_test_wr"))          # 0〜100
    tr = _f(r.get("total_test_trades"))
    return round((pf / 10) * 40 + (wr / 100) * 40 + min(tr / 30, 1) * 20, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).date().isoformat())
    ap.add_argument("--select-ho", type=int, default=180, choices=[30, 90, 180],
                    help="選定に使うホールドアウトCSV。既定180=直近180日を"
                         "選定に使わない=クリーンOOSが最長(180日)")
    ap.add_argument("--per-strategy", type=int, default=10,
                    help="戦略あたり採用する上位銘柄数 (選定スコア順)")
    ap.add_argument("--min-test-pf", type=float, default=1.2,
                    help="選定窓 TEST の最小平均PF (既定 1.2)")
    ap.add_argument("--min-test-trades", type=int, default=3,
                    help="選定窓 TEST の最小取引数 (既定 3)")
    ap.add_argument("--strict-both", action="store_true",
                    help="HO90&HO180両方で合格を要求 (クリーンOOSは直近90日に縮む)")
    ap.add_argument("--strategies", nargs="+", default=["Donchian", "Pivot"],
                    help="採用する戦略 (既定: Donchian Pivot = クリーンOOSでプラス確認済の2戦略)。"
                         " 全戦略を見るなら --strategies "
                         "Donchian GapReversal VWAP RSI Pivot OpenMomentum MACDBreak StochATR VolSurge")
    ap.add_argument("--out", default="daytrade_watchlist.py")
    args = ap.parse_args()
    use_strategies = [s for s in STRATEGIES if s in set(args.strategies)]

    sho = args.select_ho
    watchlist: dict[str, list] = {}
    print("=" * 104)
    print(f"OOS堅牢 WATCHLIST 選定  (date={args.date})")
    print(f"  選定窓: HO{sho} のCSV (直近{sho}日は選定に不使用＝クリーンOOS{sho}日)"
          + ("  +HO90も合格必須" if args.strict_both else ""))
    print(f"  候補条件(TESTのみ): total_test_pnl>0 / avg_test_pf>={args.min_test_pf}"
          f" / test_trades>={args.min_test_trades}")
    print(f"  厳選: 選定スコア(TEST窓のPF/勝率/取引数) 上位{args.per_strategy}銘柄/戦略")
    print(f"  ※ HO* 列は選定に使っていない純OOS答え合わせ (表示のみ・選定不使用)")
    print(f"  採用戦略: {', '.join(use_strategies)}")
    print("=" * 104)

    def selectable(r):
        return (r
                and _f(r.get("total_test_pnl")) > 0
                and _f(r.get("avg_test_pf")) >= args.min_test_pf
                and _f(r.get("total_test_trades")) >= args.min_test_trades)

    for strat in use_strategies:
        data = {ho: _load(strat, ho, args.date) for ho in HOLDOUTS}
        pool = data.get(sho, {})
        if not pool:
            continue
        cand = []
        for code, r in pool.items():
            if not selectable(r):
                continue
            r90 = data[90].get(code)
            if args.strict_both and not selectable(r90):
                continue
            cand.append({
                "code": code, "name": r.get("name", ""),
                "price": _f(r.get("latest_price")),
                "score": _test_score(r),
                "test": _f(r.get("total_test_pnl")),
                "pf": _f(r.get("avg_test_pf")),
                "wr": _f(r.get("avg_test_wr")),
                # 答え合わせ (純OOS, 表示のみ)
                "ho_sel": _f(r.get("holdout_pnl")),       # 選定窓のholdout=クリーン
                "ho90": _f(r90.get("holdout_pnl")) if r90 else 0.0,
            })
        if not cand:
            continue
        # 選定スコア降順 → 上位 per-strategy
        cand.sort(key=lambda x: -x["score"])
        picks = cand[:args.per_strategy]
        watchlist[strat] = [(p["code"], p["name"]) for p in picks]
        ho_sum = sum(p["ho_sel"] for p in picks)
        print(f"\n【{strat}】 候補{len(cand)} → 採用{len(picks)}銘柄  "
              f"(純OOS HO{sho}合計 {ho_sum:+,.0f})")
        print(f"  {'銘柄':<9}{'名前':<20}{'株価':>7}"
              f" │ {'Score':>6}{'TEST':>9}{'PF':>6}{'WR':>5}"
              f" │OOS {('HO'+str(sho)):>9}{'HO90':>9}")
        print("  " + "-" * 96)
        for p in picks:
            print(f"  {p['code']:<9}{p['name'][:18]:<20}{p['price']:>7,.0f}"
                  f" │ {p['score']:>6.1f}{p['test']:>+9,.0f}{p['pf']:>6.2f}"
                  f"{p['wr']:>4.0f}%"
                  f" │    {p['ho_sel']:>+9,.0f}{p['ho90']:>+9,.0f}")

    # ── daytrade_watchlist.py 出力 ──
    total = sum(len(v) for v in watchlist.values())
    lines = [
        '"""daytrade_watchlist.py ― OOS堅牢なデイトレ WATCHLIST',
        f'build_watchlist_wf.py により {args.date} に自動生成。',
        f'選定窓: HO{sho} のCSV (直近{sho}日は選定に不使用＝クリーンOOS{sho}日)。',
        '選定は WF TRAIN/TEST のみで実施し、holdout(直近HO日)は選定に不使用。',
        f'候補(TESTのみ): test_pnl>0 / avg_test_pf>={args.min_test_pf} を満たす中から',
        f'選定スコア(TEST窓のPF/勝率/取引数)上位{args.per_strategy}銘柄/戦略を採用。',
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
