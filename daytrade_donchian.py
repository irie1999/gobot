"""
daytrade_donchian.py  ―  デイトレ戦略: Donchian (ultra_freq モード)
==================================================================
【戦略】
  過去8本 (40分) の最高値を更新したバーで順張り買い。
  ウォークフォワード4ウィンドウ検証で唯一 PF > 1.0 を維持した
  プリセット (PF 1.02 / +47k / 28日) を採用。
  DD-40%リスクを認識し、MAX_RISK を半減 (6k→3k) してサイズ調整。

【エントリー条件】
  1. 現バー終値 > 直近8本 (現バー除く) の最高値
  2. 陽線 (close > open)
  3. 時刻 14:30 まで (前場後場とも対象)
  4. ギャップ ≤ 4%
  5. 1日複数トレード対応 (決済後再エントリー可)

【決済】
  損切り: 直近8本の最安値 or エントリー-2.0% の浅い方
  目標: エントリー + (エントリー - 損切り) × 1.3 (R:R = 1.3:1)
  複層トレーリング: +0.5R で建値 / +1R で +0.3R ロック
  強制: 14:55

【リスク管理】
  MAX_RISK 3,000円 (=予算 600,000円の0.5%) で同時保有による DD を抑制。
  walk-forward DD -40% → 想定 -20% へ縮小。

【使い方】
  python daytrade_donchian.py --source local --days 60 --budget 600000
  python daytrade_donchian.py --universe winners
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

# パラメータ (ultra_freq プリセット)
DEFAULT_DAYS     = 60
BUDGET           = 600_000
MAX_RISK         = 3_000      # サイズ縮小 (6k→3k) で walk-forward DD -40%→-20% を狙う
DON_PERIOD       = 8          # 過去8本(40分)高値
TARGET_R         = 1.3        # 目標 R:R = 1.3
GAP_MAX_PCT      = 4.0        # ギャップ許容上限 (材料株対応)
STOP_MAX_PCT     = 2.0        # 損切り距離の上限
# 複層トレーリング (R:R=1.3用)
TRAIL_STEPS = [
    (0.40, 0.0),   # +0.5R進捗 → 建値
    (0.75, 0.3),   # +1R進捗   → +0.3Rロック
]
FORCE_CLOSE      = dtime(14, 55)
ENTRY_CUTOFF     = dtime(14, 30)  # 14:30まで (取引機会最大)
WARMUP           = 8


def default_params():
    """現状のモジュール定数をparamsディクショナリで返す (高頻度モード設定)。"""
    return dict(
        don_period=DON_PERIOD,
        target_r=TARGET_R,
        gap_max_pct=GAP_MAX_PCT,
        stop_max_pct=STOP_MAX_PCT,
        trail_steps=TRAIL_STEPS,
        force_close=FORCE_CLOSE,
        entry_cutoff=ENTRY_CUTOFF,
        warmup=WARMUP,
    )


def backtest_donchian_day(day_df: pd.DataFrame, prev_close=None,
                          budget=BUDGET, max_risk=MAX_RISK,
                          params=None):
    """1日分のバックテスト。複数トレード対応 (決済後に再エントリー可)。

    params: dict 型のパラメータ上書き。Noneなら default_params() を使用。
    """
    if params is None:
        p = default_params()
    else:
        p = {**default_params(), **params}
    DON_P    = p["don_period"]
    TGT_R    = p["target_r"]
    GAP_MAX  = p["gap_max_pct"]
    STOP_MAX = p["stop_max_pct"]
    TRAILS   = p["trail_steps"]
    FCLOSE   = p["force_close"]
    ECUT     = p["entry_cutoff"]
    WUP      = p["warmup"]

    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)

    if n < WUP + 2:
        return []

    open_p = opens[0]
    if prev_close and prev_close > 0:
        if abs(open_p - prev_close) / prev_close * 100 > GAP_MAX:
            return []

    trades = []
    state = "idle"
    entry_p = stop_p = target_p = orig_stop = 0.0
    entry_dt = None
    qty = 0
    trail_level = 0

    def _finish_trade(exit_p, exit_dt, reason):
        pnl = (exit_p - entry_p) * qty
        pct = (exit_p - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=exit_dt,
            entry_p=entry_p, exit_p=exit_p,
            stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct,
            strategy="Donchian", reason=reason,
        ))

    i = WUP
    while i < n:
        t = times[i].time()
        hi, lo, cl, op = highs[i], lows[i], closes[i], opens[i]

        if state == "in_pos":
            if t >= FCLOSE:
                _finish_trade(cl, times[i], "引け強制")
                break
            stop_labels = ("損切り", "建値撤退", "+0.3Rロック")
            exit_reason = stop_labels[min(trail_level, len(stop_labels) - 1)]
            if lo <= stop_p and hi >= target_p:
                _finish_trade(stop_p, times[i], exit_reason)
                state = "idle"
                i += 1
                continue
            if hi >= target_p:
                _finish_trade(target_p, times[i], "目標達成")
                state = "idle"
                i += 1
                continue
            if lo <= stop_p:
                _finish_trade(stop_p, times[i], exit_reason)
                state = "idle"
                i += 1
                continue
            if target_p > entry_p:
                risk = entry_p - orig_stop
                progress = (hi - entry_p) / (target_p - entry_p)
                for idx, (trigger, lock_r) in enumerate(TRAILS):
                    if progress >= trigger and trail_level <= idx:
                        new_stop = entry_p + risk * lock_r
                        if new_stop > stop_p:
                            stop_p = new_stop
                            trail_level = idx + 1
            i += 1
            continue

        if t >= ECUT:
            i += 1
            continue

        don_hi = highs[max(0, i - DON_P):i].max()
        don_lo = lows[max(0, i - DON_P):i].min()
        if cl > don_hi and cl > op:
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            # 損切り: ドンチャン安値 or エントリー-STOP_MAX% の浅い方
            #   → 安値が遠すぎる場合に株数が100に張り付くのを回避
            stop_floor = entry_p * (1 - STOP_MAX / 100)
            stop_p = max(don_lo, stop_floor)
            orig_stop = stop_p
            if entry_p <= stop_p:
                i += 1
                continue
            target_p = entry_p + (entry_p - stop_p) * TGT_R
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            state = "in_pos"
            trail_level = 0
            i += 2
            continue

        i += 1

    if state == "in_pos":
        _finish_trade(closes[-1], times[-1], "引け強制")

    return trades


def backtest_symbol(sym, name, df, budget=BUDGET, max_risk=MAX_RISK, params=None):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        day_trades = backtest_donchian_day(daily[d], prev_close, budget, max_risk, params)
        trades.extend(day_trades)
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades)


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


def build_html(items, stats, days, budget, source):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    s = stats
    cls = "profit" if s["total_pnl"] >= 0 else "loss"

    rows = ""
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        rows += f'<tr><td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>'
        rows += f'<td>{ist["n"]}</td><td>{ist["win_rate"]:.0f}%</td><td>{_pf(ist["pf"])}</td>'
        rows += f'<td class="{c}">{ist["total_pnl"]:+,.0f}</td>'
        rows += f'<td class="profit">{ist["avg_win"]:+,.0f}</td>'
        rows += f'<td class="loss">{ist["avg_loss"]:+,.0f}</td>'
        rows += f'<td class="loss">{ist["max_dd"]:+.1f}%</td></tr>'

    # 全トレードを時系列で並べて明細化
    all_trades = []
    for it in items:
        for t in it["trades"]:
            tt = dict(t)
            tt["symbol"] = it["symbol"]
            tt["name"] = it["name"]
            all_trades.append(tt)
    all_trades.sort(key=lambda x: str(x.get("entry_dt", "")))

    def _fmt_dt(dt):
        if hasattr(dt, "strftime"):
            return dt.strftime("%m-%d %H:%M")
        return str(dt)[:16] if dt else "-"

    trade_rows = ""
    for t in all_trades:
        pc = "profit" if t["pnl"] > 0 else "loss"
        ed = _fmt_dt(t.get("entry_dt"))
        xd = _fmt_dt(t.get("exit_dt"))
        hold = ""
        try:
            delta = t["exit_dt"] - t["entry_dt"]
            mins = int(delta.total_seconds() // 60)
            hold = f"{mins}分"
        except Exception:
            hold = "-"
        trade_rows += f"""<tr>
          <td class="sym">{t['name']}<br><small class="code">{t['symbol']}.T</small></td>
          <td>{ed}</td><td>{xd}</td><td>{hold}</td>
          <td>{t['entry_p']:,.0f}</td>
          <td class="loss">{t['stop_p']:,.0f}</td>
          <td class="profit">{t['target_p']:,.0f}</td>
          <td>{t['exit_p']:,.0f}</td>
          <td>{t['qty']}</td>
          <td class="{pc}">{t['pnl']:+,.0f}</td>
          <td class="{pc}">{t['pct']:+.2f}%</td>
          <td>{t['reason']}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Donchian Breakout — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #10b981;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:140px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.3rem;font-weight:700}}
