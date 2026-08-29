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
    # ⛔ 先読み防止: 20日平均売買代金は **当日を含めてはいけません**。
    #   監視枠 (上位50) は前夜に選ぶので、当日の出来高は使えません。
    #   rolling(20).mean() は当日を含むので shift(1) が要ります。
    #   (2026-08-29 に検査で発見。§11「前夜の売買代金」が実際には
    #    当日の出来高を見ていました)
    d["turnover"] = (d["close"] * d["volume"]).rolling(20).mean().shift(1)
    d["next_open"] = d["open"].shift(-1)   # 強制持ち越しの評価にだけ使う
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
        "next_open": d["next_open"].to_numpy(),
        "high": d["high"].to_numpy(), "low": d["low"].to_numpy(),
    })


def build_panel(symbols: list[str], workers: int,
                select: str = "rank-first") -> pd.DataFrame:
    """候補を作る。

    ⛔ 『売買代金の上位50位』をどの母集団で数えるかで件数が数倍変わります。
      rank-first  : 全銘柄を売買代金で並べて上位50 → そこに条件を当てる (既定)
      filter-first: 先に前日条件を満たす銘柄だけを取り出し、その中の上位50
    後者は「その日 前日+1.753% を満たした銘柄」が母集団なので、同じ50でも
    中身が全く違い、発火は数倍になります。参照値 144件/月 との差はここが
    いちばん疑わしい。
    """
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
    if select == "filter-first":
        # 前日条件 (どちらの符号でも) を満たす銘柄の中で上位TOPN
        p = p[p["prev_ret"].abs() >= PREV_THR]
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
            # ⛔ 帰無仮説は「等分 = 1/n」ではありません。n 個の独立な損失の
            #   最大値は、偶然でも平均を大きく超えます。指数分布なら
            #   E[max/合計] = H_n / n (H_n は調和数)。これが正しい基準線。
            #   1/n と比べると、何を測っても『集中』に見えてしまいます。
            "equal_share": (100.0 * sum(1.0 / k for k in range(1, len(neg) + 1))
                            / len(neg)) if len(neg) else float("nan"),
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
    print(f"      {'':>10}{'実測':>9}{'偶然でも':>10}")
    for q in (0.25, 0.50, 0.75, 0.90):
        print(f"      {int(q*100):>3}%点   {multi['worst_share'].quantile(q):>8.1f}%"
              f"{multi['equal_share'].quantile(q):>9.1f}%")
    print(f"      平均      {multi['worst_share'].mean():>8.1f}%"
          f"{multi['equal_share'].mean():>9.1f}%")
    print("      ★ 『偶然でも』は n 個の独立な損失で最大値が占める期待割合")
    print("        (指数分布の H_n/n)。⛔ 1/n ではありません。n=5 なら 45.7%、")
    print("        n=13 なら 24.5% が偶然の水準です。")
    print("        実測がこれと同水準 → 集中は偶然の範囲 = 共変動")
    print("        実測が大きく上回る → 1〜2銘柄に本当に寄っている = 集中")

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
              f"偶然でも {w['equal_share'].mean():.1f}%)")
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


