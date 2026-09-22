#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_sector_residual.py — ギャップを **セクター分** と **残差** に分けて測る。

⛔ **発注しない。既存の CSV を読むだけ。**

────────────────────────────────────────────────────────────────────
★ 何が新しいのか (これまでの12回のヌルと性質が違う)
────────────────────────────────────────────────────────────────────
  これまで測ったのは2種類だけ:
    (a) **日単位**の市場変数で「今日は建てない」
          §18.34b / §18.60 … 41変数 + 交互作用3,177セル → OOS R² -0.064
    (b) **銘柄の固定属性**で「この銘柄は建てない」
          §18.12/13/24/31/38/48/52/64/68/77 … 12回ヌル

  ここで測るのは **その掛け算**:
      「その銘柄の業種が、**その朝どれだけ動いたか**」
  銘柄ごとに値が違い、かつ日ごとに変わる。(a) でも (b) でもない。

★★ そして **日内で識別できる**ので検出力が変わる
      日単位の変数 → 同日は全銘柄が同じ値 → 日固定効果と共線 → 識別不能
      銘柄単位の変数 → 日内に散らばる → 日固定効果を吸収して推定できる
  §18.77 の実測 MDE 10bp(日クラスタ・単独軸)に対し、本ツールは
  日内の対比で推定するので SE がはっきり小さくなる。
  ⚠ ただし **何倍良くなるかは実データでしか分からない**ので、
     本ツールは毎回 **MDE を実測して印字する**(§18.77 の作法)。

────────────────────────────────────────────────────────────────────
★ 機構(仮説を先に言葉にする)
────────────────────────────────────────────────────────────────────
  N は「前日 +1.75%以上 上げて、翌朝 +100bp 以上ギャップアップ」を空売りする。
  そのギャップを2つに分ける:

    sector_gap  … その朝、**その業種のユニバース全体**の平均ギャップ
                  = 業界に効くニュース。**理由のある上昇**なので続伸しやすい
                  → N には不利 (bp が下がるはず)
    resid_gap   … 自分のギャップ − sector_gap
                  = その銘柄だけの思惑・需給。**オーバーシュート**
                  → 反落しやすい → N には有利 (bp が上がるはず)

  §18.52 で「ギャップアップ後の日中反落は実在する(帰無 +2.0bp に対し
  実測 +22.2bp)が +10〜18bp で執行コストに届かない」と出ている。
  **その反落が残差側に偏っているなら、残差で絞ると水準が上がる。**

  ⛔ 符号の予想を先に書いた。**逆に出たら「そういう解釈もある」と言わない。**

────────────────────────────────────────────────────────────────────
⛔⛔ いちばん危ない罠 — 軸が gap_bp の言い換えになっていないか
────────────────────────────────────────────────────────────────────
  resid_gap = gap_bp - sector_gap で、**sector_gap の散らばりは
  gap_bp よりずっと小さい**。だから放っておくと

        corr(resid_gap, gap_bp) ≒ 1

  になり、「業種残差が効いた」の正体が **ただの gap_bp** になる。
  ギャップの効果は §18.53/§18.55 で測り済み(閾値スイープ)なので、
  それを別の名前で再発見しても何も足していない。

  ⚠ 2026-09-19 に §18.77 でまったく同じ失敗をした(長期比率 long_share が
    信用倍率と Spearman 1.0000000000 = 名前が違うだけの同じ検定)。
    **単調変換で新しい軸を作れないか考えたら、まず順位相関を測る。**

  → 本ツールは **毎回 順位相関を印字**し、判定は必ず
     **gap_bp と ret1 を日内で partial out した後** の軸で行う。
     Codex の予備検証(2026-09-19)が「銘柄自身の前日上昇率・ギャップ・
     流動性を揃えると後半の差が消えた」と報告したのと同じ統制である。

