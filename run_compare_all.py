"""
run_compare_all.py  ―  全戦略一括バックテスト比較レポート
==========================================================
ロング6戦略 + ショート6戦略を複数期間で一括実行し、
成績を横断比較するHTMLレポートを生成する。

【使い方】
  python run_compare_all.py                  # 90/180/365日 × 全12戦略
  python run_compare_all.py --workers 8
  python run_compare_all.py --no-browser
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──
import os
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import check_signals_stop           as _stop
import check_signals_breakout       as _brk
import check_signals_short          as _short
import check_signals_short_breakout as _short_brk

JST     = timezone(timedelta(hours=9))
PERIODS = [90, 180, 365]

# 全グループ定義: (モジュール, グループ名, 方向)
GROUPS = [
    (_stop,      "逆指値B",         "long"),
    (_brk,       "ブレイクアウト",   "long"),
    (_short,     "ショートB",        "short"),
    (_short_brk, "ショートBRK",      "short"),
]


def _run_group(mod, workers: int) -> list[dict]:
    """1グループのバックテストを全期間分実行。"""
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in mod.WATCHLIST}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass
    return all_items


def _summarize(items: list[dict], days: int) -> dict:
    """指定期間の戦略別集計を返す。"""
    summary: dict[str, dict] = {}
    for item in items:
        strat = item["strategy"]
        pr    = item["period_results"].get(days) or {}
        if not pr or pr["trades"] == 0:
            continue
        if strat not in summary:
            summary[strat] = dict(trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0)
        s = summary[strat]
        s["trades"] += pr["trades"]
        s["wins"]   += pr["wins"]
        s["pnl"]    += pr["total_pnl"]
        for t in pr.get("trade_log", []):
            if t["pnl"] > 0:
                s["gp"] += t["pnl"]
            else:
                s["gl"] += abs(t["pnl"])
    return summary


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _score(wr: float, pf: float, pnl: float, trades: int) -> float:
    """簡易スコア: 勝率×0.4 + PF×0.3 + 安定×0.2 + 回数×0.1"""
    pf_capped = min(pf if pf != float("inf") else 10, 10)
    stable    = 1.0 if pnl > 0 else 0.0
    return (wr / 100 * 40) + (pf_capped / 10 * 30) + (stable * 20) + (min(trades / 20, 1) * 10)


def build_html(
    group_results: list[tuple],   # (mod, label, direction, items)
    today: str,
) -> str:
    from backtest_limit_entry import fetch_n225_return

    # ── 全戦略 × 全期間 の集計テーブルを構築 ──────────────────────
    # rows: [{ label, strategy, direction, periods: {days: {wr,pf,pnl,trades,score}} }]
    rows: list[dict] = []
    for mod, label, direction, items in group_results:
        strats = list(dict.fromkeys(s for _, _, s in mod.WATCHLIST))
        for strat in strats:
            strat_items = [i for i in items if i["strategy"] == strat]
            period_stats: dict[int, dict] = {}
            for days in PERIODS:
                merged: dict[str, dict] = {}
                for item in strat_items:
                    pr = item["period_results"].get(days) or {}
                    if not pr or pr["trades"] == 0:
                        continue
                    if strat not in merged:
                        merged[strat] = dict(trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0)
                    m = merged[strat]
                    m["trades"] += pr["trades"]
                    m["wins"]   += pr["wins"]
                    m["pnl"]    += pr["total_pnl"]
                    for t in pr.get("trade_log", []):
                        if t["pnl"] > 0:
                            m["gp"] += t["pnl"]
                        else:
                            m["gl"] += abs(t["pnl"])
                if strat in merged:
                    m    = merged[strat]
                    wr   = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
                    pf   = m["gp"] / m["gl"] if m["gl"] > 0 else (float("inf") if m["gp"] > 0 else 0)
                    sc   = _score(wr, pf, m["pnl"], m["trades"])
                    period_stats[days] = dict(
                        trades=m["trades"], wins=m["wins"],
                        wr=wr, pf=pf, pnl=m["pnl"], score=sc,
                    )
            rows.append(dict(
                label=label, strategy=strat,
                direction=direction, periods=period_stats,
            ))

    # ── ランキング (365日スコア降順) ────────────────────────────────
    rows.sort(key=lambda r: r["periods"].get(365, {}).get("score", -1), reverse=True)

    # ── 日経平均リターン ──────────────────────────────────────────
    n225 = {d: fetch_n225_return(d) for d in PERIODS}

    # ── 比較テーブル HTML ─────────────────────────────────────────
    period_th = "".join(
        f'<th colspan="4">{d}日 <small style="color:#64748b">(日経{n225[d]:+.1f}%)</small></th>'
        for d in PERIODS
    )
    period_sub = "<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(PERIODS)

    table_rows = ""
    for i, r in enumerate(rows):
        rank_badge = ""
        if   i == 0: rank_badge = '<span class="badge gold">1位</span>'
        elif i == 1: rank_badge = '<span class="badge silver">2位</span>'
        elif i == 2: rank_badge = '<span class="badge bronze">3位</span>'

        dir_cls  = "tag-long" if r["direction"] == "long" else "tag-short"
        dir_lbl  = "買い" if r["direction"] == "long" else "売り"
        strat_lc = r["strategy"].lower().replace("_", "-")

        cells = ""
        for d in PERIODS:
            ps = r["periods"].get(d)
            if not ps:
                cells += "<td>-</td><td>-</td><td>-</td><td>-</td>"
            else:
                wr_cls  = "good" if ps["wr"] >= 55 else ("warn" if ps["wr"] >= 45 else "bad")
                pf_cls  = "good" if (ps["pf"] == float("inf") or ps["pf"] >= 1.5) else ("warn" if ps["pf"] >= 1.2 else "bad")
                pnl_cls = "profit" if ps["pnl"] >= 0 else "loss"
                cells += (
                    f"<td>{ps['trades']}</td>"
                    f"<td class='{wr_cls}'>{ps['wr']:.1f}%</td>"
                    f"<td class='{pf_cls}'>{_pf_str(ps['pf'])}</td>"
                    f"<td class='{pnl_cls}'>{ps['pnl']:+,.0f}</td>"
                )

        sc365 = r["periods"].get(365, {}).get("score", 0)
        table_rows += f"""
        <tr>
          <td style="text-align:left">{rank_badge} <strong>{r['strategy']}</strong><br>
            <small style="color:#94a3b8">{r['label']}</small></td>
          <td><span class="tag {dir_cls}">{dir_lbl}</span></td>
          <td class="score-num">{sc365:.0f}</td>
          {cells}
        </tr>"""

    # ── 方向別サマリー ────────────────────────────────────────────
    def _dir_summary(direction: str, days: int) -> tuple[float, float, float]:
        total_trades = total_wins = total_pnl = gp = gl = 0.0
        for r in rows:
            if r["direction"] != direction:
                continue
            ps = r["periods"].get(days)
            if not ps:
                continue
            total_trades += ps["trades"]
            total_wins   += ps["wins"]
            total_pnl    += ps["pnl"]
            gp           += ps["pf"] * (ps["pnl"] if ps["pnl"] > 0 else 0)
        wr = total_wins / total_trades * 100 if total_trades > 0 else 0
        return total_trades, wr, total_pnl

    dir_rows = ""
    for direction, label in [("long", "買い戦略（ロング）"), ("short", "売り戦略（ショート）")]:
        dir_cls = "tag-long" if direction == "long" else "tag-short"
        cells = ""
        for d in PERIODS:
            trades, wr, pnl = _dir_summary(direction, d)
            p_cls = "profit" if pnl >= 0 else "loss"
            wr_cls = "good" if wr >= 55 else ("warn" if wr >= 45 else "bad")
            cells += f"<td>{trades:.0f}</td><td class='{wr_cls}'>{wr:.1f}%</td><td class='{p_cls}'>{pnl:+,.0f}</td>"
        dir_rows += f"""
        <tr>
          <td style="text-align:left"><span class="tag {dir_cls}">{label}</span></td>
          {cells}
        </tr>"""

    dir_th  = "".join(f"<th colspan='3'>{d}日</th>" for d in PERIODS)
    dir_sub = "<th>取引</th><th>勝率</th><th>損益合計</th>" * len(PERIODS)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>全戦略比較レポート — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:20px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b88; }}
  .profit {{ color:#4ade80; font-weight:600; }}
  .loss   {{ color:#f87171; }}
  .good   {{ color:#4ade80; font-weight:600; }}
  .warn   {{ color:#fbbf24; }}
  .bad    {{ color:#f87171; }}
  .score-num {{ color:#38bdf8; font-weight:700; text-align:center; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-long  {{ background:#1e3a5f; color:#93c5fd; }}
  .tag-short {{ background:#4c1d1d; color:#fca5a5; }}
  .badge {{ display:inline-block; padding:1px 6px; border-radius:4px; font-size:0.75rem; font-weight:700; margin-right:4px; }}
  .gold   {{ background:#fbbf24; color:#000; }}
  .silver {{ background:#94a3b8; color:#000; }}
  .bronze {{ background:#b45309; color:#fff; }}
  .legend {{ display:flex; gap:16px; margin-bottom:12px; font-size:0.82rem; color:#94a3b8; }}
  .legend-item {{ display:flex; align-items:center; gap:6px; }}
  .dot-good   {{ width:10px; height:10px; border-radius:50%; background:#4ade80; }}
  .dot-warn   {{ width:10px; height:10px; border-radius:50%; background:#fbbf24; }}
  .dot-bad    {{ width:10px; height:10px; border-radius:50%; background:#f87171; }}
</style>
</head>
<body>
<h1>全戦略比較レポート</h1>
<p class="subtitle">
  生成日: {today} ／ 比較期間: {', '.join(str(d)+'日' for d in PERIODS)}<br>
  ロング6戦略（逆指値B + ブレイクアウト）× ショート6戦略（ショートB + ショートBRK）を横断比較
</p>

<h2>方向別サマリー（ロング vs ショート）</h2>
<table>
  <thead>
    <tr><th rowspan="2">方向</th>{dir_th}</tr>
    <tr>{dir_sub}</tr>
  </thead>
  <tbody>{dir_rows}</tbody>
</table>

<h2>全戦略ランキング（スコア順）</h2>
<div class="legend">
  <div class="legend-item"><div class="dot-good"></div> 良好（勝率≥55% / PF≥1.5）</div>
  <div class="legend-item"><div class="dot-warn"></div> 普通（勝率45-55% / PF1.2-1.5）</div>
  <div class="legend-item"><div class="dot-bad"></div> 要注意（勝率<45% / PF<1.2）</div>
</div>
<table>
  <thead>
    <tr>
      <th rowspan="2" style="text-align:left">戦略</th>
      <th rowspan="2">方向</th>
      <th rowspan="2">スコア<br><small>(365日)</small></th>
      {period_th}
    </tr>
    <tr>{period_sub}</tr>
  </thead>
  <tbody>{table_rows}</tbody>
</table>

<p style="color:#64748b; font-size:0.8rem; margin-top:8px">
  ※ スコア = 勝率×40 + PF×30 + 安定性×20 + 取引回数×10（最大100点）<br>
  ※ 損益の単位は円（FIXED_QTY=100株固定）<br>
  ※ ショート戦略の成績は相場環境（上昇局面では不利）に強く依存します
</p>

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="全戦略一括比較レポート")
    parser.add_argument("--workers",    type=int, default=_stop._DEFAULT_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"全戦略比較 開始 — {today}", flush=True)

    group_results = []

    # 各グループを並列実行
    with ThreadPoolExecutor(max_workers=4) as outer:
        fut_map = {
            outer.submit(_run_group, mod, args.workers): (mod, label, direction)
            for mod, label, direction in GROUPS
        }
        for fut in as_completed(fut_map):
            mod, label, direction = fut_map[fut]
            try:
                items = fut.result()
                n = len(items)
                print(f"  [{label}] {n}銘柄 完了", flush=True)
                group_results.append((mod, label, direction, items))
            except Exception as e:
                print(f"  [{label}] エラー: {e}", file=sys.stderr)

    # GROUPS の順序に合わせてソート
    order = {id(mod): i for i, (mod, _, _, _) in enumerate(
        sorted(group_results, key=lambda x: [id(g[0]) for g in GROUPS].index(id(x[0])))
    )}
    group_results.sort(key=lambda x: [id(g[0]) for g in GROUPS].index(id(x[0])))

    print("\nHTML生成中...", flush=True)
    html     = build_html(group_results, today)
    out_path = Path(f"compare_all_{today}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"レポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
