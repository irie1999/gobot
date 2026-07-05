"""
report_wf_html.py  ―  デイトレ WF(ホールドアウト)検証結果を HTML で確認する。

scan_wf_strategies.py が出力した
  walkforward_results/wf_strategies_<戦略>_ho{30,90,180}_<date>.csv
を読み、戦略別タブ・色分けの HTML を生成する (スイング版レポートと同様)。

各銘柄について HO30/90/180 の『ホールドアウト(未使用OOS)実績』を横並びで表示。
HO90 & HO180 の両方で holdout損益>0 の銘柄を ★本命 として強調。

使い方:
  python report_wf_html.py
  python report_wf_html.py --date 2026-06-30
  python report_wf_html.py --no-browser
出力: daytrade_wf_report_<date>.html
"""
from __future__ import annotations

import argparse
import csv
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
RESULTS = Path("walkforward_results")
STRATEGIES = ["Donchian", "GapReversal", "VWAP", "RSI", "Pivot",
              "OpenMomentum", "MACDBreak", "StochATR", "VolSurge"]
HOLDOUTS = [30, 90, 180]


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _load(strategy: str, ho: int, date: str) -> dict:
    path = RESULTS / f"wf_strategies_{strategy}_ho{ho}_{date}.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["symbol"]] = r
    return out


def _pnl_cell(pnl: float, trades: float) -> str:
    if trades <= 0:
        return '<td class="zero">―</td>'
    cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "zero")
    return f'<td class="{cls}">{pnl:+,.0f}<span class="sub">{int(trades)}件</span></td>'


def _collect(strategy: str, date: str):
    data = {ho: _load(strategy, ho, date) for ho in HOLDOUTS}
    if not any(data.values()):
        return []
    codes = set()
    for d in data.values():
        codes.update(d.keys())
    rows = []
    for code in codes:
        r = {"code": code}
        # 名前/株価は見つかった最初のHOから
        for ho in HOLDOUTS:
            if code in data[ho]:
                r["name"] = data[ho][code].get("name", "")
                r["price"] = _f(data[ho][code].get("latest_price"))
                break
        for ho in HOLDOUTS:
            rr = data[ho].get(code)
            r[f"ho{ho}_pnl"] = _f(rr.get("holdout_pnl")) if rr else 0.0
            r[f"ho{ho}_tr"] = _f(rr.get("holdout_trades")) if rr else 0.0
            r[f"ho{ho}_pf"] = _f(rr.get("holdout_pf")) if rr else 0.0
            r[f"ho{ho}_wr"] = _f(rr.get("holdout_wr")) if rr else 0.0
            r[f"ho{ho}_test"] = _f(rr.get("total_test_pnl")) if rr else 0.0
            r[f"ho{ho}_in"] = rr is not None
        # 本命判定: HO90 & HO180 で holdout損益>0 かつ 取引>=3
        r["robust"] = (r["ho90_pnl"] > 0 and r["ho90_tr"] >= 3
                       and r["ho180_pnl"] > 0 and r["ho180_tr"] >= 3)
        r["score"] = r["ho180_pnl"] + r["ho90_pnl"]
        rows.append(r)
    rows.sort(key=lambda x: (-int(x["robust"]), -x["score"]))
    return rows


def _strategy_table(strategy: str, rows: list) -> str:
    robust_n = sum(1 for r in rows if r["robust"])
    h = [f'<h2>{strategy} <span class="badge">候補{len(rows)} / ★本命{robust_n}</span></h2>']
    h.append('<table><thead><tr>'
             '<th>★</th><th>コード</th><th>銘柄</th><th>株価</th>')
    for ho in HOLDOUTS:
        h.append(f'<th colspan="2" class="hohdr">HO{ho}日<br>'
                 f'<span class="sub">選定除外→OOS</span></th>')
    h.append('</tr><tr><th></th><th></th><th></th><th></th>')
    for ho in HOLDOUTS:
        h.append('<th class="sub">TEST損益</th><th class="sub">HO損益(取引)</th>')
    h.append('</tr></thead><tbody>')
    for r in rows:
        star = "★" if r["robust"] else ""
        cls = ' class="robust"' if r["robust"] else ""
        h.append(f'<tr{cls}><td class="star">{star}</td>'
                 f'<td>{r["code"]}</td><td class="nm">{r.get("name","")[:18]}</td>'
                 f'<td>{r.get("price",0):,.0f}</td>')
        for ho in HOLDOUTS:
            test = r[f"ho{ho}_test"]
            test_s = f'{test:+,.0f}' if r[f"ho{ho}_in"] else "―"
            h.append(f'<td class="test">{test_s}</td>')
            h.append(_pnl_cell(r[f"ho{ho}_pnl"], r[f"ho{ho}_tr"]))
        h.append('</tr>')
    h.append('</tbody></table>')
    return "".join(h)


