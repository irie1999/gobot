"""
check_signals_limit.py  ―  監視銘柄 指値エントリー バックテスト
=================================================================
選定した24銘柄（MACD/A7/RSI2）のバックテストと本日シグナルを確認。

【使い方】
  python check_signals_limit.py               # 全期間(365日) HTMLレポート
  python check_signals_limit.py --days 90     # 直近90日
  python check_signals_limit.py --no-browser  # ブラウザを開かない
  python check_signals_limit.py --signal-only # 本日シグナル銘柄のみ表示
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)

JST     = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]

# ── 監視銘柄リスト ─────────────────────────────────────────────────
# (コード, 銘柄名, 戦略)
WATCHLIST: list[tuple[str, str, str]] = [
    # MACD 戦略
    ("4368.T", "扶桑化学工業",       "MACD"),
    ("4186.T", "東京応化工業",       "MACD"),
    ("5714.T", "DOWAホールディングス", "MACD"),
    ("8341.T", "七十七銀行",         "MACD"),
    ("3964.T", "オークネット",       "MACD"),
    ("4118.T", "カネカ",             "MACD"),
    ("6323.T", "ローツェ",           "MACD"),
    ("5713.T", "住友金属鉱山",       "MACD"),
    ("5805.T", "SWCC",               "MACD"),
    ("3104.T", "富士紡ホールディングス", "MACD"),
    # A7 戦略
    ("1861.T", "熊谷組",             "A7"),
    ("6331.T", "三菱化工機",         "A7"),
    ("6954.T", "ファナック",         "A7"),
    ("8381.T", "山陰合同銀行",       "A7"),
    ("8031.T", "三井物産",           "A7"),
    ("4554.T", "富士製薬工業",       "A7"),
    ("8141.T", "新光商事",           "A7"),
    ("1605.T", "INPEX",              "A7"),
    # RSI2 戦略
    ("4506.T", "住友ファーマ",       "RSI2"),
    ("6644.T", "大崎電気工業",       "RSI2"),
    ("9507.T", "四国電力",           "RSI2"),
    ("9003.T", "相鉄ホールディングス", "RSI2"),
    ("9678.T", "カナモト",           "RSI2"),
    ("5981.T", "東京製綱",           "RSI2"),
]

STRATEGY_PARAMS = {
    "MACD": (calc_macd, 0.0, 1.5, 3.0),
    "A7":   (calc_a7,   0.0, 1.5, 3.0),
    "RSI2": (calc_rsi2, 0.5, 2.0, 4.0),
}


# ── 本日シグナル確認 ──────────────────────────────────────────────
def check_today_signal(symbol: str, strategy: str) -> dict | None:
    """最新データで本日（最終足）のシグナルを確認。"""
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, 365)
    if df is None or len(df) < 5:
        return None
    try:
        df = calc_fn(df)
    except Exception:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    entry_sig = bool(prev.get("entry_sig", False))
    close_p   = float(last.get("close", 0))
    atr_v     = float(prev.get("atr", 0))

    if not entry_sig or atr_v <= 0:
        return None

    close_prev = float(prev["close"])
    lp = close_prev - atr_v * STRATEGY_PARAMS[strategy][1]
    sp = lp - atr_v * STRATEGY_PARAMS[strategy][2]
    tp = lp + atr_v * STRATEGY_PARAMS[strategy][3]

    return dict(
        limit_price=round(lp, 0),
        stop_price=round(sp, 0),
        target_price=round(tp, 0),
        current_price=close_p,
    )


# ── 1銘柄×全期間バックテスト ──────────────────────────────────────
def backtest_one(symbol: str, name: str, strategy: str) -> dict | None:
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, max(PERIODS))
    if df is None:
        return None

    period_results: dict[int, dict] = {}
    for days in PERIODS:
        r = run_limit_backtest(symbol, name, df, calc_fn, em, sm, tm, days, strategy)
        if r and r["trades"] >= 1:
            period_results[days] = r

    # 本日シグナル
    today_sig = check_today_signal(symbol, strategy)

    return dict(
        symbol=symbol,
        name=name,
        strategy=strategy,
        period_results=period_results,
        today_sig=today_sig,
    )


# ── HTML生成 ─────────────────────────────────────────────────────
def _pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "∞"
    return f"{pf:.2f}"


def build_html(all_items: list[dict], show_days: int) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── サマリー（戦略別）
    strategy_summary: dict[str, dict] = {}
    for item in all_items:
        strat = item["strategy"]
        if strat not in strategy_summary:
            strategy_summary[strat] = dict(trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0)
        pr = item["period_results"].get(show_days) or {}
        if pr:
            strategy_summary[strat]["trades"] += pr["trades"]
            strategy_summary[strat]["wins"]   += pr["wins"]
            strategy_summary[strat]["pnl"]    += pr["total_pnl"]
            for t in (item["period_results"].get(show_days, {}).get("trade_log") or []):
                if t["pnl"] > 0:
                    strategy_summary[strat]["gp"] += t["pnl"]
                else:
                    strategy_summary[strat]["gl"] += abs(t["pnl"])

    summary_rows = ""
    for strat, s in strategy_summary.items():
        wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        pf = s["gp"] / s["gl"] if s["gl"] > 0 else (float("inf") if s["gp"] > 0 else 0)
        cls = "profit" if s["pnl"] >= 0 else "loss"
        summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td>{s['trades']}</td><td>{s['wins']}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf)}</td>
          <td class="{cls}">{s['pnl']:+,.0f}円</td>
        </tr>"""

    # ── 本日シグナル
    signal_rows = ""
    for item in sorted(all_items, key=lambda x: x["strategy"]):
        sig = item["today_sig"]
        if not sig:
            continue
        strat = item["strategy"]
        signal_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td>{sig['current_price']:,.0f}</td>
          <td class="limit">{sig['limit_price']:,.0f}</td>
          <td class="loss">{sig['stop_price']:,.0f}</td>
          <td class="profit">{sig['target_price']:,.0f}</td>
        </tr>"""
    if not signal_rows:
        signal_rows = '<tr><td colspan="6" style="text-align:center;color:#94a3b8">本日シグナルなし</td></tr>'

    # ── 銘柄別バックテスト結果（全期間比較）
    period_headers = "".join(f"<th colspan='4'>{p}日</th>" for p in PERIODS)
    period_subheads = ("<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(PERIODS))

    stock_rows = ""
    for strat in ["MACD", "A7", "RSI2"]:
        items = [i for i in all_items if i["strategy"] == strat]
        # show_days の損益でソート
        items.sort(key=lambda x: (x["period_results"].get(show_days) or {}).get("total_pnl", -999999), reverse=True)
        for item in items:
            pr = item["period_results"]
            cells = ""
            for p in PERIODS:
                r = pr.get(p)
                if not r:
                    cells += "<td>-</td><td>-</td><td>-</td><td>-</td>"
                else:
                    pnl_cls = "profit" if r["total_pnl"] >= 0 else "loss"
                    cells += (
                        f"<td>{r['trades']}</td>"
                        f"<td>{r['win_rate']:.0f}%</td>"
                        f"<td>{_pf_str(r['pf'])}</td>"
                        f"<td class='{pnl_cls}'>{r['total_pnl']:+,.0f}</td>"
                    )
            sig_mark = "🔔" if item["today_sig"] else ""
            stock_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}{sig_mark}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          {cells}
        </tr>"""

    # ── 個別トレード一覧
    trade_sections = ""
    for item in all_items:
        pr = item["period_results"].get(show_days) or {}
        logs = pr.get("trade_log") or []
        if not logs:
            continue
        trade_rows = ""
        fill_days_list = []
        for t in logs:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            e_str = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            x_str = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
            dtf = t.get("days_to_fill", "-")
            fill_days_list.append(dtf) if isinstance(dtf, int) else None
            trade_rows += f"""
              <tr>
                <td>{e_str}</td><td>{x_str}</td>
                <td>{t['entry_p']:,.0f}</td><td>{t['exit_p']:,.0f}</td>
                <td>{t['qty']}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['hold_days']}日</td>
                <td class="limit">{dtf}日</td>
                <td>{t['reason']}</td>
              </tr>"""
        strat = item["strategy"]
        pnl_total = pr.get("total_pnl", 0)
        pnl_cls2 = "profit" if pnl_total >= 0 else "loss"
        # 約定日数統計
        if fill_days_list:
            avg_fill = sum(fill_days_list) / len(fill_days_list)
            min_fill = min(fill_days_list)
            max_fill = max(fill_days_list)
            dist = {d: fill_days_list.count(d) for d in sorted(set(fill_days_list))}
            dist_str = " / ".join(f"{d}日:{n}回" for d, n in dist.items())
            fill_stat = f'<p class="fill-stat">約定日数 — 平均:{avg_fill:.1f}日 最短:{min_fill}日 最長:{max_fill}日 &nbsp;|&nbsp; 分布: {dist_str}</p>'
        else:
            fill_stat = ""
        trade_sections += f"""
      <div class="trade-section">
        <h3>{item['symbol']} {item['name']}
          <span class="tag tag-{strat.lower()}">{strat}</span>
          <span class="{pnl_cls2}">{pnl_total:+,.0f}円</span>
          <small>（{show_days}日）</small>
        </h3>
        {fill_stat}
        <table>
          <thead><tr>
            <th>エントリー</th><th>エグジット</th>
            <th>エントリー価格</th><th>エグジット価格</th>
            <th>数量</th><th>損益(円)</th><th>損益(%)</th>
            <th>保有日数</th><th>約定日数</th><th>理由</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>監視銘柄 指値バックテスト — {today_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .limit  {{ color:#facc15; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-macd {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7   {{ background:#065f46; color:#a7f3d0; }}
  .tag-rsi2 {{ background:#7c3aed; color:#ddd6fe; }}
  .signal-badge {{ background:#f59e0b; color:#000; padding:2px 8px; border-radius:4px; font-size:0.8rem; }}
  .trade-section {{ margin-bottom:20px; }}
  .card {{ background:#1e293b; border-radius:8px; padding:16px; margin-bottom:16px; }}
  .fill-stat {{ color:#facc15; font-size:0.82rem; margin-bottom:6px; }}
</style>
</head>
<body>
<h1>監視銘柄 指値エントリー バックテスト</h1>
<p class="subtitle">生成日: {today_str} ／ 表示期間: {show_days}日 ／ 銘柄数: {len(all_items)}銘柄（MACD×10 / A7×8 / RSI2×6）</p>

<h2>戦略サマリー（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>本日シグナル <span class="signal-badge">要確認</span></h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>現在値</th><th>指値（エントリー）</th><th>損切り</th><th>目標</th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
</table>

<h2>銘柄別バックテスト（4期間比較）</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">銘柄</th>
      <th rowspan="2">戦略</th>
      {period_headers}
    </tr>
    <tr>{period_subheads}</tr>
  </thead>
  <tbody>{stock_rows}</tbody>
</table>

<h2>個別トレード一覧（{show_days}日）</h2>
{trade_sections}

</body>
</html>"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser(description="監視銘柄 指値バックテスト")
    parser.add_argument("--days",        type=int, default=365, help="表示期間(日) ※バックテストは常に全期間実行")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true", help="本日シグナル銘柄のみターミナル表示")
    parser.add_argument("--workers",     type=int, default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    print(f"監視銘柄バックテスト開始 ({len(WATCHLIST)}銘柄)...", flush=True)

    all_items: list[dict] = []

    def _proc(row):
        sym, name, strat = row
        return backtest_one(sym, name, strat)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_proc, row): row for row in WATCHLIST}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r is not None:
                    all_items.append(r)
            except Exception:
                pass
            if done % 6 == 0 or done == len(WATCHLIST):
                print(f"  {done}/{len(WATCHLIST)} 完了", flush=True)

    # 元の順序に並び替え（WATCHLIST定義順）
    order = {(s, st): i for i, (s, _, st) in enumerate(WATCHLIST)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    # ── ターミナル出力 ──
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 80)
    print(f"  監視銘柄 指値バックテスト結果  {today}  ({args.days}日表示)")
    print("=" * 80)

    # 本日シグナル
    signals_today = [i for i in all_items if i["today_sig"]]
    print(f"\n【本日シグナル】 {len(signals_today)}件")
    if signals_today:
        print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<6} {'現在値':>8} {'指値':>8} {'損切り':>8} {'目標':>8}")
        print("  " + "-" * 72)
        for item in signals_today:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12} {item['name']:<20} {item['strategy']:<6}"
                  f" {sig['current_price']:>8,.0f} {sig['limit_price']:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    # 銘柄別サマリー
    show_days = args.days
    print(f"\n【銘柄別バックテスト ({show_days}日)】")
    print(f"  {'銘柄':<12} {'名前':<20} {'戦略':<6} {'取引':>4} {'勝率':>6} {'PF':>6} {'損益':>10}  {'約定日数(平均/最短/最長)':>20}")
    print("  " + "-" * 90)
    for strat in ["MACD", "A7", "RSI2"]:
        items = [i for i in all_items if i["strategy"] == strat]
        for item in items:
            r = item["period_results"].get(show_days)
            if not r:
                print(f"  {item['symbol']:<12} {item['name']:<20} {strat:<6} {'データなし':>30}")
                continue
            pf_s = _pf_str(r["pf"])
            fill_days = [t.get("days_to_fill") for t in r["trade_log"] if isinstance(t.get("days_to_fill"), int)]
            if fill_days:
                avg_f = sum(fill_days) / len(fill_days)
                fill_info = f"avg:{avg_f:.1f} min:{min(fill_days)} max:{max(fill_days)}"
                dist = {d: fill_days.count(d) for d in sorted(set(fill_days))}
                dist_str = " ".join(f"{d}日×{n}" for d, n in dist.items())
                fill_str = f"{fill_info}  [{dist_str}]"
            else:
                fill_str = "-"
            print(f"  {item['symbol']:<12} {item['name']:<20} {strat:<6}"
                  f" {r['trades']:>4} {r['win_rate']:>5.1f}% {pf_s:>6} {r['total_pnl']:>+10,.0f}円  {fill_str}")

    # HTMLレポート出力
    out_path = Path(f"watchlist_limit_{today}.html")
    html = build_html(all_items, show_days)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
