"""
daytrade_vwap.py  ―  デイトレ戦略②: VWAP Pullback (押し目反発)
==================================================================
【戦略概要】
  日中 VWAP (Volume Weighted Average Price) を計算し、
  上昇トレンド中の VWAP 押し目からの反発を捉える。

【シグナル条件 (すべて満たす)】
  1. 現在バー終値 > VWAP          (上昇トレンド)
  2. VWAP 傾きが正                (直近 LOOKBACK 本でVWAP上昇)
  3. 直近 WINDOW 本で安値 ≤ VWAP×(1+TOL)  (押し目タッチ)
  4. 現在バー終値 > 前バー終値    (反発確認)
  5. 現在時刻 < ENTRY_CUTOFF       (エントリー締切)
  6. 当日ポジションなし            (1日最大1ポジ)

【決済】
  - 損切り : エントリー価格 × (1 - STOP_PCT)
             (VWAP 割れ想定の簡易版、現バー VWAP からの下方バッファ)
  - 目標   : エントリー + (エントリー - 損切り) × TARGET_R   (R:R = 2:1 デフォルト)
  - 強制決済: 14:55 以降の最初のバー終値
  - OCO 順序: 同バー内両方ヒットは損切り優先

【データ】
  - yfinance 5 分足、直近 60 日

【使い方】
  python daytrade_vwap.py
  python daytrade_vwap.py --days 30
  python daytrade_vwap.py 7203.T 9984.T
  python daytrade_vwap.py --stop-pct 0.004 --target-r 2.5
  python daytrade_vwap.py --tol 0.002
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
from daytrade_data import load_intraday_batch, split_by_day

JST = timezone(timedelta(hours=9))

# ── パラメータ ──────────────────────────────────────────────
INTERVAL       = "5m"
DEFAULT_DAYS   = 60

VWAP_LOOKBACK  = 10     # VWAP 傾き判定の参照本数
PULLBACK_WINDOW = 5     # 押し目タッチ参照本数
PULLBACK_TOL   = 0.001  # 押し目判定の許容率 (VWAP+0.1% 以内で "タッチ" 扱い)
STOP_PCT       = 0.004  # 損切り = エントリー × (1 - 0.4%)
TARGET_R       = 2.0    # リスクリワード比

WARMUP_BARS    = 12           # 最低ウォームアップ本数 (9:00〜10:00程度)
FORCE_CLOSE    = dtime(14, 55)
ENTRY_CUTOFF   = dtime(14, 30)

FIXED_QTY      = 100
INITIAL_CASH   = 1_000_000

AM_START = dtime(9, 0)
AM_END   = dtime(11, 30)
PM_START = dtime(12, 30)
PM_END   = dtime(15, 0)

DEFAULT_SYMBOLS = DAYTRADE_SYMBOLS  # 共通リスト (60銘柄)


def attach_vwap(day_df: pd.DataFrame) -> pd.DataFrame:
    """1日分 DataFrame に累積 VWAP 列を追加。"""
    df = day_df.copy()
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].astype(float)
    cum_v = vol.cumsum().replace(0, np.nan)
    df["vwap"] = (tp * vol).cumsum() / cum_v
    df["vwap"] = df["vwap"].ffill().fillna(df["close"])
    return df


# ─────────────────────────────────────────────────────────────
# 1日分 VWAP Pullback バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_vwap_day(day_df: pd.DataFrame, stop_pct: float,
                      target_r: float, tol: float) -> dict | None:
    """1日分 VWAP Pullback。1日最大1ポジ。"""
    df = attach_vwap(day_df)
    closes = df["close"].to_numpy(dtype=float)
    lows   = df["low"].to_numpy(dtype=float)
    highs  = df["high"].to_numpy(dtype=float)
    opens  = df["open"].to_numpy(dtype=float)
    vwap   = df["vwap"].to_numpy(dtype=float)
    times  = df.index

    n = len(df)
    if n < WARMUP_BARS + 2:
        return None

    state    = "idle"
    entry_p  = 0.0
    entry_dt = None
    stop_p   = 0.0
    target_p = 0.0

    exit_p  = None
    exit_dt = None
    reason  = None
    signal_idx = -1

    i = WARMUP_BARS
    while i < n:
        t  = times[i].time()
        cl = closes[i]
        hi = highs[i]
        lo = lows[i]

        # in_pos: 決済チェック
        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p  = cl
                exit_dt = times[i]
                reason  = "引け強制"
                state   = "closed"
                break
            if lo <= stop_p and hi >= target_p:
                exit_p  = stop_p
                exit_dt = times[i]
                reason  = "損切り"
                state   = "closed"
                break
            if hi >= target_p:
                exit_p  = target_p
                exit_dt = times[i]
                reason  = "目標達成"
                state   = "closed"
                break
            if lo <= stop_p:
                exit_p  = stop_p
                exit_dt = times[i]
                reason  = "損切り"
                state   = "closed"
                break
            i += 1
            continue

        # idle: シグナル判定
        if t >= ENTRY_CUTOFF:
            break

        vw_now  = vwap[i]
        vw_past = vwap[max(0, i - VWAP_LOOKBACK)]
        prev_cl = closes[i - 1]

        # 条件 1: 終値 > VWAP
        cond_above = cl > vw_now
        # 条件 2: VWAP 傾き正
        cond_slope = vw_now > vw_past
        # 条件 3: 直近 WINDOW 本で安値が VWAP×(1+TOL) 以下 (押し目タッチ)
        start_w = max(0, i - PULLBACK_WINDOW + 1)
        touched = False
        for j in range(start_w, i + 1):
            if lows[j] <= vwap[j] * (1 + tol):
                touched = True
                break
        # 条件 4: 反発確認 (終値 > 前終値)
        cond_bounce = cl > prev_cl

        if cond_above and cond_slope and touched and cond_bounce:
            # 翌バー寄付で約定
            if i + 1 >= n:
                break
            entry_p  = opens[i + 1]
            entry_dt = times[i + 1]
            stop_p   = entry_p * (1 - stop_pct)
            target_p = entry_p + (entry_p - stop_p) * target_r
            if stop_p <= 0 or target_p <= entry_p:
                i += 1
                continue
            signal_idx = i
            state      = "in_pos"
            # 次バーから決済チェック開始
            nxt_hi = highs[i + 1]
            nxt_lo = lows[i + 1]
            nxt_t  = times[i + 1].time()
            if nxt_t >= FORCE_CLOSE:
                exit_p  = closes[i + 1]
                exit_dt = times[i + 1]
                reason  = "引け強制"
                state   = "closed"
                break
            if nxt_lo <= stop_p and nxt_hi >= target_p:
                exit_p  = stop_p
                exit_dt = times[i + 1]
                reason  = "損切り"
                state   = "closed"
                break
            if nxt_hi >= target_p:
                exit_p  = target_p
                exit_dt = times[i + 1]
                reason  = "目標達成"
                state   = "closed"
                break
            if nxt_lo <= stop_p:
                exit_p  = stop_p
                exit_dt = times[i + 1]
                reason  = "損切り"
                state   = "closed"
                break
            i += 2
            continue

        i += 1

    # 未決済 → 最終バー終値で強制決済
    if state == "in_pos":
        exit_p  = closes[-1]
        exit_dt = times[-1]
        reason  = "引け強制"
        state   = "closed"

    if state != "closed" or exit_p is None or entry_dt is None:
        return None

    qty = FIXED_QTY
    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p, qty=qty,
        pnl=pnl, pct=pct,
        signal_dt=times[signal_idx] if signal_idx >= 0 else None,
        vwap_at_signal=float(vwap[signal_idx]) if signal_idx >= 0 else None,
        stop_p=stop_p, target_p=target_p,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# 銘柄単位バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, name: str, df: pd.DataFrame,
                    stop_pct: float, target_r: float, tol: float) -> dict | None:
    """pre-fetched df を使って銘柄バックテスト。"""
    if df is None or df.empty:
        return None
    daily = split_by_day(df)
    if len(daily) < 3:
        return None

    trades = []
    for _, day_df in daily.items():
        t = backtest_vwap_day(day_df, stop_pct, target_r, tol)
        if t:
            trades.append(t)

    if not trades:
        return dict(symbol=symbol, name=name, trades=[], stats=_empty_stats())

    return dict(symbol=symbol, name=name, trades=trades, stats=_calc_stats(trades))


def _empty_stats() -> dict:
    return dict(n=0, wins=0, losses=0, win_rate=0.0, pf=0.0,
                total_pnl=0.0, avg_win=0.0, avg_loss=0.0, max_dd=0.0)


def _calc_stats(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    total = sum(t["pnl"] for t in trades)
    avg_w = gp / len(wins) if wins else 0.0
    avg_l = -gl / len(losses) if losses else 0.0

    eq = INITIAL_CASH
    peak = eq
    max_dd = 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd

    return dict(
        n=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n * 100 if n else 0.0,
        pf=pf, total_pnl=total, avg_win=avg_w, avg_loss=avg_l, max_dd=max_dd,
    )


# ─────────────────────────────────────────────────────────────
# レポート出力
# ─────────────────────────────────────────────────────────────

def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def print_report(results: list[dict], days: int, stop_pct: float,
                 target_r: float, tol: float) -> None:
    W = 88
    print("=" * W)
    print(f"  デイトレ戦略②: VWAP Pullback  — 直近{days}日 / stop={stop_pct*100:.2f}% / "
          f"R={target_r} / tol={tol*100:.2f}%")
    print("=" * W)

    print(f"\n{'銘柄':<10} {'名前':<20} {'取引':>4} {'勝率':>6} {'PF':>6} "
          f"{'総損益':>12} {'平均利益':>10} {'平均損失':>10} {'最大DD':>8}")
    print("-" * W)

    all_trades = []
    for r in results:
        s = r["stats"]
        print(f"  {r['symbol']:<10} {r['name']:<18} {s['n']:>4} {s['win_rate']:>5.1f}% "
              f"{_pf_str(s['pf']):>6} {s['total_pnl']:>+12,.0f} "
              f"{s['avg_win']:>+10,.0f} {s['avg_loss']:>+10,.0f} {s['max_dd']:>+7.2f}%")
        all_trades.extend(r["trades"])

    agg = _calc_stats(all_trades) if all_trades else _empty_stats()
    print("-" * W)
    print(f"  {'合計':<10} {'':<18} {agg['n']:>4} {agg['win_rate']:>5.1f}% "
          f"{_pf_str(agg['pf']):>6} {agg['total_pnl']:>+12,.0f} "
          f"{agg['avg_win']:>+10,.0f} {agg['avg_loss']:>+10,.0f} {agg['max_dd']:>+7.2f}%")
    print("=" * W)


def build_html(results: list[dict], days: int, stop_pct: float,
               target_r: float, tol: float) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    all_trades = []
    for r in results:
        all_trades.extend(r["trades"])
    agg = _calc_stats(all_trades) if all_trades else _empty_stats()

    rows = ""
    for r in sorted(results, key=lambda x: x["stats"]["total_pnl"], reverse=True):
        s = r["stats"]
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        rows += f"""
        <tr>
          <td class="sym">{r['symbol']}<br><small>{r['name']}</small></td>
          <td>{s['n']}</td><td>{s['wins']}</td>
          <td>{s['win_rate']:.1f}%</td><td>{_pf_str(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}円</td>
          <td class="profit">{s['avg_win']:+,.0f}</td>
          <td class="loss">{s['avg_loss']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.2f}%</td>
        </tr>"""

    trade_sections = ""
    for r in results:
        if not r["trades"]:
            continue
        trows = ""
        for t in r["trades"]:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            sig_t = t["signal_dt"].strftime("%H:%M") if t.get("signal_dt") else "-"
            vw = t.get("vwap_at_signal")
            vw_str = f"{vw:,.1f}" if vw is not None else "-"
            trows += f"""
              <tr>
                <td>{t['entry_dt'].strftime('%Y-%m-%d')}</td>
                <td>{sig_t}</td>
                <td>{t['entry_dt'].strftime('%H:%M')}</td>
                <td>{t['exit_dt'].strftime('%H:%M')}</td>
                <td class="vwap">{vw_str}</td>
                <td>{t['entry_p']:,.1f}</td>
                <td class="loss">{t['stop_p']:,.1f}</td>
                <td class="profit">{t['target_p']:,.1f}</td>
                <td>{t['exit_p']:,.1f}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['reason']}</td>
              </tr>"""
        s = r["stats"]
        tot_cls = "profit" if s["total_pnl"] >= 0 else "loss"
        trade_sections += f"""
      <div class="trade-section">
        <h3>{r['symbol']} {r['name']}
          <span class="{tot_cls}">{s['total_pnl']:+,.0f}円</span>
          <small>（{s['n']}取引 勝率{s['win_rate']:.0f}%）</small>
        </h3>
        <table>
          <thead><tr>
            <th>日付</th><th>シグナル</th><th>エントリー</th><th>決済</th>
            <th>VWAP</th><th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
            <th>損益(円)</th><th>損益(%)</th><th>理由</th>
          </tr></thead>
          <tbody>{trows}</tbody>
        </table>
      </div>"""

    agg_cls = "profit" if agg["total_pnl"] >= 0 else "loss"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>VWAP Pullback デイトレード バックテスト — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#a78bfa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#a78bfa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #a78bfa; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  .vwap {{ color:#a78bfa; }}
  .summary-box {{ background:#1e293b; padding:16px; border-radius:8px; margin-bottom:16px; }}
  .summary-box .big {{ font-size:1.6rem; font-weight:700; }}
  .trade-section {{ margin-bottom:24px; }}
</style>
</head>
<body>
<h1>デイトレ戦略②: VWAP Pullback (押し目反発)</h1>
<p class="subtitle">
  生成: {today} ／ 期間: 直近{days}日 ／ 足種: {INTERVAL}<br>
  条件: (1) 終値 &gt; VWAP  (2) VWAP傾き正  (3) 直近{PULLBACK_WINDOW}本でVWAP押し目タッチ  (4) 反発確認<br>
  決済: 損切り=エントリー×(1-{stop_pct*100:.2f}%)  目標=R:R {target_r}:1  強制={FORCE_CLOSE.strftime('%H:%M')}
</p>

<div class="summary-box">
  <span style="color:#94a3b8">総損益:</span>
  <span class="big {agg_cls}">{agg['total_pnl']:+,.0f}円</span>
  &nbsp;&nbsp;
  <span style="color:#94a3b8">取引数:</span> {agg['n']}回
  &nbsp;&nbsp;
  <span style="color:#94a3b8">勝率:</span> {agg['win_rate']:.1f}%
  &nbsp;&nbsp;
  <span style="color:#94a3b8">PF:</span> {_pf_str(agg['pf'])}
  &nbsp;&nbsp;
  <span style="color:#94a3b8">最大DD:</span> <span class="loss">{agg['max_dd']:+.2f}%</span>
</div>

<h2>銘柄別サマリー</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>取引</th><th>勝</th><th>勝率</th><th>PF</th>
    <th>総損益</th><th>平均利益</th><th>平均損失</th><th>最大DD</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>

<h2>個別トレード一覧</h2>
{trade_sections}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="VWAP Pullback デイトレ バックテスト")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--stop-pct", type=float, default=STOP_PCT,
                        help="損切り幅 (例: 0.004 = 0.4%%)")
    parser.add_argument("--target-r", type=float, default=TARGET_R,
                        help="リスクリワード比 (例: 2.0)")
    parser.add_argument("--tol", type=float, default=PULLBACK_TOL,
                        help="押し目タッチ許容率 (例: 0.001 = 0.1%%)")
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto",
                        help="データソース (auto=ローカル優先, local=保存データのみ)")
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        print("[info] yfinance 5分足は最大60日 → 60日に調整", file=sys.stderr)
        args.days = 60

    if args.symbols:
        targets = [(s, s) for s in args.symbols]
    else:
        targets = DEFAULT_SYMBOLS

    print(f"VWAP Pullback バックテスト開始: {len(targets)}銘柄 / {args.days}日 / "
          f"stop={args.stop_pct*100:.2f}% / R={args.target_r} / source={args.source}",
          flush=True)

    symbols_list = [s for s, _ in targets]
    print(f"データ取得中 ({len(symbols_list)}銘柄)...", flush=True)
    fetched = load_intraday_batch(symbols_list, args.days, source=args.source)
    print(f"  取得成功: {len(fetched)}/{len(symbols_list)}銘柄", flush=True)

    results: list[dict] = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        try:
            r = backtest_symbol(sym, name, fetched[sym],
                                args.stop_pct, args.target_r, args.tol)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  [err] {sym}: {e}", file=sys.stderr)

    order = {s: i for i, (s, _) in enumerate(targets)}
    results.sort(key=lambda x: order.get(x["symbol"], 999))

    print_report(results, args.days, args.stop_pct, args.target_r, args.tol)

    if not args.no_html and results:
        out = Path(f"daytrade_vwap_{datetime.now(JST).strftime('%Y%m%d')}.html")
        out.write_text(
            build_html(results, args.days, args.stop_pct, args.target_r, args.tol),
            encoding="utf-8",
        )
        print(f"\nHTMLレポート: {out.resolve()}")
        if not args.no_browser:
            try:
                open_html(out.resolve().as_uri())
            except Exception:
                pass


if __name__ == "__main__":
    main()
