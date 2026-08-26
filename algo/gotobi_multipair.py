"""gotobi_multipair.py — ゴトー日効果を複数通貨ペアで検証する。

  python algo/gotobi_multipair.py

事前に algo/fetch_fx_data.py で各ペアの m15 を取得しておく:
  for p in USDJPY EURJPY GBPJPY AUDJPY EURUSD GBPUSD; do
      python algo/fetch_fx_data.py --pair $p --tf m15
  done

検証の狙い:
  ① クロス円で再現するか   — 円の実需が原因なら、円絡み全部に出るはず
  ② 非円ペアで出ないか     — ★偽陽性の検出テスト。出たら解析が間違っている
  ③ 分散の効果            — 複数ペアに分けるとリスクが下がるか
"""
from __future__ import annotations
import sys
from math import sqrt
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent / "data"
TZ_SRC = "Europe/Helsinki"          # ブローカー時間(実証済み)
ENTRY, EXIT = "03:00", "09:45"      # JST
SPREAD_PCT = 0.002 / 150 * 100      # 0.2銭相当
JPY = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"]
NON_JPY = ["EURUSD", "GBPUSD"]


def load(pair: str) -> pd.DataFrame:
    f = DATA / f"{pair}m15.csv"
    if not f.exists():
        raise FileNotFoundError(f"{f} がありません。fetch_fx_data.py で取得してください")
    d = pd.read_csv(f)
    d["Date"] = pd.to_datetime(d["Date"])
    idx = pd.DatetimeIndex(d["Date"]).tz_localize(TZ_SRC, ambiguous="NaT", nonexistent="NaT")
    d.index = idx
    d = d[~d.index.isna()]
    d.index = d.index.tz_convert("Asia/Tokyo")
    for c in ("open", "close"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d.dropna(subset=["open", "close"]).sort_index()


def is_gotobi(ts: pd.Timestamp) -> bool:
    return ts.day in (5, 10, 15, 20, 25) or ts.is_month_end


def day_ret(g: pd.DataFrame) -> float:
    s = g.between_time(ENTRY, EXIT)
    return np.nan if len(s) == 0 else (s["close"].iloc[-1] / s["open"].iloc[0] - 1) * 100


def welch(a, b):
    a = np.asarray([x for x in a if not np.isnan(x)])
    b = np.asarray([x for x in b if not np.isnan(x)])
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    return a.mean(), b.mean(), (a.mean() - b.mean()) / sqrt(va + vb)


def main() -> None:
    series = {}
    print("=" * 86)
    print(f"通貨ペア横断 — ゴトー日 {ENTRY}→{EXIT} (JST)")
    print("=" * 86)
    print(f"  {'ペア':<9} {'ゴトー日%':>10} {'非ゴトー日%':>11} {'差':>9} {'Welch t':>9} {'勝率%':>7} {'円':>4}")
    for pair in JPY + NON_JPY:
        try:
            d = load(pair)
        except FileNotFoundError as e:
            print(f"  {pair:<9} skip ({e})")
            continue
        days = {k: g for k, g in d.groupby(d.index.normalize()) if len(g) > 10}
        gb = {k: day_ret(g) for k, g in days.items() if is_gotobi(k)}
        ng = [day_ret(g) for k, g in days.items() if not is_gotobi(k)]
        mg, mn, t = welch(list(gb.values()), ng)
        wr = np.nanmean(np.asarray([v for v in gb.values() if not np.isnan(v)]) > 0) * 100
        print(f"  {pair:<9} {mg:>+10.4f} {mn:>+11.4f} {mg-mn:>+9.4f} {t:>+9.2f} {wr:>7.1f} {'○' if pair in JPY else '×':>4}")
        if pair in JPY:
            series[pair] = pd.Series(gb).dropna()

    print("\n  ★ 非円ペア(EURUSD/GBPUSD)で効果が出たら、解析が間違っている合図。")

    if len(series) < 2:
        sys.exit("\nクロス円のデータが足りません。")

    M = pd.DataFrame(series).dropna()
    print("\n" + "=" * 86)
    print(f"分散の効果 — 4ペア均等 ({len(M)}日)")
    print("=" * 86)
    print("\n--- ペア間相関 ---")
    print(M.corr().round(3).to_string())

    eq = M.mean(axis=1)
    print(f"\n  {'':<12} {'平均%':>9} {'標準偏差':>9} {'t値':>8} {'勝率%':>7}")
    for p in M.columns:
        x = M[p]
        print(f"  {p:<12} {x.mean():>+9.4f} {x.std(ddof=1):>9.4f} "
              f"{x.mean()/(x.std(ddof=1)/sqrt(len(x))):>+8.2f} {(x>0).mean()*100:>7.1f}")
    t_eq = eq.mean() / (eq.std(ddof=1) / sqrt(len(eq)))
    print(f"  {'均等分散':<12} {eq.mean():>+9.4f} {eq.std(ddof=1):>9.4f} {t_eq:>+8.2f} {(eq>0).mean()*100:>7.1f}  ★")
    print(f"\n  標準偏差: 単独平均 {M.std(ddof=1).mean():.4f} → 分散 {eq.std(ddof=1):.4f} "
          f"({(1-eq.std(ddof=1)/M.std(ddof=1).mean())*100:.1f}% 減)")

    yrs = (M.index.max() - M.index.min()).days / 365.25
    net = eq.mean() - SPREAD_PCT
    print(f"\n--- 実用性(均等分散・コスト後) ---")
    print(f"  年 {len(M)/yrs:.1f}回 × {net:+.4f}% = 年 {len(M)/yrs*net:+.2f}% (レバ1倍)")
    for lev in (3, 5, 10):
        print(f"    レバ{lev:>2}倍 → 年 {len(M)/yrs*net*lev:+.2f}%")

    print("\n--- 期間別 ---")
    for lo, hi in [(2012, 2014), (2015, 2016), (2017, 2018), (2019, 2020), (2021, 2022)]:
        g = eq[(eq.index.year >= lo) & (eq.index.year <= hi)]
        if len(g) < 10:
            continue
        print(f"  {lo}-{hi}  n={len(g):>3}  {g.mean():+.4f}%  "
              f"t={g.mean()/(g.std(ddof=1)/sqrt(len(g))):+.2f}  勝率 {(g>0).mean()*100:5.1f}%")


if __name__ == "__main__":
    main()
