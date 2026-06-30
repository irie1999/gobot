"""
daytrade_report.py  ―  デイトレ 日次レポート (スイング run_signals_holdout_all 風)

2つの内容を 1 つの HTML にまとめる:
  1. 今日のシグナル … daytrade_watchlist.py の (銘柄×戦略) を最新営業日で評価し、
                      今日エントリーが出た銘柄を表示。
  2. WF検証(ホールドアウト) … wf_strategies_*_ho{30,90,180}.csv を戦略別タブで表示。
                      HO90 & HO180 の両方で holdout損益>0 を ★本命 として強調。

スイング版と同じダークテーマ・上部タブ・色分け。

使い方:
  python daytrade_report.py
  python daytrade_report.py --date 2026-06-30 --no-browser
  python daytrade_report.py --signal-days 60   # 今日シグナル判定用のロード日数
"""
from __future__ import annotations

import argparse
import csv
import importlib
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
RESULTS = Path("walkforward_results")
HOLDOUTS = [30, 90, 180]

# 戦略名 → (モジュール, 表示色クラス)
STRAT_MODULES = {
    "Donchian":     "daytrade_donchian",
    "GapReversal":  "daytrade_gap_reversal",
    "VWAP":         "daytrade_vwap_rebound",
    "RSI":          "daytrade_rsi",
    "Pivot":        "daytrade_pivot",
    "OpenMomentum": "daytrade_opening_momentum",
    "MACDBreak":    "daytrade_macd_break",
    "StochATR":     "daytrade_stoch_atr",
    "VolSurge":     "daytrade_volsurge",
}
STRATEGIES = list(STRAT_MODULES.keys())


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


# ─────────────────────────────────────────────────────────────
# WF検証データ
# ─────────────────────────────────────────────────────────────
def _load_csv(strategy: str, ho: int, date: str) -> dict:
    path = RESULTS / f"wf_strategies_{strategy}_ho{ho}_{date}.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["symbol"]] = r
    return out


def _collect_wf(strategy: str, date: str):
    data = {ho: _load_csv(strategy, ho, date) for ho in HOLDOUTS}
    if not any(data.values()):
        return []
    codes = set()
    for d in data.values():
        codes.update(d.keys())
    rows = []
    for code in codes:
        r = {"code": code}
        for ho in HOLDOUTS:
            if code in data[ho]:
                r["name"] = data[ho][code].get("name", "")
                r["price"] = _f(data[ho][code].get("latest_price"))
                break
        for ho in HOLDOUTS:
            rr = data[ho].get(code)
            r[f"ho{ho}_pnl"] = _f(rr.get("holdout_pnl")) if rr else 0.0
            r[f"ho{ho}_tr"] = _f(rr.get("holdout_trades")) if rr else 0.0
            r[f"ho{ho}_test"] = _f(rr.get("total_test_pnl")) if rr else 0.0
            r[f"ho{ho}_in"] = rr is not None
        r["robust"] = (r["ho90_pnl"] > 0 and r["ho90_tr"] >= 3
                       and r["ho180_pnl"] > 0 and r["ho180_tr"] >= 3)
        r["score"] = r["ho180_pnl"] + r["ho90_pnl"]
        rows.append(r)
    rows.sort(key=lambda x: (-int(x["robust"]), -x["score"]))
    return rows


