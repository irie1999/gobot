"""検算用のダミー(このディレクトリだけ)。実物は /home/user/gobot/preopen_market.py"""
import numpy as np
def preopen_features(days):
    rng = np.random.default_rng(7)
    out = {}
    for d in days:
        out[d] = {"sp500_ret": float(rng.normal(0, 1)),
                  "vix": float(abs(rng.normal(18, 5))),
                  "n225_ret": float(rng.normal(0, 1.2)),
                  "fut_gap": float(rng.normal(0, 0.4)),
                  "usdjpy_ret": float(rng.normal(0, 0.5))}
    return out
