r"""audit_delay_diff.py — delay1 の『現実−楽観』(窓埋めペナルティ)がどこで発生しているかを特定する。

なぜ必要か (CLAUDE.md 18.27)
----------------------------
同じトレード集合で base(delay0) は健全(+832円/件)なのに、delay1 だけ
`--holdout-days` を伴う TRAIN窓で -5,954円/件 と崩壊する。

  ・決済理由の内訳は TRAIN/TEST でほぼ同じ(close 25→29% vs 27→30%)
    → **件数ではなく1件あたりの損失額**が違う
  ・base は `real = line` で `opens[]` を一切読まない
    delay1 だけ `real = max(line, opens[stop_from])` で5分足の始値を読む
    → 5分足の値が何らかの理由で日足由来の損切りラインより大きくずれていると、
      max() が常にそちらを拾って**片側にだけ**巨大な損失が出る

このツールは `--audit delay1` が出す全トレードCSVを読み、
`現実−楽観` の分布・集中度・外れ値を TRAIN と TEST で並べる。

使い方
------
  # ① TRAIN窓(ホールドアウトあり)と TEST窓(なし)で監査CSVを作る
  #    出力ファイル名は基準日で変わるので上書きされない
  python compare_lss_rules.py --days 365 --holdout-days 60 --bt-min 30 \
         --min-price 1000 --max-price 6000 --workers 8 --sm 0.1 --tm 1.0 --audit delay1
  python compare_lss_rules.py --days 180 --bt-min 30 \
         --min-price 1000 --max-price 6000 --workers 8 --sm 0.1 --tm 1.0 --audit delay1

  # ② 比較
  python audit_delay_diff.py

読み方
------
  ・『現実−楽観』の合計が -4,000万円級で、上位20件が過半を占める
      → **データ異常**(5分足と日足の価格基準ずれ / 分割未調整 等)。銘柄名を見ること
  ・広く薄く発生していて MAE も数%程度
      → 相場現象。delay1 はその期間で本当に弱い
"""
from __future__ import annotations

import argparse
import glob as _glob
import sys

import pandas as pd

ap = argparse.ArgumentParser(description="delay1 の窓埋めペナルティの発生源を特定する")
ap.add_argument("--pattern", default="lss_audit_*.csv", help="監査CSVのグロブ")
ap.add_argument("--top", type=int, default=20, help="悪い順に何件表示するか")
args = ap.parse_args()

files = sorted(_glob.glob(args.pattern))
if not files:
    sys.exit(f"[error] {args.pattern} に一致するファイルがありません。\n"
             f"        先に compare_lss_rules.py --audit delay1 を実行してください。")

for f in files:
    d = pd.read_csv(f, encoding="utf-8-sig")
    if "現実−楽観" not in d.columns:
        print(f"[skip] {f}: 『現実−楽観』列がありません")
        continue
    pen = d["現実−楽観"]
    stops = d[d["reason"].isin(["損切り", "ハード損切り"])]
    print("=" * 92)
    print(f"■ {f}   {len(d):,}トレード")
    print("=" * 92)
    print(f"  pnl_現実 合計 {d['pnl_現実'].sum():+,.0f}円 "
          f"({d['pnl_現実'].mean():+,.0f}円/件)")
    print(f"  pnl_楽観 合計 {d['pnl_report_楽観'].sum():+,.0f}円 "
          f"({d['pnl_report_楽観'].mean():+,.0f}円/件)")
    print(f"  窓埋めペナルティ(現実−楽観) 合計 {pen.sum():+,.0f}円 / "
          f"全体 {pen.mean():+,.0f}円per件 / 損切り {len(stops):,}件あたり "
          f"{(pen.sum() / len(stops) if len(stops) else 0):+,.0f}円")

    nz = pen[pen != 0]
    print(f"\n  ペナルティが乗ったトレード: {len(nz):,}件 ({len(nz) / len(d) * 100:.1f}%)")
    if len(nz):
        q = nz.quantile([0.5, 0.9, 0.99]).round(0)
        print(f"    中央値 {q.iloc[0]:+,.0f} / 90%点 {q.iloc[1]:+,.0f} / "
              f"99%点 {q.iloc[2]:+,.0f} / 最悪 {nz.min():+,.0f}")
        # 集中度: 上位N件が全体の何%を占めるか。データ異常なら極端に偏る。
        srt = nz.sort_values()
        tot = srt.sum()
        for k in (5, 20, 100):
            if len(srt) >= k and tot != 0:
                print(f"    最悪{k:>3}件で全ペナルティの {srt.head(k).sum() / tot * 100:>5.1f}%")

    print(f"\n  ── 現実で悪い順 上位{args.top}件 ──")
    cols = [c for c in ("date", "sym", "name", "strat", "reason", "entry", "stop",
                        "MAE最大逆行%", "pnl_report_楽観", "pnl_現実", "現実−楽観")
            if c in d.columns]
    top = d.sort_values("pnl_現実").head(args.top)[cols]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(top.to_string(index=False))

    # MAE が非現実的な水準なら価格基準のずれを疑う
    if "MAE最大逆行%" in d.columns:
        m = d["MAE最大逆行%"]
        print(f"\n  MAE(最大逆行%): 中央値 {m.median():.1f}% / 90%点 {m.quantile(.9):.1f}% / "
              f"最大 {m.max():.1f}%   ※ 10%超が多数なら5分足と日足の価格基準ずれを疑う")
        big = d[m > 20]
        if len(big):
            print(f"    MAE>20% が {len(big):,}件。銘柄上位: "
                  + ", ".join(f"{s}({n})" for s, n in
                              big["sym"].value_counts().head(8).items()))
    print()
