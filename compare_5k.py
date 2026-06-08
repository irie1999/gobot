"""
compare_5k.py  ―  5,000円以下スクリプト × モード横断比較レポート
================================================================
5,000円以下の銘柄で構成された4スクリプトを
conservative / aggressive の2モードで比較し1つのHTMLに表示する。

比較対象:
  1. 既存版       (run_signals.py)
  2. WF 2026-05-19 (run_signals_wf.py)
  3. WF+既存 統合  (run_signals_merged.py)
  4. WF+既存 積極  (run_signals_aggressive.py)

【使い方】
  python compare_5k.py                     # 全期間(365日)・本日シグナル
  python compare_5k.py --days 90           # 直近90日
  python compare_5k.py --date 2026-05-19   # 指定日シグナル
  python compare_5k.py --no-browser        # HTML生成のみ

【出力】
  compare_5k_YYYY-MM-DD.html
    タブ1: Conservative（目標+9% / 損切-4.5%）
    タブ2: Aggressive  （目標+6% / 損切-4.5%）
    タブ3: 本日シグナル比較
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import webbrowser
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from _signal_funds import filter_items

JST = timezone(timedelta(hours=9))

# ── WATCHLIST取得 ─────────────────────────────────────────────────────────────

def _get_orig_watchlists():
    import check_signals_stop as s, check_signals_breakout as b
    return list(s.WATCHLIST), list(b.WATCHLIST)


def _get_wf_watchlists():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_rsw", "run_signals_wf.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return list(m._STOP_WATCHLIST), list(m._BRK_WATCHLIST)


def _get_merged_watchlists():
    import importlib.util, check_signals_stop as s, check_signals_breakout as b
    spec = importlib.util.spec_from_file_location("_rsm", "run_signals_merged.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    def _merge(wf, orig):
        seen, out = set(), []
        for t in list(wf) + list(orig):
            key = (t[0], t[2])
            if key not in seen:
                seen.add(key); out.append(tuple(t))
        return out
    return _merge(m._WF_STOP, s.WATCHLIST), _merge(m._WF_BRK, b.WATCHLIST)


def _get_aggressive_watchlists():
    import importlib.util
    orig = os.environ.get("TRADING_MODE", "conservative")
    os.environ["TRADING_MODE"] = "aggressive"
    spec = importlib.util.spec_from_file_location("_rsa", "run_signals_aggressive.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    os.environ["TRADING_MODE"] = orig
    return list(m.STOP_WATCHLIST), list(m.BRK_WATCHLIST)


# ── バックテスト ──────────────────────────────────────────────────────────────

def _run_wl(mod, watchlist, sig_date, workers: int) -> list[dict]:
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in watchlist}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r: all_items.append(r)
            except Exception:
                pass
    order = {(s, st): i for i, (s, _, st) in enumerate(watchlist)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))
    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)
    return all_items


def run_config(label: str, stop_wl, brk_wl, stop_mod, brk_mod,
               sig_date, workers: int) -> dict:
    from backtest_limit_entry import compute_period_result

    stop_mod.WATCHLIST = stop_wl
    brk_mod.WATCHLIST  = brk_wl

    items_stop = filter_items(_run_wl(stop_mod, stop_wl, sig_date, workers))
    items_brk  = filter_items(_run_wl(brk_mod,  brk_wl,  sig_date, workers))
    all_items  = items_stop + items_brk

    period_summary = {}
    for d in [30, 90, 180, 365]:
        all_trades = []
        for it in all_items:
            pr = compute_period_result(it, d)
            if pr:
                all_trades.extend(pr.get("trade_log", []))
        n          = len(all_trades)
        wins       = sum(1 for t in all_trades if t["pnl"] > 0)
        pnl        = sum(t["pnl"] for t in all_trades)
        gross_win  = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
        avg_hold   = (sum(t.get("hold_days", 0) for t in all_trades) / n) if n > 0 else 0.0
        wr  = wins / n * 100 if n > 0 else 0.0
        pf  = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        period_summary[d] = dict(trades=n, wr=wr, pf=pf, pnl=pnl, avg_hold=avg_hold)

    signals = [
        dict(symbol=it["symbol"], name=it["name"], strategy=it["strategy"],
             score=it.get("score", 0), rank=it.get("rank", "-"),
             sig=it["today_sig"])
        for it in all_items if it.get("today_sig")
    ]
    signals.sort(key=lambda x: x["score"], reverse=True)

    return dict(label=label, n_stop=len(stop_wl), n_brk=len(brk_wl),
                period_summary=period_summary, signals=signals)


# ── HTML ─────────────────────────────────────────────────────────────────────

COLORS       = ["#38bdf8", "#34d399", "#fb923c", "#f472b6"]
LABELS_SHORT = ["既存版", "WF-2026-05-19", "WF+既存統合", "WF+既存積極"]


def _pnl_color(v):
    return "#34d399" if v > 0 else "#f87171" if v < 0 else "#94a3b8"


def _fmt_pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _matrix_section(results: list[dict], mode_label: str, mode_color: str) -> str:
    rows = ""
    for i, r in enumerate(results):
        c    = COLORS[i]
        cells = ""
        for d in [30, 90, 180, 365]:
            p   = r["period_summary"].get(d, {})
            t   = p.get("trades", 0)
            wr  = p.get("wr", 0.0)
            pf  = p.get("pf", 0.0)
            pnl = p.get("pnl", 0.0)
            ah  = p.get("avg_hold", 0.0)
            pc  = _pnl_color(pnl)
            if t == 0:
                cells += '<td style="color:#4b5563;text-align:center">—</td>'
            else:
                cells += (
                    f'<td style="text-align:center;padding:8px 6px">'
                    f'<div style="font-size:11px;color:#64748b">{t}取引</div>'
                    f'<div style="font-weight:700">{wr:.1f}%</div>'
                    f'<div style="font-size:11px">PF {_fmt_pf(pf)}</div>'
                    f'<div style="color:{pc};font-weight:700;font-size:12px">{pnl:+,.0f}円</div>'
                    f'<div style="font-size:10px;color:#475569">平均{ah:.1f}日</div>'
                    f'</td>'
                )
        rows += (
            f'<tr>'
            f'<td style="padding:10px 12px;white-space:nowrap">'
            f'  <span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'  background:{c};margin-right:6px"></span>'
            f'  <strong style="color:{c}">{r["label"]}</strong>'
            f'  <div style="font-size:11px;color:#4b5563;margin-top:2px">'
            f'  逆指値B {r["n_stop"]}銘柄 / ブレイクアウト {r["n_brk"]}銘柄</div>'
            f'</td>'
            f'{cells}'
            f'</tr>\n'
        )

    return f"""