def build_html(date: str) -> tuple[str, dict]:
    per = {}
    for s in STRATEGIES:
        rows = _collect(s, date)
        if rows:
            per[s] = rows
    # タブ
    tabs, panes = [], []
    summary = {}
    for i, (s, rows) in enumerate(per.items()):
        rob = sum(1 for r in rows if r["robust"])
        summary[s] = (len(rows), rob)
        act = " active" if i == 0 else ""
        tabs.append(f'<button class="tab{act}" onclick="show(\'{s}\')">'
                    f'{s} <span class="tn">{rob}</span></button>')
        panes.append(f'<div id="{s}" class="pane{act}">{_strategy_table(s, rows)}</div>')

    sum_rows = "".join(
        f'<tr><td>{s}</td><td>{n}</td><td class="pos">{rob}</td></tr>'
        for s, (n, rob) in summary.items())
    total_rob = sum(rob for _, rob in summary.values())

    css = """
    body{font-family:'Meiryo',sans-serif;margin:16px;background:#0f1115;color:#e6e6e6}
    h1{font-size:18px} h2{font-size:15px;margin:14px 0 6px}
    .badge{font-size:11px;color:#9ad;font-weight:normal}
    .tab{background:#1b1f27;color:#bbb;border:0;padding:8px 14px;margin:2px;
         cursor:pointer;border-radius:6px;font-size:13px}
    .tab.active{background:#2d6cdf;color:#fff}
    .tn{background:#0008;border-radius:8px;padding:0 6px;margin-left:4px;font-size:11px}
    .pane{display:none;margin-top:10px} .pane.active{display:block}
    table{border-collapse:collapse;font-size:12px;width:100%}
    th,td{border:1px solid #2a2f3a;padding:3px 7px;text-align:right}
    th{background:#1b1f27;color:#cdd} td.nm,td.star{text-align:left}
    .hohdr{text-align:center;background:#222b3a}
    .sub{font-size:10px;color:#89a;font-weight:normal;margin-left:3px}
    .pos{color:#5fd38a} .neg{color:#e8736a} .zero{color:#667}
    .test{color:#9ab}
    tr.robust{background:#173a26} tr.robust td.star{color:#ffd24a;font-weight:bold}
    tr:hover{background:#222938}
    .summary{margin:8px 0 14px;font-size:12px}
    .summary td{border:1px solid #2a2f3a;padding:3px 10px}
    """
    js = """
    function show(id){
      document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
      document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      event.target.closest('.tab').classList.add('active');
    }
    """
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>デイトレ WF検証 {date}</title><style>{css}</style></head><body>
<h1>デイトレ WalkForward(ホールドアウト)検証レポート <span class="badge">{date}</span></h1>
<p class="badge">★本命 = HO90 & HO180 の両方で holdout損益>0(取引≥3)。
HO損益 = 選定に使っていない直近N日(真の未使用OOS)の実績。これがプラスなら本物。</p>
<table class="summary"><thead><tr><th>戦略</th><th>候補</th><th>★本命</th></tr></thead>
<tbody>{sum_rows}<tr><td><b>合計★本命</b></td><td></td><td class="pos"><b>{total_rob}</b></td></tr></tbody></table>
<div>{''.join(tabs)}</div>
{''.join(panes)}
<script>{js}</script></body></html>"""
    return html, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).date().isoformat())
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    html, summary = build_html(args.date)
    if not summary:
        print(f"[ERROR] {args.date} の wf_strategies_*.csv が見つかりません。"
              f"先に scan_wf_strategies.py / run_overnight.ps1 を実行してください。")
        return
    out = Path(f"daytrade_wf_report_{args.date}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML 生成: {out.resolve()}")
    print("  戦略別 (候補/★本命):",
          {s: f"{n}/{r}" for s, (n, r) in summary.items()})
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
