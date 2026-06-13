"""
verify_watchlist_daytrade.py  ―  WATCHLIST 動作検証
==================================================================
逆指値ロング (verify_watchlist.py) を5分足デイトレ用に移植。

WATCHLIST 提案ファイルを読み、実成績で検証して HTML レポート生成。

【検証内容】
- 直近30日/60日/90日 の戦略別パフォーマンス
- 銘柄ごとの取引明細
- スコアリング (★★★/★★/★/△)
- 相場局面とのマッチ度

【使い方】
  python verify_watchlist_daytrade.py daytrade_watchlist_macd.py
  python verify_watchlist_daytrade.py --watchlist daytrade_combined_watchlist.py
  python verify_watchlist_daytrade.py --period 60
"""

from __future__ import annotations

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_signals_daytrade import backtest_one, _load_watchlist, SIGNAL_PERIODS
from nikkei_filter_daytrade import detect_regime, format_regime_html
from risk_metrics_5m import calc_recommend_score

JST = timezone(timedelta(hours=9))


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    parser = argparse.ArgumentParser(description="WATCHLIST 検証")
    parser.add_argument("watchlist_file", nargs="?", default=None)
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--period", type=int, default=90,
                        choices=SIGNAL_PERIODS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    wl_file = args.watchlist_file or args.watchlist
    watchlist = _load_watchlist(wl_file)
    if not watchlist:
        print(f"[error] WATCHLIST 読込失敗: {wl_file}")
        return

    print(f"検証: {wl_file or 'デフォルト'} ({len(watchlist)}件)")

    # 相場局面
    regime = detect_regime()
    print(f"現在の相場: {regime['label']} / 推奨戦略: {regime.get('allowed_strategies')}")

    # backtest
    items = []
    targets = []
    for entry in watchlist:
        if len(entry) >= 3:
            sym, name, strat = entry[:3]
        else:
            sym, name = entry[:2]
            strat = "DON"
        targets.append((sym, name, strat))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(backtest_one, s, n, st): (s, st)
                for s, n, st in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                items.append(r)
            if i % 20 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)}", flush=True)

    items.sort(key=lambda x: -x["score"])

    # 集計 (指定期間)
    days = args.period
    total_n = 0
    total_pnl = 0.0
    profitable = 0
    for it in items:
        s = it["period_stats"].get(days, {})
        total_n += s.get("n", 0)
        total_pnl += s.get("total_pnl", 0)
        if s.get("total_pnl", 0) > 0:
            profitable += 1

    # 相場マッチ判定
    allowed = regime.get("allowed_strategies")
    if allowed:
        matched = [it for it in items if it["strategy"] in allowed]
        match_rate = len(matched) / len(items) * 100 if items else 0
    else:
        matched = items
        match_rate = 100

    # HTML
    today = datetime.now(JST).strftime("%Y-%m-%d")

    rows = ""
    for it in items:
        s = it["period_stats"].get(days, {})
        if s.get("n", 0) == 0:
            continue
        is_allowed = (not allowed) or (it["strategy"] in allowed)
        bg = "#0d4d2f" if is_allowed else "#2d1f00"
        mark = "✓" if is_allowed else "—"
        pc = "profit" if s.get("total_pnl", 0) >= 0 else "loss"
        pf_v = s.get("pf", 0)
        pf_c = "#4ade80" if pf_v >= 1.5 else "#facc15" if pf_v >= 1.0 else "#f87171"
        rows += f"""
<tr style="background:{bg}">
  <td>{mark}</td>
  <td><strong style="color:{['#f87171','#f59e0b','#facc15','#4ade80'][min(it['score']//20, 3)]}">
    {it['rank']}{it['score']}</strong></td>
  <td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>
  <td>{it["strategy"]}</td>
  <td>{it.get("last_price", 0):,.0f}</td>
  <td>{s.get("n", 0)}</td>
  <td>{s.get("win_rate", 0):.0f}%</td>
  <td style="color:{pf_c}">{_pf(pf_v)}</td>
  <td class="{pc}">{s.get("total_pnl", 0):+,.0f}</td>
  <td class="loss">{s.get("max_dd", 0):+.1f}%</td>
  <td>{s.get("sharpe", 0):.2f}</td>
</tr>"""

    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; }
h1 { color: #10b981; font-size: 1.5rem; }
.subtitle { color: #94a3b8; margin-bottom: 20px; font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th { background: #1e293b; color: #94a3b8; padding: 6px 8px;
     text-align: center; border: 1px solid #334155; }
td { padding: 5px 8px; border: 1px solid #1e293b;
     text-align: right; white-space: nowrap; }
.sym { text-align: left; font-weight: 600; }
.code { color: #64748b; font-size: 0.72rem; }
.profit { color: #4ade80; }
.loss { color: #f87171; }
.box { background: #1e293b; padding: 14px; border-radius: 8px;
       margin-bottom: 14px; display: flex; gap: 24px; }
.box .it { text-align: center; }
.box .lb { color: #94a3b8; font-size: 0.72rem; }
.box .vl { font-size: 1.3rem; font-weight: 700; }
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>WATCHLIST検証 — {today}</title>
<style>{css}</style></head><body>
<h1>✓ WATCHLIST 検証レポート</h1>
<p class="subtitle">対象: {wl_file or 'デフォルト'} / 検証期間: 直近{days}日 / {today}</p>
{format_regime_html(regime)}
<div class="box">
  <div class="it"><div class="lb">監視銘柄</div><div class="vl">{len(items)}</div></div>
  <div class="it"><div class="lb">相場マッチ</div>
    <div class="vl profit">{len(matched)} ({match_rate:.0f}%)</div></div>
  <div class="it"><div class="lb">{days}日取引</div><div class="vl">{total_n}</div></div>
  <div class="it"><div class="lb">{days}日損益</div>
    <div class="vl {'profit' if total_pnl >= 0 else 'loss'}">{total_pnl:+,.0f}円</div></div>
  <div class="it"><div class="lb">プラス銘柄</div>
    <div class="vl profit">{profitable}/{len(items)}</div></div>
</div>
<table>
  <thead><tr>
    <th>マッチ</th><th>スコア</th><th>銘柄</th><th>戦略</th><th>現値</th>
    <th>取引</th><th>勝率</th><th>PF</th><th>損益</th><th>DD</th><th>Sharpe</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="color:#64748b;font-size:0.78rem;margin-top:16px">
  💡 <strong>マッチ列</strong>: ✓ = 現在の相場局面 ({regime["label"]}) に
  推奨される戦略。— = 不適合 (検討対象外)
</p>
</body></html>"""

    out = Path(f"verify_watchlist_daytrade_{today}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