def _wf_table(strategy: str, rows: list) -> str:
    rob = sum(1 for r in rows if r["robust"])
    h = [f'<h2>{strategy} <span class="badge">候補{len(rows)} / ★本命{rob}</span></h2>',
         '<table><thead><tr><th>★</th><th>コード</th><th>銘柄</th><th>株価</th>']
    for ho in HOLDOUTS:
        h.append(f'<th colspan="2" class="cen">HO{ho}日</th>')
    h.append('</tr><tr><th></th><th></th><th></th><th></th>')
    for ho in HOLDOUTS:
        h.append('<th class="sub">TEST</th><th class="sub">HO損益(取引)</th>')
    h.append('</tr></thead><tbody>')
    for r in rows:
        cls = ' class="robust"' if r["robust"] else ""
        h.append(f'<tr{cls}><td class="star">{"★" if r["robust"] else ""}</td>'
                 f'<td>{r["code"]}</td><td class="nm">{r.get("name","")[:18]}</td>'
                 f'<td>{r.get("price",0):,.0f}</td>')
        for ho in HOLDOUTS:
            test = f'{r[f"ho{ho}_test"]:+,.0f}' if r[f"ho{ho}_in"] else "―"
            h.append(f'<td class="test">{test}</td>')
            pnl, tr = r[f"ho{ho}_pnl"], r[f"ho{ho}_tr"]
            if tr <= 0:
                h.append('<td class="zero">―</td>')
            else:
                c = "pos" if pnl > 0 else ("neg" if pnl < 0 else "zero")
                h.append(f'<td class="{c}">{pnl:+,.0f}<span class="sub">{int(tr)}</span></td>')
        h.append('</tr>')
    h.append('</tbody></table>')
    return "".join(h)


# ─────────────────────────────────────────────────────────────
# 今日のシグナル (WATCHLIST × 戦略 を最新営業日で評価)
# ─────────────────────────────────────────────────────────────
def _today_signals(signal_days: int):
    try:
        wl = importlib.import_module("daytrade_watchlist").WATCHLIST
    except Exception:
        return None, "daytrade_watchlist.py が無いか壊れています。build_watchlist_wf.py を先に実行してください。"
    try:
        from daytrade_data import load_intraday
    except Exception as e:
        return None, f"daytrade_data 読込失敗: {e}"

    sigs = []
    for strat, syms in wl.items():
        mod_name = STRAT_MODULES.get(strat)
        if not mod_name:
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for code, name in syms:
            sym = code if code.endswith(".T") else f"{code}.T"
            try:
                df = load_intraday(sym, days=signal_days, source="local")
                if df is None or df.empty:
                    continue
                last_day = df.index[-1].date()
                try:
                    res = mod.backtest_symbol(sym, name, df)
                except TypeError:
                    res = mod.backtest_symbol(sym, name, df, 600000, 6000)
                for t in (res or {}).get("trades", []):
                    ed = t.get("entry_dt")
                    if ed is not None and ed.date() == last_day:
                        sigs.append({
                            "strat": strat, "code": code, "name": name,
                            "date": last_day, "entry_t": ed.strftime("%H:%M"),
                            "entry": t.get("entry_p"), "exit": t.get("exit_p"),
                            "pnl": t.get("pnl", 0), "reason": t.get("reason", ""),
                        })
            except Exception:
                continue
    sigs.sort(key=lambda s: -_f(s.get("pnl")))
    return sigs, None


def _signals_html(sigs, err) -> str:
    if err:
        return f'<h2>今日のシグナル</h2><p class="warn">{err}</p>'
    if not sigs:
        return ('<h2>今日のシグナル</h2>'
                '<p class="badge">最新営業日にエントリーした銘柄はありません'
                '（引け後に実行すると当日約定が反映されます）。</p>')
    day = sigs[0]["date"]
    h = [f'<h2>今日のシグナル <span class="badge">{day} / {len(sigs)}件</span></h2>',
         '<table><thead><tr><th>戦略</th><th>コード</th><th>銘柄</th>'
         '<th>時刻</th><th>エントリー</th><th>決済</th><th>損益</th><th>理由</th>'
         '</tr></thead><tbody>']
    for s in sigs:
        pnl = _f(s["pnl"])
        c = "pos" if pnl > 0 else ("neg" if pnl < 0 else "zero")
        h.append(f'<tr><td class="nm">{s["strat"]}</td><td>{s["code"]}</td>'
                 f'<td class="nm">{s["name"][:16]}</td><td>{s["entry_t"]}</td>'
                 f'<td>{_f(s["entry"]):,.0f}</td><td>{_f(s["exit"]):,.0f}</td>'
                 f'<td class="{c}">{pnl:+,.0f}</td><td class="nm">{s["reason"]}</td></tr>')
    h.append('</tbody></table>')
    return "".join(h)


