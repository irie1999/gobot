"""
analyze_strategy_type.py — 順張り系 vs 逆張り系 の収益性比較

戦略タイプ分類:
  逆張り系 (mean-reversion): RSI2, A7, MACD
    → 売られすぎ・押し目を狙う。含み損を抱えやすい。
  順張り系 (trend-following): DON, VOL, MOM
    → ブレイクアウト・モメンタムに乗る。

全トレード（目標達成・損切り・タイムカット）で集計し、
どちらのタイプが収益性が高いかを多角的に比較する。

Usage:
  python analyze_strategy_type.py
  python analyze_strategy_type.py --days 365 --workers 8
  python analyze_strategy_type.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "conservative")
import check_signals_stop     as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import fetch, run_limit_backtest, WORKERS, JST

# ── 戦略タイプ分類 ────────────────────────────────────────────────────────────
STRATEGY_TYPE = {
    "RSI2": "逆張り", "A7": "逆張り", "MACD": "逆張り",
    "DON":  "順張り", "VOL": "順張り", "MOM": "順張り",
}


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int, default=365)
    p.add_argument("--workers",    type=int, default=WORKERS)
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


def _run_one(sym, name, strat, calc_fn, em, sm, tm, days, entry_type):
    try:
        df = fetch(sym, days + 120)
        if df is None or len(df) < 30:
            return None
        r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm, days,
                               strat, entry_type=entry_type)
        if not r or not r.get("trade_log"):
            return None
        return strat, r["trade_log"]
    except Exception:
        return None


def collect_trades(days: int, workers: int) -> list[dict]:
    items = []
    for sym, name, strat in _stop.WATCHLIST:
        p = _stop.STRATEGY_PARAMS[strat]
        items.append((sym, name, strat, p[0], p[1], p[2], p[3], days, _stop.ENTRY_TYPE))
    for sym, name, strat in _brk.WATCHLIST:
        p = _brk.STRATEGY_PARAMS[strat]
        items.append((sym, name, strat, p[0], p[1], p[2], p[3], days, _brk.ENTRY_TYPE))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, *it): it for it in items}
        for fut in as_completed(futs):
            r = fut.result()
            if not r:
                continue
            strat, trade_log = r
            for t in trade_log:
                if t.get("exit_dt") is None:
                    continue
                if t.get("reason") == "発注中":
                    continue
                results.append({
                    "strategy": strat,
                    "type":     STRATEGY_TYPE.get(strat, "?"),
                    "reason":   t.get("reason", ""),
                    "pnl":      t["pnl"],
                    "pct":      t["pct"],
                    "hold_days": t.get("hold_days", 0),
                    "mae_pct":  t.get("mae_pct", 0.0),
                    "mfe_pct":  t.get("mfe_pct", 0.0),
                })
    return results


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {}
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    tgt = sum(1 for t in trades if "目標" in t["reason"])
    stp = sum(1 for t in trades if "損切" in t["reason"])
    tmo = sum(1 for t in trades if "タイム" in t["reason"])
    return dict(
        n=n, wr=wins / n * 100, pf=pf,
        gross_profit=gp, gross_loss=gl,
        total_pnl=sum(t["pnl"] for t in trades),
        avg_pnl=sum(t["pnl"] for t in trades) / n,
        avg_pct=sum(t["pct"] for t in trades) / n,
        avg_hold=sum(t["hold_days"] for t in trades) / n,
        avg_mae=sum(t["mae_pct"] for t in trades) / n,
        avg_mfe=sum(t["mfe_pct"] for t in trades) / n,
        tgt_pct=tgt / n * 100, stp_pct=stp / n * 100, tmo_pct=tmo / n * 100,
    )


# ── CSS / HTML ────────────────────────────────────────────────────────────────
_CSS = """
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:24px}
h1{color:#38bdf8;font-size:1.4rem}
h2{color:#94a3b8;font-size:1.05rem;margin-top:2.5rem}
p.note{color:#64748b;font-size:0.8rem;margin:-0.4rem 0 1rem}
table{border-collapse:separate;border-spacing:0;width:100%;margin-bottom:0.5rem;font-size:0.85rem}
thead th{position:sticky;top:0;z-index:10}
th{background:#1e293b;color:#94a3b8;padding:9px 12px;text-align:right;white-space:nowrap;
   box-shadow:inset 0 -1px 0 #334155, inset 0 1px 0 #334155}
th:first-child{text-align:left}
td{padding:8px 12px;border-bottom:1px solid #1e293b;text-align:right}
td:first-child{text-align:left;color:#e2e8f0;font-weight:600}
tr:hover td{background:#1e293b55}
.good{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}.gray{color:#64748b}
.big{font-size:1.3rem;font-weight:700}
"""


def _cells(s: dict) -> str:
    if not s:
        return "<td colspan='12' class='gray'>データなし</td>"
    wr_c  = "good" if s["wr"] >= 55 else ("warn" if s["wr"] >= 45 else "bad")
    pf_s  = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    pf_c  = "good" if s["pf"] >= 1.5 else ("warn" if s["pf"] >= 1.0 else "bad")
    pnl_c = "good" if s["total_pnl"] >= 0 else "bad"
    ap_c  = "good" if s["avg_pnl"] >= 0 else "bad"
    return (
        f"<td>{s['n']}</td>"
        f"<td class='{wr_c}'>{s['wr']:.1f}%</td>"
        f"<td class='{pf_c}'>{pf_s}</td>"
        f"<td class='good'>+{s['gross_profit']:,.0f}円</td>"
        f"<td class='bad'>-{s['gross_loss']:,.0f}円</td>"
        f"<td class='{pnl_c}'>{s['total_pnl']:+,.0f}円</td>"
        f"<td class='{ap_c}'>{s['avg_pnl']:+,.0f}円</td>"
        f"<td class='{ap_c}'>{s['avg_pct']:+.2f}%</td>"
        f"<td class='gray'>{s['avg_hold']:.1f}日</td>"
        f"<td class='bad'>{s['avg_mae']:+.2f}%</td>"
        f"<td class='good'>{s['avg_mfe']:+.2f}%</td>"
        f"<td class='gray'>{s['tgt_pct']:.0f}/{s['stp_pct']:.0f}/{s['tmo_pct']:.0f}</td>"
    )


_TH = (
    "<tr>"
    "<th style='text-align:left'>区分</th>"
    "<th>取引数</th><th>勝率</th><th>PF</th>"
    "<th>合計利益<br><span style='font-size:0.75em;color:#64748b'>勝ち分のみ</span></th>"
    "<th>合計損失<br><span style='font-size:0.75em;color:#64748b'>負け分のみ</span></th>"
    "<th>合計損益<br><span style='font-size:0.75em;color:#64748b'>純益</span></th>"
    "<th>平均損益</th><th>平均%</th>"
    "<th>平均保有</th>"
    "<th>平均MAE<br><span style='font-size:0.75em;color:#64748b'>最大含み損</span></th>"
    "<th>平均MFE<br><span style='font-size:0.75em;color:#64748b'>最大含み益</span></th>"
    "<th>目/損/時%<br><span style='font-size:0.75em;color:#64748b'>達成/損切/時間</span></th>"
    "</tr>"
)


def build_html(trades: list[dict], days: int) -> str:
    by_type = defaultdict(list)
    by_strat = defaultdict(list)
    for t in trades:
        by_type[t["type"]].append(t)
        by_strat[t["strategy"]].append(t)

    s_mean = _stats(by_type.get("逆張り", []))
    s_trend = _stats(by_type.get("順張り", []))

    # ── タイプ別サマリー ──────────────────────────────────────────────
    t1_rows = (
        f"<tr><td>📉 逆張り系 (RSI2/A7/MACD)</td>{_cells(s_mean)}</tr>"
        f"<tr><td>📈 順張り系 (DON/VOL/MOM)</td>{_cells(s_trend)}</tr>"
    )
    t1 = f"<h2>① 戦略タイプ別 総合比較</h2><table><thead>{_TH}</thead><tbody>{t1_rows}</tbody></table>"

    # ── 戦略別 (タイプ順にソート) ─────────────────────────────────────
    order = ["RSI2", "A7", "MACD", "DON", "VOL", "MOM"]
    t2_rows = ""
    for strat in order:
        if strat not in by_strat:
            continue
        s = _stats(by_strat[strat])
        typ = STRATEGY_TYPE[strat]
        icon = "📉" if typ == "逆張り" else "📈"
        t2_rows += f"<tr><td>{icon} {strat} ({typ})</td>{_cells(s)}</tr>"
    t2 = f"<h2>② 戦略別 詳細比較</h2><table><thead>{_TH}</thead><tbody>{t2_rows}</tbody></table>"

    # ── 結論ボックス ──────────────────────────────────────────────────
    def _winner(key, higher=True):
        mv, tv = s_mean.get(key, 0), s_trend.get(key, 0)
        if higher:
            return ("逆張り", mv, tv) if mv >= tv else ("順張り", tv, mv)
        return ("逆張り", mv, tv) if mv <= tv else ("順張り", tv, mv)

    w_pnl  = _winner("avg_pnl")
    w_wr   = _winner("wr")
    w_pf   = _winner("pf")
    w_mae  = _winner("avg_mae", higher=True)  # MAEは0に近い(浅い)方が良い→大きい方

    cards = f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 8px">
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">平均損益/取引 が高い</div>
    <div class="big" style="color:#4ade80">{w_pnl[0]}</div>
    <div style="color:#64748b;font-size:0.75rem">{w_pnl[1]:+,.0f}円 vs {w_pnl[2]:+,.0f}円</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">勝率 が高い</div>
    <div class="big" style="color:#4ade80">{w_wr[0]}</div>
    <div style="color:#64748b;font-size:0.75rem">{w_wr[1]:.1f}% vs {w_wr[2]:.1f}%</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">PF が高い</div>
    <div class="big" style="color:#4ade80">{w_pf[0]}</div>
    <div style="color:#64748b;font-size:0.75rem">{w_pf[1]:.2f} vs {w_pf[2]:.2f}</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">含み損が浅い (MAE)</div>
    <div class="big" style="color:#4ade80">{w_mae[0]}</div>
    <div style="color:#64748b;font-size:0.75rem">{w_mae[1]:+.2f}% vs {w_mae[2]:+.2f}%</div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>順張り vs 逆張り 収益性比較</title>
<style>{_CSS}</style></head>
<body>
<h1>順張り系 vs 逆張り系 — 収益性比較分析</h1>
<p class="note">
  分析期間: 直近{days}日 / 全WATCHLIST / 総トレード{len(trades)}件<br>
  逆張り系 = RSI2 / A7 / MACD（売られすぎ・押し目）&nbsp;&nbsp;
  順張り系 = DON / VOL / MOM（ブレイクアウト・モメンタム）<br>
  ※ 全トレード（目標達成・損切り・タイムカット）で集計。
</p>
{cards}
{t1}
{t2}
</body></html>"""


if __name__ == "__main__":
    args = _args()
    print(f"戦略タイプ比較: 直近{args.days}日 / workers={args.workers}", flush=True)
    print("トレードデータ収集中...", flush=True)
    trades = collect_trades(args.days, args.workers)
    print(f"収集完了: {len(trades)}件", flush=True)
    if not trades:
        print("トレードデータなし")
        sys.exit(1)

    # コンソール簡易出力
    by_type = defaultdict(list)
    for t in trades:
        by_type[t["type"]].append(t)
    print("\n=== 戦略タイプ別 ===")
    for typ in ["逆張り", "順張り"]:
        s = _stats(by_type.get(typ, []))
        if s:
            pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            print(f"{typ}: {s['n']}件 勝率{s['wr']:.1f}% PF{pf_s} "
                  f"合計{s['total_pnl']:+,.0f}円 平均{s['avg_pnl']:+,.0f}円/取引 "
                  f"MAE{s['avg_mae']:+.2f}%")

    html = build_html(trades, args.days)
    out = Path(f"strategy_type_{datetime.now(JST).date()}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML出力: {out}", flush=True)
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())