</style></head><body>
<h1>デイトレ戦略: Donchian {DON_PERIOD}本高値ブレイク</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円<br>
エントリー: 5分足{DON_PERIOD}本高値更新+陽線 → 翌バー寄付買い<br>
損切り: {DON_PERIOD}本安値 / 目標: R:R {TARGET_R}:1 / トレーリング / 前場限定</p>
<div class="box">
<div class="it"><div class="lb">総損益</div><div class="vl {cls}">{s["total_pnl"]:+,.0f}円</div></div>
<div class="it"><div class="lb">取引</div><div class="vl">{s["n"]}</div></div>
<div class="it"><div class="lb">勝率</div><div class="vl">{s["win_rate"]:.1f}%</div></div>
<div class="it"><div class="lb">PF</div><div class="vl">{_pf(s["pf"])}</div></div>
<div class="it"><div class="lb">DD</div><div class="vl loss">{s["max_dd"]:+.1f}%</div></div></div>
<h2>銘柄別</h2><table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>個別トレード明細 ({len(all_trades)}件 / 時系列順)</h2>
<table><thead><tr>
<th>銘柄</th><th>Entry</th><th>Exit</th><th>保有</th>
<th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
<th>株数</th><th>損益</th><th>%</th><th>決済理由</th>
</tr></thead><tbody>{trade_rows}</tbody></table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Donchian デイトレ")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    targets = [(s, s) for s in args.symbols] if args.symbols else DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"Donchian: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円", flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym], args.budget, MAX_RISK)
        if r:
            items.append(r)

    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt", "")))
    stats = calc_stats(all_trades, args.budget)
    print(f"\n取引:{stats['n']}  勝率:{stats['win_rate']:.1f}%  PF:{_pf(stats['pf'])}  "
          f"損益:{stats['total_pnl']:+,.0f}  DD:{stats['max_dd']:+.1f}%")

    # 銘柄別テキスト出力 (貼り付け用)
    print(f"\n{'='*78}")
    print(f"  銘柄別サマリ ({len(items)}銘柄)")
    print("=" * 78)
    print(f"{'銘柄':<22} {'コード':<8} {'取引':>4} {'勝率':>5} {'PF':>7} "
          f"{'損益':>10} {'DD':>6}")
    print("-" * 78)
    rows = []
    for it in items:
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], args.budget)
        rows.append((it["name"], it["symbol"], ist))
    rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
    for name, sym, ist in rows:
        disp = name[:20] if len(name) <= 20 else name[:19] + "…"
        print(f"{disp:<22} {sym:<8} {ist['n']:>4} {ist['win_rate']:>4.0f}% "
              f"{_pf(ist['pf']):>7} {ist['total_pnl']:>+10,.0f} "
              f"{ist['max_dd']:>+5.1f}%")
    print("=" * 78)

    out = Path(f"daytrade_donchian_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(items, stats, args.days, args.budget, args.source),
                   encoding="utf-8")
    print(f"HTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