CSS = """
body{margin:0;padding:0;background:#0f172a;color:#94a3b8;font-family:sans-serif}
.wrap{padding:22px 28px}
h1{color:#e2e8f0;font-size:19px} h2{color:#e2e8f0;font-size:15px;margin:16px 0 6px}
.badge{font-size:11px;color:#7dd3fc;font-weight:normal}
.warn{color:#fbbf24}
.nav{display:flex;gap:0;align-items:flex-end;border-bottom:2px solid #1e293b;
     background:#0f172a;padding:8px 18px 0;flex-wrap:wrap}
.tab{padding:10px 22px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
     color:#94a3b8;cursor:pointer;font-size:0.98rem;font-weight:600;margin-right:3px}
.tab:hover:not(.active){background:#263349;color:#e2e8f0}
.tab.active{background:#0f172a;border-bottom:2px solid #fbbf24;color:#fbbf24}
.tab .tn{background:#0006;border-radius:8px;padding:0 6px;margin-left:5px;font-size:11px}
.sep{width:1px;background:#334155;margin:8px 10px 4px;align-self:stretch}
.pane{display:none} .pane.active{display:block}
table{border-collapse:collapse;font-size:12px;width:100%;margin-top:6px}
th,td{border:1px solid #1e293b;padding:4px 8px;text-align:right}
th{background:#1e293b;color:#cbd5e1} td.nm,td.star{text-align:left}
.cen{text-align:center;background:#172033}
.sub{font-size:10px;color:#64748b;font-weight:normal;margin-left:3px}
.pos{color:#34d399} .neg{color:#f87171} .zero{color:#475569} .test{color:#7d93b0}
tr.robust{background:#0f2a1c} tr.robust td.star{color:#fbbf24;font-weight:bold}
tr:hover{background:#172033}
"""

JS = """
function show(id, el){
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
}
"""


def build_html(date: str, signal_days: int):
    # WF検証
    wf = {}
    for s in STRATEGIES:
        rows = _collect_wf(s, date)
        if rows:
            wf[s] = rows
    # 今日のシグナル
    sigs, err = _today_signals(signal_days)

    tabs, panes = [], []
    # シグナルタブ (先頭)
    sig_n = len(sigs) if sigs else 0
    tabs.append(f'<button class="tab active" onclick="show(\'sig\',this)">'
                f'🚀 今日のシグナル <span class="tn">{sig_n}</span></button>')
    panes.append(f'<div id="sig" class="pane active">{_signals_html(sigs, err)}</div>')
    tabs.append('<span class="sep"></span>')
    # WF検証タブ
    summary = {}
    for s, rows in wf.items():
        rob = sum(1 for r in rows if r["robust"])
        summary[s] = (len(rows), rob)
        tabs.append(f'<button class="tab" onclick="show(\'{s}\',this)">'
                    f'{s} <span class="tn">{rob}</span></button>')
        panes.append(f'<div id="{s}" class="pane">{_wf_table(s, rows)}</div>')

    total_rob = sum(r for _, r in summary.values())
    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>デイトレ日次レポート {date}</title><style>{CSS}</style></head><body>
<div class="nav">{''.join(tabs)}</div>
<div class="wrap">
<h1>デイトレ日次レポート <span class="badge">{date} / WF★本命 計{total_rob}</span></h1>
<p class="badge">🚀今日のシグナル=WATCHLIST銘柄が最新営業日に出したエントリー。
WF検証の★本命=HO90&HO180両方で未使用OOS損益>0(取引≥3)。</p>
{''.join(panes)}
</div>
<script>{JS}</script></body></html>"""
    return html, summary, sig_n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).date().isoformat())
    ap.add_argument("--signal-days", type=int, default=60)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    html, summary, sig_n = build_html(args.date, args.signal_days)
    out = Path(f"daytrade_report_{args.date}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML 生成: {out.resolve()}")
    print(f"  今日のシグナル: {sig_n}件")
    print("  WF検証 (候補/★本命):",
          {s: f"{n}/{r}" for s, (n, r) in summary.items()})
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
