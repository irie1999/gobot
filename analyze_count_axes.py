#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N の「件数」3軸を1つの表に並べる（調査用・発注しない）。

⛔ **日々の手順には入れない。** CSV を読むだけ。

★ なぜ要るのか（2026-09-07）

  「予算なし(全建て)」タブで **合格件数 → 損益 r=-0.873** が出た。
  ところが実際の発注条件に戻すと、3段階で薄まる:

      予算なし・watch なし   r = -0.873  [-0.95, -0.70]
      予算400万・watch50     r = -0.446  [-0.74, -0.00]
      09:00:36 の早期カウント r = -0.300  [-0.66, +0.16]  ← 実装できる形

  そして **ワースト5日は判定時点で全部「中央値付近」に見えた**（08-05 は
  9件 = ちょうど中央値。最終31件になるのは09:01以降に寄る銘柄）。

  この表は「どの段階の件数なら発注判断に使えるか」を1枚で見るためのもの。

★★ 3軸は **いつ分かるか** が決定的に違う

  | 軸 | いつ確定するか | 発注判断に使えるか |
  |---|---|---|
  | cand (前夜の候補) | **前夜**。ret1 >= 閾値 の銘柄数 | ✅ **コストゼロで使える** |
  | hit (09:00の合格) | **09:10**（遅寄りが最後まで来る） | ⛔ 発注はもう終わっている |
  | built (建てた数)  | 事後 | ⛔ 使えない |

  ⛔ **hit で良い結果が出ても採用できない。** 実装できるのは cand だけ。
    hit は「もし分かっていたら」の上限値として置く。

使い方
    # ① レポートに日次CSVを出させる
    $env:LSS_NEWGAP_DAYS_CSV = "n_days.csv"
    .\dailyfast --no-serve
    $env:LSS_NEWGAP_DAYS_CSV = $null

    # ② 3軸を並べる
    python analyze_count_axes.py --csv n_days.csv

⚠ 前例は良くない。§18.65 ① で `--sweep-cands` が前夜の候補数を3軸×5分位で
  掃いて **3条件すべて不合格**（TRAIN/TEST で最悪分位が入れ替わる /
  ランダム帯の外に出た2つはどちらも逆側 / 帰無 p=0.056）。
  §18.13 の「同日発注数」も候補ゼロ。**同じ軸の3回目。**
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--csv", default="n_days.csv",
                help="LSS_NEWGAP_DAYS_CSV が出した日次CSV")
ap.add_argument("--band", type=int, default=400, help="ランダム帯の本数")
ap.add_argument("--split", type=float, default=0.5,
                help="TRAIN/TEST の分割位置（0.5 = 前半/後半）")
a = ap.parse_args()


def _ci(r: float, n: int) -> tuple[float, float]:
    if n < 5:
        return (-1.0, 1.0)
    z = np.arctanh(np.clip(r, -0.999999, 0.999999)); se = 1 / np.sqrt(n - 3)
    return float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se))


def _cor(x: pd.Series, y: pd.Series) -> tuple[float, float, float, int]:
    m = x.notna() & y.notna()
    n = int(m.sum())
    if n < 5 or x[m].std() == 0:
        return 0.0, -1.0, 1.0, n
    r = float(np.corrcoef(x[m], y[m])[0, 1])
    lo, hi = _ci(r, n)
    return r, lo, hi, n


def _cvar(v: np.ndarray, q: float = 0.05) -> float:
    if not len(v):
        return 0.0
    k = max(1, int(np.ceil(len(v) * q)))
    return float(np.sort(v)[:k].mean())


