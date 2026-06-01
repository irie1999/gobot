"""
score_sizing_sim.py — スコア集中ポジションサイジング シミュレーション

「スコアが高い銘柄に集中した場合、過去の成績はどう変わったか」を検証する。
フィルター戦略(低スコア除外)とサイズアップ戦略(高スコアに多めの株数)を比較。

Usage:
    python score_sizing_sim.py                  # 365日 HTML生成 & ブラウザ表示
    python score_sizing_sim.py --days 180       # 直近180日
    python score_sizing_sim.py --no-browser     # HTML生成のみ
    python score_sizing_sim.py --workers 8      # 並列数
    python score_sizing_sim.py --aggressive     # aggressiveモード
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

# ── 環境設定 ──────────────────────────────────────────────────────────────────
_IS_AGG = "--aggressive" in __import__("sys").argv
os.environ["TRADING_MODE"] = "aggressive" if _IS_AGG else "conservative"

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import WORKERS as _DEF_WORKERS, JST, INITIAL_CASH, FIXED_QTY
from check_signals_stop import calc_recommend_score

_TODAY = datetime.now(JST).date()

# ── シナリオ定義 ───────────────────────────────────────────────────────────────
# (label, color, fn(score)->倍率  0=スキップ)
def _sc(threshold: int, top_mult: float = 1.0, mid_mult: float = 0.0) -> object:
    """スコア閾値ベースの倍率関数ファクトリ。"""
    def fn(s: int) -> float:
        if s >= threshold:
            return top_mult
        return mid_mult
    return fn

def _tiered(t90: float, t80: float, t60: float) -> object:
    def fn(s: int) -> float:
        if s >= 90: return t90
        if s >= 80: return t80
        if s >= 60: return t60
        return 0.0
    return fn

SCENARIOS: list[tuple[str, str, object]] = [
    ("① 全取引 100株 (ベースライン)",          "#94a3b8", lambda s: 1.0),
    ("② スコア60以上 100株",                   "#60a5fa", _sc(60, 1.0, 0.0)),
    ("③ スコア70以上 100株",                   "#34d399", _sc(70, 1.0, 0.0)),
    ("④ スコア80以上 100株",                   "#f59e0b", _sc(80, 1.0, 0.0)),
    ("⑤ スコア80以上 200株 + 60-79 100株",     "#f87171", _tiered(2.0, 2.0, 1.0)),
    ("⑥ 段階加重 90→3倍 / 80→2倍 / 60→1倍",  "#a78bfa", _tiered(3.0, 2.0, 1.0)),
]


# ── 取引収集 ──────────────────────────────────────────────────────────────────
def _build_watchlist() -> list[tuple[str, str, str, bool]]:
    """全設定から重複なし (sym, name, strat, is_stop) を返す。"""
    seen: set = set()
    out: list = []

    def _add(wl, is_stop: bool):
        for sym, name, strat in wl:
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                out.append((sym, name, strat, is_stop))

    _add(_stop.WATCHLIST, True)
    _add(_brk.WATCHLIST, False)

    # WF / NOLIMIT / 統合
    for mod_name in ("run_signals_wf", "run_signals_nolimit", "run_signals_merged"):
        try:
            m = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr, is_stop in [
            ("_STOP_WATCHLIST_CONSERVATIVE", True),
            ("_STOP_WATCHLIST_AGGRESSIVE",   True),
            ("STOP_WATCHLIST",               True),
            ("_WF_STOP",                     True),
            ("_STOP_WATCHLIST",              True),
            ("_BRK_WATCHLIST_CONSERVATIVE",  False),
            ("_BRK_WATCHLIST_AGGRESSIVE",    False),
            ("BRK_WATCHLIST",                False),
            ("_WF_BRK",                      False),
            ("_BRK_WATCHLIST",               False),
        ]:
            wl = getattr(m, attr, None)
            if wl:
                _add(list(wl), is_stop)

    return out


def collect_trades(days: int, workers: int) -> list[dict]:
    """
    全WATCHLISTをバックテストし、スコア付きトレードログを返す。
    pnl は FIXED_QTY(100株) 基準。倍率適用は呼び出し側で行う。
    """
    wl = _build_watchlist()
    print(f"  {len(wl)}銘柄ペアをバックテスト中 (workers={workers})…")

    results: list[dict] = []
    since = _TODAY - timedelta(days=days)

    def _one(sym: str, name: str, strat: str, is_stop: bool):
        fn = _stop.backtest_one if is_stop else _brk.backtest_one
        try:
            return fn(sym, name, strat)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, sym, name, strat, is_stop): (sym, strat)
                for sym, name, strat, is_stop in wl}
        done = 0
        for fut in as_completed(futs):
            done += 1
            print(f"\r  {done}/{len(wl)} ...", end="", flush=True)
            r = fut.result()
            if not r:
                continue
            period_results = r.get("period_results") or {}
            if not period_results:
                continue
            score, rank = calc_recommend_score(period_results)
            max_period  = max(period_results.keys())
            trade_log   = period_results[max_period].get("trade_log") or []
            for t in trade_log:
                exit_dt = t.get("exit_dt")
                if exit_dt is None:
                    continue
                exit_d = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
                if exit_d < since or exit_d > _TODAY:
                    continue
                results.append({
                    "symbol":   r["symbol"],
                    "name":     r["name"],
                    "strategy": r["strategy"],
                    "score":    score,
                    "rank":     rank,
                    "entry_p":  t.get("entry_p", 0.0),
                    "exit_d":   exit_d,
                    "pnl":      t.get("pnl", 0.0),
                    "reason":   t.get("reason") or "",
                })
    print()

    # (symbol, strategy, exit_d) で重複除去（同銘柄が複数設定にある場合）
    seen_key: set = set()
    deduped: list[dict] = []
    for t in sorted(results, key=lambda x: x["score"], reverse=True):
        key = (t["symbol"], t["strategy"], t["exit_d"])
        if key not in seen_key:
            seen_key.add(key)
            deduped.append(t)

    print(f"  → {len(deduped)}件のトレード (重複除去後)")
    return deduped


# ── シミュレーション ───────────────────────────────────────────────────────────
def simulate(trades: list[dict], mult_fn) -> dict:
    """
    mult_fn(score) -> 倍率 (0=スキップ) を適用してシミュレーション統計を返す。
    """
    # 日次P&L集計
    daily: dict[date, float] = defaultdict(float)
    n_total = 0
    n_win   = 0
    gp = gl = 0.0
    cap_list: list[float] = []   # 1トレードあたり投入資金
    reason_cnt: dict[str, int] = defaultdict(int)

    for t in trades:
        m = mult_fn(t["score"])
        if m == 0.0:
            continue
        pnl = t["pnl"] * m
        daily[t["exit_d"]] += pnl
        n_total += 1
        if pnl > 0:
            n_win += 1
            gp += pnl
        else:
            gl += abs(pnl)
        cap = t["entry_p"] * FIXED_QTY * m
        if cap > 0:
            cap_list.append(cap)
        reason_cnt[t["reason"]] += 1

    if n_total == 0:
        return {"n_total": 0}

    total_pnl = gp - gl
    wr = n_win / n_total * 100
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    avg = total_pnl / n_total

    # 最大ドローダウン (累積P&L ベース)
    sorted_days = sorted(daily.keys())
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for d in sorted_days:
        cum += daily[d]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    max_dd_pct = max_dd / INITIAL_CASH * 100

    # 累積P&L 系列 (全日付空白補完用に daily を返す)
    avg_cap = sum(cap_list) / len(cap_list) if cap_list else 0.0
    max_cap = max(cap_list) if cap_list else 0.0

    return dict(
        n_total=n_total, n_win=n_win, wr=wr, pf=pf, avg=avg,
        total_pnl=total_pnl, max_dd=max_dd, max_dd_pct=max_dd_pct,
        avg_cap=avg_cap, max_cap=max_cap,
        daily=dict(daily),
        reason_cnt=dict(reason_cnt),
    )


def build_cum_series(daily: dict[date, float], all_dates: list[str]) -> list[float]:
    """all_dates (isoformat) を x 軸にして累積P&Lリストを返す。"""
    cum = 0.0
    out: list[float] = []
    for ds in all_dates:
        d = date.fromisoformat(ds)
        cum += daily.get(d, 0.0)
        out.append(round(cum, 0))
    return out


def score_bucket_stats(trades: list[dict]) -> list[dict]:
    """スコア帯別の集計を返す (シナリオに依存しない)。"""
    buckets = [
        (90, 101, "90-100", "#4ade80"),
        (80,  90, "80-89",  "#86efac"),
        (70,  80, "70-79",  "#60a5fa"),
        (60,  70, "60-69",  "#93c5fd"),
        (50,  60, "50-59",  "#fbbf24"),
        (40,  50, "40-49",  "#fcd34d"),
        (30,  40, "30-39",  "#f87171"),
        (0,   30, "0-29",   "#94a3b8"),
    ]
    result = []
    for lo, hi, lbl, col in buckets:
        sub = [t for t in trades if lo <= t["score"] < hi]
        n = len(sub)
        if n == 0:
            continue
        wins = sum(1 for t in sub if t["pnl"] > 0)
        pnl  = sum(t["pnl"] for t in sub)
        gp   = sum(t["pnl"] for t in sub if t["pnl"] > 0)
        gl   = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        avg_cap = sum(t["entry_p"] * FIXED_QTY for t in sub if t["entry_p"] > 0)
        avg_cap = avg_cap / n if n else 0
        result.append(dict(
            label=lbl, color=col,
            n=n, wins=wins,
            wr=wins / n * 100,
            pf=pf, pnl=pnl,
            avg=pnl / n,
            avg_cap=avg_cap,
        ))
    return result


# ── HTML 生成 ─────────────────────────────────────────────────────────────────
_CSS = """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Helvetica Neue',sans-serif;background:#0f172a;color:#e2e8f0;
     padding:24px 32px;line-height:1.55}
