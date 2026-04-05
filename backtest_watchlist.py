"""
backtest_watchlist.py  ―  監視銘柄バックテスト損益レポート
===========================================================
監視リスト（PRESETS）の銘柄を各担当戦略でバックテストし、
損益・勝率・PFを銘柄別・合算でHTML出力する。

使い方:
  python backtest_watchlist.py                      # core 8銘柄 / 365日
  python backtest_watchlist.py --preset all         # 全22銘柄
  python backtest_watchlist.py --days 90            # 直近90日
  python backtest_watchlist.py --preset core --days 180 --no-browser
"""

from __future__ import annotations

import argparse
import math
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest_adaptive_mr       as _amr
import backtest_strong_pullback   as _sp
import backtest_donchian_pullback as _don
import backtest_bb_volume         as _bbv
import backtest_limit_oco         as _oco

# check_signals の監視リスト・プリセットを共有
from check_signals import WATCHLIST, PRESETS, STRATEGY_NAMES, STRATEGY_COLOR

JST = timezone(timedelta(hours=9))
LOT = 100


# ─────────────────────────────────────────────────────────────────────
# バックテスト実行
# ─────────────────────────────────────────────────────────────────────

def _run_backtest(symbol: str, key: str, days: int) -> list[dict]:
    """1銘柄×1戦略のバックテストを実行してトレードリストを返す。"""
    try:
        if key == "adaptive_mr":
            df = _amr.fetch(symbol, days)
            if df is None: return []
            df = _amr.calc(df)
            return _amr.backtest_amr(df, days)
        elif key == "strong_pullback":
            df = _sp.fetch(symbol, days)
            if df is None: return []
            df = _sp.calc(df)
            return _sp.backtest_strong_pullback(df, days)
        elif key == "donchian":
            df = _don.fetch(symbol, days)
            if df is None: return []
            df = _don.calc(df)
            return _don.backtest_donchian(df, days)
        elif key == "bb_volume":
            df = _bbv.fetch(symbol, days)
            if df is None: return []
            df = _bbv.calc(df)
            return _bbv.backtest_bb_vol(df, days)
        elif key == "limit_oco":
            df = _oco.fetch(symbol, days)
            if df is None: return []
            df = _oco.calc(df)
            return _oco.backtest_limit(df, days, _oco.DEFAULT_PARAMS)
    except Exception as e:
        print(f"  ※ {symbol} [{key}] エラー: {e}")
    return []


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wins=0, wr=0.0, pf=0.0, total=0.0,
                    avg=0.0, max_win=0.0, max_loss=0.0,
                    max_dd=0.0, max_consec_loss=0)
    sorted_t   = sorted(trades, key=lambda t: t.get("exit_dt") or "")
    pnls       = [t["pnl"] for t in sorted_t]
    wins       = [p for p in pnls if p > 0]
    loss       = [p for p in pnls if p <= 0]
    wr         = len(wins) / len(pnls) * 100
    gross_win  = sum(wins)
    gross_loss = abs(sum(loss))
    pf         = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # 最大ドローダウン（累積損益のピーク→谷）
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # 最大連敗数
    max_consec = cur_consec = 0
    for p in pnls:
        if p <= 0:
            cur_consec += 1
            max_consec  = max(max_consec, cur_consec)
        else:
            cur_consec  = 0

    return dict(
        n              = len(pnls),
        wins           = len(wins),
        wr             = wr,
        pf             = pf,
        total          = sum(pnls),
        avg            = sum(pnls) / len(pnls),
        max_win        = max(pnls),
        max_loss       = min(pnls),
        max_dd         = max_dd,
        max_consec_loss= max_consec,
    )


def run_all(watchlist: list, days: int) -> list[dict]:
    """
    watchlist の各銘柄を担当戦略でバックテスト（並列実行）。
    戻り値: [{symbol, name, key, trades, stats}, ...]
    """
    tasks = []
    for symbol, name, strategies in watchlist:
        key = strategies[0]   # 主戦略のみ使用
        tasks.append((symbol, name, key))

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut_map = {
            ex.submit(_run_backtest, sym, key, days): (sym, nm, key)
            for sym, nm, key in tasks
        }
        for fut in as_completed(fut_map):
            sym, nm, key = fut_map[fut]
            trades = fut.result()
            results.append(dict(
                symbol = sym,
                name   = nm,
                key    = key,
                trades = trades,
                stats  = _stats(trades),
            ))

    results.sort(key=lambda r: (-r["stats"]["total"], r["symbol"]))
    return results


