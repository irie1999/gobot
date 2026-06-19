"""
analyze_entry_delay.py  ―  エントリー遅延（entry_delay）効果分析
=================================================================
シグナル発生から何日後に注文を受け付けるか（entry_delay=0,1,2,3）を
WATCHLIST全銘柄×全戦略で比較分析し、HTMLレポートを出力する。

【使い方】
  python analyze_entry_delay.py                     # 全戦略365日
  python analyze_entry_delay.py --days 180          # 直近180日
  python analyze_entry_delay.py --workers 8         # 並列数
  python analyze_entry_delay.py --strategies MACD,A7,RSI2
  python analyze_entry_delay.py --no-browser
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)
from check_signals_stop import WATCHLIST as STOP_WL, STRATEGY_PARAMS as STOP_PARAMS
from check_signals_breakout import WATCHLIST as BRK_WL, STRATEGY_PARAMS as BRK_PARAMS

JST = timezone(timedelta(hours=9))
DELAYS = list(range(11))  # 0〜10日後


# ──────────────────────────────────────────────
# 1 銘柄×戦略の遅延別バックテスト
# ──────────────────────────────────────────────

def _run_one(
    symbol: str,
    name: str,
    strategy: str,
    calc_fn,
    em: float,
    sm: float,
    tm: float,
    days: int,
) -> dict[str, Any]:
    """1銘柄×戦略について delay=0,1,2,3 のバックテストを実行して結果を返す。"""
    try:
        df = fetch(symbol, days + 60)  # バッファ込みで fetch
    except Exception as e:
        return {
            "symbol": symbol, "name": name, "strategy": strategy,
            "error": str(e),
            "delays": {},
        }

    results: dict[int, dict] = {}
    for d in DELAYS:
        try:
            r = run_limit_backtest(
                symbol=symbol,
                name=name,
                df=df.copy(),
                calc_fn=calc_fn,
                entry_atr_mult=em,
                stop_atr_mult=sm,
                target_atr_mult=tm,
                backtest_days=days,
                strategy_name=strategy,
                entry_type="stop",
                stop_mode=None,
                max_hold=None,
                entry_delay=d,
                entry_expire=1,  # 各delayで1日のみ有効（当日 or 指定日だけ）
            )
        except Exception as e:
            r = {"error": str(e)}
        results[d] = r

    return {
        "symbol": symbol,
        "name": name,
        "strategy": strategy,
        "delays": results,
    }


# ──────────────────────────────────────────────
# 集計ヘルパー
# ──────────────────────────────────────────────

def _extract_metrics(r: dict) -> tuple[int, float, float, float, float]:
    """(trades, win_rate, pf, total_pnl, avg_hold) を返す。失敗時は (0, 0, 0, 0, 0)。"""
    if not r or "error" in r:
        return 0, 0.0, 0.0, 0.0, 0.0
    trades = r.get("trades", 0)
    if trades == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    win_rate = r.get("win_rate", 0.0) / 100.0  # backtest returns 0-100; normalize to 0-1
    pf = r.get("profit_factor", 0.0)
    total_pnl = r.get("total_pnl", 0.0)
    avg_hold = r.get("avg_hold", 0.0)
    return trades, win_rate, pf, total_pnl, avg_hold


# ──────────────────────────────────────────────
# HTML 生成
# ──────────────────────────────────────────────

_DARK_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #0f172a;
    color: #e2e8f0;
    font-family: 'Segoe UI', 'Noto Sans JP', sans-serif;
    font-size: 13px;
    padding: 24px;
}
h1 { font-size: 22px; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }
h2 { font-size: 16px; font-weight: 600; color: #94a3b8; margin: 24px 0 10px; }
.subtitle { color: #64748b; font-size: 12px; margin-bottom: 24px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 32px; }
th {
    background: #1e293b;
    color: #94a3b8;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 8px 12px;
    text-align: right;
    border-bottom: 1px solid #334155;
}
th:first-child, th:nth-child(2) { text-align: left; }
td {
    padding: 7px 12px;
    border-bottom: 1px solid #1e293b;
    text-align: right;
    color: #cbd5e1;
}
td:first-child, td:nth-child(2) { text-align: left; }
tr:hover td { background: #1e293b; }
.pos  { color: #4ade80; }
.neg  { color: #f87171; }
.best { background: #164e35 !important; color: #4ade80 !important; font-weight: 700; }
.delay-header { color: #60a5fa; font-weight: 600; }
"""


