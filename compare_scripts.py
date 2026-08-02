from _open_html import open_html
"""
compare_scripts.py  ─  スクリプト × 戦略 比較バックテスト
=================================================================
6種のシグナルスクリプト設定（既存版/WF/NOLIMIT/統合）×
6戦略（MACD/A7/RSI2/DON/VOL/MOM）の取引成績を一覧比較します。

デフォルトは +3% 指値上限（現行設定）でバックテスト。

Usage:
  python compare_scripts.py --html
  python compare_scripts.py --days 180 --html
  python compare_scripts.py --no-browser --html
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# ── TRADING_MODE は conservative 固定 (各設定で params を明示的に渡す) ──
os.environ.setdefault("TRADING_MODE", "conservative")

import backtest_limit_entry as _bt
import check_signals_stop        as _stop
import check_signals_breakout    as _brk
from backtest_limit_entry import WORKERS, fetch, run_limit_backtest

STOP_STRATEGIES = {"MACD", "A7", "RSI2"}
BRK_STRATEGIES  = {"DON", "VOL", "MOM"}
ALL_STRATEGIES  = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]


# ── 設定定義 ──────────────────────────────────────────────────────────
def _build_configs(days: int) -> list[dict]:
    """6設定のタスクリストを構築。各タスクは (sym, name, strat, days, fn, em, sm, tm)。"""

    def _stop_tasks(watchlist, params):
        return [
            (sym, name, strat, days, *params[strat])
            for sym, name, strat in watchlist
            if strat in params
        ]

    def _brk_tasks(watchlist, params):
        return [
            (sym, name, strat, days, *params[strat])
            for sym, name, strat in watchlist
            if strat in params
        ]

    def _all_tasks(stop_wl, brk_wl, stop_params, brk_params):
        return _stop_tasks(stop_wl, stop_params) + _brk_tasks(brk_wl, brk_params)

    def _dedup(wl: list) -> list:
        """(sym, strat) 重複除去。"""
        seen: set[tuple] = set()
        out = []
        for item in wl:
            key = (item[0], item[2])
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    def _merge(existing: list, wf: list) -> list:
        ex_keys = {(sym, strat) for sym, _, strat in existing}
        merged = list(existing)
        for item in wf:
            if (item[0], item[2]) not in ex_keys:
                merged.append(item)
        return merged

    # WF watchlists
    try:
        import run_signals_wf as _wf
        wf_stop_con = _wf._STOP_WATCHLIST_CONSERVATIVE
        wf_stop_agg = _wf._STOP_WATCHLIST_AGGRESSIVE
        wf_brk_con  = _wf._BRK_WATCHLIST_CONSERVATIVE
        wf_brk_agg  = _wf._BRK_WATCHLIST_AGGRESSIVE
    except ImportError:
        wf_stop_con = wf_stop_agg = wf_brk_con = wf_brk_agg = []

    # NOLIMIT WF watchlists
    try:
        import run_signals_nolimit as _nl
        nl_stop = _nl.STOP_CANDIDATES
        nl_brk  = _nl.BRK_CANDIDATES
    except ImportError:
        nl_stop = nl_brk = []

    # merged watchlists (run_signals_merged)
    try:
        import run_signals_merged as _mg
        mg_stop = _merge(_stop.WATCHLIST, _mg._WF_STOP)
        mg_brk  = _merge(_brk.WATCHLIST,  _mg._WF_BRK)
    except ImportError:
        mg_stop = _stop.WATCHLIST
        mg_brk  = _brk.WATCHLIST

    con_sp = _stop.STRATEGY_PARAMS_CONSERVATIVE
    agg_sp = _stop.STRATEGY_PARAMS_AGGRESSIVE
    con_bp = _brk.STRATEGY_PARAMS_CONSERVATIVE
    agg_bp = _brk.STRATEGY_PARAMS_AGGRESSIVE

    configs = [
        {
            "label": "既存版\nconservative",
            "short": "既存con",
            "tasks": _dedup(_all_tasks(_stop.WATCHLIST, _brk.WATCHLIST, con_sp, con_bp)),
        },
        {
            "label": "既存版\naggressive",
            "short": "既存agg",
            "tasks": _dedup(_all_tasks(_stop.WATCHLIST, _brk.WATCHLIST, agg_sp, agg_bp)),
        },
        {
            "label": "WF\nconservative",
            "short": "WF con",
            "tasks": _dedup(_all_tasks(wf_stop_con, wf_brk_con, con_sp, con_bp)),
        },
        {
            "label": "WF\naggressive",
            "short": "WF agg",
            "tasks": _dedup(_all_tasks(wf_stop_agg, wf_brk_agg, agg_sp, agg_bp)),
        },
        {
            "label": "NOLIMIT WF\n(株価制限なし)",
            "short": "NOLIMIT",
            "tasks": _dedup(_all_tasks(nl_stop, nl_brk, con_sp, con_bp)),
        },
        {
            "label": "WF+既存\n統合",
            "short": "統合",
            "tasks": _dedup(_all_tasks(mg_stop, mg_brk, con_sp, con_bp)),
        },
    ]
    return configs


# ── 1銘柄バックテスト ──────────────────────────────────────────────────
def _run_one(sym, name, strat, days, fn, em, sm, tm):
    try:
        df = fetch(sym, days + 60)
        if df is None or len(df) < 10:
            return None
        return run_limit_backtest(sym, name, df, fn, em, sm, tm, days, strat, entry_type="stop")
    except Exception as e:
        print(f"\n  [ERROR] {sym} {strat}: {e}")
        return None


def _run_config(tasks: list, workers: int, label: str) -> list[dict]:
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, *t): t for t in tasks}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                results.append(r)
            print(f"\r  [{label}] {i}/{len(tasks)} ...", end="", flush=True)
    print()
    return results


# ── 集計 ──────────────────────────────────────────────────────────────
def _aggregate(results: list[dict]) -> dict:
    trades = wins = 0
    gross_p = gross_l = total_pnl = 0.0
    for r in results:
        trades    += r.get("trades", 0)
        wins      += r.get("wins", 0)
        total_pnl += r.get("total_pnl", 0)
        for t in r.get("trade_log", []):
            if t["pnl"] > 0:
                gross_p += t["pnl"]
            else:
                gross_l += abs(t["pnl"])
    pf       = gross_p / gross_l if gross_l > 0 else float("inf")
    win_rate = wins / trades * 100 if trades > 0 else 0.0
    return dict(trades=trades, wins=wins, win_rate=win_rate, pf=pf,
                total_pnl=total_pnl, gross_p=gross_p, gross_l=gross_l)


def _by_strategy(results: list[dict]) -> dict[str, dict]:
    """戦略ごとに集計。"""
    grouped: dict[str, list] = defaultdict(list)
    for r in results:
        strat = r.get("strategy_name") or r.get("strategy", "?")
        grouped[strat].append(r)
    return {s: _aggregate(v) for s, v in grouped.items()}


# ── テキスト出力 ──────────────────────────────────────────────────────
def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def print_summary(configs: list[dict], data: list[dict[str, dict]], days: int):
    """configs × 全戦略 の集計テキスト出力。"""
    labels = [c["short"] for c in configs]
    col = 14

    print(f"\n{'='*70}")
    print(f"スクリプト比較  期間: {days}日")
    print(f"{'='*70}")

    for strat in ALL_STRATEGIES + ["全合計"]:
        print(f"\n【{strat}】")
        print(f"  {'スクリプト':12s}  {'取引':>4}  {'勝率':>5}  {'PF':>5}  {'損益':>10}")
        print(f"  {'-'*45}")
        for cfg, strat_data in zip(configs, data):
            if strat == "全合計":
                # 全戦略の集計値をマージ
                gp = gl = tot = 0.0
                tr = wi = 0
                for s in ALL_STRATEGIES:
                    a = strat_data.get(s) or {}
                    tr  += a.get("trades", 0)
                    wi  += a.get("wins", 0)
                    tot += a.get("total_pnl", 0.0)
                    gp  += a.get("gross_p", 0.0)
                    gl  += a.get("gross_l", 0.0)
                agg = dict(trades=tr, wins=wi,
                           win_rate=wi/tr*100 if tr > 0 else 0.0,
                           pf=gp/gl if gl > 0 else float("inf"),
                           total_pnl=tot)
            else:
                agg = strat_data.get(strat) or {}
            trades    = agg.get("trades", 0)
            win_rate  = agg.get("win_rate", 0.0)
            pf        = agg.get("pf", 0.0)
            total_pnl = agg.get("total_pnl", 0.0)
            if trades == 0:
                row = f"  {cfg['short']:12s}  {'--':>4}  {'--':>5}  {'--':>5}  {'--':>10}"
            else:
                pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
                row = (f"  {cfg['short']:12s}"
                       f"  {trades:>4}"
                       f"  {win_rate:>5.1f}%"
                       f"  {pf_s:>5}"
                       f"  {total_pnl:>+10,.0f}")
            print(row)


# ── HTML 生成 ─────────────────────────────────────────────────────────
_CSS = """
body{margin:0;padding:20px;background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;font-size:13px}
h1{color:#f1f5f9;font-size:18px;margin-bottom:4px}
.subtitle{color:#94a3b8;font-size:12px;margin-bottom:24px}
h2{color:#7dd3fc;font-size:14px;margin:24px 0 8px;padding-left:4px;border-left:3px solid #0ea5e9}
table{border-collapse:collapse;width:100%;margin-bottom:8px}
th{background:#1e293b;color:#94a3b8;font-weight:600;padding:7px 10px;text-align:center;border:1px solid #334155;font-size:11px}
td{padding:6px 10px;border:1px solid #1e293b;text-align:right;white-space:nowrap}
td.label{text-align:left;color:#cbd5e1;white-space:pre}
tr:hover td{background:#1e293b}
.pos{color:#4ade80}.neg{color:#f87171}.zero{color:#94a3b8}
.inf{color:#fbbf24}
.best{background:#14532d !important;font-weight:700}
.worst{background:#450a0a !important}
.no-data{color:#475569;text-align:center}
.summary-row td{background:#1e293b;font-weight:700;border-top:2px solid #334155}
"""

def _pnl_class(v: float) -> str:
    if v > 0:   return "pos"
    if v < 0:   return "neg"
    return "zero"

def _pf_class(v: float) -> str:
    if v == float("inf"): return "inf"
    if v >= 1.5:  return "pos"
    if v < 1.0:   return "neg"
    return "zero"


def _strat_table(strat: str, configs: list[dict], data: list[dict[str, dict]]) -> str:
    """1戦略の比較テーブルHTML。"""
    rows = []
    pnl_vals = []
    for cfg in configs:
        agg = data[configs.index(cfg)].get(strat)
        pnl_vals.append(agg["total_pnl"] if agg and agg["trades"] > 0 else None)

    # best/worst 損益
    valid_pnl = [v for v in pnl_vals if v is not None]
    best_pnl  = max(valid_pnl) if valid_pnl else None
    worst_pnl = min(valid_pnl) if valid_pnl else None

    for i, cfg in enumerate(configs):
        agg = data[i].get(strat)
        if not agg or agg["trades"] == 0:
            rows.append(
                f'<tr><td class="label">{cfg["label"]}</td>'
                f'<td colspan="5" class="no-data">データなし</td></tr>'
            )
            continue
        t  = agg["trades"]
        w  = agg["wins"]
        wr = agg["win_rate"]
        pf = agg["pf"]
        pn = agg["total_pnl"]
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        pnl_cls = _pnl_class(pn)
        pf_cls  = _pf_class(pf)
        best_cls  = ' class="best"'  if pn == best_pnl  else ""
        worst_cls = ' class="worst"' if pn == worst_pnl else ""
        rows.append(
            f'<tr>'
            f'<td class="label">{cfg["label"]}</td>'
            f'<td>{t}</td>'
            f'<td>{w}</td>'
            f'<td class="{_pnl_class(wr-50)}">{wr:.1f}%</td>'
            f'<td class="{pf_cls}">{pf_s}</td>'
            f'<td class="{pnl_cls}"{best_cls}{worst_cls}>{pn:+,.0f}円</td>'
            f'</tr>'
        )

    rows_html = "\n".join(rows)
    return f"""
<h2>{strat}</h2>
<table>
  <thead>
    <tr>
      <th style="text-align:left;width:140px">スクリプト</th>
      <th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益</th>
    </tr>
  </thead>
  <tbody>
{rows_html}
  </tbody>
</table>"""


def _build_html(configs: list[dict], data: list[dict[str, dict]], days: int) -> str:
    strat_tables = "\n".join(_strat_table(s, configs, data) for s in ALL_STRATEGIES)

    # 全戦略合計テーブル
    total_rows = []
    for i, cfg in enumerate(configs):
        t = w = 0
        gp = gl = pn = 0.0
        for strat in ALL_STRATEGIES:
            a = data[i].get(strat) or {}
            t  += a.get("trades", 0)
            w  += a.get("wins", 0)
            pn += a.get("total_pnl", 0.0)
            gp += a.get("gross_p", 0.0)
            gl += a.get("gross_l", 0.0)
        wr = w / t * 100 if t > 0 else 0.0
        pf = gp / gl if gl > 0 else float("inf")
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        pnl_cls = _pnl_class(pn)
        pf_cls  = _pf_class(pf)
        total_rows.append(
            f'<tr class="summary-row">'
            f'<td class="label">{cfg["label"]}</td>'
            f'<td>{t}</td><td>{w}</td>'
            f'<td class="{_pnl_class(wr-50)}">{wr:.1f}%</td>'
            f'<td class="{pf_cls}">{pf_s}</td>'
            f'<td class="{pnl_cls}">{pn:+,.0f}円</td>'
            f'</tr>'
        )
    total_html = "\n".join(total_rows)
    total_section = f"""
<h2>全戦略合計</h2>
<table>
  <thead>
    <tr>
      <th style="text-align:left;width:140px">スクリプト</th>
      <th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益</th>
    </tr>
  </thead>
  <tbody>
{total_html}
  </tbody>
</table>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8">
<title>スクリプト比較 {date.today()}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>スクリプト × 戦略 比較バックテスト</h1>
<div class="subtitle">期間: {days}日 ／ 指値上限: +3% (現行) ／ 実行日: {date.today()}</div>
{total_section}
{strat_tables}
</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────────────
def _parse():
    p = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                description=__doc__)
    p.add_argument("--days",       type=int, default=365)
    p.add_argument("--workers",    type=int, default=WORKERS)
    p.add_argument("--html",       action="store_true")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


def main():
    args = _parse()
    configs = _build_configs(args.days)

    print(f"スクリプト比較  期間: {args.days}日")
    for c in configs:
        print(f"  {c['short']:14s}: {len(c['tasks'])}銘柄")
    print()

    # 全設定を順番に実行
    all_data: list[dict[str, dict]] = []
    for cfg in configs:
        label = cfg["short"]
        tasks = cfg["tasks"]
        if not tasks:
            print(f"  [{label}] スキップ (銘柄なし)")
            all_data.append({})
            continue
        print(f"▶ [{label}] バックテスト中… ({len(tasks)}銘柄)")
        results = _run_config(tasks, args.workers, label)
        strat_data = _by_strategy(results)
        all_data.append(strat_data)

    print_summary(configs, all_data, args.days)

    if args.html:
        html  = _build_html(configs, all_data, args.days)
        fname = f"compare_scripts_{date.today().isoformat()}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML出力: {fname}")
        if not args.no_browser:
            open_html(f"file://{os.path.abspath(fname)}")


if __name__ == "__main__":
    main()
