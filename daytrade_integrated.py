"""
daytrade_integrated.py  ―  統合デイトレ戦略 (6改善適用)
==================================================================
VolSurge (メイン) + ORB (サブ) を統合し、以下の改善を適用:

  ① ポジションサイジング : 固定リスク額方式 (1トレード6,000円リスク)
  ② 銘柄スコアリング    : PF > 1.0 の銘柄のみ取引
  ③ トレーリングストップ : 含み益50%到達で建値撤退に切替
  ④ エントリー時間制限   : 前場のみ (9:00-11:00)
  ⑤ 戦略優先順位        : VolSurge優先 → ORBサブ / 1日1ポジ
  ⑥ 損益曲線            : HTMLにエクイティカーブ表示

【使い方】
  python daytrade_integrated.py --source yfinance --days 60 --budget 600000
  python daytrade_integrated.py --source local --days 730 --budget 600000
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
from daytrade_data import (
    load_intraday_batch, split_by_day, calc_position_size,
)

JST = timezone(timedelta(hours=9))

# ── パラメータ ──────────────────────────────────────────────
BUDGET         = 600_000
MAX_RISK       = 6_000      # 1トレードの最大損失額
FORCE_CLOSE    = dtime(14, 55)
ENTRY_CUTOFF   = dtime(11, 0)   # 改善④: 前場のみ

# ORB パラメータ
OR_MINUTES     = 30
TARGET_K       = 1.5

# VolSurge パラメータ
VOL_LOOKBACK   = 20
VOL_MULT       = 2.0
BREAK_LOOKBACK = 20
MIN_BODY_PCT   = 0.002
VOL_TARGET_R   = 1.5
STOP_BUFFER    = 0.001

# トレーリングストップ (改善③)
TRAILING_TRIGGER = 0.5  # 含み益が目標の50%に達したら発動
AM_START = dtime(9, 0)
AM_END   = dtime(11, 30)


# ─────────────────────────────────────────────────────────────
# VolSurge シグナル判定
# ─────────────────────────────────────────────────────────────

def _check_volsurge(day_df, i, opens, highs, lows, closes, vols):
    """VolSurge のシグナル条件をチェック。True なら (entry候補idx, stop, target) を返す。"""
    if i < VOL_LOOKBACK:
        return None
    vol_avg = vols[i - VOL_LOOKBACK:i].mean()
    if vol_avg <= 0:
        return None
    if vols[i] / vol_avg < VOL_MULT:
        return None
    if closes[i] <= opens[i]:  # 陽線でない
        return None
    prev_hi = highs[i - BREAK_LOOKBACK:i].max()
    if closes[i] <= prev_hi:  # 高値ブレイクでない
        return None
    if opens[i] <= 0 or (closes[i] - opens[i]) / opens[i] < MIN_BODY_PCT:
        return None
    stop_p = lows[i] * (1 - STOP_BUFFER)
    return stop_p


# ─────────────────────────────────────────────────────────────
# ORB シグナル判定
# ─────────────────────────────────────────────────────────────

def _check_orb(day_df, i, closes, or_hi, or_lo, or_w):
    """ORB ブレイク条件。True なら stop を返す。"""
    if or_w <= 0:
        return None
    if closes[i] > or_hi:
        return or_lo
    return None


# ─────────────────────────────────────────────────────────────
# 1日分統合バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_day(day_df: pd.DataFrame, budget: int,
                 max_risk: int) -> dict | None:
    """
    1日分の統合バックテスト。
    VolSurge 優先 → ORB サブ、1日最大1ポジ。
    トレーリングストップ適用。
    """
    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    vols   = day_df["volume"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)

    if n < VOL_LOOKBACK + 2:
        return None

    # OR 計算
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=OR_MINUTES)).time()
    or_mask = day_df.index.time < or_end
    or_bars = day_df[or_mask]
    if len(or_bars) >= 2:
        or_hi = float(or_bars["high"].max())
        or_lo = float(or_bars["low"].min())
        or_w  = or_hi - or_lo
    else:
        or_hi = or_lo = or_w = 0.0

    state     = "idle"
    entry_p   = 0.0
    entry_dt  = None
    stop_p    = 0.0
    target_p  = 0.0
    orig_stop = 0.0
    qty       = 0
    strategy  = ""
    trailing_active = False

    exit_p  = None
    exit_dt = None
    reason  = None

    warm = max(VOL_LOOKBACK, 6)  # OR後 + ウォームアップ
    i = warm
    while i < n:
        t  = times[i].time()
        hi = highs[i]
        lo = lows[i]
        cl = closes[i]
        op = opens[i]

        # ── in_pos: 決済チェック ─────────────────────────
        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p, exit_dt, reason = cl, times[i], "引け強制"
                state = "closed"
                break

            # 改善③: トレーリングストップ
            if not trailing_active:
                progress = (cl - entry_p) / (target_p - entry_p) if target_p > entry_p else 0
                if progress >= TRAILING_TRIGGER:
                    stop_p = entry_p  # 建値撤退
                    trailing_active = True

            # OCO (損切り優先)
            if lo <= stop_p and hi >= target_p:
                exit_p, exit_dt, reason = stop_p, times[i], "損切り" if not trailing_active else "建値撤退"
                state = "closed"
                break
            if hi >= target_p:
                exit_p, exit_dt, reason = target_p, times[i], "目標達成"
                state = "closed"
                break
            if lo <= stop_p:
                exit_p, exit_dt, reason = stop_p, times[i], "損切り" if not trailing_active else "建値撤退"
                state = "closed"
                break
            i += 1
            continue

        # ── idle: シグナル判定 ───────────────────────────
        if t >= ENTRY_CUTOFF:  # 改善④
            break

        # 改善⑤: VolSurge 優先チェック
        vs_stop = _check_volsurge(day_df, i, opens, highs, lows, closes, vols)
        if vs_stop is not None and i + 1 < n:
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            stop_p = vs_stop
            orig_stop = vs_stop
            risk = entry_p - stop_p
            if risk > 0:
                target_p = entry_p + risk * VOL_TARGET_R
                qty = calc_position_size(entry_p, stop_p, budget, max_risk)
                strategy = "VolSurge"
                state = "in_pos"
                trailing_active = False
                i += 2
                continue

        # ORB サブ (VolSurge シグナルなし時)
        if or_w > 0 and t >= or_end:
            orb_stop = _check_orb(day_df, i, closes, or_hi, or_lo, or_w)
            if orb_stop is not None and i + 1 < n:
                entry_p = opens[i + 1]
                entry_dt = times[i + 1]
                stop_p = orb_stop
                orig_stop = orb_stop
                if entry_p > stop_p:
                    target_p = entry_p + or_w * TARGET_K
                    qty = calc_position_size(entry_p, stop_p, budget, max_risk)
                    strategy = "ORB"
                    state = "in_pos"
                    trailing_active = False
                    i += 2
                    continue

        i += 1

    # 未決済
    if state == "in_pos":
        exit_p, exit_dt, reason = closes[-1], times[-1], "引け強制"
        state = "closed"

    if state != "closed" or exit_p is None or entry_dt is None:
        return None

    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p,
        stop_p=orig_stop, target_p=target_p,
        qty=qty, pnl=pnl, pct=pct,
        strategy=strategy, reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# 銘柄単位バックテスト
# ─────────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, name: str, df: pd.DataFrame,
                    budget: int, max_risk: int) -> dict | None:
    if df is None or df.empty:
        return None
    daily = split_by_day(df)
    if len(daily) < 3:
        return None

    trades = []
    for _, day_df in daily.items():
        t = backtest_day(day_df, budget, max_risk)
        if t:
            trades.append(t)
    return dict(symbol=symbol, name=name, trades=trades)


def calc_stats(trades: list[dict], budget: int = BUDGET) -> dict:
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, losses=0, win_rate=0.0, pf=0.0,
                    total_pnl=0.0, avg_win=0.0, avg_loss=0.0, max_dd=0.0,
                    equity_curve=[budget])
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    total = sum(t["pnl"] for t in trades)

    # 改善⑥: エクイティカーブ
    eq = budget
    peak = eq
    max_dd = 0.0
    equity_curve = [eq]
    for t in trades:
        eq += t["pnl"]
        equity_curve.append(eq)
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd

    return dict(
        n=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n * 100, pf=pf, total_pnl=total,
        avg_win=gp / len(wins) if wins else 0.0,
        avg_loss=-gl / len(losses) if losses else 0.0,
        max_dd=max_dd, equity_curve=equity_curve,
    )


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────
# HTML レポート (改善⑥: エクイティカーブ含む)
# ─────────────────────────────────────────────────────────────

def build_html(all_items: list[dict], stats: dict,
               days: int, budget: int, source: str) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    s = stats
    cls = "profit" if s["total_pnl"] >= 0 else "loss"

    # エクイティカーブ SVG (改善⑥)
    eq = s.get("equity_curve", [budget])
    if len(eq) > 1:
        w, h = 800, 200
        mn, mx = min(eq), max(eq)
        rng = mx - mn if mx > mn else 1
        points = []
        for i, v in enumerate(eq):
            x = i / (len(eq) - 1) * w
            y = h - (v - mn) / rng * (h - 20) - 10
            points.append(f"{x:.0f},{y:.0f}")
        poly = " ".join(points)
        base_y = h - (budget - mn) / rng * (h - 20) - 10
        svg = f"""
        <svg viewBox="0 0 {w} {h}" style="width:100%;max-width:800px;height:200px;background:#0f172a;border:1px solid #334155;border-radius:6px">
          <line x1="0" y1="{base_y:.0f}" x2="{w}" y2="{base_y:.0f}" stroke="#334155" stroke-dasharray="4"/>
          <polyline points="{poly}" fill="none" stroke="#4ade80" stroke-width="2"/>
          <text x="5" y="15" fill="#94a3b8" font-size="11">資金推移</text>
          <text x="5" y="{base_y-5:.0f}" fill="#94a3b8" font-size="10">{budget:,.0f}</text>
          <text x="{w-80}" y="15" fill="{'#4ade80' if eq[-1]>=budget else '#f87171'}" font-size="12">{eq[-1]:,.0f}円</text>
        </svg>"""
    else:
        svg = ""

    # 戦略別集計
    vs_trades = [t for item in all_items for t in item["trades"] if t["strategy"] == "VolSurge"]
    orb_trades = [t for item in all_items for t in item["trades"] if t["strategy"] == "ORB"]
    vs_s = calc_stats(vs_trades, budget)
    orb_s = calc_stats(orb_trades, budget)

    # 銘柄別テーブル
    sym_rows = ""
    for item in sorted(all_items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        trades = item["trades"]
        if not trades:
            continue
        ist = calc_stats(trades, budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        vs_n = sum(1 for t in trades if t["strategy"] == "VolSurge")
        orb_n = sum(1 for t in trades if t["strategy"] == "ORB")
        # 戦略タグ
        tags = ""
        if orb_n > 0:
            tags += f'<span class="tag tag-orb">ORB</span> '
        if vs_n > 0:
            tags += f'<span class="tag tag-vs">VS</span> '
        sym_rows += f"""
        <tr>
          <td class="sym">{item['name']}<br><small class="code">{item['symbol']}</small> {tags}</td>
          <td>{ist['n']}</td>
          <td>{ist['win_rate']:.0f}%</td><td>{_pf_str(ist['pf'])}</td>
          <td class="{c}">{ist['total_pnl']:+,.0f}円</td>
          <td class="profit">{ist['avg_win']:+,.0f}</td>
          <td class="loss">{ist['avg_loss']:+,.0f}</td>
          <td class="loss">{ist['max_dd']:+.1f}%</td>
        </tr>"""

    # トレード明細 (折りたたみ)
    trade_sections = ""
    for item in sorted(all_items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        if not item["trades"]:
            continue
        tpnl = sum(t["pnl"] for t in item["trades"])
        tc = "profit" if tpnl >= 0 else "loss"
        trows = ""
        for t in sorted(item["trades"], key=lambda x: str(x.get("entry_dt", ""))):
            pc = "profit" if t["pnl"] > 0 else "loss"
            stag = '<span class="tag tag-vs">VS</span>' if t["strategy"] == "VolSurge" else '<span class="tag tag-orb">ORB</span>'
            ed = t["entry_dt"].strftime("%Y-%m-%d %H:%M") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            xd = t["exit_dt"].strftime("%H:%M") if hasattr(t["exit_dt"], "strftime") else str(t["exit_dt"])
            trows += f"""<tr>
              <td>{stag}</td>
              <td>{ed}</td><td>{xd}</td>
              <td>{t['qty']}</td>
              <td>{t['entry_p']:,.1f}</td><td class="loss">{t['stop_p']:,.1f}</td>
              <td class="profit">{t['target_p']:,.1f}</td><td>{t['exit_p']:,.1f}</td>
              <td class="{pc}">{t['pnl']:+,.0f}</td><td class="{pc}">{t['pct']:+.2f}%</td>
              <td>{t['reason']}</td></tr>"""
        trade_sections += f"""
        <details class="trade-section">
          <summary><strong>{item['name']}</strong> <small class="code">{item['symbol']}</small>
            <span class="{tc}">{tpnl:+,.0f}円</span>
            <small>({len(item['trades'])}取引)</small></summary>
          <table><thead><tr>
            <th>戦略</th><th>Entry</th><th>Exit</th><th>株数</th>
            <th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
            <th>損益</th><th>%</th><th>理由</th>
          </tr></thead><tbody>{trows}</tbody></table>
        </details>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>統合デイトレ戦略 — {today}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
  h1{{color:#60a5fa;margin-bottom:4px;font-size:1.6rem}}
  .subtitle{{color:#94a3b8;margin-bottom:20px;font-size:0.85rem}}
  h2{{color:#60a5fa;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #60a5fa;padding-left:10px}}
  table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:0.82rem}}
  th{{background:#1e293b;color:#94a3b8;padding:5px 7px;text-align:center;border:1px solid #334155;white-space:nowrap}}
  td{{padding:4px 7px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
  tr:hover td{{background:#1e293b}}
  .sym{{text-align:left;font-weight:600;min-width:140px}}
  .sym .code{{color:#64748b;font-weight:400;font-size:0.75rem}}
  .profit{{color:#4ade80}}.loss{{color:#f87171}}
  .tag{{display:inline-block;padding:1px 7px;border-radius:99px;font-size:0.72rem;font-weight:600}}
  .tag-orb{{background:#1d4ed8;color:#bfdbfe}}
  .tag-vs{{background:#c2410c;color:#fed7aa}}
  .box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
  .box .item{{text-align:center}}.box .label{{color:#94a3b8;font-size:0.75rem}}
  .box .val{{font-size:1.3rem;font-weight:700}}
  details.trade-section{{margin-bottom:6px;background:#1e293b;border-radius:6px}}
  details.trade-section summary{{cursor:pointer;padding:8px 12px;font-size:0.85rem;list-style:none}}
  details.trade-section summary::-webkit-details-marker{{display:none}}
  details.trade-section summary::before{{content:"▶ ";font-size:0.7rem;color:#94a3b8}}
  details.trade-section[open] summary::before{{content:"▼ "}}
  details.trade-section table{{margin:0 12px 6px;width:calc(100% - 24px)}}
</style></head><body>
<h1>統合デイトレ戦略 (VolSurge + ORB)</h1>
<p class="subtitle">
  生成: {today} ／ 期間: {days}日 ／ source: {source} ／ 予算: {budget:,}円<br>
  改善: ①固定リスク額{MAX_RISK:,}円/trade ②銘柄PFフィルタ ③トレーリングストップ
  ④前場限定({ENTRY_CUTOFF.strftime('%H:%M')}まで) ⑤VolSurge優先 ⑥損益曲線
</p>

<div class="box">
  <div class="item"><div class="label">総損益</div><div class="val {cls}">{s['total_pnl']:+,.0f}円</div></div>
  <div class="item"><div class="label">取引数</div><div class="val">{s['n']}</div></div>
  <div class="item"><div class="label">勝率</div><div class="val">{s['win_rate']:.1f}%</div></div>
  <div class="item"><div class="label">PF</div><div class="val">{_pf_str(s['pf'])}</div></div>
  <div class="item"><div class="label">最大DD</div><div class="val loss">{s['max_dd']:+.1f}%</div></div>
  <div class="item"><div class="label">VolSurge</div><div class="val" style="color:#fb923c">{vs_s['n']}回 PF{_pf_str(vs_s['pf'])}</div></div>
  <div class="item"><div class="label">ORB</div><div class="val" style="color:#fbbf24">{orb_s['n']}回 PF{_pf_str(orb_s['pf'])}</div></div>
</div>

<h2>資金推移</h2>
{svg}

<h2>戦略別サマリー</h2>
<table><thead><tr>
  <th>戦略</th><th>取引</th><th>勝率</th><th>PF</th><th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th>
</tr></thead><tbody>
<tr><td style="color:#fb923c">VolSurge</td><td>{vs_s['n']}</td><td>{vs_s['win_rate']:.1f}%</td><td>{_pf_str(vs_s['pf'])}</td>
<td class="{'profit' if vs_s['total_pnl']>=0 else 'loss'}">{vs_s['total_pnl']:+,.0f}</td>
<td class="profit">{vs_s['avg_win']:+,.0f}</td><td class="loss">{vs_s['avg_loss']:+,.0f}</td><td class="loss">{vs_s['max_dd']:+.1f}%</td></tr>
<tr><td style="color:#fbbf24">ORB</td><td>{orb_s['n']}</td><td>{orb_s['win_rate']:.1f}%</td><td>{_pf_str(orb_s['pf'])}</td>
<td class="{'profit' if orb_s['total_pnl']>=0 else 'loss'}">{orb_s['total_pnl']:+,.0f}</td>
<td class="profit">{orb_s['avg_win']:+,.0f}</td><td class="loss">{orb_s['avg_loss']:+,.0f}</td><td class="loss">{orb_s['max_dd']:+.1f}%</td></tr>
</tbody></table>

<h2>銘柄別サマリー</h2>
<table><thead><tr>
  <th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
  <th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th>
</tr></thead><tbody>{sym_rows}</tbody></table>

<h2>個別トレード一覧</h2>
{trade_sections}
</body></html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="統合デイトレ戦略")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--max-risk", type=int, default=MAX_RISK)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    if args.symbols:
        targets = [(s, s) for s in args.symbols]
    else:
        targets = DAYTRADE_SYMBOLS

    print(f"統合デイトレ戦略: {len(targets)}銘柄 / {args.days}日 / "
          f"予算{args.budget:,}円 / リスク{args.max_risk:,}円/trade", flush=True)

    # データ取得
    symbols_list = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols_list, args.days, source=args.source)
    print(f"  取得成功: {len(fetched)}/{len(symbols_list)}銘柄", flush=True)

    # 予算フィルタ
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄 (株価{max_price:,.0f}円以下)",
          flush=True)

    # バックテスト
    print("バックテスト実行中...", flush=True)
    all_items = []
    for i, (sym, name) in enumerate(targets, 1):
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym], args.budget, args.max_risk)
        if r:
            all_items.append(r)

    # 改善②: 銘柄スコアリング (PF < 1.0 の銘柄を除外して再集計)
    scored = []
    excluded_syms = []
    for item in all_items:
        ist = calc_stats(item["trades"], args.budget)
        if ist["n"] >= 3 and ist["pf"] >= 1.0:
            scored.append(item)
        elif item["trades"]:
            excluded_syms.append(
                f"{item['symbol']} (PF={_pf_str(ist['pf'])}, n={ist['n']})")

    if excluded_syms:
        print(f"  銘柄フィルタ: {len(all_items)} → {len(scored)}銘柄 "
              f"(PF≥1.0 & n≥3)", flush=True)

    use_items = scored if scored else all_items

    # 全トレード統合
    all_trades = sorted(
        [t for item in use_items for t in item["trades"]],
        key=lambda x: str(x.get("entry_dt", ""))
    )
    stats = calc_stats(all_trades, args.budget)

    # コンソール
    print()
    print("=" * 70)
    print(f"  統合デイトレ戦略  {args.days}日 / {len(use_items)}銘柄")
    print("=" * 70)
    print(f"  取引数: {stats['n']}  勝率: {stats['win_rate']:.1f}%  "
          f"PF: {_pf_str(stats['pf'])}  損益: {stats['total_pnl']:+,.0f}円  "
          f"DD: {stats['max_dd']:+.1f}%")
    print("=" * 70)

    # HTML
    out = Path(f"daytrade_integrated_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(use_items, stats, args.days, args.budget,
                              args.source), encoding="utf-8")
    print(f"\nHTMLレポート: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