def _fmt_pnl(v: float) -> str:
    s = f"{v:+,.0f}"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">{s}</span>' if cls else s


def _fmt_pct(v: float) -> str:
    s = f"{v*100:.1f}%"
    return s


def _fmt_pf(v: float) -> str:
    if v == 0:
        return "—"
    return f"{v:.2f}"


def build_html(
    all_results: list[dict],
    days: int,
    strategies_filter: list[str] | None,
    run_dt: str,
) -> str:

    # ── サマリー集計 (delay別) ──
    summary: dict[int, dict] = {
        d: {"trades": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0,
            "total_pnl": 0.0, "hold_sum": 0.0, "hold_cnt": 0}
        for d in DELAYS
    }

    # ── 銘柄別テーブル行 ──
    item_rows: list[dict] = []

    for item in all_results:
        sym = item["symbol"]
        nm  = item["name"]
        strat = item["strategy"]
        if strategies_filter and strat not in strategies_filter:
            continue
        if "error" in item:
            continue

        row: dict[str, Any] = {
            "symbol": sym, "name": nm, "strategy": strat,
        }
        best_pnl = None
        best_d = None
        for d in DELAYS:
            r = item["delays"].get(d, {})
            trades, wr, pf, pnl, ah = _extract_metrics(r)
            row[f"d{d}_trades"] = trades
            row[f"d{d}_wr"]     = wr
            row[f"d{d}_pf"]     = pf
            row[f"d{d}_pnl"]    = pnl
            row[f"d{d}_hold"]   = ah

            # summary accumulate
            s = summary[d]
            s["trades"]    += trades
            s["total_pnl"] += pnl
            if trades > 0:
                wins = round(trades * wr)
                s["wins"]       += wins
                # gross_win / gross_loss はバックテスト結果に直接ないので pnl で近似
                if ah > 0:
                    s["hold_sum"] += ah * trades
                    s["hold_cnt"] += trades

            if best_pnl is None or pnl > best_pnl:
                best_pnl = pnl
                best_d   = d

        row["best_d"] = best_d
        item_rows.append(row)

    # ── サマリーテーブル HTML ──
    def _summary_table() -> str:
        rows_html = ""
        for d in DELAYS:
            s = summary[d]
            trades = s["trades"]
            total_pnl = s["total_pnl"]
            if trades > 0:
                wr = s["wins"] / trades
                avg_hold = s["hold_sum"] / s["hold_cnt"] if s["hold_cnt"] > 0 else 0.0
            else:
                wr = 0.0
                avg_hold = 0.0

            label = f"<span class='delay-header'>{d}日後</span>"
            pnl_html = _fmt_pnl(total_pnl)
            rows_html += f"""
            <tr>
              <td>{label}</td>
              <td>{trades:,}</td>
              <td>{_fmt_pct(wr)}</td>
              <td>{avg_hold:.1f}日</td>
              <td>{pnl_html}</td>
            </tr>"""

        return f"""
        <table>
          <thead>
            <tr>
              <th>entry_delay</th>
              <th>件数</th>
              <th>勝率</th>
              <th>平均保有日数</th>
              <th>損益合計</th>
            </tr>
          </thead>
          <tbody>{rows_html}
          </tbody>
        </table>"""

    # ── 銘柄別テーブル HTML ──
    def _detail_table() -> str:
        # 戦略ごとにグループ化
        strats_in_data = sorted({r["strategy"] for r in item_rows})
        html = ""
        for strat in strats_in_data:
            rows = [r for r in item_rows if r["strategy"] == strat]
            # delay=0 の損益でソート（降順）
            rows.sort(key=lambda x: x.get("d0_pnl", 0), reverse=True)

            d_headers = "".join(
                f'<th class="delay-header">{d}日後 PnL</th><th>{d}日後 取引</th><th>{d}日後 勝率</th>'
                for d in DELAYS
            )
            rows_html = ""
            for r in rows:
                best_d = r.get("best_d")
                sym_cell = f"{r['symbol']}<br><small style='color:#64748b'>{r['name']}</small>"
                cells = f"<td>{sym_cell}</td>"
                for d in DELAYS:
                    pnl   = r.get(f"d{d}_pnl", 0.0)
                    trd   = r.get(f"d{d}_trades", 0)
                    wr    = r.get(f"d{d}_wr", 0.0)
                    is_best = (d == best_d)
                    cls = " class='best'" if is_best else ""
                    cells += (
                        f"<td{cls}>{_fmt_pnl(pnl)}</td>"
                        f"<td{cls}>{trd}</td>"
                        f"<td{cls}>{_fmt_pct(wr)}</td>"
                    )
                rows_html += f"<tr>{cells}</tr>\n"

            html += f"""
            <h2>戦略: {strat}</h2>
            <table>
              <thead>
                <tr>
                  <th>銘柄</th>
                  {d_headers}
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>"""
        return html

    summary_html = _summary_table()
    detail_html  = _detail_table()

    total_items = len(item_rows)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Entry Delay 分析 {run_dt}</title>
