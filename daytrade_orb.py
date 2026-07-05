"""
daytrade_orb.py  ―  デイトレ戦略①: Opening Range Breakout (ORB)
==================================================================
【戦略概要】
  寄付きから最初の 30 分 (9:00-9:30) で形成される高値・安値を
  「オープニングレンジ (OR)」とする。
  その後、5分足終値が OR 上限を上抜けたバーの次のバー寄付で買う。

【エントリー】
  - 9:30 以降の 5分足終値が OR 高値 > である
  - まだ当日ポジションなし
  - 翌バー寄付で成行ロング

【決済】
  - 損切り : OR 安値 (下抜けで即決済)
  - 目標   : エントリー価格 + OR 幅 × TARGET_K  (デフォルト 1.5)
  - 時間切れ: 14:55 までに決済されなければ終値強制
  - OCO 順序: 同じバー内で両方ヒットしたら損切り優先(保守的)

【データ】
  - yfinance 5 分足、直近 60 日
  - 前場 9:00-11:30 / 後場 12:30-15:00 (JST)

【使い方】
  python daytrade_orb.py                     # デフォルトWL・60日
  python daytrade_orb.py --days 30           # 直近30日
  python daytrade_orb.py 7203.T 9984.T       # 個別銘柄
  python daytrade_orb.py --target-k 2.0      # 目標倍率変更
  python daytrade_orb.py --or-minutes 15     # OR 時間変更
  python daytrade_orb.py --no-html
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

BUDGET   = 600_000
MAX_RISK = 6_000

JST = timezone(timedelta(hours=9))

# ── パラメータ ──────────────────────────────────────────────
INTERVAL      = "5m"
DEFAULT_DAYS  = 60
OR_MINUTES    = 30          # オープニングレンジ時間 (9:00-9:30)
TARGET_K      = 1.5         # 目標 = エントリー + OR幅 × K
STOP_BUFFER_K = 0.0         # 損切り = OR安値 - OR幅×K (0=OR安値そのまま)
FORCE_CLOSE   = dtime(14, 55)   # 強制決済時刻
ENTRY_CUTOFF  = dtime(14, 30)   # 新規エントリー打ち切り

FIXED_QTY     = 100         # 固定株数
INITIAL_CASH  = 1_000_000   # 初期資金 (評価用)

# フィルター
GAP_MAX_PCT   = 2.0         # 前日比ギャップ上限 (%)。超えたら見送り

# 立会時間
AM_START = dtime(9, 0)
AM_END   = dtime(11, 30)
PM_START = dtime(12, 30)
PM_END   = dtime(15, 0)

DEFAULT_SYMBOLS = DAYTRADE_SYMBOLS  # 共通リスト (60銘柄)


# ─────────────────────────────────────────────────────────────
# 1日分ORBバックテスト
# ─────────────────────────────────────────────────────────────

def backtest_orb_day(day_df: pd.DataFrame, target_k: float,
                     or_minutes: int,
                     prev_close: float | None = None,
                     budget: int = BUDGET, max_risk: int = MAX_RISK) -> dict | None:
    """1日分のORBバックテスト。エントリー0〜1回 (1ポジ/日)。"""
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=or_minutes)).time()

    or_bars = day_df[day_df.index.time < or_end]
    rest    = day_df[day_df.index.time >= or_end]
    if len(or_bars) < 2 or rest.empty:
        return None

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w  = or_hi - or_lo
    if or_w <= 0:
        return None

    # ギャップフィルター: 前日比が大きい日は見送り
    if prev_close and prev_close > 0:
        open_price = float(or_bars.iloc[0]["open"])
        gap_pct = abs(open_price - prev_close) / prev_close * 100
        if gap_pct > GAP_MAX_PCT:
            return None

    # 状態
    state    = "idle"       # idle / in_pos / closed
    entry_p  = 0.0
    entry_dt: datetime | None = None
    stop_p   = or_lo - or_w * STOP_BUFFER_K
    target_p = 0.0

    exit_p: float | None = None
    exit_dt: datetime | None = None
    reason  = None

    bars_list = list(rest.itertuples())

    for i, bar in enumerate(bars_list):
        ts  = bar.Index
        op  = float(bar.open)
        hi  = float(bar.high)
        lo  = float(bar.low)
        cl  = float(bar.close)
        t   = ts.time()

        if state == "in_pos":
            # 強制決済: 14:55 以降の最初のバー
            if t >= FORCE_CLOSE:
                exit_p = cl
                exit_dt = ts
                reason  = "引け強制"
                state   = "closed"
                break
            # OCO: 同バーで両方ヒットしたら損切り優先
            if lo <= stop_p and hi >= target_p:
                exit_p = stop_p
                exit_dt = ts
                reason  = "損切り"
                state   = "closed"
                break
            if hi >= target_p:
                exit_p = target_p
                exit_dt = ts
                reason  = "目標達成"
                state   = "closed"
                break
            if lo <= stop_p:
                exit_p = stop_p
                exit_dt = ts
                reason  = "損切り"
                state   = "closed"
                break
            continue

        if state == "idle":
            if t >= ENTRY_CUTOFF:
                break
            # シグナル: 終値が OR 高値を上抜け
            if cl > or_hi:
                # 翌バー寄付で約定
                if i + 1 >= len(bars_list):
                    break
                nxt = bars_list[i + 1]
                entry_p  = float(nxt.open)
                entry_dt = nxt.Index
                target_p = entry_p + or_w * target_k
                # 損切りは OR 安値ベース (エントリー直下でない)
                if entry_p <= stop_p:
                    # すでに OR 安値以下で寄付き → スキップ
                    break
                state = "in_pos"
                # 同じバーで決済チェック
                nhi = float(nxt.high)
                nlo = float(nxt.low)
                if nlo <= stop_p and nhi >= target_p:
                    exit_p = stop_p
                    exit_dt = nxt.Index
                    reason  = "損切り"
                    state   = "closed"
                    break
                if nhi >= target_p:
                    exit_p = target_p
                    exit_dt = nxt.Index
                    reason  = "目標達成"
                    state   = "closed"
                    break
                if nlo <= stop_p:
                    exit_p = stop_p
                    exit_dt = nxt.Index
                    reason  = "損切り"
                    state   = "closed"
                    break
                continue

    # 未決済 → 最終バー終値で強制決済
    if state == "in_pos":
        last = bars_list[-1]
        exit_p = float(last.close)
        exit_dt = last.Index
        reason  = "引け強制"
        state   = "closed"

    if state != "closed" or exit_p is None or entry_dt is None:
        return None

    # サイジングを他のデイトレ戦略と統一 (リスクベース∩予算)
    qty = calc_position_size(entry_p, stop_p, budget, max_risk)
    if qty <= 0:        # 予算で100株買えない高額株 → エントリーしない
        return None
    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p, qty=qty,
        pnl=pnl, pct=pct,
        or_hi=or_hi, or_lo=or_lo, or_w=or_w,
        stop_p=stop_p, target_p=target_p,
        strategy="ORB", reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# 銘柄単位バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, name: str, df: pd.DataFrame,
                    budget: int = BUDGET, max_risk: int = MAX_RISK,
                    target_k: float = TARGET_K,
                    or_minutes: int = OR_MINUTES) -> dict | None:
    """pre-fetched df を使って銘柄バックテスト (並列取得問題を回避)。
    引数順は他のデイトレ戦略 (budget, max_risk) に統一しパイプライン互換。"""
    if df is None or df.empty:
        return None
    daily = split_by_day(df)
    if len(daily) < 3:
        return None

    trades = []
    dates = sorted(daily.keys())
    prev_close = None
    for date in dates:
        day_df = daily[date]
        t = backtest_orb_day(day_df, target_k, or_minutes,
                             prev_close=prev_close,
                             budget=budget, max_risk=max_risk)
        if t:
            trades.append(t)
        prev_close = float(day_df.iloc[-1]["close"])

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

    # 最大DD
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


def print_report(results: list[dict], days: int, target_k: float, or_min: int) -> None:
    W = 88
    print("=" * W)
    print(f"  デイトレ戦略①: Opening Range Breakout (ORB)  — 直近{days}日 / OR={or_min}分 / K={target_k}")
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

    # 合計
    agg = _calc_stats(all_trades) if all_trades else _empty_stats()
    print("-" * W)
    print(f"  {'合計':<10} {'':<18} {agg['n']:>4} {agg['win_rate']:>5.1f}% "
          f"{_pf_str(agg['pf']):>6} {agg['total_pnl']:>+12,.0f} "
          f"{agg['avg_win']:>+10,.0f} {agg['avg_loss']:>+10,.0f} {agg['max_dd']:>+7.2f}%")
    print("=" * W)


def build_html(results: list[dict], days: int, target_k: float, or_min: int) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # 全体サマリー
    all_trades = []
    for r in results:
        all_trades.extend(r["trades"])
    agg = _calc_stats(all_trades) if all_trades else _empty_stats()

    # 銘柄サマリー
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

    # トレード明細
    trade_sections = ""
    for r in results:
        if not r["trades"]:
            continue
        trows = ""
        for t in r["trades"]:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            trows += f"""
              <tr>
                <td>{t['entry_dt'].strftime('%Y-%m-%d %H:%M')}</td>
                <td>{t['exit_dt'].strftime('%H:%M')}</td>
                <td>{t['or_lo']:,.1f}</td>
                <td>{t['or_hi']:,.1f}</td>
                <td>{t['or_w']:,.2f}</td>
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
            <th>エントリー</th><th>決済</th>
            <th>OR安値</th><th>OR高値</th><th>OR幅</th>
            <th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
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
<title>ORB デイトレード バックテスト — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#fbbf24; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#fbbf24; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #fbbf24; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  .summary-box {{ background:#1e293b; padding:16px; border-radius:8px; margin-bottom:16px; }}
  .summary-box .big {{ font-size:1.6rem; font-weight:700; }}
  .trade-section {{ margin-bottom:24px; }}
