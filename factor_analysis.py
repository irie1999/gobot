"""
factor_analysis.py ― どの要素が「成績」に直結するかを機械的にランキングする
================================================================================
nikkei_analysis が出力する trades_factors.csv を読み、各要素(BTスコア/WFスコア/
戦略/設定/損切幅/目標幅/保有日数/約定遅延/重複保有 など)ごとに勝率・平均損益・
PF・総損益を集計し、「成績を最も分離する要素」を上から並べる。

※ 注意: この集計は in-sample(バックテスト全期間)を含むため、"どの要素が効くか"
   の当たりを付ける探索用。実運用の判断は OOS(ロールフォワード/ホールドアウト
   損益タブ)で裏取りすること。

使い方:
  python factor_analysis.py                      # trades_factors.csv を分析
  python factor_analysis.py --csv other.csv
  python factor_analysis.py --min-count 20       # 20件未満のグループは無視
"""
from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _load(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{path} が見つかりません。先に nikkei_analysis(レポート)を"
                         f"生成すると trades_factors.csv が出ます。")
    with open(p, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {}
    pnls = [_f(r["pnl"]) for r in rows]
    wins = sum(1 for x in pnls if x > 0)
    gp = sum(x for x in pnls if x > 0)
    gl = abs(sum(x for x in pnls if x <= 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    tot = sum(pnls)
    return dict(n=n, wr=wins / n * 100, pf=pf, avg=tot / n, total=tot)


def _bucket_numeric(rows, key, edges, labels):
    """数値列を edges で区切ってラベル付きバケットに割り振る。"""
    out = defaultdict(list)
    for r in rows:
        v = _f(r.get(key))
        placed = False
        for i, e in enumerate(edges):
            if v < e:
                out[labels[i]].append(r)
                placed = True
                break
        if not placed:
            out[labels[-1]].append(r)
    return out


def _print_factor(title, groups, min_count, base_avg):
    """1要素のグループ別成績を表示し、判別力(平均損益の分散)を返す。"""
    valid = {k: _stats(v) for k, v in groups.items() if len(v) >= min_count}
    if len(valid) < 2:
        return None
    print(f"\n■ {title}")
    print(f"  {'グループ':<22}{'件数':>6}{'勝率':>8}{'PF':>7}"
          f"{'平均損益':>11}{'総損益':>13}{'vs全体':>10}")
    print("  " + "-" * 84)
    # 平均損益の重み付き分散(=判別力の指標)
    total_n = sum(s["n"] for s in valid.values())
    spread = 0.0
    for k in sorted(valid, key=lambda x: -valid[x]["avg"]):
        s = valid[k]
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        diff = s["avg"] - base_avg
        spread += s["n"] / total_n * (s["avg"] - base_avg) ** 2
        print(f"  {str(k):<22}{s['n']:>6}{s['wr']:>7.1f}%{pf_s:>7}"
              f"{s['avg']:>+10,.0f}円{s['total']:>+12,.0f}円{diff:>+9,.0f}")
    rms = math.sqrt(spread)   # 平均損益のばらつき(円) = 判別力
    return rms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="trades_factors.csv")
    ap.add_argument("--min-count", type=int, default=15,
                    help="この件数未満のグループは無視 (既定15)")
    args = ap.parse_args()

    rows = _load(args.csv)
    base = _stats(rows)
    print("=" * 88)
    print(f"成績に効く要素の分析  ({args.csv})  ※ in-sample を含む探索用")
    print(f"  全取引 {base['n']}件 / 勝率 {base['wr']:.1f}% / "
          f"PF {base['pf']:.2f} / 平均損益 {base['avg']:+,.0f}円 / "
          f"総損益 {base['total']:+,.0f}円")
    print("=" * 88)

    factors: list[tuple[str, dict]] = []

    # ① BTスコア帯
    factors.append(("BTスコア帯", _bucket_numeric(
        rows, "bt_score", [30, 50, 60, 70, 80],
        ["0-29", "30-49", "50-59", "60-69", "70-79", "80+"])))
    # ② WFスコア帯
    factors.append(("WFスコア帯", _bucket_numeric(
        rows, "wf_score", [30, 50, 70, 90],
        ["<30", "30-49", "50-69", "70-89", "90+"])))
    # ③ 損切り幅
    factors.append(("損切り幅(絶対%)", _bucket_numeric(
        rows, "stop_pct", [-15, -10, -7, -5, -3, 0],
        ["<-15", "-15~-10", "-10~-7", "-7~-5", "-5~-3", "-3~0", ">0"])))
    # ④ 目標幅
    factors.append(("目標幅(%)", _bucket_numeric(
        rows, "target_pct", [5, 8, 12, 18],
        ["<5", "5-8", "8-12", "12-18", "18+"])))
    # ⑤ 保有日数
    factors.append(("保有日数", _bucket_numeric(
        rows, "hold_days", [1, 3, 5, 8, 12],
        ["0", "1-2", "3-4", "5-7", "8-11", "12+"])))
    # ⑥ 約定遅延
    factors.append(("約定遅延(日)", _bucket_numeric(
        rows, "days_to_fill", [1, 2],
        ["0(当日)", "1", "2+"])))

    # カテゴリ要素
    def _by(key):
        g = defaultdict(list)
        for r in rows:
            g[r.get(key, "") or "?"].append(r)
        return g

    factors.append(("戦略", _by("strategy")))
    factors.append(("モード(con/agg)", _by("mode")))
    factors.append(("ホールドアウト設定", _by("holdout")))
    factors.append(("BTタイプ", _by("bt_type")))
    factors.append(("重複保有(再エントリー)", _by("overlap")))
    factors.append(("決済理由", _by("reason")))

    scored = []
    for title, groups in factors:
        rms = _print_factor(title, groups, args.min_count, base["avg"])
        if rms is not None:
            scored.append((rms, title))

    # 判別力ランキング
    scored.sort(reverse=True)
    print("\n" + "=" * 88)
    print("★ 成績を最も分離する要素ランキング (平均損益のばらつき=判別力 / 大きいほど効く)")
    print("=" * 88)
    for i, (rms, title) in enumerate(scored, 1):
        bar = "█" * min(int(rms / 2000), 40)
        print(f"  {i:>2}. {title:<24}判別力 {rms:>8,.0f}円  {bar}")
    print("\n※ 判別力 = 各グループの平均損益が全体平均からどれだけ離れているかの"
          "加重RMS(円)。\n  上位ほど『そのグループ分けで成績が大きく変わる』=効く要素。"
          "\n  ただし in-sample を含むので、実運用判断はロールフォワードOOSで裏取りを。")


if __name__ == "__main__":
    main()