<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
  <h2 style="color:#e2e8f0;font-size:16px;margin:0">📊 成績比較マトリクス</h2>
  <span style="background:{mode_color}22;color:{mode_color};border:1px solid {mode_color}44;
    padding:3px 12px;border-radius:12px;font-size:12px;font-weight:700">{mode_label}</span>
</div>
<p style="font-size:11px;color:#4b5563;margin-bottom:12px">
  各セル: 取引数 / 勝率 / PF / 損益 / 平均保有日数
</p>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead>
<tr style="background:#1a1e2e">
  <th style="padding:10px 12px;text-align:left;color:#64748b">スクリプト</th>
  <th style="padding:10px 8px;color:#64748b;text-align:center">30日</th>
  <th style="padding:10px 8px;color:#64748b;text-align:center">90日</th>
  <th style="padding:10px 8px;color:#64748b;text-align:center">180日</th>
  <th style="padding:10px 8px;color:#64748b;text-align:center">365日</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
</div>"""


def _signals_section(con_results: list[dict], agg_results: list[dict],
                     date_label: str) -> str:
    strat_colors = {
        "MACD": "#38bdf8", "A7": "#818cf8", "RSI2": "#fb923c",
        "DON":  "#34d399", "VOL": "#f472b6", "MOM":  "#facc15",
    }

    # (symbol, strategy) → {scripts_con, scripts_agg, sig, ...}
    sig_map: dict[tuple, dict] = {}
    for i, r in enumerate(con_results):
        for s in r["signals"]:
            key = (s["symbol"], s["strategy"])
            if key not in sig_map:
                sig_map[key] = dict(symbol=s["symbol"], name=s["name"],
                                    strategy=s["strategy"], score=s["score"],
                                    rank=s["rank"], sig=s["sig"],
                                    scripts_con=[], scripts_agg=[])
            sig_map[key]["scripts_con"].append(i)

    for i, r in enumerate(agg_results):
        for s in r["signals"]:
            key = (s["symbol"], s["strategy"])
            if key not in sig_map:
                sig_map[key] = dict(symbol=s["symbol"], name=s["name"],
                                    strategy=s["strategy"], score=s["score"],
                                    rank=s["rank"], sig=s["sig"],
                                    scripts_con=[], scripts_agg=[])
            sig_map[key]["scripts_agg"].append(i)

    if not sig_map:
        return '<p style="color:#64748b;padding:16px">本日のシグナルはありません。</p>'

    items = sorted(sig_map.values(), key=lambda x: x["score"], reverse=True)
    rows  = ""
    for it in items:
        sig  = it["sig"]
        sc   = strat_colors.get(it["strategy"], "#94a3b8")
        con_badges = "".join(
            f'<span style="display:inline-block;padding:1px 7px;border-radius:8px;'
            f'font-size:10px;font-weight:600;background:{COLORS[idx]}22;'
            f'color:{COLORS[idx]};border:1px solid {COLORS[idx]}44;margin:1px">'
            f'{LABELS_SHORT[idx]}</span>'
            for idx in it["scripts_con"]
        ) or '<span style="color:#4b5563;font-size:11px">—</span>'
        agg_badges = "".join(
            f'<span style="display:inline-block;padding:1px 7px;border-radius:8px;'
            f'font-size:10px;font-weight:600;background:{COLORS[idx]}22;'
            f'color:{COLORS[idx]};border:1px solid {COLORS[idx]}44;margin:1px">'
            f'{LABELS_SHORT[idx]}</span>'
            for idx in it["scripts_agg"]
        ) or '<span style="color:#4b5563;font-size:11px">—</span>'
        rows += (
            f'<tr style="border-bottom:1px solid #1e2235">'
            f'<td style="padding:9px 8px;white-space:nowrap">'
            f'  <div style="font-weight:600">{it["symbol"]}</div>'
            f'  <div style="font-size:11px;color:#64748b">{it["name"]}</div>'
            f'</td>'
            f'<td style="padding:9px 8px;text-align:center">'
            f'  <span style="background:{sc}22;color:{sc};padding:2px 8px;'
            f'  border-radius:4px;font-size:12px;font-weight:600">{it["strategy"]}</span>'
            f'</td>'
            f'<td style="padding:9px 8px;text-align:center;font-weight:700">'
            f'  {it["rank"]}{it["score"]}点</td>'
            f'<td style="padding:9px 8px;text-align:right">'
            f'  {sig["order_price"]:,.0f}<div style="font-size:10px;color:#4b5563">逆指値</div></td>'
            f'<td style="padding:9px 8px;text-align:right;color:#f87171">'
            f'  {sig["stop_price"]:,.0f}<div style="font-size:10px;color:#4b5563">損切</div></td>'
            f'<td style="padding:9px 8px;text-align:right;color:#34d399">'
            f'  {sig["target_price"]:,.0f}<div style="font-size:10px;color:#4b5563">目標</div></td>'
            f'<td style="padding:9px 8px;font-size:12px">'
            f'  <div style="margin-bottom:3px"><span style="color:#94a3b8;font-size:10px">CON▶</span> {con_badges}</div>'
            f'  <div><span style="color:#94a3b8;font-size:10px">AGG▶</span> {agg_badges}</div>'
            f'</td>'
            f'</tr>\n'
        )

    return f"""
