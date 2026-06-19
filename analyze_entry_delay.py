"""
analyze_entry_delay.py  ―  エントリー遅延（entry_delay）効果分析
=================================================================
【使い方】
  python analyze_entry_delay.py --max-price 6000 --min-price 1000
  python analyze_entry_delay.py --short
  python analyze_entry_delay.py --min-bt-score 60   # BTスコア60以上のみ
  python analyze_entry_delay.py --days 180
  python analyze_entry_delay.py --workers 8
  python analyze_entry_delay.py --no-browser
"""

from __future__ import annotations

import argparse
import csv
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)

JST = timezone(timedelta(hours=9))
DELAYS = list(range(11))  # 0〜10日後
HOLDOUT_CONFIGS = [30, 60, 90, 120, 150, 180]
BT_PERIODS = [30, 60, 90, 120, 150, 180]  # BTスコア計算用スライス


# ──────────────────────────────────────────────
# WATCHLIST ローダー
# ──────────────────────────────────────────────

def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))


def _find_csv(strategy: str, holdout_days: int, wf_dir: Path,
              mode: str = "conservative") -> Path | None:
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    cands = sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_holdout{holdout_days}d_*.csv"), reverse=True)
    if cands:
        return cands[0]
    fallback = [f for f in sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_*.csv"), reverse=True)
                if "holdout" not in f.name]
    return fallback[0] if fallback else None


def _load_wl_from_csv(csv_path: Path, strategy: str,
                       max_price: float = 0.0, min_price: float = 0.0,
                       per_strategy: int = 10) -> list[tuple]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    filtered = [r for r in rows if (
        _float(r.get("total_test_pnl", 0)) > 0
        and _float(r.get("max_drawdown_pct", 999)) <= 15.0
        and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
        and (min_price <= 0 or _float(r.get("latest_price", 0)) >= min_price)
    )]
    filtered.sort(key=_composite_score, reverse=True)
    return [(r.get("symbol", ""), r.get("name", ""), strategy)
            for r in filtered[:per_strategy] if r.get("symbol")]


def build_tasks(is_short: bool, max_price: float, min_price: float,
                strategies_filter: list[str] | None, wf_dir: Path) -> list[tuple]:
    if is_short:
        stop_strats = ["A7_S", "RSI2_S", "MACD_S"]
        brk_strats  = ["DON_S", "MOM_S", "GAP_S"]
        import check_signals_short          as _stop_mod
        import check_signals_short_breakout as _brk_mod
    else:
        stop_strats = ["MACD", "A7", "RSI2"]
        brk_strats  = ["DON", "VOL", "MOM"]
        import check_signals_stop     as _stop_mod
        import check_signals_breakout as _brk_mod

    entry_type = "stop_sell" if is_short else "stop"
    seen: set[tuple[str, str]] = set()
    combined_wl: list[tuple] = []

    for ho_days in HOLDOUT_CONFIGS:
        for mode in ("conservative", "aggressive"):
            for strat in stop_strats + brk_strats:
                if strategies_filter and strat not in strategies_filter:
                    continue
                p = _find_csv(strat, ho_days, wf_dir, mode)
                if p:
                    for sym, nm, st in _load_wl_from_csv(p, strat, max_price, min_price):
                        if (sym, st) not in seen:
                            seen.add((sym, st))
                            combined_wl.append((sym, nm, st))

    if not combined_wl:
        print("[INFO] WFスキャンCSVなし → 標準WATCHLISTを使用")
        for sym, nm, strat in list(_stop_mod.WATCHLIST) + list(_brk_mod.WATCHLIST):
            if strategies_filter and strat not in strategies_filter:
                continue
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                combined_wl.append((sym, nm, strat))

    all_params = {**_stop_mod.STRATEGY_PARAMS, **_brk_mod.STRATEGY_PARAMS}
    tasks = []
    for sym, nm, strat in combined_wl:
        if strat not in all_params:
            continue
        calc_fn, em, sm, tm = all_params[strat]
        tasks.append((sym, nm, strat, calc_fn, em, sm, tm, entry_type))
    return tasks


