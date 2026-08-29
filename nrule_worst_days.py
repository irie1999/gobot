"""N ルールの大負け日を解剖し、建玉サイズの決め方を並べて比べる。

⛔ このスクリプトは **指数を一切使いません**。N ルールは生パーセントの
   条件だけで決まるので、β も残差も要りません。gap_reversal_daily の
   --nrule が指数の穴で件数を落としていた問題をここでは踏みません。

N ルール (丸めない):
    前夜   前日リターン >= +1.753% / 建値 1,000〜6,000円
           売買代金20日平均の その日の上位50銘柄
    09:00  始値が前日終値の +100bp 以上 → 空売り
    決済   その日の終値。損切りも利確も置かない
    予算   400万 / 100株単位 / 発注順 |ギャップ|降順
    コスト 片道 4.4bp (呼値1円 / 建値2,257円)
鏡像 = 符号を全部反転 (前日 -1.753% 以下 → 始値 -100bp 以下 → 買い → 引け)

    python nrule_worst_days.py                  # 空売り側 (N)
    python nrule_worst_days.py --side long      # 鏡像 (買い)
    python nrule_worst_days.py --corr           # セクター代理 (相関) も出す
    python nrule_worst_days.py --self-test
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

import gap_reversal_daily as G

PREV_THR = 0.01753        # ⛔ 1.75% ではない。丸めない
GAP_THR = 0.01000
TOPN = 50                 # 売買代金20日平均の上位N銘柄
ONEWAY_BPS = 4.4
BUDGET = 4_000_000
LOT = 100
MIN_PRICE, MAX_PRICE = 1_000.0, 6_000.0
MAX_MOVE = 0.30           # 値幅制限より緩い上限。超えるのはデータ破損
ATR_N = 20
TRAIN_END = pd.Timestamp("2020-09-01")   # TRAIN 〜2020-08 / TEST 2020-09〜

_RET: dict[str, pd.Series] = {}          # 相関の代理計算用


def build_rows(sym: str) -> pd.DataFrame | None:
    df = G.fetch_daily(sym)
    if df is None or len(df) < 250:
        return None
    d = df.copy()
    ok = ((d["high"] >= d["low"])
          & d["open"].between(d["low"], d["high"])
          & d["close"].between(d["low"], d["high"]))
    d = d[ok]
    if len(d) < 250:
        return None
    d["ret"] = d["close"].pct_change()
    d["prev_ret"] = d["ret"].shift(1)
    d["prev_close"] = d["close"].shift(1)
    d["gap"] = d["open"] / d["prev_close"] - 1.0
    d["o2c"] = d["close"] / d["open"] - 1.0
    d["atr"] = G._atr_pct(d, ATR_N)
    d["turnover"] = (d["close"] * d["volume"]).rolling(20).mean()
    sane = ((d["gap"].abs() <= MAX_MOVE) & (d["o2c"].abs() <= MAX_MOVE)
            & (d["ret"].abs() <= MAX_MOVE))
    keep = (sane & d["prev_close"].between(MIN_PRICE, MAX_PRICE)
            & d["turnover"].notna() & d["atr"].notna() & (d["atr"] > 0)
            & d["prev_ret"].notna())
    d = d[keep]
    if d.empty:
        return None
    _RET[sym] = df["close"].pct_change()
    return pd.DataFrame({
        "date": d.index, "symbol": sym,
        "open": d["open"].to_numpy(), "close": d["close"].to_numpy(),
        "prev_close": d["prev_close"].to_numpy(),
        "prev_ret": d["prev_ret"].to_numpy(), "gap": d["gap"].to_numpy(),
        "o2c": d["o2c"].to_numpy(), "atr": d["atr"].to_numpy(),
        "turnover": d["turnover"].to_numpy(),
    })


def build_panel(symbols: list[str], workers: int) -> pd.DataFrame:
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(build_rows, symbols), 1):
            if r is not None and len(r):
                rows.append(r)
            if i % 300 == 0:
                print(f"  ... {i}/{len(symbols)}", file=sys.stderr)
    if not rows:
        raise SystemExit("候補が1件もありません")
    p = pd.concat(rows, ignore_index=True)
    # その日の売買代金 上位TOPN に絞る
    p["rank"] = p.groupby("date")["turnover"].rank(ascending=False,
                                                   method="first")
    p = p[p["rank"] <= TOPN].sort_values(["date", "symbol"])
    return p


def signals(panel: pd.DataFrame, side: str) -> pd.DataFrame:
    if side == "short":
        m = (panel["prev_ret"] >= PREV_THR) & (panel["gap"] >= GAP_THR)
        sd = -1
    else:
        m = (panel["prev_ret"] <= -PREV_THR) & (panel["gap"] <= -GAP_THR)
        sd = +1
    s = panel[m].copy()
    s["side"] = sd
    return s.sort_values(["date", "gap"],
                         ascending=[True, side == "long"])


def size_day(g: pd.DataFrame, scheme: str) -> pd.DataFrame:
    """1日ぶんの建玉数を決める。発注順は |ギャップ| 降順、予算 400万で打ち切り。

    ⚠ 最小単位が100株なので、金額均等は『大きくする』方向にしか動けません。
      2単元以上になった割合を必ず併記すること。
    """
    g = g.copy()
    n = len(g)
    if scheme == "fixed":
        want = np.full(n, float(LOT))
    elif scheme == "equal":
        tgt = BUDGET / n
        want = np.floor(tgt / (g["open"] * LOT)).to_numpy() * LOT
    elif scheme == "atr":
        inv = 1.0 / g["atr"].to_numpy()
        tgt = BUDGET * inv / inv.sum()
        want = np.floor(tgt / (g["open"] * LOT)).to_numpy() * LOT
    else:
        raise ValueError(scheme)
    want = np.maximum(want, LOT)          # 100株が最小単位
    # 予算で打ち切る (|ギャップ| 降順の順に埋める)
    notional = want * g["open"].to_numpy()
    cum = np.cumsum(notional)
    take = cum <= BUDGET
    if not take.any():
        take[0] = True                     # 1件も建たないのは不自然
    g = g[take].copy()
    g["qty"] = want[take]
    g["notional"] = g["qty"] * g["open"]
    return g


def price_pnl(g: pd.DataFrame) -> pd.DataFrame:
    gross = g["side"] * (g["close"] - g["open"]) * g["qty"]
    fee = g["notional"] * (ONEWAY_BPS / 10000.0) * 2
    g = g.copy()
    g["pnl"] = gross - fee
    g["units"] = g["qty"] / LOT
    return g


def apply_sizing(sig: pd.DataFrame, scheme: str) -> pd.DataFrame:
    out = [price_pnl(size_day(g, scheme)) for _, g in sig.groupby("date")]
    return pd.concat(out, ignore_index=True)


def monthly(trades: pd.DataFrame) -> pd.Series:
    d = trades.groupby("date")["pnl"].sum()
    return d.groupby(pd.PeriodIndex(d.index, freq="M")).sum()


def describe(trades: pd.DataFrame, label: str) -> dict:
    daily = trades.groupby("date")["pnl"].sum()
    mon = monthly(trades)
    mu, sd = float(mon.mean()), float(mon.std(ddof=1))
    t = mu / (sd / math.sqrt(len(mon))) if sd > 0 and len(mon) > 1 else float("nan")
    two = float((trades["units"] >= 2).mean() * 100)
    return {
        "label": label, "trades": len(trades), "days": len(daily),
        "months": len(mon), "月平均": mu, "月次σ": sd,
        "月平均/σ": mu / sd if sd > 0 else float("nan"), "t": t,
        "最悪の日": float(daily.min()), "最悪の月": float(mon.min()),
        "プラス月%": float((mon > 0).mean() * 100),
        "2単元以上%": two,
        "1日建玉額 中央": float(trades.groupby("date")["notional"].sum().median()),
    }


def print_table(rows: list[dict]) -> None:
    cols = ["label", "trades", "days", "months", "月平均", "月次σ", "月平均/σ",
            "t", "最悪の日", "最悪の月", "プラス月%", "2単元以上%"]
    print(f"    {'方式':<12}{'取引':>7}{'日':>6}{'月':>5}{'月平均':>11}"
          f"{'月次σ':>11}{'月平均/σ':>10}{'t':>7}{'最悪の日':>11}"
          f"{'最悪の月':>11}{'ﾌﾟﾗｽ月%':>9}{'2単元+%':>9}")
    for r in rows:
        print(f"    {r['label']:<12}{r['trades']:>7,}{r['days']:>6,}"
              f"{r['months']:>5}{r['月平均']:>11,.0f}{r['月次σ']:>11,.0f}"
              f"{r['月平均/σ']:>10.3f}{r['t']:>7.2f}{r['最悪の日']:>11,.0f}"
              f"{r['最悪の月']:>11,.0f}{r['プラス月%']:>9.0f}"
              f"{r['2単元以上%']:>9.1f}")


def day_anatomy(trades: pd.DataFrame, n225: pd.DataFrame | None,
                k: int, worst: bool, corr: bool) -> None:
    """大負け日 / 大勝ち日の解剖。集中か共変動かを分ける。"""
    daily = trades.groupby("date")["pnl"].sum().sort_values(
        ascending=worst).head(k)
    tag = "大負け" if worst else "大勝ち"
    print(f"\n  {tag}の日 上位{k}")
    print(f"      {'日付':<12}{'件':>4}{'建玉合計':>12}{'日損益':>12}"
          f"{'最大1件':>11}{'中央':>10}{'最小':>11}{'最大の寄与%':>13}"
          f"{'負け比率':>10}{'その建値':>10}{'HHI':>7}{'N225寄り%':>11}{'N225日中%':>11}"
          + ("{:>10}".format("相関") if corr else ""))
    for d in daily.index:
        g = trades[trades["date"] == d]
        tot = float(g["pnl"].sum())
        ext = float(g["pnl"].min() if worst else g["pnl"].max())
        row = g.loc[g["pnl"].idxmin() if worst else g["pnl"].idxmax()]
        share = ext / tot * 100 if tot else float("nan")
        w = g["notional"] / g["notional"].sum()
        hhi = float((w ** 2).sum())
        ng = nd = float("nan")
        if n225 is not None and d in n225.index:
            ng = float(n225.loc[d, "gap"]) * 100
            nd = float(n225.loc[d, "o2c"]) * 100
        line = (f"      {d:%Y-%m-%d}{len(g):>4}{g['notional'].sum():>12,.0f}"
                f"{tot:>12,.0f}{ext:>11,.0f}{g['pnl'].median():>10,.0f}"
                f"{(g['pnl'].max() if worst else g['pnl'].min()):>11,.0f}"
                f"{share:>13.1f}{(g['pnl'] < 0).mean()*100:>9.0f}%"
                f"{row['open']:>10,.0f}{hhi:>7.3f}"
                f"{ng:>11.2f}{nd:>11.2f}")
        if corr:
            line += f"{mean_pair_corr(list(g['symbol']), d):>10.2f}"
        print(line)


def mean_pair_corr(syms: list[str], asof: pd.Timestamp, win: int = 60) -> float:
    """その日建てた銘柄どうしの、過去60日リターンの平均相関 (セクターの代理)。

    高ければ『同じ賭けを13回している』ことになります。
    """
    if len(syms) < 2:
        return float("nan")
    cols = {}
    for s in syms:
        r = _RET.get(s)
        if r is None:
            continue
        r = r[r.index < asof].tail(win)
        if len(r) >= win // 2:
            cols[s] = r
    if len(cols) < 2:
        return float("nan")
    c = pd.DataFrame(cols).dropna().corr().to_numpy()
    iu = np.triu_indices_from(c, k=1)
    v = c[iu]
    v = v[~np.isnan(v)]
    return float(v.mean()) if len(v) else float("nan")


def concentration(trades: pd.DataFrame) -> None:
    """集中か共変動か。⛔ ここが本題です。

    純損益に対する寄与率は、他の建玉がプラスだと 100% を超えて発散するので
    判別に使えません。使うのは次の2つ:
      (1) **その日の負けだけの合計** に対する最悪1件の割合 (0〜100%)
      (2) **その日の建玉のうち負けた割合** ← これがいちばん素直な判別
    全部が一緒に負けていれば (2) は 1.0 に寄り、1〜2銘柄なら小さくなります。
    """
    daily = trades.groupby("date")["pnl"].sum()
    loss_days = daily[daily < 0].index
    g = trades[trades["date"].isin(loss_days)]

    def per_day(x: pd.DataFrame) -> pd.Series:
        neg = x.loc[x["pnl"] < 0, "pnl"]
        return pd.Series({
            "n": len(x),
            "n_loss": len(neg),
            "loss_ratio": len(neg) / len(x),
            "worst_share": (float(neg.min() / neg.sum() * 100)
                            if len(neg) and float(neg.sum()) != 0
                            else float("nan")),
            "equal_share": 100.0 / len(neg) if len(neg) else float("nan"),
        })

    per = g.groupby("date")[["pnl"]].apply(
        lambda x: per_day(g[g["date"] == x.name]))
    per = per.dropna(subset=["worst_share"])
    multi = per[per["n"] >= 3]          # 1〜2件の日は判別に使えない
    hhi = trades.groupby("date").apply(
        lambda x: float(((x["notional"] / x["notional"].sum()) ** 2).sum()),
        include_groups=False)

    print("\n  ⛔ 集中か共変動か (これが本題)")
    print(f"    負けの日 {len(per):,} 日 / うち3件以上建てた日 {len(multi):,} 日")
    print(f"    1日あたり建玉 中央 {per['n'].median():.0f} 件")
    if len(multi) < 20:
        print("    ⚠ 3件以上の日が少なすぎて判別できません")
        return
    print("\n    (1) その日の**負けの合計**に対する最悪1件の割合 (3件以上の日)")
    print(f"      {'':>10}{'実測':>9}{'等分なら':>10}")
    for q in (0.25, 0.50, 0.75, 0.90):
        print(f"      {int(q*100):>3}%点   {multi['worst_share'].quantile(q):>8.1f}%"
              f"{multi['equal_share'].quantile(q):>9.1f}%")
    print(f"      平均      {multi['worst_share'].mean():>8.1f}%"
          f"{multi['equal_share'].mean():>9.1f}%")
    print("      ★ 実測が『等分なら』と同じ水準 → 全部が同じように負けた = 共変動")
    print("        実測がそれを大きく上回る → 1〜2銘柄に寄っている = 集中")

    print("\n    (2) その日の建玉のうち **負けた割合** (いちばん素直な判別)")
    for q in (0.25, 0.50, 0.75, 0.90):
        print(f"      {int(q*100):>3}%点   {multi['loss_ratio'].quantile(q)*100:>8.0f}%")
    print(f"      平均      {multi['loss_ratio'].mean()*100:>8.0f}%")
    worst20 = daily.nsmallest(20).index
    w = per[per.index.isin(worst20)]
    if len(w):
        print(f"      大負け上位20日に限ると 平均 "
              f"{w['loss_ratio'].mean()*100:.0f}%  "
              f"(最悪1件の割合 平均 {w['worst_share'].mean():.1f}% / "
              f"等分なら {w['equal_share'].mean():.1f}%)")
    print("      ★ 8割以上が負けているなら **共変動**。半分程度なら **集中**。")

    print(f"\n    建玉額のハーフィンダール指数  中央 {hhi.median():.3f}"
          f" / 90%点 {hhi.quantile(0.9):.3f}")
    print(f"      参考: 1日中央 {per['n'].median():.0f} 件を等額なら "
          f"{1/max(per['n'].median(),1):.3f}。"
          "これより大きいほど建玉額が偏っています")
    print("\n    ⛔ 共変動なら、ヘッジ (コスト負け) も予測 (不可能) も潰れている")
    print("       以上、残るのはサイズを下げることだけです。")
    print("       それは発見ではなく決断で、検証で出せる答えではありません。")


def load_n225() -> pd.DataFrame | None:
    df = G.fetch_index() if hasattr(G, "fetch_index") else None
    if df is None:
        saved = G._LOCAL
        G.__dict__["_LOCAL"] = None
        df = G.fetch_daily("^N225")
        G.__dict__["_LOCAL"] = saved
    if df is None:
        return None
    out = pd.DataFrame(index=df.index)
    out["gap"] = df["open"] / df["close"].shift(1) - 1.0
    out["o2c"] = df["close"] / df["open"] - 1.0
    return out


def run(sig: pd.DataFrame, n225, args) -> None:
    schemes = [("100株固定", "fixed"), ("金額均等", "equal"),
               ("ATR均等", "atr")]
    sized = {name: apply_sizing(sig, key) for name, key in schemes}

    for split, lo, hi in (("TRAIN (〜2020-08)", None, TRAIN_END),
                          ("TEST (2020-09〜)", TRAIN_END, None),
                          ("全期間", None, None)):
        print(f"\n■ {split}")
        rows = []
        for name, _ in schemes:
            t = sized[name]
            if lo is not None:
                t = t[t["date"] >= lo]
            if hi is not None:
                t = t[t["date"] < hi]
            if len(t) < 50:
                continue
            rows.append(describe(t, name))
        if rows:
            print_table(rows)
    print("\n  ⛔ 見るのは総額ではなく 月平均/σ と 最悪の日/月 です。")
    print("     総額が上がってσも上がるなら、ただのレバレッジで成功ではありません。")
    print("     ⚠ 100株が最小単位なので金額均等は『大きくする』方向にしか")
    print("       動けません。2単元以上% が小さいなら 100株固定と大差ない行です。")

    base = sized["100株固定"]
    print("\n" + "=" * 78)
    print("■ 大負け日の解剖 (100株固定)")
    day_anatomy(base, n225, 20, True, args.corr)
    day_anatomy(base, n225, 20, False, args.corr)
    concentration(base)


def self_test() -> int:
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2015-01-01", "2024-12-31")
    rows = []
    for k in range(80):
        sym = f"T{k:04d}.T"
        n = len(dates)
        ret = rng.normal(0, 0.02, n)
        px = 2000 * np.exp(np.cumsum(ret))
        op = px * (1 + rng.normal(0, 0.01, n))
        rows.append(pd.DataFrame({
            "date": dates, "symbol": sym, "open": op, "close": px,
            "prev_close": np.roll(px, 1),
            "prev_ret": np.roll(ret, 1), "gap": op / np.roll(px, 1) - 1.0,
            "o2c": px / op - 1.0, "atr": 0.02, "turnover": 1e9,
        }))
        _RET[sym] = pd.Series(ret, index=dates)
    panel = pd.concat(rows, ignore_index=True)
    panel["rank"] = panel.groupby("date")["turnover"].rank(
        ascending=False, method="first")
    panel = panel[panel["rank"] <= TOPN]
    sig = signals(panel, "short")
    print(f"SELF-TEST: 発火 {len(sig):,} 件 / {sig['date'].nunique():,} 日")
    args = argparse.Namespace(corr=False)
    run(sig, None, args)
    ok = len(sig) > 100
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--side", choices=["short", "long"], default="short",
                    help="short=N (空売り) / long=鏡像 (買い)")
    ap.add_argument("--cache-dir", dest="cache_dir", default=".rsi2_cache")
    ap.add_argument("--limit", type=int, help="先頭N銘柄だけ (デバッグ)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--corr", action="store_true",
                    help="セクターの代理として、その日の銘柄どうしの"
                         "過去60日リターン平均相関を出す")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    print(G.provenance("nrule_worst_days.py"))
    syms, _ = G.load_cache_dir(args.cache_dir, "^N225", want_index=False)
    if args.limit:
        syms = syms[: args.limit]
    print(f"  {len(syms)} 銘柄からパネルを作ります (指数は使いません)")
    panel = build_panel(syms, args.workers)
    print(f"  上位{TOPN}位の候補 {len(panel):,} 行 / "
          f"{panel['date'].nunique():,} 営業日 "
          f"({panel['date'].min():%Y-%m-%d} 〜 {panel['date'].max():%Y-%m-%d})")
    sig = signals(panel, args.side)
    nd = panel["date"].nunique()
    print(f"\nN ルール ({args.side})  前日 {PREV_THR*100:.3f}% / "
          f"ギャップ {GAP_THR*100:.2f}% / 売買代金 上位{TOPN}位")
    print(f"  発火 {len(sig):,} 件 / {sig['date'].nunique():,} 日 "
          f"= {len(sig)/nd*20.7:.0f} 件/月 (20.7営業日/月)")
    print("  ⛔ 参照値は 144件/月、別の独立実装は 150〜190件/月。")
    print("     ここが桁で違うなら、成績を論じる前に仕様差を特定すること。")
    if not len(sig):
        return 1
    n225 = load_n225()
    if n225 is None:
        print("  ⚠ ^N225 が取れないので N225 列は空になります")
    else:
        print("  ⚠ N225 列は記述用です。yfinance の ^N225 は"
              "2017-07〜2019-07 が欠けている実績があります")
    run(sig, n225, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
