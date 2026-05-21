"""
check_signals_stop.py  ―  監視銘柄 逆指値エントリー バックテスト（パターンB）
=================================================================
check_signals_limit.py の逆指値版。
エントリー条件: 高値 ≥ 前日終値（上がれば買う）
監視銘柄: scan_entry_compare.py のパターンB（逆指値・終値以上）スキャン上位銘柄

【使い方】
  python check_signals_stop.py               # 全期間(365日) HTMLレポート
  python check_signals_stop.py --days 90     # 直近90日
  python check_signals_stop.py --date 2026-03-28  # 任意日シグナル確認
  python check_signals_stop.py --no-browser
  python check_signals_stop.py --signal-only
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf
import pandas as pd

from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    fetch_n225_return,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, LIMIT_ENTRY_MARGIN_PCT,
    MAX_HOLD, ENTRY_EXPIRE,
    INITIAL_CASH as _INITIAL_CASH,
    WORKERS as _DEFAULT_WORKERS,
    compute_period_result,
)
from risk_metrics import enrich_backtest_result, calc_hold_stats

JST     = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]


def _fetch_live_price(symbol: str, fallback: float) -> float:
    """最新の日足終値をキャッシュを使わず直接取得。失敗時はフォールバック。"""
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1d",
                                       auto_adjust=False, actions=False)
        if df is not None and not df.empty:
            p = float(df["Close"].iloc[-1])
            return p if p > 0 else fallback
    except Exception:
        pass
    return fallback


WATCHLIST: list[tuple[str, str, str]] = [
    # ── MACD: Walk-forward 選定 (2026-05-20) ──
    ("5706.T", "三井金属",                           "MACD"),
    ("6269.T", "三井海洋開発",                       "MACD"),
    ("6387.T", "サムコ",                             "MACD"),
    ("6101.T", "ツガミ",                             "MACD"),
    ("6310.T", "井関農機",                           "MACD"),
    ("1893.T", "五洋建設",                           "MACD"),
    ("6376.T", "日機装",                             "MACD"),
    ("2264.T", "森永乳業",                           "MACD"),
    ("5715.T", "古河機械金属",                       "MACD"),
    ("4205.T", "日本ゼオン",                         "MACD"),
    # ── A7: Walk-forward 選定 (2026-05-20) ──
    ("6101.T", "ツガミ",                             "A7"),
    ("5334.T", "日本特殊陶業",                       "A7"),
    ("6103.T", "オークマ",                           "A7"),
    ("1814.T", "大末建設",                           "A7"),
    ("4506.T", "住友ファーマ",                       "A7"),
    ("5803.T", "フジクラ",                           "A7"),
    ("6963.T", "ローム",                             "A7"),
    ("8227.T", "しまむら",                           "A7"),
    ("8002.T", "丸紅",                               "A7"),
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),
    # ── RSI2: Walk-forward 選定 (2026-05-20) ──
    ("5801.T", "古河電気工業",                       "RSI2"),
    ("8551.T", "北日本銀行",                         "RSI2"),
    ("6101.T", "ツガミ",                             "RSI2"),
    ("9869.T", "加藤産業",                           "RSI2"),
    ("7011.T", "三菱重工業",                         "RSI2"),
    ("9948.T", "アークス",                           "RSI2"),
    ("8344.T", "山形銀行",                           "RSI2"),
    ("9069.T", "センコーグループホールディングス",   "RSI2"),
    ("3036.T", "アルコニックス",                     "RSI2"),
    ("8370.T", "紀陽銀行",                           "RSI2"),
]

# ── 逆指値版パラメータ (プリセット切替) ───────────────────────
# TRADING_MODE 環境変数 or --aggressive CLI で aggressive を選択
# デフォルトは conservative (現行踏襲)
STRATEGY_PARAMS_CONSERVATIVE = {
    "MACD": (calc_macd, 0.0, 1.5, 3.0),
    "A7":   (calc_a7,   0.0, 1.5, 3.0),
    "RSI2": (calc_rsi2, 0.0, 2.0, 4.0),   # 指値版は0.5だったがstopは0.0に統一
}
# aggressive: sm=1.5/tm=2.0 (run_signals_prime.py / scan_walkforward と統一)
STRATEGY_PARAMS_AGGRESSIVE = {
    "MACD": (calc_macd, 0.0, 1.5, 2.0),   # 目標 +6% / 損切 -4.5% (1.33R)
    "A7":   (calc_a7,   0.0, 1.5, 2.0),
    "RSI2": (calc_rsi2, 0.0, 1.5, 2.0),
}

import os as _os
# デフォルトは conservative (標準)。--aggressive で積極利確。
TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()
if TRADING_MODE == "aggressive":
    STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
else:
    STRATEGY_PARAMS = STRATEGY_PARAMS_CONSERVATIVE
    TRADING_MODE = "conservative"   # 正規化

ENTRY_TYPE = "stop"   # 逆指値（高値 ≥ 注文価格 で約定）


def calc_recommend_score(period_results: dict) -> tuple[int, str]:
    """
    バックテスト成績からおすすめスコア(0-100)とランクを計算。
      勝率     : 最大40点
      PF       : 最大30点（PF=10でキャップ、∞は10扱い）
      期間安定性: 最大20点（プラス期間数 / 有効期間数）
      取引回数  : 最大10点（20取引で満点）
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
    """target_date の前営業日にシグナルが出ているか確認。"""
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, 365)
    if df is None or len(df) < 5:
        return None
    try:
        df = calc_fn(df)
    except Exception:
        return None

    if target_date is None:
        next_idx = -1
        prev_idx = -1   # 最新足のみ判定（連続シグナルでも当日分だけ表示）
    else:
        ts = pd.Timestamp(target_date)
        cands = df.index[df.index <= ts]
        if len(cands) < 1:
            return None
        prev_idx = df.index.get_loc(cands[-1])  # 指定日そのものを判定
        next_idx = prev_idx

    prev      = df.iloc[prev_idx]
    next_row  = df.iloc[next_idx]
    entry_sig = bool(prev.get("entry_sig", False))
    atr_v     = float(prev.get("atr", 0))
    if not entry_sig or atr_v <= 0:
        return None

    close_prev = float(prev["close"])
    current_p  = float(next_row["close"])
    if target_date is None:
        current_p = _fetch_live_price(symbol, current_p)

    # 逆指値: 終値 + ATR×em（emが0.0なら終値ちょうど）
    order_p     = close_prev + atr_v * em
    sl          = order_p - atr_v * sm   # 損切り
    tp          = order_p + atr_v * tm   # 目標
    # 逆指値→指値注文の指値上限 (kabu 発注時 AfterHitPrice 用)
    limit_entry = order_p * (1.0 + LIMIT_ENTRY_MARGIN_PCT)

    sig_dt   = df.index[prev_idx]
    sig_date = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else str(sig_dt)

    return dict(
        order_price=round(order_p, 0),         # 逆指値トリガー価格
        limit_entry_price=round(limit_entry, 0),  # 逆指値→指値 の指値上限 (+1%)
        stop_price=round(sl, 0),
        target_price=round(tp, 0),
        current_price=current_p,
        signal_date=sig_date,
        signal_price=round(close_prev, 0),
    )