────────────────────────────────────────────────────────────────────
★★ 合格条件 (回す前に凍結。結果を見て動かさない)
────────────────────────────────────────────────────────────────────
  ⓪ 判定は **gap_bp と ret1 を日内で partial out した軸**で行う
       生の軸は参考表示だけ。生で効いて partial で消えたら、それは
       「業種の情報」ではなく「その銘柄自身の動き」である。
  ① TRAIN の端の分位(Q1 または Q5)が、**日内シャッフルの帰無95%点**を超える
       ⛔ 帰無は「同じ日の中だけ」で軸のラベルを入れ替える。日をまたぐと
          日効果が壊れて帰無分布が狭くなり、偽陽性を過小評価する(§18.13)。
       ⛔ 「最良/最悪の分位を選ぶ」操作込みで較正するので、
          **帰無の中央は 0 ではない**(§18.68 で +5.4bp 出た)。
  ② **日クラスタ頑健 t** が |t| >= 2.0 (日固定効果を吸収した回帰)
  ③ TRAIN の **前半・後半で符号が一致**
  ④ 軸は **sector_gap と resid_gap の2つだけ**。⛔ ここに足さない
       (2026-09-19 時点で 41変数 + 3,177セル + 選別軸12回を消費済み)

  → ①〜③を全部満たしたものだけ `--confirm` で TEST を1回だけ使う。
  → 1つも通らなければ **この方向を閉じる**。

  ⛔ `--confirm` なしでは TEST を画面にも出さない(誤って見ないため)。
  ⛔ TEST は 1軸につき 1回だけ。

★ 通った後にやること(ここでは測らない)
  **除外ではなく発注順に載せる。** N は稼働率40%前後で、除外して空いた枠を
  埋める代わりがいない(§18.64)。一方 候補 約25件/日 に対し建てるのは
  約7件/日 なので、**どれを先に建てるかは実質タダ**。
  ⚠ 発注順は §18.24/§18.31 で3回ヌルだが、あれは全部 **静的な軸**
    (流動性・BT・ギャップ・建値)。日内で動く軸は試していない。
  ⚠ 載せるときは **ランダム順12シードの帯**と比べること(§18.24)。

────────────────────────────────────────────────────────────────────
使い方
────────────────────────────────────────────────────────────────────
  # ① 建てた明細
  python analyze_gap_edge.py --days 4200 --min-gap-bp 100 --split 2020-09-01 \
      --min-ret1 1.753 --min-price 1000 --max-price 6000 --dump-picks n_picks.csv

  # ② (date, 業種) ごとのギャップの合計と件数(セクターの動きを作るのに要る)
  python analyze_gap_edge.py --days 4200 --min-gap-bp 100 --split 2020-09-01 \
      --min-ret1 1.753 --min-price 1000 --max-price 6000 \
      --dump-universe-gaps n_universe.csv

  # ③ TRAIN で掃く (TEST は出さない)
  python analyze_sector_residual.py --picks n_picks.csv --universe n_universe.csv

  # ④ ①〜③を通ったものだけ、1回だけ
  python analyze_sector_residual.py --picks n_picks.csv --universe n_universe.csv \
      --confirm resid_gap:Q5

  業種マスタが無ければ: python fetch_jquants_extra.py --only master
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── 凍結した定数。⛔ コマンドラインから変えられない ──────────────────
_PASS_T = 2.0          # ② 日クラスタ頑健 t の下限
_PASS_NULL = 95.0      # ① 帰無較正のパーセンタイル
_NQ = 5                # 分位数
_AXES = ("sector_gap", "resid_gap")   # ⛔ ここに足さない(④)
_EPS = 1e-9

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--picks", required=True,
                help="analyze_gap_edge.py --dump-picks の出力")
ap.add_argument("--universe", required=True,
                help="analyze_gap_edge.py --dump-universe-gaps の出力")
ap.add_argument("--sector-file", default="jquants_extra/master.csv",
                help="業種マスタ(Code / Sector33CodeName)。§18.64 と同じもの")
ap.add_argument("--sector-col", default="33", choices=("33", "17"),
                help="33業種 か 17業種 か。⛔ **両方試して良い方を採らないこと**。"
                     "既定 33。同業種の相手が少なすぎたら 17 に落とすが、"
                     "その場合は『落とした』事実を記録して検定数に数える")
