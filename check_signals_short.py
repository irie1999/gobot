"""
check_signals_short.py  ―  監視銘柄 逆指値ショートエントリー バックテスト
=================================================================
check_signals_stop.py の空売り版。
エントリー条件: 安値 ≤ 前日終値（下がれば売る）= 逆指値売り（信用売り）

【空売りの仕組み】
  order_p = close_prev - ATR × em   （em=0.0 → 終値ちょうど）
  stop    = order_p + ATR × sm      （損切り = 買い戻し価格: 上）
  target  = order_p - ATR × tm      （目標  = 買い戻し価格: 下）
  PnL     = (entry_p - exit_p) × 株数 - 手数料

  ※ 逆指値売り約定は不利な方向（安め）にスリッページ

【使い方】
  python check_signals_short.py               # 全期間(365日) HTMLレポート
  python check_signals_short.py --days 90     # 直近90日
  python check_signals_short.py --date 2026-03-28  # 任意日シグナル確認
  python check_signals_short.py --no-browser
  python check_signals_short.py --signal-only

【WATCHLIST の更新方法】
  scan_walkforward.py に --family short_stop オプションを追加後、
  同様の Walk-forward パイプラインで短期売りに適した銘柄を選定してください。
  現状のリストは参考用プレースホルダーです。
"""

from __future__ import annotations

import argparse
import os as _os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from backtest_limit_entry import (
    fetch,
    calc_macd_short, calc_a7_short, calc_rsi2_short,
    calc_donchian_short, calc_vol_breakdown, calc_momentum_short,
    run_limit_backtest,
    fetch_n225_return,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, LIMIT_ENTRY_MARGIN_PCT,
    INITIAL_CASH as _INITIAL_CASH,
    WORKERS as _DEFAULT_WORKERS,
)
from risk_metrics import enrich_backtest_result, calc_hold_stats

JST     = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]

# ── ショート監視銘柄リスト ─────────────────────────────────────────
# 注意: このWATCHLISTは暫定プレースホルダーです。
# 実運用では scan_walkforward.py でショート向けの銘柄を選定し更新してください。
# 信用取引の売り建て規制・逆日歩・調達コストを別途確認すること。
WATCHLIST: list[tuple[str, str, str]] = [
    # ── MACD ショート候補（下降トレンド + MACD ベアリッシュ）──
    ("7201.T", "日産自動車",             "MACD_S"),
    ("9984.T", "ソフトバンクグループ",   "MACD_S"),
    ("6752.T", "パナソニックHD",         "MACD_S"),
    ("6702.T", "富士通",                 "MACD_S"),
    ("4689.T", "LINEヤフー",             "MACD_S"),
    ("2413.T", "エムスリー",             "MACD_S"),
    # ── A7 ショート候補（ストキャスデスクロス + MA75下）──
    ("4543.T", "テルモ",                 "A7_S"),
    ("6981.T", "村田製作所",             "A7_S"),
    ("6758.T", "ソニーグループ",         "A7_S"),
    ("6861.T", "キーエンス",             "A7_S"),
    ("4519.T", "中外製薬",               "A7_S"),
    ("4568.T", "第一三共",               "A7_S"),
    # ── RSI2 ショート候補（RSI2≥90 + MA200下 + IBS高）──
    ("8035.T", "東京エレクトロン",       "RSI2_S"),
    ("6367.T", "ダイキン工業",           "RSI2_S"),
    ("7974.T", "任天堂",                 "RSI2_S"),
    ("4063.T", "信越化学工業",           "RSI2_S"),
    ("6594.T", "日本電産（ニデック）",   "RSI2_S"),
    ("4901.T", "富士フイルムHD",         "RSI2_S"),
    # ── DON_S ドンチャン安値ブレイクダウン候補 ──
    ("7201.T", "日産自動車",             "DON_S"),
    ("4689.T", "LINEヤフー",             "DON_S"),
    ("2413.T", "エムスリー",             "DON_S"),
    ("6752.T", "パナソニックHD",         "DON_S"),
    ("4901.T", "富士フイルムHD",         "DON_S"),
    ("6594.T", "日本電産（ニデック）",   "DON_S"),
    ("6981.T", "村田製作所",             "DON_S"),
    ("4568.T", "第一三共",               "DON_S"),
    # ── VOL_S 出来高急増ブレイクダウン候補 ──
    ("9984.T", "ソフトバンクグループ",   "VOL_S"),
    ("6758.T", "ソニーグループ",         "VOL_S"),
    ("8035.T", "東京エレクトロン",       "VOL_S"),
    ("6861.T", "キーエンス",             "VOL_S"),
    ("4063.T", "信越化学工業",           "VOL_S"),
    ("6367.T", "ダイキン工業",           "VOL_S"),
    ("6702.T", "富士通",                 "VOL_S"),
    ("4543.T", "テルモ",                 "VOL_S"),
    # ── MOM_S モメンタム下落候補 ──
    ("7201.T", "日産自動車",             "MOM_S"),
    ("2413.T", "エムスリー",             "MOM_S"),
    ("4689.T", "LINEヤフー",             "MOM_S"),
    ("6752.T", "パナソニックHD",         "MOM_S"),
    ("9984.T", "ソフトバンクグループ",   "MOM_S"),
    ("6702.T", "富士通",                 "MOM_S"),
    ("7974.T", "任天堂",                 "MOM_S"),
    ("4568.T", "第一三共",               "MOM_S"),
]

