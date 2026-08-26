"""共通バックテストエンジン。

algo/data/<PAIR>m15.csv (MT5 由来, Europe/Helsinki, 価格は整数スケール) を読み、
JST に変換し、pip 建て / 円建ての損益を出す土台。

全戦略スクリプトはここを import して使う。時刻・スケール・スプレッドの扱いを
1 箇所に閉じ込めるのが目的。
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent / "data"
TZ_SRC = "Europe/Helsinki"   # MT5 ブローカーのサーバー時刻 (実測で確定, RESULTS_FX.md 参照)
TZ_JST = "Asia/Tokyo"

JPY_PAIRS = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"]
USD_PAIRS = ["EURUSD", "GBPUSD"]
ALL_PAIRS = JPY_PAIRS + USD_PAIRS

# CSV は価格を整数で持つ (80163 = 80.163円 / 129478 = 1.29478)
SCALE = {**{p: 1000.0 for p in JPY_PAIRS}, **{p: 100000.0 for p in USD_PAIRS}}
# 1 pip の大きさ (実価格ベース)
PIP = {**{p: 0.01 for p in JPY_PAIRS}, **{p: 0.0001 for p in USD_PAIRS}}
# 想定スプレッド (pip)。実測できるまでの暫定値。
SPREAD_PIP = {p: 0.2 for p in ALL_PAIRS}

_CACHE: dict[str, pd.DataFrame] = {}


def load(pair: str, tz: str = TZ_JST) -> pd.DataFrame:
    """1 ペアの 15 分足を実価格・指定タイムゾーンで返す。"""
    key = f"{pair}|{tz}"
    if key in _CACHE:
        return _CACHE[key]
    files = glob.glob(str(DATA_DIR / f"{pair}m15.csv"))
    if not files:
        raise FileNotFoundError(f"{pair} のデータがありません: {DATA_DIR}")
    df = pd.read_csv(files[0])
    idx = pd.DatetimeIndex(pd.to_datetime(df[df.columns[0]], errors="coerce"))
    idx = idx.tz_localize(TZ_SRC, ambiguous="NaT", nonexistent="NaT").tz_convert(tz)
    df.index = idx
    df = df[~df.index.isna()].copy()
    df.columns = [c.lower() for c in df.columns]
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] / SCALE[pair]
    df = df[["open", "high", "low", "close", "tick_volume"]]
    _CACHE[key] = df
    return df


def load_all(pairs: list[str] | None = None, tz: str = TZ_JST) -> dict[str, pd.DataFrame]:
    return {p: load(p, tz) for p in (pairs or ALL_PAIRS)}


def to_pip(pair: str, price_diff: float | pd.Series) -> float | pd.Series:
    """価格差を pip に変換。"""
    return price_diff / PIP[pair]


def net_pip(pair: str, entry: float, exit_: float, side: int) -> float:
    """スプレッド差引後の pip 損益。side=+1 買い / -1 売り。"""
    return side * (exit_ - entry) / PIP[pair] - SPREAD_PIP[pair]


def pip_value_jpy(pair: str, lot: int = 10000, usdjpy: float = 150.0) -> float:
    """1 pip あたりの円換算額。JPY クロスは為替換算不要。"""
    if pair in JPY_PAIRS:
        return PIP[pair] * lot            # 0.01 * 10000 = 100円/pip
    return PIP[pair] * lot * usdjpy       # ドルストレートは USDJPY で円換算


def summarize(trades: pd.DataFrame, pip_col: str = "pip", periods_per_year: float | None = None) -> dict:
    """トレード列 (pip) から成績指標を出す。

    trades: index に決済日時、pip_col に 1 トレードの pip 損益を持つ DataFrame。
    """
    if len(trades) == 0:
        return {"n": 0}
    p = trades[pip_col].astype(float)
    wins, losses = p[p > 0], p[p < 0]
    gp, gl = wins.sum(), -losses.sum()
    ret = {
        "n": len(p),
        "total_pip": p.sum(),
        "mean_pip": p.mean(),
        "sd_pip": p.std(ddof=1),
        "win_rate": (p > 0).mean() * 100,
        "pf": (gp / gl) if gl > 0 else np.inf,
        "t": (p.mean() / p.std(ddof=1) * np.sqrt(len(p))) if p.std(ddof=1) > 0 else 0.0,
        "max_dd_pip": float((p.cumsum() - p.cumsum().cummax()).min()),
        "max_consec_loss": _max_consec(p < 0),
    }
    if periods_per_year:
        # 1 トレード = 1 期間としたシャープ (年率換算)
        ret["sharpe"] = ret["mean_pip"] / ret["sd_pip"] * np.sqrt(periods_per_year) if ret["sd_pip"] > 0 else 0.0
        ret["trades_per_year"] = periods_per_year
    return ret


def _max_consec(mask: pd.Series) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def split_train_test(trades: pd.DataFrame, boundary: str = "2019-01-01") -> tuple[pd.DataFrame, pd.DataFrame]:
    """時系列で in-sample / out-of-sample に分ける。"""
    b = pd.Timestamp(boundary, tz=TZ_JST)
    return trades[trades.index < b], trades[trades.index >= b]