<style>{_DARK_CSS}</style>
</head>
<body>
<h1>Entry Delay 分析</h1>
<p class="subtitle">
  対象期間: 直近 {days} 日 ／ 銘柄×戦略数: {total_items} ／ 生成: {run_dt}
</p>

<h2>全体サマリー（delay 別）</h2>
{summary_html}

<h2>銘柄別最適遅延テーブル（緑背景 = 各行の最高損益 delay）</h2>
{detail_html}

</body>
</html>"""


# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Entry delay analysis")
    parser.add_argument("--days",       type=int, default=365, help="バックテスト日数（デフォルト365）")
    parser.add_argument("--workers",    type=int, default=_DEFAULT_WORKERS, help="並列数")
    parser.add_argument("--strategies", type=str, default="", help="カンマ区切り戦略名 (例: MACD,RSI2)")
    parser.add_argument("--no-browser", action="store_true", help="HTMLをブラウザで開かない")
    args = parser.parse_args()

    days    = args.days
    workers = args.workers
    strategies_filter: list[str] | None = (
        [s.strip().upper() for s in args.strategies.split(",") if s.strip()]
        if args.strategies else None
    )

    # ── タスクリスト作成 ──
    tasks: list[tuple[str, str, str, Any, float, float, float]] = []

    # STOP WATCHLIST
    seen_stop = set()
    for sym, nm, strat in STOP_WL:
        if strat not in STOP_PARAMS:
            continue
        if strategies_filter and strat not in strategies_filter:
            continue
        key = (sym, strat)
        if key in seen_stop:
            continue
        seen_stop.add(key)
        calc_fn, em, sm, tm = STOP_PARAMS[strat]
        tasks.append((sym, nm, strat, calc_fn, em, sm, tm))

    # BREAKOUT WATCHLIST
    seen_brk = set()
    for sym, nm, strat in BRK_WL:
        if strat not in BRK_PARAMS:
            continue
        if strategies_filter and strat not in strategies_filter:
            continue
        key = (sym, strat)
        if key in seen_brk:
            continue
        seen_brk.add(key)
        calc_fn, em, sm, tm = BRK_PARAMS[strat]
        tasks.append((sym, nm, strat, calc_fn, em, sm, tm))

    print(f"Entry delay 分析開始: {len(tasks)} タスク, {days}日, workers={workers}", flush=True)

    all_results: list[dict] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_run_one, sym, nm, strat, calc_fn, em, sm, tm, days): (sym, strat)
            for sym, nm, strat, calc_fn, em, sm, tm in tasks
        }
        for fut in as_completed(futures):
            sym, strat = futures[fut]
            done += 1
            try:
                result = fut.result()
                all_results.append(result)
            except Exception as e:
                print(f"  ERROR {sym} {strat}: {e}", flush=True)
            if done % 10 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} 完了", flush=True)

    run_dt = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    today_str = date.today().strftime("%Y-%m-%d")
    out_path = Path(f"entry_delay_analysis_{today_str}.html")

    html = build_html(all_results, days, strategies_filter, run_dt)
    out_path.write_text(html, encoding="utf-8")
    print(f"出力: {out_path.resolve()}", flush=True)

    if not args.no_browser:
        try:
            from _open_html import open_html
            open_html(str(out_path.resolve()))
        except ImportError:
            webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
