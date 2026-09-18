"""analyze_margin_days.py — 信用残の「日数」換算が N の選別軸になるか。

⛔ **発注しない。既存の CSV を読むだけ。**

────────────────────────────────────────────────────────────────────
★ この検証で測るもの (3つだけ。これで打ち切る)
────────────────────────────────────────────────────────────────────
    buy_days  = 信用買い残 ÷ ADV20     … やれ込み(戻り売りの燃料)
    sell_days = 信用売り残 ÷ ADV20     … 踏み上げリスク
    net_days  = (買残 − 売残) ÷ ADV20  … 上2つの差(独立ではない)

  ⛔ 交互作用(ギャップ幅 × / 前日上昇率 ×)は掃かない。
     2026-09-18 までに 6指標 + 12条件 = 18検定を消費済み。
     ここに条件を足すと帰無の期待が上がるだけ。

  ⚠ 生の株数は使わない。大型株ほど絶対値が大きいので **流動性の代理**に
     なる(流動性は 2026-08 までに3回ヌル)。必ず ADV20 で割る。

────────────────────────────────────────────────────────────────────
★★ 合格条件 (回す前に凍結。結果を見て動かさない)
────────────────────────────────────────────────────────────────────
  ① TRAIN の最悪分位の bp が **明確にマイナス**
       N は稼働率40%前後で、除外して空いた枠を埋める代わりがいない。
       抜いた集団がプラスなら、抜くことは純損になる。
  ② TRAIN の **日クラスタ t** が |t| >= 2.0
       同日決済なので、下げた日は全銘柄がまとめて勝つ。件数で t を
       計算すると実効サンプルを誤認する。日を単位に数える。
  ③ **帰無較正の95%点**を超える
       同じ日の中で分位ラベルだけシャッフルし、「最悪分位を選ぶ」操作
       込みで較正する。日をまたぐシャッフルは日内相関を壊すので不可。
  ④ TRAIN の **前半・後半で符号が一致**
       片方だけなら期間への合わせ込み。

  → ①〜④を全部満たしたものだけ `--confirm` で TEST を1回だけ使う。
  → 1つも通らなければ **週次信用残ルートを閉じる**。

  ⛔ `--confirm` なしでは TEST を画面にも出さない(誤って見ないため)。
  ⛔ TEST は 1候補につき 1回だけ。

────────────────────────────────────────────────────────────────────
使い方
────────────────────────────────────────────────────────────────────
  # ① N の建てた明細を出す(adv20 列が要るので 2026-09-18 以降の版で)
  python analyze_gap_edge.py --days 4200 --min-gap-bp 100 --split 2020-09-01 \
      --min-ret1 1.753 --min-price 1000 --max-price 6000 --dump-picks n_picks.csv

  # ② TRAIN で掃く (TEST は出さない)
  python analyze_margin_days.py --picks n_picks.csv --margin margin_interest_n_10y.csv

  # ③ ①〜④を通ったものだけ、1回だけ
  python analyze_margin_days.py --picks n_picks.csv --margin margin_interest_n_10y.csv \
      --confirm sell_days:Q5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── 凍結した定数。⛔ コマンドラインから変えられない ──────────────────
_PASS_T = 2.0          # ② 日クラスタ t の下限
_PASS_NULL = 95.0      # ③ 帰無較正のパーセンタイル
_NQ = 5                # 分位数
_LAG_DAYS = 7          # 公表日 = Date + これ(暦日)。翌週金曜 = 火曜夕方より後
_AXES = ("buy_days", "sell_days", "net_days")   # ⛔ ここに足さない

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--picks", required=True,
                help="analyze_gap_edge.py --dump-picks の出力")
ap.add_argument("--margin", required=True,
                help="週次信用残 CSV (Date,Code,ShrtVol,LongVol,...)")
ap.add_argument("--confirm", default="",
                help="TEST を1回だけ使う。形式 axis:Qn (例 sell_days:Q5)")
ap.add_argument("--nulls", type=int, default=500,
                help="帰無較正の試行回数(既定500)")
ap.add_argument("--seed", type=int, default=20260918)
a = ap.parse_args()

_EPS = 1e-9


def _die(msg: str) -> None:
    sys.exit(f"[error] {msg}")


# ══════════════════════════════════════════════════════════════════
# 1. 読み込みと検品
# ══════════════════════════════════════════════════════════════════
print("=" * 74)
print(" 信用残の日数換算 — N の選別軸になるか")
print("=" * 74)

for _p in (a.picks, a.margin):
    if not Path(_p).exists():
        _die(f"{_p} がありません")

pk = pd.read_csv(a.picks)
mg = pd.read_csv(a.margin)

print(f"\n[入力] picks  {a.picks}  {len(pk):,}行")
print(f"[入力] margin {a.margin}  {len(mg):,}行")

# ── picks の検品 ──────────────────────────────────────────────────
_need = {"date", "symbol", "entry_p", "qty", "pnl"}
_miss = _need - set(pk.columns)
if _miss:
    _die(f"picks に列がありません: {sorted(_miss)}")
if "adv20" not in pk.columns:
    _die("picks に adv20 列がありません。\n"
         "         → analyze_gap_edge.py を 2026-09-18 以降の版にして\n"
         "           --dump-picks を作り直してください")
if "win" not in pk.columns:
    _die("picks に win 列(TRAIN/TEST)がありません")

pk["date"] = pd.to_datetime(pk["date"])
pk = pk.sort_values("date").reset_index(drop=True)

# ★ bp 換算。pnl は既に qty ぶんスケールされている
pk["notional"] = pk["entry_p"] * pk["qty"]
pk = pk[pk["notional"] > _EPS].copy()
pk["bp"] = pk["pnl"] / pk["notional"] * 10000.0

_n_adv = int(pk["adv20"].notna().sum())
print(f"       adv20 あり {_n_adv:,}/{len(pk):,}件 "
      f"({_n_adv / max(1, len(pk)) * 100:.1f}%)")
print(f"       窓の内訳  " + " / ".join(
    f"{_w} {int((pk['win'] == _w).sum()):,}件" for _w in pk["win"].unique()))

# ── margin の検品 ─────────────────────────────────────────────────
_needm = {"Date", "Code", "ShrtVol", "LongVol"}
_missm = _needm - set(mg.columns)
if _missm:
    _die(f"margin に列がありません: {sorted(_missm)}")

mg["Date"] = pd.to_datetime(mg["Date"])

print(f"\n[検品] margin 期間 {mg['Date'].min().date()} 〜 "
      f"{mg['Date'].max().date()} / 銘柄 {mg['Code'].nunique():,}")

# ★ 検算: 合計 == 一般 + 制度。ここが合わないとデータが壊れている
_has_split = {"ShrtNegVol", "ShrtStdVol"} <= set(mg.columns)
if _has_split:
    _d1 = (mg["ShrtVol"] - mg["ShrtNegVol"] - mg["ShrtStdVol"]).abs()
    _d2 = ((mg["LongVol"] - mg["LongNegVol"] - mg["LongStdVol"]).abs()
           if {"LongNegVol", "LongStdVol"} <= set(mg.columns) else pd.Series([0.0]))
    _bad = int((_d1 > 1.0).sum() + (_d2 > 1.0).sum())
    print(f"[検算] 合計 == 一般 + 制度 … "
          + ("✅ 全行一致" if _bad == 0 else f"⛔ **{_bad:,}行 不一致**"))
    if _bad:
        print("       ⚠ データが壊れている可能性。先に取り直してください")
else:
    print("[検算] 一般/制度の内訳列がないのでスキップ")

# ── Code を yfinance 形式へ ───────────────────────────────────────
#   J-Quants は 5桁(末尾0)。13010 → 1301.T
def _to_yf(c) -> str:
    s = str(c).strip()
    if len(s) == 5 and s.endswith("0"):
        return s[:4] + ".T"
    return s if s.endswith(".T") else s + ".T"


mg["symbol"] = mg["Code"].map(_to_yf)

_ov = len(set(mg["symbol"]) & set(pk["symbol"]))
print(f"[検品] 銘柄の重なり {_ov:,} / picks {pk['symbol'].nunique():,}銘柄")
if _ov < pk["symbol"].nunique() * 0.5:
    print("       ⛔ **半分も重なっていません**。Code の変換を疑ってください")
    print(f"         margin の例: {list(mg['Code'].head(3))}")
    print(f"         picks  の例: {list(pk['symbol'].head(3))}")

# ══════════════════════════════════════════════════════════════════
# 2. 公表日と結合  ⛔ ここが先読み防止の要
# ══════════════════════════════════════════════════════════════════
#   Date は **前週末(金)時点**。公表は翌週火曜の夕方。
#   N は前夜に候補を作るので、火曜の朝には間に合わない。
#   Date + 7暦日 = 翌週金曜 なら、どんな祝日配置でも公表後になる。
mg["avail"] = mg["Date"] + pd.Timedelta(days=_LAG_DAYS)
mg = mg.sort_values("avail").reset_index(drop=True)

_cols = ["symbol", "avail", "Date", "ShrtVol", "LongVol"]
if _has_split:
    _cols += ["ShrtStdVol", "ShrtNegVol"]
if "IssType" in mg.columns:
    _cols.append("IssType")

jd = pd.merge_asof(
    pk.sort_values("date"),
    mg[_cols],
    left_on="date", right_on="avail", by="symbol",
    direction="backward", allow_exact_matches=True,
)

_ok = jd["ShrtVol"].notna() & jd["adv20"].notna() & (jd["adv20"] > _EPS)
print(f"\n[結合] {int(_ok.sum()):,}/{len(jd):,}件 "
      f"({_ok.sum() / max(1, len(jd)) * 100:.1f}%)")

jd = jd[_ok].copy()
if jd.empty:
    _die("結合できた取引がありません")

# ★ 先読みチェック。1件でも avail > date なら結合が壊れている
_bad_fw = int((jd["avail"] > jd["date"]).sum())
print(f"[検算] 先読み(公表日 > 取引日) … "
      + ("✅ 0件" if _bad_fw == 0 else f"⛔ **{_bad_fw:,}件**"))
if _bad_fw:
    _die("結合が壊れています")

_age = (jd["date"] - jd["Date"]).dt.days
print(f"[検算] 信用残の古さ(取引日 − 残高時点) 中央 {int(_age.median())}日 / "
      f"90%点 {int(_age.quantile(0.9))}日 / 最大 {int(_age.max())}日")

# ══════════════════════════════════════════════════════════════════
# 3. 指標
# ══════════════════════════════════════════════════════════════════
jd["buy_days"] = jd["LongVol"] / jd["adv20"]
jd["sell_days"] = jd["ShrtVol"] / jd["adv20"]
jd["net_days"] = (jd["LongVol"] - jd["ShrtVol"]) / jd["adv20"]

tr = jd[jd["win"] == "TRAIN"].copy()
te = jd[jd["win"] == "TEST"].copy()
if tr.empty:
    _die("TRAIN の行がありません")

print(f"\n[窓] TRAIN {len(tr):,}件 / {tr['date'].nunique():,}営業日 "
      f"({tr['date'].min().date()} 〜 {tr['date'].max().date()})")
print(f"     TEST  {len(te):,}件 / {te['date'].nunique():,}営業日  "
      "← ⛔ --confirm まで中身は見ません")
print(f"\n[基準] TRAIN 全体 {tr['bp'].mean():+.1f}bp/件")

# ── 参考表示(判定に使わない) ──────────────────────────────────────
if _has_split:
    _sd = jd["ShrtStdVol"] / jd["adv20"]
    print(f"[参考] 制度信用の売残日数 中央 {_sd.median():.2f}日 "
          "(規制がかかるのは制度側。**判定には使いません**)")


# ══════════════════════════════════════════════════════════════════
# 4. 日クラスタ t と分位
# ══════════════════════════════════════════════════════════════════
def _daily_t(sub: pd.DataFrame, base: pd.DataFrame) -> float:
    """その分位の bp が、同じ日の全体平均とどれだけ違うか(日クラスタ頑健)。

    ★ 件数で t を出すと同日相関で実効サンプルを誤認する(下げた日は全銘柄が
      まとめて勝つ)。そこで **日をクラスタとした頑健標準誤差**を使う。

    ⛔ 「日ごとに平均して、日を単位に t」は駄目だった(2026-09-18 の自己
      テストで、力のある合成データを検出できなかった)。N は1日6〜24件で、
      5分位に割ると **1日1〜5件**しかない。日次平均のノイズが巨大になり、
      検出力が壊滅する。日内の残差を **合計**するのが正しい形。
    """
    if sub.empty or len(sub) < 10:
        return float("nan")
    _m = base.groupby("date")["bp"].mean()
    _d = sub["bp"].values - sub["date"].map(_m).values
    _d = _d[~np.isnan(_d)]
    if len(_d) < 10:
        return float("nan")
    _mean = float(_d.mean())
    _r = pd.DataFrame({"date": sub["date"].values[:len(_d)], "r": _d - _mean})
    _S = _r.groupby("date")["r"].sum().values
    _var = float((_S ** 2).sum()) / (len(_d) ** 2)
    if _var < _EPS:
        return float("nan")
    return _mean / np.sqrt(_var)


def _daily_se(sub: pd.DataFrame, base: pd.DataFrame) -> float:
    """上と同じ日クラスタ頑健の **標準誤差**(bp)。

    ★ これで **検出できる最小の効果(MDE)** が出る。「候補ゼロ」が
      『効果がない』のか『検出力が足りない』のかを区別するために要る。
      MDE = _PASS_T × SE。これより小さい効果は、あっても見えない。

    ⚠ ここで言う「効果」は **最悪分位と全体平均の差**であって、
      分位間(Q1 と Q5)の差ではない。分位間の差はこの約2倍になるので、
      「Q1-Q5 で 15bp」なら 最悪分位の差は 7〜8bp 程度。MDE と比べる
      ときはこの換算を忘れないこと(2026-09-18 の自己テストで実測)。
    """
    if sub.empty or len(sub) < 10:
        return float("nan")
    _m = base.groupby("date")["bp"].mean()
    _d = sub["bp"].values - sub["date"].map(_m).values
    _d = _d[~np.isnan(_d)]
    if len(_d) < 10:
        return float("nan")
    _r = pd.DataFrame({"date": sub["date"].values[:len(_d)],
                       "r": _d - _d.mean()})
    _S = _r.groupby("date")["r"].sum().values
    _var = float((_S ** 2).sum()) / (len(_d) ** 2)
    return float(np.sqrt(_var)) if _var > _EPS else float("nan")


def _quantiles(df: pd.DataFrame, ax: str, edges=None):
    """分位に割る。edges を渡すとその境界を使う(TEST 用)。"""
    _v = df[ax]
    if edges is None:
        _q = np.linspace(0, 1, _NQ + 1)[1:-1]
        edges = list(np.unique(_v.quantile(_q).values))
    _lab = np.searchsorted(edges, _v.values, side="right")
    out = df.copy()
    out["_q"] = [f"Q{int(i) + 1}" for i in _lab]
    return out, edges


def _report(df: pd.DataFrame, ax: str, edges=None, title=""):
    _d, edges = _quantiles(df, ax, edges)
    print(f"\n  ── {ax} {title} ─────────────────────────────")
    print(f"    {'分位':<5}{'件数':>8}{'bp/件':>10}{'日t':>8}   実数境界(日数)")
    rows = []
    for i in range(_NQ):
        _q = f"Q{i + 1}"
        _s = _d[_d["_q"] == _q]
        if _s.empty:
            continue
        _bp = float(_s["bp"].mean())
        _t = _daily_t(_s, _d)
        _lo = "-inf" if i == 0 else f"{edges[i - 1]:.2f}"
        _hi = "+inf" if i >= len(edges) else f"{edges[i]:.2f}"
        print(f"    {_q:<5}{len(_s):>8,}{_bp:>+10.1f}{_t:>+8.2f}   "
              f"{_lo} 〜 {_hi}")
        rows.append((_q, len(_s), _bp, _t))
    return _d, edges, rows


def _null_worst(df: pd.DataFrame, n: int, seed: int) -> np.ndarray:
    """帰無較正: 同じ日の中で分位ラベルだけ入れ替える。

    ⛔ 日をまたいでシャッフルしない。日内相関が壊れて帰無分布が狭くなり、
      偽陽性を過小評価する。
    ★ 「最悪分位を選ぶ」操作込みで較正する。最小を選ぶだけで平均が
      下振れするので、0 と比べてはいけない。
    """
    rng = np.random.default_rng(seed)
    _g = df.groupby("date")
    _idx = [g.index.values for _, g in _g]
    _lab = df["_q"].values
    _bp = df["bp"].values
    _qs = sorted(set(_lab))
    out = np.empty(n, dtype=float)
    for k in range(n):
        _sh = _lab.copy()
        for ix in _idx:
            _sh[ix] = rng.permutation(_lab[ix])
        _m = [_bp[_sh == q].mean() if (_sh == q).any() else np.nan for q in _qs]
        out[k] = np.nanmin(_m)
    return out


# ══════════════════════════════════════════════════════════════════
# 5. TEST を1回だけ使う (--confirm)
# ══════════════════════════════════════════════════════════════════
if a.confirm:
    if ":" not in a.confirm:
        _die("--confirm は axis:Qn の形です (例 sell_days:Q5)")
    _ax, _q = a.confirm.split(":", 1)
    if _ax not in _AXES:
        _die(f"軸は {_AXES} のいずれかです")
    if te.empty:
        _die("TEST の行がありません")

    print("\n" + "=" * 74)
    print(f" ★ TEST で確認 — {_ax} {_q}  (この軸について TEST はこれ1回)")
    print("=" * 74)

    _dtr, _edges, _ = _report(tr, _ax, None, "(TRAIN / 境界を決める)")
    _dte, _, _ = _report(te, _ax, _edges, "(TEST / TRAIN の境界を当てる)")

    _s_tr = _dtr[_dtr["_q"] == _q]
    _s_te = _dte[_dte["_q"] == _q]
    if _s_tr.empty or _s_te.empty:
        _die(f"{_q} の行がありません")

    _bt, _be = float(_s_tr["bp"].mean()), float(_s_te["bp"].mean())
    _tt, _tte = _daily_t(_s_tr, _dtr), _daily_t(_s_te, _dte)
    print(f"\n  TRAIN {_q}  {_bt:+.1f}bp/件  日t {_tt:+.2f}")
    print(f"  TEST  {_q}  {_be:+.1f}bp/件  日t {_tte:+.2f}")
    _same = (_bt < 0) == (_be < 0)
    print(f"\n  符号一致 … {'✅' if _same else '⛔ **反転**'}")
    print(f"  TEST で明確にマイナス … "
          f"{'✅' if (_be < 0 and _tte <= -_PASS_T) else '⛔'}")
    if _same and _be < 0 and _tte <= -_PASS_T:
        print("\n  ▶ 通りました。次は sim(予算・上限キャンセル込み)で確認してください。")
        print("    ⚠ compare 系の総額比較だけで発注ルールを決めないこと。")
    else:
        print("\n  ▶ 不合格。**この軸は閉じます**。基準を緩めて再判定しないこと。")
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════
# 6. TRAIN で掃く
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 74)
print(f" TRAIN で掃く — {len(_AXES)}軸。⛔ 交互作用は掃かない")
print("=" * 74)

_half = tr["date"].quantile(0.5)
_h1, _h2 = tr[tr["date"] <= _half], tr[tr["date"] > _half]

_cand: list = []
_mdes: list = []
for _ax in _AXES:
    _d, _edges, _rows = _report(tr, _ax, None, "(TRAIN)")
    if not _rows:
        continue

    # 最悪分位(= 除外候補)
    _w = min(_rows, key=lambda r: r[2])
    _q, _n, _bp, _t = _w

    # ③ 帰無較正
    _null = _null_worst(_d, a.nulls, a.seed)
    _p5 = float(np.percentile(_null, 100.0 - _PASS_NULL))
    print(f"    最悪 {_q}  {_bp:+.1f}bp/件  日t {_t:+.2f}")
    print(f"    帰無({a.nulls}本 / 同日シャッフル・最悪を選ぶ操作込み) "
          f"中央 {np.median(_null):+.1f} / {_PASS_NULL:.0f}%点 {_p5:+.1f}bp")

    # ④ 前半・後半
    _b1 = _b2 = float("nan")
    for _lbl, _sub in (("前半", _h1), ("後半", _h2)):
        _dd, _ = _quantiles(_sub, _ax, _edges)
        _ss = _dd[_dd["_q"] == _q]
        _v = float(_ss["bp"].mean()) if not _ss.empty else float("nan")
        if _lbl == "前半":
            _b1 = _v
        else:
            _b2 = _v
    _sign_ok = (not np.isnan(_b1) and not np.isnan(_b2)
                and (_b1 < 0) == (_b2 < 0))
    print(f"    前半 {_b1:+.1f} / 後半 {_b2:+.1f}  "
          f"{'✅ 符号一致' if _sign_ok else '⛔ 反転(期間依存)'}")

    # 判定
    _c1 = _bp < 0
    _c2 = (not np.isnan(_t)) and _t <= -_PASS_T
    _c3 = _bp < _p5
    _c4 = _sign_ok
    _all = _c1 and _c2 and _c3 and _c4
    print(f"    判定 ①マイナス {'✅' if _c1 else '⛔'} / "
          f"②日t<=-{_PASS_T} {'✅' if _c2 else '⛔'} / "
          f"③帰無超え {'✅' if _c3 else '⛔'} / "
          f"④符号一致 {'✅' if _c4 else '⛔'}  → "
          + ("★ 候補" if _all else "不合格"))
    # ★ 検出力。これを出さないと「候補ゼロ」を『効果なし』と読み違える
    _se = _daily_se(_d[_d["_q"] == _q], _d)
    if not np.isnan(_se):
        _mde = _PASS_T * _se
        _mdes.append(_mde)
        print(f"    検出力 SE {_se:.1f}bp → **{_mde:.1f}bp より小さい効果は"
              f"見えません**(t={_PASS_T} に必要な差)")
    if _all:
        _cand.append((_ax, _q, _bp, _t))

print("\n" + "=" * 74)
if _cand:
    print(" ★ TRAIN を通った候補")
    for _ax, _q, _bp, _t in _cand:
        print(f"    {_ax}:{_q}  {_bp:+.1f}bp/件  日t {_t:+.2f}")
    print("\n ▶ 次(1候補につき1回だけ):")
    for _ax, _q, _, _ in _cand:
        print(f"    python analyze_margin_days.py --picks {a.picks} "
              f"--margin {a.margin} --confirm {_ax}:{_q}")
else:
    print(" ⛔ **候補ゼロ。週次信用残ルートを閉じます。**")
    print("    TEST は1回も使っていません(次の軸のために温存されています)。")
    # ★★ 「効果がない」と「見えない」を混同しないための注記
    if _mdes:
        _md = float(np.median(_mdes))
        print(f"\n ⚠ ただし **検出できる下限は {_md:.0f}bp/件** です"
              f"(TRAIN {len(tr):,}件 / {tr['date'].nunique():,}営業日)。")
        print(f"    これより小さい効果は、あっても この窓では見えません。")
        print(f"    『効果がない』ではなく『{_md:.0f}bp 以上の効果は無い』"
              "が正確な結論です。")
        print(f"    ⚠ 参考: N のグロスは +15.7bp/件(2015-2020)。"
              "選別軸がその数倍の識別力を持つことは考えにくいので、")
        print(f"      **この窓で検出できる水準の効果は元から期待しにくい**"
              "という読み方になります。")
print("=" * 74)