<h2 style="color:#e2e8f0;font-size:16px;margin-bottom:6px">
  🔔 本日シグナル比較 ({date_label})
</h2>
<p style="font-size:11px;color:#4b5563;margin-bottom:14px">
  CON = conservative / AGG = aggressive で出現したスクリプト
</p>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead>
<tr style="background:#1a1e2e">
  <th style="padding:9px 8px;text-align:left;color:#64748b">銘柄</th>
  <th style="padding:9px 8px;text-align:center;color:#64748b">戦略</th>
  <th style="padding:9px 8px;text-align:center;color:#64748b">スコア</th>
  <th style="padding:9px 8px;text-align:right;color:#64748b">逆指値</th>
  <th style="padding:9px 8px;text-align:right;color:#64748b">損切</th>
  <th style="padding:9px 8px;text-align:right;color:#64748b">目標</th>
  <th style="padding:9px 8px;color:#64748b">出現スクリプト</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
</div>"""


def build_html(con_results: list[dict], agg_results: list[dict],
               date_label: str) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:18px;font-size:12px">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:{COLORS[i]};flex-shrink:0"></span>'
        f'<span style="color:{COLORS[i]}">{r["label"]}</span>'
        f'<span style="color:#4b5563;font-size:11px">({r["n_stop"]+r["n_brk"]}銘柄)</span>'
        f'</span>'
        for i, r in enumerate(con_results)
    )

    params_table = """
