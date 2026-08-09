r"""analyze_sizing.py — lss取引CSVで「均等100株 vs 重複あり(100×戦略一致)」を
固定予算(既定400万円/日)の日次シミュで比較する。

背景: lssは同一銘柄が複数戦略(DON/MOM/MACDTF…)で同時シグナル化すると、既定では
100株×一致数(3戦略一致=300株)で発注する。ユーザー観察「重複より均等100株の方が良い」を、
実CSV(HTMLと同一トレード)で検証する。

重要: 実運用は1日の資金が有限(既定400万)。無制限で延べ損益を比べても意味がない。
  固定予算では:
    重複あり(weighted) : 高一致銘柄に100×一致(=300株)集中 → 少ない銘柄で枠が埋まる。
    均等   (flat)      : 1銘柄100株 → 同じ予算でより多くの銘柄を持てる(分散=lssのエッジ)。
  各営業日ごとに BT降順で予算まで詰め、両方式の合計損益を比較する。

lssは同一銘柄なら全戦略でトリガー/損切/目標が同一(=同じ1トレード)。よって:
  1銘柄の重複pnl = 一致数 × (100株あたりpnl) / 重複cost = 一致数 × 100株 × 発注価格。

使い方(あなたの機械):
  set LSS_TRADES_CSV=lss_trades.csv & .\daily   # CSVを最新化してから
  python analyze_sizing.py lss_trades.csv --bt-min 30 --budget 4000000
  python analyze_sizing.py lss_trades.csv --bt-min 40 --budget 4000000
  python analyze_sizing.py lss_trades.csv --bt-min 40 --budget 0   # 無制限(参考)
"""
from __future__ import annotations
import argparse
import csv
from collections import defaultdict

ap = argparse.ArgumentParser(description="lss 均等 vs 重複(100×一致) を固定予算日次で比較")
ap.add_argument("csv", help="LSS_TRADES_CSV で出力した取引CSV")
ap.add_argument("--bt-min", type=float, default=30.0, help="BTスコア下限(実投資集団)")
ap.add_argument("--budget", type=float, default=4_000_000,
                help="1営業日の投下上限(円)。既定400万。0=無制限")
args = ap.parse_args()

# 1) CSV読込 → (銘柄, 決済日) 単位に集約(一致数 n / 100株pnl / 発注価格 / BT)
stocks: dict = {}
for r in csv.DictReader(open(args.csv, encoding="utf-8-sig")):
    if r.get("reason") in ("発注中", "保有中"):
        continue
    try:
        bt = float(r.get("bt") or 0)
    except Exception:
        bt = 0.0
    if bt < args.bt_min:
        continue
    try:
        pnl = float(r.get("pnl") or 0)
        cost1 = float(r.get("order_limit") or r.get("entry_p") or 0)  # 100株あたり発注価格(想定額基準)
    except Exception:
        continue
    if cost1 <= 0:
        continue
    k = (r.get("symbol"), r.get("exit_date"))
    if k not in stocks:
        stocks[k] = {"bt": bt, "pnl": pnl, "cost1": cost1, "n": 0, "date": r.get("exit_date")}
    stocks[k]["n"] += 1   # 一致した戦略数(=重複行数)

by_day: dict = defaultdict(list)
for s in stocks.values():
    by_day[s["date"]].append(s)


def _sim(weighted: bool):
    """各営業日 BT降順で予算まで詰めて発注。weighted=True は 100×一致数、False は 100株固定。"""
    tot = gp = gl = cap_sum = 0.0
    names = wins = 0
    for _date, ds in by_day.items():
        ds = sorted(ds, key=lambda s: -s["bt"])
        cap = 0.0
        for s in ds:
            mult = s["n"] if weighted else 1
            cost = mult * 100 * s["cost1"]
            if args.budget > 0 and cap + cost > args.budget:
                continue   # 予算超過はスキップして次(より小さい/低BT)を詰める
            cap += cost
            pnl = mult * s["pnl"]
            tot += pnl
            names += 1
            if pnl > 0:
                wins += 1
                gp += pnl
            else:
                gl += -pnl
        cap_sum += cap
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    days = len(by_day)
    return {"tot": tot, "names": names, "wins": wins, "pf": pf,
            "avg_cap": cap_sum / days if days else 0.0, "days": days}


def _line(lbl, r):
    _pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
    _wr = r["wins"] / r["names"] * 100 if r["names"] else 0
    return (f"  {lbl:<22} 合計={r['tot']:>+13,.0f}  発注延べ={r['names']:>5}  "
            f"1件平均={r['tot']/r['names'] if r['names'] else 0:>+8,.0f}  "
            f"勝率={_wr:>4.0f}%  PF={_pf:>5}  1日平均投下={r['avg_cap']:>12,.0f}")


w = _sim(True)
f = _sim(False)
_bud = "無制限" if args.budget <= 0 else f"{args.budget:,.0f}円/日"
print(f"\nBT{args.bt_min:.0f}以上 lss  均等 vs 重複  (予算={_bud}, {w['days']}営業日, {args.csv})")
print("=" * 104)
print(_line("重複あり(100×一致)", w))
print(_line("均等   (1銘柄100株)", f))
print("=" * 104)
_diff = f["tot"] - w["tot"]
print(f"  → 合計損益差(均等 − 重複) = {_diff:>+13,.0f}円")
if args.budget > 0:
    print("  読み方(固定予算): 合計損益が高い方が、その予算で稼げる方式。")
    print("    均等が上 → 少額×多銘柄の分散が勝つ(あなたの観察が正しい)。")
    print("    重複が上 → 高一致銘柄への集中が予算内でも勝つ。")
    print("    ※ 発注延べ=延べ発注銘柄数(均等の方が多い=分散大)。1日平均投下=実際に使った資金。")
else:
    print("  ※ 無制限では重複が延べで有利に出やすい(高一致=勝ちやすい銘柄に無制限に賭けられるため)。")
    print("    実運用は予算有限なので --budget 4000000 で見ること。")