# ──────────────────────────────────────────────
# BTスコア計算
# ──────────────────────────────────────────────

def _compute_bt_score(symbol, name, strategy, calc_fn, em, sm, tm, entry_type, df) -> float:
    """check_signals_stop.calc_recommend_score と同じ6期間スライス方式でBTスコアを計算。"""
    period_results = []
    for p in BT_PERIODS:
        try:
            r = run_limit_backtest(
                symbol=symbol, name=name, df=df.copy(),
                calc_fn=calc_fn, entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
                backtest_days=p, strategy_name=strategy,
                entry_type=entry_type, stop_mode=None, max_hold=None,
            )
            if r.get("trades", 0) > 0:
                period_results.append(r)
        except Exception:
            pass

    if not period_results:
        return 0.0

    n = len(period_results)
    avg_wr  = sum(r["win_rate"] for r in period_results) / n  # 0-100スケール
    pf_vals = [min(r.get("profit_factor", 0) or 0, 10) if r.get("profit_factor") != float("inf")
               else 10 for r in period_results]
    avg_pf  = sum(pf_vals) / n
    stable  = sum(1 for r in period_results if r["total_pnl"] > 0) / n
    t_trades = sum(r["trades"] for r in period_results)

    score = round(
        avg_wr * 0.4
        + (avg_pf / 10) * 30
        + stable * 20
        + min(t_trades / 20, 1) * 10
    )
    return float(max(0, min(100, score)))


# ──────────────────────────────────────────────
# 1 銘柄×戦略の遅延別バックテスト
# ──────────────────────────────────────────────

def _run_one(
    symbol: str, name: str, strategy: str,
    calc_fn, em: float, sm: float, tm: float,
    entry_type: str, days: int,
) -> dict[str, Any]:
    try:
        df = fetch(symbol, days + 60)
    except Exception as e:
        return {"symbol": symbol, "name": name, "strategy": strategy,
                "error": str(e), "delays": {}, "bt_score": 0.0}
    if df is None:
        return {"symbol": symbol, "name": name, "strategy": strategy,
                "error": "データ取得失敗", "delays": {}, "bt_score": 0.0}

    # BTスコア計算（6期間スライス）
    bt_score = _compute_bt_score(symbol, name, strategy, calc_fn, em, sm, tm, entry_type, df)

    results: dict[int, dict] = {}
    for d in DELAYS:
        try:
            r = run_limit_backtest(
                symbol=symbol, name=name, df=df.copy(),
                calc_fn=calc_fn, entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
                backtest_days=days, strategy_name=strategy,
                entry_type=entry_type, stop_mode=None, max_hold=None,
                entry_delay=d, entry_expire=1,
            )
        except Exception as e:
            r = {"error": str(e)}
        results[d] = r

    return {"symbol": symbol, "name": name, "strategy": strategy,
            "delays": results, "bt_score": bt_score}


# ──────────────────────────────────────────────
# 集計ヘルパー
# ──────────────────────────────────────────────

def _extract_metrics(r: dict) -> tuple[int, float, float, float, float, float, float]:
    """(trades, win_rate[0-1], pf, total_pnl, avg_hold, gross_win, gross_loss)"""
    if not r or "error" in r:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    trades = r.get("trades", 0)
    if trades == 0:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    win_rate  = r.get("win_rate", 0.0) / 100.0
    pf        = r.get("profit_factor", 0.0)
    total_pnl = r.get("total_pnl", 0.0)
    avg_hold  = r.get("avg_hold", 0.0)
    trade_log = r.get("trade_log", [])
    gross_win  = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t["pnl"] for t in trade_log if t.get("pnl", 0) < 0))
    return trades, win_rate, pf, total_pnl, avg_hold, gross_win, gross_loss


# ──────────────────────────────────────────────
# HTML 生成
# ──────────────────────────────────────────────