<table style="border-collapse:collapse;font-size:12px;margin-bottom:0">
<tr style="background:#1a1e2e">
  <th style="padding:6px 14px;color:#64748b;text-align:left">モード</th>
  <th style="padding:6px 14px;color:#64748b">損切</th>
  <th style="padding:6px 14px;color:#64748b">目標</th>
  <th style="padding:6px 14px;color:#64748b">R比率</th>
  <th style="padding:6px 14px;color:#64748b">特徴</th>
</tr>
<tr>
  <td style="padding:7px 14px;color:#94a3b8">Conservative</td>
  <td style="padding:7px 14px;color:#f87171;text-align:center">−4.5%</td>
  <td style="padding:7px 14px;color:#34d399;text-align:center">+9.0%</td>
  <td style="padding:7px 14px;text-align:center">2R</td>
  <td style="padding:7px 14px;color:#64748b">高PF・長保有・安定志向</td>
</tr>
<tr style="background:#12151f">
  <td style="padding:7px 14px;color:#fb923c">Aggressive</td>
  <td style="padding:7px 14px;color:#f87171;text-align:center">−4.5%</td>
  <td style="padding:7px 14px;color:#34d399;text-align:center">+6.0%</td>
  <td style="padding:7px 14px;text-align:center">1.33R</td>
  <td style="padding:7px 14px;color:#64748b">高回転・短保有・利確優先</td>
