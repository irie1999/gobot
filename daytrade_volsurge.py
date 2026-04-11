"""
daytrade_volsurge.py  ―  デイトレ戦略③: Volume Surge Momentum
==================================================================
【戦略概要】
  出来高が直近平均の倍以上に急増した陽線ブレイク足に追随。
  急激な買い需要(機関投資家の動き・ニュース反応)を捉える。

【シグナル条件 (すべて満たす)】
  1. 現在バー出来高 ≥ 直近 VOL_LOOKBACK 本平均 × VOL_MULT
  2. 現在バーが陽線 (close > open)
  3. 現在バー終値 > 直近 BREAK_LOOKBACK 本(現在バー含まず)の高値
  4. 現在バーの (close - open) / open ≥ MIN_BODY_PCT  (小陽線除外)
  5. 現在時刻 < ENTRY_CUTOFF
  6. 当日ポジションなし (1日最大1ポジ)

【決済】
  - 損切り : シグナルバーの安値 × (1 - STOP_BUFFER_PCT)
  - 目標   : エントリー + (エントリー - 損切り) × TARGET_R  (R:R = 1.5 デフォルト)
  - 強制決済: 14:55 以降の最初のバー終値
  - OCO 順序: 同バー内両方ヒットは損切り優先

【データ】
  - yfinance 5 分足、直近 60 日

【使い方】
  python daytrade_volsurge.py
  python daytrade_volsurge.py --days 30
  python daytrade_volsurge.py 7203.T 9984.T
  python daytrade_volsurge.py --vol-mult 2.5 --target-r 2.0
  python daytrade_volsurge.py --break-lookback 30
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

JST = timezone(timedelta(hours=9))

# ── パラメータ ──────────────────────────────────────────────
INTERVAL         = "5m"
DEFAULT_DAYS     = 60

VOL_LOOKBACK     = 20      # 出来高平均の参照本数
VOL_MULT         = 2.0     # 出来高急増倍率
BREAK_LOOKBACK   = 20      # 直近高値ブレイクの参照本数
MIN_BODY_PCT     = 0.002   # 最小実体幅 0.2% (小陽線除外)
STOP_BUFFER_PCT  = 0.001   # シグナルバー安値 × (1 - 0.1%) を損切りに
TARGET_R         = 1.5     # リスクリワード比

WARMUP_BARS      = 20      # ウォームアップ (出来高平均・直近高値計算)
FORCE_CLOSE      = dtime(14, 55)
ENTRY_CUTOFF     = dtime(14, 30)

FIXED_QTY        = 100
INITIAL_CASH     = 1_000_000
WORKERS          = 4

AM_START = dtime(9, 0)
AM_END   = dtime(11, 30)
PM_START = dtime(12, 30)
PM_END   = dtime(15, 0)

DEFAULT_SYMBOLS = [
    ("7203.T", "トヨタ自動車"),
    ("9984.T", "ソフトバンクG"),
    ("8306.T", "三菱UFJ"),
    ("6758.T", "ソニーG"),
    ("9432.T", "NTT"),
    ("6098.T", "リクルートHD"),
    ("8035.T", "東京エレクトロン"),
    ("6861.T", "キーエンス"),
    ("7974.T", "任天堂"),
    ("8001.T", "伊藤忠商事"),
]


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────

def fetch_intraday(symbol: str, days: int, interval: str = INTERVAL) -> pd.DataFrame | None:
    try:
        df = yf.download(
            symbol, period=f"{days}d", interval=interval,
            auto_adjust=False, progress=False,
        )
    except Exception as e:
        print(f"  [warn] {symbol}: {e}", file=sys.stderr)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[cols].dropna(subset=["close"])
    return df if not df.empty else None


def split_by_day(df: pd.DataFrame) -> dict:
    result = {}
    for date in sorted(set(df.index.date)):
        sub = df[df.index.date == date]
        if len(sub) >= 5:
            result[date] = sub
    return result


# ─────────────────────────────────────────────────────────────
# 1日分 Volume Surge バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_volsurge_day(day_df: pd.DataFrame, vol_mult: float,
                          vol_lookback: int, break_lookback: int,
                          target_r: float, min_body: float,
                          stop_buf: float) -> dict | None:
    """1日分 Volume Surge。1日最大1ポジ。"""
    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    vols   = day_df["volume"].to_numpy(dtype=float)
    times  = day_df.index

    n = len(day_df)
    warm = max(WARMUP_BARS, vol_lookback, break_lookback)
    if n < warm + 2:
        return None

    state     = "idle"
    entry_p   = 0.0
    entry_dt  = None
    stop_p    = 0.0
    target_p  = 0.0
    signal_idx = -1
    sig_vol_ratio = 0.0

    exit_p  = None
    exit_dt = None
    reason  = None

    i = warm
    while i < n:
        t  = times[i].time()
        op = opens[i]
        hi = highs[i]
        lo = lows[i]
        cl = closes[i]

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

        # 条件 1: 出来高急増
        vol_avg = vols[i - vol_lookback:i].mean() if vol_lookback > 0 else 0.0
        if vol_avg <= 0:
            i += 1
            continue
        vol_ratio = vols[i] / vol_avg
        if vol_ratio < vol_mult:
            i += 1
            continue

        # 条件 2: 陽線
        if cl <= op:
            i += 1
            continue

        # 条件 3: 直近高値ブレイク
        prev_hi = highs[i - break_lookback:i].max() if break_lookback > 0 else 0.0
        if cl <= prev_hi:
            i += 1
            continue

        # 条件 4: 最小実体幅
        if op <= 0 or (cl - op) / op < min_body:
            i += 1
            continue

        # 全条件クリア → 翌バー寄付で約定
        if i + 1 >= n:
            break
        entry_p  = opens[i + 1]
        entry_dt = times[i + 1]
        stop_p   = lo * (1 - stop_buf)
        risk     = entry_p - stop_p
        if risk <= 0:
            i += 1
            continue
        target_p = entry_p + risk * target_r
        signal_idx    = i
        sig_vol_ratio = vol_ratio
        state         = "in_pos"

        # 翌バー (約定バー) の決済チェック
        nxt_hi = highs[i + 1]
        nxt_lo = lows[i + 1]
        nxt_cl = closes[i + 1]
        nxt_t  = times[i + 1].time()
        if nxt_t >= FORCE_CLOSE:
            exit_p  = nxt_cl
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
        vol_ratio=sig_vol_ratio,
        stop_p=stop_p, target_p=target_p,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# 銘柄単位バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, name: str, days: int,
                    vol_mult: float, vol_lookback: int,
                    break_lookback: int, target_r: float,
                    min_body: float, stop_buf: float) -> dict | None:
    df = fetch_intraday(symbol, days)
    if df is None:
        return None
    daily = split_by_day(df)
    if len(daily) < 3:
        return None

    trades = []
    for _, day_df in daily.items():
        t = backtest_volsurge_day(
            day_df, vol_mult, vol_lookback, break_lookback,
            target_r, min_body, stop_buf,
        )
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


def print_report(results: list[dict], days: int, vol_mult: float,
                 target_r: float) -> None:
    W = 88
    print("=" * W)
    print(f"  デイトレ戦略③: Volume Surge Momentum  — 直近{days}日 / "
          f"vol×{vol_mult} / R={target_r}")
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


def build_html(results: list[dict], days: int, vol_mult: float,
               break_lookback: int, target_r: float) -> str:
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
            vr = t.get("vol_ratio", 0)
            trows += f"""
              <tr>
                <td>{t['entry_dt'].strftime('%Y-%m-%d')}</td>
                <td>{sig_t}</td>
                <td class="volspike">×{vr:.1f}</td>
                <td>{t['entry_dt'].strftime('%H:%M')}</td>
                <td>{t['exit_dt'].strftime('%H:%M')}</td>
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
            <th>日付</th><th>シグナル</th><th>出来高比</th>
            <th>エントリー</th><th>決済</th>
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
<title>Volume Surge Momentum デイトレード バックテスト — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#fb923c; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#fb923c; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #fb923c; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  .volspike {{ color:#fb923c; font-weight:700; }}
  .summary-box {{ background:#1e293b; padding:16px; border-radius:8px; margin-bottom:16px; }}
  .summary-box .big {{ font-size:1.6rem; font-weight:700; }}
  .trade-section {{ margin-bottom:24px; }}
</style>
</head>
<body>
<h1>デイトレ戦略③: Volume Surge Momentum (出来高急増モメンタム)</h1>
<p class="subtitle">
  生成: {today} ／ 期間: 直近{days}日 ／ 足種: {INTERVAL}<br>
  条件: (1) 出来高 ≥ 平均×{vol_mult}  (2) 陽線  (3) 直近{break_lookback}本高値ブレイク  (4) 実体≥{MIN_BODY_PCT*100:.1f}%<br>
  決済: 損切り=シグナル安値-{STOP_BUFFER_PCT*100:.1f}%  目標=R:R {target_r}:1  強制={FORCE_CLOSE.strftime('%H:%M')}
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
    parser = argparse.ArgumentParser(description="Volume Surge Momentum デイトレ バックテスト")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--vol-mult", type=float, default=VOL_MULT,
                        help="出来高急増倍率 (例: 2.0)")
    parser.add_argument("--vol-lookback", type=int, default=VOL_LOOKBACK,
                        help="出来高平均参照本数")
    parser.add_argument("--break-lookback", type=int, default=BREAK_LOOKBACK,
                        help="直近高値ブレイク参照本数")
    parser.add_argument("--target-r", type=float, default=TARGET_R,
                        help="リスクリワード比 (例: 1.5)")
    parser.add_argument("--min-body", type=float, default=MIN_BODY_PCT,
                        help="最小実体幅 (例: 0.002 = 0.2%%)")
    parser.add_argument("--stop-buf", type=float, default=STOP_BUFFER_PCT,
                        help="シグナル安値からの損切りバッファ")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.days > 60:
        print("[info] 5分足は最大60日 → 60日に調整", file=sys.stderr)
        args.days = 60

    if args.symbols:
        targets = [(s, s) for s in args.symbols]
    else:
        targets = DEFAULT_SYMBOLS

    print(f"Volume Surge バックテスト開始: {len(targets)}銘柄 / {args.days}日 / "
          f"vol×{args.vol_mult} / R={args.target_r}", flush=True)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(backtest_symbol, sym, name, args.days,
                          args.vol_mult, args.vol_lookback,
                          args.break_lookback, args.target_r,
                          args.min_body, args.stop_buf): sym
                for sym, name in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            sym = futs[fut]
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception as e:
                print(f"  [err] {sym}: {e}", file=sys.stderr)
            print(f"  {i}/{len(targets)} 完了", flush=True)

    order = {s: i for i, (s, _) in enumerate(targets)}
    results.sort(key=lambda x: order.get(x["symbol"], 999))

    print_report(results, args.days, args.vol_mult, args.target_r)

    if not args.no_html and results:
        out = Path(f"daytrade_volsurge_{datetime.now(JST).strftime('%Y%m%d')}.html")
        out.write_text(
            build_html(results, args.days, args.vol_mult,
                       args.break_lookback, args.target_r),
            encoding="utf-8",
        )
        print(f"\nHTMLレポート: {out.resolve()}")
        if not args.no_browser:
            try:
                webbrowser.open(out.resolve().as_uri())
            except Exception:
                pass


if __name__ == "__main__":
    main()