_DARK_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    background: #0f172a; color: #e2e8f0;
    font-family: 'Segoe UI', 'Noto Sans JP', sans-serif;
    font-size: 13px; padding: 24px;
}
h1 { font-size: 22px; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }
h2 { font-size: 16px; font-weight: 600; color: #94a3b8; margin: 24px 0 8px; }
h3 { font-size: 13px; font-weight: 600; color: #64748b; margin: 16px 0 6px; }
.subtitle { color: #64748b; font-size: 12px; margin-bottom: 24px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 24px; }
th {
    background: #1e293b; color: #94a3b8;
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
    padding: 8px 12px; text-align: right; border-bottom: 1px solid #334155;
}
th:first-child { text-align: left; }
td { padding: 7px 12px; border-bottom: 1px solid #1e293b; text-align: right; color: #cbd5e1; }
td:first-child { text-align: left; }
tr:hover td { background: #1e293b; }
.pos  { color: #4ade80; }
.neg  { color: #f87171; }
.best { background: #164e35 !important; color: #4ade80 !important; font-weight: 700; }
.delay-header { color: #60a5fa; font-weight: 600; }
.section-profit { border-left: 3px solid #4ade80; padding-left: 8px; margin-bottom: 4px; }
.section-loss   { border-left: 3px solid #f87171; padding-left: 8px; margin-bottom: 4px; }
.bt-badge { display: inline-block; background: #1e3a5f; color: #60a5fa;
            font-size: 10px; padding: 1px 6px; border-radius: 4px; margin-left: 6px; }
"""


def _fmt_pnl(v: float) -> str:
    s = f"{v:+,.0f}"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">{s}</span>' if cls else s


def _fmt_pct(v: float) -> str:
    return f"{v*100:.1f}%"


def build_html(
    all_results: list[dict],
    days: int,
    is_short: bool,
    max_price: float,
    min_price: float,
    min_bt_score: float,
    strategies_filter: list[str] | None,
    run_dt: str,
) -> str:

    summary: dict[int, dict] = {
        d: {"trades": 0, "wins": 0, "total_pnl": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0,
            "hold_sum": 0.0, "hold_cnt": 0}
        for d in DELAYS
    }
    item_rows: list[dict] = []
    skipped_bt = 0
    error_cnt  = 0

    for item in all_results:
        if "error" in item:
            error_cnt += 1
            continue
        if strategies_filter and item["strategy"] not in strategies_filter:
            continue
        bt = item.get("bt_score", 0.0)
        if min_bt_score > 0 and bt < min_bt_score:
            skipped_bt += 1
            continue

        row: dict[str, Any] = {
            "symbol": item["symbol"], "name": item["name"],
            "strategy": item["strategy"], "bt_score": bt,
        }
        best_pnl = None
        best_d   = None
        for d in DELAYS:
            r = item["delays"].get(d, {})
            trades, wr, pf, pnl, ah, gw, gl = _extract_metrics(r)
            row[f"d{d}_trades"] = trades
            row[f"d{d}_wr"]     = wr
            row[f"d{d}_pf"]     = pf
            row[f"d{d}_pnl"]    = pnl
            row[f"d{d}_gw"]     = gw
            row[f"d{d}_gl"]     = gl

            s = summary[d]
            s["trades"]     += trades
            s["total_pnl"]  += pnl
            s["gross_win"]  += gw
            s["gross_loss"] += gl
            if trades > 0:
                s["wins"]     += round(trades * wr)
                s["hold_sum"] += ah * trades
                s["hold_cnt"] += trades

            if best_pnl is None or pnl > best_pnl:
                best_pnl = pnl
                best_d   = d

        row["best_d"] = best_d
        item_rows.append(row)

    # ── サマリーテーブル ──
    def _summary_table() -> str:
        rows_html = ""
        for d in DELAYS:
            s      = summary[d]
            trades = s["trades"]
            pnl    = s["total_pnl"]
            gw     = s["gross_win"]
            gl     = s["gross_loss"]
            wr     = s["wins"] / trades if trades > 0 else 0.0
            ah     = s["hold_sum"] / s["hold_cnt"] if s["hold_cnt"] > 0 else 0.0
            rows_html += f"""
            <tr>
              <td><span class='delay-header'>{d}日後</span></td>
              <td>{trades:,}</td>
              <td>{_fmt_pct(wr)}</td>
              <td>{ah:.1f}日</td>
              <td><span class="pos">+{gw:,.0f}</span></td>
              <td><span class="neg">-{gl:,.0f}</span></td>
              <td>{_fmt_pnl(pnl)}</td>
            </tr>"""
        return f"""
        <table>
          <thead><tr>
            <th>entry_delay</th><th>件数</th><th>勝率</th><th>平均保有</th>
            <th>利益合計</th><th>損失合計</th><th>損益合計</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>"""

    # ── 銘柄別テーブル ──
    def _detail_table() -> str:
        strats_in_data = sorted({r["strategy"] for r in item_rows})
        html = ""
        for strat in strats_in_data:
            rows = [r for r in item_rows if r["strategy"] == strat]
            rows.sort(key=lambda x: x.get("d0_pnl", 0), reverse=True)

            # 利益銘柄 / 損失銘柄に分割
            profit_rows = [r for r in rows if r.get("d0_pnl", 0) >= 0]
            loss_rows   = [r for r in rows if r.get("d0_pnl", 0) < 0]

            d_headers = "".join(
                f'<th class="delay-header">{d}日後 PnL</th>'
                f'<th>利益</th><th>損失</th><th>取引</th><th>勝率</th>'
                for d in DELAYS
            )

            def _render_rows(rlist: list[dict]) -> str:
                out = ""
                for r in rlist:
                    best_d = r.get("best_d")
                    bt     = r.get("bt_score", 0.0)
                    bt_badge = f"<span class='bt-badge'>BT{int(bt)}</span>"
                    sym_cell = f"{r['symbol']}{bt_badge}<br><small style='color:#64748b'>{r['name']}</small>"
                    cells = f"<td>{sym_cell}</td>"
                    for d in DELAYS:
                        pnl = r.get(f"d{d}_pnl", 0.0)
                        gw  = r.get(f"d{d}_gw",  0.0)
                        gl  = r.get(f"d{d}_gl",  0.0)
                        trd = r.get(f"d{d}_trades", 0)
                        wr  = r.get(f"d{d}_wr", 0.0)
                        cls = " class='best'" if d == best_d else ""
                        gw_s = f"<span class='pos'>+{gw:,.0f}</span>" if gw > 0 else "—"
                        gl_s = f"<span class='neg'>-{gl:,.0f}</span>" if gl > 0 else "—"
                        cells += (
                            f"<td{cls}>{_fmt_pnl(pnl)}</td>"
                            f"<td{cls}>{gw_s}</td>"
                            f"<td{cls}>{gl_s}</td>"
                            f"<td{cls}>{trd}</td>"
                            f"<td{cls}>{_fmt_pct(wr)}</td>"
                        )
                    out += f"<tr>{cells}</tr>\n"
                return out

            html += f"<h2>戦略: {strat}</h2>"

            if profit_rows:
                html += f"""
                <h3 class='section-profit'>▲ 利益銘柄 (delay=0でプラス) — {len(profit_rows)}件</h3>
                <table>
                  <thead><tr><th>銘柄</th>{d_headers}</tr></thead>
                  <tbody>{_render_rows(profit_rows)}</tbody>
                </table>"""

            if loss_rows:
                html += f"""
                <h3 class='section-loss'>▼ 損失銘柄 (delay=0でマイナス) — {len(loss_rows)}件</h3>
                <table>
                  <thead><tr><th>銘柄</th>{d_headers}</tr></thead>
                  <tbody>{_render_rows(loss_rows)}</tbody>
                </table>"""

        return html

    mode_label  = "ショート" if is_short else "ロング"
    price_label = ""
    if min_price > 0 and max_price > 0:
        price_label = f" ／ 株価 {int(min_price):,}〜{int(max_price):,}円"
    elif max_price > 0:
        price_label = f" ／ 株価 〜{int(max_price):,}円"
    elif min_price > 0:
        price_label = f" ／ 株価 {int(min_price):,}円〜"

    bt_label   = f" ／ BTスコア≥{int(min_bt_score)}" if min_bt_score > 0 else ""
    skip_note  = f" （BTフィルターで{skipped_bt}件除外）" if skipped_bt else ""
    error_note = f" （データ取得失敗 {error_cnt}件）" if error_cnt else ""

    no_data_msg = ""
    if not item_rows:
        if error_cnt > 0:
            no_data_msg = f"""
<p style="color:#f87171; font-size:14px; padding: 20px 0;">
  ⚠️ データ取得に失敗しました ({error_cnt}件)。<br>
  ネットワーク接続を確認するか、ローカル環境で実行してください。
</p>"""
        elif skipped_bt > 0:
            no_data_msg = f"""
<p style="color:#fbbf24; font-size:14px; padding: 20px 0;">
  ℹ️ BTスコア≥{int(min_bt_score)} の銘柄が見つかりませんでした（{skipped_bt}件除外）。<br>
  --min-bt-score を下げるか、指定なしで実行してください。
</p>"""
        else:
            no_data_msg = '<p style="color:#94a3b8; padding:20px 0;">結果なし</p>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Entry Delay 分析 {run_dt}</title>
<style>{_DARK_CSS}</style>
</head>
<body>
<h1>Entry Delay 分析 [{mode_label}]</h1>
<p class="subtitle">
  対象期間: 直近 {days} 日 ／ 銘柄×戦略数: {len(item_rows)}{price_label}{bt_label}{skip_note}{error_note} ／ 生成: {run_dt}
</p>
{no_data_msg}
<h2>全体サマリー（delay 別）</h2>
{_summary_table()}

<h2>銘柄別（緑背景 = 各行の最高損益 delay）</h2>
{_detail_table()}

</body>
</html>"""


# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Entry delay analysis")
    parser.add_argument("--days",          type=int,   default=365)
    parser.add_argument("--workers",       type=int,   default=_DEFAULT_WORKERS)
    parser.add_argument("--short",         action="store_true")
    parser.add_argument("--max-price",     type=float, default=0.0)
    parser.add_argument("--min-price",     type=float, default=0.0)
    parser.add_argument("--min-bt-score",  type=float, default=0.0, help="BTスコア下限 (例: 60)")
    parser.add_argument("--wf-dir",        type=Path,  default=Path("walkforward_results"))
    parser.add_argument("--strategies",    type=str,   default="")
    parser.add_argument("--no-browser",    action="store_true")
    args = parser.parse_args()

    strategies_filter: list[str] | None = (
        [s.strip().upper() for s in args.strategies.split(",") if s.strip()]
        if args.strategies else None
    )

    tasks = build_tasks(
        is_short=args.short, max_price=args.max_price, min_price=args.min_price,
        strategies_filter=strategies_filter, wf_dir=args.wf_dir,
    )

    print(f"Entry delay 分析開始: {len(tasks)} タスク, {args.days}日, workers={args.workers}", flush=True)
    if args.min_bt_score > 0:
        print(f"  BTスコアフィルター: ≥{args.min_bt_score}", flush=True)

    all_results: list[dict] = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(_run_one, sym, nm, strat, calc_fn, em, sm, tm, et, args.days): (sym, strat)
            for sym, nm, strat, calc_fn, em, sm, tm, et in tasks
        }
        for fut in as_completed(futures):
            sym, strat = futures[fut]
            done += 1
            try:
                all_results.append(fut.result())
            except Exception as e:
                print(f"  ERROR {sym} {strat}: {e}", flush=True)
            if done % 10 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} 完了", flush=True)

    errors = [r for r in all_results if "error" in r]
    if errors:
        print(f"  [警告] データ取得失敗: {len(errors)}/{len(tasks)} 件 "
              f"(例: {errors[0]['symbol']} - {errors[0]['error']})", flush=True)

    run_dt    = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    today_str = date.today().strftime("%Y-%m-%d")
    suffix    = "_short" if args.short else ""
    bt_suffix = f"_bt{int(args.min_bt_score)}" if args.min_bt_score > 0 else ""
    out_path  = Path(f"entry_delay_analysis{suffix}{bt_suffix}_{today_str}.html")

    html = build_html(
        all_results, args.days, args.short,
        args.max_price, args.min_price, args.min_bt_score,
        strategies_filter, run_dt,
    )
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
