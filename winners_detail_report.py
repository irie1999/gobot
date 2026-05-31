"""
winners_detail_report.py  ―  winners 全銘柄の取引明細を1HTMLに集約
==================================================================
daytrade_donchian.py と同じダークテーマで winners 全銘柄を集約表示。

【使い方】
  python winners_detail_report.py                # 60日
  python winners_detail_report.py --days 30      # 30日
  python winners_detail_report.py --no-browser

【出力】
  daytrade_winners_detail_YYYYMMDD_HHMM.html
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats, DON_PERIOD, TARGET_R

JST = timezone(timedelta(hours=9))


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _fmt_dt(dt):
    if hasattr(dt, "strftime"):
        return dt.strftime("%m-%d %H:%M")
    return str(dt)[:16] if dt else "-"


def build_html(items, days, budget, source, today):
    # 全体集計
    all_trades_flat = [t for it in items for t in it["trades"]]
    s = calc_stats(all_trades_flat, budget) if all_trades_flat else dict(
        n=0, win_rate=0, pf=0, total_pnl=0, max_dd=0, avg_win=0, avg_loss=0
    )
    cls = "profit" if s["total_pnl"] >= 0 else "loss"

    # 銘柄別サマリ (損益順)
    sorted_items = sorted(items,
                          key=lambda x: sum(t["pnl"] for t in x["trades"]),
                          reverse=True)
    sym_rows = ""
    nav_links = ""
    for it in sorted_items:
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        sym_short = it["symbol"].replace(".T", "")
        sym_rows += (
            f'<tr><td class="sym"><a href="#sym-{sym_short}" style="color:#10b981;text-decoration:none">'
            f'{it["name"]}</a><br><small class="code">{it["symbol"]}</small></td>'
            f'<td>{ist["n"]}</td><td>{ist["win_rate"]:.0f}%</td>'
            f'<td>{_pf(ist["pf"])}</td>'
            f'<td class="{c}">{ist["total_pnl"]:+,.0f}</td>'
            f'<td class="profit">{ist["avg_win"]:+,.0f}</td>'
            f'<td class="loss">{ist["avg_loss"]:+,.0f}</td>'
            f'<td class="loss">{ist["max_dd"]:+.1f}%</td></tr>'
        )
        nav_links += (f'<a href="#sym-{sym_short}" style="color:#10b981;'
                      f'margin:0 8px;font-size:.85rem;text-decoration:none">'
                      f'{it["name"]}</a> ')

    # 銘柄ごとの取引明細
    detail_html = ""
    for it in sorted_items:
        if not it["trades"]:
            continue
        sym_short = it["symbol"].replace(".T", "")
        ist = calc_stats(it["trades"], budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"

        trade_rows = ""
        for t in sorted(it["trades"], key=lambda x: str(x.get("entry_dt", ""))):
            pc = "profit" if t["pnl"] > 0 else "loss"
            ed = _fmt_dt(t.get("entry_dt"))
            xd = _fmt_dt(t.get("exit_dt"))
            try:
                delta = t["exit_dt"] - t["entry_dt"]
                hold = f"{int(delta.total_seconds() // 60)}分"
            except Exception:
                hold = "-"
            trade_rows += f"""<tr>
              <td>{ed}</td><td>{xd}</td><td>{hold}</td>
              <td>{t['entry_p']:,.0f}</td>
              <td class="loss">{t['stop_p']:,.0f}</td>
              <td class="profit">{t['target_p']:,.0f}</td>
              <td>{t['exit_p']:,.0f}</td>
              <td>{t['qty']}</td>
              <td class="{pc}">{t['pnl']:+,.0f}</td>
              <td class="{pc}">{t['pct']:+.2f}%</td>
              <td>{t['reason']}</td></tr>"""

        detail_html += f"""
        <h2 id="sym-{sym_short}">{it["name"]} ({it["symbol"]})</h2>
        <div class="box">
          <div class="it"><div class="lb">総損益</div>
            <div class="vl {c}">{ist["total_pnl"]:+,.0f}円</div></div>
          <div class="it"><div class="lb">取引</div><div class="vl">{ist["n"]}</div></div>
          <div class="it"><div class="lb">勝率</div><div class="vl">{ist["win_rate"]:.1f}%</div></div>
          <div class="it"><div class="lb">PF</div><div class="vl">{_pf(ist["pf"])}</div></div>
          <div class="it"><div class="lb">DD</div><div class="vl loss">{ist["max_dd"]:+.1f}%</div></div>
          <div class="it"><div class="lb">平均利益</div><div class="vl profit">{ist["avg_win"]:+,.0f}</div></div>
          <div class="it"><div class="lb">平均損失</div><div class="vl loss">{ist["avg_loss"]:+,.0f}</div></div>
        </div>
        <table><thead><tr>
          <th>Entry</th><th>Exit</th><th>保有</th>
          <th>買値</th><th>損切</th><th>目標</th><th>決済値</th><th>株数</th>
          <th>損益</th><th>%</th><th>決済理由</th>
        </tr></thead><tbody>{trade_rows}</tbody></table>
        """

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>winners Detail — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #10b981;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:160px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.3rem;font-weight:700}}
.nav{{background:#1e293b;padding:10px;border-radius:6px;margin-bottom:14px;line-height:1.8}}
</style></head><body>
<h1>winners 取引明細レポート (Donchian {DON_PERIOD}本高値ブレイク)</h1>
<p class="sub">生成:{today} / 直近{days}日 / source:{source} / 予算:{budget:,}円 / 対象:{len(items)}銘柄<br>
エントリー: 5分足{DON_PERIOD}本高値更新+陽線 → 翌バー寄付買い<br>
損切り: {DON_PERIOD}本安値 / 目標: R:R {TARGET_R}:1 / トレーリング / 前場限定</p>
<div class="box">
<div class="it"><div class="lb">総損益</div><div class="vl {cls}">{s["total_pnl"]:+,.0f}円</div></div>
<div class="it"><div class="lb">取引</div><div class="vl">{s["n"]}</div></div>
<div class="it"><div class="lb">勝率</div><div class="vl">{s["win_rate"]:.1f}%</div></div>
<div class="it"><div class="lb">PF</div><div class="vl">{_pf(s["pf"])}</div></div>
<div class="it"><div class="lb">DD</div><div class="vl loss">{s["max_dd"]:+.1f}%</div></div>
</div>
<h2>銘柄別サマリ (損益順)</h2>
<table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
<tbody>{sym_rows}</tbody></table>
<div class="nav"><strong style="color:#94a3b8">銘柄ジャンプ:</strong><br>{nav_links}</div>
{detail_html}
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="winners 全銘柄取引明細レポート (ダークテーマ)")
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
    html = build_html(items, args.days, args.budget, "local", today)

    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
    out = Path(f"daytrade_winners_detail_{stamp}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out.resolve()}", flush=True)
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