def backtest_one(symbol: str, name: str, strategy: str) -> dict | None:
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, max(PERIODS))
    if df is None:
        return None

    # 365日を1回だけ実行し、trade_log を期間別にスライスして統計を再計算。
    # 期間ごとに独立バックテストすると「開始時のポジション状態が違う」ため
    # 同じ取引ログに見えない損益が混入する問題を回避する。
    full_r = run_limit_backtest(symbol, name, df, calc_fn,
                                em, sm, tm, max(PERIODS), strategy,
                                entry_type=ENTRY_TYPE)
    if not full_r:
        return None

    today  = datetime.now(JST).date()
    period_results: dict[int, dict] = {}
    for days in PERIODS:
        cutoff = today - timedelta(days=days)
        sub    = [t for t in full_r["trade_log"]
                  if t["signal_dt"].date() >= cutoff]
        if not sub:
            continue
        filled = len(sub)
        wins   = sum(1 for t in sub if t["pnl"] > 0)
        losses = sum(1 for t in sub if t["pnl"] <= 0)
        gp     = sum(t["pnl"] for t in sub if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        period_results[days] = dict(
            symbol=symbol, name=name, strategy=strategy,
            signals=full_r["signals"], filled=filled,
            trades=filled, wins=wins, losses=losses,
            win_rate=wins / filled * 100,
            pf=pf, total_pnl=sum(t["pnl"] for t in sub),
            total_fee=sum(t.get("fee", 0) for t in sub),
            slippage_pct=full_r["slippage_pct"],
            fee_pct_one_way=full_r["fee_pct_one_way"],
            avg_hold=sum(t["hold_days"] for t in sub) / filled,
            fill_rate=full_r["fill_rate"],
            trade_log=sub,
        )

    return dict(symbol=symbol, name=name, strategy=strategy,
                period_results=period_results, today_sig=None)


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_html(all_items: list[dict], show_days: int,
               date_label: str = "本日", run_cmd: str = "") -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # サマリー (戦略別に trade_log を結合してリスク指標計算)
    strategy_summary: dict[str, dict] = {}
    for item in all_items:
        strat = item["strategy"]
        if strat not in strategy_summary:
            strategy_summary[strat] = dict(
                trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0, trade_log=[])
        pr = compute_period_result(item, show_days)
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

    # ベンチマーク (日経平均) リターン
    n225_ret = fetch_n225_return(show_days)

    summary_rows = ""
    for strat, s in strategy_summary.items():
        wr  = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        pf  = s["gp"] / s["gl"] if s["gl"] > 0 else (float("inf") if s["gp"] > 0 else 0)
        cls = "profit" if s["pnl"] >= 0 else "loss"
        # リスク指標
        enriched = enrich_backtest_result({"trade_log": s["trade_log"]}, _INITIAL_CASH)
        max_dd_pct = enriched.get("max_drawdown_pct", 0.0)
        max_cl     = enriched.get("max_consecutive_losses", 0)
        sharpe     = enriched.get("sharpe", 0.0)
        hs         = enriched.get("hold_stats", {})
        # ベンチマーク相対 α (戦略リターン - 日経リターン, INITIAL_CASH 基準)
        strat_ret_pct = s["pnl"] / _INITIAL_CASH * 100 if _INITIAL_CASH > 0 else 0
        alpha         = strat_ret_pct - n225_ret
        alpha_cls     = "profit" if alpha >= 0 else "loss"
        dd_cls        = "profit" if max_dd_pct < 10 else ("loss" if max_dd_pct > 20 else "")
        # 平均保有日数の表示 (メイン + 理由別内訳)
        hold_break    = []
        if hs.get("target_n", 0):
            hold_break.append(f"目標{hs['target_avg']:.1f}({hs['target_n']})")
        if hs.get("stop_n", 0):
            hold_break.append(f"損切{hs['stop_avg']:.1f}({hs['stop_n']})")
        if hs.get("tc_n", 0):
            hold_break.append(f"TC{hs['tc_avg']:.0f}({hs['tc_n']})")
        if hs.get("same_day_n", 0):
            hold_break.append(f"同日({hs['same_day_n']})")
        hold_break_str = " / ".join(hold_break) if hold_break else ""
        summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td>{s['trades']}</td><td>{s['wins']}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf)}</td>
          <td class="{cls}">{s['pnl']:+,.0f}円</td>
          <td class="{dd_cls}">{max_dd_pct:.1f}%</td>
          <td>{max_cl}</td>
          <td>{sharpe:.2f}</td>
          <td class="{alpha_cls}">{alpha:+.1f}%</td>
          <td>{hs.get('avg', 0):.1f}日<br><small class="hold-break">{hold_break_str}</small></td>
        </tr>"""

    # シグナル行（当日新規のみ。ルックバック継続/保有中は除外）
    signal_items = [(item, calc_recommend_score(item["period_results"]))
                    for item in all_items
                    if item["today_sig"]
                    and not item["today_sig"].get("_pending_lookback")
                    and not item["today_sig"].get("_filled_holding")]
    signal_items.sort(key=lambda x: x[1][0], reverse=True)

    signal_rows = ""
    for item, (score, rank) in signal_items:
        sig   = item["today_sig"]
        strat = item["strategy"]
        rank_cls = {"★★★": "rank-s", "★★": "rank-a", "★": "rank-b"}.get(rank, "rank-c")
        # 最大決済日: シグナル日 + 約定期限(ENTRY_EXPIRE) + 最大保有(MAX_HOLD) 営業日
        _sig_dt = pd.to_datetime(sig['signal_date'])
        _max_exit = pd.bdate_range(start=_sig_dt, periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
        max_exit_str = _max_exit.strftime("%Y-%m-%d")
        signal_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td class="score-cell"><span class="{rank_cls}">{rank}</span><br>{score}点</td>
          <td>{sig['signal_date']}</td>
          <td>{sig['signal_price']:,.0f}</td>
          <td>{sig['current_price']:,.0f}</td>
          <td class="stop">{sig['order_price']:,.0f}</td>
          <td class="limit-entry">{sig.get('limit_entry_price', sig['order_price']):,.0f}</td>
          <td class="loss">{sig['stop_price']:,.0f}</td>
          <td class="profit">{sig['target_price']:,.0f}</td>
          <td style="color:#94a3b8">{MAX_HOLD}日</td>
          <td style="color:#f59e0b;font-size:12px">{max_exit_str}</td>
        </tr>"""
    if not signal_rows:
        signal_rows = f'<tr><td colspan="12" style="text-align:center;color:#94a3b8">{date_label} シグナルなし</td></tr>'

    # 4期間比較
    period_headers  = "".join(f"<th colspan='4'>{p}日</th>" for p in PERIODS)
    period_subheads = "<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(PERIODS)

    stock_rows = ""
    for strat in ["MACD", "A7", "RSI2"]:
        items = [i for i in all_items if i["strategy"] == strat]
        items.sort(key=lambda x: (compute_period_result(x, show_days)).get("total_pnl", -999999), reverse=True)
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
            # show_days 期間の平均保有日数
            pr_show   = compute_period_result(item, show_days)
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

    # 個別トレード
    trade_sections = ""
    for item in all_items:
        pr   = compute_period_result(item, show_days)
        logs = pr.get("trade_log") or []
        if not logs and not item.get("today_sig"):
            continue
        trade_rows     = ""
        fill_days_list = []
        for t in logs:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            e_str   = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            x_str   = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
            sig_dt  = t.get("signal_dt")
            s_str   = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else (str(sig_dt) if sig_dt else "-")
            # 最大決済日: シグナル日 + ENTRY_EXPIRE + MAX_HOLD 営業日
            if sig_dt is not None:
                _max_exit = pd.bdate_range(start=pd.to_datetime(sig_dt), periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
                max_exit_str = _max_exit.strftime("%Y-%m-%d")
            else:
                max_exit_str = "-"
            s_p     = t.get("signal_price", "-")
            s_p_str = f"{s_p:,.0f}" if isinstance(s_p, float) else str(s_p)
            dtf     = t.get("days_to_fill", "-")
            fill_days_list.append(dtf) if isinstance(dtf, int) else None
            ol  = t.get("order_limit")    # 逆指値の注文価格
            osl = t.get("order_stop")     # 損切り
            otg = t.get("order_target")   # 目標価格
            ole = ol * (1.0 + LIMIT_ENTRY_MARGIN_PCT) if isinstance(ol, float) else None
            ol_str  = f"{ol:,.0f}"  if isinstance(ol,  float) else str(ol  or "-")
            osl_str = f"{osl:,.0f}" if isinstance(osl, float) else str(osl or "-")
            otg_str = f"{otg:,.0f}" if isinstance(otg, float) else str(otg or "-")
            ole_str = f"{ole:,.0f}" if isinstance(ole, float) else "-"
            trade_rows += f"""
              <tr>
                <td>{s_str}</td><td class="stop">{s_p_str}</td>
                <td>{e_str}</td><td>{x_str}</td>
                <td class="stop">{ol_str}</td>
                <td class="limit-entry">{ole_str}</td>
                <td class="loss">{osl_str}</td>
                <td class="profit">{otg_str}</td>
                <td>{t['entry_p']:,.0f}</td><td>{t['exit_p']:,.0f}</td>
                <td>{t['qty']}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['hold_days']}日</td>
                <td class="stop">{dtf}日</td>
                <td style="color:#f59e0b;font-size:12px">{max_exit_str}</td>
                <td>{t['reason']}</td>
              </tr>"""
        # 未約定/保有中シグナルを取引詳細の末尾に追記
        if item.get("today_sig"):
            sig = item["today_sig"]
            _sig_dt_p   = pd.to_datetime(sig["signal_date"])
            _max_exit_p = pd.bdate_range(start=_sig_dt_p, periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
            max_exit_pending = _max_exit_p.strftime("%Y-%m-%d")
            ol_p  = sig["order_price"]
            ole_p = sig.get("limit_entry_price", round(ol_p * (1 + LIMIT_ENTRY_MARGIN_PCT), 0))
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
                <td>{sig['signal_date']}</td><td class="stop">{sig['signal_price']:,.0f}</td>
                <td>-</td><td>-</td>
                <td class="stop">{ol_p:,.0f}</td>
                <td class="limit-entry">{ole_p:,.0f}</td>
                <td class="loss">{sig['stop_price']:,.0f}</td>
                <td class="profit">{sig['target_price']:,.0f}</td>
                <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                <td style="color:#f59e0b;font-size:12px">{max_exit_pending}</td>
                {reason_td}
              </tr>"""
        strat     = item["strategy"]
        pnl_total = pr.get("total_pnl", 0)
        pc2       = "profit" if pnl_total >= 0 else "loss"
        if fill_days_list:
            avg_f    = sum(fill_days_list) / len(fill_days_list)
            dist     = {d: fill_days_list.count(d) for d in sorted(set(fill_days_list))}
            dist_str = " / ".join(f"{d}日:{n}回" for d, n in dist.items())
            fill_stat = f'<p class="fill-stat">約定日数 — 平均:{avg_f:.1f}日 最短:{min(fill_days_list)}日 最長:{max(fill_days_list)}日 | 分布: {dist_str}</p>'
        else:
            fill_stat = ""
        # 保有日数統計 (理由別内訳付き)
        hs_sec = calc_hold_stats(logs)
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
            brk = " / ".join(hold_break)
            hold_stat = f'<p class="hold-stat">保有日数 — 平均:{hs_sec["avg"]:.1f}日 | 内訳: {brk}</p>'
        trade_sections += f"""
      <div class="trade-section">
        <h3>{item['symbol']} {item['name']}
          <span class="tag tag-{strat.lower()}">{strat}</span>
          <span class="{pc2}">{pnl_total:+,.0f}円</span>
          <small>（{show_days}日）</small>
        </h3>
        {fill_stat}
        {hold_stat}
        <table>
          <thead><tr>
            <th>シグナル日</th><th>シグナル時株価</th>
            <th>エントリー</th><th>エグジット</th>
            <th>逆指値</th><th>指値上限<br><small>(+{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%)</small></th><th>損切り</th><th>目標価格</th>
            <th>エントリー価格</th><th>エグジット価格</th>
            <th>数量</th><th>損益(円)</th><th>損益(%)</th>
            <th>保有日数</th><th>約定日数</th><th>最大決済日</th><th>理由</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>監視銘柄 逆指値バックテスト — {today_str}</title>
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
  .tag-macd {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7   {{ background:#065f46; color:#a7f3d0; }}
  .tag-rsi2 {{ background:#7c3aed; color:#ddd6fe; }}
  .signal-badge {{ background:#38bdf8; color:#000; padding:2px 8px; border-radius:4px; font-size:0.8rem; }}
  .trade-section {{ margin-bottom:20px; }}
  .fill-stat {{ color:#38bdf8; font-size:0.82rem; margin-bottom:6px; }}
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
<h1>監視銘柄 逆指値エントリー バックテスト</h1>
<p class="subtitle">
  生成日: {today_str} ／ シグナル確認日: {date_label} ／ 表示期間: {show_days}日 ／
  <span style="color:#fbbf24">モード: <strong>{TRADING_MODE}</strong></span><br>
  エントリー: <strong>逆指値</strong>（高値 ≥ 前日終値 で約定 ＝ 上がれば買う）<br>
  コストモデル: スリッページ <strong>{SLIPPAGE_STOP_PCT*100:.2f}%</strong>（逆指値買い+/損切り売り-）／
  手数料 <strong>片道 {FEE_PCT_ONE_WAY*100:.2f}%</strong>（往復 {FEE_PCT_ONE_WAY*200:.2f}%）／
  ベンチマーク: 日経平均 ({show_days}日) <strong>{n225_ret:+.1f}%</strong>
  {f'<br>▶ 実行: <code style="background:#0f172a;padding:2px 8px;border-radius:4px;color:#38bdf8;font-size:0.88rem">{run_cmd}</code>' if run_cmd else ""}
</p>

<h2>戦略サマリー（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益合計</th>
    <th>MaxDD%</th><th>連敗</th><th>Sharpe</th><th>α vs 日経</th>
    <th>平均保有<br><small style="font-weight:normal">日数（内訳：件数）</small></th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>シグナル ({date_label}) <span class="signal-badge">要確認</span></h2>
<p style="color:#94a3b8;font-size:0.82rem;margin-bottom:8px">
  ※ 逆指値注文（青色）= 翌日高値がこの価格以上になれば発動<br>
  ※ 指値上限（橙色, +{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%）= 逆指値→指値発注時の指値。寄付ギャップがこれ以下なら約定、超えたら不約定
</p>
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>スコア</th><th>シグナル日</th><th>シグナル時株価</th>
    <th>現在値</th><th>逆指値<br><small>(トリガー)</small></th><th>指値上限<br><small>(+{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%)</small></th><th>損切り</th><th>目標</th><th>最大保有日</th><th>最大決済日<br><small>(約定期限+保有)</small></th>
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
    parser = argparse.ArgumentParser(description="監視銘柄 逆指値バックテスト")
    parser.add_argument("--days",        type=int,  default=365)
    parser.add_argument("--date",        type=str,  default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int,  default=_DEFAULT_WORKERS)
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
    print(f"逆指値バックテスト開始 ({len(WATCHLIST)}銘柄) シグナル確認日: {date_label}...", flush=True)

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
    print(f"  監視銘柄 逆指値バックテスト  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 85)

    signals_today = [(i, calc_recommend_score(i["period_results"]))
                     for i in all_items if i["today_sig"]]
    signals_today.sort(key=lambda x: x[1][0], reverse=True)
    print(f"\n【シグナル ({date_label})】 {len(signals_today)}件")
    if signals_today:
        print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<6} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値':>8} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 108)
        for item, (score, rank) in signals_today:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12} {item['name']:<20} {item['strategy']:<6}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    show_days = args.days
    print(f"\n【銘柄別バックテスト ({show_days}日)】")
    print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<6} {'取引':>4} {'勝率':>6} {'PF':>6} {'損益':>10}")
    print("  " + "-" * 70)
    for strat in ["MACD", "A7", "RSI2"]:
        for item in [i for i in all_items if i["strategy"] == strat]:
            r = compute_period_result(item, show_days)
            if not r:
                print(f"  {item['symbol']:<12} {item['name']:<20} {strat:<6} データなし")
                continue
            print(f"  {item['symbol']:<12} {item['name']:<20} {strat:<6}"
                  f" {r['trades']:>4} {r['win_rate']:>5.1f}% {_pf_str(r['pf']):>6}"
                  f" {r['total_pnl']:>+10,.0f}円")

    date_suffix = args.date if args.date else today
    out_path    = Path(f"watchlist_stop_{date_suffix}.html")
    out_path.write_text(build_html(all_items, show_days, date_label), encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