h1{font-size:1.6rem;color:#f8fafc;border-bottom:3px solid #3b82f6;
   padding-bottom:10px;margin-bottom:20px}
h2{font-size:1.15rem;color:#94a3b8;margin:28px 0 12px}
h3{font-size:1rem;color:#64748b;margin:20px 0 8px}
table{width:100%;border-collapse:collapse;margin-bottom:20px;font-size:0.87rem}
th{background:#1e293b;color:#94a3b8;padding:9px 12px;text-align:right;
   border-bottom:2px solid #334155;white-space:nowrap}
th:first-child{text-align:left}
td{padding:9px 12px;border-bottom:1px solid #1e293b;text-align:right;
   color:#cbd5e1;vertical-align:middle}
td:first-child{text-align:left}
tr:hover td{background:#1e293b}
.profit{color:#4ade80;font-weight:600}
.loss{color:#f87171;font-weight:600}
.neutral{color:#94a3b8}
.best{background:#052e1620!important}
.chart-wrap{background:#1e293b;border:1px solid #334155;border-radius:10px;
            padding:18px;margin-bottom:24px}
.kpi-grid{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
.kpi{background:#1e293b;border:1px solid #334155;border-radius:8px;
     padding:14px 18px;min-width:140px;flex:1}
.kpi-l{font-size:0.78rem;color:#64748b;margin-bottom:4px}
.kpi-v{font-size:1.35rem;font-weight:700;color:#f8fafc}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;
     margin-right:6px;vertical-align:middle}
.note-box{background:#1e293b;border-left:4px solid #f59e0b;border-radius:6px;
          padding:14px 18px;color:#94a3b8;font-size:0.85rem;margin-bottom:20px}
.note-box strong{color:#fbbf24}
</style>
"""


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_html(trades: list[dict], days: int, mode: str) -> str:
    if not trades:
        return "<html><body><p>取引データがありません</p></body></html>"

    # 全日付を収集
    all_dates_sorted = sorted({t["exit_d"].isoformat() for t in trades})

    # シナリオごとに統計 + 累積系列を計算
    sim_results: list[tuple[tuple, dict, list[float]]] = []
    best_pnl = -float("inf")
    for sc in SCENARIOS:
        st = simulate(trades, sc[2])
        cum_series = []
        if st["n_total"] > 0:
            cum_series = build_cum_series(st["daily"], all_dates_sorted)
            if st["total_pnl"] > best_pnl:
                best_pnl = st["total_pnl"]
        sim_results.append((sc, st, cum_series))

    # ── KPI (ベースライン) ───────────────────────────────────────────────────
    base_st = sim_results[0][1]
    kpi_html = f"""
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-l">分析期間</div><div class="kpi-v">{days}日</div></div>
  <div class="kpi"><div class="kpi-l">総取引数 (ベースライン)</div><div class="kpi-v">{base_st['n_total']}件</div></div>
  <div class="kpi"><div class="kpi-l">勝率 (ベースライン)</div><div class="kpi-v">{base_st['wr']:.1f}%</div></div>
  <div class="kpi"><div class="kpi-l">モード</div><div class="kpi-v" style="font-size:1rem">{mode}</div></div>
</div>"""

    # ── シナリオ比較テーブル ──────────────────────────────────────────────────
    rows = ""
    for (label, color, _), st, _ in sim_results:
        if st.get("n_total", 0) == 0:
            rows += f"<tr><td><span class='dot' style='background:{color}'></span>{label}</td>" + "<td>—</td>" * 8 + "</tr>\n"
            continue
        pf_s = _pf_str(st["pf"])
        pnl_c = "profit" if st["total_pnl"] >= 0 else "loss"
        avg_c = "profit" if st["avg"] >= 0 else "loss"
        is_best = abs(st["total_pnl"] - best_pnl) < 1
        best_cls = ' class="best"' if is_best else ""
        rows += f"""<tr{best_cls}>
  <td><span class='dot' style='background:{color}'></span>{label}</td>
  <td>{st['n_total']}</td>
  <td>{st['wr']:.1f}%</td>
  <td>{pf_s}</td>
  <td class="{pnl_c}">{st['total_pnl']:+,.0f}円</td>
  <td class="{avg_c}">{st['avg']:+,.0f}円</td>
  <td>{st['max_dd_pct']:.1f}%</td>
  <td style="color:#94a3b8;font-size:0.82rem">{st['avg_cap']:,.0f}円</td>
</tr>"""

    scenario_table = f"""
<table>
<thead><tr>
  <th style="text-align:left">シナリオ</th>
  <th>取引数</th><th>勝率</th><th>PF</th>
  <th>合計損益</th><th>平均損益/取引</th>
  <th>最大DD%</th><th>平均投入資金/取引</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""

    # ── Chart.js データ ───────────────────────────────────────────────────────
    datasets = []
    for (label, color, _), st, cum_series in sim_results:
        if not cum_series:
            continue
        datasets.append({
            "label":           label,
            "data":            cum_series,
            "borderColor":     color,
            "backgroundColor": color + "22",
            "borderWidth":     2,
            "pointRadius":     0,
            "tension":         0.3,
            "fill":            False,
        })

    chart_labels_js = json.dumps(all_dates_sorted)
    chart_datasets_js = json.dumps(datasets)

    chart_html = f"""
<div class="chart-wrap">
  <canvas id="cumChart" height="80"></canvas>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
new Chart(document.getElementById('cumChart'), {{
  type: 'line',
  data: {{
    labels: {chart_labels_js},
    datasets: {chart_datasets_js}
  }},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ labels: {{ color: '#94a3b8', font: {{ size: 12 }} }} }},
      tooltip: {{
        callbacks: {{
          label: ctx => ctx.dataset.label + ': ' + ctx.parsed.y.toLocaleString() + '円'
        }}
      }}
    }},
    scales: {{
      x: {{
        ticks: {{ color: '#64748b', maxTicksLimit: 12 }},
        grid:  {{ color: '#1e293b' }}
      }},
      y: {{
        ticks: {{ color: '#64748b',
          callback: v => (v >= 0 ? '+' : '') + v.toLocaleString() + '円' }},
        grid: {{ color: '#1e293b' }},
        title: {{ display: true, text: '累積損益 (円)', color: '#64748b' }}
      }}
    }}
  }}
}});
</script>"""

    # ── スコア帯別詳細 ───────────────────────────────────────────────────────
    bucket_rows = ""
    total_pnl_base = sum(t["pnl"] for t in trades)
    for b in score_bucket_stats(trades):
        pf_s = _pf_str(b["pf"])
        pnl_c = "profit" if b["pnl"] >= 0 else "loss"
        avg_c = "profit" if b["avg"] >= 0 else "loss"
        contrib = b["pnl"] / total_pnl_base * 100 if total_pnl_base else 0
        bucket_rows += f"""<tr>
  <td style="color:{b['color']};font-weight:600">{b['label']}</td>
  <td>{b['n']}</td>
  <td>{b['wr']:.1f}%</td>
  <td>{pf_s}</td>
  <td class="{pnl_c}">{b['pnl']:+,.0f}円</td>
  <td class="{avg_c}">{b['avg']:+,.0f}円</td>
  <td style="color:#94a3b8">{contrib:+.1f}%</td>
  <td style="color:#64748b;font-size:0.82rem">{b['avg_cap']:,.0f}円</td>
</tr>"""

    bucket_table = f"""
<table>
<thead><tr>
  <th style="text-align:left">スコア帯</th>
  <th>取引数</th><th>勝率</th><th>PF</th>
  <th>合計損益</th><th>平均損益/取引</th>
  <th>利益寄与率</th><th>平均株価×100株</th>
</tr></thead>
<tbody>{bucket_rows}</tbody>
</table>"""

    # ── スコア80以上 詳細 ────────────────────────────────────────────────────
    top80 = [t for t in trades if t["score"] >= 80]
    top80_sym_rows = ""
    sym_stats: dict = defaultdict(lambda: dict(n=0, wins=0, pnl=0.0, scores=[]))
    for t in top80:
        k = (t["symbol"], t["name"], t["strategy"])
        sym_stats[k]["n"]     += 1
        sym_stats[k]["wins"]  += 1 if t["pnl"] > 0 else 0
        sym_stats[k]["pnl"]   += t["pnl"]
        sym_stats[k]["scores"].append(t["score"])
    for (sym, name, strat), s in sorted(sym_stats.items(),
                                         key=lambda x: -x[1]["pnl"]):
        wr_s = s["wins"] / s["n"] * 100
        avg_score = sum(s["scores"]) / len(s["scores"])
        pnl_c = "profit" if s["pnl"] >= 0 else "loss"
        top80_sym_rows += f"""<tr>
  <td style="font-size:0.82rem;color:#64748b">{sym}</td>
  <td>{name[:12]}</td>
  <td><span style="background:#1e293b;padding:2px 6px;border-radius:4px;
       font-size:0.78rem">{strat}</span></td>
  <td>{s['n']}</td>
  <td>{wr_s:.0f}%</td>
  <td class="{pnl_c}">{s['pnl']:+,.0f}円</td>
  <td style="color:#fbbf24">{avg_score:.0f}</td>
</tr>"""

    top80_table = f"""
<table>
<thead><tr>
  <th style="text-align:left">コード</th><th style="text-align:left">銘柄</th>
  <th style="text-align:left">戦略</th>
  <th>取引数</th><th>勝率</th><th>合計損益</th><th>平均スコア</th>
</tr></thead>
<tbody>{top80_sym_rows}</tbody>
</table>"""

    # ── 注意事項 ─────────────────────────────────────────────────────────────
    note_html = """
<div class="note-box">
  <strong>⚠️ 解釈上の注意点</strong><br>
  <ul style="margin:8px 0 0 16px;line-height:1.8">
    <li><strong>In-sample bias</strong>: スコアは同期間のバックテスト成績から計算されます。
    「高スコア=過去に良かった銘柄」であり、将来も高スコア銘柄が勝ち続ける保証はありません。</li>
    <li><strong>資金集中リスク</strong>: 段階加重シナリオ (⑥) は1トレードで平均投入資金の3倍を使います。
    複数シグナルが重なると総資産を超える発注になりうるため、同時保有数の上限設定が必須です。</li>
    <li><strong>流動性</strong>: スタンダード市場の薄い銘柄で200〜300株の逆指値を入れると
    スリッページが想定の0.5%を大きく超える可能性があります。</li>
    <li><strong>推奨確認手順</strong>: フォワードテスト (<code>forward_test.py</code>) で
    まず100株の実績を1〜3ヶ月積み、スコアと実P&Lの相関が実データでも成立するか確認してから
    サイズアップを検討してください。</li>
  </ul>
</div>"""

    today_str = _TODAY.isoformat()
    mode_badge = (f'<span style="background:#7c3aed;color:#fff;padding:3px 10px;'
                  f'border-radius:4px;font-size:0.85rem;margin-left:10px">{mode}</span>')

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>スコア集中シミュレーション {today_str}</title>
{_CSS}
</head>
<body>
<h1>スコア集中ポジションサイジング シミュレーション{mode_badge}</h1>
<p style="color:#64748b;margin-bottom:20px;font-size:0.88rem">
  分析対象期間: 直近{days}日 ／ 基準日: {today_str} ／ ベース株数: {FIXED_QTY}株
</p>

{kpi_html}

<h2>📊 シナリオ別 累積損益グラフ</h2>
{chart_html}

<h2>📋 シナリオ比較サマリー</h2>
<p style="color:#64748b;font-size:0.83rem;margin-bottom:8px">
  ★ 最高合計損益シナリオを強調表示。平均投入資金は1トレードあたりの試算。
</p>
{scenario_table}

<h2>🎯 スコア帯別詳細 (ベースライン全取引)</h2>
<p style="color:#64748b;font-size:0.83rem;margin-bottom:8px">
  利益寄与率 = そのスコア帯の合計損益 ÷ 全取引合計損益
</p>
{bucket_table}

<h2>🏆 スコア80以上 銘柄別成績</h2>
{top80_table if top80 else "<p style='color:#64748b'>スコア80以上の取引がありません</p>"}

<h2>⚠️ 注意事項</h2>
{note_html}

</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="スコア集中サイジング シミュレーション")
    p.add_argument("--days",       type=int, default=365,        help="バックテスト期間 (日)")
    p.add_argument("--workers",    type=int, default=_DEF_WORKERS, help="並列数")
    p.add_argument("--no-browser", action="store_true",          help="ブラウザ自動起動しない")
    p.add_argument("--aggressive", action="store_true",          help="aggressiveモード")
    args = p.parse_args()

    mode = "aggressive" if args.aggressive or _IS_AGG else "conservative"
    suffix = "_aggressive" if mode == "aggressive" else ""

    print(f"[score_sizing_sim] days={args.days} mode={mode}")
    trades = collect_trades(args.days, args.workers)

    if not trades:
        print("取引データがありません。")
        return

    print("シナリオ計算中…")
    html = build_html(trades, args.days, mode)

    fname = f"score_sizing_sim{suffix}_{_TODAY.isoformat()}.html"
    Path(fname).write_text(html, encoding="utf-8")
    print(f"→ {fname}")

    if not args.no_browser:
        webbrowser.open(str(Path(fname).resolve()))


if __name__ == "__main__":
    main()