def combined(panel: pd.DataFrame, scheme: str = "fixed") -> None:
    """N (空売り) と 鏡像 (買い) を同時に建てた場合。

    ★ 両者の大負け日は「市場が自分と逆に日中動いた日」で、向きが逆です。
      N の大負け日は日経が日中プラス、鏡像の大負け日は日経が日中マイナス。
      **同時に建てると市場エクスポージャが相殺されるはず**、という検証。
    ⚠ これは『両建て』(同一銘柄の両方向) とは別物です。独立な2つの
      シグナル集合で、βの符号が逆というだけ。過去に潰した系統ではありません。
    """
    s_short = signals(panel, "short")
    s_long = signals(panel, "long")
    both = pd.concat([s_short, s_long], ignore_index=True)
    both = both.sort_values(["date", "gap"], key=lambda c: (
        c if c.name == "date" else -c.abs()))
    # 予算は両側合わせて 400万。発注順は |ギャップ| 降順で共通
    t_both = apply_sizing(both, scheme)
    t_s = apply_sizing(s_short, scheme)
    t_l = apply_sizing(s_long, scheme)
    print("\n" + "=" * 78)
    print("■ 両側を同時に建てた場合 (100株固定, 予算は両側で400万を共有)")
    print("  ★ N の大負け日は日経が日中プラス、鏡像の大負け日は日中マイナス。")
    print("    市場エクスポージャの符号が逆なので、合算すると相殺されるはず。")
    print("  ⚠ 『両建て』(同一銘柄の両方向) とは別物です。")
    for split, lo, hi in (("TRAIN (〜2020-08)", None, TRAIN_END),
                          ("TEST (2020-09〜)", TRAIN_END, None),
                          ("全期間", None, None)):
        rows = []
        for name, t in (("N のみ", t_s), ("鏡像のみ", t_l), ("両側", t_both)):
            x = t
            if lo is not None:
                x = x[x["date"] >= lo]
            if hi is not None:
                x = x[x["date"] < hi]
            if len(x) >= 50:
                rows.append(describe(x, name))
        if rows:
            print(f"\n  {split}")
            print_table(rows)
    # 同じ日に両側が出ているか
    d_s, d_l = set(t_s["date"]), set(t_l["date"])
    print(f"\n  発火日  N {len(d_s):,} 日 / 鏡像 {len(d_l):,} 日 / "
          f"両方 {len(d_s & d_l):,} 日 ({len(d_s & d_l)/len(d_s | d_l)*100:.0f}%)")
    # ★ 相殺が起きたなら **最悪の日が改善する** はずです。ここが判定。
    for name, t in (("N のみ", t_s), ("鏡像のみ", t_l), ("両側", t_both)):
        d = t.groupby("date")["pnl"].sum()
        print(f"    {name:<8} 最悪の日 {d.min():>12,.0f}  "
              f"(その日: {d.idxmin():%Y-%m-%d})")
    print("    ⛔ 『両側』の最悪の日が片側の最悪の日と同じ値なら、その日は")
    print("       片側しか建っていません = **相殺は起きていません**。")
    print("       月平均/σ が改善しても、それは日をまたいだ分散にすぎず、")
    print("       裾のリスクは1円も減っていません。")
    print("  ⛔ 両方が同じ日に出るのが稀なら、合算しても相殺は起きません。")
    print("     その場合『両側』は単に取引機会が増えただけで、σ が下がるのは")
    print("     日をまたいだ分散にすぎません。月平均/σ の改善幅で判断すること。")


def _es(x: np.ndarray, q: float) -> float:
    """下位 q の Expected Shortfall (平均損失)。VaR ではなく『その先の平均』。"""
    if not len(x):
        return float("nan")
    thr = np.quantile(x, q)
    tail = x[x <= thr]
    return float(tail.mean()) if len(tail) else float("nan")


