"""
daytrade_compare.py  ―  5戦略一括実行 & 比較レポート
==================================================================
ORB / VWAP / VolSurge / Pivot / RSI の5戦略を一括バックテストし、
銘柄別×戦略別の比較HTMLレポートを生成してブラウザで自動表示する。

【使い方】
  python daytrade_compare.py                                    # 60銘柄・60日
  python daytrade_compare.py --source yfinance --days 60 --budget 600000
  python daytrade_compare.py --source local --days 730
  python daytrade_compare.py 7203.T 9984.T 8306.T
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
    backtest_symbol as orb_backtest_symbol,
    _calc_stats, _empty_stats, OR_MINUTES, TARGET_K,
)
from daytrade_vwap import (
    backtest_symbol as vwap_backtest_symbol,
    STOP_PCT, TARGET_R as VWAP_TARGET_R, PULLBACK_TOL,
)
from daytrade_volsurge import (
    backtest_symbol as volsurge_backtest_symbol,
    VOL_MULT, VOL_LOOKBACK, BREAK_LOOKBACK,
    TARGET_R as VOL_TARGET_R, MIN_BODY_PCT, STOP_BUFFER_PCT,
)
from daytrade_pivot import (
    backtest_symbol as pivot_backtest_symbol,
)
from daytrade_rsi import (
    backtest_symbol as rsi_backtest_symbol,
)

JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = DAYTRADE_SYMBOLS


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────
# 5戦略一括バックテスト
# ─────────────────────────────────────────────────────────────

BUDGET = 600_000
MAX_RISK = 6_000

STRATEGY_MAP = {
    "ORB": lambda sym, name, df, budget: orb_backtest_symbol(sym, name, df, TARGET_K, OR_MINUTES),
    "VWAP": lambda sym, name, df, budget: vwap_backtest_symbol(sym, name, df, STOP_PCT, VWAP_TARGET_R, PULLBACK_TOL),
    "VolSurge": lambda sym, name, df, budget: volsurge_backtest_symbol(
        sym, name, df, VOL_MULT, VOL_LOOKBACK, BREAK_LOOKBACK, VOL_TARGET_R, MIN_BODY_PCT, STOP_BUFFER_PCT),
    "Pivot": lambda sym, name, df, budget: pivot_backtest_symbol(sym, name, df, budget, MAX_RISK),
    "RSI": lambda sym, name, df, budget: rsi_backtest_symbol(sym, name, df, budget, MAX_RISK),
}


def _process_symbol_from_file(args_tuple):
    """1銘柄を処理 (ファイルから読み込み、メモリ転送なし)。"""
    sym, name, pkl_path, strat_names, budget, days = args_tuple
    import pickle
    from daytrade_data import normalize_minute_df, resample_to_5m
    try:
        raw = pickle.loads(pkl_path.read_bytes())
        df = normalize_minute_df(raw)
        if df.empty:
            return {}
        df = resample_to_5m(df)
        if df.empty:
            return {}
        import pandas as pd
        from datetime import datetime, timedelta, timezone
        cutoff = pd.Timestamp(datetime.now(timezone(timedelta(hours=9))).date()
                              - timedelta(days=days))
        df = df[df.index >= cutoff]
        if len(df) < 50:
            return {}
    except Exception:
        return {}

    results = {}
    for sn in strat_names:
        fn = STRATEGY_MAP.get(sn)
        if fn:
            try:
                r = fn(sym, name, df, budget)
                if r:
                    results[sn] = r
            except Exception:
                pass
    return results


def run_all_strategies(fetched: dict[str, pd.DataFrame],
                       targets: list[tuple[str, str]],
                       budget: int = BUDGET,
                       strat_names: list[str] | None = None,
                       workers: int = 1,
                       days: int = 730) -> dict[str, list[dict]]:
    """指定戦略を全銘柄に適用。workers>1でファイルベース並列処理。"""
    if strat_names is None:
        strat_names = list(STRATEGY_MAP.keys())

    results = {s: [] for s in strat_names}
    total = len([s for s, _ in targets if s in fetched])

    if workers > 1:
        # ファイルベース並列 (メモリ転送なし)
        from multiprocessing import Pool
        from daytrade_data import DATA_DIR, yf_to_jquants
        args_list = []
        for sym, name in targets:
            if sym not in fetched:
                continue
            jq = yf_to_jquants(sym)
            pkl = DATA_DIR / f"{jq}.pkl"
            if pkl.exists():
                args_list.append((sym, name, pkl, strat_names, budget, days))

        with Pool(workers) as pool:
            for i, res in enumerate(pool.imap_unordered(
                    _process_symbol_from_file, args_list, chunksize=20), 1):
                for sn, r in res.items():
                    results[sn].append(r)
                if i % 500 == 0 or i == len(args_list):
                    print(f"  {i}/{len(args_list)} 銘柄完了", flush=True)
    else:
        valid = [(sym, name) for sym, name in targets if sym in fetched]
        for i, (sym, name) in enumerate(valid, 1):
            df = fetched[sym]
            for sn in strat_names:
                fn = STRATEGY_MAP.get(sn)
                if fn:
                    try:
                        r = fn(sym, name, df, budget)
                        if r:
                            results[sn].append(r)
                    except Exception:
                        pass
            if i % 500 == 0 or i == len(valid):
                print(f"  {i}/{len(valid)} 銘柄完了", flush=True)

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
        s = _calc_stats(all_trades) if all_trades else _empty_stats()
        cls = "+" if s["total_pnl"] >= 0 else ""
        print(f"  {strat_name:<12} {s['n']:>5} {s['win_rate']:>5.1f}% "
              f"{_pf_str(s['pf']):>6} {s['total_pnl']:>+14,.0f} "
              f"{s['avg_win']:>+10,.0f} {s['avg_loss']:>+10,.0f} "
              f"{s['max_dd']:>+7.2f}%")
    print("=" * W)


# ─────────────────────────────────────────────────────────────
# HTML: 個別トレード明細
# ─────────────────────────────────────────────────────────────

def _build_trade_details(results: dict[str, list[dict]],
                         strat_colors: dict[str, str]) -> str:
    """銘柄ごと×戦略ごとの個別トレード明細HTMLを生成。"""
    # 銘柄を集約
    sym_trades: dict[str, list[tuple[str, dict]]] = {}
    for strat_name, items in results.items():
        for item in items:
            sym = item["symbol"]
            name = item["name"]
            key = f"{sym} {name}"
            if key not in sym_trades:
                sym_trades[key] = []
            for t in item.get("trades", []):
                sym_trades[key].append((strat_name, t))

    # 銘柄別にトレードを時系列ソート
    sections = ""
    for sym_key in sorted(sym_trades.keys()):
        trades = sym_trades[sym_key]
        if not trades:
            continue

        # 戦略別の損益サマリー
        strat_pnl = {}
        for strat, t in trades:
            strat_pnl[strat] = strat_pnl.get(strat, 0) + t["pnl"]
        total_pnl = sum(strat_pnl.values())
        total_cls = "profit" if total_pnl >= 0 else "loss"

        pnl_badges = " ".join(
            f'<span style="color:{strat_colors.get(s,"#e2e8f0")}">'
            f'{s} {p:+,.0f}</span>'
            for s, p in strat_pnl.items()
        )

        # トレード行 (時系列ソート)
        trades_sorted = sorted(trades, key=lambda x: str(x[1].get("entry_dt", "")))
        trows = ""
        for strat, t in trades_sorted:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            color = strat_colors.get(strat, "#e2e8f0")

            entry_dt = t.get("entry_dt")
            exit_dt = t.get("exit_dt")
            if hasattr(entry_dt, "strftime"):
                e_date = entry_dt.strftime("%Y-%m-%d")
                e_time = entry_dt.strftime("%H:%M")
            else:
                e_date = str(entry_dt)[:10] if entry_dt else "-"
                e_time = str(entry_dt)[11:16] if entry_dt else "-"
            if hasattr(exit_dt, "strftime"):
                x_time = exit_dt.strftime("%H:%M")
            else:
                x_time = str(exit_dt)[11:16] if exit_dt else "-"

            reason = t.get("reason", "-")
            entry_p = t.get("entry_p", 0)
            exit_p = t.get("exit_p", 0)
            stop_p = t.get("stop_p", t.get("order_stop", 0))
            target_p = t.get("target_p", 0)

            trows += f"""
              <tr>
                <td style="color:{color};font-weight:600">{strat}</td>
                <td>{e_date}</td>
                <td>{e_time}</td>
                <td>{x_time}</td>
                <td>{entry_p:,.1f}</td>
                <td class="loss">{stop_p:,.1f}</td>
                <td class="profit">{target_p:,.1f}</td>
                <td>{exit_p:,.1f}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t.get('pct', 0):+.2f}%</td>
                <td>{reason}</td>
              </tr>"""

        sections += f"""
      <details class="trade-section">
        <summary>
          <strong>{sym_key}</strong>
          &nbsp; <span class="{total_cls}">{total_pnl:+,.0f}円</span>
          &nbsp; <small>{len(trades)}取引</small>
          &nbsp; {pnl_badges}
        </summary>
        <table>
          <thead><tr>
            <th>戦略</th><th>日付</th><th>Entry</th><th>Exit</th>
            <th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
            <th>損益</th><th>%</th><th>理由</th>
          </tr></thead>
          <tbody>{trows}</tbody>
        </table>
      </details>"""

    return sections


# ─────────────────────────────────────────────────────────────
# HTML: メインレポート
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
        strat_summaries[strat_name] = _calc_stats(all_trades) if all_trades else _empty_stats()

    strat_colors = {"ORB": "#fbbf24", "VWAP": "#a78bfa", "VolSurge": "#fb923c",
                     "Pivot": "#22d3ee", "RSI": "#f472b6"}

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
            # stats がない場合 (Pivot/RSI) は trades から計算
            if "stats" in item:
                all_symbols[sym][strat_name] = item["stats"]
            else:
                all_symbols[sym][strat_name] = _calc_stats(item.get("trades", []))

    symbol_rows = ""
    sorted_syms = sorted(all_symbols.keys(),
                         key=lambda s: sum(
                             all_symbols[s].get(st, {}).get("total_pnl", 0)
                             for st in ["ORB", "VWAP", "VolSurge", "Pivot", "RSI"]),
                         reverse=True)

    for sym in sorted_syms:
        info = all_symbols[sym]
        name = info["name"]
        total_pnl = sum(info.get(st, {}).get("total_pnl", 0)
                        for st in ["ORB", "VWAP", "VolSurge", "Pivot", "RSI"])
        total_cls = "profit" if total_pnl >= 0 else "loss"

        cells = ""
        for strat in ["ORB", "VWAP", "VolSurge", "Pivot", "RSI"]:
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
    for strat in ["ORB", "VWAP", "VolSurge", "Pivot", "RSI"]:
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
  details.trade-section {{ margin-bottom:8px; background:#1e293b; border-radius:6px; padding:0; }}
  details.trade-section summary {{ cursor:pointer; padding:10px 14px; font-size:0.9rem; list-style:none; }}
  details.trade-section summary::-webkit-details-marker {{ display:none; }}
  details.trade-section summary::before {{ content:"▶ "; font-size:0.7rem; color:#94a3b8; }}
  details.trade-section[open] summary::before {{ content:"▼ "; }}
  details.trade-section[open] {{ padding-bottom:8px; }}
  details.trade-section table {{ margin:0 14px 8px; width:calc(100% - 28px); }}
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

<h2>個別トレード一覧</h2>
{_build_trade_details(results, strat_colors)}

</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="デイトレ5戦略一括比較バックテスト")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--budget", type=int, default=0,
                        help="投資資金 (円)。100株で買える銘柄のみに絞り込み "
                             "(例: --budget 600000 → 株価6000円以下)")
    parser.add_argument("--all-local", action="store_true",
                        help="ローカル保存済みの全銘柄を対象にする (data/minute_5m/)")
    parser.add_argument("--strategy", nargs="*",
                        choices=["ORB", "VWAP", "VolSurge", "Pivot", "RSI"],
                        default=None,
                        help="実行する戦略 (省略=全5戦略、例: --strategy ORB VolSurge)")
    parser.add_argument("--workers", type=int, default=1,
                        help="並列処理数 (例: --workers 4)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        print("[info] yfinance 5分足は最大60日 → 60日に調整", file=sys.stderr)
        args.days = 60

    if args.all_local:
        from daytrade_data import available_local_symbols
        local_codes = available_local_symbols()
        # J-Quants 5桁コード → yfinance 4桁.T に変換
        targets = []
        for code in local_codes:
            if len(code) == 5 and code.endswith("0") and code.isdigit():
                yf = code[:4] + ".T"
            else:
                yf = code + ".T"
            targets.append((yf, yf))
        args.source = "local"
        print(f"ローカル全銘柄モード: {len(targets)}銘柄", flush=True)
    elif args.symbols:
        targets = [(s, s) for s in args.symbols]
    else:
        targets = DEFAULT_SYMBOLS

    budget_str = f" / 予算{args.budget:,}円" if args.budget > 0 else ""
    print(f"デイトレ5戦略比較: {len(targets)}銘柄 / {args.days}日 / "
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

    # 戦略実行
    budget_val = args.budget if args.budget > 0 else BUDGET
    strats = args.strategy or list(STRATEGY_MAP.keys())
    print(f"バックテスト実行中 ({len(fetched)}銘柄 × {len(strats)}戦略 "
          f"/ workers={args.workers})...", flush=True)
    results = run_all_strategies(fetched, targets, budget=budget_val,
                                 strat_names=strats, workers=args.workers,
                                 days=args.days)

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
