"""S001 ゴトー日 仲値ロング (凍結版)

------------------------------------------------------------------
ステータス : 保管 (実装候補 / 実スプレッド未測定)
確定日     : 2026-08-26
検証期間   : 2012-11 〜 2022-03 (113ヶ月 / 570トレード日)
------------------------------------------------------------------

■ ロジック
  毎月 5, 10, 15, 20, 25 日と月末 (ゴトー日) に、
  4 つの円クロスを 03:00 JST に成行で買い、09:45 JST に成行で決済する。

■ なぜ動くか (一文)
  ゴトー日は輸入企業の決済が集中し、銀行が仲値 (09:55 JST) までに
  実需の円売り/ドル買いを持ち込むため、その手前で円が売られやすい。

■ 主要成績 (スプレッド 0.2pip 前提, 4ペア各 1万通貨)
  シャープ 1.56 / 年率 +2.30% / 年率ボラ 1.48% / MaxDD -2.00%
  t = 4.79 / 勝率 59.6% / 月平均 +9,600円 / 市場エクスポージャー 4.7%
  対照群 (EURUSD, GBPUSD) はほぼゼロ → 円需要が要因である裏付け

■ 既知の弱点
  - 年 61 回しかない。単月の成績はほぼ運。
  - 2014 年は通年マイナス、2019 年はほぼゼロ。
  - 1 回の平均利益が 1 ペア約 4.8pip。実スプレッドが 2pip なら期待値 4 割減。
  - 検証期間が円安トレンドと重なっている可能性を排除できていない。

■ 触ってはいけない点 (過去の検証で棄却済み)
  - 曜日フィルター (金曜が突出して見えるが 2021-2022 で勝率 41.7% に崩壊)
  - 日付の間引き (25日が最強・5日が最弱に見えるが n=96 で有意差なし)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402

NAME = "S001_gotobi"
PAIRS = engine.JPY_PAIRS
ENTRY_JST = "03:00"
EXIT_JST = "09:45"
LOT = 10000


def is_gotobi(ts: pd.Timestamp) -> bool:
    return ts.day in (5, 10, 15, 20, 25) or ts.is_month_end


def run(pairs: list[str] | None = None) -> pd.DataFrame:
    """1 行 = 1 ペア 1 トレード。index は決済日時。"""
    rows = []
    for pair in pairs or PAIRS:
        df = engine.load(pair)
        for day, g in df.groupby(df.index.normalize()):
            if not is_gotobi(day):
                continue
            s = g.between_time(ENTRY_JST, EXIT_JST)
            if len(s) == 0:
                continue
            entry, exit_ = s["open"].iloc[0], s["close"].iloc[-1]
            rows.append({
                "exit_at": s.index[-1], "date": day, "pair": pair,
                "entry": entry, "exit": exit_,
                "pip": engine.net_pip(pair, entry, exit_, side=+1),
            })
    t = pd.DataFrame(rows)
    return t.set_index("exit_at").sort_index() if len(t) else t


def daily_yen(trades: pd.DataFrame, lot: int = LOT) -> pd.Series:
    """同日の全ペアを合算した円損益。"""
    y = trades.assign(
        yen=[engine.pip_value_jpy(p, lot) * v for p, v in zip(trades["pair"], trades["pip"])]
    )
    return y.groupby("date")["yen"].sum()


if __name__ == "__main__":
    tr = run()
    st = engine.summarize(tr, periods_per_year=61.3)
    yen = daily_yen(tr)
    print(f"=== {NAME} ===")
    print(f"トレード {st['n']}  勝率 {st['win_rate']:.1f}%  PF {st['pf']:.2f}  t {st['t']:.2f}")
    print(f"平均 {st['mean_pip']:+.2f}pip  合計 {st['total_pip']:+.0f}pip")
    print(f"日次(4ペア合計 1万通貨): 平均 {yen.mean():+,.0f}円  合計 {yen.sum():+,.0f}円")
    m = yen.groupby(yen.index.to_period("M")).sum()
    print(f"月次: 平均 {m.mean():+,.0f}円  プラス月 {(m>0).sum()}/{len(m)}")