# ─────────────────────────────────────────────────────────────────────
# HTML 生成
# ─────────────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Meiryo',sans-serif;background:#0d1117;color:#e6edf3;padding:20px}
h1{font-size:1.2em;color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:16px}
h2{font-size:.95em;color:#8b949e;margin:24px 0 8px}
table{border-collapse:collapse;width:100%;font-size:.82em;margin-bottom:24px}
th{background:#161b22;color:#8b949e;padding:7px 12px;border:1px solid #30363d;
   text-align:right;white-space:nowrap;cursor:pointer}
th:first-child,th:nth-child(2),th:nth-child(3){text-align:left}
td{padding:6px 12px;border:1px solid #21262d;text-align:right;vertical-align:middle}
td:first-child,td:nth-child(2),td:nth-child(3){text-align:left}
tr:nth-child(even){background:#0d1117}
tr:nth-child(odd){background:#161b22}
tr.total-row td{background:#1a2332;font-weight:bold;border-top:2px solid #38bdf8}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.sub{font-size:.75em;color:#6b7280;margin-top:1px}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;
       font-size:.75em;font-weight:bold;border:1px solid}
.equity-wrap{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;margin-bottom:24px}
svg text{font-size:11px;font-family:'Meiryo',sans-serif}
details{margin-bottom:8px}
details summary{cursor:pointer;color:#58a6ff;font-size:.85em;padding:4px 0;user-select:none}
details summary:hover{color:#7dd3fc}
"""

_JS = """
function sortTable(tbl, col) {
  var rows = Array.from(tbl.tBodies[0].rows);
  var asc  = tbl.dataset.sortCol == col && tbl.dataset.sortDir == 'asc';
  rows.sort(function(a, b) {
    var av = a.cells[col].dataset.v ?? a.cells[col].textContent.replace(/[^0-9.\\-]/g,'');
    var bv = b.cells[col].dataset.v ?? b.cells[col].textContent.replace(/[^0-9.\\-]/g,'');
    return (asc ? 1 : -1) * (parseFloat(av) - parseFloat(bv) || av.localeCompare(bv));
  });
  rows.forEach(r => tbl.tBodies[0].appendChild(r));
  tbl.dataset.sortCol = col;
  tbl.dataset.sortDir = asc ? 'desc' : 'asc';
}
"""


def _equity_svg(all_trades: list[dict], width: int = 700, height: int = 160) -> str:
    """全トレードの累積損益 SVG折れ線グラフを返す。"""
    if not all_trades:
        return "<p style='color:#4b5563'>トレードなし</p>"

    sorted_trades = sorted(all_trades, key=lambda t: t.get("exit_dt") or t.get("entry_dt") or "")
    cumulative = 0.0
    points: list[tuple[int, float]] = [(0, 0.0)]
    for t in sorted_trades:
        cumulative += t["pnl"]
        points.append((len(points), cumulative))

    n      = len(points)
    px     = [40 + (p[0] / (n - 1)) * (width - 50) for p in points] if n > 1 else [40]
    values = [p[1] for p in points]
    mn, mx = min(values), max(values)
    rng    = mx - mn or 1
    pad    = 20

    def vy(v: float) -> float:
        return pad + (1 - (v - mn) / rng) * (height - pad * 2)

    pts = " ".join(f"{px[i]:.1f},{vy(values[i]):.1f}" for i in range(n))
    zero_y = vy(0)
    zero_line = f'<line x1="40" x2="{width-10}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" stroke="#374151" stroke-dasharray="4,3"/>'

    final = values[-1]
    color = "#3fb950" if final >= 0 else "#f85149"
    label_color = "#3fb950" if mx >= 0 else "#f85149"

    return f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  {zero_line}
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>
  <text x="42" y="{vy(mx)-4:.1f}" fill="{label_color}" text-anchor="start">+{mx:,.0f}円</text>
  <text x="42" y="{vy(mn)+14:.1f}" fill="#f85149" text-anchor="start">{mn:,.0f}円</text>
  <text x="{width-10}" y="{vy(final):.1f}" fill="{color}" text-anchor="end" font-weight="bold">{final:+,.0f}円</text>
</svg>"""


def _trade_rows(trades: list[dict]) -> str:
    if not trades:
        return "<tr><td colspan='13' style='color:#4b5563;text-align:center'>トレードなし</td></tr>"
    rows = ""
    for t in sorted(trades, key=lambda t: t.get("exit_dt") or ""):
        pnl      = t["pnl"]
        pnl_cls  = "up" if pnl > 0 else "dn"
        reason   = t.get("reason", "—")
        entry_d  = str(t.get("entry_dt", ""))[:10]
        exit_d   = str(t.get("exit_dt",  ""))[:10]
        hold     = t.get("hold", "—")

        # シグナル日・シグナル株価
        sig_dt    = str(t.get("signal_dt", ""))[:10]
        sig_cl    = t.get("signal_close", 0) or 0

        # 指値・損切・目標（戦略によってキー名が異なる）
        limit_p  = t.get("limit_p")  or t.get("limit_price") or t.get("entry_p", 0)
        stop_p   = t.get("stop_p")   or t.get("stop_loss")   or t.get("stop",    0)
        target_p = t.get("target_p") or t.get("profit_target") or t.get("target", 0)
        entry_p  = t.get("entry_p", 0)
        exit_p   = t.get("exit_p",  0)
        atr      = t.get("atr", 0)

        # nan/0 ガード
        def _valid(v): return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0

        # 指値からの乖離率
        slip = (entry_p - limit_p) / limit_p * 100 if _valid(limit_p) else 0
        slip_s = f"<div class='sub'>約定 {slip:+.1f}%</div>" if abs(slip) > 0.01 else ""

        # ストップまでのリスク
        risk   = entry_p - stop_p if _valid(stop_p) else 0
        rr_val = (target_p - entry_p) / risk if (risk > 0 and _valid(target_p)) else None
        rr_s   = f"{rr_val:.1f}R" if rr_val else "—"

        sig_cl_s    = f"¥{sig_cl:,.0f}" if sig_cl else "—"
        target_s    = f"¥{target_p:,.0f}" if _valid(target_p) else "—(動的)"
        stop_s      = f"¥{stop_p:,.0f}" if _valid(stop_p) else "—"

        rows += (
            f"<tr>"
            f"<td>{sig_dt}<div class='sub'>{sig_cl_s}</div></td>"
            f"<td>{entry_d}</td>"
            f"<td>{exit_d}</td>"
            f"<td>¥{limit_p:,.0f}<div class='sub'>指値</div></td>"
            f"<td>¥{entry_p:,.0f}{slip_s}</td>"
            f"<td class='dn'>{stop_s}<div class='sub'>{'リスク¥' + f'{risk:,.0f}' if risk else ''}</div></td>"
            f"<td class='up'>{target_s}<div class='sub'>{rr_s}</div></td>"
            f"<td>¥{exit_p:,.0f}</td>"
            f"<td class='{pnl_cls}'>{pnl:+,.0f}円</td>"
            f"<td>{hold}日</td>"
            f"<td class='neu' style='font-size:.78em'>ATR¥{atr:,.0f}</td>"
            f"<td>{reason}</td>"
            f"</tr>"
        )
    return rows


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _strategy_scoring(results: list[dict], days: int = 0) -> str:
    """
    戦略ごとに銘柄をまとめ、5指標をスコアリングして優先度テーブルを返す。
    指標: 勝率・PF・平均損益・最大DD・最大連敗（各0〜5点、合計25点満点）
    """
    from collections import defaultdict

    # 戦略ごとに全トレードを集約
    strat_trades: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        strat_trades[r["key"]].extend(r["trades"])

    if not strat_trades:
        return "<p style='color:#4b5563'>データなし</p>"

    # 各戦略の集計統計
    strat_stats = {k: _stats(v) for k, v in strat_trades.items()}

    # スコアリング（相対ランキングで 1〜5点）
    def _rank_scores(keys: list[str], metric_fn, higher_is_better: bool) -> dict[str, int]:
        vals = [(k, metric_fn(strat_stats[k])) for k in keys]
        vals.sort(key=lambda x: x[1], reverse=higher_is_better)
        n = len(vals)
        scores = {}
        for rank, (k, _) in enumerate(vals):
            scores[k] = max(1, round(5 - rank * 4 / max(n - 1, 1)))
        return scores

    keys = list(strat_stats.keys())
    sc_wr   = _rank_scores(keys, lambda s: s["wr"],              True)
    sc_pf   = _rank_scores(keys, lambda s: min(s["pf"], 10),     True)
    sc_avg  = _rank_scores(keys, lambda s: s["avg"],             True)
    sc_dd   = _rank_scores(keys, lambda s: s["max_dd"],          False)   # 低い方が良い
    sc_cl   = _rank_scores(keys, lambda s: s["max_consec_loss"], False)   # 低い方が良い

    # テーブル生成（合計スコア降順）
    ranked = sorted(keys, key=lambda k: -(sc_wr[k]+sc_pf[k]+sc_avg[k]+sc_dd[k]+sc_cl[k]))

    def bar(score: int, max_s: int = 5) -> str:
        filled = "■" * score + "□" * (max_s - score)
        color  = "#3fb950" if score >= 4 else ("#f0883e" if score >= 2 else "#f85149")
        return f"<span style='color:{color};letter-spacing:1px'>{filled}</span> {score}"

    rows = ""
    for rank, k in enumerate(ranked, 1):
        s     = strat_stats[k]
        color = STRATEGY_COLOR.get(k, "#94a3b8")
        name  = STRATEGY_NAMES.get(k, k)
        total = sc_wr[k] + sc_pf[k] + sc_avg[k] + sc_dd[k] + sc_cl[k]
        medal = ["🥇", "🥈", "🥉", "4位", "5位"][rank - 1] if rank <= 5 else str(rank)
        pf_s  = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        rows += (
            f"<tr>"
            f"<td style='font-weight:700'>{medal}</td>"
            f"<td><span class='badge' style='color:{color};border-color:{color}'>{name}</span></td>"
            f"<td>{s['n']}件</td>"
            f"<td>{bar(sc_wr[k])}<div class='sub'>{s['wr']:.1f}%</div></td>"
            f"<td>{bar(sc_pf[k])}<div class='sub'>PF {pf_s}</div></td>"
            f"<td>{bar(sc_avg[k])}<div class='sub'>{s['avg']:+,.0f}円/件</div></td>"
            f"<td>{bar(sc_dd[k])}<div class='sub'>▼{s['max_dd']:,.0f}円</div></td>"
            f"<td>{bar(sc_cl[k])}<div class='sub'>{s['max_consec_loss']}連敗</div></td>"
            f"<td style='font-weight:700;font-size:1.1em'>{total}/25</td>"
            f"</tr>"
        )

    return f"""<table>
<thead><tr>
  <th>順位</th><th>戦略</th><th>取引数</th>
  <th>勝率</th><th>PF</th><th>平均損益</th>
  <th>最大DD<br><span style='font-weight:normal;font-size:.85em'>(低い方が良)</span></th>
  <th>最大連敗<br><span style='font-weight:normal;font-size:.85em'>(少ない方が良)</span></th>
  <th>合計スコア</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<p style='font-size:.8em;color:#4b5563;margin-top:4px'>
  ※ 各指標を相対ランキングで1〜5点採点（25点満点）。バックテスト期間({days}日)の結果に基づく。
</p>"""


def build_html(results: list[dict], all_trades: list[dict],
               days: int, preset_name: str) -> str:
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    total_s = _stats(all_trades)

    # ── サマリーテーブル ─────────────────────────────────────────
    active_results  = [r for r in results if r["stats"]["n"] > 0]
    no_trade_results = [r for r in results if r["stats"]["n"] == 0]

    rows = ""
    for r in active_results:
        s       = r["stats"]
        color   = STRATEGY_COLOR.get(r["key"], "#94a3b8")
        strat   = STRATEGY_NAMES.get(r["key"], r["key"])
        pnl_cls = "up" if s["total"] > 0 else ("dn" if s["total"] < 0 else "neu")
        avg_cls = "up" if s["avg"] > 0 else "dn"
        pf_val  = 0 if s["pf"] == float("inf") else s["pf"]
        rows += (
            f"<tr>"
            f"<td>{r['symbol']}</td>"
            f"<td>{r['name']}</td>"
            f"<td><span class='badge' style='color:{color};border-color:{color}'>{strat}</span></td>"
            f"<td data-v='{s['n']}'>{s['n']}</td>"
            f"<td data-v='{s['wr']:.1f}'>{s['wr']:.1f}%</td>"
            f"<td data-v='{pf_val:.2f}'>{_pf_str(s['pf'])}</td>"
            f"<td data-v='{s['total']:.0f}' class='{pnl_cls}'>{s['total']:+,.0f}円</td>"
            f"<td data-v='{s['avg']:.0f}' class='{avg_cls}'>{s['avg']:+,.0f}円</td>"
            f"<td class='up'>{s['max_win']:+,.0f}円</td>"
            f"<td class='dn'>{s['max_loss']:+,.0f}円</td>"
            f"<td class='dn' data-v='{s['max_dd']:.0f}'>▼{s['max_dd']:,.0f}円</td>"
            f"<td data-v='{s['max_consec_loss']}'>{s['max_consec_loss']}連敗</td>"
            f"</tr>"
        )
    # 合計行（取引あり銘柄のみ）
    total_cls = "up" if total_s["total"] > 0 else ("dn" if total_s["total"] < 0 else "neu")
    total_avg_cls = "up" if total_s["avg"] > 0 else "dn"
    rows += (
        f"<tr class='total-row'>"
        f"<td colspan='3'>合計 ({len(active_results)}銘柄)</td>"
        f"<td>{total_s['n']}</td>"
        f"<td>{total_s['wr']:.1f}%</td>"
        f"<td>{_pf_str(total_s['pf'])}</td>"
        f"<td class='{total_cls}'>{total_s['total']:+,.0f}円</td>"
        f"<td class='{total_avg_cls}'>{total_s['avg']:+,.0f}円</td>"
        f"<td class='up'>{total_s['max_win']:+,.0f}円</td>"
        f"<td class='dn'>{total_s['max_loss']:+,.0f}円</td>"
        f"<td class='dn'>▼{total_s['max_dd']:,.0f}円</td>"
        f"<td>{total_s['max_consec_loss']}連敗</td>"
        f"</tr>"
    )

    # 取引なし銘柄を薄く追記
    if no_trade_results:
        strat_names = ", ".join(
            STRATEGY_NAMES.get(r["key"], r["key"]) for r in no_trade_results[:3]
        )
        rows += (
            f"<tr style='color:#4b5563;font-size:.82em'>"
            f"<td colspan='3' style='padding-top:8px'>取引なし（{days}日間）: "
            + "  ".join(f"{r['symbol']} {r['name']}" for r in no_trade_results)
            + "</td><td colspan='9'></td></tr>"
        )

    summary_table = f"""
<table id="summary">
<thead><tr>
  <th onclick="sortTable(this.closest('table'),0)">コード</th>
  <th onclick="sortTable(this.closest('table'),1)">銘柄名</th>
  <th onclick="sortTable(this.closest('table'),2)">戦略</th>
  <th onclick="sortTable(this.closest('table'),3)">取引数</th>
  <th onclick="sortTable(this.closest('table'),4)">勝率</th>
  <th onclick="sortTable(this.closest('table'),5)">PF</th>
  <th onclick="sortTable(this.closest('table'),6)">合計損益</th>
  <th onclick="sortTable(this.closest('table'),7)">平均損益</th>
  <th onclick="sortTable(this.closest('table'),8)">最大利益</th>
  <th onclick="sortTable(this.closest('table'),9)">最大損失</th>
  <th onclick="sortTable(this.closest('table'),10)">最大DD</th>
  <th onclick="sortTable(this.closest('table'),11)">最大連敗</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""

    # ── 銘柄別トレード詳細（取引あり銘柄のみ）────────────────────
    detail_blocks = ""
    for r in active_results:
        strat = STRATEGY_NAMES.get(r["key"], r["key"])
        s     = r["stats"]
        trade_rows = _trade_rows(r["trades"])
        detail_blocks += f"""
<details>
  <summary>▶ {r['symbol']} {r['name']}  [{strat}]
    {s['n']}件 / 勝率{s['wr']:.0f}% / PF{_pf_str(s['pf'])} /
    <span class='{"up" if s["total"]>0 else "dn"}'>{s['total']:+,.0f}円</span>
  </summary>
  <table style="margin-top:6px">
    <thead><tr>
      <th>シグナル日<br><span style='font-weight:normal;font-size:.85em'>(終値)</span></th>
      <th>約定日</th><th>決済日</th>
      <th>指値<br><span style='font-weight:normal;font-size:.85em'>(注文)</span></th>
      <th>約定価格</th>
      <th>損切<br><span style='font-weight:normal;font-size:.85em'>(逆指値)</span></th>
      <th>目標<br><span style='font-weight:normal;font-size:.85em'>(指値)</span></th>
      <th>決済価格</th><th>損益</th><th>保有日数</th><th>ATR</th><th>決済理由</th>
    </tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</details>"""

    equity_svg   = _equity_svg(all_trades)
    scoring_html = _strategy_scoring(results, days)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>監視銘柄バックテスト {now_str[:10]}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>監視銘柄バックテスト損益レポート
  <span style="font-size:.8em;color:#8b949e">
    プリセット: {preset_name} &nbsp;|&nbsp; 期間: 直近{days}日 &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>
<p style="font-size:.8em;color:#4b5563;margin-bottom:16px">
  ※ 主戦略のみ使用 / ロット {LOT}株固定 / 列ヘッダーでソート可
</p>

<h2>▶ 戦略別スコアリング（優先度評価）</h2>
{scoring_html}

<h2>▶ 銘柄別サマリー</h2>
{summary_table}

<h2>▶ 累積損益推移（全銘柄合算 / 決済日順）</h2>
<div class="equity-wrap">{equity_svg}</div>

<h2>▶ 銘柄別トレード詳細（クリックで展開）</h2>
{detail_blocks}

<script>{_JS}</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="監視銘柄バックテスト損益レポート",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_watchlist.py                       # core 8銘柄 / 365日
  python backtest_watchlist.py --preset all          # 全22銘柄
  python backtest_watchlist.py --days 90             # 直近90日
  python backtest_watchlist.py --preset all --days 180
  python backtest_watchlist.py --no-browser          # HTML生成のみ
""")
    preset_choices = list(PRESETS.keys())
    parser.add_argument("--preset",    "-p", choices=preset_choices, default="core",
                        help=f"銘柄グループ: {' / '.join(preset_choices)}（デフォルト: core）")
    parser.add_argument("--days",      type=int, default=365,
                        help="バックテスト日数（デフォルト: 365）")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    codes = PRESETS.get(args.preset, [])
    wl    = [r for r in WATCHLIST if r[0] in codes] if codes else WATCHLIST

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    print(f"\n{'='*72}")
    print(f"  監視銘柄バックテスト  {now}")
    print(f"  プリセット: {args.preset} ({len(wl)}銘柄) / 直近{args.days}日")
    print(f"{'='*72}\n")

    print("バックテスト実行中...")
    results = run_all(wl, args.days)

    # ── ターミナル出力 ───────────────────────────────────────────
    W = (10, 20, 12, 5, 7, 6, 12, 10)
    hdr = (f"{'コード':<{W[0]}}  {'銘柄名':<{W[1]}}  {'戦略':<{W[2]}}"
           f"  {'取引':>{W[3]}}  {'勝率':>{W[4]}}  {'PF':>{W[5]}}"
           f"  {'合計損益':>{W[6]}}  {'平均損益':>{W[7]}}")
    print("\n  " + hdr)
    print("  " + "─" * len(hdr))

    all_trades: list[dict] = []
    no_trade_syms: list[str] = []
    for r in results:
        s     = r["stats"]
        strat = STRATEGY_NAMES.get(r["key"], r["key"])
        all_trades.extend(r["trades"])
        if s["n"] == 0:
            no_trade_syms.append(r["symbol"])
            continue
        pnl_s = f"{s['total']:>+12,.0f}円"
        avg_s = f"{s['avg']:>+9,.0f}円"
        print(f"  {r['symbol']:<{W[0]}}  {r['name']:<{W[1]}}  {strat:<{W[2]}}"
              f"  {s['n']:>{W[3]}}  {s['wr']:>5.1f}%  {_pf_str(s['pf']):>{W[5]}}"
              f"  {pnl_s}  {avg_s}")
    if no_trade_syms:
        print(f"\n  取引なし（{args.days}日間）: {' '.join(no_trade_syms)}")

    total = _stats(all_trades)
    print("  " + "─" * len(hdr))
    print(f"  {'合計':<{W[0]+W[1]+W[2]+6}}"
          f"  {total['n']:>{W[3]}}  {total['wr']:>5.1f}%  {_pf_str(total['pf']):>{W[5]}}"
          f"  {total['total']:>+12,.0f}円  {total['avg']:>+9,.0f}円")
    print()

    # ── HTML 出力 ────────────────────────────────────────────────
    html     = build_html(results, all_trades, args.days, args.preset)
    today_s  = datetime.now(JST).strftime("%Y-%m-%d")
    outpath  = Path(f"backtest_watchlist_{args.preset}_{today_s}.html")
    outpath.write_text(html, encoding="utf-8")
    print(f"HTML: {outpath.resolve()}")

    if not args.no_browser:
        webbrowser.open(f"file://{outpath.resolve()}")

    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