ap.add_argument("--min-peers", type=int, default=3,
                help="同じ日・同じ業種の相手が何銘柄以上必要か(既定3)。"
                     "少ないとセクターの動きではなく個別のノイズになる")
ap.add_argument("--confirm", default="",
                help="TEST を1回だけ使う。形式 axis:Qn (例 resid_gap:Q5)")
ap.add_argument("--nulls", type=int, default=500,
                help="帰無較正の試行回数(既定500)")
ap.add_argument("--seed", type=int, default=20260919)
a = ap.parse_args()


def _die(msg: str) -> None:
    sys.exit(f"[error] {msg}")


# ══════════════════════════════════════════════════════════════════
# 1. 読み込みと検品
# ══════════════════════════════════════════════════════════════════
print("=" * 78)
print(" ギャップの セクター分 vs 残差 — N の選別軸になるか")
print("=" * 78)

for _p in (a.picks, a.universe):
    if not Path(_p).exists():
        _die(f"{_p} がありません")

pk = pd.read_csv(a.picks, encoding="utf-8-sig")
un = pd.read_csv(a.universe, encoding="utf-8-sig")
print(f"\n[入力] picks    {a.picks}     {len(pk):,}行")
print(f"[入力] universe {a.universe}  {len(un):,}行")

_need = {"date", "symbol", "entry_p", "qty", "pnl", "gap_bp", "win"}
if _need - set(pk.columns):
    _die(f"picks に列がありません: {sorted(_need - set(pk.columns))}")
# ★ universe は **(date, 業種) ごとの合計と件数**。銘柄日を全部持たない。
#   leave-one-out は (合計 - 自分) / (件数 - 1) で作れるので、これで足りる。
#   ⛔ 銘柄日を全部書き出す版は 2.6M行 でメモリが尽きた(2026-09-19 実機)。
_needu = {"date", "sector", "gap_sum", "gap_n"}
if _needu - set(un.columns):
    _die(f"universe に列がありません: {sorted(_needu - set(un.columns))}\n"
         "         → analyze_gap_edge.py --dump-universe-gaps を\n"
         "           2026-09-19 以降の版で出し直してください")
# ★ ret1 は統制に要る。picks 側に入っている(2026-09-19 以降の版)
_HAS_RET1 = "ret1" in pk.columns and pk["ret1"].notna().mean() > 0.95
if not _HAS_RET1:
    print("\n⚠ picks に ret1 列がありません(古い版で出したファイル)。\n"
          "   統制が gap_bp だけになります。ret1 も揃えたいなら\n"
          "   --dump-picks を 2026-09-19 以降の版で出し直してください")

for _d in (pk, un):
    _d["date"] = pd.to_datetime(_d["date"])

# ★ bp 換算。pnl は既に qty ぶんスケールされている
pk["notional"] = pk["entry_p"] * pk["qty"]
pk = pk[pk["notional"] > _EPS].copy()
pk["bp"] = pk["pnl"] / pk["notional"] * 1e4

# ⛔ 方向が混ざっていないか。side 列が全部 +1 になる不具合を3ヶ月見逃した前例あり
if "side" in pk.columns:
    _sd = pk["side"].value_counts().to_dict()
    print(f"[検品] 方向 {_sd}")
    if len(_sd) > 1:
        _die("picks に両方向が混ざっています。--side を片側にして出し直してください")

print(f"[検品] picks    {pk['date'].nunique():,}営業日 / "
      f"{pk['symbol'].nunique():,}銘柄 / 窓 "
      + " ".join(f"{w} {int((pk['win'] == w).sum()):,}件"
                 for w in pk["win"].unique()))
print(f"[検品] universe {un['date'].nunique():,}営業日 × "
      f"{un['sector'].nunique():,}業種 / "
      f"1業種あたり中央 {un['gap_n'].median():.0f}銘柄/日")

# ── 業種マスタ ────────────────────────────────────────────────────
_sp = Path(a.sector_file)
if not _sp.exists():
    _die(f"{_sp} がありません。\n"
         "         → python fetch_jquants_extra.py --only master")
