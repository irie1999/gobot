"""
daytrade_morning.py  ―  朝特化型デイトレ戦略 4本立て
==================================================================
寄付〜前場前半の値動きを捉える4戦略を統合。

【戦略一覧】
  A: Gap & Run         ギャップアップ+連続陽線 → 追随
  B: First 15min ORB   9:00-9:15 の15分ORB
  C: Opening Drive     9:00-9:15 連続陽線3本 → 短期決済
  D: Morning Composite ギャップ+モメンタム+出来高スコア

【使い方】
  python daytrade_morning.py --strategy gap       # Gap & Run
  python daytrade_morning.py --strategy orb15     # 15min ORB
  python daytrade_morning.py --strategy drive     # Opening Drive
  python daytrade_morning.py --strategy composite # Composite
  python daytrade_morning.py --strategy all       # 4戦略全て
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from _open_html import open_html
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

BUDGET = 600_000
MAX_RISK = 6_000
AM_START = dtime(9, 0)
FORCE_CLOSE = dtime(14, 55)
TRAILING_TRIGGER = 0.5


# ─────────────────────────────────────────────────────────────
# 戦略A: Gap & Run
# ─────────────────────────────────────────────────────────────

def strategy_gap_run(day_df: pd.DataFrame, prev_close: float | None):
    """ギャップアップ+連続陽線で追随買い。"""
    if prev_close is None or prev_close <= 0:
        return None
    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    vols   = day_df["volume"].to_numpy(dtype=float)
    times  = day_df.index

    if len(day_df) < 5:
        return None

    day_open = opens[0]
    gap_pct = (day_open - prev_close) / prev_close * 100
    # ギャップアップ 0.5-3% に限定
    if gap_pct < 0.5 or gap_pct > 3.0:
        return None

    # 9:00-9:10 で連続陽線2本以上
    am_10 = dtime(9, 10)
    early_bars = [i for i in range(len(day_df)) if times[i].time() <= am_10]
    if len(early_bars) < 2:
        return None

    # 直近2本が陽線か
    bullish_count = sum(1 for i in early_bars[-2:] if closes[i] > opens[i])
    if bullish_count < 2:
        return None

    # エントリー: 2本目陽線の次バー寄付
    entry_idx = early_bars[-1] + 1
    if entry_idx >= len(day_df):
        return None

    entry_p = opens[entry_idx]
    entry_dt = times[entry_idx]
    stop_p = day_open  # ギャップ埋めで撤退
    if entry_p <= stop_p:
        return None
    target_p = entry_p * (1 + gap_pct / 100 * 1.5)
    qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)

    return _simulate_exit(day_df, entry_idx, entry_p, entry_dt,
                          stop_p, target_p, qty, "Gap&Run")


# ─────────────────────────────────────────────────────────────
# 戦略B: First 15-min ORB
# ─────────────────────────────────────────────────────────────

def strategy_orb15(day_df: pd.DataFrame, prev_close: float | None):
    """9:00-9:15 の15分ORB。"""
    or_end = dtime(9, 15)
    or_bars = day_df[day_df.index.time <= or_end]
    rest = day_df[day_df.index.time > or_end]
    if len(or_bars) < 2 or rest.empty:
        return None

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w = or_hi - or_lo
    if or_w <= 0:
        return None

    opens = day_df["open"].to_numpy(dtype=float)
    if prev_close and prev_close > 0:
        if abs(opens[0] - prev_close) / prev_close * 100 > 2.0:
            return None

    # 出来高確認: OR中の平均出来高
    or_avg_vol = or_bars["volume"].mean()

    highs = day_df["high"].to_numpy(dtype=float)
    lows  = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    vols = day_df["volume"].to_numpy(dtype=float)
    times = day_df.index

    # OR期間後を探索
    for i in range(len(day_df)):
        if times[i].time() <= or_end:
            continue
        # エントリー締切: 11:00
        if times[i].time() >= dtime(11, 0):
            return None
        # OR高値ブレイク + 出来高確認
        if closes[i] > or_hi and vols[i] > or_avg_vol * 1.5:
            if i + 1 >= len(day_df):
                return None
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            stop_p = or_lo
            if entry_p <= stop_p:
                return None
            target_p = entry_p + or_w * 2.0
            qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)
            return _simulate_exit(day_df, i + 1, entry_p, entry_dt,
                                  stop_p, target_p, qty, "ORB15")
    return None


# ─────────────────────────────────────────────────────────────
# 戦略C: Opening Drive (連続陽線3本)
# ─────────────────────────────────────────────────────────────

def strategy_opening_drive(day_df: pd.DataFrame, prev_close: float | None):
    """9:00-9:15 で連続陽線3本 → 買い。"""
    opens  = day_df["open"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    vols   = day_df["volume"].to_numpy(dtype=float)
    times  = day_df.index

    if len(day_df) < 5:
        return None

    if prev_close and prev_close > 0:
        if abs(opens[0] - prev_close) / prev_close * 100 > 2.0:
            return None

    # 9:00-9:15 の最初3本を確認
    early_idxs = [i for i in range(len(day_df))
                  if times[i].time() <= dtime(9, 15)]
    if len(early_idxs) < 3:
        return None

    first_3 = early_idxs[:3]
    # 全て陽線 + 出来高増加
    all_bullish = all(closes[i] > opens[i] for i in first_3)
    vol_increasing = all(vols[first_3[j]] >= vols[first_3[j-1]] * 0.8
                         for j in range(1, 3))

    if not (all_bullish and vol_increasing):
        return None

    # エントリー: 3本目の次
    entry_idx = first_3[-1] + 1
    if entry_idx >= len(day_df):
        return None

    entry_p = opens[entry_idx]
    entry_dt = times[entry_idx]
    stop_p = entry_p * (1 - 0.005)  # -0.5%損切り
    target_p = entry_p * (1 + 0.015)  # +1.5%目標
    qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)

    return _simulate_exit(day_df, entry_idx, entry_p, entry_dt,
                          stop_p, target_p, qty, "OpenDrive",
                          force_close=dtime(10, 30))


# ─────────────────────────────────────────────────────────────
# 戦略D: Morning Composite Score
# ─────────────────────────────────────────────────────────────

def strategy_composite(day_df: pd.DataFrame, prev_close: float | None,
                        avg_vol_20d: float | None = None):
    """ギャップ+モメンタム+出来高の総合スコア。"""
    if prev_close is None or prev_close <= 0:
        return None

    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    vols   = day_df["volume"].to_numpy(dtype=float)
    times  = day_df.index

    if len(day_df) < 4:
        return None

    day_open = opens[0]
    gap_pct = (day_open - prev_close) / prev_close * 100

    # 9:00-9:15 の最初3本
    early_idxs = [i for i in range(len(day_df))
                  if times[i].time() <= dtime(9, 15)]
    if len(early_idxs) < 3:
        return None

    first_3 = early_idxs[:3]

    # スコア計算
    # ① ギャップスコア (0-40): -1〜+3% を 0〜40点
    gap_score = max(0, min(40, (gap_pct + 1) / 4 * 40))

    # ② モメンタムスコア (0-30): 3本中の陽線比率
    bullish_ratio = sum(1 for i in first_3 if closes[i] > opens[i]) / 3
    mom_score = bullish_ratio * 30

    # ③ 出来高スコア (0-30): 20日平均との比率
    if avg_vol_20d and avg_vol_20d > 0:
        vol_ratio = sum(vols[i] for i in first_3) / (avg_vol_20d / (6 * 60 / 5) * 3)
        vol_score = min(30, vol_ratio * 15)
    else:
        # フォールバック: その日の平均出来高との比
        day_avg = vols.mean()
        if day_avg > 0:
            vol_ratio = sum(vols[i] for i in first_3) / day_avg / 3
            vol_score = min(30, vol_ratio * 15)
        else:
            vol_score = 0

    total = gap_score + mom_score + vol_score
    if total < 70:
        return None

    entry_idx = first_3[-1] + 1
    if entry_idx >= len(day_df):
        return None

    entry_p = opens[entry_idx]
    entry_dt = times[entry_idx]
    stop_p = entry_p * (1 - 0.01)  # -1%損切り
    target_p = entry_p * (1 + 0.02)  # +2%目標
    qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)

    return _simulate_exit(day_df, entry_idx, entry_p, entry_dt,
                          stop_p, target_p, qty, "Composite",
                          force_close=dtime(11, 30))


# ─────────────────────────────────────────────────────────────
# 共通決済シミュレータ
# ─────────────────────────────────────────────────────────────

def _simulate_exit(day_df, entry_idx: int, entry_p: float,
                   entry_dt, stop_p: float, target_p: float,
                   qty: int, strategy: str,
                   force_close: dtime = FORCE_CLOSE):
    """エントリー後の決済をシミュレート (トレーリング付き)。"""
    highs = day_df["high"].to_numpy(dtype=float)
    lows = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times = day_df.index

    orig_stop = stop_p
    trailing = False

    for i in range(entry_idx, len(day_df)):
        t = times[i].time()
        hi, lo, cl = highs[i], lows[i], closes[i]

        if t >= force_close:
            exit_p = cl
            reason = "引け強制"
            return _build_trade(entry_dt, times[i], entry_p, exit_p,
                                orig_stop, target_p, qty, strategy, reason)

        # トレーリング: 含み益50%で建値撤退
        if not trailing and target_p > entry_p:
            if (cl - entry_p) / (target_p - entry_p) >= TRAILING_TRIGGER:
                stop_p = entry_p
                trailing = True

        if lo <= stop_p and hi >= target_p:
            exit_p = stop_p
            reason = "建値撤退" if trailing else "損切り"
            return _build_trade(entry_dt, times[i], entry_p, exit_p,
                                orig_stop, target_p, qty, strategy, reason)
        if hi >= target_p:
            return _build_trade(entry_dt, times[i], entry_p, target_p,
                                orig_stop, target_p, qty, strategy, "目標達成")
        if lo <= stop_p:
            exit_p = stop_p
            reason = "建値撤退" if trailing else "損切り"
            return _build_trade(entry_dt, times[i], entry_p, exit_p,
                                orig_stop, target_p, qty, strategy, reason)

    # 最終バーで強制決済
    return _build_trade(entry_dt, times[-1], entry_p, float(closes[-1]),
                        orig_stop, target_p, qty, strategy, "引け強制")


def _build_trade(entry_dt, exit_dt, entry_p, exit_p,
                 stop_p, target_p, qty, strategy, reason):
    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p,
        stop_p=stop_p, target_p=target_p,
        qty=qty, pnl=pnl, pct=pct,
        strategy=strategy, reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# バックテスト (銘柄単位)
# ─────────────────────────────────────────────────────────────

STRATEGY_FNS = {
    "gap": strategy_gap_run,
    "orb15": strategy_orb15,
    "drive": strategy_opening_drive,
    "composite": strategy_composite,
}


def backtest_symbol(sym, name, df, strat_name):
    fn = STRATEGY_FNS.get(strat_name)
    if fn is None:
        return None
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None

    trades = []
    prev_close = None
    for d in dates:
        if strat_name == "composite":
            # 20日平均出来高も渡す
            recent_20 = dates[max(0, dates.index(d) - 20):dates.index(d)]
            if recent_20:
                vols = [daily[rd]["volume"].sum() for rd in recent_20]
                avg_vol = sum(vols) / len(vols) if vols else None
            else:
                avg_vol = None
            t = fn(daily[d], prev_close, avg_vol)
        else:
            t = fn(daily[d], prev_close)
        if t:
            trades.append(t)
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades, strategy=strat_name)


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=dd)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def build_html(strat_results, days, budget, source):
    """複数戦略の比較HTML。"""
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    strat_labels = {
        "gap": ("Gap & Run", "#10b981"),
        "orb15": ("First 15min ORB", "#3b82f6"),
        "drive": ("Opening Drive", "#f59e0b"),
        "composite": ("Composite Score", "#ec4899"),
    }

    # 戦略サマリー
    summary_rows = ""
    for strat_key, (label, color) in strat_labels.items():
        if strat_key not in strat_results:
            continue
        all_trades = [t for item in strat_results[strat_key] for t in item["trades"]]
        s = calc_stats(all_trades, budget)
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        summary_rows += f"""<tr>
          <td style="color:{color};font-weight:700">{label}</td>
          <td>{s['n']}</td><td>{s['wins']}</td>
          <td>{s['win_rate']:.1f}%</td><td>{_pf(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}</td>
          <td class="profit">{s['avg_win']:+,.0f}</td>
          <td class="loss">{s['avg_loss']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.1f}%</td></tr>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>朝特化戦略4本 — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#fbbf24;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#fbbf24;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #fbbf24;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
</style></head><body>
<h1>朝特化デイトレ戦略 4本比較</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円<br>
A: Gap&Run (ギャップ追随) / B: 15min ORB / C: Opening Drive (連続陽線) / D: Composite (総合スコア)</p>
<h2>戦略サマリー</h2>
<table><thead><tr><th>戦略</th><th>取引</th><th>勝</th><th>勝率</th><th>PF</th>
<th>総損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
<tbody>{summary_rows}</tbody></table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="朝特化デイトレ戦略")
    parser.add_argument("--strategy",
                        choices=["gap", "orb15", "drive", "composite", "all"],
                        default="all")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    targets = DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"朝特化戦略: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
          flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    strats = list(STRATEGY_FNS.keys()) if args.strategy == "all" else [args.strategy]
    strat_results = {}
    for strat in strats:
        print(f"\n[{strat}] バックテスト...", flush=True)
        items = []
        for sym, name in targets:
            if sym not in fetched:
                continue
            r = backtest_symbol(sym, name, fetched[sym], strat)
            if r and r["trades"]:
                items.append(r)
        all_trades = [t for it in items for t in it["trades"]]
        s = calc_stats(all_trades, args.budget)
        print(f"  取引:{s['n']}  勝率:{s['win_rate']:.1f}%  "
              f"PF:{_pf(s['pf'])}  損益:{s['total_pnl']:+,.0f}  DD:{s['max_dd']:+.1f}%")
        strat_results[strat] = items

    # コンソールサマリー
    labels = {"gap": "Gap & Run", "orb15": "15min ORB",
              "drive": "Opening Drive", "composite": "Composite"}
    print()
    print("=" * 70)
    print(f"  朝特化戦略 比較サマリー ({args.days}日)")
    print("=" * 70)
    print(f"  {'戦略':<18} {'取引':>4} {'勝率':>5} {'PF':>6} {'損益':>12} {'DD':>7}")
    print("-" * 70)
    for strat in strats:
        items = strat_results.get(strat, [])
        all_trades = [t for it in items for t in it["trades"]]
        s = calc_stats(all_trades, args.budget)
        print(f"  {labels[strat]:<18} {s['n']:>4} "
              f"{s['win_rate']:>4.0f}% {_pf(s['pf']):>6} "
              f"{s['total_pnl']:>+12,.0f} {s['max_dd']:>+6.1f}%")
    print("=" * 70)

    out = Path(f"daytrade_morning_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(strat_results, args.days, args.budget, args.source),
                   encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
