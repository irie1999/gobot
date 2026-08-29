#!/usr/bin/env python3
"""
gap_reversal_intraday.py — ギャップ反転を「実際に入れる価格」で検証する。

gap_reversal_daily.py は 始値→終値 で成績を測っていたが、これは執行できない。
残差ギャップは **当日の始値と指数の始値を見て初めて計算できる**ので、
09:00 の板寄せで始値が付いた後に判定するなら、実際の約定はその次の
売買価格である。事前に条件付きの寄成注文を置ける注文仕様が無い限り、
日足の 始値→引け 成績をそのまま執行可能な成績と見なすことはできない。

このスクリプトは分足で以下を測る。

  1. 「最初に存在する実取引の価格」で入った場合の成績
  2. 1分 / 5分 / 10分 / 30分 遅延での劣化
  3. 資金を、入る時点の出来高の一定割合以下に抑えた場合の成績
  4. 買い側 / 空売り側 を完全に分離した成績

⛔ ホールドアウト
    分足があるのは 2024-07 以降。この期間は **ルールを一切触らない最終
    ホールドアウト**として扱う。閾値・ユニバース・残差化の設計は日足側で
    確定させたものを凍結して持ち込み、ここでは執行モデルだけを検証する。
    ここで閾値を動かした瞬間、ホールドアウトではなくなる。

使い方:
    python gap_reversal_intraday.py --self-test
    python gap_reversal_intraday.py                       # 自動検出
    python gap_reversal_intraday.py --interval 5m
    python gap_reversal_intraday.py --minute-dir path/to/dir
    python gap_reversal_intraday.py --max-vol-share 0.05  # 出来高の5%まで
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import gap_reversal_daily as gd

# ── 凍結されたルール (ホールドアウトで動かさないこと) ──────────────
FROZEN_PREV_THR = 0.0
FROZEN_GAP_THR = 3.0

LAGS = [0, 1, 5, 10, 30]          # 最初のバーから何分後に入るか
MINUTE_START = pd.Timestamp("2024-07-01")
SPLIT_TOL = 0.30                  # 分足と日足の終値がこれ以上ずれたら分割未調整


# ══════════════════════════════════════════════════════════════════
# 分足の読み込み
# ══════════════════════════════════════════════════════════════════
def yf_to_jquants(sym: str) -> str:
    """7203.T -> 72030 (J-Quants の5桁コード)。"""
    base = sym.split(".")[0]
    return f"{base}0" if len(base) == 4 else base


def resolve_minute_dir(interval: str, explicit: str | None) -> Path | None:
    """分足の置き場を解決する。1分足と5分足で仕組みが別。"""
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None
    if interval == "1m":
        cands = [os.environ.get("MINUTE_1M_DIR"),
                 str(Path.home() / ".jquants_cache" / "minute")]
    else:
        cands = [os.environ.get("MINUTE_5M_DIR"), "data/minute_5m", "stock_5min",
                 "../stock_5min"]
    for c in cands:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def normalize_minute_df(df: pd.DataFrame) -> pd.DataFrame | None:
    """index = tz-naive DatetimeIndex (JST)、列 = 小文字 ohlcv に揃える。"""
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated(keep="first")]
    for c in ("datetime", "date", "time", "timestamp"):
        if c in df.columns:
            df = df.set_index(pd.to_datetime(df[c])).drop(columns=[c])
            break
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    need = ["open", "high", "low", "close"]
    if any(c not in df.columns for c in need):
        return None
    if "volume" not in df.columns:
        df["volume"] = np.nan
    out = df[need + ["volume"]].apply(pd.to_numeric, errors="coerce")
    out = out[out.index.notna()].dropna(subset=need).sort_index()
    return out[(out[need] > 0).all(axis=1)]


def load_minute(sym: str, mdir: Path, interval: str) -> pd.DataFrame | None:
    """1銘柄ぶんの分足。ファイル名は J-Quants の5桁コード。"""
    j = yf_to_jquants(sym)
    names = [f"{j}_1m.pkl", f"{j}.pkl"] if interval == "1m" else [f"{j}.pkl"]
    for n in names:
        p = mdir / n
        if not p.exists():
            continue
        try:
            with open(p, "rb") as f:
                raw = pickle.load(f)
        except Exception:
            return None
        return normalize_minute_df(raw) if isinstance(raw, pd.DataFrame) else None
    return None


def day_bars(mdf: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """その日のバーだけ切り出す。先頭が 09:00 とは限らない点に注意。"""
    d = pd.Timestamp(day).normalize()
    return mdf[(mdf.index >= d) & (mdf.index < d + pd.Timedelta(days=1))]


# ══════════════════════════════════════════════════════════════════
# 執行シミュレーション
# ══════════════════════════════════════════════════════════════════
def simulate_one(bars: pd.DataFrame, daily_close: float, lags: list[int],
                 interval: str) -> dict | None:
    """1銘柄日の執行価格を返す。

    - 分足と日足の終値が SPLIT_TOL 以上ずれていたら分割未調整として捨てる
      (5分足は分割を遡及調整しないため、分割前の日が日足の2〜15倍になる)
    価格の階段 (ここが本質):
      - `auction`  = 最初のバーの始値 = 09:00 の板寄せの約定価格。
                     残差ギャップはこの価格から計算するので、**これで建てる
                     ことはできない**。参考値としてのみ出す。
      - `entry_fc` = 最初のバーの終値。板寄せが終わって条件が判定できた後、
                     **実際に入れる最速の価格**。
      - `entry_N`  = 最初のバーから N 分後のバーの始値。
      - 決済は最終バーの終値 (引け)
    """
    if len(bars) < 3:
        return None
    med = float(bars["close"].median())
    if not np.isfinite(med) or med <= 0:
        return None
    if abs(med / daily_close - 1.0) > SPLIT_TOL:
        return {"contaminated": True}

    step = 1 if interval == "1m" else 5
    first_t = bars.index[0]
    exit_p = float(bars["close"].iloc[-1])

    fv = float(bars["volume"].iloc[0])
    # 日中に約定が1本しかない = ストップで張り付き、引けの板寄せだけで成立。
    # 始値=終値なので、始値で建てて引けで閉じることが原理的にできない。
    single = len(bars) == 1
    out: dict = {
        "contaminated": False,
        "first_bar_time": first_t,
        "exit_p": exit_p,
        "n_bars": len(bars),
        "single_print": single,
        # 板寄せの約定価格。条件判定に使う価格なので、これでは建てられない
        "auction": float(bars["open"].iloc[0]),
        # 板寄せ後、実際に入れる最速の価格
        "entry_fc": float(bars["close"].iloc[0]),
        "vol_fc": fv if np.isfinite(fv) else np.nan,
        "first_vol": fv if np.isfinite(fv) else np.nan,
    }
    for lag in lags:
        if lag == 0:
            continue
        k = lag // step
        if k < len(bars):
            out[f"entry_{lag}"] = float(bars["open"].iloc[k])
            v = bars["volume"].iloc[:max(k, 1)].sum()
            out[f"vol_{lag}"] = float(v) if np.isfinite(v) else np.nan
        else:
            out[f"entry_{lag}"] = np.nan
            out[f"vol_{lag}"] = np.nan
    return out


def dump_raw_bars(sig: pd.DataFrame, mdir: Path, interval: str, n: int) -> None:
    """ギャップの大きい銘柄日について、生の分足を無加工で表示する。

    幻バー (出来高0 / OHLC同値) の除去も、時刻の丸めも一切しない。
    「寄りが遅れている」のが実在なのか処理の産物なのかは、これを見れば分かる。

      - 09:00 から出来高 0 のバーが並び、途中から出来高が付く
          → 気配のプレースホルダ。除去すると『遅延』に見える
      - 09:00 台にバーが存在せず、途中から始まる
          → 板寄せが実際に遅れて成立している (実在)
    """
    top = sig.reindex(sig["gap"].abs().sort_values(ascending=False).index).head(n)
    print("\n" + "=" * 78)
    print(f"生の分足ダンプ (|ギャップ| 上位 {len(top)} 銘柄日 / 無加工)")
    print("=" * 78)
    for _, r in top.iterrows():
        mdf = load_minute(r["symbol"], mdir, interval)
        if mdf is None:
            print(f"\n{r['symbol']} {r['date']:%Y-%m-%d}: 分足ファイルなし")
            continue
        bars = day_bars(mdf, r["date"])
        print(f"\n{r['symbol']}  {r['date']:%Y-%m-%d}  "
              f"残差ギャップ {r['gap']*100:+.2f}%  "
              f"日足 始値 {r['open_day']:,.1f} / 終値 {r['close_day']:,.1f}")
        if bars.empty:
            print("  当日のバーなし")
            continue
        print(f"  バー総数 {len(bars)}  先頭 {bars.index[0]:%H:%M}  "
              f"末尾 {bars.index[-1]:%H:%M}")
        print(f"    {'時刻':<8}{'始値':>10}{'高値':>10}{'安値':>10}"
              f"{'終値':>10}{'出来高':>12}   印")
        for t, b in bars.head(10).iterrows():
            mark = []
            if not (b["volume"] > 0):
                mark.append("出来高0")
            if b["open"] == b["high"] == b["low"] == b["close"]:
                mark.append("OHLC同値")
            if b["open"] == r["open_day"]:
                mark.append("始値=日足始値")
            print(f"    {t:%H:%M}   {b['open']:>10,.1f}{b['high']:>10,.1f}"
                  f"{b['low']:>10,.1f}{b['close']:>10,.1f}{b['volume']:>12,.0f}"
                  f"   {' '.join(mark)}")
    print("=" * 78)


def phantom_diagnosis(ex: pd.DataFrame) -> pd.DataFrame:
    """先頭バーの素性を検査し、執行可能な部分集合を返す。

    - 幻バー (出来高0のプレースホルダ) を掴んでいないか
    - 寄りの遅れが実在するか。**自分の生バーから測る**。他人の delay 列は使わない
    - 単一プリント日 (ストップ張り付き) を分離する。日中の取引が存在しないので
      始値で建てて引けで閉じることが原理的にできない
    """
    if ex.empty or "first_vol" not in ex:
        return ex
    print("\n【0】先頭バーの検査")
    v = ex["first_vol"]
    zero = (v.fillna(0) <= 0).mean()
    same = (ex["auction"] == ex["open_day"]).mean()
    print(f"  先頭バーの出来高が 0 または欠損 : {zero*100:>5.1f}%")
    print(f"  先頭バーの始値が日足始値と一致  : {same*100:>5.1f}%")
    if zero > 0.05:
        print("  ⚠ 出来高0の先頭バーがあります。板寄せ前の気配プレースホルダを")
        print("    掴んでいる可能性があり、そのバーの終値は約定価格ではありません。")
    if same < 0.90:
        print("  ⚠ 先頭バーの始値が日足始値と合いません (分割の未調整など)。")

    # 単一プリント日
    sp = ex["single_print"] if "single_print" in ex else pd.Series(False, index=ex.index)
    print(f"\n  日中の約定が1本だけ (ストップ張り付き) : {int(sp.sum()):,} / "
          f"{len(ex):,} 件 ({sp.mean()*100:.1f}%)")
    if sp.any():
        g = ex[sp]
        print(f"    そのうち 始値=終値 : {int((g['auction'] == g['exit_p']).sum()):,} 件")
        print(f"    平均 |ギャップ| : {g['gap'].abs().mean()*100:.1f}% "
              f"(全体 {ex['gap'].abs().mean()*100:.1f}%)")
        print("    → 日中の取引が存在しないので執行不能。以降の集計から除きます。")

    # 寄り時刻 (09:00 からの遅れ) を生バーから測る
    dly = ((ex["first_bar_time"] - ex["first_bar_time"].dt.normalize()
            - pd.Timedelta(hours=9)).dt.total_seconds() / 60).clip(lower=0)
    ex = ex.assign(delay_min=dly)
    live = ex[~sp]
    print(f"\n  寄りの遅れ (生バーの先頭時刻から。単一プリント日を除く {len(live):,} 件)")
    if len(live) >= 20:
        q = live["delay_min"].quantile([0, .25, .5, .75, .9, 1.0])
        print(f"    最小 {q[0]:.0f}分 / 25% {q[.25]:.0f} / 中央 {q[.5]:.0f} / "
              f"75% {q[.75]:.0f} / 90% {q[.9]:.0f} / 最大 {q[1.0]:.0f}")
        print(f"    09:00 ちょうどに寄った: {int((live['delay_min'] <= 0).sum()):,} 件 "
              f"({(live['delay_min'] <= 0).mean()*100:.1f}%)")
        try:
            qq = pd.qcut(live["gap"].abs(), 5, duplicates="drop")
            tab = live.groupby(qq, observed=True).apply(
                lambda x: pd.Series({
                    "件数": len(x),
                    "中央gap%": x["gap"].abs().median() * 100,
                    "遅れ0の件数": int((x["delay_min"] <= 0).sum()),
                    "中央の遅れ分": x["delay_min"].median()}),
                include_groups=False)
            print("\n    ギャップ分位 × 寄りの遅れ (中間分位だけ 0 件なら処理の産物)")
            print("      " + tab.to_string().replace("\n", "\n      "))
            c = np.corrcoef(live["gap"].abs(), live["delay_min"])[0, 1]
            print(f"      corr(|ギャップ|, 遅れ) = {c:+.3f}  "
                  f"(特別気配が理由なら正のはず)")
        except Exception as e:
            print(f"    分位別の集計に失敗: {e}")
    return ex[~sp].copy()


def build_exec_table(sig: pd.DataFrame, mdir: Path, interval: str,
                     lags: list[int], workers: int) -> pd.DataFrame:
    """シグナル1件ごとに、分足から見た執行価格を付ける。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    by_sym: dict[str, pd.DataFrame] = {}
    for s, g in sig.groupby("symbol"):
        by_sym[s] = g

    rows: list[dict] = []
    stat = {"no_file": 0, "no_day": 0, "contaminated": 0, "ok": 0}

    def work(symbol: str, g: pd.DataFrame):
        mdf = load_minute(symbol, mdir, interval)
        if mdf is None or mdf.empty:
            return ("no_file", len(g), [])
        got, miss, cont = [], 0, 0
        for _, r in g.iterrows():
            bars = day_bars(mdf, r["date"])
            res = simulate_one(bars, float(r["close_day"]), lags, interval)
            if res is None:
                miss += 1
                continue
            if res["contaminated"]:
                cont += 1
                continue
            got.append({**r.to_dict(), **res})
        return ("ok", (miss, cont), got)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, s, g): s for s, g in by_sym.items()}
        for fu in as_completed(futs):
            kind, extra, got = fu.result()
            if kind == "no_file":
                stat["no_file"] += extra
                continue
            miss, cont = extra
            stat["no_day"] += miss
            stat["contaminated"] += cont
            stat["ok"] += len(got)
            rows.extend(got)

    print(f"分足の突き合わせ: 成立 {stat['ok']:,} / "
          f"分足ファイル無し {stat['no_file']:,} / "
          f"当日のバー無し {stat['no_day']:,} / "
          f"分割未調整で除外 {stat['contaminated']:,}")
    return pd.DataFrame(rows)