# ── ショート戦略パラメータ ─────────────────────────────────────────
# conservative (デフォルト): 2R 設定
STRATEGY_PARAMS_CONSERVATIVE = {
    "MACD_S":  (calc_macd_short,     0.0, 1.5, 3.0),
    "A7_S":    (calc_a7_short,       0.0, 1.5, 3.0),
    "RSI2_S":  (calc_rsi2_short,     0.0, 2.0, 4.0),
    "DON_S":   (calc_donchian_short, 0.0, 1.5, 3.0),
    "VOL_S":   (calc_vol_breakdown,  0.0, 1.5, 3.0),
    "MOM_S":   (calc_momentum_short, 0.0, 1.5, 3.0),
}
# aggressive: 1.5R 設定（回転率優先）
STRATEGY_PARAMS_AGGRESSIVE = {
    "MACD_S":  (calc_macd_short,     0.0, 1.0, 1.5),
    "A7_S":    (calc_a7_short,       0.0, 1.0, 1.5),
    "RSI2_S":  (calc_rsi2_short,     0.0, 1.2, 1.8),
    "DON_S":   (calc_donchian_short, 0.0, 1.0, 1.5),
    "VOL_S":   (calc_vol_breakdown,  0.0, 1.0, 1.5),
    "MOM_S":   (calc_momentum_short, 0.0, 1.0, 1.5),
}

TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()
if TRADING_MODE == "aggressive":
    STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
else:
    STRATEGY_PARAMS = STRATEGY_PARAMS_CONSERVATIVE
    TRADING_MODE = "conservative"

ENTRY_TYPE = "short_stop"   # 逆指値売り（安値 ≤ 注文価格 で約定）


def calc_recommend_score(period_results: dict) -> tuple[int, str]:
    """
    バックテスト成績からおすすめスコア(0-100)とランクを計算。
    ロング版と同一ロジック。
    """
    results = [r for r in period_results.values() if r and r.get("trades", 0) > 0]
    if not results:
        return 0, "-"

    avg_wr   = sum(r["win_rate"] for r in results) / len(results)
    avg_pf   = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                   for r in results) / len(results)
    stable   = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = sum(r["trades"] for r in results)

    score = round(
        avg_wr * 0.4
        + (avg_pf / 10) * 30
        + stable * 20
        + min(t_trades / 20, 1) * 10
    )
    rank = "★★★" if score >= 80 else "★★" if score >= 60 else "★" if score >= 40 else "△"
    return score, rank


