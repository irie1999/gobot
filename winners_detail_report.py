"""
winners_detail_report.py  ―  winners 全銘柄の取引明細を1HTMLに集約
==================================================================
daytrade_donchian_winners.py の全銘柄を直近N日で個別バックテストし、
全銘柄の取引明細を1つのHTMLレポートにまとめる。

【使い方】
  python winners_detail_report.py                # 60日
  python winners_detail_report.py --days 30      # 30日
  python winners_detail_report.py --no-browser   # ブラウザ自動オープン無効

【出力】
  daytrade_winners_detail_YYYYMMDD.html
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats

JST = timezone(timedelta(hours=9))


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _fmt_dt(dt):
    if hasattr(dt, "strftime"):
        return dt.strftime("%m-%d %H:%M")
    return str(dt)[:16] if dt else "-"


def build_html(items, days, budget, today):
    # 全体集計
    all_trades = [t for it in items for t in it["trades"]]
    overall = calc_stats(all_trades, budget) if all_trades else None

    css = """
    <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 20px; background: #f5f5f5; color: #222; }
    h1 { color: #333; }
    h2 { color: #555; margin-top: 40px; border-bottom: 2px solid #ccc; padding-bottom: 4px; }
    h3 { color: #555; }
    table { border-collapse: collapse; margin-bottom: 16px; background: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); width: 100%; }
    th, td { padding: 6px 10px; text-align: right; border-bottom: 1px solid #eee;
             font-size: 13px; }
    th { background: #f8f8f8; font-weight: 600; }
    .sym { text-align: left; font-weight: 600; min-width: 140px; }
    .code { color: #999; font-weight: normal; }
    .profit { color: #c0392b; }
    .loss { color: #2980b9; }
    .summary { background: #fff8e1; padding: 12px; border-radius: 6px;
               margin-bottom: 20px; }
    .nav { background: white; padding: 12px; border-radius: 6px; margin-bottom: 20px; }
    .nav a { display: inline-block; margin: 3px 6px; color: #2980b9;
             text-decoration: none; font-size: 13px; }
    .nav a:hover { text-decoration: underline; }
    details { background: white; padding: 8px; border-radius: 4px; margin-bottom: 4px; }
    summary { cursor: pointer; padding: 4px; font-weight: 600; }
    </style>
    """

    # 全体サマリ
    if overall:
        n = overall["n"]
        sum_html = f"""
        <div class="summary">
          <strong>{len(items)}銘柄 × {days}日</strong>　|
          総取引: <strong>{n}</strong>　|
          勝率: <strong>{overall["win_rate"]:.1f}%</strong>　|
          PF: <strong>{_pf(overall["pf"])}</strong>　|
          総損益: <strong class="{'profit' if overall['total_pnl'] >= 0 else 'loss'}">
            {overall["total_pnl"]:+,.0f}円</strong>　|
          DD: <span class="loss">{overall["max_dd"]:+.1f}%</span>
        </div>
        """
    else:
        sum_html = "<div class='summary'>取引なし</div>"

    # 銘柄別サマリ表
    sym_summary_rows = ""
    sorted_items = sorted(items,
                          key=lambda x: sum(t["pnl"] for t in x["trades"]),
                          reverse=True)
    for it in sorted_items:
        ist = calc_stats(it["trades"], budget) if it["trades"] else None
        if ist is None or ist["n"] == 0:
            continue
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        sym_short = it["symbol"].replace(".T", "")
        sym_summary_rows += (
            f'<tr><td class="sym"><a href="#sym-{sym_short}">{it["name"]}</a>'
            f'<br><small class="code">{it["symbol"]}</small></td>'
            f'<td>{ist["n"]}</td><td>{ist["win_rate"]:.0f}%</td>'
            f'<td>{_pf(ist["pf"])}</td>'
            f'<td class="{c}">{ist["total_pnl"]:+,.0f}</td>'
            f'<td class="profit">{ist["avg_win"]:+,.0f}</td>'
            f'<td class="loss">{ist["avg_loss"]:+,.0f}</td>'
            f'<td class="loss">{ist["max_dd"]:+.1f}%</td></tr>'
        )

    sym_table = f"""
    <h2>銘柄別サマリ (損益順)</h2>
    <table>
      <thead><tr><th class="sym">銘柄</th><th>取引</th><th>勝率</th>
        <th>PF</th><th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th>
      </tr></thead>
      <tbody>{sym_summary_rows}</tbody>
    </table>
    """

    # ナビ
    nav_links = ""
    for it in sorted_items:
        if not it["trades"]:
            continue
        sym_short = it["symbol"].replace(".T", "")
        nav_links += f'<a href="#sym-{sym_short}">{it["name"]} ({sym_short})</a>'
    nav = f'<div class="nav"><strong>銘柄ジャンプ:</strong><br>{nav_links}</div>'

    # 銘柄別の取引明細 (折りたたみ)
    detail_html = "<h2>銘柄別 取引明細</h2>"
    for it in sorted_items:
        if not it["trades"]:
            continue
        sym_short = it["symbol"].replace(".T", "")
        ist = calc_stats(it["trades"], budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        detail_html += f'<h3 id="sym-{sym_short}">{it["name"]} ({it["symbol"]})  '
        detail_html += (f'<span style="font-weight:normal;font-size:14px;">'
                        f'取引{ist["n"]} | 勝率{ist["win_rate"]:.0f}% | '
                        f'PF {_pf(ist["pf"])} | '
                        f'<span class="{c}">{ist["total_pnl"]:+,.0f}円</span></span></h3>')

        trade_rows = ""
        for t in it["trades"]:
            ed = _fmt_dt(t.get("entry_dt"))
            xd = _fmt_dt(t.get("exit_dt"))
            try:
                delta = t["exit_dt"] - t["entry_dt"]
                hold = f"{int(delta.total_seconds() // 60)}分"
            except Exception:
                hold = "-"
            pc = "profit" if t["pnl"] >= 0 else "loss"
            trade_rows += f"""<tr>
              <td>{ed}</td><td>{xd}</td><td>{hold}</td>
              <td>{t['entry_p']:,.1f}</td>
              <td class="loss">{t['stop_p']:,.1f}</td>
              <td class="profit">{t['target_p']:,.1f}</td>
              <td>{t['exit_p']:,.1f}</td>
              <td>{t['qty']:,}</td>
              <td class="{pc}">{t['pnl']:+,.0f}</td>
              <td class="{pc}">{t['pct']:+.2f}%</td>
              <td>{t['reason']}</td></tr>"""

        detail_html += f"""
        <details open>
          <summary>取引明細 ({len(it['trades'])}件)</summary>
          <table>
            <thead><tr>
              <th>Entry</th><th>Exit</th><th>保有</th>
              <th>買値</th><th>損切</th><th>目標</th><th>決済値</th><th>株数</th>
              <th>損益</th><th>%</th><th>決済理由</th>
            </tr></thead>
            <tbody>{trade_rows}</tbody>
          </table>
        </details>
        """

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>winners 取引明細 - {today}</title>
{css}</head><body>
<h1>winners 取引明細レポート</h1>
<p>生成: {today} / 直近{days}日 / 戦略: Donchian (ultra_freq_v2) / 予算: {budget:,}円</p>
{sum_html}
{sym_table}
{nav}
{detail_html}
</body></html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="winners 全銘柄取引明細レポート")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        from daytrade_donchian_winners import SYMBOLS as WINNERS
    except ImportError:
        print("[error] daytrade_donchian_winners.py がありません。"
              "先に --extract-winners で生成してください")
        return

    print(f"winners 取引明細レポート生成: {len(WINNERS)}銘柄 / {args.days}日",
          flush=True)
    symbols = [s for s, _ in WINNERS]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    targets = [(s, n) for s, n in WINNERS if s in fetched]
    print(f"  ロード成功: {len(targets)}/{len(WINNERS)}銘柄", flush=True)

    items = []
    for i, (sym, name) in enumerate(targets, 1):
        r = backtest_symbol(sym, name, fetched[sym], args.budget, args.max_risk)
        if r:
            items.append(r)
        if i % 5 == 0 or i == len(targets):
            print(f"    {i}/{len(targets)} 処理済み", flush=True)

    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    html = build_html(items, args.days, args.budget, today)

    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
    out = Path(f"daytrade_winners_detail_{stamp}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out.resolve()}", flush=True)
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
