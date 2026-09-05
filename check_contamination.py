r"""check_contamination.py — 汚染日(5分足の分割未調整)がどの検証結果に混ざっていたかを突合する。

`intraday_integrity.py` が出した `intraday_integrity.csv`(銘柄×日)と、
トレードCSV(`lss_trades.csv` / `oos_raw_fold*.csv` / `compare_lss_rules_*.csv` 等)を
突き合わせ、**汚染日に建てた取引が何件・いくら含まれていたか**を出す。

汚染は `max(stop, bar_open)` が片側にだけ効くので **必ず損失方向**に出る。
つまりここで見つかったマイナスは、v17(ガードあり)で測り直すと消える。

使い方
------
  python check_contamination.py --trades "oos_raw_fold*.csv"
  python check_contamination.py --trades lss_trades.csv
  python check_contamination.py --trades lss_trades.csv --integrity intraday_integrity.csv

トレードCSVは列名を自動判別する(symbol/sym、entry_date/date、pnl)。
"""
from __future__ import annotations

import argparse
import glob as _glob
import sys

import pandas as pd

ap = argparse.ArgumentParser(description="汚染日がトレードに混ざっていたかを突合する")
ap.add_argument("--integrity", default="intraday_integrity.csv",
                help="intraday_integrity.py の出力")
ap.add_argument("--trades", required=True, help="トレードCSV(グロブ可)")
ap.add_argument("--top", type=int, default=15)
a = ap.parse_args()

try:
    ig = pd.read_csv(a.integrity, encoding="utf-8-sig")
except Exception as e:
    sys.exit(f"[error] {a.integrity} を読めません: {e}\n"
             f"        先に python intraday_integrity.py を実行してください。")
ig["date"] = pd.to_datetime(ig["date"])
badset = set(zip(ig["symbol"].astype(str), ig["date"].dt.date))
print(f"[汚染日] {len(badset):,}銘柄日 / {ig['symbol'].nunique()}銘柄 "
      f"/ {ig['date'].min().date()}〜{ig['date'].max().date()}")

files = sorted(_glob.glob(a.trades)) if any(c in a.trades for c in "*?[") else [a.trades]
if not files:
    sys.exit(f"[error] {a.trades} に一致するファイルがありません")
d = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in files], ignore_index=True)

_sym = next((c for c in ("symbol", "sym", "銘柄") if c in d.columns), None)
_dt = next((c for c in ("entry_date", "date", "約定日", "日付") if c in d.columns), None)
_pnl = next((c for c in ("pnl", "pnl_現実", "損益") if c in d.columns), None)
if not (_sym and _dt):
    sys.exit(f"[error] 銘柄/日付の列が見つかりません: {list(d.columns)[:12]}")
d["_d"] = pd.to_datetime(d[_dt], errors="coerce").dt.date
if "filled" in d.columns:                 # 生CSVは不約定行を含む
    d = d[d["filled"] == 1]
if "strategy" in d.columns:               # 転換は lss ではない(18.5.3)
    d = d[d["strategy"].astype(str) != "転換"]

hit = pd.Series([(s, x) in badset for s, x in zip(d[_sym].astype(str), d["_d"])],
                index=d.index)
print(f"\n[突合] {' / '.join(files[:3])}{' ...' if len(files) > 3 else ''}")
print(f"  取引 {len(d):,}件 / {d['_d'].min()}〜{d['_d'].max()}")
print(f"  **汚染日の取引 {int(hit.sum()):,}件 ({hit.mean() * 100:.2f}%)**")
if _pnl:
    tot, bad_p = d[_pnl].sum(), d.loc[hit, _pnl].sum()
    print(f"  全体損益   {tot:>+14,.0f}円 ({d[_pnl].mean():+,.0f}円/件)")
    print(f"  汚染分     {bad_p:>+14,.0f}円 "
          f"({d.loc[hit, _pnl].mean() if hit.sum() else 0:+,.0f}円/件)")
    print(f"  除いた場合 {tot - bad_p:>+14,.0f}円 "
          f"({d.loc[~hit, _pnl].mean():+,.0f}円/件)")
    if hit.sum():
        print(f"\n  → 汚染は max(stop, bar_open) が片側に効くので**必ず損失方向**。")
        print(f"     v17(ガードあり)で測り直すと約 {-bad_p:+,.0f}円 ぶん改善するはず。")

if hit.sum():
    print(f"\n  銘柄別(損失の大きい順 上位{a.top}):")
    cols = {"n": (_pnl or _sym, "count")}
    g = d[hit].groupby(_sym)
    agg = g[_pnl].agg(["count", "sum", "mean"]).sort_values("sum") if _pnl \
        else g.size().to_frame("count")
    for s, r in agg.head(a.top).iterrows():
        if _pnl:
            print(f"    {s:<10}{int(r['count']):>5}件 {r['sum']:>+13,.0f}円 "
                  f"{r['mean']:>+9,.0f}円/件")
        else:
            print(f"    {s:<10}{int(r['count']):>5}件")
else:
    print("\n  → 汚染日に建てた取引はありませんでした。この結果は汚染されていません。")
