#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analyze_earnings_flag.py — 決算・業績修正のフラグが N の選別軸になるか。

⛔ 発注しない。picks CSV と 決算 CSV を読むだけ。

────────────────────────────────────────────────────────────────────
★ このツールが答える問いは **1つだけ**
────────────────────────────────────────────────────────────────────

  「決算銘柄の **絶対円損益** がマイナスで、除外後の総円損益が
    **同数をランダムに除外した帯** より改善するか」

  ⛔ 「決算銘柄が他より悪い」では足りない。N は **稼働率40%** で
    除外して空いた枠を埋める代わりの銘柄がいないので(§18.64)、
    除外の利得は **抜いた集団の絶対損益そのもの**。
    抜いた集団がプラスなら、相対的に悪くても抜くと損する。

────────────────────────────────────────────────────────────────────
★ 測る前に「測れるか」を出す (§18.77 の作法)
────────────────────────────────────────────────────────────────────

  決算あり率 r が小さいと、MDE(検出できる最小の差)が N のエッジ
  (+345円/件)と同じ桁になり、**ゼロとマイナスを区別できない**。

     r= 5%  -> MDE 385円/件  ⛔ 測れない
     r=10%  -> MDE 280円/件  ⚠ きわどい
     r=30%  -> MDE 183円/件  ✅ 十分

  「当日が決算発表予定日」は年4回なので r が数%になる。
  **その分類は最初から測れない。** 分類ごとに r を出して足切りする。

────────────────────────────────────────────────────────────────────
⛔ 契約期間より前は「決算なし」ではなく **欠測**
────────────────────────────────────────────────────────────────────

  J-Quants の契約期間は 2016-09-23 以降(2026-09-22 時点)。
  §18.54 の TRAIN は 2015-03〜2020-08 なので、**最初の18ヶ月
  (概算 2,600件・27%)に決算ラベルが付かない**。

  ⛔ ここを「決算なし」として扱うと、その期間の取引が丸ごと
    「材料なし」群に入り、**仮説が正しい場合に差が過大に出る**。
  -> `--data-since` より前は既定で **落とす**。

────────────────────────────────────────────────────────────────────
使い方
────────────────────────────────────────────────────────────────────

    # ★ これだけ。picks も決算CSV も **自動で探す**
    python analyze_earnings_flag.py

      決算CSV が無ければ  -> 検出限界の下見だけ出す
      決算CSV があれば    -> TRAIN(既定)で判定まで通す
      どのファイルを使ったかは必ず印字する

    # TRAIN で候補が出たときだけ、最後に TEST を **1回**
    python analyze_earnings_flag.py --win TEST --confirm

    # 損益を見ずに r と MDE だけ
    python analyze_earnings_flag.py --scout

    # 自己検証(合成データ。力がある/ない の2ケース + 日付境界)
    python analyze_earnings_flag.py --selftest

    # ファイルを明示したいとき
    python analyze_earnings_flag.py --picks n_picks.csv --earnings jquants_extra/statements.csv

決算 CSV に要る列(名前は自動で探す):
    コード   Code / LocalCode / symbol / code
    開示日   DisclosedDate / Date / date
    開示時刻 DisclosedTime / time            (任意。あれば寄り前/場中を分ける)
    種別     TypeOfDocument / type           (任意。決算/業績修正の区別)