_mst = pd.read_csv(_sp, dtype=str, encoding="utf-8-sig")
_cc = next((c for c in _mst.columns if c.lower() == "code"), None)
_key = f"sector{a.sector_col}"
_cs = next((c for c in _mst.columns
            if _key in c.lower() and "name" in c.lower()), None)
if not (_cc and _cs):
    _die(f"{_sp} に Code / Sector{a.sector_col}CodeName がありません "
         f"(列: {list(_mst.columns)[:8]})")


def _to_yf(c) -> str:
    """J-Quants の5桁(末尾0) → yfinance 形式。13010 → 1301.T"""
    s = str(c).strip()
    if len(s) == 5 and s.endswith("0"):
        return s[:4] + ".T"
    return s if s.endswith(".T") else s + ".T"


_sec = {_to_yf(getattr(r, _cc)): str(getattr(r, _cs))
        for r in _mst.itertuples(index=False)}
print(f"[入力] 業種マスタ {_sp} … {len(_sec):,}銘柄 ({a.sector_col}業種 / "
      f"{len(set(_sec.values())):,}区分)")
print("  ⚠ **いまの上場一覧を過去に遡って当てている**。上場廃止銘柄の欠落と"
      "業種変更の未反映は残る(探索用)")

pk["sector"] = pk["symbol"].map(_sec)
_r1 = pk["sector"].notna().mean() * 100
print(f"[検算] 業種の判明率  picks {_r1:.1f}%")
if _r1 < 90:
    _die("picks の業種判明率が低すぎます。マスタを取り直してください")
# ⛔ picks と universe が **同じマスタ**で作られているか。違うと
#   leave-one-out の分母がずれて残差が壊れる(気づけない形で)。
_us = set(un["sector"].unique())
_ps = set(pk["sector"].dropna().unique())
_bad = _ps - _us
print(f"[検算] picks の業種が universe に在る … "
      + ("✅ 全区分" if not _bad else f"⛔ **{len(_bad)}区分 欠け** {sorted(_bad)[:3]}"))
if _bad:
    _die("業種マスタが食い違っています。同じ --sector-file で出し直してください")
pk = pk[pk["sector"].notna()].copy()

# ══════════════════════════════════════════════════════════════════
# 2. セクター分と残差
# ══════════════════════════════════════════════════════════════════
#   sector_gap … その朝、その業種の **ユニバース全体** の平均ギャップ
#   ⛔ 候補(ギャップアップした銘柄)だけの平均を使ってはいけない。
#     定義上そこは必ず大きく、「セクターの動き」にならない。
#   ★ 自分自身を除く(leave-one-out)。除かないと resid が自分の分だけ
#     機械的に縮み、軸が自分の bp と相関してしまう。
_g = un.set_index(["date", "sector"])[["gap_sum", "gap_n"]]
_g.columns = ["s", "n"]
pk = pk.join(_g, on=["date", "sector"])
pk["n_peers"] = (pk["n"].fillna(0) - 1).astype(int)     # 自分を除いた相手の数
pk["sector_gap"] = (pk["s"] - pk["gap_bp"]) / pk["n_peers"].where(
    pk["n_peers"] > 0)
pk["resid_gap"] = pk["gap_bp"] - pk["sector_gap"]

print("\n[検算] 同業種の相手の数(自分を除く)")
_np_ = pk["n_peers"]
for _q, _v in (("中央", _np_.median()), ("25%点", _np_.quantile(.25)),
               ("10%点", _np_.quantile(.10))):
    print(f"    {_q:<6} {_v:>6.0f}銘柄")
_thin = int((_np_ < a.min_peers).sum())
print(f"    相手が {a.min_peers}銘柄未満 … {_thin:,}件 "
      f"({_thin / max(1, len(pk)) * 100:.1f}%) → 除外")
if _np_.median() < a.min_peers:
    print(f"    ⚠ **中央値が {a.min_peers} を下回っています**。"
          f"--sector-col 17 で粗くすることを検討(ただし検定数に数える)")

