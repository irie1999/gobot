"""
compute_wf_scores.py
Walk-forward スキャン結果CSVからWFスコアを計算してwf_scores.jsonに保存。

使い方:
  python compute_wf_scores.py
  → wf_scores.json を生成

WFスコアの意味 (0〜100点):
  out-of-sampleデータで評価した「このシグナルが将来勝つ確率」の指標。
  同じデータでスコア計算したin-sampleスコアと異なり、未来の予測に使える。

  70以上  → 高信頼 ★★★  エントリー推奨
  55〜69  → 中程度 ★★   状況次第
  40〜54  → 低信頼 ★    慎重に
  40未満  → 非推奨 △    見送り推奨

スコア計算式:
  勝率スコア    : (WR% - 45) / 40 × 50点  (45%→0点 / 85%以上→50点)
  PFスコア      : (PF - 1.0) / 4.0 × 30点 (PF1→0点 / PF5以上→30点)
  Fold安定性    : (fold通過数 / 3) × 20点  (3fold全通過→20点)
  合計          : 最大100点
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


WF_DIR = Path("walkforward_results")


def calc_wf_score(wr_pct: float, pf: float, folds: int) -> int:
    """WFスコア (0-100) を計算。"""
    wr_pts   = max(0.0, min(50.0, (wr_pct - 45.0) / 40.0 * 50.0))
    pf_cap   = min(pf if not math.isinf(pf) else 10.0, 10.0)
    pf_pts   = max(0.0, min(30.0, (pf_cap - 1.0) / 4.0 * 30.0))
    fold_pts = (folds / 3.0) * 20.0
    return round(wr_pts + pf_pts + fold_pts)


def wf_rank(score: int) -> str:
    if score >= 70:
        return "★★★"
    elif score >= 55:
        return "★★"
    elif score >= 40:
        return "★"
    else:
        return "△"


def build_wf_scores() -> dict:
    """walkforward_results配下の最新CSVを読み込んでスコア辞書を返す。"""
    if not WF_DIR.exists():
        return {}

    # 戦略ごとに最新日付のCSVを選択
    latest: dict[str, tuple[str, Path]] = {}
    for f in sorted(WF_DIR.glob("walkforward_*_????-??-??.csv")):
        parts = f.stem.split("_")
        if len(parts) < 3:
            continue
        strategy = parts[1]
        date_str = "_".join(parts[2:])
        if strategy not in latest or date_str > latest[strategy][0]:
            latest[strategy] = (date_str, f)

    scores: dict[str, dict] = {}
    for strategy, (date_str, f) in latest.items():
        try:
            with open(f, newline="", encoding="utf-8") as fp:
                reader = csv.DictReader(fp)
                for row in reader:
                    try:
                        symbol = row["symbol"]
                        wr     = float(row["avg_test_wr"])
                        pf     = float(row["avg_test_pf"])
                        folds  = int(row["folds_passed"])
                        score  = calc_wf_score(wr, pf, folds)
                        key    = f"{symbol}_{strategy}"
                        scores[key] = {
                            "score":    score,
                            "rank":     wf_rank(score),
                            "wr":       round(wr, 1),
                            "pf":       round(pf, 2),
                            "folds":    folds,
                            "csv_date": date_str,
                        }
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue

    return scores


if __name__ == "__main__":
    scores = build_wf_scores()
    if not scores:
        print("⚠️  walkforward_results/ にCSVがありません。先に scan_walkforward.py を実行してください。")
    else:
        out = Path("wf_scores.json")
        with open(out, "w", encoding="utf-8") as fp:
            json.dump(scores, fp, ensure_ascii=False, indent=2)
        print(f"✅  WFスコアを {len(scores)} 件保存: {out}")

        # 上位15件をプレビュー
        top = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)[:15]
        print(f"\n上位15件:")
        print(f"{'銘柄_戦略':35s}  スコア  ランク   WR%    PF   Folds")
        print("-" * 70)
        for key, v in top:
            print(f"{key:35s}  {v['score']:5d}  {v['rank']:4s}  {v['wr']:5.1f}%  {v['pf']:5.2f}    {v['folds']}")

        # スコア分布
        n70  = sum(1 for v in scores.values() if v["score"] >= 70)
        n55  = sum(1 for v in scores.values() if 55 <= v["score"] < 70)
        n40  = sum(1 for v in scores.values() if 40 <= v["score"] < 55)
        nlow = sum(1 for v in scores.values() if v["score"] < 40)
        print(f"\nスコア分布 (全{len(scores)}件):")
        print(f"  70以上 ★★★ エントリー推奨: {n70}件")
        print(f"  55〜69 ★★  状況次第      : {n55}件")
        print(f"  40〜54 ★   慎重に        : {n40}件")
        print(f"  40未満 △   見送り推奨    : {nlow}件")
        print(f"\n次のステップ: python run_signals_nolimit.py")
