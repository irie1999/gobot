"""時間帯エッジの総当たりスクリーニング。

「JST の h 時に入って k 時間持つ」を全組み合わせ試し、
train (2012-2018) で有意 & test (2019-2022) でも同符号のものだけを残す。

取引回数が多い雑なエッジを見つけるための最初の網。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

HOLDS = [1, 2, 3, 4, 6, 8, 12, 24]
BOUNDARY = "2019-01-01"


def hourly(pair: str) -> pd.DataFrame:
    df = engine.load(pair)
    h = df.resample("1h").agg(open=("open", "first"), high=("high", "max"),
                              low=("low", "min"), close=("close", "last"))
    return h.dropna()


def screen(pair: str, dow: int | None = None) -> pd.DataFrame:
    h = hourly(pair)
    o = h["open"]
    c = h["close"]
    rows = []
    for hold in HOLDS:
        # h 時 open で入り、hold 時間後の close で出る
        ex = c.shift(-(hold - 1))
        pip = (ex - o) / engine.PIP[pair] - engine.SPREAD_PIP[pair]
        pip = pip.dropna()
        for hr in range(24):
            s = pip[pip.index.hour == hr]
            if dow is not None:
                s = s[s.index.dayofweek == dow]
            if len(s) < 200:
                continue
            tr = s[s.index < pd.Timestamp(BOUNDARY, tz=engine.TZ_JST)]
            te = s[s.index >= pd.Timestamp(BOUNDARY, tz=engine.TZ_JST)]
            if len(tr) < 150 or len(te) < 80:
                continue
            for side in (+1, -1):
                a, b = tr * side, te * side
                # side を反転すると往復スプレッドが二重に引かれないよう補正
                adj = 2 * engine.SPREAD_PIP[pair] if side < 0 else 0.0
                a, b = a - adj, b - adj
                t_tr = a.mean() / a.std(ddof=1) * np.sqrt(len(a))
                t_te = b.mean() / b.std(ddof=1) * np.sqrt(len(b))
                rows.append({
                    "pair": pair, "hour": hr, "hold": hold, "side": side, "dow": dow,
                    "n_tr": len(a), "mean_tr": a.mean(), "t_tr": t_tr,
                    "n_te": len(b), "mean_te": b.mean(), "t_te": t_te,
                    "n": len(a) + len(b), "mean": (a.sum() + b.sum()) / (len(a) + len(b)),
                })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="*", default=engine.ALL_PAIRS)
    ap.add_argument("--dow", action="store_true", help="曜日別にも分ける")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", default="algo/results/screen_hourly.csv")
    a = ap.parse_args()

    frames = []
    for p in a.pairs:
        frames.append(screen(p))
        if a.dow:
            for d in range(5):
                frames.append(screen(p, dow=d))
    r = pd.concat(frames, ignore_index=True)
    r.to_csv(a.out, index=False)

    n_tests = len(r)
    thr = abs(np.sqrt(2) * 1.0)  # 表示用。実判定は下の Bonferroni
    bonf_t = 3.9 if n_tests < 5000 else 4.3
    print(f"検定した組み合わせ {n_tests}  (Bonferroni の目安 |t| > {bonf_t})\n")

    ok = r[(r["t_tr"] > bonf_t) & (r["t_te"] > 0)].sort_values("t_te", ascending=False)
    print(f"=== train で Bonferroni 突破 かつ test でも同方向: {len(ok)} 件 ===")
    if len(ok):
        print(ok.head(a.top).to_string(
            index=False,
            formatters={"mean_tr": "{:+.3f}".format, "mean_te": "{:+.3f}".format,
                        "t_tr": "{:+.2f}".format, "t_te": "{:+.2f}".format,
                        "mean": "{:+.3f}".format}))
    print(f"\n=== 参考: train の t 上位 (test 不問) ===")
    print(r.nlargest(a.top, "t_tr").to_string(
        index=False,
        formatters={"mean_tr": "{:+.3f}".format, "mean_te": "{:+.3f}".format,
                    "t_tr": "{:+.2f}".format, "t_te": "{:+.2f}".format,
                    "mean": "{:+.3f}".format}))
    print(f"\n保存: {a.out}")


if __name__ == "__main__":
    main()
