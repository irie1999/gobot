"""
daytrade_compare.py  ―  3戦略一括実行 & 比較レポート
==================================================================
ORB / VWAP Pullback / Volume Surge の3戦略を一括バックテストし、
銘柄別×戦略別の比較HTMLレポートを生成してブラウザで自動表示する。

【使い方】
  python daytrade_compare.py                          # デフォルト60銘柄・60日
  python daytrade_compare.py --source local --days 730  # ローカル2年
  python daytrade_compare.py --days 30                # 30日
  python daytrade_compare.py 7203.T 9984.T 8306.T     # 個別銘柄
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, timedelta, time as dtime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day

# ── 戦略モジュール import ────────────────────────────────────
from daytrade_orb import (
    backtest_orb_day, backtest_symbol as orb_backtest_symbol,
    _calc_stats as orb_calc_stats, _empty_stats,
    OR_MINUTES, TARGET_K,
)
from daytrade_vwap import (
    backtest_vwap_day, backtest_symbol as vwap_backtest_symbol,
    _calc_stats as vwap_calc_stats,
    STOP_PCT, TARGET_R as VWAP_TARGET_R, PULLBACK_TOL,
)
from daytrade_volsurge import (
    backtest_volsurge_day, backtest_symbol as volsurge_backtest_symbol,
    _calc_stats as volsurge_calc_stats,
    VOL_MULT, VOL_LOOKBACK, BREAK_LOOKBACK,
    TARGET_R as VOL_TARGET_R, MIN_BODY_PCT, STOP_BUFFER_PCT,
)

JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = DAYTRADE_SYMBOLS


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────
# 3戦略一括バックテスト
# ─────────────────────────────────────────────────────────────

def run_all_strategies(fetched: dict[str, pd.DataFrame],
                       targets: list[tuple[str, str]]) -> dict[str, list[dict]]:
    """3戦略を全銘柄に適用し、結果を返す。"""
    results = {"ORB": [], "VWAP": [], "VolSurge": []}

    total = len(targets)
    for i, (sym, name) in enumerate(targets, 1):
        if sym not in fetched:
            continue
        df = fetched[sym]

        # ORB
        r = orb_backtest_symbol(sym, name, df, TARGET_K, OR_MINUTES)
        if r:
            results["ORB"].append(r)

        # VWAP
        r = vwap_backtest_symbol(sym, name, df, STOP_PCT, VWAP_TARGET_R, PULLBACK_TOL)
        if r:
            results["VWAP"].append(r)

        # VolSurge
        r = volsurge_backtest_symbol(
            sym, name, df, VOL_MULT, VOL_LOOKBACK,
            BREAK_LOOKBACK, VOL_TARGET_R, MIN_BODY_PCT, STOP_BUFFER_PCT)
        if r:
            results["VolSurge"].append(r)

        if i % 10 == 0 or i == total:
            print(f"  {i}/{total} 銘柄完了", flush=True)

    return results


# ─────────────────────────────────────────────────────────────
# コンソールレポート
# ─────────────────────────────────────────────────────────────

def print_comparison(results: dict[str, list[dict]], days: int) -> None:
    W = 88
    print("=" * W)
    print(f"  デイトレ3戦略比較  — 直近{days}日")
    print("=" * W)

    print(f"\n{'戦略':<12} {'取引':>5} {'勝率':>6} {'PF':>6} "
          f"{'総損益':>14} {'平均利益':>10} {'平均損失':>10} {'最大DD':>8}")
    print("-" * W)

    for strat_name, items in results.items():
        all_trades = []
        for item in items:
            all_trades.extend(item.get("trades", []))
        s = orb_calc_stats(all_trades) if all_trades else _empty_stats()
        cls = "+" if s["total_pnl"] >= 0 else ""
        print(f"  {strat_name:<12} {s['n']:>5} {s['win_rate']:>5.1f}% "
              f"{_pf_str(s['pf']):>6} {s['total_pnl']:>+14,.0f} "
              f"{s['avg_win']:>+10,.0f} {s['avg_loss']:>+10,.0f} "
              f"{s['max_dd']:>+7.2f}%")
    print("=" * W)


# ─────────────────────────────────────────────────────────────
# HTML レポート
# ─────────────────────────────────────────────────────────────

def build_comparison_html(results: dict[str, list[dict]], days: int,
                          source: str) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # 戦略別サマリー
    strat_summaries = {}
    for strat_name, items in results.items():
        all_trades = []
        for item in items:
            all_trades.extend(item.get("trades", []))
        strat_summaries[strat_name] = orb_calc_stats(all_trades) if all_trades else _empty_stats()

    strat_colors = {"ORB": "#fbbf24", "VWAP": "#a78bfa", "VolSurge": "#fb923c"}

    # 戦略サマリーテーブル
    summary_rows = ""
    for strat, s in strat_summaries.items():
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        color = strat_colors.get(strat, "#e2e8f0")
        summary_rows += f"""
        <tr>
          <td style="color:{color};font-weight:700">{strat}</td>
          <td>{s['n']}</td><td>{s['win_rate']:.1f}%</td>
          <td>{_pf_str(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}円</td>
          <td class="profit">{s['avg_win']:+,.0f}</td>
          <td class="loss">{s['avg_loss']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.2f}%</td>
        </tr>"""

    # 銘柄別×戦略別テーブル
    all_symbols = {}
    for strat_name, items in results.items():
        for item in items:
            sym = item["symbol"]
            if sym not in all_symbols:
                all_symbols[sym] = {"name": item["name"]}
            all_symbols[sym][strat_name] = item["stats"]

    symbol_rows = ""
    sorted_syms = sorted(all_symbols.keys(),
                         key=lambda s: sum(
                             all_symbols[s].get(st, {}).get("total_pnl", 0)
                             for st in ["ORB", "VWAP", "VolSurge"]),
                         reverse=True)

    for sym in sorted_syms:
        info = all_symbols[sym]
        name = info["name"]
        total_pnl = sum(info.get(st, {}).get("total_pnl", 0)
                        for st in ["ORB", "VWAP", "VolSurge"])
        total_cls = "profit" if total_pnl >= 0 else "loss"

        cells = ""
        for strat in ["ORB", "VWAP", "VolSurge"]:
            s = info.get(strat)
            if s and s.get("n", 0) > 0:
                cls = "profit" if s["total_pnl"] >= 0 else "loss"
                cells += (f'<td>{s["n"]}</td>'
                          f'<td>{s["win_rate"]:.0f}%</td>'
                          f'<td>{_pf_str(s["pf"])}</td>'
                          f'<td class="{cls}">{s["total_pnl"]:+,.0f}</td>')
            else:
                cells += "<td>-</td><td>-</td><td>-</td><td>-</td>"

        symbol_rows += f"""
        <tr>
          <td class="sym">{sym}<br><small>{name}</small></td>
          {cells}
          <td class="{total_cls}" style="font-weight:700">{total_pnl:+,.0f}</td>
        </tr>"""

    # 戦略別サブヘッダー
    strat_headers = ""
    strat_subheads = ""
    for strat in ["ORB", "VWAP", "VolSurge"]:
        color = strat_colors.get(strat, "#e2e8f0")
        strat_headers += f'<th colspan="4" style="color:{color}">{strat}</th>'
        strat_subheads += '<th>N</th><th>WR</th><th>PF</th><th>損益</th>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>デイトレ3戦略比較 — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; min-width:110px; }}
  .profit {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  .summary-box {{ background:#1e293b; padding:16px; border-radius:8px; margin-bottom:16px; display:flex; gap:32px; flex-wrap:wrap; }}
  .summary-item {{ text-align:center; }}
  .summary-item .label {{ color:#94a3b8; font-size:0.8rem; }}
  .summary-item .value {{ font-size:1.4rem; font-weight:700; }}
</style>
</head>
<body>
<h1>デイトレ 3戦略比較レポート</h1>
<p class="subtitle">
  生成: {today} ／ 期間: 直近{days}日 ／ データソース: {source}<br>
  戦略: <span style="color:#fbbf24">ORB</span> /
        <span style="color:#a78bfa">VWAP Pullback</span> /
        <span style="color:#fb923c">Volume Surge</span>
</p>

<div class="summary-box">
  {"".join(f'''
  <div class="summary-item">
    <div class="label">{strat}</div>
    <div class="value {'profit' if s['total_pnl']>=0 else 'loss'}">{s['total_pnl']:+,.0f}円</div>
    <div class="label">PF {_pf_str(s['pf'])} / WR {s['win_rate']:.0f}% / DD {s['max_dd']:+.1f}%</div>
  </div>''' for strat, s in strat_summaries.items())}
</div>

<h2>戦略サマリー</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>取引</th><th>勝率</th><th>PF</th>
    <th>総損益</th><th>平均利益</th><th>平均損失</th><th>最大DD</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>銘柄別 × 戦略別 比較</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">銘柄</th>
      {strat_headers}
      <th rowspan="2">合計損益</th>
    </tr>
    <tr>{strat_subheads}</tr>
  </thead>
  <tbody>{symbol_rows}</tbody>
</table>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="デイトレ3戦略一括比較バックテスト")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--budget", type=int, default=0,
                        help="投資資金 (円)。100株で買える銘柄のみに絞り込み "
                             "(例: --budget 600000 → 株価6000円以下)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        print("[info] yfinance 5分足は最大60日 → 60日に調整", file=sys.stderr)
        args.days = 60

    if args.symbols:
        targets = [(s, s) for s in args.symbols]
    else:
        targets = DEFAULT_SYMBOLS

    budget_str = f" / 予算{args.budget:,}円" if args.budget > 0 else ""
    print(f"デイトレ3戦略比較: {len(targets)}銘柄 / {args.days}日 / "
          f"source={args.source}{budget_str}", flush=True)

    # データ取得
    symbols_list = [s for s, _ in targets]
    print(f"データ取得中 ({len(symbols_list)}銘柄)...", flush=True)
    fetched = load_intraday_batch(symbols_list, args.days, source=args.source)
    print(f"  取得成功: {len(fetched)}/{len(symbols_list)}銘柄", flush=True)

    if not fetched:
        print("[ERROR] データが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    # 予算フィルタ: 最新終値 × 100株 が予算以内の銘柄のみ
    if args.budget > 0:
        max_price = args.budget / 100
        before = len(fetched)
        excluded = []
        filtered = {}
        for sym, df in fetched.items():
            latest_close = float(df.iloc[-1]["close"])
            if latest_close <= max_price:
                filtered[sym] = df
            else:
                excluded.append((sym, latest_close))
        fetched = filtered
        targets = [(s, n) for s, n in targets if s in fetched]
        print(f"  予算フィルタ: {before} → {len(fetched)}銘柄 "
              f"(株価{max_price:,.0f}円以下)", flush=True)
        if excluded:
            excluded.sort(key=lambda x: x[1], reverse=True)
            for sym, price in excluded[:5]:
                print(f"    除外: {sym} ({price:,.0f}円)", flush=True)
            if len(excluded) > 5:
                print(f"    ... 他{len(excluded)-5}銘柄", flush=True)

    if not fetched:
        print("[ERROR] 予算内で購入可能な銘柄がありません", file=sys.stderr)
        sys.exit(1)

    # 3戦略実行
    print(f"バックテスト実行中 ({len(fetched)}銘柄)...", flush=True)
    results = run_all_strategies(fetched, targets)

    # コンソール
    print_comparison(results, args.days)

    # HTML
    out = Path(f"daytrade_compare_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(
        build_comparison_html(results, args.days, args.source),
        encoding="utf-8",
    )
    print(f"\nHTMLレポート: {out.resolve()}")

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
