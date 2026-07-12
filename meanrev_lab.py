"""
meanrev_lab.py — 横ばい(レンジ)相場向け 平均回帰(逆張り)戦略の実験ハーネス。

現行(上げ用・順張り)とは独立した実験場。**ライブエンジン(run_limit_backtest)には
一切触れない**ので、日々のシグナルには影響しない。ここで勝つ構成を見つけてから、
check_signals_* / エンジンに正式移植する、という段取り(CLAUDE.md §13 の検証優先方針)。

── v1 の結果(全銀柄・生逆張り)= 32構成すべて負け(PF 0.4-0.7)。原因は3つ:
   (1) 押し目を指値で下に買う → 落ちるナイフを掴む
   (2) ATR損切りのスリッページで出血
   (3) シグナルが浅く選別力不足(勝率50-56%止まり)

── v2(このファイル)の改良ノブ:
   entry_style:
     dip    = 前日終値-ATR×em を指値買い(v1と同じ・ナイフ掴み)
     bounce = 売られすぎ翌日、前日高値を上抜けたら逆指値買い(反転確認=ナイフ回避)
   exit_style:
     atr  = target=order±ATR×tm / stop=order∓ATR×sm  (v1と同じ)
     mean = target=MA10到達で利確 / 損切りは広いディザスター(ATR×sm、滅多に当たらない)
   signal 閾値を深く(選別力↑): IBS<0.10 / RSI2<5 / BB 2.5σ / STO<10
   コスト: --fee / --slip で信用(0.03%)想定に下げて感度を見られる

各構成を「大局レジーム=横ばいの日に出たシグナルだけ」と「全レジーム」で集計して比較。

使い方:
  python meanrev_lab.py --limit 300 --since 2014-01-01 --max-price 10000 --min-price 1000 --workers 8
  python meanrev_lab.py --limit 300 --fee 0.0003 --slip 0.003     # 信用コスト想定で再テスト
  python meanrev_lab.py --signal IBS --signal RSI2r               # 一部シグナルだけ
  python meanrev_lab.py --range-band 0.20                         # MA200±20%内の押し目だけ(ナイフ更に回避)
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import pandas as pd

import backtest_limit_entry as ble
from backtest_limit_entry import (
    fetch, calc_qty,
    MIN_PRICE, MAX_PRICE, MAX_ATR_RATIO,
)
import nikkei_analysis as na


# ── 指標 ─────────────────────────────────────────────────────────
def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    c, h, l = df["close"], df["high"], df["low"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


# シグナル閾値 (v2: 深く=選別力↑)
TH_IBS_L, TH_IBS_S = 0.10, 0.90
TH_RSI_L, TH_RSI_S = 5, 95
TH_BB_K = 2.5
TH_STO_L, TH_STO_S = 10, 90


def _signals(df: pd.DataFrame) -> dict:
    """各シグナルの (ロングbool, ショートbool) を返す。"""
    c, h, l = df["close"], df["high"], df["low"]
    out = {}

    rng = (h - l).replace(0, np.nan)
    ibs = ((c - l) / rng).fillna(0.5)
    out["IBS"] = (ibs < TH_IBS_L, ibs > TH_IBS_S)

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    out["RSI2r"] = (rsi2 < TH_RSI_L, rsi2 > TH_RSI_S)

    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    out["BB"] = (c < ma20 - TH_BB_K * sd20, c > ma20 + TH_BB_K * sd20)

    ll = l.rolling(14).min()
    hh = h.rolling(14).max()
    k = (c - ll) / (hh - ll).replace(0, np.nan) * 100
    ks = k.rolling(3).mean()
    out["STO"] = (ks < TH_STO_L, ks > TH_STO_S)

    return out


# ── 構成 (coherent な入口×出口の組合せのみ) ──────────────────────
# (entry_style, exit_style, name, params)
#   dip+atr1R  : 押し目を指値買い→対称1R利確  (v1流用の素朴逆張り)
#   dip+mean   : 押し目を指値買い→MA10回帰で利確+広いディザスター損切り(ATR×3)
#   bounce+atr : 反転確認(前日高安ブレイク)で逆指値→ATR利確  (ナイフ回避)
#   bounce+wide: 反転確認→広め利確(tm3.0)で伸ばす         (ナイフ回避+トレンド乗り)
# ※ bounce+mean は除外: 反転買いは既にMA10より上に居るため中心回帰出口が退化する
COMBOS = [
    ("dip",    "atr",  "atr1R", dict(em=0.5, sm=2.0, tm=2.0, mh=6)),
    ("dip",    "mean", "mean",  dict(em=0.5, sm=3.0, tm=0.0, mh=5)),
    ("bounce", "atr",  "atr1R", dict(em=0.5, sm=2.0, tm=2.0, mh=6)),
    ("bounce", "atr",  "wide",  dict(em=0.0, sm=2.0, tm=3.0, mh=8)),
]
EXPIRE = 3


def _run_mr(df, sig_long, sig_short, direction, entry_style, exit_style,
            em, sm, tm, max_hold, fee, slip, range_band, stock_ok=None) -> list[dict]:
    """単一銘柄・単一構成の平均回帰バックテスト(同時1ポジション)。損切りは終値判定。
    stock_ok: 各バーで発注を許可するか(銘柄自身がレンジの日だけ True)。None=常に許可。"""
    atr = _atr(df).to_numpy()
    ma10 = df["close"].rolling(10).mean().to_numpy()
    ma200 = df["close"].rolling(200).mean().to_numpy()
    hi = df["high"].to_numpy(); lo = df["low"].to_numpy(); cl = df["close"].to_numpy()
    idx = df.index
    sig = (sig_long if direction == "long" else sig_short).to_numpy()
    is_long = direction == "long"

    trades: list[dict] = []
    pending = None
    pos = None
    n = len(df)

    for i in range(1, n):
        a = atr[i - 1]
        if not np.isfinite(a) or a <= 0:
            continue

        # ── 1. 決済 ──
        if pos is not None:
            hold = i - pos["start"]
            if is_long:
                if exit_style == "mean":
                    # 中心回帰: MA10 が約定値より上(=上に戻る余地あり)のときだけ有効目標
                    tgt = ma10[i]
                    hit_t = np.isfinite(tgt) and tgt > pos["ep"] and hi[i] >= tgt
                else:
                    tgt = pos["tp"]; hit_t = hi[i] >= tgt
                hit_s = cl[i] <= pos["sp"]
            else:
                if exit_style == "mean":
                    tgt = ma10[i]
                    hit_t = np.isfinite(tgt) and tgt < pos["ep"] and lo[i] <= tgt
                else:
                    tgt = pos["tp"]; hit_t = lo[i] <= tgt
                hit_s = cl[i] >= pos["sp"]

            xp = xr = None
            if hit_t:
                xp = tgt; xr = "target"
            elif hit_s:
                xp = cl[i] * (1 - slip) if is_long else cl[i] * (1 + slip)
                xr = "stop"
            elif hold >= max_hold:
                xp = cl[i]; xr = "timecut"

            if xp is not None:
                ep = pos["ep"]; qty = pos["qty"]
                f = (ep + xp) * qty * fee
                pnl = ((xp - ep) if is_long else (ep - xp)) * qty - f
                trades.append(dict(sig_dt=pos["sig_dt"], pnl=pnl, hold=hold, reason=xr))
                pos = None

        # ── 2. 新規シグナル → 注文 ──
        stock_ranging = (stock_ok is None) or bool(stock_ok[i - 1])
        if pos is None and pending is None and bool(sig[i - 1]) and stock_ranging:
            cp = float(cl[i - 1])
            m2 = ma200[i - 1]
            in_range = (not range_band) or (np.isfinite(m2) and m2 > 0
                        and abs(cp / m2 - 1.0) <= range_band)
            if MIN_PRICE <= cp <= MAX_PRICE and a / cp <= MAX_ATR_RATIO and in_range:
                if entry_style == "bounce":
                    # 反転確認: 前日(売られすぎ)の高値/安値をブレイクしたら逆指値
                    trig = float(hi[i - 1]) if is_long else float(lo[i - 1])
                else:  # dip: 前日終値からATR×em 逆方向に指値
                    trig = cp - a * em if is_long else cp + a * em
                if is_long:
                    sp = trig - a * sm; tp = trig + a * tm
                    ok = trig > 0 and sp > 0 and (exit_style == "mean" or tp > trig)
                else:
                    sp = trig + a * sm; tp = trig - a * tm
                    ok = trig > 0 and (exit_style == "mean" or (0 < tp < trig)) and sp > trig
                if ok:
                    pending = {"trig": trig, "sp": sp, "tp": tp,
                               "expire": i + EXPIRE, "sig_i": i - 1}

        # ── 3. 約定判定 ──
        if pending is not None and pos is None:
            if i > pending["expire"]:
                pending = None
            else:
                trig = pending["trig"]
                if entry_style == "bounce":
                    # 逆指値: ロング=高値≥trig / ショート=安値≤trig で約定(スリッページあり)
                    filled = (hi[i] >= trig) if is_long else (lo[i] <= trig)
                    ep = round(trig * (1 + slip)) if is_long else round(trig * (1 - slip))
                else:
                    # 指値: ロング=安値≤trig / ショート=高値≥trig で約定(スリッページ0)
                    filled = (lo[i] <= trig) if is_long else (hi[i] >= trig)
                    ep = round(trig)
                if filled:
                    pos = {"ep": ep, "sp": pending["sp"], "tp": pending["tp"],
                           "qty": calc_qty(ep, pending["sp"]),
                           "start": i, "sig_dt": idx[pending["sig_i"]]}
                    pending = None
    return trades


# ── 大局レジーム(日次)系列 ───────────────────────────────────────
def _regime_series(years: int, strict: bool = False) -> pd.Series | None:
    """日経の大局レジーム(先読みなし)。
    strict=True: 横ばいを ER<0.15 かつ |MA200傾き|<0.5% の「本当に方向感なし」に厳格化。
                 (トレンド寄りの日は sideways から外れて up/down/? になる)"""
    c = na.fetch_n225(years)
    if c is None or len(c) < 260:
        return None
    c = c.sort_index()
    ma200 = c.rolling(200).mean()
    slope = ma200.pct_change(20) * 100
    er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)
    above = c >= ma200
    er_th = 0.15 if strict else 0.20
    slope_band = 0.5 if strict else None
    reg = pd.Series(index=c.index, dtype=object)
    for i in range(len(c)):
        if not np.isfinite(ma200.iloc[i]) or not np.isfinite(er.iloc[i]):
            reg.iloc[i] = "?"
        elif er.iloc[i] < er_th and (slope_band is None or abs(slope.iloc[i]) < slope_band):
            reg.iloc[i] = "sideways"
        elif above.iloc[i] and slope.iloc[i] > 0:
            reg.iloc[i] = "up"
        elif (not above.iloc[i]) and slope.iloc[i] < 0:
            reg.iloc[i] = "down"
        else:
            reg.iloc[i] = "?" if strict else "sideways"
    return reg


def _regime_at(reg: pd.Series, d) -> str:
    try:
        return reg.asof(pd.Timestamp(d))
    except Exception:
        return "?"


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "hold": 0.0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gp = sum(wins); gl = -sum(losses)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "wr": len(wins) / n * 100, "pf": pf,
            "pnl": sum(t["pnl"] for t in trades),
            "hold": sum(t["hold"] for t in trades) / n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--since", default="2014-01-01")
    ap.add_argument("--years", type=int, default=15)
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--max-price", type=float, default=1e9)
    ap.add_argument("--signal", action="append", default=None,
                    help="対象シグナル (IBS/RSI2r/BB/STO)。複数可")
    ap.add_argument("--fee", type=float, default=0.001, help="片道手数料(既定0.001=現物. 信用は0.0003)")
    ap.add_argument("--slip", type=float, default=0.005, help="損切り/逆指値スリッページ(既定0.005)")
    ap.add_argument("--range-band", type=float, default=0.0,
                    help="MA200±この割合の押し目だけ採用(例0.20=±20%%内)。0=無効")
    ap.add_argument("--strict-regime", action="store_true",
                    help="横ばい定義を厳格化(日経ER<0.15かつMA200傾き±0.5%%以内)")
    ap.add_argument("--stock-range", type=float, default=0.0,
                    help="銘柄自身の効率比(60日ER)がこの値未満の日だけ発注(例0.25)。0=無効")
    ap.add_argument("--loose", action="store_true",
                    help="低ボラ銘柄(指数ETF)向けに閾値を緩和(IBS<0.15/RSI2<10/BB2.0σ/STO<20)")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    if args.loose:
        global TH_IBS_L, TH_IBS_S, TH_RSI_L, TH_RSI_S, TH_BB_K, TH_STO_L, TH_STO_S
        TH_IBS_L, TH_IBS_S = 0.15, 0.85
        TH_RSI_L, TH_RSI_S = 10, 90
        TH_BB_K = 2.0
        TH_STO_L, TH_STO_S = 20, 80

    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    bt_days = (datetime.now().date() - since).days + 400

    import scan_walkforward as swf
    universe, src = swf.load_universe(args.symbols)
    if args.limit > 0:
        universe = universe[: args.limit]
    print(f"ユニバース: {src} ({len(universe)}銘柄) / since={since} / エンジン:meanrev_lab v2(独立)")
    print(f"コスト: 手数料片道{args.fee*100:.2f}% / スリッページ{args.slip*100:.2f}%"
          f" / range_band={args.range_band or 'off'}"
          f" / strict_regime={'on' if args.strict_regime else 'off'}"
          f" / stock_range={args.stock_range or 'off'}"
          f" / 閾値 IBS<{TH_IBS_L} RSI2<{TH_RSI_L} BB{TH_BB_K}σ STO<{TH_STO_L}")

    reg = _regime_series(args.years, strict=args.strict_regime)
    if reg is None:
        print("[ERROR] 日経レジーム系列の取得に失敗しました")
        return
    _rc = reg.value_counts()
    print(f"日経レジーム日数: 上げ{_rc.get('up',0)} / 横ばい{_rc.get('sideways',0)}"
          f" / 下げ{_rc.get('down',0)} / 除外(?){_rc.get('?',0)}\n")

    sig_names = args.signal or ["IBS", "RSI2r", "BB", "STO"]
    directions = ["long", "short"]
    results: dict = defaultdict(lambda: {"all": [], "sideways": []})

    def _work(sym_name):
        sym = sym_name[0]
        df = fetch(sym, bt_days, min_start_date=since)
        if df is None or len(df) < 260:
            return None
        px = float(df["close"].iloc[-1])
        if not (args.min_price <= px <= args.max_price):
            return None
        sigs = _signals(df)
        # 銘柄自身がレンジか: 60日効率比(ER) が閾値未満の日だけ発注を許可
        stock_ok = None
        if args.stock_range > 0:
            sc = df["close"]
            s_er = (sc - sc.shift(60)).abs() / sc.diff().abs().rolling(60).sum().replace(0, np.nan)
            stock_ok = (s_er < args.stock_range).to_numpy()
        local: dict = defaultdict(list)
        for sname in sig_names:
            sl, ss = sigs[sname]
            for direction in directions:
                for estyle, xstyle, cname, p in COMBOS:
                    trs = _run_mr(df, sl, ss, direction, estyle, xstyle,
                                  p["em"], p["sm"], p["tm"], p["mh"],
                                  args.fee, args.slip, args.range_band, stock_ok)
                    local[(sname, direction, estyle, cname)].extend(trs)
        return local

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_work, sn): sn for sn in universe}
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(universe)}", flush=True)
            try:
                local = fut.result()
            except Exception:
                local = None
            if not local:
                continue
            for key, trs in local.items():
                for t in trs:
                    results[key]["all"].append(t)
                    if _regime_at(reg, t["sig_dt"]) == "sideways":
                        results[key]["sideways"].append(t)

    rows = []
    for key, buckets in results.items():
        rows.append((key, _stats(buckets["sideways"]), _stats(buckets["all"])))
    rows.sort(key=lambda r: -r[1]["pnl"])

    def _pf(v):
        return "∞" if v == float("inf") else f"{v:.2f}"

    print("\n" + "=" * 100)
    print("  平均回帰v2 構成ランキング  ※横ばいレジームの日に出たシグナルだけの損益で降順")
    print("=" * 100)
    print(f"{'シグナル':<8}{'方向':<6}{'入口':<8}{'出口':<7}"
          f"|{'横ばい件':>7}{'勝率':>6}{'PF':>6}{'損益':>13}{'保有':>5}"
          f"  |{'全件':>7}{'勝率':>6}{'PF':>6}{'損益':>13}")
    print("-" * 100)
    for (sname, direction, estyle, cname), sw, al in rows:
        dlabel = "ロング" if direction == "long" else "ｼｮｰﾄ"
        print(f"{sname:<8}{dlabel:<6}{estyle:<8}{cname:<7}"
              f"|{sw['n']:>7}{sw['wr']:>5.0f}%{_pf(sw['pf']):>6}{sw['pnl']:>+13,.0f}{sw['hold']:>5.1f}"
              f"  |{al['n']:>7}{al['wr']:>5.0f}%{_pf(al['pf']):>6}{al['pnl']:>+13,.0f}")
    print("-" * 100)
    print("読み方: 横ばい損益プラス かつ 横ばいPF>1.0 なら本物。bounce(反転確認)がdip(ナイフ掴み)より")
    print("        改善しているか、mean(中心回帰)出口が効くかに注目。PF>1 が1つも無ければ横ばいロングは不可。")


if __name__ == "__main__":
    main()
