"""
analyze_short_rr_sweep.py — ショート 目標×損切り 2次元スイープ + 銘柄選定可能性

目的:
  1) 損切りを目標に合わせて振る (リスクリワードを公平に検証)
     → 「小さい目標 + 小さい損切り」が機能するか
  2) 銘柄選定の効果を見る
     → 全ユニバース平均は負けでも、+収益の銘柄だけ選べば黒字化できるか

出力:
  - 戦略ごとに 目標(行) × 損切り(列) の PF マトリクス (緑=PF≥1.5)
  - 各戦略のベスト組合せで「個別に+収益の銘柄数」と上位銘柄リスト
    → これがショートWATCHLIST候補になる

Usage:
  python analyze_short_rr_sweep.py --budget 600000 --min-price 1000
  python analyze_short_rr_sweep.py --days 90 --limit 400
  python analyze_short_rr_sweep.py --targets 0.5,1.0,1.5,2.0,3.0 --stops 0.5,1.0,1.5,2.0
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
from backtest_limit_entry import fetch, run_limit_backtest, WORKERS, JST
from scan_walkforward import load_universe, STRATEGY_DEFS_CONSERVATIVE

_SHORT_DEFS = {k: v for k, v in STRATEGY_DEFS_CONSERVATIVE.items()
               if v[4] in ("short", "short_brk")}


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int,   default=180)
    p.add_argument("--workers",    type=int,   default=WORKERS)
    p.add_argument("--budget",     type=float, default=0.0)
    p.add_argument("--max-price",  type=float, default=0.0)
    p.add_argument("--min-price",  type=float, default=0.0)
    p.add_argument("--limit",      type=int,   default=0)
    p.add_argument("--targets",    type=str,   default="0.5,1.0,1.5,2.0,3.0")
    p.add_argument("--stops",      type=str,   default="0.5,1.0,1.5,2.0")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


def _one_symbol(sym, name, days, targets, stops, max_price, min_price):
    """1銘柄 × 全ショート戦略 × 全(目標,損切り)。
    返り値: [(strat, tm, sm, gp, gl, n, wins, sym_total, sym, name), ...]"""
    try:
        df = fetch(sym, days + 160)
        if df is None or len(df) < 60:
            return None
        latest = float(df.iloc[-1]["close"])
        if max_price > 0 and latest > max_price:
            return None
        if min_price > 0 and latest < min_price:
            return None
    except Exception:
        return None

    out: list[tuple] = []
    for strat, (calc_fn, em, _sm0, _tm0, family, entry_type) in _SHORT_DEFS.items():
        for sm in stops:
            for tm in targets:
                try:
                    r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                                           days, strat, entry_type=entry_type)
                except Exception:
                    continue
                if not r or not r.get("trade_log"):
                    continue
                gp = gl = tot = 0.0
                n = wins = 0
                for t in r["trade_log"]:
                    if t.get("exit_dt") is None or t.get("reason") == "発注中":
                        continue
                    pnl = t["pnl"]; tot += pnl; n += 1
                    if pnl > 0:
                        gp += pnl; wins += 1
                    elif pnl < 0:
                        gl += -pnl
                if n > 0:
                    out.append((strat, tm, sm, gp, gl, n, wins, tot, sym, name))
    return out


def _pf(d: dict) -> float:
    gp, gl = d["gp"], d["gl"]
    return gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)


_CSS = """
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:24px}
h1{color:#38bdf8;font-size:1.4rem}h2{color:#94a3b8;font-size:1.05rem;margin-top:2rem}
p.note{color:#64748b;font-size:.8rem;margin:-.4rem 0 1rem}
table{border-collapse:collapse;width:auto;margin-bottom:1rem;font-size:.85rem}
th,td{padding:7px 12px;border:1px solid #1e293b;text-align:center}
th{background:#1e293b;color:#94a3b8}
td.lbl{background:#16203a;color:#e2e8f0;font-weight:600;text-align:right}
.good{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}.gray{color:#64748b}
table.lst{width:100%}table.lst td{text-align:right}table.lst td:first-child{text-align:left}
"""


def _cell(d: dict | None) -> str:
    if not d or d["n"] == 0:
        return "<td class='gray'>-</td>"
    pf = _pf(d)
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    if pf >= 1.5:   bg, col = "#14331f", "#4ade80"
    elif pf >= 1.2: bg, col = "#1e2e14", "#a3e635"
    elif pf >= 1.0: bg, col = "#2a2410", "#fbbf24"
    else:           bg, col = "#2a1414", "#f87171"
    return (f"<td style='background:{bg};color:{col}'>{pf_s}"
            f"<br><span style='font-size:.72em;color:#64748b'>{d['tot']:+,.0f}</span></td>")


def build_html(grid: dict, persym: dict, targets, stops, days, n_syms) -> str:
    out = ""

    def _matrix(title, strat):
        head = "<tr><th>目標↓ / 損切り→</th>" + "".join(
            f"<th>×{sm}</th>" for sm in stops) + "</tr>"
        body = ""
        best = (-1e18, None)
        for tm in targets:
            row = f"<tr><td class='lbl'>×{tm}</td>"
            for sm in stops:
                d = grid.get((strat, tm, sm))
                row += _cell(d)
                if d and d["n"] and d["tot"] > best[0]:
                    best = (d["tot"], (tm, sm))
            body += row + "</tr>"
        note = ""
        if best[1]:
            bt, bs = best[1]
            note = (f"<p class='note'>最高損益: 目標×{bt} / 損切り×{bs} "
                    f"(合計 {best[0]:+,.0f}円)</p>")
        return f"<h2>{title}</h2>{note}<table>{head}{body}</table>", best[1]

    m, _ = _matrix("① 全ショート合算 — PF マトリクス (目標×損切り)", "ALL")
    out += m

    for strat in _SHORT_DEFS:
        m, best = _matrix(f"{strat} — PF マトリクス", strat)
        out += m
        if best:
            tm, sm = best
            sym_pnl = persym.get((strat, tm, sm), {})
            winners = [(s, v[0], v[1]) for s, v in sym_pnl.items() if v[0] > 0]
            winners.sort(key=lambda x: x[1], reverse=True)
            total_pos = sum(w[1] for w in winners)
            rows = "".join(
                f"<tr><td>{s} <span style='color:#64748b;font-size:.78em'>{nm}</span></td>"
                f"<td class='good'>{p:+,.0f}円</td></tr>"
                for s, p, nm in winners[:15])
            out += (
                f"<p class='note'>↑ベスト組合せ(目標×{tm}/損切り×{sm}): 全{len(sym_pnl)}銘柄中 "
                f"<b style='color:#4ade80'>{len(winners)}銘柄が+収益</b> "
                f"(合計 {total_pos:+,.0f}円)。これらがショートWATCHLIST候補。</p>"
                f"<table class='lst'><thead><tr><th style='text-align:left'>銘柄</th>"
                f"<th>損益</th></tr></thead><tbody>{rows}</tbody></table>")

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>ショート 目標×損切り スイープ</title><style>{_CSS}</style></head><body>
<h1>ショート 目標×損切り 2次元スイープ + 銘柄選定可能性</h1>
<p class="note">
 直近{days}日 / ユニバース{n_syms}銘柄 / セル=PF, 下段=合計損益<br>
 緑=PF≥1.5 / 黄緑=≥1.2 / 黄=≥1.0 / 赤=&lt;1.0<br>
 ※「+収益銘柄」= その組合せで個別に黒字の銘柄。全体平均が負けでも選定で黒字化できるかの指標。
</p>
{out}
</body></html>"""


if __name__ == "__main__":
    a = _args()
    targets = [float(x) for x in a.targets.split(",") if x.strip()]
    stops   = [float(x) for x in a.stops.split(",")   if x.strip()]
    max_price = a.max_price or (a.budget / 100 if a.budget > 0 else 0.0)

    syms, src = load_universe()
    if a.limit > 0:
        syms = syms[:a.limit]
    print(f"ショートRRスイープ: {src} {len(syms)}銘柄 / 直近{a.days}日 / "
          f"目標{targets} × 損切り{stops} / workers={a.workers}", flush=True)
    if max_price > 0:
        print(f"  価格上限: {max_price:,.0f}円", flush=True)
    if a.min_price > 0:
        print(f"  価格下限: {a.min_price:,.0f}円", flush=True)

    # grid[(strat,tm,sm)] = {gp,gl,n,wins,tot}; persym[(strat,tm,sm)] = {sym:(tot,name)}
    grid: dict = defaultdict(lambda: {"gp": 0.0, "gl": 0.0, "n": 0, "wins": 0, "tot": 0.0})
    persym: dict = defaultdict(dict)
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(_one_symbol, s, n, a.days, targets, stops,
                          max_price, a.min_price): s for s, n in syms}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  進捗: {done}/{len(syms)}", flush=True)
            r = fut.result()
            if not r:
                continue
            for strat, tm, sm, gp, gl, n, wins, tot, sym, name in r:
                for key in [(strat, tm, sm), ("ALL", tm, sm)]:
                    d = grid[key]
                    d["gp"] += gp; d["gl"] += gl; d["n"] += n
                    d["wins"] += wins; d["tot"] += tot
                persym[(strat, tm, sm)][sym] = (tot, name)

    if not grid:
        print("トレードなし"); sys.exit(1)

    print("\n=== 全ショート合算 PF (目標行 × 損切り列) ===")
    print("目標\\損切" + "".join(f"{'×'+str(sm):>10}" for sm in stops))
    for tm in targets:
        line = f"×{tm:<7}"
        for sm in stops:
            d = grid.get(("ALL", tm, sm))
            pf = _pf(d) if d and d["n"] else 0.0
            pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
            line += f"{pf_s:>10}"
        print(line)

    html = build_html(grid, persym, targets, stops, a.days, len(syms))
    out = Path(f"short_rr_sweep_{datetime.now(JST).date()}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML出力: {out}", flush=True)
    if not a.no_browser:
        webbrowser.open(out.resolve().as_uri())