def exec_returns(ex: pd.DataFrame, lag, cost_bps: float) -> pd.Series:
    """指定の価格で入った場合の1トレードのリターン (方向込み・コスト後)。

    lag は分数、または "auction" / "fc" (最初のバーの終値)。
    """
    col = ("auction" if lag == "auction"
           else "entry_fc" if lag == "fc" else f"entry_{lag}")
    if col not in ex:
        return pd.Series(dtype=float)
    e = ex[col]
    ok = e.notna() & (e > 0)
    r = ex["side"] * (ex["exit_p"] / e - 1.0)
    return (r - cost_bps / 10000.0).where(ok)


def daily_from_trades(ex: pd.DataFrame, r: pd.Series,
                      weights: pd.Series | None = None) -> pd.Series:
    """日次ポートフォリオに集約する。weights を渡すと資金加重。"""
    df = pd.DataFrame({"date": ex["date"], "r": r})
    if weights is None:
        return df.dropna().groupby("date")["r"].mean().sort_index()
    df["w"] = weights
    df = df.dropna()
    g = df.groupby("date")
    return (g.apply(lambda x: (x["r"] * x["w"]).sum() / max(x["w"].sum(), 1e-12),
                    include_groups=False)).sort_index()


# ══════════════════════════════════════════════════════════════════
# レポート
# ══════════════════════════════════════════════════════════════════
def report(ex: pd.DataFrame, sig_all: pd.DataFrame, mkt: pd.Series,
           args) -> None:
    print("\n" + "=" * 78)
    print("執行可能性を織り込んだ検証  [ホールドアウト: 分足がある期間のみ]")
    print(f"ルールは凍結: 前日={FROZEN_PREV_THR} / ギャップ={FROZEN_GAP_THR} "
          f"/ コスト={args.cost_bps}bp / 足={args.interval}")
    print("=" * 78)
    if ex.empty:
        print("分足と突き合わせられたシグナルがありません。")
        return
    ex = phantom_diagnosis(ex)
    if ex.empty:
        print("執行可能な銘柄日が残りませんでした。")
        return
    print(f"期間 {ex['date'].min():%Y-%m-%d} 〜 {ex['date'].max():%Y-%m-%d}   "
          f"シグナル {len(ex):,} 件 / {ex['date'].nunique():,} 日")

    # ── §1 日足の始値 (執行不能) vs 実際に入れる価格 ─────────────
    print("\n【1】執行遅延による劣化")
    print("  日足の始値は、残差ギャップを計算するのに使った価格そのものです。")
    print("  その価格で建てることはできません (板寄せで始値が付いて初めて")
    print("  条件が判定できるため)。下の行が実際に入れる価格です。")
    base_r = ex["side"] * (ex["exit_p"] / ex["open_day"] - 1.0) - args.cost_bps / 1e4
    b = gd.stats(daily_from_trades(ex, base_r))
    print(gd._fmt("日足の始値 (執行不能)", b, width=30))
    r_auc = exec_returns(ex, "auction", args.cost_bps)
    print(gd._fmt("分足の板寄せ価格 (執行不能)",
                  gd.stats(daily_from_trades(ex, r_auc)), width=30))
    print("  ↑ ここまでは条件判定に使う価格。↓ ここからが実際に入れる価格。")
    # ⛔ 執行価格が板寄せ価格と一致していないかを検査する。
    # 分足の参照に失敗して公式始値に落ちる実装事故が起きやすく、そうなると
    # 「劣化ゼロ」という結果が出るが、それは執行不能な価格を測り直しただけ。
    ident = (ex["entry_fc"] == ex["auction"]).mean()
    id_open = (ex["entry_fc"] == ex["open_day"]).mean()
    if max(ident, id_open) > 0.05:
        print(f"  ⚠ 執行価格の {max(ident, id_open)*100:.1f}% が板寄せ価格と完全一致しています。")
        print("     分足の参照に失敗して始値へフォールバックしている可能性が高く、")
        print("     この場合『劣化ゼロ』は執行可能性の証拠になりません。")
    r_fc = exec_returns(ex, "fc", args.cost_bps)
    first = gd.stats(daily_from_trades(ex, r_fc))
    step_lbl = "1分" if args.interval == "1m" else "5分"
    print(gd._fmt(f"最初のバーの終値 (≈+{step_lbl})", first, width=30))
    for lag in args.lags:
        if lag == 0:
            continue
        r = exec_returns(ex, lag, args.cost_bps)
        print(gd._fmt(f"  +{lag}分", gd.stats(daily_from_trades(ex, r)), width=30))
    if first.get("n", 0) >= 10 and b.get("mean_bp"):
        keep = first["mean_bp"] / b["mean_bp"] * 100
        print(f"  → 日足の始値の成績のうち、実際に入れる最速の価格で残るのは "
              f"{keep:.0f}%")

    # ── §2 方向別 × 遅延 ─────────────────────────────────────────
    print("\n【2】方向別 (完全に分離)")
    for sd, name in ((1.0, "買い側 (ギャップダウンを買う)"),
                     (-1.0, "空売り側 (ギャップアップを売る)")):
        sub = ex[ex["side"] == sd]
        print(f"\n  {name}  ({len(sub):,}件)")
        if len(sub) < 20:
            print("    サンプル不足")
            continue
        for lag in ["auction", "fc"] + [x for x in args.lags if x != 0]:
            r = exec_returns(sub, lag, args.cost_bps)
            st = gd.stats(daily_from_trades(sub, r))
            label = ("板寄せ(執行不能)" if lag == "auction"
                     else "最初のバー終値" if lag == "fc" else f"+{lag}分")
            print(gd._fmt("    " + label, st, width=26))

    # ── §3 出来高制約 ────────────────────────────────────────────
    print("\n【3】資金を出来高の一定割合以下に抑えた場合")
    print(f"  入る時点までの出来高 × 割合 を、その銘柄に入れられる上限とします。")
    print(f"  資金 {args.capital/1e4:.0f}万円 をその日のシグナルに等分し、上限で切ります。")
    lag, e, volcol = "fc", ex["entry_fc"], "vol_fc"
    if volcol not in ex or ex[volcol].isna().all():
        print("  出来高が分足に無いため、この節は測れません。")
    else:
        r = exec_returns(ex, lag, args.cost_bps)
        per_day = ex.groupby("date")["symbol"].transform("size")
        want = args.capital / per_day.clip(lower=1)
        for share in args.vol_shares:
            cap_yen = ex[volcol] * e * share
            filled = np.minimum(want, cap_yen.fillna(0.0))
            d = daily_from_trades(ex, r, weights=filled)
            fill_rate = float((filled / want).clip(0, 1).mean())
            st = gd.stats(d)
            print(gd._fmt(f"出来高の {share*100:.0f}% まで", st, width=26)
                  + f"  約定率 {fill_rate*100:.0f}%")
        print("  約定率 = 建てたい金額のうち実際に建てられた割合の平均。")
        print("  これが低いなら、資金を使い切れないので年換算の額は目減りします。")

    # ── §4 同期間の日足ベース (比較用) ───────────────────────────
    print("\n【4】同じ期間の日足ベース成績 (探索バイアスの目安)")
    sub = sig_all[sig_all["date"] >= ex["date"].min()]
    if len(sub) >= 30:
        st = gd.stats(gd.to_daily(sub, "alpha", args.cost_bps, None))
        print(gd._fmt("  日足 α (ホールドアウト期間)", st, width=28))
        print("  日足の全期間 (2007-) の成績より大きく落ちるなら、閾値の探索が")
        print("  効いていた可能性があります。分足の数字と併せて見てください。")

    print("\n" + "=" * 78)
    print("判定: 『最初のバーの終値』の行がコスト後でプラスかつ t>2 で、")
    print("      出来高制約をかけても残るなら、初めて実弾の検討に進めます。")
    print("      ここで消えるなら、日足の成績は執行できない幻です。")
    print("=" * 78)


