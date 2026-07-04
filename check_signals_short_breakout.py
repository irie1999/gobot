"""
check_signals_short_breakout.py  ―  ショートブレイクアウト バックテスト
=================================================================
逆指値売り (stop_sell) を使ったブレイクアウト系ショート戦略。

【戦略】
  DON_S: ドンチャン安値ブレイク — 終値 < 20日最安値 + MA50 下方
  MOM_S: ネガティブモメンタム — ROC(10) < -3% + MA25 < MA75 (デッドクロス)
  GAP_S: ギャップダウン + 戻り失敗 — 2%以上GD + 出来高急増 + IBS < 0.35

論文根拠:
  George & Hwang (2004) "The 52-Week High and Momentum Investing"
  Moskowitz, Ooi & Pedersen (2012) "Time Series Momentum"

【使い方】
  python check_signals_short_breakout.py               # 全期間(365日)
  python check_signals_short_breakout.py --days 90
  python check_signals_short_breakout.py --date 2026-04-08
  python check_signals_short_breakout.py --signal-only
  python check_signals_short_breakout.py --no-browser
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    fetch_n225_return,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, LIMIT_ENTRY_MARGIN_PCT,
    INITIAL_CASH as _INITIAL_CASH,
    WORKERS as _DEFAULT_WORKERS,
)
from risk_metrics import enrich_backtest_result, calc_hold_stats

JST     = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]

WATCHLIST: list[tuple[str, str, str]] = [
    # ── DON_S: Walk-forward 選定 (2026-05-20) ──
    ("9552.T", "クオンツ総研ホールディングス",       "DON_S"),
    ("4680.T", "ラウンドワン",                       "DON_S"),
    ("4343.T", "イオンファンタジー",                 "DON_S"),
    ("5011.T", "ニチレキグループ",                   "DON_S"),
    ("3436.T", "ＳＵＭＣＯ",                         "DON_S"),
    ("4480.T", "メドレー",                           "DON_S"),
    ("4849.T", "エン",                               "DON_S"),
    ("3199.T", "綿半ホールディングス",               "DON_S"),
    ("6615.T", "ユー・エム・シー・エレクトロニクス", "DON_S"),
    ("7199.T", "プレミアグループ",                   "DON_S"),
    # ── MOM_S: Walk-forward 選定 (2026-05-20) ──
    ("2791.T", "大黒天物産",                         "MOM_S"),
    ("9612.T", "ラックランド",                       "MOM_S"),
    ("7095.T", "Ｍａｃｂｅｅ　Ｐｌａｎｅｔ",       "MOM_S"),
    ("4680.T", "ラウンドワン",                       "MOM_S"),
    ("3901.T", "マークラインズ",                     "MOM_S"),
    ("9552.T", "クオンツ総研ホールディングス",       "MOM_S"),
    ("9501.T", "東京電力ホールディングス",           "MOM_S"),
    # ── GAP_S: Walk-forward 選定なし (0銘柄) ──
]


# ── ショートブレイクアウト用インジケーター ───────────────────────

def calc_donchian_short(df: pd.DataFrame) -> pd.DataFrame:
    """ドンチャン安値ブレイク売りシグナル。終値 < 20日最安値 + MA50 下方。"""
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]

    low_N  = l.rolling(20).min().shift(1)   # 過去20日の最安値（前日まで）
    ma50   = c.rolling(50).mean()

    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()

    df["low_N"]     = low_N
    df["ma50"]      = ma50
    df["atr"]       = atr
    df["entry_sig"] = (c < low_N) & (c < ma50)
    return df


def calc_momentum_short(df: pd.DataFrame) -> pd.DataFrame:
    """ネガティブモメンタム売りシグナル。ROC(10) < -3% + MA25 < MA75。"""
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]

    roc10 = c.pct_change(10) * 100
    ma25  = c.rolling(25).mean()
    ma75  = c.rolling(75).mean()

    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()

    df["roc10"]     = roc10
    df["ma25"]      = ma25
    df["ma75"]      = ma75
    df["atr"]       = atr
    df["entry_sig"] = (roc10 < -3.0) & (ma25 < ma75)
    return df


def calc_gap_short(df: pd.DataFrame) -> pd.DataFrame:
    """ギャップダウン + 戻り失敗の売りシグナル。"""
    df = df.copy()
    c   = df["close"]
    h   = df["high"]
    l   = df["low"]
    o   = df["open"]
    vol = df["volume"]

    prev_c    = c.shift(1)
    gap_down  = o < prev_c * 0.98              # 2%以上ギャップダウン
    vol_ma    = vol.rolling(20).mean()
    vol_spike = vol > vol_ma * 1.5             # 出来高急増
    bar_range = h - l
    ibs       = np.where(bar_range > 0, (c - l) / bar_range, 0.5)
    near_low  = pd.Series(ibs, index=c.index) < 0.35   # 安値引け（戻り失敗）

    tr  = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean()

    df["atr"]       = atr
    df["entry_sig"] = gap_down & vol_spike & near_low
    return df


def calc_gap_short_tf(df: pd.DataFrame) -> pd.DataFrame:
    """GAP_S のトレンドフィルタ版。GAP_S はトレンドフィルタが無く、中期上昇
    トレンド中の一時的なギャップダウンでも空売りしてしまう(踏み上げリスク)。
    終値 < MA50 を追加し、既に下降基調にある銘柄の売りに限定する
    (買い側 VOLTF の裏返し)。"""
    df = calc_gap_short(df)
    ma50 = df["close"].rolling(50).mean()
    df["entry_sig"] = df["entry_sig"] & (df["close"] < ma50)
    return df


# ── プリセット切替 (TRADING_MODE: conservative / aggressive) ──────────────────
STRATEGY_PARAMS_CONSERVATIVE = {
    "DON_S": (calc_donchian_short,  0.0, 1.5, 3.0),
    "MOM_S": (calc_momentum_short,  0.0, 1.5, 3.0),
    "GAP_S": (calc_gap_short,       0.0, 2.0, 1.5),  # sweep最適: sm=2.0, tm=1.5 (PF1.30)
    "GAP_S_TF": (calc_gap_short_tf, 0.0, 2.0, 1.5),  # GAP_S + MA50下方フィルタ
}
STRATEGY_PARAMS_AGGRESSIVE = {
    "DON_S": (calc_donchian_short,  0.0, 1.5, 2.0),
    "MOM_S": (calc_momentum_short,  0.0, 1.5, 2.0),
    "GAP_S": (calc_gap_short,       0.0, 2.0, 1.5),  # sweep最適: sm=2.0, tm=1.5
    "GAP_S_TF": (calc_gap_short_tf, 0.0, 2.0, 1.5),  # GAP_S + MA50下方フィルタ
}

import os as _os
TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()
if TRADING_MODE == "aggressive":
    STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
else:
    STRATEGY_PARAMS = STRATEGY_PARAMS_CONSERVATIVE
    TRADING_MODE = "conservative"

ENTRY_TYPE = "stop_sell"


# 薄サンプル減点の閾値: 最長窓(365日)の実取引数がこれ未満だと、勝率/PF/安定の
# 品質点(最大80点)を線形に割り引く。少数トレードでの過大評価(例: 2取引で91点)を抑制。
MIN_TRADES_FOR_FULL_BT = 5


def calc_recommend_score(period_results: dict) -> tuple[int, str]:
    results = [r for r in period_results.values() if r and r.get("trades", 0) > 0]
    if not results:
        return 0, "-"
    avg_wr   = sum(r["win_rate"] for r in results) / len(results)
    avg_pf   = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                   for r in results) / len(results)
    stable   = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = sum(r["trades"] for r in results)
    # 薄サンプル減点: 実サンプル数=最長窓の取引数で信頼度を判定し、
    # 勝率/PF/安定(最大80点)を線形に割り引く(少数トレードの満点化を抑制)。
    _sample = max((r["trades"] for r in results), default=0)
    confidence = min(1.0, _sample / MIN_TRADES_FOR_FULL_BT) if MIN_TRADES_FOR_FULL_BT > 0 else 1.0
    quality = avg_wr * 0.4 + (avg_pf / 10) * 30 + stable * 20
    score = round(quality * confidence + min(t_trades / 20, 1) * 10)
    rank = "★★★" if score >= 80 else "★★" if score >= 60 else "★" if score >= 40 else "△"
    return score, rank


def check_signal_on_date(symbol: str, strategy: str,
                         target_date=None) -> dict | None:
    """target_date の終値でショートシグナルが出ているか確認。"""
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, 365)
    if df is None or len(df) < 5:
        return None
    try:
        df = calc_fn(df)
    except Exception:
        return None

    if target_date is None:
        prev_idx = -1
    else:
        ts    = pd.Timestamp(target_date)
        cands = df.index[df.index <= ts]
        if len(cands) < 1:
            return None
        prev_idx = df.index.get_loc(cands[-1])

    prev      = df.iloc[prev_idx]
    entry_sig = bool(prev.get("entry_sig", False))
    atr_v     = float(prev.get("atr", 0))
    if not entry_sig or atr_v <= 0:
        return None

    close_prev = float(prev["close"])
    current_p  = float(df.iloc[prev_idx]["close"])

    order_p     = close_prev - atr_v * em
    sl          = order_p + atr_v * sm   # 損切り (ABOVE)
    tp          = order_p - atr_v * tm   # 目標   (BELOW)
    limit_entry = order_p * (1.0 - LIMIT_ENTRY_MARGIN_PCT)  # 指値下限 (-3%)

    sig_dt   = df.index[prev_idx]
    sig_date = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else str(sig_dt)

    return dict(
        order_price=round(order_p, 0),
        limit_entry_price=round(limit_entry, 0),  # 指値下限 (-3%)
        stop_price=round(sl, 0),
        target_price=round(tp, 0),
        current_price=current_p,
        signal_date=sig_date,
        signal_price=round(close_prev, 0),
    )


def backtest_one(symbol: str, name: str, strategy: str,
                 max_hold: int | None = None) -> dict | None:
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, max(PERIODS))
    if df is None:
        return None
    period_results: dict[int, dict] = {}
    for days in PERIODS:
        r = run_limit_backtest(symbol, name, df, calc_fn,
                               em, sm, tm, days, strategy,
                               entry_type=ENTRY_TYPE,
                               max_hold=max_hold)
        if r and r["trades"] >= 1:
            period_results[days] = r
    return dict(symbol=symbol, name=name, strategy=strategy,
                period_results=period_results, today_sig=None)


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_html(all_items: list[dict], show_days: int,
               date_label: str = "本日", run_cmd: str = "") -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    n225_ret  = fetch_n225_return(show_days)

    strategy_summary: dict[str, dict] = {}
    for item in all_items:
        strat = item["strategy"]
        if strat not in strategy_summary:
            strategy_summary[strat] = dict(trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0, trade_log=[])
        pr = item["period_results"].get(show_days) or {}
        if pr:
            strategy_summary[strat]["trades"] += pr["trades"]
            strategy_summary[strat]["wins"]   += pr["wins"]
            strategy_summary[strat]["pnl"]    += pr["total_pnl"]
            for t in pr.get("trade_log", []):
                if t["pnl"] > 0:
                    strategy_summary[strat]["gp"] += t["pnl"]
                else:
                    strategy_summary[strat]["gl"] += abs(t["pnl"])
            strategy_summary[strat]["trade_log"].extend(pr.get("trade_log", []))

    summary_rows = ""
    for strat, s in strategy_summary.items():
        wr       = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        pf       = s["gp"] / s["gl"] if s["gl"] > 0 else (float("inf") if s["gp"] > 0 else 0)
        cls      = "profit" if s["pnl"] >= 0 else "loss"
        enriched = enrich_backtest_result({"trade_log": s["trade_log"]}, _INITIAL_CASH)
        max_dd   = enriched.get("max_drawdown_pct", 0.0)
        max_cl   = enriched.get("max_consecutive_losses", 0)
        sharpe   = enriched.get("sharpe", 0.0)
        hs       = enriched.get("hold_stats", {})
        alpha    = s["pnl"] / _INITIAL_CASH * 100 - n225_ret if _INITIAL_CASH > 0 else 0
        a_cls    = "profit" if alpha >= 0 else "loss"
        dd_cls   = "profit" if max_dd < 10 else ("loss" if max_dd > 20 else "")
        hold_parts = []
        if hs.get("target_n", 0): hold_parts.append(f"目標{hs['target_avg']:.1f}({hs['target_n']})")
        if hs.get("stop_n",   0): hold_parts.append(f"損切{hs['stop_avg']:.1f}({hs['stop_n']})")
        if hs.get("tc_n",     0): hold_parts.append(f"TC{hs['tc_avg']:.0f}({hs['tc_n']})")
        summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td>{s['trades']}</td><td>{s['wins']}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf)}</td>
          <td class="{cls}">{s['pnl']:+,.0f}円</td>
          <td class="{dd_cls}">{max_dd:.1f}%</td>
          <td>{max_cl}</td><td>{sharpe:.2f}</td>
          <td class="{a_cls}">{alpha:+.1f}%</td>
          <td>{hs.get('avg',0):.1f}日<br><small class="hold-break">{' / '.join(hold_parts)}</small></td>
        </tr>"""

    signal_items = [(item, calc_recommend_score(item["period_results"]))
                    for item in all_items
                    if item["today_sig"]
                    and not item["today_sig"].get("_pending_lookback")
                    and not item["today_sig"].get("_filled_holding")]
    signal_items.sort(key=lambda x: x[1][0], reverse=True)

    signal_rows = ""
    for item, (score, rank) in signal_items:
        sig      = item["today_sig"]
        strat    = item["strategy"]
        rank_cls = {"★★★": "rank-s", "★★": "rank-a", "★": "rank-b"}.get(rank, "rank-c")
        signal_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td class="score-cell"><span class="{rank_cls}">{rank}</span><br>{score}点</td>
          <td>{sig['signal_date']}</td>
          <td>{sig['signal_price']:,.0f}</td>
          <td>{sig['current_price']:,.0f}</td>
          <td class="short-entry">{sig['order_price']:,.0f}</td>
          <td class="loss">{sig['stop_price']:,.0f}</td>
          <td class="profit">{sig['target_price']:,.0f}</td>
        </tr>"""
    if not signal_rows:
        signal_rows = f'<tr><td colspan="9" style="text-align:center;color:#94a3b8">{date_label} シグナルなし</td></tr>'

    period_headers  = "".join(f"<th colspan='4'>{p}日</th>" for p in PERIODS)
    period_subheads = "<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(PERIODS)

    stock_rows = ""
    for strat in ["DON_S", "MOM_S", "GAP_S"]:
        items = [i for i in all_items if i["strategy"] == strat]
        items.sort(key=lambda x: (x["period_results"].get(show_days) or {}).get("total_pnl", -999999), reverse=True)
        for item in items:
            cells = ""
            for p in PERIODS:
                r = item["period_results"].get(p)
                if not r:
                    cells += "<td>-</td><td>-</td><td>-</td><td>-</td>"
                else:
                    pc = "profit" if r["total_pnl"] >= 0 else "loss"
                    cells += (f"<td>{r['trades']}</td>"
                              f"<td>{r['win_rate']:.0f}%</td>"
                              f"<td>{_pf_str(r['pf'])}</td>"
                              f"<td class='{pc}'>{r['total_pnl']:+,.0f}</td>")
            pr_show   = item["period_results"].get(show_days) or {}
            hs_item   = calc_hold_stats(pr_show.get("trade_log", []))
            hold_cell = f"{hs_item['avg']:.1f}日" if hs_item["count"] > 0 else "-"
            sig_m = item["today_sig"]
            mark = ("🔔" if sig_m
                         and not sig_m.get("_pending_lookback")
                         and not sig_m.get("_filled_holding")
                    else "")
            stock_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}{mark}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          {cells}
          <td>{hold_cell}</td>
        </tr>"""

    trade_sections = ""
    for item in all_items:
        pr   = item["period_results"].get(show_days) or {}
        logs = pr.get("trade_log") or []
        if not logs and not item.get("today_sig"):
            continue
        trade_rows = ""
        for t in logs:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            e_str   = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            x_str   = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
            sig_dt  = t.get("signal_dt")
            s_str   = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else (str(sig_dt) if sig_dt else "-")
            trade_rows += f"""
              <tr>
                <td>{s_str}</td><td>{t.get('signal_price', '-')}</td>
                <td>{e_str}</td><td>{x_str}</td>
                <td class="short-entry">{t['entry_p']:,.0f}</td>
                <td>{t['exit_p']:,.0f}</td>
                <td>{t['qty']}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['hold_days']}日</td>
                <td>{t['reason']}</td>
              </tr>"""
        # 未約定/保有中シグナルを取引詳細の末尾に追記
        if item.get("today_sig"):
            sig = item["today_sig"]
            _sig_date_str = sig["signal_date"]
            _already = any(
                t.get("signal_dt") is not None
                and t["signal_dt"].strftime("%Y-%m-%d") == _sig_date_str
                for t in logs
            )
            if _already:
                sig = None  # skip
            if sig:
                if sig.get("_filled_holding"):
                    row_bg    = "background:rgba(34,197,94,0.12)"
                    reason_td = '<td style="color:#4ade80">✅ 保有中（約定済み）</td>'
                elif sig.get("_pending_lookback"):
                    row_bg    = "background:rgba(245,158,11,0.12)"
                    reason_td = '<td style="color:#f59e0b">⏳ 未約定（継続中）</td>'
                else:
                    row_bg    = "background:rgba(245,158,11,0.12)"
                    reason_td = '<td style="color:#f59e0b">⏳ 未約定</td>'
                trade_rows += f"""
              <tr style="{row_bg}">
                <td>{sig['signal_date']}</td><td>{sig['signal_price']:,.0f}</td>
                <td>-</td><td>-</td>
                <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                {reason_td}
              </tr>"""
        strat     = item["strategy"]
        pnl_total = pr.get("total_pnl", 0)
        pc2       = "profit" if pnl_total >= 0 else "loss"
        hs_sec    = calc_hold_stats(logs)
        hold_stat = ""
        if hs_sec["count"] > 0:
            parts = []
            if hs_sec["target_n"]: parts.append(f"目標{hs_sec['target_avg']:.1f}日({hs_sec['target_n']})")
            if hs_sec["stop_n"]:   parts.append(f"損切{hs_sec['stop_avg']:.1f}日({hs_sec['stop_n']})")
            if hs_sec["tc_n"]:     parts.append(f"TC{hs_sec['tc_avg']:.0f}日({hs_sec['tc_n']})")
            hold_stat = f'<p class="hold-stat">保有日数 — 平均:{hs_sec["avg"]:.1f}日 | {" / ".join(parts)}</p>'
        trade_sections += f"""
      <div class="trade-section">
        <h3>{item['symbol']} {item['name']}
          <span class="tag tag-{strat.lower()}">{strat}</span>
          <span class="{pc2}">{pnl_total:+,.0f}円</span>
          <small>（{show_days}日）</small>
        </h3>
        {hold_stat}
        <table>
          <thead><tr>
            <th>シグナル日</th><th>シグナル時株価</th>
            <th>エントリー</th><th>エグジット</th>
            <th>売り逆指値</th><th>エグジット価格</th>
            <th>数量</th><th>損益(円)</th><th>損益(%)</th>
            <th>保有日数</th><th>理由</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>ショートブレイクアウト バックテスト — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#f87171; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#f87171; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #f87171; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym  {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .short-entry {{ color:#fb923c; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-don_s  {{ background:#78350f; color:#fde68a; }}
  .tag-mom_s  {{ background:#1e1b4b; color:#c4b5fd; }}
  .tag-gap_s  {{ background:#831843; color:#fbcfe8; }}
  .signal-badge {{ background:#f87171; color:#000; padding:2px 8px; border-radius:4px; font-size:0.8rem; }}
  .trade-section {{ margin-bottom:20px; }}
  .hold-stat {{ color:#a5b4fc; font-size:0.82rem; margin-bottom:6px; }}
  .hold-break {{ color:#94a3b8; font-size:0.70rem; font-weight:normal; white-space:nowrap; }}
  .rank-s {{ background:#fbbf24; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-a {{ background:#4ade80; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-b {{ background:#38bdf8; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-c {{ background:#94a3b8; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .score-cell {{ text-align:center; }}
</style>
</head>
<body>
<h1>ショートブレイクアウト バックテスト</h1>
<p class="subtitle">
  生成日: {today_str} ／ シグナル確認日: {date_label} ／ 表示期間: {show_days}日<br>
  エントリー: <strong>逆指値売り</strong>（下がれば売る）<br>
  コストモデル: スリッページ <strong>{SLIPPAGE_STOP_PCT*100:.2f}%</strong> ／
  手数料 <strong>片道 {FEE_PCT_ONE_WAY*100:.2f}%</strong> ／
  ベンチマーク: 日経平均 ({show_days}日) <strong>{n225_ret:+.1f}%</strong>
</p>

<h2>戦略サマリー（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益合計</th>
    <th>MaxDD%</th><th>連敗</th><th>Sharpe</th><th>α vs 日経</th><th>平均保有</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>シグナル ({date_label}) <span class="signal-badge">ショート候補</span></h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>スコア</th><th>シグナル日</th><th>シグナル時株価</th>
    <th>現在値</th><th>売り逆指値</th><th>損切り</th><th>目標</th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
</table>

<h2>銘柄別バックテスト（4期間比較）</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">銘柄</th><th rowspan="2">戦略</th>
      {period_headers}
      <th rowspan="2">平均保有<br><small>({show_days}日)</small></th>
    </tr>
    <tr>{period_subheads}</tr>
  </thead>
  <tbody>{stock_rows}</tbody>
</table>

<h2>個別トレード一覧（{show_days}日）</h2>
{trade_sections}

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="ショートブレイクアウト バックテスト")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None)
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int, default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    print(f"ショートブレイクアウト開始 ({len(WATCHLIST)}銘柄) シグナル確認日: {date_label}...", flush=True)

    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in WATCHLIST}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass
            if done % 6 == 0 or done == len(WATCHLIST):
                print(f"  {done}/{len(WATCHLIST)} 完了", flush=True)

    order = {(s, st): i for i, (s, _, st) in enumerate(WATCHLIST)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    for item in all_items:
        item["today_sig"] = check_signal_on_date(item["symbol"], item["strategy"], sig_date)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    signals_today = [(i, calc_recommend_score(i["period_results"]))
                     for i in all_items if i["today_sig"]]
    signals_today.sort(key=lambda x: x[1][0], reverse=True)
    print(f"\n【ショートシグナル ({date_label})】 {len(signals_today)}件")
    for item, (score, rank) in signals_today:
        sig = item["today_sig"]
        print(f"  {item['symbol']:<12} {item['name']:<20} {item['strategy']:<8}"
              f" {sig['signal_date']:<12} {sig['order_price']:>8,.0f}"
              f" 損切:{sig['stop_price']:>8,.0f} 目標:{sig['target_price']:>8,.0f}"
              f"  {rank}{score}点")

    if args.signal_only:
        return

    show_days   = args.days
    date_suffix = args.date if args.date else today
    out_path    = Path(f"watchlist_short_brk_{date_suffix}.html")
    out_path.write_text(build_html(all_items, show_days, date_label), encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")
    if not args.no_browser:
        open_html(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
