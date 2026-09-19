r"""analyze_oos_edge.py — ローリングOOSの生トレードから『効くフィルタ』を探す。

なぜこのデータでやるのか
------------------------
`run_oos_folds.py` が出す `oos_raw_fold*.csv` は **純OOS**:
基準月ごとに選定し直し → 累積マージ → **翌月だけ**を評価(= daily.bat と同一手順)。
選定バイアスが構造的に入らないので、フィルタ探索の土台として最も信頼できる。

⛔ ただし 18.13 で同じことをやって **15属性×5分位=78検定 → 候補ゼロ**だった。
   そのとき踏んだ罠を全部避ける設計にしてある:

   1. **日クラスタ頑健**: lss は同日決済なので、下げた日は全銘柄まとめて勝つ。
      件数を n にすると実効サンプルを数十倍に誤認する。日ごとに等ウェイト平均して
      **日数**で検定する。
   2. **探索/確認の分割**: この10ヶ月の中で最良の閾値を選べば必ず良く見える。
      前半5フォールドで探索し、**後半5フォールドで確認**する。両方で同じ向きに
      出ないものは採らない。
   3. **多重検定の帰無較正**: 属性×分位を何十個も試せば偶然いくつか通る。
      損益を **日ブロックを保った巡回シフト**で並べ替えた偽データで同じ探索を回し、
      『偶然でも出る個数』を実測して比べる。完全シャッフルは日内相関を壊すので
      帰無分布が狭くなり偽陽性を過小評価する。

使い方
------
  python analyze_oos_edge.py --raw "oos_raw_fold*.csv"
  python analyze_oos_edge.py --raw "oos_raw_fold*.csv" --bt-min 40   # 予算シミュと同じ母集団
  python analyze_oos_edge.py --raw "oos_raw_fold*.csv" --nulls 500

読み方
------
候補が帰無の95%点を超えて出て、かつ確認フォールドでも同じ向きなら本物の可能性。
候補0個 or 帰無と同数なら **その属性群にエッジは無い**(18.13 と同じ結論)。
"""
from __future__ import annotations

import argparse
import glob as _glob
import math
import sys

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(description="ローリングOOSの生データからフィルタを探す")
ap.add_argument("--raw", default="oos_raw_fold*.csv", help="生CSVのグロブ")
ap.add_argument("--bt-min", type=float, default=0.0,
                help="BT下限。予算シミュと母集団を揃えるなら 40")
ap.add_argument("--bins", type=int, default=5, help="連続値の分位数")
ap.add_argument("--nulls", type=int, default=300, help="帰無較正の反復回数")
ap.add_argument("--t-min", type=float, default=2.0, help="候補とみなす |t| の下限")
ap.add_argument("--min-n", type=int, default=100, help="ビンの最低件数")
ap.add_argument("--exclude-strat", default="転換",
                help="除外する戦略(既定『転換』= lssではない / 18.5.3)")
ap.add_argument("--seed", type=int, default=42)
args = ap.parse_args()

files = sorted(_glob.glob(args.raw))
if not files:
    sys.exit(f"[error] {args.raw} に一致するファイルがありません")
d = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in files], ignore_index=True)

if args.exclude_strat.strip():
    _ex = {s.strip() for s in args.exclude_strat.split(",") if s.strip()}
    _hit = d["strategy"].astype(str).isin(_ex)
    if _hit.any():
        print(f"[入力] 戦略 {'/'.join(sorted(_ex))} を除外: {int(_hit.sum()):,}件")
        d = d[~_hit].reset_index(drop=True)

d = d[d["filled"] == 1].reset_index(drop=True)          # 約定分だけが損益を持つ
if args.bt_min > 0:
    n0 = len(d)
    d = d[d["bt_score"] >= args.bt_min].reset_index(drop=True)
    print(f"[入力] BT>={args.bt_min:.0f} で {n0:,} → {len(d):,}件")
d["date"] = pd.to_datetime(d["entry_date"])

print(f"[入力] {len(files)}ファイル / 約定 {len(d):,}件 / "
      f"{d['date'].min().date()}〜{d['date'].max().date()} / {d['date'].nunique():,}営業日")