def check_signal_on_date(symbol: str, strategy: str,
                         target_date=None) -> dict | None:
    """target_date にシグナルが出ているか確認（ショート版）。"""
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, 365)
    if df is None or len(df) < 5:
        return None
    try:
        df = calc_fn(df)
    except Exception:
        return None

    if target_date is None:
        prev_idx, next_idx = -1, -1
    else:
        ts = pd.Timestamp(target_date)
        cands = df.index[df.index <= ts]
        if len(cands) < 1:
            return None
        prev_idx = df.index.get_loc(cands[-1])
        next_idx = prev_idx

    prev      = df.iloc[prev_idx]
    next_row  = df.iloc[next_idx]
    entry_sig = bool(prev.get("entry_sig", False))
    atr_v     = float(prev.get("atr", 0))
    if not entry_sig or atr_v <= 0:
        return None

    close_prev = float(prev["close"])
    current_p  = float(next_row["close"])

    # 逆指値売り: 終値 - ATR×em（em=0.0なら終値ちょうど）
    order_p = close_prev - atr_v * em
    sl      = order_p + atr_v * sm   # 損切り（上）
    tp      = order_p - atr_v * tm   # 目標（下）
    # 逆指値→指値の指値下限（最低でもこの価格で売れる）
    limit_short_entry = order_p * (1.0 - LIMIT_ENTRY_MARGIN_PCT)

    sig_dt   = df.index[prev_idx]
    sig_date = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else str(sig_dt)

    return dict(
        order_price=round(order_p, 0),              # 逆指値トリガー価格（売り）
        limit_short_entry=round(limit_short_entry, 0),  # 指値下限 (-1%)
        stop_price=round(sl, 0),                    # 損切り（買い戻し）
        target_price=round(tp, 0),                  # 目標（買い戻し）
        current_price=current_p,
        signal_date=sig_date,
        signal_price=round(close_prev, 0),
    )


