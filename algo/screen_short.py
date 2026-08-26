"""ショート方向のエッジ探索。

2012-2022 は構造的な円安 (JPY クロス上昇) 期間なので、素の検定では
ショートが不利に出る。そこで「素の結果」と「ドリフト除去後の結果」を
並べて出し、時間帯そのものに下向きの偏りがあるかを見る。

ドリフト除去 = そのペアの全期間の平均リターン (時間あたり) を差し引く。
これで「相場全体の上昇を除いても、この時間帯だけ下げているか」が分かる。
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

warnings.filterwarnings("ignore")

HOLDS = [1, 2, 3, 4, 6, 8, 12]
BOUNDARY = "2019-01-01"


def hourly(pair: str) -> pd.DataFrame:
    df = engine.load(pair)
    return df.resample("1h").agg(open=("open", "first"), close=("close", "last")).dropna()


def screen(pair: str, demean: bool) -> pd.DataFrame:
    h = hourly(pair)
    o, c = h["open"], h["close"]
    # 1 時間あたりの平均ドリフト (pip)
    drift = ((c - o) / engine.PIP[pair]).mean()
    b = pd.Timestamp(BOUNDARY, tz=engine.TZ_JST)
    rows = []
    for hold in HOLDS:
        pip = ((c.shift(-(hold - 1)) - o) / engine.PIP[pair]).dropna()
        if demean:
            pip = pip - drift * hold
        for hr in range(24):
            s = pip[pip.index.hour == hr]
            if len(s) < 300:
                continue
            # ショート: 符号反転 + 往復スプレッド
            s = -s - engine.SPREAD_PIP[pair]
            tr, te = s[s.index < b], s[s.index >= b]
            if len(tr) < 200 or len(te) < 100:
                continue
            f = lambda x: x.mean() / x.std(ddof=1) * np.sqrt(len(x))
            rows.append({"pair": pair, "hour": hr, "hold": hold,
                         "n": len(s), "mean": s.mean(), "t": f(s),
                         "n_tr": len(tr), "t_tr": f(tr),
                         "n_te": len(te), "t_te": f(te),
                         "win": (s > 0).mean() * 100})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="*", default=engine.ALL_PAIRS)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()

    for demean in (False, True):
        tag = "ドリフト除去後" if demean else "素 (トレンド込み)"
        r = pd.concat([screen(p, demean) for p in a.pairs], ignore_index=True)
        r.to_csv(f"algo/results/screen_short{'_demean' if demean else ''}.csv", index=False)
        bonf = 3.9
        ok = r[(r["t_tr"] > bonf) & (r["t_te"] > 0)].sort_values("t_te", ascending=False)
        print(f"\n{'='*70}\n### ショート {tag}  (検定 {len(r)} 通り, Bonferroni |t|>{bonf})")
        print(f"train 突破 かつ test 同方向: {len(ok)} 件")
        fm = {c: "{:+.2f}".format for c in ("mean", "t", "t_tr", "t_te", "win")}
        if len(ok):
            print(ok.head(a.top).to_string(index=False, formatters=fm))
        print(f"\n-- 参考: train の t 上位 --")
        print(r.nlargest(a.top, "t_tr").to_string(index=False, formatters=fm))


if __name__ == "__main__":
    main()