print(f"       損益 {d['pnl'].sum():+,.0f}円 ({d['pnl'].mean():+,.0f}円/件) "
      f"勝率 {(d['pnl'] > 0).mean() * 100:.1f}%")

# ── 探索 / 確認 の分割 (フォールドの前半 / 後半) ─────────────────
folds = sorted(d["fold"].unique())
cut = folds[len(folds) // 2]
disc = d[d["fold"] < cut].reset_index(drop=True)
conf = d[d["fold"] >= cut].reset_index(drop=True)
print(f"[分割] 探索 fold{folds[0]}〜{cut - 1} ({len(disc):,}件) / "
      f"確認 fold{cut}〜{folds[-1]} ({len(conf):,}件)")


# ── 属性 ────────────────────────────────────────────────────────────
def _add_features(x: pd.DataFrame) -> pd.DataFrame:
    x = x.copy()
    # 同日のシグナル数 = その日どれだけ候補が出たか(相場の荒れ具合の代理)
    x["同日件数"] = x.groupby("date")["pnl"].transform("size")
    # 同じ銘柄が同日に何戦略で出たか(重なり=確信度の代理)
    x["銘柄重複"] = x.groupby(["date", "symbol"])["pnl"].transform("size")
    x["曜日"] = x["date"].dt.dayofweek
    # ★ 予算キャップの本丸: その日の流動性降順で何番目か(1=最も流動的)。
    #   予算400万は1日十数件しか建てられないので、実運用は必ず上位だけを買う。
    #   ここに差が無ければ『予算キャップの切り捨て方』にエッジは無い。
    _liq = pd.to_numeric(x["liquidity"], errors="coerce").fillna(0.0)
    x["_liq"] = _liq
    x["日内順位"] = x.groupby("date")["_liq"].rank(ascending=False, method="first")
    x["日内順位率"] = x["日内順位"] / x["同日件数"]
    return x


disc, conf, d = _add_features(disc), _add_features(conf), _add_features(d)

CONT = ["bt_score", "liquidity", "entry_p", "同日件数", "銘柄重複",
        "日内順位", "日内順位率"]
CATS = ["strategy", "曜日"]


def _daily_t(sub: pd.DataFrame, rest: pd.DataFrame) -> float:
    """ビン vs それ以外 の差を **日クラスタ頑健** に t 検定する。

    同日決済は日内で強く相関するので、日ごとに等ウェイト平均を取り
    『日』を1標本として Welch の t を計算する(件数ベースは実効Nを誤認する)。
    """
    a = sub.groupby("date")["pnl"].mean()
    b = rest.groupby("date")["pnl"].mean()
    if len(a) < 10 or len(b) < 10:
        return float("nan")
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    if not (va + vb > 0):
        return float("nan")
    return float((a.mean() - b.mean()) / math.sqrt(va + vb))


def _scan(x: pd.DataFrame) -> list:
    """全属性×全ビンを走査し、|t| が閾値以上のものを候補として返す。"""
    out = []
    for col in CONT:
        v = pd.to_numeric(x[col], errors="coerce")
        try:
            lab = pd.qcut(v, args.bins, labels=False, duplicates="drop")
        except Exception:
            continue
        for b in sorted(pd.Series(lab).dropna().unique()):
            m = (lab == b).to_numpy()
            if m.sum() < args.min_n or (~m).sum() < args.min_n:
                continue
            t = _daily_t(x[m], x[~m])
            if t == t and abs(t) >= args.t_min:
                out.append((col, f"分位{int(b) + 1}/{args.bins}", int(m.sum()),
                            float(x[m]["pnl"].mean()), t))
    for col in CATS:
        for val in x[col].dropna().unique():
            m = (x[col] == val).to_numpy()
            if m.sum() < args.min_n or (~m).sum() < args.min_n:
                continue
            t = _daily_t(x[m], x[~m])
            if t == t and abs(t) >= args.t_min:
                out.append((col, str(val), int(m.sum()),
                            float(x[m]["pnl"].mean()), t))
    return out


# ── 全属性の分位表 (候補に上がらなかったものも必ず見せる) ──────
# 候補リストだけ出すと「何を試して何が出なかったか」が残らない。
# 探索/確認を並べて出し、符号が揃っているかを目で確認できるようにする。
print(f"\n{'=' * 96}")
print("■ 全属性の分位表 (探索 fold前半 / 確認 fold後半)")
print(f"{'=' * 96}")
for col in CONT:
    v_d = pd.to_numeric(disc[col], errors="coerce")
    try:
        lab_d, edges = pd.qcut(v_d, args.bins, labels=False, retbins=True,
                               duplicates="drop")
    except Exception:
        continue
    print(f"\n  ◆ {col}")
    print(f"    {'分位':<8}{'範囲':>24}{'探索件数':>9}{'探索円/件':>11}{'t':>7}"
          f"{'確認件数':>9}{'確認円/件':>11}")
    _cv = pd.to_numeric(conf[col], errors="coerce")
    for b in range(len(edges) - 1):
        m = (lab_d == b).to_numpy()
        if m.sum() == 0:
            continue
        lo = -float("inf") if b == 0 else edges[b]
        hi = float("inf") if b == len(edges) - 2 else edges[b + 1]
        mc = ((_cv >= lo) & (_cv <= hi)).to_numpy()
        t = _daily_t(disc[m], disc[~m])
        rng_s = f"{edges[b]:,.0f}〜{edges[b + 1]:,.0f}"
        print(f"    {b + 1:<8}{rng_s:>24}{int(m.sum()):>9,}"
              f"{disc[m]['pnl'].mean():>+11,.0f}{t:>+7.2f}"
              f"{int(mc.sum()):>9,}"
              f"{(conf[mc]['pnl'].mean() if mc.sum() else float('nan')):>+11,.0f}")
for col in CATS:
    print(f"\n  ◆ {col}")
    print(f"    {'値':<12}{'探索件数':>9}{'探索円/件':>11}{'t':>7}"
          f"{'確認件数':>9}{'確認円/件':>11}")
    for val in sorted(disc[col].dropna().unique(), key=str):
        m = (disc[col] == val).to_numpy()
        mc = (conf[col] == val).to_numpy()
        if m.sum() == 0:
            continue
        t = _daily_t(disc[m], disc[~m])
        print(f"    {str(val):<12}{int(m.sum()):>9,}{disc[m]['pnl'].mean():>+11,.0f}"
              f"{t:>+7.2f}{int(mc.sum()):>9,}"
              f"{(conf[mc]['pnl'].mean() if mc.sum() else float('nan')):>+11,.0f}")

# ── 帰無較正 (日ブロックを保った巡回シフト) ─────────────────────
# ⛔ 完全シャッフルにしてはいけない。lss は同日決済で日内相関が強いので、
#    シャッフルすると帰無分布が不当に狭くなり偽陽性を過小評価する。
#
# ⛔ 日付ラベルだけをずらしてもいけない(最初の実装の誤り)。日ブロックを丸ごと
#    移すと groupby("date") の分割が元と同一のままなので t が一切変わらず、
#    帰無が縮退する(毎回まったく同じ候補数が出る)。
#
# 正しくは **損益の側を日ブロック単位で回す**: 各日の行に、別の日の損益ベクトルを
# (長さを合わせて循環コピーして)割り当てる。日内相関は保たれ、
# 『属性と損益の対応』だけが壊れる。
rng = np.random.default_rng(args.seed)
days = np.array(sorted(disc["date"].unique()))
_day_pnl = [disc.loc[disc["date"] == dt, "pnl"].to_numpy() for dt in days]
_day_pos = [np.flatnonzero((disc["date"] == dt).to_numpy()) for dt in days]
null_counts = []
print(f"\n[帰無較正] 損益を日ブロック単位で巡回シフト {args.nulls}回 ...", flush=True)
for k in range(args.nulls):
    sh = int(rng.integers(1, len(days)))
    fake = disc.copy()
    newp = np.empty(len(fake), dtype=float)
    for i in range(len(days)):
        src = _day_pnl[(i + sh) % len(days)]
        pos = _day_pos[i]
        newp[pos] = np.resize(src, len(pos))         # 長さ違いは循環コピー
    fake["pnl"] = newp
    null_counts.append(len(_scan(fake)))
null_counts = np.array(null_counts)
p95 = float(np.percentile(null_counts, 95))
print(f"           偶然出る候補数: 平均 {null_counts.mean():.1f} / "
      f"95%点 {p95:.0f} / 最大 {null_counts.max()}")

# ── 本番の探索 ──────────────────────────────────────────────────
cands = _scan(disc)
print(f"\n{'=' * 96}")
print(f"■ 探索フォールドでの候補 ({len(cands)}個 / 帰無の95%点 {p95:.0f}個)")
print(f"{'=' * 96}")
if not cands:
    print("  候補なし。")
else:
    print(f"  {'属性':<12}{'ビン':<12}{'件数':>7}{'円/件':>10}{'t(日)':>8}"
          f"{'確認t':>8}{'確認円/件':>11}  判定")
    print("  " + "-" * 88)
    survivors = []
    for col, lab, n, mu, t in sorted(cands, key=lambda r: -abs(r[4])):
        # 同じ条件を確認フォールドにそのまま当てる(閾値は探索側で決めたものを流用)
        if col in CONT:
            v_d = pd.to_numeric(disc[col], errors="coerce")
            b = int(lab.split("分位")[1].split("/")[0]) - 1
            edges = pd.qcut(v_d, args.bins, retbins=True, duplicates="drop")[1]
            lo, hi = edges[b], edges[b + 1]
            # 端のビンは開区間にする。確認側は分布が違うので、閉区間で切ると
            # 探索側の min/max を外れた行が全部落ちて日数が足りず nan になる。
            if b == 0:
                lo = -float("inf")
            if b == len(edges) - 2:
                hi = float("inf")
            _cv = pd.to_numeric(conf[col], errors="coerce")
            m = ((_cv >= lo) & (_cv <= hi)).to_numpy()
        else:
            m = (conf[col].astype(str) == lab).to_numpy()
        if m.sum() < 30 or (~m).sum() < 30:
            ct, cmu = float("nan"), float("nan")
        else:
            ct, cmu = _daily_t(conf[m], conf[~m]), float(conf[m]["pnl"].mean())
        ok = (ct == ct) and (np.sign(ct) == np.sign(t)) and abs(ct) >= 1.0
        if ok:
            survivors.append((col, lab, n, mu, t, ct, cmu))
        print(f"  {col:<12}{lab:<12}{n:>7,}{mu:>+10,.0f}{t:>+8.2f}"
              f"{ct:>+8.2f}{cmu:>+11,.0f}  {'✅生存' if ok else '❌消滅'}")

# ── 判定 ────────────────────────────────────────────────────────
print(f"\n{'─' * 96}")
print("■ 判定")
print(f"{'─' * 96}")
n_c = len(cands)
if n_c == 0:
    print(f"  探索で候補ゼロ。**この属性群にエッジは無い。**(18.13 と同じ結論)")
elif n_c <= p95:
    print(f"  候補 {n_c}個 は帰無の95%点 {p95:.0f}個 **以下**。"
          f"偶然の範囲。**エッジとは言えない。**")
else:
    _sv = len(survivors) if cands else 0
    print(f"  候補 {n_c}個 > 帰無95%点 {p95:.0f}個。うち確認フォールドで生存 **{_sv}個**。")
    if _sv == 0:
        print(f"  → 探索では出たが**確認で全滅**。過剰適合。採用しない。")
    else:
        print(f"  → 生存した条件は**予算シミュ(sim_oos_budget)で検証すること**。")
        print(f"     18.10 の教訓: 『全部買えるなら得』と『予算内でどれを買うか』は別問題。")
print(f"\n  ※ 探索/確認の分割は fold 前半/後半。期間が違うのでレジームの差も混ざる。")
print(f"     生存しても『別の相場でも効く』証拠にはならない。")