def _block_boot(daily: pd.Series, q: float, n_boot: int = 2000,
                block: int = 5, seed: int = 0) -> tuple[float, float]:
    """日ブロックのブートストラップで ES の信頼区間を出す。

    ⛔ 観測された最悪の1日は将来の上限ではありません。**ES の信頼上限**で
      決めること。日次損益は日をまたいで相関するのでブロックで抜きます。
    """
    rng = np.random.default_rng(seed)
    x = daily.to_numpy()
    n = len(x)
    if n < 60:
        return (float("nan"), float("nan"))
    nb = max(1, n // block)
    out = np.empty(n_boot)
    for i in range(n_boot):
        st = rng.integers(0, max(1, n - block), size=nb)
        idx = (st[:, None] + np.arange(block)[None, :]).ravel()
        out[i] = _es(x[np.clip(idx, 0, n - 1)], q)
    return (float(np.quantile(out, 0.05)), float(np.quantile(out, 0.95)))


def forced_carry(trades: pd.DataFrame) -> pd.Series:
    """引けで決済できなかった建玉を翌営業日の始値まで持った場合の日次損益。

    N は空売りなので、終値がストップ高だと買い戻せません。
    ⚠ 持ち越しは日中の値幅制限の外に出るので **別のテール**です。
      含む版と除く版を必ず並べること。
    """
    px_prev = trades["prev_close"]
    lim = px_prev.map(G.tse_price_limit)
    up, dn = px_prev + lim, px_prev - lim
    stuck = np.where(trades["side"] < 0,
                     (trades["close"] - up).abs() <= 0.01,
                     (trades["close"] - dn).abs() <= 0.01)
    stuck = pd.Series(stuck, index=trades.index) & trades["next_open"].notna()
    t = trades.copy()
    if stuck.any():
        b = t[stuck]
        gross = b["side"] * (b["next_open"] - b["open"]) * b["qty"]
        fee = b["notional"] * (ONEWAY_BPS / 10000.0) * 2
        t.loc[stuck, "pnl"] = gross - fee
    t.attrs["stuck"] = int(stuck.sum())
    return t


def risk_report(trades: pd.DataFrame) -> None:
    """サイズを決めるための数字。⛔ 観測最悪値では決めないこと。"""
    carried = forced_carry(trades)
    n_stuck = carried.attrs.get("stuck", 0)
    print("\n" + "=" * 78)
    print("■ サイズを決めるための数字 (100株固定・予算400万)")
    print("  ⛔ 観測された最悪の1日は将来の上限ではありません。")
    print("     決めるのは **ES (下位xx%の平均損失) の信頼上限** です。")
    print(f"  強制持ち越し (引けがストップ値で決済できない) {n_stuck} 件")
    print("  ⚠ 持ち越しは日中の値幅制限の外に出るので **別のテール**です。")

    for label, t in (("持ち越しを除く", trades), ("持ち越しを含む", carried)):
        d = t.groupby("date")["pnl"].sum().sort_index()
        x = d.to_numpy()
        mu = float(d.mean())
        print(f"\n  【{label}】 {len(d):,} 日 / 平均日次 {mu:,.0f} 円")
        print(f"      {'':<14}{'ES':>12}{'90%区間 下':>14}{'90%区間 上':>14}"
              f"{'平均/|ES|':>12}")
        for q, name in ((0.01, "下位1%"), (0.05, "下位5%")):
            es = _es(x, q)
            lo, hi = _block_boot(d, q)
            # ⛔ 上限として使うのは「より悪い方」= 区間の下端 (損失が大きい側)
            print(f"      {name:<14}{es:>12,.0f}{lo:>14,.0f}{hi:>14,.0f}"
                  f"{(mu/abs(es) if es else float('nan')):>12.3f}")
        srt = np.sort(x)
        print(f"      最悪5日の平均  {srt[:5].mean():>12,.0f}"
              f"   最悪10日の平均 {srt[:10].mean():>12,.0f}")
        print(f"      観測最悪の1日  {srt[0]:>12,.0f}"
              "   ← ⛔ これで決めないこと")
    d0 = trades.groupby("date")["pnl"].sum()
    mu0 = float(d0.mean())
    es0 = _es(carried.groupby("date")["pnl"].sum().to_numpy(), 0.01)
    if mu0 > 0 and es0:
        nd = abs(es0) / mu0
        per_month = len(d0) / max(len(pd.PeriodIndex(d0.index, freq="M")
                                      .unique()), 1)
        print(f"\n  ★ 言い換え: 下位1%の日 1回を取り返すのに、平均的な発火日が"
              f" **{nd:,.0f} 日** 必要です")
        print(f"     (発火は月 {per_month:.1f} 日 なので およそ "
              f"{nd / max(per_month, 1e-9):.1f} ヶ月)")
    print("\n  ★ 予算の決め方: 許容できる1日の損失 ÷ |下位1% ES の信頼下端|")
    print("     を現行の400万に掛ける。ESの下端 (悪い側) を使うのは、")
    print("     点推定で決めると半分の確率で外れるからです。")
    print("  ⚠ ただし 100株が最小単位なので、**1銘柄あたりを小さくはできません**。")
    print("     下げる手段は『1日に建てる銘柄数の上限』だけで、それは平均も")
    print("     同じ比率で削ります。ES だけを選んで削ることはできません。")


def vol_regression(sig: pd.DataFrame, trades: pd.DataFrame,
                   panel: pd.DataFrame) -> None:
    """ボラ回帰 — 1本だけやって判定し、ダメなら打ち切る。

    仮説: 寄り前に分かる材料で予想ポートフォリオσを作り、σが高い日は
    建玉を落とせば 平均損益÷ES が改善する。

    ⛔ 判定は2つだけ:
      (a) |日次損益| を予測ボラで回帰 → R²  (予測できているか)
      (b) 日次損益の**平均**が予測ボラに **比例するか / フラットか**
          比例 → サイズ変更は中立。打ち切り
          フラット → 平均÷ES が改善する。そのときだけ次へ
    """
    print("\n" + "=" * 78)
    print("■ ボラ回帰 (1本だけ。フラットでなければ打ち切り)")
    # 寄り前に確定する材料だけを使う
    pre = panel[panel["prev_ret"].abs() >= PREV_THR]
    day = pre.groupby("date").agg(n_pre=("symbol", "size"),
                                  atr_mean=("atr", "mean"))
    # ポートフォリオσの素朴な予測: 平均ATR × sqrt(候補件数)
    day["pred_vol"] = day["atr_mean"] * np.sqrt(day["n_pre"])
    d = trades.groupby("date")["pnl"].sum().rename("pnl")
    j = pd.concat([d, day], axis=1, join="inner").dropna()
    if len(j) < 100:
        print("  日数が足りません")
        return
    print(f"  {len(j):,} 日  (予測ボラ = 前夜の候補件数と平均ATRだけで作る)")

    print("\n  (a) |日次損益| ~ 予測ボラ の回帰")
    for col in ("pred_vol", "atr_mean", "n_pre"):
        xx = j[col].to_numpy()
        yy = j["pnl"].abs().to_numpy()
        r = np.corrcoef(xx, yy)[0, 1]
        print(f"      {col:<12} R² = {r*r:>6.3f}  (相関 {r:+.3f})")
    print("      ★ R² が 0.1 も無ければ、そもそもボラを予測できていません。")

    print("\n  (b) 予測ボラの5分位別  ← ここが判定")
    j["q"] = pd.qcut(j["pred_vol"], 5, labels=False, duplicates="drop")
    print(f"      {'分位':<6}{'日数':>6}{'平均日次':>12}{'日次σ':>12}"
          f"{'平均/σ':>10}{'下位5%ES':>12}{'平均/|ES|':>11}")
    for q, g in j.groupby("q"):
        x = g["pnl"].to_numpy()
        mu, sd = x.mean(), x.std(ddof=1)
        es = _es(x, 0.05)
        print(f"      Q{int(q)+1:<5}{len(g):>6}{mu:>12,.0f}{sd:>12,.0f}"
              f"{(mu/sd if sd else float('nan')):>10.3f}{es:>12,.0f}"
              f"{(mu/abs(es) if es else float('nan')):>11.3f}")
    lo = j[j["q"] == j["q"].min()]["pnl"]
    hi = j[j["q"] == j["q"].max()]["pnl"]
    vr = (hi.std(ddof=1) / lo.std(ddof=1)) if lo.std(ddof=1) else float("nan")
    # ⛔ Q1 の平均がほぼゼロだと比が発散します。比ではなく **平均/σ の推移**
    #   で読むこと。一度 234.71倍 という無意味な数字を出しました。
    if abs(lo.mean()) < 0.05 * abs(hi.mean()):
        print(f"\n      ⛔ Q1 の平均が {lo.mean():,.0f} 円でほぼゼロなので、"
              "Q5/Q1 の比は発散します。")
        print("         比ではなく上の **平均/σ と 平均/|ES| の推移** で読んでください。")
        print(f"         (σ比だけは意味があります: {vr:.2f}倍)")
    else:
        print(f"\n      Q5/Q1  平均 {hi.mean()/lo.mean():.2f}倍 / σ {vr:.2f}倍")
    print("      ★ 読み方は3つに分かれます:")
    print("        平均/|ES| が分位で **フラット**        → サイズ変更は中立。打ち切り")
    print("        平均/|ES| が高分位で **下がる**        → 高ボラ日を小さくすれば改善")
    print("        平均/|ES| が高分位で **上がる**        → ⛔ 逆。高ボラ日こそ")
    print("          エッジがある。落とすと悪化する。**仮説は逆向きに棄却**")
    print("      ⚠ n_pre (候補件数) が効いている場合、この『ボラ』の正体は")
    print("        建玉数です。件数が増えると平均は n に比例、σ は √n でしか")
    print("        増えないので、平均/σ は自動的に上がります。**分散の効果**")
    print("        であって高ボラ日のエッジではありません。混同しないこと。")


# 東証の呼値 (標準)。bp をティックに直すのに要る。
# ⛔ 3,000円の境界で 1円 → 5円 と5倍になります。閾値は株価に比例するだけなので、
#   3,000円超の銘柄では同じ bp が 1/5 のティック数になります。
_TICK = [(0, 1.0), (3_000, 5.0), (5_000, 10.0), (30_000, 50.0),
         (50_000, 100.0), (100_000, 1_000.0)]


def tick_size(px: float) -> float:
    t = 1.0
    for lo, v in _TICK:
        if px >= lo:
            t = v
    return t


def with_stop(sig: pd.DataFrame, sm: float) -> pd.DataFrame:
    """損切りだけを入れる (利確は入れない)。

    ⚠ 日足で **損切りだけ** を測るのは方法論的に妥当です。発動判定は
      高値/安値、約定価格は損切値なので、日中の順序が分からなくても
      結果は一意に決まります。
    ⛔ 利確を併用すると「どちらが先に触れたか」が日足からは復元できません。
      だからこのスクリプトは利確を実装しません (tm=0.0 の列だけ)。
    ⚠ 滑りはゼロ、つまり損切値ちょうどで約定する前提です。**上限の見積り**です。
    """
    g = sig.copy()
    g["qty"] = float(LOT)
    g["notional"] = g["qty"] * g["open"]
    dist = sm * g["atr"]                     # ATR単位 → 建値比
    if sm <= 0:
        g["stop_px"] = np.nan
        g["hit"] = False
    elif (g["side"] < 0).all():              # 空売り: 上に抜けたら損切り
        g["stop_px"] = g["open"] * (1.0 + dist)
        g["hit"] = g["high"] >= g["stop_px"]
    else:                                    # 買い: 下に抜けたら損切り
        g["stop_px"] = g["open"] * (1.0 - dist)
        g["hit"] = g["low"] <= g["stop_px"]
    g["exit_px"] = np.where(g["hit"], g["stop_px"], g["close"])
    gross = g["side"] * (g["exit_px"] - g["open"]) * g["qty"]
    fee = g["notional"] * (ONEWAY_BPS / 10000.0) * 2
    g["pnl"] = gross - fee
    return g


def _stop_row(sig: pd.DataFrame, sm: float, base_mean: float) -> dict:
    g = with_stop(sig, sm)
    m = float(g["pnl"].mean())
    hit = float(g["hit"].mean()) if sm > 0 else 0.0
    eff = m - base_mean
    # 分岐点: 損切りの買い戻しが損切値よりどれだけ不利に約定したら効果が消えるか。
    # ⛔ 片道・損切り決済のみ・発動した取引だけに掛かるコストです。
    hitrows = g[g["hit"]] if sm > 0 else g.iloc[0:0]
    if len(hitrows) and eff > 0:
        notion = float((hitrows["stop_px"] * hitrows["qty"]).mean())
        be = eff / (hit * notion) * 10000.0 if hit > 0 else float("nan")
    else:
        be = float("nan")
    # bp をティックに直す。呼値は株価で変わるので中央値と、3,000円で分けた値
    def ticks(rows):
        if not len(rows) or not (be == be):
            return float("nan")
        t = rows["open"].map(tick_size)
        return float((be / 10000.0 * rows["open"] / t).median())
    lo = hitrows[hitrows["open"] < 3000] if len(hitrows) else hitrows
    hi = hitrows[hitrows["open"] >= 3000] if len(hitrows) else hitrows
    return {"sm": sm, "n": len(g), "mean": m, "eff": eff, "hit": hit * 100,
            "be_bp": be, "tick_all": ticks(hitrows),
            "tick_lo": ticks(lo), "tick_hi": ticks(hi),
            "n_lo": len(lo), "n_hi": len(hi)}


def _print_stop_table(rows: list) -> None:
    print(f"    {'損切ATR':>8}{'取引':>8}{'発動率%':>9}{'円/件':>10}"
          f"{'現行との差':>12}{'分岐点bp':>10}"
          f"{'ﾃｨｯｸ<3000':>11}{'ﾃｨｯｸ>=3000':>12}")
    for r in rows:
        print(f"    {r['sm']:>8.2f}{r['n']:>8,}{r['hit']:>9.1f}"
              f"{r['mean']:>10,.0f}{r['eff']:>+12,.0f}"
              f"{r['be_bp']:>10.2f}{r['tick_lo']:>11.2f}{r['tick_hi']:>12.2f}")


def stop_grid(panel: pd.DataFrame, side: str, train_from: str,
              train_end: str) -> None:
    """① TRAIN でグリッドを延長する。**形を見るためであって、選ぶためではない。**

    ⛔ ここで最良セルを選び直さないこと。見るのは2点だけ:
      - 山が 0.1 より下にあるか
      - 0.1 は崖の手前か
    """
    sig = signals(panel, side)
    lo, hi = pd.Timestamp(train_from), pd.Timestamp(train_end)
    tr = sig[(sig["date"] >= lo) & (sig["date"] < hi)]
    print(f"\n■ ① TRAIN グリッド延長 ({lo:%Y-%m} 〜 {hi:%Y-%m})  利確なし (tm=0.0)")
    print(f"  取引 {len(tr):,} 件 / {tr['date'].nunique():,} 日"
          f" = {len(tr)/max(tr['date'].nunique(),1):.1f} 件/発火日")
    print("  ⚠ 件数と発動率を必ず別実装と突き合わせること。母集団の差で3回踏んでいます。")
    if len(tr) < 200:
        print("  件数が足りません")
        return
    base = float(with_stop(tr, 0.0)["pnl"].mean())
    print(f"  現行 (損切りなし) = {base:,.0f} 円/件")
    rows = [_stop_row(tr, sm, base) for sm in
            (0.02, 0.03, 0.05, 0.1, 0.3, 0.5, 1.0, 2.0)]
    _print_stop_table(rows)
    best = max(rows, key=lambda r: r["eff"])
    print(f"\n  ★ 形: 最大は sm={best['sm']:.2f}。")
    print("     0.1 より下に山があれば、グリッドは端で切れていたことになります。")
    print("     0.02〜0.05 で急に落ちるなら、0.1 は崖の手前です。")
    print("  ⛔ ここで最良セルを選び直さないこと。②で見るのは sm=0.1 だけです。")
    print("  ⛔ 分岐点bp は **片道・損切り決済のみ・発動した取引だけ** に掛かる")
    print("     コストです。往復ではありません。")
    print("  ⚠ 3,000円で呼値が 1円 → 5円 と5倍になるので、同じ bp でも")
    print("     ティック数は 1/5 になります。>=3000 の列を必ず見ること。")


def stop_test(panel: pd.DataFrame, side: str, train_from: str,
              train_end: str) -> None:
    """② TEST で1回だけ確認する。⛔ 見るのは sm=0.1 のみ。

    事前宣言した合格条件 (3つ全部):
      (a) TEST の「現行との差」がプラス
      (b) 分岐点の滑りが 0.08% (8bp) 以上
      (c) 発動率が TRAIN から ±10ポイント以内
    1つでも落ちたら棄却。滑りの測定に進みません。
    """
    sig = signals(panel, side)
    lo, hi = pd.Timestamp(train_from), pd.Timestamp(train_end)
    tr = sig[(sig["date"] >= lo) & (sig["date"] < hi)]
    te = sig[sig["date"] >= hi]
    print(f"\n■ ② TEST 確認 ({hi:%Y-%m} 〜)  sm=0.1 / 利確なし のみ")
    print("  ⛔ 他のセルは見ません。TEST の消費は1回です。")
    if len(te) < 200 or len(tr) < 200:
        print("  件数が足りません")
        return
    b_tr = float(with_stop(tr, 0.0)["pnl"].mean())
    b_te = float(with_stop(te, 0.0)["pnl"].mean())
    r_tr = _stop_row(tr, 0.1, b_tr)
    r_te = _stop_row(te, 0.1, b_te)
    print(f"  {'':<8}{'取引':>8}{'発動率%':>9}{'現行':>10}{'損切り後':>10}"
          f"{'差':>10}{'分岐点bp':>10}")
    for lbl, r, b in (("TRAIN", r_tr, b_tr), ("TEST", r_te, b_te)):
        print(f"  {lbl:<8}{r['n']:>8,}{r['hit']:>9.1f}{b:>10,.0f}"
              f"{r['mean']:>10,.0f}{r['eff']:>+10,.0f}{r['be_bp']:>10.2f}")
    a = r_te["eff"] > 0
    b_ = (r_te["be_bp"] == r_te["be_bp"]) and r_te["be_bp"] >= 8.0
    c = abs(r_te["hit"] - r_tr["hit"]) <= 10.0
    print("\n  事前宣言した合格条件 (3つ全部):")
    print(f"    (a) TEST の差がプラス            {r_te['eff']:+,.0f} 円/件"
          f"   {'✅' if a else '⛔'}")
    print(f"    (b) 分岐点が 8bp 以上            {r_te['be_bp']:.2f} bp"
          f"   {'✅' if b_ else '⛔'}")
    print(f"    (c) 発動率が TRAIN から ±10pt    "
          f"{r_te['hit']:.1f}% vs {r_tr['hit']:.1f}%"
          f"   {'✅' if c else '⛔'}")
    print(f"\n  → {'通過。滑りの測定に進めます。' if (a and b_ and c) else '⛔ 棄却。滑りの測定に進みません。'}")
    print("  ⚠ +190 の再現は求めていません。TRAIN の最良セルなので縮むのが自然です。")
    print(f"  ⚠ ティック換算 <3000円 {r_te['tick_lo']:.2f} / "
          f">=3000円 {r_te['tick_hi']:.2f}  "
          f"(件数 {r_te['n_lo']:,} / {r_te['n_hi']:,})")


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
    ap.add_argument("--select", choices=["rank-first", "filter-first"],
                    default="rank-first",
                    help="売買代金 上位50位をどの母集団で数えるか。"
                         "filter-first は前日条件を満たす銘柄の中で上位50")
    ap.add_argument("--stop-grid", dest="stop_grid", action="store_true",
                    help="① TRAIN で損切りのグリッドを延長する (形を見るだけ)")
    ap.add_argument("--stop-test", dest="stop_test", action="store_true",
                    help="② TEST で sm=0.1 だけを1回確認する")
    ap.add_argument("--train-from", dest="train_from", default="2015-02-01",
                    help="TRAIN の開始日。参照実装と母集団を揃えるため")
    ap.add_argument("--risk", action="store_true",
                    help="サイズを決めるための ES と、ボラ回帰を1本出す")
    ap.add_argument("--both", action="store_true",
                    help="N と鏡像を同時に建てた場合を測る")
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
    panel = build_panel(syms, args.workers, args.select)
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
    if args.stop_grid or args.stop_test:
        te = f"{TRAIN_END:%Y-%m-%d}"
        if args.stop_grid:
            stop_grid(panel, args.side, args.train_from, te)
        if args.stop_test:
            stop_test(panel, args.side, args.train_from, te)
        return 0
    if args.both:
        combined(panel)
        return 0
    if args.risk:
        t = apply_sizing(sig, "fixed")
        risk_report(t)
        vol_regression(sig, t, panel)
        return 0
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