# ══════════════════════════════════════════════════════════════════
def self_test() -> int:
    """合成データで配管を確認する。ネットワーク不要。"""
    rng = np.random.default_rng(7)
    days = pd.bdate_range("2024-07-01", "2026-08-01")
    rows, bars_store = [], {}
    for k in range(30):
        sym = f"{1300+k}.T"
        frames = []
        for d in days:
            n = 60
            t = pd.date_range(d + pd.Timedelta(hours=9), periods=n, freq="1min")
            base = 2000.0
            # 寄りで 12% ギャップし、その後じわじわ戻る (= 本物の反転)
            drift = np.linspace(0.0, -0.006, n)
            px = base * (1 + drift + rng.normal(0, 0.0006, n))
            frames.append(pd.DataFrame(
                {"open": px, "high": px * 1.001, "low": px * 0.999,
                 "close": px, "volume": rng.integers(1000, 5000, n).astype(float)},
                index=t))
            rows.append({"date": d, "symbol": sym, "side": -1.0,
                         "open_day": base, "close_day": float(px[-1]),
                         "gap": 0.12, "turnover": 1e9})
        bars_store[sym] = pd.concat(frames)

    sig = pd.DataFrame(rows)
    tmp = Path("_selftest_minute")
    tmp.mkdir(exist_ok=True)
    for s, df in bars_store.items():
        with open(tmp / f"{yf_to_jquants(s)}_1m.pkl", "wb") as f:
            pickle.dump(df, f)

    ex = build_exec_table(sig, tmp, "1m", LAGS, 4)
    args = argparse.Namespace(cost_bps=0.0, interval="1m", lags=LAGS,
                              capital=4_000_000, vol_shares=[0.05, 0.10])
    report(ex, sig.assign(alpha=0.0), pd.Series(dtype=float), args)

    r0 = exec_returns(ex, "fc", 0.0)
    r30 = exec_returns(ex, 30, 0.0)
    d0, d30 = gd.stats(daily_from_trades(ex, r0)), gd.stats(daily_from_trades(ex, r30))
    ok = d0["mean_bp"] > d30["mean_bp"] > 0 and len(ex) > 100
    print(f"\nSELF-TEST: 最初のバー終値 {d0['mean_bp']:.1f}bp > 遅延30分 "
          f"{d30['mean_bp']:.1f}bp (劣化を検出)")
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ギャップ反転の執行可能性検証")
    ap.add_argument("--interval", choices=["1m", "5m"], default="1m")
    ap.add_argument("--minute-dir", dest="minute_dir")
    ap.add_argument("--cache-dir", dest="cache_dir", default=".rsi2_cache")
    ap.add_argument("--cost-bps", dest="cost_bps", type=float, default=30.0)
    ap.add_argument("--capital", type=float, default=4_000_000)
    ap.add_argument("--vol-shares", dest="vol_shares", default="0.02,0.05,0.10",
                    help="入る時点の出来高に対する上限の割合")
    ap.add_argument("--lags", default=",".join(str(x) for x in LAGS))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-index", dest="no_index", action="store_true")
    ap.add_argument("--dump-bars", dest="dump_bars", type=int, default=0,
                    help="ギャップ上位N銘柄日の生の分足を無加工で表示する")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    args = ap.parse_args()
    args.lags = [int(x) for x in str(args.lags).replace(",", " ").split()]
    args.vol_shares = [float(x) for x in
                       str(args.vol_shares).replace(",", " ").split()]

    if args.self_test:
        return self_test()

    mdir = resolve_minute_dir(args.interval, args.minute_dir)
    if mdir is None:
        print("分足のディレクトリが見つかりません。")
        print("  1分足: 環境変数 MINUTE_1M_DIR、既定 ~/.jquants_cache/minute")
        print("  5分足: 環境変数 MINUTE_5M_DIR、既定 data/minute_5m / stock_5min")
        print("  --minute-dir で直接指定もできます。")
        return 1
    print(f"分足: {mdir}  ({args.interval})")

    syms, has_index = gd.load_cache_dir(args.cache_dir, gd.INDEX_SYM,
                                        want_index=not args.no_index)
    if not syms:
        print("日足キャッシュが見つかりません。")
        return 1
    if args.limit:
        syms = syms[: args.limit]
    if not has_index and not args.no_index:
        print(f"指数 {gd.INDEX_SYM} がキャッシュにありません。--no-index を検討")
        return 1

    panel, mkt = gd.build_panel(syms, args.workers, False, 0.9,
                                no_index=args.no_index)
    gd._BOUNCE = gd.bounce_slopes(panel)
    sig = gd.apply_signal(panel, mkt, False, FROZEN_PREV_THR, FROZEN_GAP_THR)

    # 分足がある期間だけに絞る (ホールドアウト)
    sig = sig[sig["date"] >= MINUTE_START].copy()
    print(f"ホールドアウト ({MINUTE_START:%Y-%m}〜) のシグナル: {len(sig):,} 件")
    if sig.empty:
        print("ホールドアウト期間にシグナルがありません。")
        return 1
    # 分足との突き合わせに使う日足の始値・終値
    sig["open_day"] = sig["open"]
    close_map = {}
    for s in sig["symbol"].unique():
        df = gd.fetch_daily(s)
        if df is not None:
            close_map[s] = df["close"]
    sig["close_day"] = [
        float(close_map[r.symbol].get(r.date, np.nan))
        if r.symbol in close_map else np.nan
        for r in sig.itertuples()
    ]
    sig = sig[sig["close_day"].notna()]

    if args.dump_bars:
        dump_raw_bars(sig, mdir, args.interval, args.dump_bars)
        return 0

    ex = build_exec_table(sig, mdir, args.interval, args.lags, args.workers)
    report(ex, sig, mkt, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
