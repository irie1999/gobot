"""
meanrev_lab.py — 横ばい(レンジ)相場向け 平均回帰(逆張り)戦略の実験ハーネス。

現行(上げ用・順張り)とは独立した実験場。**ライブエンジン(run_limit_backtest)には
一切触れない**ので、日々のシグナルには影響しない。ここで勝つ構成を見つけてから、
check_signals_* / エンジンに正式移植する、という段取り(CLAUDE.md §13 の検証優先方針)。

検証する構成(グリッド):
  シグナル(行き過ぎの測り方4種) × 方向(ロング/ショート)
    IBS   : 足内位置 (終値が安値/高値寄り)
    RSI2r : 短期モメンタム売られ/買われすぎ (MA200フィルタ無し=横ばい版)
    BB    : ボリンジャー±2σ (ボラ正規化乖離)
    STO   : ストキャス %K (N日レンジ内の位置)
  × 出口タイプ2種
    atr  : target = order ± ATR×tm  (既存流用)
    mean : target = MA20 到達で利確 (平均回帰を素直に表現)
  × プリセット2種
    tight: em0.5 sm1.5 tm1.0 保有5日  (高勝率・小利)
    bal  : em0.5 sm1.5 tm1.5 保有6日  (バランス1R)

各構成を「大局レジーム=横ばいの日に出たシグナルだけ」と「全レジーム」で集計して
比較する。横ばいで勝ち・トレンドで負けるなら、レジームゲートの価値が数字で見える。

使い方:
  python meanrev_lab.py --limit 300 --since 2014-01-01        # 300銘柄で全構成
  python meanrev_lab.py --symbols symbols_listed_prime.py     # ユニバース明示
  python meanrev_lab.py --max-price 10000 --min-price 1000    # 価格フィルタ
  python meanrev_lab.py --signal IBS --signal RSI2r           # 一部シグナルだけ
  python meanrev_lab.py --workers 8
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import backtest_limit_entry as ble
from backtest_limit_entry import (
    fetch, calc_qty,
    SLIPPAGE_STOP_PCT, SLIPPAGE_LIMIT_PCT, FEE_PCT_ONE_WAY,
    MIN_PRICE, MAX_PRICE, MAX_ATR_RATIO,
)
import nikkei_analysis as na


# ── 指標 ─────────────────────────────────────────────────────────
def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    c, h, l = df["close"], df["high"], df["low"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def _signals(df: pd.DataFrame) -> dict:
    """各シグナルの (ロングbool, ショートbool) を返す。"""
    c, h, l = df["close"], df["high"], df["low"]
    out = {}

    # IBS = (終値-安値)/(高値-安値)
    rng = (h - l).replace(0, np.nan)
    ibs = ((c - l) / rng).fillna(0.5)
    out["IBS"] = (ibs < 0.15, ibs > 0.85)

    # RSI(2) Wilder α=0.5 (MA200フィルタ無しの横ばい版)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    out["RSI2r"] = (rsi2 < 10, rsi2 > 90)

    # ボリンジャー ±2σ
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    out["BB"] = (c < ma20 - 2.0 * sd20, c > ma20 + 2.0 * sd20)

    # ストキャス %K(14) → 3日平滑 (slow %K)
    ll = l.rolling(14).min()
    hh = h.rolling(14).max()
    k = (c - ll) / (hh - ll).replace(0, np.nan) * 100
    ks = k.rolling(3).mean()
    out["STO"] = (ks < 20, ks > 80)

    return out


# ── プリセット & 出口タイプ ──────────────────────────────────────
PRESETS = {
    #        em   sm   tm   max_hold
    "tight": (0.5, 1.5, 1.0, 5),
    "bal":   (0.5, 1.5, 1.5, 6),
}
EXIT_MODES = ["atr", "mean"]
EXPIRE = 3   # 指値の有効日数(シグナルから)


def _run_mr(df: pd.DataFrame, sig_long: pd.Series, sig_short: pd.Series,
            direction: str, em: float, sm: float, tm: float,
            exit_mode: str, max_hold: int) -> list[dict]:
    """単一銘柄・単一構成の平均回帰バックテスト(同時1ポジション)。
    ロング=押し目を指値買い / ショート=吹き値を指値売り。損切りは終値判定(close)。"""
    atr = _atr(df).to_numpy()
    ma20 = df["close"].rolling(20).mean().to_numpy()
    op = df["open"].to_numpy(); hi = df["high"].to_numpy()
    lo = df["low"].to_numpy();  cl = df["close"].to_numpy()
    idx = df.index
    sig = (sig_long if direction == "long" else sig_short).to_numpy()
    is_long = direction == "long"

    trades: list[dict] = []
    pending = None   # {lp,sp,tp,expire,sig_i}
    pos = None       # {ep,sp,tp,start,sig_dt}
    n = len(df)

    for i in range(1, n):
        a = atr[i - 1]
        if not np.isfinite(a) or a <= 0:
            continue

        # ── 1. 保有ポジションの決済 ──
        if pos is not None:
            hold = i - pos["start"]
            m = ma20[i]
            if is_long:
                tp = pos["tp"] if exit_mode == "atr" else m
                hit_t = np.isfinite(tp) and hi[i] >= tp
                hit_s = cl[i] <= pos["sp"]
            else:
                tp = pos["tp"] if exit_mode == "atr" else m
                hit_t = np.isfinite(tp) and lo[i] <= tp
                hit_s = cl[i] >= pos["sp"]

            xp = xr = None
            if hit_t:
                xp = tp; xr = "target"
            elif hit_s:
                xp = cl[i] * (1 - SLIPPAGE_STOP_PCT) if is_long else cl[i] * (1 + SLIPPAGE_STOP_PCT)
                xr = "stop"
            elif hold >= max_hold:
                xp = cl[i]; xr = "timecut"

            if xp is not None:
                ep = pos["ep"]; qty = pos["qty"]
                fee = (ep + xp) * qty * FEE_PCT_ONE_WAY
                pnl = ((xp - ep) if is_long else (ep - xp)) * qty - fee
                trades.append(dict(sig_dt=pos["sig_dt"], pnl=pnl, hold=hold, reason=xr))
                pos = None

        # ── 2. 新規シグナル → 指値注文 (flat のときだけ) ──
        if pos is None and pending is None and bool(sig[i - 1]):
            cp = float(cl[i - 1])
            if MIN_PRICE <= cp <= MAX_PRICE and a / cp <= MAX_ATR_RATIO:
                if is_long:
                    lp = cp - a * em            # 押し目 (前日終値より下)
                    sp = lp - a * sm; tp = lp + a * tm
                    ok = lp > 0 and sp > 0 and tp > lp
                else:
                    lp = cp + a * em            # 吹き値 (前日終値より上)
                    sp = lp + a * sm; tp = lp - a * tm
                    ok = lp > 0 and tp > 0 and tp < lp and sp > lp
                if ok:
                    pending = {"lp": lp, "sp": sp, "tp": tp,
                               "expire": i + EXPIRE, "sig_i": i - 1}

        # ── 3. pending の約定判定 (T+1 の当バーから) ──
        if pending is not None and pos is None:
            if i > pending["expire"]:
                pending = None
            else:
                filled = (lo[i] <= pending["lp"]) if is_long else (hi[i] >= pending["lp"])
                if filled:
                    ep = round(pending["lp"] * (1 + SLIPPAGE_LIMIT_PCT))  # 指値=スリッページ0
                    pos = {"ep": ep, "sp": pending["sp"], "tp": pending["tp"],
                           "qty": calc_qty(ep, pending["sp"]),
                           "start": i, "sig_dt": idx[pending["sig_i"]]}
                    pending = None
    return trades


# ── 大局レジーム(日次)系列 ───────────────────────────────────────
def _regime_series(years: int) -> pd.Series | None:
    """日経終値から各日の大局レジーム(up/sideways/down)を先読みなしで返す。"""
    c = na.fetch_n225(years)
    if c is None or len(c) < 260:
        return None
    c = c.sort_index()
    ma200 = c.rolling(200).mean()
    slope = ma200.pct_change(20) * 100
    er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)
    above = c >= ma200

    reg = pd.Series(index=c.index, dtype=object)
    for i in range(len(c)):
        if not np.isfinite(ma200.iloc[i]) or not np.isfinite(er.iloc[i]):
            reg.iloc[i] = "?"
        elif er.iloc[i] < 0.20:
            reg.iloc[i] = "sideways"
        elif above.iloc[i] and slope.iloc[i] > 0:
            reg.iloc[i] = "up"
        elif (not above.iloc[i]) and slope.iloc[i] < 0:
            reg.iloc[i] = "down"
        else:
            reg.iloc[i] = "sideways"
    return reg


def _regime_at(reg: pd.Series, d) -> str:
    """日付 d 時点(以前で直近)のレジーム。"""
    try:
        return reg.asof(pd.Timestamp(d))
    except Exception:
        return "?"


# ── 集計 ─────────────────────────────────────────────────────────
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
    ap.add_argument("--symbols", default=None, help="ユニバースファイル (省略時は自動検出)")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ (0=全部)")
    ap.add_argument("--since", default="2014-01-01", help="この日付まで遡ってデータ取得")
    ap.add_argument("--years", type=int, default=15, help="日経レジーム系列の取得年数")
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--max-price", type=float, default=1e9)
    ap.add_argument("--signal", action="append", default=None,
                    help="対象シグナル (IBS/RSI2r/BB/STO)。複数可。省略時は全部")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    bt_days = (datetime.now().date() - since).days + 400

    import scan_walkforward as swf
    universe, src = swf.load_universe(args.symbols)
    if args.limit > 0:
        universe = universe[: args.limit]
    print(f"ユニバース: {src} ({len(universe)}銘柄) / since={since} / エンジン:meanrev_lab(独立)")

    reg = _regime_series(args.years)
    if reg is None:
        print("[ERROR] 日経レジーム系列の取得に失敗しました")
        return
    _rc = reg.value_counts()
    print(f"日経レジーム日数: 上げ{_rc.get('up',0)} / 横ばい{_rc.get('sideways',0)} / 下げ{_rc.get('down',0)}\n")

    sig_names = args.signal or ["IBS", "RSI2r", "BB", "STO"]
    directions = ["long", "short"]

    # 構成キー -> {"all": [trades], "sideways": [trades]}
    results: dict = defaultdict(lambda: {"all": [], "sideways": []})

    def _work(sym_name):
        sym, name = sym_name[0], (sym_name[1] if len(sym_name) > 1 else "")
        df = fetch(sym, bt_days, min_start_date=since)
        if df is None or len(df) < 260:
            return None
        px = float(df["close"].iloc[-1])
        if not (args.min_price <= px <= args.max_price):
            return None
        sigs = _signals(df)
        local: dict = defaultdict(list)
        for sname in sig_names:
            sl, ss = sigs[sname]
            for direction in directions:
                for pkey, (em, sm, tm, mh) in PRESETS.items():
                    for xmode in EXIT_MODES:
                        trs = _run_mr(df, sl, ss, direction, em, sm, tm, xmode, mh)
                        key = (sname, direction, xmode, pkey)
                        local[key].extend(trs)
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

    # ── レポート: 横ばい損益で降順ソート ──
    rows = []
    for key, buckets in results.items():
        sw = _stats(buckets["sideways"])
        al = _stats(buckets["all"])
        rows.append((key, sw, al))
    rows.sort(key=lambda r: -r[1]["pnl"])

    def _pf(v):
        return "∞" if v == float("inf") else f"{v:.2f}"

    print("\n" + "=" * 96)
    print("  平均回帰(逆張り)構成ランキング  ※横ばいレジームの日に出たシグナルだけの損益で降順")
    print("=" * 96)
    hdr = (f"{'シグナル':<8}{'方向':<6}{'出口':<6}{'presets':<7}"
           f"|{'横ばい件':>7}{'勝率':>6}{'PF':>6}{'損益':>12}{'保有':>5}"
           f"  |{'全件':>6}{'勝率':>6}{'PF':>6}{'損益':>12}")
    print(hdr)
    print("-" * 96)
    for (sname, direction, xmode, pkey), sw, al in rows:
        dlabel = "ロング" if direction == "long" else "ｼｮｰﾄ"
        print(f"{sname:<8}{dlabel:<6}{xmode:<6}{pkey:<7}"
              f"|{sw['n']:>7}{sw['wr']:>5.0f}%{_pf(sw['pf']):>6}{sw['pnl']:>+12,.0f}{sw['hold']:>5.1f}"
              f"  |{al['n']:>6}{al['wr']:>5.0f}%{_pf(al['pf']):>6}{al['pnl']:>+12,.0f}")

    print("-" * 96)
    print("読み方: 横ばい損益がプラス かつ 横ばいPF>全件PF なら『横ばい特化で機能』の証拠。")
    print("        横ばいだけプラスで全件マイナスなら、レジームゲート(横ばい時のみ発動)の価値あり。")


if __name__ == "__main__":
    main()
