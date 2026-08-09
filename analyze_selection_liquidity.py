r"""analyze_selection_liquidity.py — 選定した銘柄が、実際に発注されているのかを測る。

問題意識
--------
現行は工程ごとに基準が違う:

  選定 (scan_lss_universe)  … TRAIN期待値で選ぶ。**流動性は一切見ていない**
  発注 (予算400万)          … **流動性降順**で上から埋める。1日十数件で打ち止め

つまり「選ばれたが流動性が低く、毎日 最後尾に回って一度も発注されない」ペアが
存在しうる。候補は約1,900ペアあるのに1日13件しか建てないので、下位はかなり厚いはず。

これが本当なら2つ問題がある:
  ① 選定の労力が無駄。発注されないペアを選ぶために計算している
  ② より本質的: 「全体から最良を選んで後で流動性で切る」より
     **「流動性のある銘柄の中から最良を選ぶ」**ほうが、同じ発注枠に対して
     良い集団になるはず。現状は『良いが薄い銘柄』を選んで枠に入れていない

やること
--------
`lss_trades.csv`(全トレード)に対して、実運用と同じ
「その日の流動性降順で予算400万まで埋める」を再現し、

  ・ペア別の発注率(枠内に入った日数 / シグナルが出た日数)
  ・一度も枠に入らなかったペアが何本あるか
  ・流動性分位ごとの発注率と損益
  ・予算が飽和した日の割合(飽和していない日は薄い銘柄も枠に入る)

を出す。**枠外ペアの損益**も出すので、「切り捨てている利益/避けている損失」も分かる。

⚠ 注意
  lss_trades.csv は決済済みトレードのみ。実運用では不約定の注文も枠を消費する
  (§18.5.4)ので、ここで再現する枠は実際よりやや緩い(=枠内と判定される件数が多め)。
  傾向を見る用途には十分だが、絶対値は sim_oos_budget を正とすること。

使い方
------
  python analyze_selection_liquidity.py
  python analyze_selection_liquidity.py --budget 4000000 --proposal lss_proposal_cumul.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="選定銘柄が実際に発注されているかを測る")
ap.add_argument("--trades", default="lss_trades.csv")
ap.add_argument("--proposal", default="lss_proposal_cumul.py",
                help="選定ファイル。選ばれたが1件もトレードが出なかったペアを数えるのに使う")
ap.add_argument("--budget", type=float, default=4_000_000.0)
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--exclude-strat", default="転換")
ap.add_argument("--bins", type=int, default=5, help="流動性の分位数")
args = ap.parse_args()

p = Path(args.trades)
if not p.exists():
    sys.exit(f"[error] {p} が見つかりません。先に .\\daily を実行してください。")
d = pd.read_csv(p, encoding="utf-8-sig")
if args.exclude_strat.strip() and "strategy" in d.columns:
    _ex = {s.strip() for s in args.exclude_strat.split(",") if s.strip()}
    d = d[~d["strategy"].astype(str).isin(_ex)]
if "reason" in d.columns:
    d = d[d["reason"].astype(str) != "約定せず"]
for c in ("pnl", "entry_p", "liquidity"):
    if c not in d.columns:
        sys.exit(f"[error] {p} に列 {c} がありません")
    d[c] = pd.to_numeric(d[c], errors="coerce")
d["date"] = pd.to_datetime(d["entry_date"], errors="coerce")
d = d[d["date"].notna() & d["pnl"].notna() & (d["entry_p"] > 0)].copy()
d["pair"] = d["symbol"].astype(str) + "|" + d["strategy"].astype(str)

# ⛔ liquidity 列が空だと『流動性順に並べたつもりが、CSVの行順で埋めていた』
#    という事故になる(2026-08-09 に実際に起きた。分位表が空になって気づいた)。
#    黙って 0 埋めせず、日足から計算し直す。それも無理なら中断する。
_have = int((d["liquidity"].fillna(0) > 0).sum())
print(f"[流動性] CSVに値がある行 {_have:,}/{len(d):,} ({_have/len(d)*100:.1f}%)")
if _have < len(d) * 0.5:
    print("[流動性] 不足 → 日足から売買代金(直近120日平均)を計算します(初回は時間がかかります)")
    try:
        from backtest_limit_entry import fetch as _fq
        from lss_order_rank import daily_turnover as _dt
    except Exception as e:
        sys.exit(f"[error] 流動性を計算できません: {e}\n"
                 f"        .\\daily を回し直して lss_trades.csv に liquidity 列を"
                 f"入れてから再実行してください。")
    _cache: dict = {}
    for _i, _sym in enumerate(sorted(d["symbol"].astype(str).unique()), 1):
        try:
            _cache[_sym] = float(_dt(_fq(_sym, 200)))
        except Exception:
            _cache[_sym] = 0.0
        if _i % 200 == 0:
            print(f"  ...{_i}銘柄", flush=True)
    d["liquidity"] = d["symbol"].astype(str).map(_cache).fillna(0.0)
    _have2 = int((d["liquidity"] > 0).sum())
    print(f"[流動性] 補完後 {_have2:,}/{len(d):,} ({_have2/len(d)*100:.1f}%)")
    if _have2 < len(d) * 0.5:
        sys.exit("[error] 半数以上で流動性が取れませんでした。中断します"
                 "(この状態で並べても流動性順になりません)。")
d["liquidity"] = d["liquidity"].fillna(0.0)

# ── 実運用と同じ: その日の流動性降順で予算まで埋める ────────────────
# 流動性0は最後尾(lss_order_rank と同じ規則)
d["_ord0"] = (d["liquidity"] <= 0).astype(int)
d = d.sort_values(["date", "_ord0", "liquidity"], ascending=[True, True, False])
d["rank"] = d.groupby("date").cumcount() + 1
d["cost"] = d["entry_p"] * args.qty
d["cum"] = d.groupby("date")["cost"].cumsum()
d["in_budget"] = d["cum"] <= args.budget
# その日の予算が埋まったか(埋まらない日は薄い銘柄も枠に入る)
_day = d.groupby("date").agg(need=("cost", "sum"), n=("pnl", "size"),
                             used=("cost", lambda s: s.cumsum().where(
                                 s.cumsum() <= args.budget).max()))
_sat = (_day["need"] >= args.budget)

print(f"[入力] {p.name}: {len(d):,}トレード / {d['date'].nunique():,}営業日 "
      f"/ {d['pair'].nunique():,}ペア")
print(f"       予算 {args.budget:,.0f}円 / {args.qty}株固定 / 流動性(売買代金)降順")
print(f"\n■ 予算は毎日埋まっているか")
print(f"  飽和した日 {int(_sat.sum()):,} / {len(_sat):,}日 ({_sat.mean()*100:.1f}%)")
print(f"  枠内に入ったトレード {int(d['in_budget'].sum()):,} / {len(d):,} "
      f"({d['in_budget'].mean()*100:.1f}%)")
print(f"  1日あたり 枠内 {d['in_budget'].sum()/d['date'].nunique():.1f}件 "
      f"/ 候補 {len(d)/d['date'].nunique():.1f}件")
_ni, _no = int(d["in_budget"].sum()), int((~d["in_budget"]).sum())
_pi, _po = d.loc[d["in_budget"], "pnl"].sum(), d.loc[~d["in_budget"], "pnl"].sum()
print(f"  枠内 {_ni:>5,}件 {_pi:>+12,.0f}円 ({_pi/max(_ni,1):+,.0f}円/件)")
print(f"  枠外 {_no:>5,}件 {_po:>+12,.0f}円 ({_po/max(_no,1):+,.0f}円/件)")

# ── ペア別の発注率 ──────────────────────────────────────────────
g = d.groupby("pair").agg(sig=("pnl", "size"), inb=("in_budget", "sum"),
                          liq=("liquidity", "median"),
                          pnl_in=("pnl", lambda s: s[d.loc[s.index, "in_budget"]].sum()),
                          pnl_out=("pnl", lambda s: s[~d.loc[s.index, "in_budget"]].sum()))
g["rate"] = g["inb"] / g["sig"]
never = g[g["inb"] == 0]
print(f"\n■ ペア別の発注率 (枠内日数 / シグナル日数)")
print(f"  トレードが出たペア        {len(g):,}")
print(f"  **一度も枠に入らないペア  {len(never):,} ({len(never)/len(g)*100:.1f}%)**")
print(f"    そのシグナル {int(never['sig'].sum()):,}件 / 損益 {never['pnl_out'].sum():+,.0f}円")
print(f"    (= 選定したが実運用では一度も発注されない = 選定の無駄打ち)")
for lo, hi, lab in ((0.0, 0.001, "0%(常に枠外)"), (0.001, 0.25, "0-25%"),
                    (0.25, 0.5, "25-50%"), (0.5, 0.75, "50-75%"),
                    (0.75, 1.001, "75-100%")):
    sub = g[(g["rate"] >= lo) & (g["rate"] < hi)]
    if len(sub):
        print(f"    発注率 {lab:<14}{len(sub):>5}ペア  シグナル{int(sub['sig'].sum()):>6,}件"
              f"  枠内損益 {sub['pnl_in'].sum():>+12,.0f}円")

# ── 流動性分位 ──────────────────────────────────────────────────
print(f"\n■ 流動性(売買代金 中央値)の分位別")
try:
    g["q"] = pd.qcut(g["liq"], args.bins, labels=False, duplicates="drop")
except Exception:
    g["q"] = 0
_qs = sorted(g["q"].dropna().unique())
if not _qs:
    print("  ⛔ 分位を作れません(流動性が全て同値/欠損)。**この出力の並び順は"
          "流動性順になっていません。** 結果を使わないでください。")
print(f"  {'分位':<8}{'ペア':>7}{'売買代金中央値':>18}{'発注率':>9}"
      f"{'枠内損益':>14}{'枠内/件':>10}{'枠外損益':>14}{'枠外/件':>10}")
for q in _qs:
    s = g[g["q"] == q]
    _i, _o = int(s["inb"].sum()), int(s["sig"].sum() - s["inb"].sum())
    print(f"  {int(q)+1}/{args.bins:<6}{len(s):>7,}{s['liq'].median()/1e8:>15,.0f}億"
          f"{s['inb'].sum()/max(s['sig'].sum(),1)*100:>8.1f}%"
          f"{s['pnl_in'].sum():>+14,.0f}{s['pnl_in'].sum()/max(_i,1):>+10,.0f}"
          f"{s['pnl_out'].sum():>+14,.0f}{s['pnl_out'].sum()/max(_o,1):>+10,.0f}")

# ── 発注順の反実仮想 (同じトレード集合・同じ予算で、並べ方だけ変える) ─────
# 18.24 の作法: **ランダム順の帯を作ってから**判定する。2条件を1回ずつ比べない。
import numpy as _np

_INF = float("inf")
_liq = d["liquidity"].to_numpy(dtype=float)
_cost = d["cost"].to_numpy(dtype=float)


def _sim(key: "_np.ndarray") -> tuple:
    """key の昇順で日ごとに予算まで埋め、(件数, 損益, 平均建値) を返す"""
    t = d.assign(_k=key).sort_values(["date", "_k"], kind="mergesort")
    inb = t.groupby("date")["cost"].cumsum() <= args.budget
    return (int(inb.sum()), float(t.loc[inb, "pnl"].sum()),
            float(t.loc[inb, "entry_p"].mean()))


_rules = {
    "流動性 降順(現行)": _np.where(_liq > 0, -_liq, _INF),
    "流動性 昇順(薄い順)": _np.where(_liq > 0, _liq, _INF),
    "建値 昇順(安い順)": d["entry_p"].to_numpy(dtype=float),
}
_base = _sim(_rules["流動性 降順(現行)"])

_rand = []
for _sd in (42, 1, 7, 99, 123, 2024, 31337, 5, 77, 808, 2718, 1618,
            3, 11, 17, 23, 29, 37, 41, 53, 59, 61, 67, 71):
    _rand.append(_sim(_np.random.default_rng(_sd).random(len(d))))
_rp = _np.array([r[1] for r in _rand], dtype=float)
_rmu, _rsd = float(_rp.mean()), float(_rp.std(ddof=1))

print(f"\n■ 発注順の反実仮想 (同じトレード集合・同じ予算・並べ方だけ変える)")
print(f"  {'ルール':<22}{'件数':>7}{'損益':>14}{'円/件':>10}{'z':>8}{'帯内順位':>12}")
_rn = float(_np.mean([r[0] for r in _rand]))
print(f"  {'ランダム×' + str(len(_rand)) + '(帯)':<22}{_rn:>7,.0f}{_rmu:>+14,.0f}"
      f"{_rmu / max(_rn, 1):>+10,.0f}{'σ=' + format(_rsd, ',.0f'):>12}"
      f"   [{_rp.min():+,.0f} 〜 {_rp.max():+,.0f}]")
for _lbl, _k in _rules.items():
    _n, _v, _ep = _sim(_k)
    _z = (_v - _rmu) / _rsd if _rsd > 0 else 0.0
    _beat = int((_rp < _v).sum())
    print(f"  {_lbl:<22}{_n:>7,}{_v:>+14,.0f}{_v/max(_n,1):>+10,.0f}{_z:>+8.2f}"
          f"{str(_beat) + '/' + str(len(_rp)):>12}"
          f"{'  ← 帯の外' if _beat in (0, len(_rp)) else ''}")

print(f"\n  ■ 損益差を消すのに必要な『執行コストの差』(このシムは slip=0)")
print(f"    {'ルール':<22}{'現行との差':>13}{'円/件':>9}{'必要な往復コスト差':>20}")
for _lbl, _k in _rules.items():
    if _lbl.startswith("流動性 降順"):
        continue
    _n, _v, _ep = _sim(_k)
    _dv, _per = _v - _base[1], (_v - _base[1]) / max(_n, 1)
    _bp = _per / max(_ep * args.qty, 1) * 10000.0
    print(f"    {_lbl:<22}{_dv:>+13,.0f}{_per:>+9,.0f}{_bp:>17.1f}bp"
          f"   (建値平均 {_ep:,.0f}円)")
print(f"    → 薄い銘柄が現行より **この bp ぶん余計に** 執行コストを払うなら、"
      f"優位はゼロになる。")
print(f"      小型株のスプレッドは片道 20〜50bp が普通なので、"
      f"往復で 20bp 未満の優位は追う価値がない。")

# ── 選定ファイルとの突合 ────────────────────────────────────────
pf = Path(args.proposal)
if pf.exists():
    ns: dict = {}
    try:
        exec(pf.read_text(encoding="utf-8"), ns)
        sel = list(ns.get("SELECTED") or [])
    except Exception as e:
        sel = []
        print(f"\n[warn] {pf} を読めません: {e}")
    if sel:
        def _k(t):
            c = str(t[0]).upper()
            c = c if c.endswith(".T") else c + ".T"
            return c + "|" + str(t[2] if len(t) > 2 else "")
        selset = {_k(t) for t in sel}
        traded = set(g.index)
        inb = set(g[g["inb"] > 0].index)
        print(f"\n■ 選定ファイル {pf.name} との突合")
        print(f"  選定ペア                    {len(selset):,}")
        print(f"  うち窓内でトレードが出た    {len(selset & traded):,}")
        print(f"  **うち一度でも発注された    {len(selset & inb):,} "
              f"({len(selset & inb)/len(selset)*100:.1f}%)**")
        print(f"  → 残り {len(selset) - len(selset & inb):,}ペア "
              f"({(1-len(selset & inb)/len(selset))*100:.1f}%) は"
              f"**この窓では一度も発注されていない**")

print(f"\n{'─' * 78}")
print("■ 読み方")
print(f"{'─' * 78}")
print("  ・『一度も発注されないペア』が多いなら、選定は発注に反映されていない。")
print("    流動性で足切りしてから選定するほうが、同じ計算量で密度が上がる。")
print("  ・ただし **枠外ペアの損益がプラスなら『取りこぼし』**、マイナスなら")
print("    『流動性順が結果的に悪い銘柄を避けている』。符号を必ず見ること。")
print("  ・**符号だけで動かないこと**。反実仮想の z が ±1 に収まっていれば")
print("    ランダム順と区別できていない(18.24: 発注順は σ 12万円/10ヶ月 動く)。")
print("  ・帯の外に出ていても、必要な往復コスト差が 20bp 未満なら、")
print("    薄い銘柄のスプレッドで消える。このシムは slip=0 で執行コストに盲目(18.21)。")
print("  ・予算の飽和率が低いなら薄い銘柄も枠に入るので、足切りは効かない。")
print("  ⚠ lss_trades.csv は決済済みのみ。実運用は不約定注文も枠を消費するので")
print("    ここの枠はやや緩い。絶対値は sim_oos_budget を正とすること。")