_n0 = len(pk)
pk = pk[(pk["n_peers"] >= a.min_peers) & pk["sector_gap"].notna()].copy()
print(f"[検算] 残った取引 {len(pk):,}/{_n0:,}件 "
      f"({len(pk) / max(1, _n0) * 100:.1f}%)")
if pk.empty:
    _die("残った取引がありません")

print(f"\n[検算] 軸の分布")
for _ax in _AXES:
    _v = pk[_ax]
    print(f"    {_ax:<12} 中央 {_v.median():+8.1f}bp / "
          f"10%点 {_v.quantile(.10):+8.1f} / 90%点 {_v.quantile(.90):+8.1f}")
# ★ 分解が足し算として閉じているか
_chk = (pk["sector_gap"] + pk["resid_gap"] - pk["gap_bp"]).abs().max()
print(f"[検算] sector_gap + resid_gap == gap_bp … "
      + ("✅" if _chk < 1e-6 else f"⛔ **最大ずれ {_chk:.3g}**"))

# ── ret1 を universe から持ってくる(統制に要る)────────────────────
_CTRL = ["gap_bp"] + (["ret1"] if _HAS_RET1 else [])
print(f"[検算] 統制に使う列 … {' + '.join(_CTRL)}")

# ⛔⛔ 軸が gap_bp の言い換えになっていないか。**判定の前に必ず見る**
print("\n[検算] 軸と gap_bp の順位相関 "
      "(1.0 に近いほど『名前が違うだけの gap_bp』)")
for _ax in _AXES:
    _sp = pk[_ax].rank().corr(pk["gap_bp"].rank())
    _mk = ("⛔ **ほぼ gap_bp そのもの**" if abs(_sp) > 0.95
           else "⚠ 強い" if abs(_sp) > 0.80 else "✅")
    print(f"    {_ax:<12} Spearman {_sp:+.4f}  {_mk}")
print(f"    → 判定は **{'+'.join(_CTRL)} を日内で partial out した軸**で行う")
# ★ gap_bp を統制した時点で resid_gap ≡ -sector_gap(定義上 gap = sector + resid)。
#   合成データで β が ±0.4114 と符号だけ反転することを確認済み(2026-09-19)。
#   ⛔ 2軸に見えるが **実質1検定**。多重検定はこちらで数える。
print("    ⚠ gap_bp を統制すると resid_gap ≡ -sector_gap(gap = sector + resid)。")
print("      2軸に見えて **実質1検定**。符号が反転するだけなので、"
      "両方が『候補』になっても 1つと数える")