</style>
</head>
<body>
<h1>デイトレ戦略①: Opening Range Breakout (ORB)</h1>
<p class="subtitle">
  生成: {today} ／ 期間: 直近{days}日 ／ 足種: {INTERVAL} ／ OR: {or_min}分<br>
  エントリー: 5分足終値がOR高値上抜け → 翌バー寄付成行買い<br>
  決済: 損切り=OR安値 ／ 目標=エントリー+OR幅×{target_k} ／ 強制決済={FORCE_CLOSE.strftime('%H:%M')}
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
    parser = argparse.ArgumentParser(description="ORB デイトレ バックテスト")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--target-k", type=float, default=TARGET_K)
    parser.add_argument("--or-minutes", type=int, default=OR_MINUTES)
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

    print(f"ORB バックテスト開始: {len(targets)}銘柄 / {args.days}日 / "
          f"OR={args.or_minutes}分 / K={args.target_k} / source={args.source}",
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
                                target_k=args.target_k, or_minutes=args.or_minutes)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  [err] {sym}: {e}", file=sys.stderr)

    order = {s: i for i, (s, _) in enumerate(targets)}
    results.sort(key=lambda x: order.get(x["symbol"], 999))

    print_report(results, args.days, args.target_k, args.or_minutes)

    if not args.no_html and results:
        out = Path(f"daytrade_orb_{datetime.now(JST).strftime('%Y%m%d')}.html")
        out.write_text(build_html(results, args.days, args.target_k, args.or_minutes),
                       encoding="utf-8")
        print(f"\nHTMLレポート: {out.resolve()}")
        if not args.no_browser:
            try:
                open_html(out.resolve().as_uri())
            except Exception:
                pass


if __name__ == "__main__":
    main()