"""
from __future__ import annotations

import argparse
import math
import sys

import numpy as np
import pandas as pd

# ── 判定の閾値。⛔ ここを実行時に変えられないようにする ──────────────
_PASS_T = 2.0        # 日クラスタ頑健 t の下限
_PASS_Z = 2.0        # ランダム帯の外と言うための z
_MDE_NG = 300.0      # 円/件。MDE がこれを超えたら「測れない」
_MDE_WARN = 200.0
_EDGE_YEN = 345.0    # §18.55 TRAIN の N のグロス +15.3bp/件
_SEEDS = 200         # ランダム除外の帯の本数
_DATA_SINCE = "2016-09-23"   # J-Quants の契約開始

# MDE の較正基準: §18.77 で N の TRAIN(9,670件)・日単位の軸は 10bp
#   5分位比較なので実効 n = 1/(1/1934+1/1934) = 967
_BASE_N, _BASE_MDE_BP = 967.0, 10.0

#   決算CSV の置き場。fetch_jquants_extra.py は <out-dir>/<name>.csv に書く
#   (既定の out-dir は jquants_extra/、name は statements / earnings_cal)
_EARN_CANDS = (
    "jquants_extra/statements.csv",
    "jquants_extra/fin_summary.csv",
    "jquants_extra/earnings_cal.csv",
    "jq_earnings.csv", "earnings.csv", "statements.csv",
)
#   ⛔ glob を広く取らない。jquants_extra/*.csv は信用残・銘柄マスタ・
#     業種などを全部拾う。2026-09-22 に margin_interest_n_10y.csv を
#     決算CSV として読んで、週次公表のカレンダーを測る結果を出した。
_EARN_GLOBS = ("jquants_extra/*statement*.csv", "jquants_extra/*earn*.csv",
               "jquants_extra/fin*.csv", "*earning*.csv", "*statement*.csv",
               "*kessan*.csv")
#   決算は 1銘柄あたり 年4〜8回(本決算+四半期+業績修正)。
#   これを大きく超えるなら別のデータ(信用残=年52回 / 日足=年244回)。
_MAX_PER_SYM_YEAR = 20.0

#   ⛔ **この列があったら決算CSV ではない**。頻度チェックだけでは
#     取りこぼす(2026-09-22: 合成した信用残が毎週ちがう銘柄だったので
#     頻度が低く出て通った)。列名で確実に弾く。
_NOT_EARNINGS = {
    "ShortMarginTradeVolume": "週次の信用残",
    "LongMarginTradeVolume": "週次の信用残",
    "ShortNegotiableMarginTradeVolume": "週次の信用残",
    "LongStandardizedMarginTradeVolume": "週次の信用残",
    "IssueType": "信用残 / 貸借区分",
    "CompanyName": "銘柄マスタ",
    "MarketCode": "銘柄マスタ",
    "ScaleCategory": "銘柄マスタ",
    "Sector33CodeName": "業種マスタ",
    "TurnoverValue": "日足",
    "AdjustmentClose": "日足",
}
_PICKS_CANDS = ("n_picks.csv", "picks.csv", "n_picks_train.csv")

_CODE_COLS = ("Code", "LocalCode", "symbol", "code", "Symbol")
_DATE_COLS = ("DisclosedDate", "Date", "date", "disclosed_date", "AnnouncementDate")
_TIME_COLS = ("DisclosedTime", "time", "disclosed_time")
_TYPE_COLS = ("TypeOfDocument", "type", "DocumentType")


def _autofind(kind: str) -> str | None:
    """決算CSV / picks を自動で探す。

    ★ ファイル名を覚えるのが面倒なので、よくある置き場を順に見る。
      ⛔ **どれを使ったかは必ず印字する**。黙って別のファイルを掴むと、
        §18.40b(1日つぶした)と同じ「ラベルと中身が食い違う」事故になる。
    """
    import glob
    import os

    cands = _EARN_CANDS if kind == "earnings" else _PICKS_CANDS
    hits: list[str] = []
    for c in cands:
        if os.path.isfile(c):
            hits.append(c)
    if kind == "earnings":
        for g in _EARN_GLOBS:
            for f in sorted(glob.glob(g)):
                if f not in hits and os.path.getsize(f) > 0:
                    hits.append(f)
    if not hits:
        return None
    if len(hits) > 1:
        print(f"[自動検出] {kind} の候補 {len(hits)}件: " + " / ".join(hits[:6]))
        print(f"           -> **{hits[0]}** を使います "
              f"(別のを使うなら --{kind} で指定)")
    else:
        print(f"[自動検出] {kind} = {hits[0]}")
    return hits[0]


def _pick_col(df: pd.DataFrame, cands: tuple) -> str | None:
    for c in cands:
        if c in df.columns:
            return c
    low = {str(c).lower(): c for c in df.columns}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    return None


def _norm_code(s: pd.Series) -> pd.Series:
    """銘柄コードを 4桁の文字列に揃える。

    ⛔ J-Quants は 5桁(末尾0)、picks は '7203.T' 形式。
      §18.54 の `_jq_to_yf` と同じ変換を逆向きに掛ける。
    """
    t = s.astype(str).str.strip().str.upper()
    t = t.str.replace(r"\.T$", "", regex=True)
    # 5桁で末尾が 0 なら J-Quants 形式 -> 先頭4桁
    t = t.where(~(t.str.len() == 5) | ~t.str.endswith("0"), t.str[:4])
    return t


def _mde_yen(n1: float, n2: float, px_yen: float) -> float:
    """2群に割ったときの検出下限(円/件)。"""
    if n1 <= 0 or n2 <= 0:
        return float("inf")
    neff = 1.0 / (1.0 / n1 + 1.0 / n2)
    mde_bp = _BASE_MDE_BP * math.sqrt(_BASE_N / neff)
    return mde_bp * px_yen / 10000.0


def _cluster_t(df: pd.DataFrame, flag: str) -> tuple[float, float]:
    """日クラスタ頑健な「フラグあり − なし」の差と t。

    ⛔ N は同日決済なので、下げた日は全銘柄がまとめて勝つ(§18.13)。
      件数で t を作ると実効サンプルを誤認する。日単位で束ねる。
    """
    d = df[["date", "pnl", flag]].dropna()
    if d[flag].nunique() < 2:
        return float("nan"), float("nan")
    x = d[flag].to_numpy(float)
    y = d["pnl"].to_numpy(float)
    xm = x.mean()
    xd = x - xm
    sxx = float((xd * xd).sum())
    if sxx <= 0:
        return float("nan"), float("nan")
    beta = float((xd * (y - y.mean())).sum() / sxx)
    e = y - y.mean() - beta * xd
    # V = Σ_d (Σ_i x_i e_i)^2 / (Σ x²)²
    g = pd.DataFrame({"d": d["date"].to_numpy(), "v": xd * e})
    s = g.groupby("d")["v"].sum().to_numpy(float)
    var = float((s * s).sum()) / (sxx ** 2)
    se = math.sqrt(var) if var > 0 else float("nan")
    return beta, (beta / se if se and se == se and se > 0 else float("nan"))


def _exclude_sim(df: pd.DataFrame, mask_drop: np.ndarray, seeds: int,
                 rng: np.random.Generator) -> dict:
    """除外シミュレーション。⛔ 必ず「同数ランダム除外」の帯と比べる(§18.24)。

    ★ N は稼働率40%で枠が余っているので、抜いた枠は埋まらない。
      だから除外後の総額 = 全体 − 抜いた集団の絶対損益。
      件数を減らせば裾は必ず軽くなるので、**ランダム帯の外**に
      出ていなければ「その基準で選んだから」ではない。
    """
    pnl = df["pnl"].to_numpy(float)
    n_drop = int(mask_drop.sum())
    total_all = float(pnl.sum())
    total_ex = float(pnl[~mask_drop].sum())

    # 日次系列(CVaR・最悪日用)
    def _daily(m: np.ndarray) -> np.ndarray:
        s = pd.Series(pnl[~m], index=df["date"].to_numpy()[~m])
        return s.groupby(level=0).sum().to_numpy(float)

    d_ex = _daily(mask_drop)
    d_all = _daily(np.zeros(len(df), bool))

    rnd = []
    idx = np.arange(len(df))
    for _ in range(seeds):
        m = np.zeros(len(df), bool)
        m[rng.choice(idx, size=n_drop, replace=False)] = True
        rnd.append(float(pnl[~m].sum()))
    rnd = np.array(rnd)
    z = ((total_ex - rnd.mean()) / rnd.std(ddof=1)) if rnd.std(ddof=1) > 0 else float("nan")

    def _cvar(a: np.ndarray, q: float = 5.0) -> float:
        if len(a) == 0:
            return float("nan")
        thr = np.percentile(a, q)
        tail = a[a <= thr]
        return float(tail.mean()) if len(tail) else float("nan")

    return {
        "n_drop": n_drop,
        "dropped_pnl": float(pnl[mask_drop].sum()),
        "total_all": total_all,
        "total_ex": total_ex,
        "rnd_mean": float(rnd.mean()),
        "rnd_sd": float(rnd.std(ddof=1)),
        "z": float(z),
        "cvar_all": _cvar(d_all),
        "cvar_ex": _cvar(d_ex),
        "worst_all": float(d_all.min()) if len(d_all) else float("nan"),
        "worst_ex": float(d_ex.min()) if len(d_ex) else float("nan"),
    }


def _load(picks: str, earnings: str | None, data_since: str,
          lag_days: tuple[int, ...]) -> tuple[pd.DataFrame, list[str]]:
    p = pd.read_csv(picks)
    for c in ("date", "symbol", "pnl"):
        if c not in p.columns:
            sys.exit(f"[error] picks に列 '{c}' がありません: {list(p.columns)[:20]}")
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    p["code4"] = _norm_code(p["symbol"])
    p = p.sort_values("date").reset_index(drop=True)

    flags: list[str] = []
    if earnings is None:
        return p, flags

    try:
        e = pd.read_csv(earnings)
    except FileNotFoundError:
        sys.exit(
            f"\n[error] 決算CSV '{earnings}' がありません。\n"
            "\n"
            "  このファイルは **J-Quants から取ってくるもの** で、まだ存在しません。\n"
            "  必要な列(名前は自動で探します):\n"
            "      コード   Code / LocalCode / symbol\n"
            "      開示日   DisclosedDate / Date\n"
            "      開示時刻 DisclosedTime            (任意。寄り前/場中を分ける)\n"
            "\n"
            "  ★ 決算CSV が無くても、picks だけで **検出限界(MDE)の下見**が\n"
            "    できます。決算あり率ごとに何円/件まで測れるかが出ます:\n"
            f"        python analyze_earnings_flag.py --picks {picks} --scout\n")
    #   ⛔ まず「決算CSV ではない」ものを列名で弾く。Code と Date を持つ
    #     CSV は他にもたくさんある(信用残・マスタ・日足)。
    _bad = [(c, _NOT_EARNINGS[c]) for c in e.columns if c in _NOT_EARNINGS]
    if _bad:
        names = " / ".join(f"{c}({w})" for c, w in _bad[:4])
        sys.exit(
            f"\n⛔ '{earnings}' は決算CSV ではありません。\n"
            f"\n  決算にはあり得ない列があります: {names}\n"
            f"  列: {list(e.columns)[:12]}\n"
            "\n  --earnings で決算CSV を明示してください。\n"
            "  まだ無いなら、決算CSV 無しで下見だけできます:\n"
            "      python analyze_earnings_flag.py --no-earnings\n")

    cc = _pick_col(e, _CODE_COLS)
    dc = _pick_col(e, _DATE_COLS)
    if cc is None or dc is None:
        sys.exit(f"[error] 決算CSV にコード列/日付列が見つかりません: {list(e.columns)[:20]}")
    tc = _pick_col(e, _TIME_COLS)
    yc = _pick_col(e, _TYPE_COLS)

    e = e[[c for c in (cc, dc, tc, yc) if c]].copy()
    e.columns = ["code4", "edate"] + (["etime"] if tc else []) + (["etype"] if yc else [])
    e["code4"] = _norm_code(e["code4"])
    e["edate"] = pd.to_datetime(e["edate"], errors="coerce").dt.normalize()
    e = e.dropna(subset=["edate"]).drop_duplicates()

    #   ⛔⛔ **中身が決算か確かめる**。Code と Date の列を持つCSVは他にもある
    #     (信用残・銘柄マスタ・業種)。2026-09-22 に margin_interest_n_10y.csv を
    #     読んで、週次公表のカレンダーを「決算フラグ」として測る結果を出した。
    #     ★ 印字するだけでは防げなかった。**頻度で判定する**。
    _ns = e["code4"].nunique()
    _yr = max((e["edate"].max() - e["edate"].min()).days / 365.25, 0.5)
    _per = len(e) / max(_ns, 1) / _yr
    print(f"[検証] 決算CSV {len(e):,}行 / {_ns:,}銘柄 / {_yr:.1f}年"
          f" -> 1銘柄あたり **年 {_per:.1f}回**")
    if _per > _MAX_PER_SYM_YEAR:
        sys.exit(
            f"\n⛔ '{earnings}' は決算CSV ではありません。\n"
            f"\n  1銘柄あたり **年 {_per:.1f}回** の頻度があります。\n"
            "  決算は年4〜8回(本決算+四半期+業績修正)です。\n"
            "    年 約52回 -> 週次の信用残\n"
            "    年 約244回 -> 日足\n"
            "\n  --earnings で決算CSV を明示してください。\n"
            "  まだ無いなら、決算CSV 無しで下見だけできます:\n"
            "      python analyze_earnings_flag.py --no-earnings\n")
    if _per < 2.0:
        print(f"  ⚠ 年 {_per:.1f}回 は決算(年4〜8回)より**少ない**。"
              "本決算だけ / 一部銘柄だけの可能性があります")

    # ── フラグを作る ────────────────────────────────────────────
    #   ⛔ 「当日(D+1)の開示」は **時刻**で寄り前/場中を分ける。
    #     場中の開示は理由にはなるが 09:00 には分からない(回避不能)。
    ekeys = set(zip(e["code4"], e["edate"]))
    p["earn_d1"] = [(c, d) in ekeys for c, d in zip(p["code4"], p["date"])]
    flags.append("earn_d1")

    if "etime" in e.columns:
        pre = e[e["etime"].astype(str).str[:5].le("09:00")]
        pk = set(zip(pre["code4"], pre["edate"]))
        p["earn_d1_pre"] = [(c, d) in pk for c, d in zip(p["code4"], p["date"])]
        flags.append("earn_d1_pre")

    #   ★ 日付の整理 (ここを取り違えると寄り前フラグでなくなる)
    #       D    = シグナル日。前日比 +1.75% 以上 上げた日
    #       D+1  = **建てる日**。picks の `date` はこちら
    #
    #   ⛔ earn_prev{k} は **D+1 を含めない**。D+1 の開示は場中かもしれず
    #     09:00 には分からない(= 回避できない)。寄り前に確実に分かるのは
    #     **D の引けまで**。D+1 の開示は earn_d1 / earn_d1_pre で別に見る。
    #
    #     earn_prev1 = {D}            ← 最も典型的。D に決算 -> 上げる -> D+1 にギャップ
    #     earn_prev3 = {D-2, D-1, D}
    #     earn_prev5 = {D-4, ..., D}
    #
    #   ⛔ 2026-09-22: ここを range(lo, i+1) と書いて D+1 を含めていた。
    #     「寄り前に分かる」はずのフラグに場中の開示が混ざっていた。
    bdays = np.array(sorted(p["date"].unique()))
    pos = {d: i for i, d in enumerate(bdays)}
    for k in lag_days:
        col = f"earn_prev{k}"
        hit = []
        for c, d in zip(p["code4"], p["date"]):
            i = pos.get(d, None)
            if i is None:
                hit.append(False)
                continue
            lo = max(0, i - k)
            hit.append(any((c, bdays[j]) in ekeys for j in range(lo, i)))
        p[col] = hit
        flags.append(col)

    # ⛔ 契約期間より前は **欠測**。決算なしとして混ぜない
    cut = pd.Timestamp(data_since)
    n0 = len(p)
    p = p[p["date"] >= cut].reset_index(drop=True)
    if len(p) < n0:
        print(f"[欠測除外] {data_since} より前の {n0 - len(p):,}行 "
              f"({(n0 - len(p)) / n0:.0%}) を落としました")
        print("           ⛔ ここを『決算なし』として扱うと差が過大に出ます")
    return p, flags


def _report(p: pd.DataFrame, flags: list[str], scout: bool, seeds: int,
            rng: np.random.Generator) -> None:
    px_yen = float((p["entry_p"] * 100).median()) if "entry_p" in p.columns else 225_700.0
    print(f"\n  母集団 {len(p):,}件 / {p['date'].nunique():,}営業日 "
          f"/ {p['date'].min().date()}〜{p['date'].max().date()}")
    print(f"  建値中央 {px_yen / 100:,.0f}円 -> 1bp = {px_yen / 10000:.1f}円")
    print(f"  合計損益 {p['pnl'].sum():>+14,.0f}円  ({p['pnl'].mean():+,.0f}円/件)")

    # ── ① まず測れるか ────────────────────────────────────────
    print("\n" + "=" * 78)
    print(" ① 検出限界 (r と MDE)  ★ 止めない。②の差と並べて読むための物差し")
    print("=" * 78)
    print(f"  {'フラグ':<16}{'あり':>8}{'r':>7}{'MDE(円/件)':>12}   判定")
    print("  " + "-" * 72)
    #   ⛔ **MDE では止めない**(2026-09-22 に一度そう作って、力があるケースまで
    #     止まった)。§18.77 の作法は「MDE を **併記する**」であって
    #     「MDE で足切りする」ではない。観測した差と並べて読ませる。
    mdes: dict[str, float] = {}
    usable = []
    for f in flags:
        n1 = float(p[f].sum())
        n2 = float(len(p) - n1)
        r = n1 / len(p) if len(p) else 0.0
        mde = _mde_yen(n1, n2, px_yen)
        mdes[f] = mde
        if n1 < 30 or n2 < 30:
            j = "⛔ 件数が足りず計算できない"
        else:
            usable.append(f)
            if mde > _MDE_NG:
                j = f"⚠ **検出限界が大きい** (エッジ {_EDGE_YEN:.0f}円と同じ桁)"
            elif mde > _MDE_WARN:
                j = "⚠ きわどい"
            else:
                j = "✅ 十分"
        print(f"  {f:<16}{n1:>8,.0f}{r:>7.1%}{mde:>12,.0f}   {j}")
    if not usable:
        print("\n  ⛔ どのフラグも件数が足りません。")
        return
    print("\n  ★ MDE より小さい差は『測れていない』のであって『効果がない』ではない。")
    print("     ②で観測した差と必ず並べて読むこと。")
    if scout:
        print("\n  (--scout なので損益は見ません)")
        return

    # ── ② 絶対損益 ───────────────────────────────────────────
    print("\n" + "=" * 78)
    print(" ② 絶対円損益  ★ これが判定の本体")
    print("=" * 78)
    print("  ⛔ 『他より悪い』では足りない。N は稼働率40%で空いた枠を埋める")
    print("     代わりがいないので、抜いた集団がプラスなら抜くと損する(§18.64)\n")
    print(f"  {'フラグ':<16}{'件数':>7}{'合計円':>13}{'円/件':>9}"
          f"{'bp/件':>8}{'gap平均':>9}{'日t':>7}")
    print("  " + "-" * 72)
    for f in usable:
        g = p[p[f]]
        h = p[~p[f]]
        bp = (g["pnl"] / (g["entry_p"] * 100) * 10000).mean() if "entry_p" in p.columns else float("nan")
        gap = g["gap_bp"].mean() if "gap_bp" in p.columns else float("nan")
        _, t = _cluster_t(p, f)
        print(f"  {f:<16}{len(g):>7,}{g['pnl'].sum():>+13,.0f}{g['pnl'].mean():>+9,.0f}"
              f"{bp:>8.1f}{gap:>9.0f}{t:>7.2f}")
        print(f"  {'  (なし)':<16}{len(h):>7,}{h['pnl'].sum():>+13,.0f}{h['pnl'].mean():>+9,.0f}")
        #   ★ 観測した差と MDE を必ず並べる。MDE 未満なら「測れていない」
        diff = float(g["pnl"].mean() - h["pnl"].mean())
        mde = mdes.get(f, float("nan"))
        mark = ("✅ MDE を超えている" if abs(diff) >= mde
                else f"⚠ **MDE({mde:,.0f}円)未満 = 測れていない**")
        print(f"  {'  差(あり−なし)':<16}{'':>7}{'':>13}{diff:>+9,.0f}   {mark}\n")

    # ── ③ 除外シミュレーション ────────────────────────────────
    print("\n" + "=" * 78)
    print(" ③ 除外したら総円損益は改善するか  vs 同数ランダム除外の帯")
    print("=" * 78)
    print(f"  帯 {seeds}本。z>0 = ランダムより良い / z<0 = ランダムより悪い")
    print(f"  ⛔ |z| < {_PASS_Z} は『件数が減っただけ』であって基準の手柄ではない\n")
    for f in usable:
        m = p[f].to_numpy(bool)
        if m.sum() == 0 or m.sum() >= len(p):
            continue
        r = _exclude_sim(p, m, seeds, rng)
        ok_abs = r["dropped_pnl"] < 0
        ok_z = r["z"] >= _PASS_Z
        print(f"  ● {f}")
        print(f"      抜く {r['n_drop']:,}件 / その集団の絶対損益 "
              f"{r['dropped_pnl']:>+12,.0f}円  "
              + ("✅ マイナス" if ok_abs else "⛔ **プラス** = 抜くと損する"))
        print(f"      総額  {r['total_all']:>+12,.0f} -> {r['total_ex']:>+12,.0f}"
              f"  (ランダム帯 {r['rnd_mean']:>+12,.0f} ± {r['rnd_sd']:,.0f})")
        print(f"      z = {r['z']:>+6.2f}   "
              + ("✅ 帯の外(良い側)" if ok_z else
                 ("⛔ 帯の外(悪い側)" if r["z"] <= -_PASS_Z else "— 帯の中 = 測れていない")))
        print(f"      日次CVaR5% {r['cvar_all']:>+11,.0f} -> {r['cvar_ex']:>+11,.0f}"
              f"   最悪日 {r['worst_all']:>+11,.0f} -> {r['worst_ex']:>+11,.0f}")
        if abs(r["worst_ex"] - r["worst_all"]) < 1.0:
            print("      ⚠ **最悪日が1円も動いていない** = その日に対象が0件。")
            print("         §18.64/§18.65/§18.77 で3回同じことが起きています")
        print()

    print("=" * 78)
    print(" ★ 合格の条件 (回す前に固定。結果を見て動かさない)")
    print("=" * 78)
    print("   1. 抜く集団の **絶対円損益がマイナス**")
    print(f"   2. 除外後の総額が **ランダム帯の外(z >= +{_PASS_Z})**")
    print(f"   3. 日クラスタ t が **|t| >= {_PASS_T}**")
    print("   3つ揃ったときだけ候補。1つでも欠けたら不採用。")
    print("   ⛔ TRAIN で候補が出たときだけ TEST を1回使う。")


def _scout_only(p: pd.DataFrame, data_since: str, split: str | None) -> None:
    """決算CSV が **まだ無い** ときの下見。

    ★ 答えるのは1つ: 「決算あり率が r のとき、何円/件の差まで測れるか」
      これが分かると、Codex が持ってくる分類のうち **どれが最初から
      測れないか** が、損益を1円も見ずに決まる。
    """
    px_yen = float((p["entry_p"] * 100).median()) if "entry_p" in p.columns else 225_700.0
    cut = pd.Timestamp(data_since)
    keep = p[p["date"] >= cut]

    print("=" * 78)
    print(" 下見 — 決算CSV が無いので、検出限界だけ出します")
    print("=" * 78)
    print(f"\n  picks {len(p):,}件 / {p['date'].nunique():,}営業日 "
          f"/ {p['date'].min().date()}〜{p['date'].max().date()}")
    print(f"  建値中央 {px_yen / 100:,.0f}円 -> 1bp = {px_yen / 10000:.1f}円")
    miss = len(p) - len(keep)
    print(f"\n  ⛔ 決算データの契約開始 {data_since} より前: "
          f"**{miss:,}件 ({miss / len(p):.0%}) が欠測**")
    print("     ここは『決算なし』ではなく **落とす**。混ぜると差が過大に出ます")
    print(f"     残る母集団: {len(keep):,}件")

    for c in ("entry_p", "gap_bp", "pnl", "side"):
        if c not in p.columns:
            print(f"  ⚠ picks に '{c}' がありません(あると精度が上がります)")

    windows = [("全体", keep)]
    if split:
        s = pd.Timestamp(split)
        windows = [("TRAIN", keep[keep["date"] < s]), ("TEST", keep[keep["date"] >= s])]

    for wname, w in windows:
        if len(w) < 100:
            print(f"\n  [{wname}] {len(w):,}件 — 少なすぎて計算できません")
            continue
        print(f"\n  [{wname}] {len(w):,}件 / {w['date'].nunique():,}営業日")
        print(f"  {'決算あり率 r':>12}{'あり':>9}{'なし':>9}"
              f"{'MDE(円/件)':>12}   判定")
        print("  " + "-" * 66)
        for r in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40):
            n1 = len(w) * r
            n2 = len(w) - n1
            mde = _mde_yen(n1, n2, px_yen)
            if mde > _MDE_NG:
                j = f"⛔ N のエッジ({_EDGE_YEN:.0f}円)と同じ桁。区別できない"
            elif mde > _MDE_WARN:
                j = "⚠ きわどい"
            else:
                j = "✅ 十分"
            print(f"  {r:>12.0%}{n1:>9,.0f}{n2:>9,.0f}{mde:>12,.0f}   {j}")

    print("\n" + "=" * 78)
    print(" ★ この表の読み方")
    print("=" * 78)
    print("   「当日が決算発表予定日」は年4回なので **r が数%**。")
    print("   その分類は、決算CSV を取ってきても **最初から測れません**。")
    print("   「過去5営業日の決算」なら r が高いので測れます。")
    print("   -> Codex に取得を頼むときは、**まず r の大きい分類から**。")
    print("\n   決算CSV ができたら:")
    print("     python analyze_earnings_flag.py --picks <picks> --earnings <決算CSV> --scout")


def _test_date_boundary() -> None:
    """フラグの日付境界を手で確かめる。

    ⛔ 2026-09-22 に range(lo, i+1) と書いて **建てる日(D+1)を含めて**
      いた。「寄り前に分かる」はずの earn_prev に場中の開示が混ざる。
      同じ取り違えを二度としないための回帰テスト。
    """
    import os
    import tempfile

    bd = pd.bdate_range("2020-01-06", periods=8)
    picks = pd.DataFrame({
        "date": bd, "symbol": ["7203.T"] * len(bd),
        "entry_p": [2000.0] * len(bd), "gap_bp": [200.0] * len(bd),
        "pnl": [0.0] * len(bd),
    })
    # 開示は 1件だけ。bd[3] に置く
    earn = pd.DataFrame({"Code": ["72030"], "DisclosedDate": [bd[3].date()]})

    d = tempfile.mkdtemp()
    fp, fe = os.path.join(d, "p.csv"), os.path.join(d, "e.csv")
    picks.to_csv(fp, index=False)
    earn.to_csv(fe, index=False)
    out, _ = _load(fp, fe, "2000-01-01", (1, 3, 5))
    got = out.set_index("date")

    # bd[3] に開示 -> その日に建てる行は earn_d1、翌営業日 bd[4] は earn_prev1
    exp = [
        ("earn_d1",    bd[3], True,  "開示日そのもの = 建てる日(D+1)に開示"),
        ("earn_d1",    bd[4], False, "翌日は当日開示ではない"),
        ("earn_prev1", bd[4], True,  "bd[4] から見て D=bd[3] に開示"),
        ("earn_prev1", bd[3], False, "⛔ 建てる日(D+1)を含めてはいけない"),
        ("earn_prev1", bd[5], False, "2営業日前は prev1 に入らない"),
        ("earn_prev3", bd[5], True,  "bd[5] の D-1 = bd[3]"),
        ("earn_prev3", bd[7], False, "4営業日前は prev3 に入らない"),
        ("earn_prev5", bd[7], True,  "bd[7] の D-3 = bd[3]"),
    ]
    ng = 0
    print("[selftest] 日付境界")
    for col, day, want, why in exp:
        have = bool(got.loc[day, col])
        ok = have == want
        ng += (not ok)
        print(f"  {'✅' if ok else '⛔'} {col:<11} {day.date()} "
              f"= {str(have):<5} (期待 {want})  {why}")
    if ng:
        sys.exit(f"\n⛔ 日付境界のテストが {ng}件 失敗しました")
    print("  -> 8件すべて一致\n")


def _selftest() -> None:
    """合成データで、力がある場合/ない場合の両方を確かめる。

    ⛔ §18.77 の反省: 下地を1回だけ引いて、効果だけ差し替える。
      ケースごとに乱数を引き直すと母集団そのものが別物になる。
    """
    _test_date_boundary()
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2017-01-04", "2020-08-31")
    rows = []
    for d in days:
        if rng.random() < 0.25:
            continue
        n = int(rng.integers(4, 16))
        shock = rng.normal(0, 60_000 / max(n, 1))       # 同日相関
        for _ in range(n):
            px = float(rng.uniform(1000, 6000))
            rows.append({"date": d, "code4": f"{rng.integers(1300, 9999)}",
                         "entry_p": px, "gap_bp": float(rng.uniform(100, 500)),
                         "shock": shock, "eps": rng.normal(0, 0.026)})
    base = pd.DataFrame(rows)
    base["symbol"] = base["code4"] + ".T"
    flag = rng.random(len(base)) < 0.25                  # 決算あり 25%
    base["earn"] = flag
    print(f"[selftest] 下地 {len(base):,}件 / {base['date'].nunique():,}営業日 "
          f"/ 決算あり {flag.mean():.0%}")

    for name, eff_bp in (("力あり(決算 -120bp)", -120.0), ("力なし(効果ゼロ)", 0.0)):
        mu = np.where(flag, eff_bp, 0.0) / 10000.0 + 0.0015
        pnl = (base["shock"].to_numpy() +
               (mu + base["eps"].to_numpy()) * base["entry_p"].to_numpy() * 100)
        p = base[["date", "symbol", "entry_p", "gap_bp"]].copy()
        p["pnl"] = pnl
        p["earn_d1"] = flag
        print("\n" + "#" * 78)
        print(f"# {name}")
        print("#" * 78)
        _report(p, ["earn_d1"], scout=False, seeds=100,
                rng=np.random.default_rng(1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--picks", help="analyze_gap_edge --dump-picks の出力。"
                                    "省略すると n_picks.csv などを自動で探す")
    ap.add_argument("--earnings", help="J-Quants の決算CSV。"
                                       "省略すると jquants_extra/ などを自動で探す")
    ap.add_argument("--no-earnings", action="store_true",
                    help="決算CSV を探さない。検出限界の下見だけ出す")
    ap.add_argument("--data-since", default=_DATA_SINCE,
                    help=f"決算データの契約開始日。これより前は **欠測として落とす** (既定 {_DATA_SINCE})")
    ap.add_argument("--lag-days", default="1,3,5",
                    help="過去N営業日以内の決算フラグを作る (既定 1,3,5)")
    ap.add_argument("--split", default="2020-09-01",
                    help="TRAIN/TEST の境目 (既定 2020-09-01 = §18.54 で固定した値)")
    ap.add_argument("--win", choices=("TRAIN", "TEST", "ALL"), default="TRAIN",
                    help="どちらの窓で測るか (既定 TRAIN)")
    ap.add_argument("--confirm", action="store_true",
                    help="TEST を使うことを明示。⛔ 1候補につき1回きり")
    ap.add_argument("--scout", action="store_true",
                    help="r と MDE だけ出して損益を見ない。**最初はこれ**")
    ap.add_argument("--seeds", type=int, default=_SEEDS, help="ランダム帯の本数")
    ap.add_argument("--selftest", action="store_true", help="合成データで自己検証")
    a = ap.parse_args()

    if a.selftest:
        _selftest()
        return

    #   ★ ファイル名を覚えなくていいように、省略されたら自動で探す
    if not a.picks:
        a.picks = _autofind("picks")
    if not a.picks:
        sys.exit(
            "\n[error] picks CSV が見つかりません。\n"
            "\n  探した場所: " + " / ".join(_PICKS_CANDS) + "\n"
            "\n  作り方(1回だけ。数分かかります):\n"
            "    python analyze_gap_edge.py --days 4200 --min-gap-bp 100 "
            "--split 2020-09-01 --min-ret1 1.753 --min-price 1000 "
            "--max-price 6000 --dump-picks n_picks.csv\n")
    if not a.earnings and not a.no_earnings:
        a.earnings = _autofind("earnings")
    if a.no_earnings:
        a.earnings = None

    lags = tuple(int(x) for x in a.lag_days.split(",") if x.strip())
    p, flags = _load(a.picks, a.earnings, a.data_since, lags)
    if not flags:
        #   ★ 決算CSV がまだ無くても、検出限界の下見はできる
        _scout_only(p, a.data_since, a.split)
        return

    if a.split:
        cut = pd.Timestamp(a.split)
        if a.win == "TRAIN":
            p = p[p["date"] < cut]
        elif a.win == "TEST":
            if not a.confirm:
                sys.exit("[error] TEST を使うなら --confirm を付けてください。\n"
                         "        ⛔ 1候補につき1回きりです(§18.54)")
            p = p[p["date"] >= cut]
        print(f"\n[窓] {a.win}  ({a.split} で分割)")
    elif a.win == "TEST":
        sys.exit("[error] --split が要ります")

    print("=" * 78)
    print(" 決算フラグは N の選別軸になるか")
    print("=" * 78)
    _report(p.reset_index(drop=True), flags, a.scout, a.seeds,
            np.random.default_rng(42))


if __name__ == "__main__":
    main()