# ══════════════════════════════════════════════════════════════════
# 3. 日内の推定(ここが本ツールの要点)
# ══════════════════════════════════════════════════════════════════
def _demean(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """日固定効果を吸収する。日単位の変数はここで **ゼロになる**(=識別不能)。"""
    out = df.copy()
    for c in cols:
        out[f"{c}_dm"] = out[c] - out.groupby("date")[c].transform("mean")
    return out


def _partial(df: pd.DataFrame, ax: str) -> pd.DataFrame:
    """軸から **gap_bp と ret1 の日内成分を抜く**。

    ⛔ これを通さないと、resid_gap が gap_bp の言い換えであることに
      気づけない(§18.77 で long_share が信用倍率と Spearman 1.0 だった件)。
    ★ Codex の予備検証(2026-09-19)が「銘柄自身の前日上昇率・ギャップ・
      流動性を揃えると後半の差が消えた」と報告したのと同じ統制。
    """
    d = _demean(df, ["bp", ax] + _CTRL)
    Z = np.column_stack([d[f"{c}_dm"].to_numpy(float) for c in _CTRL])
    y = d[f"{ax}_dm"].to_numpy(float)
    ok = np.isfinite(y) & np.isfinite(Z).all(axis=1)
    out = d.copy()
    r = np.full(len(d), np.nan)
    if ok.sum() >= 30:
        try:
            b, *_ = np.linalg.lstsq(Z[ok], y[ok], rcond=None)
            r[ok] = y[ok] - Z[ok] @ b
        except np.linalg.LinAlgError:
            r[ok] = y[ok]
    out[f"{ax}_p"] = r
    return out


def _cluster_beta(df: pd.DataFrame, ax: str, partial: bool = True):
    """日固定効果つき回帰 + 日クラスタ頑健 SE。(beta, se, t, mde) を返す。

    partial=True なら軸から gap_bp / ret1 の日内成分を抜いた後で推定する。
    """
    # ⛔ rename で f"{ax}_dm" に寄せてはいけない。_partial は _demean を
    #   通すので **同名の列が既にあり**、重複して DataFrame が返る
    #   (2026-09-19 に踏んだ。形が (n,2) になって broadcast エラー)。
    if partial:
        d = _partial(df, ax)
        xcol = f"{ax}_p"
    else:
        d = _demean(df, ["bp", ax])
        xcol = f"{ax}_dm"
    x = d[xcol].to_numpy(float)
    y = d["bp_dm"].to_numpy(float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, dt = x[ok], y[ok], d["date"].to_numpy()[ok]
    sxx = float((x * x).sum())
    if sxx < _EPS or len(x) < 30:
        return (np.nan,) * 4
    beta = float((x * y).sum() / sxx)
    e = y - beta * x
    # CRVE: V = Σ_d (Σ_i x_i e_i)^2 / (Σ x²)²
    s = pd.Series(x * e).groupby(pd.Series(dt)).sum().to_numpy()
    v = float((s * s).sum()) / (sxx ** 2)
    se = float(np.sqrt(v)) if v > 0 else np.nan
    t = beta / se if se and np.isfinite(se) else np.nan
    # ★ MDE = t閾値 × SE × (軸の日内sd)。bp 単位に直して読めるようにする
    sd_x = float(np.std(x))
    mde = _PASS_T * se * sd_x if np.isfinite(se) else np.nan
    return beta, se, t, mde


def _within_q(df: pd.DataFrame, ax: str, partial: bool = True) -> pd.DataFrame:
    """**日の中で**分位に割る。日単位の変数なら全員が同じ分位になる。

    partial=True(既定・判定用)なら gap_bp / ret1 を抜いた後の軸で割る。
    """
    if partial:
        out = _partial(df, ax)
        out = out[out[f"{ax}_p"].notna()].copy()
        ax = f"{ax}_p"
    else:
        out = df.copy()
    r = out.groupby("date")[ax].rank(pct=True, method="first")
    out["_q"] = "Q" + (np.ceil(r * _NQ).clip(1, _NQ)).astype(int).astype(str)
    # 日内の対比で見るので、bp は日平均からの残差で評価する
    out["bp_dm"] = out["bp"] - out.groupby("date")["bp"].transform("mean")
    return out


def _report(df: pd.DataFrame, ax: str, title: str = "", partial: bool = True):
    d = _within_q(df, ax, partial)
    _lbl = f"{ax}（{'+'.join(_CTRL)} を統制）" if partial else f"{ax}（生）"
    print(f"\n  ── {_lbl} {title} ─────────────────────")
    print(f"    {'分位':<5}{'件数':>8}{'日内bp':>10}{'軸の中央':>11}")
    _col = f"{ax}_p" if partial else ax
    rows = []
    for i in range(_NQ):
        q = f"Q{i + 1}"
        s = d[d["_q"] == q]
        if s.empty:
            continue
        v = float(s["bp_dm"].mean())
        print(f"    {q:<5}{len(s):>8,}{v:>+10.1f}{s[_col].median():>+10.1f}bp")
        rows.append((q, len(s), v))
    return d, rows


def _null_extreme(d: pd.DataFrame, ax: str, n: int, seed: int) -> np.ndarray:
    """**同じ日の中だけ**で分位ラベルを入れ替え、『端の分位を選ぶ』操作込みで較正。

    ⛔ 日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなる(§18.13)。
    ⛔ 端(Q1/Q5)の **絶対値が最大のほう** を選ぶので、帰無の中央は 0 ではない。
    """
    rng = np.random.default_rng(seed)
    groups = [g["bp_dm"].to_numpy(float) for _, g in d.groupby("date")]
    labs = [g["_q"].to_numpy() for _, g in d.groupby("date")]
    out = np.empty(n)
    for k in range(n):
        acc: dict = {}
        for vals, lb in zip(groups, labs):
            p = rng.permutation(len(vals))
            for q, v in zip(lb, vals[p]):
                a_ = acc.setdefault(q, [0.0, 0])
                a_[0] += v
                a_[1] += 1
        m = {q: s / c for q, (s, c) in acc.items() if c}
        lo = m.get("Q1", 0.0)
        hi = m.get(f"Q{_NQ}", 0.0)
        out[k] = hi if abs(hi) >= abs(lo) else lo
    return out


# ── 窓の分割。⛔ --confirm 以外では TEST を触らない ────────────────
tr = pk[pk["win"] == "TRAIN"].copy()
te = pk[pk["win"] == "TEST"].copy()
if tr.empty:
    _die("TRAIN の行がありません")
print(f"\n[窓] TRAIN {len(tr):,}件 / {tr['date'].nunique():,}営業日")

# ══════════════════════════════════════════════════════════════════
# 4. TEST を1回だけ使う (--confirm)
# ══════════════════════════════════════════════════════════════════
if a.confirm:
    if ":" not in a.confirm:
        _die("--confirm は axis:Qn の形です (例 resid_gap:Q5)")
    ax, q = a.confirm.split(":", 1)
    if ax not in _AXES:
        _die(f"軸は {_AXES} のいずれかです")
    if te.empty:
        _die("TEST の行がありません")

    print("\n" + "=" * 78)
    print(f" ★ TEST で確認 — {ax} {q}  (この軸について TEST はこれ1回)")
    print("=" * 78)
    dtr, _ = _report(tr, ax, "(TRAIN)")
    dte, _ = _report(te, ax, "(TEST)")
    str_ = dtr[dtr["_q"] == q]
    ste = dte[dte["_q"] == q]
    if str_.empty or ste.empty:
        _die(f"{q} の行がありません")
    btr, bte = float(str_["bp_dm"].mean()), float(ste["bp_dm"].mean())
    _, _, t_tr, _ = _cluster_beta(tr, ax)
    _, _, t_te, _ = _cluster_beta(te, ax)
    print(f"\n  TRAIN {q}  日内 {btr:+.1f}bp   回帰の日クラスタ t {t_tr:+.2f}")
    print(f"  TEST  {q}  日内 {bte:+.1f}bp   回帰の日クラスタ t {t_te:+.2f}")
    same = (btr > 0) == (bte > 0)
    print(f"\n  符号一致 … {'✅' if same else '⛔ **反転**'}")
    print(f"  TEST の |t| >= {_PASS_T} … "
          f"{'✅' if abs(t_te) >= _PASS_T else '⛔'}")
    if same and abs(t_te) >= _PASS_T:
        print("\n  ▶ ★ 通りました。ただし採用の前に:")
        print("    ① **除外ではなく発注順**に載せる(N は稼働率40%・§18.64)")
        print("    ② **ランダム順12シードの帯**と比べる(§18.24)。"
              "帯の中なら『測れていない』")
        print("    ③ 予算シミュを通す。総額比較だけで発注ルールを決めない(§18.10)")
    else:
        print("\n  ▶ ⛔ 不合格。**この軸は閉じます**。")
        print("    基準を緩めて再判定しないこと。TEST は使い切りました。")
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════
# 5. TRAIN で掃く
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print(f" TRAIN で掃く — {len(_AXES)}軸。⛔ ここに足さない(④)")
print(" 予想した符号:  sector_gap → **マイナス**(続伸して不利) /"
      " resid_gap → **プラス**(反落して有利)")
print("=" * 78)

half = tr["date"].quantile(0.5)
h1, h2 = tr[tr["date"] <= half], tr[tr["date"] > half]
cand: list = []

for ax in _AXES:
    # ★ 生の軸も出す。**生で効いて統制後に消えるなら、それは業種ではなく
    #   その銘柄自身の動き**(Codex の予備検証と同じ形)。
    _draw, _rraw = _report(tr, ax, "(TRAIN)", partial=False)
    if _rraw:
        _lo0 = next((r for r in _rraw if r[0] == "Q1"), None)
        _hi0 = next((r for r in _rraw if r[0] == f"Q{_NQ}"), None)
        if _lo0 and _hi0:
            _, _, _t0, _ = _cluster_beta(tr, ax, partial=False)
            print(f"    生の端 {_hi0[2]:+.1f} / {_lo0[2]:+.1f}bp  "
                  f"日クラスタ t {_t0:+.2f}  ← **参考。判定に使わない**")

    d, rows = _report(tr, ax, "(TRAIN)")
    if not rows:
        continue
    # 端の分位。絶対値が大きいほうを見る(帰無も同じ操作で較正する)
    lo = next((r for r in rows if r[0] == "Q1"), None)
    hi = next((r for r in rows if r[0] == f"Q{_NQ}"), None)
    if lo is None or hi is None:
        continue
    w = hi if abs(hi[2]) >= abs(lo[2]) else lo
    q, _n, bp = w

    # ① 帰無較正(日内シャッフル・端を選ぶ操作込み)
    nl = _null_extreme(d, ax, a.nulls, a.seed)
    p_hi = float(np.percentile(np.abs(nl), _PASS_NULL))
    print(f"    端の分位 {q}  日内 {bp:+.1f}bp")
    print(f"    帰無({a.nulls}本 / 日内シャッフル・端を選ぶ操作込み) "
          f"中央|値| {np.median(np.abs(nl)):.1f} / "
          f"{_PASS_NULL:.0f}%点 {p_hi:.1f}bp")

    # ② 日固定効果つき回帰 + 日クラスタ頑健 t
    beta, se, t, mde = _cluster_beta(tr, ax)
    print(f"    回帰(日固定効果あり) β {beta:+.4f} bp/bp  "
          f"日クラスタ t {t:+.2f}")
    if np.isfinite(mde):
        print(f"    検出力 → **{mde:.1f}bp より小さい効果は見えません** "
              f"(N のグロス +15.7bp の {mde / 15.7 * 100:.0f}%)")

    # ③ 前半・後半
    b1 = b2 = float("nan")
    for lbl, sub in (("前半", h1), ("後半", h2)):
        dd = _within_q(sub, ax)
        ss = dd[dd["_q"] == q]
        v = float(ss["bp_dm"].mean()) if not ss.empty else float("nan")
        if lbl == "前半":
            b1 = v
        else:
            b2 = v
    sgn = (not np.isnan(b1) and not np.isnan(b2) and (b1 > 0) == (b2 > 0))
    print(f"    前半 {b1:+.1f} / 後半 {b2:+.1f}  "
          f"{'✅ 符号一致' if sgn else '⛔ 反転(期間依存)'}")

    c1 = abs(bp) > p_hi
    c2 = (not np.isnan(t)) and abs(t) >= _PASS_T
    print(f"    判定 ①帰無超え {'✅' if c1 else '⛔'} / "
          f"②|t|>={_PASS_T} {'✅' if c2 else '⛔'} / "
          f"③符号一致 {'✅' if sgn else '⛔'}  → "
          + ("★ 候補" if (c1 and c2 and sgn) else "不合格"))
    if c1 and c2 and sgn:
        cand.append((ax, q, bp, t))

print("\n" + "=" * 78)
if cand:
    print(" ★ TRAIN を通った候補")
    for ax, q, bp, t in cand:
        print(f"    {ax}:{q}  日内 {bp:+.1f}bp  日クラスタ t {t:+.2f}")
    print("\n ▶ 次(1軸につき1回だけ):")
    for ax, q, _, _ in cand:
        print(f"    python analyze_sector_residual.py --picks {a.picks} "
              f"--universe {a.universe} --confirm {ax}:{q}")
else:
    print(" ⛔ **候補ゼロ。セクター分解のルートを閉じます。**")
    print("    ⚠ ただし『効果がない』ではなく **『MDE より大きい効果は無い』**。")
    print("      上の検出力の行を必ず併記すること(§18.77)。")
print("=" * 78)
