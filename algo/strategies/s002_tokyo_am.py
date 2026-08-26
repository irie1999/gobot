"""S002 東京朝の円売りドリフト (毎日版)

------------------------------------------------------------------
ステータス : 検証中 (パラメータ調整用)
確定日     : 2026-08-26
検証期間   : 2012-11 〜 2022-03
------------------------------------------------------------------

■ ロジック
  営業日ごとに、4 つの円クロスを ENTRY (JST) に成行で買い、EXIT (JST) に決済する。
  ゴトー日に限定しない = S001 の約 4 倍の取引回数。

■ なぜ動くか (一文)
  日本の輸入企業・機関投資家の外貨需要は仲値 (09:55 JST) 前に持ち込まれるため、
  東京早朝から仲値手前にかけて円が売られやすい。ゴトー日はその増幅版にすぎない。

■ 発見の経緯
  screen_hourly.py で JST 時間帯 x 保有時間を総当たり (2304 通り) した結果、
  train (2012-2018) で Bonferroni を突破し test (2019-2022) でも同方向だったのは
  GBPJPY 6時ロングのみ。それを 4 ペアに拡張し 15 分刻みで詰めたのが本戦略。

■ パラメータ
  ENTRY / EXIT  : 入る時刻と出る時刻 (JST)。ここが主な調整対象。
  PAIRS         : 対象ペア。全ペア単独でプラス。
  SPREAD        : engine.SPREAD_PIP。実測値に置き換えること。

■ 頑健性 (06:00 -> 08:45 の場合, スプレッド 0.2pip 前提)
  全体             n=2185 +2.35pip t=+6.78 勝率61.0%
  2021 除外        n=1946 +2.03pip t=+5.27
  ゴトー日除外        n=1757 +2.07pip t=+5.16   <- S001 とは別物である証拠
  ゴトー日+2021+月曜除外 n=1371 +1.08pip t=+2.32   <- 三重に削っても生存
  全 10 年で各年プラス。全 4 ペアで単独プラス (t=+4.15〜+7.49)。

■ 既知の弱点 (最重要)
  - スプレッド感応度が高い。0.2pip なら t=+6.78、1.0pip で t=+4.47、
    2.0pip で t=+1.59 と消える。1 回の利幅が小さいので実測が必須。
  - 2021 年だけ異常に強い (+5.02pip 勝率81.2%)。除外しても生き残るが要監視。
  - ENTRY=06:00 は月曜が週明けオープン直後にあたり、データ欠損が 9% ある。
    アーティファクトを避けるなら ENTRY=07:00 (欠損ゼロ) を使う。
    ただし 07:00 は成績が落ちる (+1.73pip t=+5.38)。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402

warnings.filterwarnings("ignore")

NAME = "S002_tokyo_am"
PAIRS = engine.JPY_PAIRS
ENTRY = "06:00"
EXIT = "08:45"
LOT = 10000

_PIVOT: dict[str, pd.DataFrame] = {}


def _pivot(pair: str) -> pd.DataFrame:
    """日付 x 時刻(HH:MM) の始値テーブル。時刻指定の総当たりを高速化する。"""
    if pair not in _PIVOT:
        df = engine.load(pair)
        d = pd.DataFrame({"date": df.index.normalize(),
                          "tod": df.index.strftime("%H:%M"),
                          "open": df["open"].values})
        _PIVOT[pair] = d.pivot_table(index="date", columns="tod", values="open")
    return _PIVOT[pair]


def run(entry: str = ENTRY, exit_: str = EXIT, pairs: list[str] | None = None) -> pd.DataFrame:
    """1 列 = 1 ペアの pip 損益、index = 日付。"""
    cols = []
    for p in pairs or PAIRS:
        t = _pivot(p)
        if entry not in t.columns or exit_ not in t.columns:
            raise KeyError(f"{p}: {entry} または {exit_} の足がありません")
        cols.append(((t[exit_] - t[entry]) / engine.PIP[p] - engine.SPREAD_PIP[p]).rename(p))
    return pd.concat(cols, axis=1, sort=True)


def equal_weight(w: pd.DataFrame) -> pd.Series:
    return w.mean(axis=1).dropna()


def stats(s: pd.Series) -> dict:
    t = s.mean() / s.std(ddof=1) * np.sqrt(len(s)) if len(s) > 2 else 0.0
    return {"n": len(s), "mean_pip": s.mean(), "t": t, "win": (s > 0).mean() * 100,
            "sharpe": s.mean() / s.std(ddof=1) * np.sqrt(252) if s.std(ddof=1) else 0.0,
            "max_dd_pip": float((s.cumsum() - s.cumsum().cummax()).min())}


def yen(w: pd.DataFrame, lot: int = LOT) -> pd.Series:
    """4 ペア各 lot 通貨を同時に建てた場合の日次円損益。"""
    return sum(w[p].fillna(0) * engine.pip_value_jpy(p, lot) for p in w.columns)


def sweep(entries: list[str], exits: list[str]) -> pd.DataFrame:
    """ENTRY / EXIT の総当たり。パラメータ改善用。"""
    b = pd.Timestamp("2019-01-01", tz=engine.TZ_JST)
    rows = []
    for e in entries:
        for x in exits:
            if x <= e:
                continue
            try:
                m = equal_weight(run(e, x))
            except KeyError:
                continue
            if len(m) < 500:
                continue
            tr, te = m[m.index < b], m[m.index >= b]
            rows.append({"entry": e, "exit": x, **stats(m),
                         "t_train": stats(tr)["t"], "t_test": stats(te)["t"]})
    return pd.DataFrame(rows).sort_values("t", ascending=False)


if __name__ == "__main__":
    w = run()
    m = equal_weight(w)
    s = stats(m)
    y = yen(w)
    print(f"=== {NAME}  {ENTRY} -> {EXIT}  {'/'.join(PAIRS)} ===")
    print(f"トレード日 {s['n']}  平均 {s['mean_pip']:+.2f}pip  t {s['t']:+.2f}  "
          f"勝率 {s['win']:.1f}%  Sharpe {s['sharpe']:.2f}")
    print(f"円建て(各1万通貨): 日次平均 {y[y.index.isin(m.index)].mean():+,.0f}円  "
          f"合計 {y[y.index.isin(m.index)].sum():+,.0f}円")
    mm = y[y.index.isin(m.index)]
    mo = mm.groupby(mm.index.to_period("M")).sum()
    print(f"月次: 平均 {mo.mean():+,.0f}円  中央 {mo.median():+,.0f}円  "
          f"プラス月 {(mo > 0).sum()}/{len(mo)}  最悪 {mo.min():+,.0f}円")
    eq = mm.cumsum()
    print(f"最大DD {float((eq - eq.cummax()).min()):+,.0f}円")
