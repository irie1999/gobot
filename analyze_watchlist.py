"""
監視銘柄分析スクリプト
3戦略の推奨銘柄リストを比較し、監視すべき銘柄を選定する

実行: python analyze_watchlist.py
"""

import importlib.util
from pathlib import Path
from collections import defaultdict

# ── ファイル読み込み ──────────────────────────────────────────
def _load_symbols(path: str) -> list[tuple]:
    p = Path(path)
    if not p.exists():
        return []
    spec = importlib.util.spec_from_file_location("_mod", p)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "SYMBOLS", [])

macd_syms = _load_symbols("symbols_watch_macd_v2.py")
a7_syms   = _load_symbols("symbols_watch_a7_v2.py")
rsi2_syms = _load_symbols("symbols_watch_rsi2_v2.py")

# ── 各戦略のセットを作成 ──────────────────────────────────────
macd_set = {s for s, n in macd_syms}
a7_set   = {s for s, n in a7_syms}
rsi2_set = {s for s, n in rsi2_syms}

# 全銘柄をまとめる (symbol -> name, 何戦略で選ばれたか)
all_syms: dict[str, dict] = {}
for syms, label in [(macd_syms, "MACD"), (a7_syms, "A7"), (rsi2_syms, "RSI2")]:
    for i, (sym, name) in enumerate(syms, 1):
        if sym not in all_syms:
            all_syms[sym] = {"name": name, "strategies": [], "ranks": {}}
        all_syms[sym]["strategies"].append(label)
        all_syms[sym]["ranks"][label] = i

# ── 戦略横断スコア計算 ──────────────────────────────────────
# 3戦略で選ばれた銘柄は最優先、2戦略は次点、1戦略は上位ランクのもの
for sym, d in all_syms.items():
    n_strat = len(d["strategies"])
    # 順位スコア: 上位ほど高いスコア (1位=20点, 2位=19点...)
    rank_score = sum(21 - r for r in d["ranks"].values())
    d["n_strat"]    = n_strat
    d["rank_score"] = rank_score
    d["total_score"] = n_strat * 100 + rank_score

sorted_syms = sorted(all_syms.items(), key=lambda x: -x[1]["total_score"])

# ── 出力 ────────────────────────────────────────────────────────
print()
print("=" * 70)
print("  監視銘柄 分析レポート")
print("=" * 70)

# 3戦略一致
three = [(s, d) for s, d in sorted_syms if d["n_strat"] == 3]
# 2戦略一致
two   = [(s, d) for s, d in sorted_syms if d["n_strat"] == 2]
# 1戦略（上位10位以内）
one   = [(s, d) for s, d in sorted_syms
         if d["n_strat"] == 1 and min(d["ranks"].values()) <= 10]

print(f"\n【★★★ 3戦略全一致 ({len(three)}銘柄) — 最優先監視】")
print(f"  {'銘柄':<20} {'コード':<10} {'戦略':<20} {'ランク'}")
print("  " + "-" * 62)
for sym, d in three:
    ranks_str = "  ".join(f"{k}:{v}位" for k, v in d["ranks"].items())
    print(f"  {d['name']:<20} {sym:<10} {'/'.join(d['strategies']):<20} {ranks_str}")

print(f"\n【★★ 2戦略一致 ({len(two)}銘柄) — 高優先度監視】")
print(f"  {'銘柄':<20} {'コード':<10} {'戦略':<20} {'ランク'}")
print("  " + "-" * 62)
for sym, d in two[:15]:
    ranks_str = "  ".join(f"{k}:{v}位" for k, v in d["ranks"].items())
    print(f"  {d['name']:<20} {sym:<10} {'/'.join(d['strategies']):<20} {ranks_str}")

print(f"\n【★ 1戦略 上位10位以内 ({len(one)}銘柄) — 参考監視】")
print(f"  {'銘柄':<20} {'コード':<10} {'戦略':<20} {'ランク'}")
print("  " + "-" * 62)
for sym, d in one[:10]:
    ranks_str = "  ".join(f"{k}:{v}位" for k, v in d["ranks"].items())
    print(f"  {d['name']:<20} {sym:<10} {'/'.join(d['strategies']):<20} {ranks_str}")

# ── 推奨監視銘柄まとめ ──────────────────────────────────────
print()
print("=" * 70)
print("  推奨 監視銘柄リスト")
print("=" * 70)
watch = three + two[:10] + one[:5]
print(f"\n合計 {len(watch)} 銘柄\n")
for sym, d in watch:
    star = "★★★" if d["n_strat"] == 3 else ("★★" if d["n_strat"] == 2 else "★")
    print(f"  {star}  {d['name']}({sym})  [{'/'.join(d['strategies'])}]")

# ── symbols_watch_combined.py を出力 ──────────────────────────
print()
out = ["\"\"\"",
       "監視銘柄 (3戦略横断分析による選定)",
       "analyze_watchlist.py により自動生成",
       "\"\"\"",
       "SYMBOLS = ["]
for sym, d in watch:
    star = "★★★" if d["n_strat"] == 3 else ("★★" if d["n_strat"] == 2 else "★")
    out.append(f'    ("{sym}", "{d["name"]}"),  # {star} [{"/".join(d["strategies"])}]')
out.append("]")
Path("symbols_watch_combined.py").write_text("\n".join(out) + "\n", encoding="utf-8")
print("  → symbols_watch_combined.py を出力しました")
print()
