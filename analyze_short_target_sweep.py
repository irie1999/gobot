"""
analyze_short_target_sweep.py — ショートの「目標%」スイープ分析

「何%の下落を目標にすればショートが機能し始めるか」を直接測る。
目標倍率 (target_atr_mult) を 0.5〜3.0 で振りながら、ユニバース全体で
ショート6戦略をバックテストし、目標%帯ごとの 勝率 / PF / 損益 を比較する。

上昇相場では大きな下落目標 (-9%) は届かず機能しないが、
小さな目標 (-2〜3%) なら反発前に利確できて機能する可能性がある。
その分岐点を見つけるのが目的。

Usage:
  python analyze_short_target_sweep.py                       # 直近180日・全ユニバース
  python analyze_short_target_sweep.py --days 90
  python analyze_short_target_sweep.py --budget 600000       # 6000円以下のみ
  python analyze_short_target_sweep.py --min-price 1000      # 1000円未満を除外(ショート向け)
  python analyze_short_target_sweep.py --limit 300           # 先頭300銘柄(高速デバッグ)
  python analyze_short_target_sweep.py --targets 0.5,1.0,1.5,2.0,2.5,3.0
  python analyze_short_target_sweep.py --no-browser
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

# ショート戦略のみ抽出 (family が short / short_brk)
_SHORT_DEFS = {k: v for k, v in STRATEGY_DEFS_CONSERVATIVE.items()
               if v[4] in ("short", "short_brk")}


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int,   default=180)
    p.add_argument("--workers",    type=int,   default=WORKERS)
    p.add_argument("--budget",     type=float, default=0.0,
                   help="予算(円)。latest_price > budget/100 を除外")
    p.add_argument("--max-price",  type=float, default=0.0)
    p.add_argument("--min-price",  type=float, default=0.0,
                   help="この価格未満を除外 (ショートは低位株を避ける)")
    p.add_argument("--limit",      type=int,   default=0)
    p.add_argument("--targets",    type=str,   default="0.5,1.0,1.5,2.0,2.5,3.0",
                   help="目標ATR倍率のスイープ値 (カンマ区切り)")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


def _one_symbol(sym, name, days, targets, max_price, min_price):
    """1銘柄について全ショート戦略×全目標倍率をバックテスト。"""
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

    out: list[tuple] = []   # (strat, tm, trade_dict)
    for strat, (calc_fn, em, sm, _tm0, family, entry_type) in _SHORT_DEFS.items():
        for tm in targets:
            try:
                r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                                       days, strat, entry_type=entry_type)
            except Exception:
                continue
            if not r or not r.get("trade_log"):
                continue
            for t in r["trade_log"]:
                if t.get("exit_dt") is None or t.get("reason") == "発注中":
                    continue
                ep  = t.get("entry_p", 0) or 0
                otg = t.get("order_target", 0) or 0
                tgt_pct = ((ep - otg) / ep * 100) if ep else 0.0  # ショート目標距離%
                out.append((strat, tm, {
                    "pnl": t["pnl"], "pct": t["pct"],
                    "reason": t.get("reason", ""),
                    "hold": t.get("hold_days", 0),
                    "tgt_pct": tgt_pct,
                }))
    return out


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {}
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    tgt = sum(1 for t in trades if "目標" in t["reason"])
    return dict(
        n=n, wr=wins / n * 100, pf=pf,
        total_pnl=sum(t["pnl"] for t in trades),
        avg_pnl=sum(t["pnl"] for t in trades) / n,
        avg_hold=sum(t["hold"] for t in trades) / n,
        avg_tgt=sum(t["tgt_pct"] for t in trades) / n,
        tgt_hit=tgt / n * 100,
    )


_CSS = """
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:24px}
h1{color:#38bdf8;font-size:1.4rem}h2{color:#94a3b8;font-size:1.05rem;margin-top:2rem}
p.note{color:#64748b;font-size:0.8rem;margin:-0.4rem 0 1rem}
table{border-collapse:separate;border-spacing:0;width:100%;margin-bottom:.5rem;font-size:.85rem}
thead th{position:sticky;top:0;z-index:10}
th{background:#1e293b;color:#94a3b8;padding:9px 12px;text-align:right;white-space:nowrap;
   box-shadow:inset 0 -1px 0 #334155,inset 0 1px 0 #334155}
th:first-child{text-align:left}
td{padding:8px 12px;border-bottom:1px solid #1e293b;text-align:right}
td:first-child{text-align:left;color:#e2e8f0;font-weight:600}
tr:hover td{background:#1e293b55}
.good{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}.gray{color:#64748b}
"""

_TH = ("<tr><th style='text-align:left'>目標(ATR倍)</th>"
       "<th>実効目標%<br><span style='font-size:.75em;color:#64748b'>平均下落幅</span></th>"
       "<th>取引数</th><th>勝率</th><th>PF</th><th>合計損益</th><th>平均損益</th>"
       "<th>目標達成%</th><th>平均保有</th></tr>")


def _row(tm: float, s: dict) -> str:
    if not s:
        return f"<tr><td>ATR×{tm}</td><td colspan='8' class='gray'>データなし</td></tr>"
    wr_c = "good" if s["wr"] >= 55 else ("warn" if s["wr"] >= 45 else "bad")
    pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    pf_c = "good" if s["pf"] >= 1.5 else ("warn" if s["pf"] >= 1.0 else "bad")
    pnl_c = "good" if s["total_pnl"] >= 0 else "bad"
    # PF>=1.5 の行を強調
    bg = " style='background:#14331f'" if s["pf"] >= 1.5 else ""
    return (f"<tr{bg}><td>ATR×{tm}</td>"
            f"<td class='warn'>-{s['avg_tgt']:.1f}%</td>"
            f"<td>{s['n']}</td>"
            f"<td class='{wr_c}'>{s['wr']:.1f}%</td>"
            f"<td class='{pf_c}'>{pf_s}</td>"
            f"<td class='{pnl_c}'>{s['total_pnl']:+,.0f}円</td>"
            f"<td class='{pnl_c}'>{s['avg_pnl']:+,.0f}円</td>"
            f"<td class='good'>{s['tgt_hit']:.0f}%</td>"
            f"<td class='gray'>{s['avg_hold']:.1f}日</td></tr>")


def build_html(agg: dict, targets: list[float], days: int, n_syms: int) -> str:
    # agg[(strat, tm)] = [trades]; agg[("ALL", tm)] = combined
    sects = ""
    # 全ショート合算
    sects += "<h2>① 全ショート戦略 合算 — 目標%スイープ</h2>"
    sects += "<p class='note'>緑背景=PF≥1.5(機能)。実効目標%が小さいほど反発前に利確できる。</p>"
    sects += f"<table><thead>{_TH}</thead><tbody>"
    for tm in targets:
        sects += _row(tm, _stats(agg.get(("ALL", tm), [])))
    sects += "</tbody></table>"
    # 戦略別
    for strat in _SHORT_DEFS:
        rows = "".join(_row(tm, _stats(agg.get((strat, tm), []))) for tm in targets)
        sects += (f"<h2>{strat} — 目標%スイープ</h2>"
                  f"<table><thead>{_TH}</thead><tbody>{rows}</tbody></table>")

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>ショート目標%スイープ</title><style>{_CSS}</style></head><body>
<h1>ショート 目標%スイープ分析 — 何%でショートが効くか</h1>
<p class="note">
 直近{days}日 / ユニバース{n_syms}銘柄 / 損切りATR×1.5固定・目標のみスイープ<br>
 ショート戦略: {' / '.join(_SHORT_DEFS.keys())}<br>
 ※ 実効目標% = 約定価格からの下落目標 (ATR依存で銘柄ごとに変動するため平均値)。
</p>
{sects}
</body></html>"""


if __name__ == "__main__":
    a = _args()
    targets = [float(x) for x in a.targets.split(",") if x.strip()]
    max_price = a.max_price or (a.budget / 100 if a.budget > 0 else 0.0)

    syms, src = load_universe()
    if a.limit > 0:
        syms = syms[:a.limit]
    print(f"ショート目標スイープ: {src} {len(syms)}銘柄 / 直近{a.days}日 / "
          f"目標倍率={targets} / workers={a.workers}", flush=True)
    if max_price > 0:
        print(f"  価格上限: {max_price:,.0f}円", flush=True)
    if a.min_price > 0:
        print(f"  価格下限: {a.min_price:,.0f}円 (低位株除外)", flush=True)

    agg: dict = defaultdict(list)
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(_one_symbol, s, n, a.days, targets, max_price, a.min_price): s
                for s, n in syms}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  進捗: {done}/{len(syms)}", flush=True)
            r = fut.result()
            if not r:
                continue
            for strat, tm, t in r:
                agg[(strat, tm)].append(t)
                agg[("ALL", tm)].append(t)

    if not agg:
        print("トレードなし"); sys.exit(1)

    print("\n=== 全ショート合算 目標%スイープ ===")
    print(f"{'目標':<10}{'実効%':>8}{'取引':>7}{'勝率':>8}{'PF':>7}{'損益':>14}")
    for tm in targets:
        s = _stats(agg.get(("ALL", tm), []))
        if s:
            pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            print(f"ATR×{tm:<6}{-s['avg_tgt']:>7.1f}%{s['n']:>7}"
                  f"{s['wr']:>7.1f}%{pf_s:>7}{s['total_pnl']:>+13,.0f}円")

    html = build_html(agg, targets, a.days, len(syms))
    out = Path(f"short_target_sweep_{datetime.now(JST).date()}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML出力: {out}", flush=True)
    if not a.no_browser:
        webbrowser.open(out.resolve().as_uri())