</tr>
</table>"""

    con_matrix = _matrix_section(con_results, "Conservative  目標+9% / 損切−4.5%", "#38bdf8")
    agg_matrix = _matrix_section(agg_results, "Aggressive  目標+6% / 損切−4.5%", "#fb923c")
    sigs       = _signals_section(con_results, agg_results, date_label)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>5,000円以下 モード比較 — {today_str}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0f1117; color:#dde1ec;
  font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif; padding:24px; }}
h1 {{ font-size:20px; color:#e2e8f0; margin-bottom:6px; }}
.tab-nav {{ display:flex; gap:0; background:#090b14; padding:10px 16px 0;
  border-bottom:2px solid #252840; margin-bottom:0; flex-wrap:wrap; }}
.tab-btn {{ background:#16192a; color:#94a3b8; border:1px solid #252840;
  border-bottom:none; padding:9px 22px; margin-right:4px;
  border-radius:6px 6px 0 0; cursor:pointer; font-size:13px;
  font-family:inherit; transition:background .15s,color .15s; }}
.tab-btn:hover {{ background:#1e2235; color:#dde1ec; }}
.tab-btn.active {{ background:#0f1117; color:#38bdf8;
  border-color:#38bdf8; border-bottom-color:#0f1117; }}
.tab-pane {{ padding:24px; background:#0f1117; min-height:60vh; }}
.card {{ background:#12151f; border:1px solid #1e2235; border-radius:10px;
  padding:20px; margin-bottom:20px; }}
table tr:nth-child(even) {{ background:#12151f; }}
table tr:hover {{ background:#1a1e2e; }}
table td, table th {{ border-bottom:1px solid #1e2235; }}
</style>
</head>
<body>
<h1>5,000円以下 スクリプト × モード 比較</h1>
<p style="color:#64748b;font-size:13px;margin:6px 0 16px">
  生成日: {today_str} ／ シグナル確認日: {date_label}
</p>
<div style="margin-bottom:18px">{legend}</div>

<div class="card">{params_table}</div>

<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">
    📊 Conservative（目標+9%）
  </button>
  <button class="tab-btn" onclick="switchTab(1)">
    ⚡ Aggressive（目標+6%・高回転）
  </button>
  <button class="tab-btn" onclick="switchTab(2)">
    🔔 本日シグナル
  </button>
</div>

<div id="tc0" class="tab-pane">
  <div class="card">{con_matrix}</div>
</div>
<div id="tc1" class="tab-pane" style="display:none">
  <div class="card">{agg_matrix}</div>
</div>
<div id="tc2" class="tab-pane" style="display:none">
  <div class="card">{sigs}</div>
</div>

<script>
function switchTab(n) {{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{
    b.classList.toggle('active', i===n);
  }});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{
    t.style.display = i===n ? 'block' : 'none';
  }});
}}
</script>
</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="5,000円以下スクリプト × モード比較")
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--date",       type=str, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workers",    type=int, default=4)
    args = parser.parse_args()

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr); sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"

    print("WATCHLISTを読み込み中...")
    orig_sw, orig_bw = _get_orig_watchlists()
    wf_sw,   wf_bw   = _get_wf_watchlists()
    mgd_sw,  mgd_bw  = _get_merged_watchlists()
    agg_sw,  agg_bw  = _get_aggressive_watchlists()

    watchlist_configs = [
        ("既存版 (run_signals)",              orig_sw, orig_bw),
        ("WF 2026-05-19 (run_signals_wf)",    wf_sw,   wf_bw),
        ("WF+既存 統合 (run_signals_merged)",  mgd_sw,  mgd_bw),
        ("WF+既存 積極 (run_signals_aggressive)", agg_sw, agg_bw),
    ]

    con_results, agg_results = [], []

    for mode, result_list in [("conservative", con_results), ("aggressive", agg_results)]:
        mode_label = "Conservative" if mode == "conservative" else "Aggressive"
        print(f"\n{'='*50}")
        print(f"  {mode_label} モード実行中")
        print(f"{'='*50}")
        os.environ["TRADING_MODE"] = mode
        importlib.reload(_stop)
        importlib.reload(_brk)

        for label, sw, bw in watchlist_configs:
            print(f"  [{label}] ({len(sw)+len(bw)}銘柄)")
            r = run_config(label, sw, bw, _stop, _brk, sig_date, args.workers)
            ps = r["period_summary"].get(args.days, {})
            ah = ps.get("avg_hold", 0.0)
            print(f"    {args.days}日: {ps.get('trades',0)}取引 / "
                  f"勝率{ps.get('wr',0):.1f}% / PF{_fmt_pf(ps.get('pf',0))} / "
                  f"{ps.get('pnl',0):+,.0f}円 / 平均{ah:.1f}日保有  "
                  f"シグナル{len(r['signals'])}件")
            result_list.append(r)

    os.environ["TRADING_MODE"] = "conservative"

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    html = build_html(con_results, agg_results, date_label)
    out  = Path(f"compare_5k_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out}")
    if not args.no_browser:
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
