r"""analyze_limit_up.py — ストップ高/安で**決済できない**日がどれだけあるか数える

なぜ要るか (2026-08-28)
-----------------------
N は **損切りも利確も置かない**(§18.55)。寄りで建てて引けの成行で決済するだけ。
その決済が唯一 成立しないのが **引けがストップ値に張り付いたとき**:

  ・N(ショート) … 引けがストップ高 = 買い気配のまま → **買い戻せない**
  ・鏡像(ロング) … 引けがストップ安 = 売り気配のまま → **売れない**

決済できないと持ち越しになり、§18.46 の事故(8建玉 / **-36,800円**)と同じ形になる。
一般信用デイトレは返済期日を過ぎると自分で閉じられず、翌朝の強制決済を待つしかない
(手数料 2,200円/銘柄 + 夜間ギャップ -2,400円/銘柄)。

⛔ **「寄りがストップ高」は危険ではない**。板寄せで約定しているので建玉は普通にできる。
   危険なのは **建てた後に飛んで、引けまで張り付いたまま**のケース。ここだけを数える。

判定
----
基準値段 = 前日終値。`picks.csv` に前日終値の列は無いが **gap_bp から復元できる**:

    gap_bp = (始値 - 前日終値) / 前日終値 * 10,000 * side
    → 前日終値 = 始値 / (1 + side * gap_bp / 10,000)

復元が正しいかは **元の gap_bp を計算し直して突き合わせる**(自己検算。§18.62 の作法)。

制限値幅は `check_price_limit._TSE_LIMIT_TABLE` を **そのまま import** する
(同じ表を2箇所に書くと片方だけ直して片方が漏れる / §18.48 ⑦)。

⚠ **株式分割の注意**(§18.27)。日足(yfinance)は分割を遡及調整するので、
  分割した銘柄の過去の値は実際の株価より安い。制限値幅の表は**円建て**なので、
  調整済みの値で引くと帯を1つ間違えうる(制限値幅は株価比 14〜30% なので、
  帯を外しても最大10ポイント程度のズレ)。**④の余裕の分布**を見れば、
  そもそも近づいてすらいないのか、帯の精度が効く距離なのかが分かる。

使い方
------
  python analyze_limit_up.py --picks picks.csv
  python analyze_limit_up.py --picks picks.csv --tol-pct 0.5   # 判定を緩める
  python analyze_limit_up.py --picks picks.csv --fee 2200      # 強制決済手数料

⚠ 照会のみ。発注はしない。既存CSVを読むだけ。
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from check_price_limit import _TSE_LIMIT_TABLE, _width   # 表は1箇所だけに置く

assert _TSE_LIMIT_TABLE, "制限値幅テーブルが空です"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--picks", default="picks.csv",
                    help="analyze_gap_edge.py --dump-picks の出力")
    ap.add_argument("--tol-pct", type=float, default=0.25,
                    help="ストップ値と見なす許容(基準値段に対する%%)。"
                         "呼値の丸めと分割調整の誤差を吸収する")
    ap.add_argument("--fee", type=float, default=2200.0,
                    help="強制決済の手数料(円/銘柄)。§18.46 の実測")
    ap.add_argument("--overnight", type=float, default=2400.0,
                    help="持ち越しの夜間ギャップ(円/銘柄)。§18.46 の実測")
    a = ap.parse_args()

    try:
        df = pd.read_csv(a.picks)
    except Exception as e:
        sys.exit(f"[error] {a.picks} が読めません: {e}")

    _need = ("date", "symbol", "side", "entry_p", "gap_bp", "d1_close", "pnl")
    _miss = [c for c in _need if c not in df.columns]
    if _miss:
        sys.exit(f"[error] 列が足りません: {', '.join(_miss)}\n"
                 f"  → python analyze_gap_edge.py ... --dump-picks {a.picks}")
    if "win" not in df.columns:
        df["win"] = "ALL"

    df = df[(df["entry_p"] > 0) & (df["d1_close"] > 0)].copy()
    df["side"] = df["side"].astype(int)

    # ── 前日終値を復元 ──────────────────────────────────────────
    df["prev_close"] = df["entry_p"] / (1.0 + df["side"] * df["gap_bp"] / 10_000.0)

    # ★ 自己検算: 復元した前日終値から gap_bp を作り直して元と比べる
    _re = (df["entry_p"] - df["prev_close"]) / df["prev_close"] * 10_000.0 * df["side"]
    _err = (_re - df["gap_bp"]).abs()
    print(f"{'=' * 74}")
    print(f"■ ストップ高/安で決済できない日 — {a.picks}")
    print(f"{'=' * 74}")
    print(f"  対象 {len(df):,}件 / {df['date'].nunique():,}営業日 / "
          f"許容 {a.tol_pct:.2f}%")
    print(f"  [検算] 前日終値の復元 … gap_bp の再計算誤差 "
          f"平均 {_err.mean():.4f}bp / 最大 {_err.max():.4f}bp"
          + ("  ✅" if _err.max() < 0.01 else "  ⛔ 復元が合っていません"))

    df["width"] = df["prev_close"].map(_width)
    df["limit_up"] = df["prev_close"] + df["width"]
    df["limit_dn"] = df["prev_close"] - df["width"]

    # side=+1 はショート(ストップ高で買い戻せない) / -1 はロング(ストップ安で売れない)
    _bad = df["limit_up"].where(df["side"] > 0, df["limit_dn"])
    _tol = df["prev_close"] * a.tol_pct / 100.0
    df["entry_at_limit"] = (df["entry_p"] - _bad).abs() <= _tol
    df["close_at_limit"] = (df["d1_close"] - _bad).abs() <= _tol
    # 引けがストップ値**を超えている**なら、それは分割汚染か帯の取り違え
    df["beyond"] = ((_bad - df["d1_close"]) * df["side"]) < -_tol

    _sname = {1: "N(ショート)", -1: "鏡像(ロング)"}

    # ── ① 建値がストップ値(= 寄りが張り付き。板寄せで約定済み = 危険ではない) ──
    print(f"\n  ── ① 建値がストップ値だった(= 寄りが張り付き) ──")
    print(f"     ⛔ これは危険ではない。板寄せで約定しているので建玉はできている")
    _e = df[df["entry_at_limit"]]
    print(f"     {len(_e):,}件 / 全体の {len(_e) / max(1, len(df)) * 100:.2f}%")
    for _s, _g in _e.groupby("side"):
        print(f"       {_sname.get(int(_s), _s):<14}{len(_g):>5,}件 "
              f"損益 {_g['pnl'].sum():>+12,.0f}円 / {_g['pnl'].mean():>+8,.0f}円/件")

    # ── ★★ ② 引けがストップ値(= 決済できない) ──────────────────
    print(f"\n  ── ★★ ② 引けがストップ値だった(= **決済できない**) ──")
    _c = df[df["close_at_limit"]]
    _days = df["date"].nunique()
    _mon = max(1.0, _days / 20.0)          # おおよその月数
    print(f"     **{len(_c):,}件 / 全体の {len(_c) / max(1, len(df)) * 100:.3f}%**"
          f"  (1営業日あたり {len(_c) / max(1, _days):.4f}件)")
    if len(_c):
        for _s, _g in _c.groupby("side"):
            print(f"       {_sname.get(int(_s), _s):<14}{len(_g):>5,}件 "
                  f"損益 {_g['pnl'].sum():>+12,.0f}円 / {_g['pnl'].mean():>+8,.0f}円/件")
        for _w, _g in _c.groupby("win"):
            _wd = df[df["win"] == _w]["date"].nunique()
            print(f"       {str(_w):<14}{len(_g):>5,}件 / {_wd:,}営業日 = "
                  f"{len(_g) / max(1, _wd):.4f}件/日")

        print(f"\n     ── 実際に起きた日(損失の大きい順 上位10件) ──")
        print(f"       {'日付':<12}{'銘柄':<9}{'方式':<14}"
              f"{'前日終値':>10}{'建値':>9}{'終値':>9}{'ストップ':>10}{'損益':>11}")
        for _r in _c.sort_values("pnl").head(10).itertuples():
            print(f"       {str(_r.date):<12}{str(_r.symbol):<9}"
                  f"{_sname.get(int(_r.side), ''):<14}"
                  f"{_r.prev_close:>10,.0f}{_r.entry_p:>9,.0f}{_r.d1_close:>9,.0f}"
                  f"{(_r.limit_up if _r.side > 0 else _r.limit_dn):>10,.0f}"
                  f"{_r.pnl:>+11,.0f}")

        _per = a.fee + a.overnight
        print(f"\n  ── ③ 持ち越しコストの概算(§18.46 の実測に当てはめる) ──")
        print(f"     強制決済手数料 {a.fee:,.0f}円 + 夜間ギャップ {a.overnight:,.0f}円"
              f" = {_per:,.0f}円/件")
        print(f"     × {len(_c):,}件 = **{_per * len(_c):,.0f}円** = "
              f"月 **{_per * len(_c) / _mon:,.0f}円**")
    else:
        print(f"     ✅ **1件も無い。** 引けが張り付いて決済できなかった日は"
              f"{_days:,}営業日で一度も発生していない")

    # ── ④ どこまで近づいたか(張り付きの手前) ───────────────────
    print(f"\n  ── ④ 引けからストップ値までの余裕 ──")
    _room = ((_bad - df["d1_close"]) * df["side"]) / df["prev_close"] * 100.0
    for _q in (0.0005, 0.005, 0.01, 0.05, 0.25, 0.50):
        print(f"       {_q * 100:>6.2f}%点  ストップまで {_room.quantile(_q):>7.2f}%")
    for _th in (0.5, 1.0, 2.0, 5.0):
        _n = int((_room <= _th).sum())
        print(f"       余裕 {_th:>4.1f}% 以下 … {_n:>5,}件 "
              f"({_n / max(1, len(df)) * 100:.3f}%)")
    _bg = int(df["beyond"].sum())
    if _bg:
        print(f"\n     ⛔ 引けが **ストップ値を超えている** {_bg:,}件 "
              f"({_bg / max(1, len(df)) * 100:.2f}%)")
        print(f"        制度上ありえないので、**株式分割の遡及調整**(§18.27)か"
              f"帯の取り違え。")
        print(f"        ②の件数はこのぶん過小に出ます(超えた日は"
              f"『一致』にならないため)")

    print(f"\n  {'=' * 68}")
    print(f"  ⚠ 測れるのは **引けの値がストップ値と一致したか**だけ。実際に"
          f"買い戻せたかは")
    print(f"     板の需給(比例配分)次第なので、②は **決済できなかった恐れの"
          f"ある上限**です。")
    print(f"  {'=' * 68}")


if __name__ == "__main__":
    main()
