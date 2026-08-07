"""lss_exit_breakdown.py — lss が『なぜ』負けているのかを決済理由別に分解する。

なぜ必要か
-----------
lss の設定は 損切ATR=0.1 / 利確ATR=1.0 = 名目 **1:10** の非対称。
実測の勝率は 38.8% なので、素朴に計算すると

    期待値 = 0.388 x 1.0ATR - 0.612 x 0.1ATR = +0.327 ATR/件

と大幅プラスのはず。ところが実際は 11ヶ月で -114,595円(予算シミュ)、
全トレードでは -413,506円 のマイナス(CLAUDE.md 18.13)。

**つまり実際の勝ち幅・負け幅が 1.0 / 0.1 ATR になっていない。**
どこで崩れているのかが分かれば、決済パラメータの再最適化で直せるのか、
それとも構造的に無理なのかが判断できる。

出すもの
--------
1. 決済理由別 (利確 / 損切り / 引け / タイムカット …) の件数・割合・1件あたり・合計
2. 実効ペイオフ比 = 平均勝ち幅 / 平均負け幅。名目の 10 とどれだけ違うか
3. 損益分岐の勝率 = 1 / (1 + ペイオフ比)。実際の勝率と比べる
4. 期間を2分割して、上の内訳が前半/後半で変わったか
5. 月別の理由構成 (相場環境で決済の内訳が動くか)

使い方
------
  python lss_exit_breakdown.py                     # lss_trades.csv を自動検出
  python lss_exit_breakdown.py --csv lss_trades.csv
  python lss_exit_breakdown.py --split 0.5         # 前半/後半の比較
  python lss_exit_breakdown.py --by-month
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(description="lss の負け方を決済理由別に分解する")
ap.add_argument("--csv", type=str, default="lss_trades.csv")
ap.add_argument("--split", type=str, default="0.5",
                help="前半/後半の境目。日付か比率(既定0.5)")
ap.add_argument("--exclude-strat", type=str, default="転換",
                help="除外する戦略。既定『転換』= lss ではない(CLAUDE.md 18.5.3)")
ap.add_argument("--by-month", action="store_true", help="月別の理由構成も出す")
args = ap.parse_args()

p = Path(args.csv)
if not p.exists():
    sys.exit(f"[error] {p} がありません。.\\daily か .\\dailyfast を1回流してください")

df = pd.read_csv(p, encoding="utf-8-sig")
_c_date = "entry_date" if "entry_date" in df.columns else "date"
df["date"] = pd.to_datetime(df[_c_date], errors="coerce")
df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
df["pnl"] = pd.to_numeric(df.get("pnl"), errors="coerce").fillna(0.0)
df["reason"] = df.get("reason", "").astype(str)
_strat_col = "strategy" if "strategy" in df.columns else "strat"

# 未決済は損益が確定していないので落とす
df = df[~df["reason"].isin(["発注中", "保有中"])].reset_index(drop=True)
if args.exclude_strat.strip() and _strat_col in df.columns:
    _ex = {s.strip() for s in args.exclude_strat.split(",") if s.strip()}
    _hit = df[_strat_col].astype(str).isin(_ex)
    if _hit.any():
        print(f"[入力] 戦略 {'/'.join(sorted(_ex))} を除外: {int(_hit.sum())}件 "
              f"({df.loc[_hit, 'pnl'].sum():+,.0f}円)")
        df = df[~_hit].reset_index(drop=True)

print(f"[入力] {len(df):,}トレード / {df['date'].min().date()} 〜 {df['date'].max().date()} "
      f"/ 合計 {df['pnl'].sum():+,.0f}円")


def _breakdown(d: pd.DataFrame, title: str) -> None:
    if d.empty:
        return
    n = len(d)
    print(f"\n{'=' * 88}")
    print(f"■ {title}   {n:,}件 / 合計 {d['pnl'].sum():+,.0f}円")
    print(f"{'=' * 88}")
    print(f"  {'決済理由':<14}{'件数':>7}{'割合':>7}{'勝率':>7}"
          f"{'1件あたり':>11}{'平均勝ち':>11}{'平均負け':>11}{'合計':>13}")
    print("  " + "-" * 84)
    for rs, g in sorted(d.groupby("reason"), key=lambda kv: -abs(kv[1]["pnl"].sum())):
        w = g[g["pnl"] > 0]["pnl"]
        l = g[g["pnl"] <= 0]["pnl"]
        print(f"  {rs[:14]:<14}{len(g):>7}{len(g) / n * 100:>6.1f}%"
              f"{(len(w) / len(g) * 100 if len(g) else 0):>6.1f}%"
              f"{g['pnl'].mean():>+11,.0f}"
              f"{(w.mean() if len(w) else 0):>+11,.0f}"
              f"{(l.mean() if len(l) else 0):>+11,.0f}"
              f"{g['pnl'].sum():>+13,.0f}")

    # ── 実効ペイオフ比 ────────────────────────────────────────────
    w_all = d[d["pnl"] > 0]["pnl"]
    l_all = d[d["pnl"] <= 0]["pnl"]
    aw = float(w_all.mean()) if len(w_all) else 0.0
    al = float(-l_all.mean()) if len(l_all) else 0.0
    wr = len(w_all) / n * 100
    payoff = aw / al if al > 0 else float("inf")
    be_wr = 1 / (1 + payoff) * 100 if payoff not in (0, float("inf")) else 0.0
    print("  " + "-" * 84)
    print(f"  平均勝ち {aw:+,.0f}円 / 平均負け {-al:+,.0f}円 "
          f"→ **実効ペイオフ比 {payoff:.2f}** (名目は損切0.1/利確1.0 = 10.0)")
    print(f"  損益分岐の勝率 {be_wr:.1f}%  vs  実際の勝率 {wr:.1f}%  "
          f"→ {'✅ 上回っている' if wr > be_wr else '❌ 足りない(' + f'{wr - be_wr:+.1f}pt' + ')'}")
    if payoff < 3:
        print(f"  ⚠ ペイオフ比が名目(10.0)から大きく崩れている。"
              f"『利確1.0ATRまで走らず、損切り0.1ATRだけが確実に当たる』状態。")


_breakdown(df, "全期間")

# ── 前半 / 後半 ────────────────────────────────────────────────────
try:
    _cut = df["date"].quantile(float(args.split))
except ValueError:
    _cut = pd.Timestamp(args.split)
_breakdown(df[df["date"] < _cut], f"前半 (〜{_cut.date()})")
_breakdown(df[df["date"] >= _cut], f"後半 ({_cut.date()}〜)")

# ── 月別の理由構成 ────────────────────────────────────────────────
if args.by_month:
    print(f"\n{'=' * 88}")
    print("■ 月別の決済理由構成 (割合% / 損益)")
    print(f"{'=' * 88}")
    df["月"] = df["date"].dt.strftime("%Y-%m")
    reasons = sorted(df["reason"].unique())
    print(f"  {'月':<9}{'件数':>6}" + "".join(f"{r[:6]:>9}" for r in reasons) + f"{'損益':>13}")
    print("  " + "-" * (15 + 9 * len(reasons) + 13))
    for mo, g in df.groupby("月"):
        row = "".join(f"{(g['reason'] == r).sum() / len(g) * 100:>8.0f}%" for r in reasons)
        print(f"  {mo:<9}{len(g):>6}{row}{g['pnl'].sum():>+13,.0f}")

print(f"\n{'─' * 88}")
print("読み方: 実効ペイオフ比が名目(10.0)より大幅に低いなら、利確1.0ATRに届く前に")
print("        引け/損切りで終わっている。その場合 tm を下げて(利確を近づけて)")
print("        到達率を上げるか、sm を上げて(損切りを遠ざけて)ヒゲ死を減らすかの")
print("        どちらかが効く可能性がある。**必ず TRAIN/TEST を分けて検証すること**")
print("        (compare_lss_rules.py --sm X --tm Y --holdout-days N)。")