def main() -> int:
    try:
        d = pd.read_csv(a.csv, encoding="utf-8-sig")
    except FileNotFoundError:
        print(f"[error] {a.csv} がありません。先にこれを実行してください:")
        print('  $env:LSS_NEWGAP_DAYS_CSV = "n_days.csv"')
        print("  .\\dailyfast --no-serve")
        print("  $env:LSS_NEWGAP_DAYS_CSV = $null")
        return 1
    need = {"date", "cand", "hit", "built", "pnl"}
    if need - set(d.columns):
        print(f"[error] {a.csv} に {sorted(need - set(d.columns))} がありません")
        print(f"  ある列: {list(d.columns)}")
        return 1
    d = d.sort_values("date").reset_index(drop=True)
    # 建てていない日は判断の対象外（ルールを掛ける余地がない）
    d = d[d["built"] > 0].reset_index(drop=True)
    n = len(d)
    if n < 40:
        print(f"[error] 建てた日が {n} 日しかありません。窓を広げてください")
        print("  $env:LSS_NEWGAP_DAYS = '2000'  など")
        return 1

    print("=" * 78)
    print(f"■ N の件数3軸 — {n}営業日 ({d['date'].iloc[0]} 〜 {d['date'].iloc[-1]})")
    print("=" * 78)
    print(f"  合計 {d['pnl'].sum():+,.0f}円 / 日平均 {d['pnl'].mean():+,.0f} / "
          f"勝日 {int((d['pnl'] > 0).sum())}/{n}")
    print()
    print(f"  {'軸':>22} {'中央':>6} {'最大':>6} {'→損益 r':>9} {'95%CI':>18} "
          f"{'発注判断に使えるか'}")
    print("  " + "-" * 88)
    AX = [("cand", "前夜の候補数", "✅ 前夜に確定。コストゼロ"),
          ("hit", "09:00の合格数(最終)", "⛔ 09:10 まで確定しない = 発注後"),
          ("built", "建てた数", "⛔ 事後。判断には使えない")]
    for c, lab, note in AX:
        r, lo, hi, k = _cor(d[c], d["pnl"])
        star = "★" if lo * hi > 0 else "ゼロをまたぐ"
        print(f"  {lab:>22} {d[c].median():>6.0f} {d[c].max():>6.0f} "
              f"{r:>+9.3f} {f'[{lo:+.2f},{hi:+.2f}]':>18} {star:<12} {note}")
    print()
    print("  ⛔ hit で良い結果が出ても **採用できません**。09:10 まで確定せず、")
    print("     そのときには発注が終わっています。実装できるのは cand だけです。")

    # ── 実装できる軸 (cand) だけを掃く ──
    rng = np.random.default_rng(0)
    pnl = d["pnl"].to_numpy(float)
    half = int(n * a.split)
    for c, lab, _ in AX:
        if c != "cand":
            continue
        print("\n" + "=" * 78)
        print(f"■ 『{lab} ≥ X なら建てない』を掃く（実装できる唯一の軸）")
        print("=" * 78)
        v = d[c].to_numpy(float)
        qs = np.quantile(v, [0.5, 0.6, 0.7, 0.8, 0.9])
        print(f"  {'X':>6} {'休む日':>6} {'ルール':>12} {'現行差':>11} "
              f"{'一律縮小':>12} {'②−③':>11} {'CVaR差':>11} {'帯z':>7} "
              f"{'前半':>9} {'後半':>9}")
        print("  " + "-" * 104)
        for X in qs:
            m = v >= X
            k = int(m.sum())
            if k < 3 or k > n - 10:
                continue
            rule = float(pnl[~m].sum())
            flat = float(pnl.sum()) * (1 - k / n)
            cv_r = _cvar(pnl[~m]); cv_f = _cvar(pnl * (1 - k / n))
            # 同じ日数をランダムに休む帯（片側。CVaR は負なので z>0 が「軽い」）
            rc = []
            for _ in range(a.band):
                mm = np.zeros(n, dtype=bool)
                mm[rng.choice(n, k, replace=False)] = True
                rc.append(float(pnl[~mm].sum()))
            rc = np.array(rc)
            z = (rule - rc.mean()) / rc.std(ddof=1) if rc.std(ddof=1) else 0.0
            # 前半/後半で符号が揃うか（§18.36 判定ルール2）
            h1 = float(pnl[:half][~m[:half]].sum()) - float(pnl[:half].sum()) \
                * (1 - m[:half].sum() / max(half, 1))
            h2 = float(pnl[half:][~m[half:]].sum()) - float(pnl[half:].sum()) \
                * (1 - m[half:].sum() / max(n - half, 1))
            print(f"  {X:>6.0f} {k:>6} {rule:>12,.0f} {rule - pnl.sum():>+11,.0f} "
                  f"{flat:>12,.0f} {rule - flat:>+11,.0f} {cv_r - cv_f:>+11,.0f} "
                  f"{z:>+7.2f} {h1:>+9,.0f} {h2:>+9,.0f}")
        print(f"\n  現行(全部建てる) {pnl.sum():+,.0f}")
        print("  ② ルール / ③ 同じ日数ぶん一律にサイズを落とした世界（§18.64 ⑤）")
        print("  ⛔ ②−③ がプラスでも、**前半と後半の符号が揃わなければノイズ**")
        print("     （§18.36 判定ルール2）。帯z は片側で +2.0 以上が『帯の外』")

    print("\n" + "=" * 78)
    print("■ 前例（読む前に知っておくこと）")
    print("=" * 78)
    print("  §18.65 ①  前夜の候補数を3軸×5分位で掃いた → **3条件すべて不合格**")
    print("             ・TRAIN と TEST で最悪分位が入れ替わる")
    print("             ・帯の外に出た2つは **どちらも逆側**(ランダムより重い)")
    print("             ・帰無較正 p=0.056（実測 0.539 / 帰無95%点 0.540）")
    print("  §18.13    同日発注数 → 候補ゼロ")
    print("  §18.24    日内上位N → 候補ゼロ")
    print("  ⚠ **同じ軸の3回目です。** ここで何か出たら、まず測り方を疑ってください")
    return 0


if __name__ == "__main__":
    sys.exit(main())
