"""analyze_sizing.py — lss取引CSVで「均等100株(1銘柄1回) vs 重複あり(100×戦略一致数)」を比較。

背景: lssは同一銘柄が複数戦略(DON/MOM/MACDTF…)で同時シグナル化すると、既定では
100株×一致数(3戦略一致=300株)で発注する。ユーザー観察「重複より均等100株の方が良い」を、
実CSV(HTMLと同一トレード)で検証する。

  重複あり(weighted) : 全トレードそのまま集計 = 一致数の多い銘柄に3倍のウェイト。
  均等 (flat)        : 同一(銘柄,決済日)を1件にデデュープ = 1銘柄100株。

lssは同一銘柄なら全戦略でトリガー/損切/目標が同一(=同じ1トレード)なので、重複は「同じ損益を
一致数ぶん重複計上」に等しい。よって PF・1件あたり がほぼ同じなら「一致数サイズUPにエッジ無し」、
=均等の方が分散が効き資金効率も良い、と判断できる。

使い方(あなたの機械):
  set LSS_TRADES_CSV=lss_trades.csv & .\daily   # CSVを最新化してから
  python analyze_sizing.py lss_trades.csv --bt-min 30
  python analyze_sizing.py lss_trades.csv --bt-min 40
"""
from __future__ import annotations
import argparse
import csv

ap = argparse.ArgumentParser(description="lss 均等100株 vs 重複あり(100×一致数) の比較")
ap.add_argument("csv", help="LSS_TRADES_CSV で出力した取引CSV")
ap.add_argument("--bt-min", type=float, default=30.0, help="BTスコア下限(実投資集団)")
args = ap.parse_args()

rows = []
for r in csv.DictReader(open(args.csv, encoding="utf-8-sig")):
    try:
        bt = float(r.get("bt") or 0)
    except Exception:
        bt = 0.0
    if bt < args.bt_min:
        continue
    if r.get("reason") in ("発注中", "保有中"):
        continue
    try:
        r["_pnl"] = float(r.get("pnl") or 0)
        r["_ent"] = float(r.get("entry_p") or 0)
    except Exception:
        continue
    rows.append(r)


def _stat(pnls, caps):
    n = len(pnls)
    if n == 0:
        return "件数=0"
    tot = sum(pnls)
    wins = [p for p in pnls if p > 0]
    gl = -sum(p for p in pnls if p <= 0)
    pf = sum(wins) / gl if gl > 0 else float("inf")
    cap = sum(caps)   # 延べ投下資金(約定値×100株の合計)。資金効率の目安。
    roc = tot / cap * 100 if cap > 0 else 0.0
    _pf = "∞" if pf == float("inf") else f"{pf:.2f}"
    return (f"件数={n:>5}  合計={tot:>+12,.0f}  1件={tot/n:>+8,.0f}  "
            f"勝率={len(wins)/n*100:>4.0f}%  PF={_pf:>5}  "
            f"延べ資金={cap:>14,.0f}  資金比={roc:>+5.2f}%")


# 重複あり(既定=100×一致数): 全トレードそのまま
w_pnl = [r["_pnl"] for r in rows]
w_cap = [r["_ent"] * 100 for r in rows]   # 1トレード=100株
# 均等(1銘柄100株): 同一(銘柄,決済日)を1件に
seen: dict = {}
for r in rows:
    k = (r.get("symbol"), r.get("exit_date"))
    if k not in seen:
        seen[k] = (r["_pnl"], r["_ent"] * 100)
f_pnl = [v[0] for v in seen.values()]
f_cap = [v[1] for v in seen.values()]

print(f"\nBT{args.bt_min:.0f}以上 lss取引 サイジング比較  ({args.csv})")
print("=" * 100)
print(f"  重複あり(100×戦略一致): {_stat(w_pnl, w_cap)}")
print(f"  均等   (1銘柄100株)   : {_stat(f_pnl, f_cap)}")
print("=" * 100)
print("読み方:")
print("  ・PF/1件あたり がほぼ同じ → 一致数でサイズUPしてもエッジは増えない(HANDOVERの却下と一致)。")
print("    その場合 均等 の方が『同じエッジをより多くの銘柄に分散＝低リスク・資金効率良し(資金比↑)』で有利。")
print("  ・重複ありの PF/1件 が明確に上 → 一致数の多い銘柄が実際に勝ちやすい=サイズUPに価値あり。")
print("  ・延べ資金=約定値×100株の合計。均等の方が同じ資金でより多くの銘柄を持てる(分散=lssのエッジ)。")