def backtest_one(symbol: str, name: str, strategy: str) -> dict | None:
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, max(PERIODS))
    if df is None:
        return None
    period_results: dict[int, dict] = {}
    for days in PERIODS:
        r = run_limit_backtest(symbol, name, df, calc_fn,
                               em, sm, tm, days, strategy,
                               entry_type=ENTRY_TYPE)
        if r and r["trades"] >= 1:
            period_results[days] = r
    return dict(symbol=symbol, name=name, strategy=strategy,
                period_results=period_results, today_sig=None)


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_html(all_items: list[dict], show_days: int,
               date_label: str = "本日") -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── 戦略サマリー（全6戦略を常に表示） ─────────────────────────
    ALL_STRATS = ["MACD_S", "A7_S", "RSI2_S", "DON_S", "VOL_S", "MOM_S"]
    strategy_summary: dict[str, dict] = {
        s: dict(trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0, trade_log=[])
        for s in ALL_STRATS
    }
    for item in all_items:
        strat = item["strategy"]
        if strat not in strategy_summary:
            continue
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

    n225_ret = fetch_n225_return(show_days)

    summary_rows = ""
    for strat in ALL_STRATS:
        s   = strategy_summary[strat]
        wr  = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        pf  = s["gp"] / s["gl"] if s["gl"] > 0 else (float("inf") if s["gp"] > 0 else 0)
        cls = "profit" if s["pnl"] >= 0 else "loss"
        enriched   = enrich_backtest_result({"trade_log": s["trade_log"]}, _INITIAL_CASH)
        max_dd_pct = enriched.get("max_drawdown_pct", 0.0)
        max_cl     = enriched.get("max_consecutive_losses", 0)
        sharpe     = enriched.get("sharpe", 0.0)
        hs         = enriched.get("hold_stats", {})
        strat_ret_pct = s["pnl"] / _INITIAL_CASH * 100 if _INITIAL_CASH > 0 else 0
        alpha     = strat_ret_pct + n225_ret
        alpha_cls = "profit" if alpha >= 0 else "loss"
        dd_cls    = "profit" if max_dd_pct < 10 else ("loss" if max_dd_pct > 20 else "")
        hold_break = []
        if hs.get("target_n", 0):
            hold_break.append(f"目標{hs['target_avg']:.1f}({hs['target_n']})")
        if hs.get("stop_n", 0):
            hold_break.append(f"損切{hs['stop_avg']:.1f}({hs['stop_n']})")
        if hs.get("tc_n", 0):
            hold_break.append(f"TC{hs['tc_avg']:.0f}({hs['tc_n']})")
        if hs.get("same_day_n", 0):
            hold_break.append(f"同日({hs['same_day_n']})")
        hold_break_str = " / ".join(hold_break) if hold_break else ""
        # 取引なしの場合は "-" 表示
        if s["trades"] == 0:
            summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower().replace('_s','')}-s">{strat}</span></td>
          <td colspan="10" style="color:#475569;text-align:center">データなし（WATCHLISTを更新してください）</td>
        </tr>"""
        else:
            summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower().replace('_s','')}-s">{strat}</span></td>
          <td>{s['trades']}</td><td>{s['wins']}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf)}</td>
          <td class="{cls}">{s['pnl']:+,.0f}円</td>
          <td class="{dd_cls}">{max_dd_pct:.1f}%</td>
          <td>{max_cl}</td>
          <td>{sharpe:.2f}</td>
          <td class="{alpha_cls}">{alpha:+.1f}%</td>
          <td>{hs.get('avg', 0):.1f}日<br><small class="hold-break">{hold_break_str}</small></td>
        </tr>"""

    # ── シグナル行（スコア降順） ──────────────────────────────────
    signal_items = [(item, calc_recommend_score(item["period_results"]))
                    for item in all_items if item["today_sig"]]
    signal_items.sort(key=lambda x: x[1][0], reverse=True)

    signal_rows = ""
    for item, (score, rank) in signal_items:
        sig      = item["today_sig"]
        strat    = item["strategy"]
        rank_cls = {"★★★": "rank-s", "★★": "rank-a", "★": "rank-b"}.get(rank, "rank-c")
        signal_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower().replace('_s','')}-s">{strat}</span></td>
          <td class="score-cell"><span class="{rank_cls}">{rank}</span><br>{score}点</td>
          <td>{sig['signal_date']}</td>
          <td>{sig['signal_price']:,.0f}</td>
          <td>{sig['current_price']:,.0f}</td>
          <td class="stop">{sig['order_price']:,.0f}</td>
          <td class="limit-entry">{sig.get('limit_short_entry', sig['order_price']):,.0f}</td>
          <td class="profit">{sig['stop_price']:,.0f}</td>
          <td class="loss">{sig['target_price']:,.0f}</td>
        </tr>"""
    if not signal_rows:
        signal_rows = f'<tr><td colspan="10" style="text-align:center;color:#94a3b8">{date_label} ショートシグナルなし</td></tr>'

    # ── 4期間比較 ────────────────────────────────────────────────
    period_headers  = "".join(f"<th colspan='4'>{p}日</th>" for p in PERIODS)
    period_subheads = "<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(PERIODS)

    stock_rows = ""
    for strat in ALL_STRATS:
        items = [i for i in all_items if i["strategy"] == strat]
        items.sort(
            key=lambda x: (x["period_results"].get(show_days) or {}).get("total_pnl", -999999),
            reverse=True,
        )
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
            mark = "🔔" if item["today_sig"] else ""
            stock_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}{mark}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower().replace('_s','')}-s">{strat}</span></td>
          {cells}
          <td>{hold_cell}</td>
        </tr>"""

    # ── 個別トレード ─────────────────────────────────────────────
    trade_sections = ""
    for item in all_items:
        pr   = item["period_results"].get(show_days) or {}
        logs = pr.get("trade_log") or []
        if not logs:
            continue
        trade_rows     = ""
        fill_days_list = []
        for t in logs:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            e_str   = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            x_str   = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
            sig_dt  = t.get("signal_dt")
            s_str   = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else (str(sig_dt) if sig_dt else "-")
            s_p     = t.get("signal_price", "-")
            s_p_str = f"{s_p:,.0f}" if isinstance(s_p, float) else str(s_p)
            dtf     = t.get("days_to_fill", "-")
            fill_days_list.append(dtf) if isinstance(dtf, int) else None
            ol      = t.get("order_limit")
            osl     = t.get("order_stop")
            ol_str  = f"{ol:,.0f}"  if isinstance(ol,  float) else str(ol  or "-")
            osl_str = f"{osl:,.0f}" if isinstance(osl, float) else str(osl or "-")
            trade_rows += f"""
              <tr>
                <td>{s_str}</td><td class="stop">{s_p_str}</td>
                <td>{e_str}</td><td>{x_str}</td>
                <td class="stop">{ol_str}</td>
                <td class="profit">{osl_str}</td>
                <td>{t['entry_p']:,.0f}</td><td>{t['exit_p']:,.0f}</td>
                <td>{t['qty']}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['hold_days']}日</td>
                <td class="stop">{dtf}日</td>
                <td>{t['reason']}</td>
              </tr>"""
        strat     = item["strategy"]
        pnl_total = pr.get("total_pnl", 0)
        pc2       = "profit" if pnl_total >= 0 else "loss"
        if fill_days_list:
            avg_f     = sum(fill_days_list) / len(fill_days_list)
            dist      = {d: fill_days_list.count(d) for d in sorted(set(fill_days_list))}
            dist_str  = " / ".join(f"{d}日:{n}回" for d, n in dist.items())
            fill_stat = f'<p class="fill-stat">約定日数 — 平均:{avg_f:.1f}日 最短:{min(fill_days_list)}日 最長:{max(fill_days_list)}日 | 分布: {dist_str}</p>'
        else:
            fill_stat = ""
        hs_sec    = calc_hold_stats(logs)
        hold_stat = ""
        if hs_sec["count"] > 0:
            hold_break = []
            if hs_sec["target_n"]:
                hold_break.append(f"目標{hs_sec['target_avg']:.1f}日({hs_sec['target_n']})")
            if hs_sec["stop_n"]:
                hold_break.append(f"損切{hs_sec['stop_avg']:.1f}日({hs_sec['stop_n']})")
            if hs_sec["tc_n"]:
                hold_break.append(f"TC{hs_sec['tc_avg']:.0f}日({hs_sec['tc_n']})")
            if hs_sec["same_day_n"]:
                hold_break.append(f"同日({hs_sec['same_day_n']})")
            if hs_sec["held_n"]:
                hold_break.append(f"保有中{hs_sec['held_avg']:.1f}日({hs_sec['held_n']})")
            brk       = " / ".join(hold_break)
            hold_stat = f'<p class="hold-stat">保有日数 — 平均:{hs_sec["avg"]:.1f}日 | 内訳: {brk}</p>'
        trade_sections += f"""
      <div class="trade-section">
        <h3>{item['symbol']} {item['name']}
          <span class="tag tag-{strat.lower().replace('_s','')}-s">{strat}</span>
          <span class="{pc2}">{pnl_total:+,.0f}円</span>
          <small>（{show_days}日）</small>
        </h3>
        {fill_stat}
        {hold_stat}
        <table>
          <thead><tr>
            <th>シグナル日</th><th>シグナル時株価</th>
            <th>エントリー</th><th>エグジット</th>
            <th>逆指値(売)</th><th>損切り(買戻)</th>
            <th>空売り価格</th><th>買戻し価格</th>
            <th>数量</th><th>損益(円)</th><th>損益(%)</th>
            <th>保有日数</th><th>約定日数</th><th>理由</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>監視銘柄 逆指値ショートバックテスト — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym  {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .stop   {{ color:#38bdf8; }}
  .limit-entry {{ color:#fb923c; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-macd-s {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7-s   {{ background:#065f46; color:#a7f3d0; }}
  .tag-rsi2-s {{ background:#7c3aed; color:#ddd6fe; }}
  .tag-don-s  {{ background:#134e4a; color:#99f6e4; }}
  .tag-vol-s  {{ background:#1e3a5f; color:#93c5fd; }}
  .tag-mom-s  {{ background:#4c1d95; color:#e9d5ff; }}
  .signal-badge {{ background:#38bdf8; color:#000; padding:2px 8px; border-radius:4px; font-size:0.8rem; }}
  .trade-section {{ margin-bottom:20px; }}
  .fill-stat  {{ color:#38bdf8; font-size:0.82rem; margin-bottom:6px; }}
  .hold-stat  {{ color:#a5b4fc; font-size:0.82rem; margin-bottom:6px; }}
  .hold-break {{ color:#94a3b8; font-size:0.70rem; font-weight:normal; white-space:nowrap; }}
  .rank-s {{ background:#fbbf24; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-a {{ background:#4ade80; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-b {{ background:#38bdf8; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-c {{ background:#94a3b8; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .score-cell {{ text-align:center; }}
</style>
</head>
<body>
<h1>監視銘柄 逆指値ショートエントリー バックテスト</h1>
<p class="subtitle">
  生成日: {today_str} ／ シグナル確認日: {date_label} ／ 表示期間: {show_days}日 ／
  <span style="color:#fbbf24">モード: <strong>{TRADING_MODE}</strong></span><br>
  エントリー: <strong>逆指値売り</strong>（安値 ≤ 前日終値 で空売り = 下がれば売る）<br>
  コストモデル: スリッページ <strong>{SLIPPAGE_STOP_PCT*100:.2f}%</strong>（逆指値売り-/損切り買戻+）／
  手数料 <strong>片道 {FEE_PCT_ONE_WAY*100:.2f}%</strong>（往復 {FEE_PCT_ONE_WAY*200:.2f}%）／
  ベンチマーク: 日経平均 ({show_days}日) <strong>{n225_ret:+.1f}%</strong>
  <span style="color:#94a3b8;font-size:0.85rem">（ショート: 日経下落時に α が出る）</span><br>
  <span style="color:#f87171;font-size:0.82rem">⚠ 空売りは損失無限大リスクあり。逆日歩・調達コスト・売り建て規制を必ず確認。</span>
</p>

<h2>戦略サマリー（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益合計</th>
    <th>MaxDD%</th><th>連敗</th><th>Sharpe</th><th>α相当<br><small>+日経</small></th>
    <th>平均保有<br><small style="font-weight:normal">日数（内訳：件数）</small></th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>シグナル ({date_label}) <span class="signal-badge">要確認</span></h2>
<p style="color:#94a3b8;font-size:0.82rem;margin-bottom:8px">
  ※ 逆指値売り（青色）= 翌日安値がこの価格以下になれば空売り発動<br>
  ※ 指値下限（橙色, -{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%）= 逆指値→指値発注時の最低売価。寄付ギャップがこれ以上下なら約定、超えたら不約定<br>
  ※ 損切り（緑色）= 買い戻し上限価格 ／ 目標（赤色）= 買い戻し目標価格
</p>
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>スコア</th><th>シグナル日</th><th>シグナル時株価</th>
    <th>現在値</th><th>逆指値(売)<br><small>(トリガー)</small></th>
    <th>指値下限<br><small>(-{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%)</small></th>
    <th>損切り<br><small>(買戻上限)</small></th><th>目標<br><small>(買戻目標)</small></th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
</table>

<h2>銘柄別バックテスト（4期間比較）</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">銘柄</th><th rowspan="2">戦略</th>
      {period_headers}
      <th rowspan="2">平均<br>保有<br><small>({show_days}日)</small></th>
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
    parser = argparse.ArgumentParser(description="監視銘柄 逆指値ショートバックテスト")
    parser.add_argument("--days",        type=int,  default=365)
    parser.add_argument("--date",        type=str,  default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int,  default=_DEFAULT_WORKERS)
    parser.add_argument("--aggressive",  action="store_true",
                        help="aggressive モード (目標 1.5R, 回転率優先)")
    args = parser.parse_args()

    # --aggressive フラグ対応
    if args.aggressive:
        _os.environ["TRADING_MODE"] = "aggressive"
        global STRATEGY_PARAMS, TRADING_MODE
        STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
        TRADING_MODE = "aggressive"

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    print(f"逆指値ショートバックテスト開始 ({len(WATCHLIST)}銘柄) シグナル確認日: {date_label}...", flush=True)
    print("⚠ 空売りは損失無限大リスクがあります。逆日歩・規制を確認してください。", flush=True)

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

    print(f"  シグナル確認中 ({date_label})...", flush=True)
    for item in all_items:
        item["today_sig"] = check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 85)
    print(f"  監視銘柄 逆指値ショートバックテスト  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 85)

    signals_today = [(i, calc_recommend_score(i["period_results"]))
                     for i in all_items if i["today_sig"]]
    signals_today.sort(key=lambda x: x[1][0], reverse=True)
    print(f"\n【ショートシグナル ({date_label})】 {len(signals_today)}件")
    if signals_today:
        print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<8} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値(売)':>10} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 115)
        for item, (score, rank) in signals_today:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12} {item['name']:<20} {item['strategy']:<8}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>10,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    show_days = args.days
    print(f"\n【銘柄別バックテスト ({show_days}日)】")
    print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<8} {'取引':>4} {'勝率':>6} {'PF':>6} {'損益':>10}")
    print("  " + "-" * 72)
    for strat in ["MACD_S", "A7_S", "RSI2_S", "DON_S", "VOL_S", "MOM_S"]:
        for item in [i for i in all_items if i["strategy"] == strat]:
            r = item["period_results"].get(show_days)
            if not r:
                print(f"  {item['symbol']:<12} {item['name']:<20} {strat:<8} データなし")
                continue
            print(f"  {item['symbol']:<12} {item['name']:<20} {strat:<8}"
                  f" {r['trades']:>4} {r['win_rate']:>5.1f}% {_pf_str(r['pf']):>6}"
                  f" {r['total_pnl']:>+10,.0f}円")

    mode_suffix = "_aggressive" if TRADING_MODE == "aggressive" else ""
    date_suffix = args.date if args.date else today
    out_path    = Path(f"watchlist_short{mode_suffix}_{date_suffix}.html")
    out_path.write_text(build_html(all_items, show_days, date_label), encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
