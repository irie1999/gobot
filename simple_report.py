"""
simple_report.py - 簡易レポート (全取引一覧 + 日付別損益のみ)

holdout_periods_report_daytrade.py の期間別タブを省略し、watchlist 銘柄の
全取引と日付別損益のみをコンパクトに HTML 出力。

shift 別 watchlist で実行することで、各 OOS 期間の実取引結果を確認できる。

【使い方】
  # shift=30 の watchlist で簡易レポート
  python simple_report.py --watchlist daytrade_winners_2026-06-16_shift30d.py

  # 期間限定 (例: 直近30日のみ)
  python simple_report.py --watchlist daytrade_winners_2026-06-16_shift30d.py --recent-days 30

  # データソース変更
  python simple_report.py --watchlist daytrade_winners_2026-06-16_shift30d.py --source hybrid
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT

JST = timezone(timedelta(hours=9))
ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}


def load_watchlist(path: Path) -> list[tuple[str, str, str]]:
    """3-tuple watchlist 読込: (symbol, name, strategy)"""
    spec = importlib.util.spec_from_file_location("wl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "SYMBOLS", [])
    out = []
    for e in raw:
        if isinstance(e, tuple) and len(e) >= 3:
            out.append((e[0], e[1], e[2]))
        elif isinstance(e, tuple) and len(e) == 2:
            out.append((e[0], e[1], ""))
    return out


def _run_one(sym, name, strat_key, df):
    strat_fn = ALL_STRATEGIES.get(strat_key)
    if strat_fn is None:
        return strat_key, sym, []
    res = backtest_symbol_5m(sym, name, df, strat_fn,
                              strategy_params={"name": strat_key})
    if not res:
        return strat_key, sym, []
    trades = []
    for t in res["trades"]:
        trades.append({
            "symbol": sym, "name": name, "strategy": strat_key,
            "entry_dt": t["entry_dt"], "exit_dt": t["exit_dt"],
            "entry_p": t["entry_p"], "exit_p": t["exit_p"],
            "stop_p": t["stop_p"], "target_p": t["target_p"],
            "qty": t["qty"], "pnl": t["pnl"], "pct": t["pct"],
            "side": t["side"], "reason": t["reason"],
        })
    return strat_key, sym, trades


def _fmt_dt(dt):
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _hold_min(t):
    try:
        return int((t["exit_dt"] - t["entry_dt"]).total_seconds() / 60)
    except Exception:
        return 0


def aggregate_daily(trades: list) -> list[dict]:
    by_date = defaultdict(list)
    for t in trades:
        d = t["entry_dt"].date()
        by_date[d].append(t)
    out = []
    cumulative = 0
    for d in sorted(by_date.keys()):
        day_trades = by_date[d]
        pnl = sum(t["pnl"] for t in day_trades)
        wins = sum(1 for t in day_trades if t["pnl"] > 0)
        cumulative += pnl
        out.append({
            "date": d,
            "n": len(day_trades),
            "wins": wins,
            "win_rate": wins / len(day_trades) * 100,
            "pnl": pnl,
            "cumulative": cumulative,
        })
    return out


def render_html(*, watchlist_path: Path, trades: list, recent_days: int,
                pair_count: int, sym_count: int, source: str) -> str:
    overall = calc_stats(trades)
    pf = overall["pf"]
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
    pf_color = "#4ade80" if pf >= 1.5 else ("#facc15" if pf >= 1.0
                                              else "#f87171")
    trades_sorted = sorted(trades, key=lambda x: x["entry_dt"], reverse=True)
    daily = aggregate_daily(trades)
    daily_sorted = list(reversed(daily))  # 新しい日付が上

    H = []
    H.append("<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>")
    H.append(f"<title>簡易レポート ({watchlist_path.name})</title>")
    H.append("""
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a;color:#e2e8f0;padding:24px;margin:0;}
  h1{font-size:20px;margin:0 0 4px;color:#10b981;}
  h2{font-size:16px;margin:24px 0 8px;color:#cbd5e1;
     border-left:3px solid #10b981;padding-left:10px;}
  .meta{color:#94a3b8;font-size:13px;margin-bottom:16px;}
  .summary{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;}
  .card{background:#1e293b;padding:12px 18px;border-radius:6px;
        min-width:100px;}
  .card .lbl{color:#94a3b8;font-size:11px;}
  .card .val{font-size:18px;font-weight:bold;margin-top:3px;}
  table{width:100%;border-collapse:collapse;background:#1e293b;
        font-size:12px;margin-bottom:16px;}
  th{background:#334155;color:#cbd5e1;padding:6px 8px;text-align:left;
     font-weight:600;position:sticky;top:0;}
  td{padding:4px 8px;border-top:1px solid #334155;}
  tr:hover td{background:#293548;}
  .pos{color:#4ade80;} .neg{color:#f87171;}
  .small{font-size:10px;color:#94a3b8;}
  .right{text-align:right;}
  .center{text-align:center;}
</style>""")
    H.append("</head><body>")
    H.append(f"<h1>簡易レポート (全取引一覧 + 日付別損益)</h1>")
    H.append(f"<div class='meta'>")
    H.append(f"  watchlist: {watchlist_path.name} / source: {source}")
    H.append(f" / {sym_count}銘柄 × {pair_count}ペア")
    if recent_days > 0:
        H.append(f" / 直近{recent_days}日のみ表示")
    H.append(f" / 生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    H.append("</div>")

    # サマリ
    H.append("<div class='summary'>")
    H.append(f"<div class='card'><div class='lbl'>総取引</div>"
             f"<div class='val'>{overall['n']:,}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>勝率</div>"
             f"<div class='val'>{overall['win_rate']:.0f}%</div></div>")
    H.append(f"<div class='card'><div class='lbl'>PF</div>"
             f"<div class='val' style='color:{pf_color}'>{pf_str}</div></div>")
    pnl_cls = "pos" if overall["total_pnl"] >= 0 else "neg"
    H.append(f"<div class='card'><div class='lbl'>損益</div>"
             f"<div class='val {pnl_cls}'>{overall['total_pnl']:+,.0f}円</div></div>")
    H.append(f"<div class='card'><div class='lbl'>平均利益</div>"
             f"<div class='val pos'>{overall['avg_win']:+,.0f}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>平均損失</div>"
             f"<div class='val neg'>{overall['avg_loss']:+,.0f}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>最大DD</div>"
             f"<div class='val neg'>{overall['max_dd']:.1f}%</div></div>")
    H.append("</div>")

    # 日付別損益
    H.append("<h2>📅 日付別損益</h2>")
    H.append("<table><thead><tr>")
    H.append("<th>日付</th><th class='right'>取引数</th>"
             "<th class='right'>勝率</th><th class='right'>損益</th>"
             "<th class='right'>累積損益</th></tr></thead><tbody>")
    for d in daily_sorted:
        pnl_c = "pos" if d["pnl"] >= 0 else "neg"
        cum_c = "pos" if d["cumulative"] >= 0 else "neg"
        H.append(f"<tr>"
                 f"<td>{d['date']}</td>"
                 f"<td class='right'>{d['n']}</td>"
                 f"<td class='right'>{d['win_rate']:.0f}%</td>"
                 f"<td class='right {pnl_c}'>{d['pnl']:+,.0f}</td>"
                 f"<td class='right {cum_c}'>{d['cumulative']:+,.0f}</td>"
                 f"</tr>")
    H.append("</tbody></table>")

    # 全取引一覧
    H.append(f"<h2>📋 全取引一覧 ({len(trades_sorted):,}件 / 日付降順)</h2>")
    H.append("<table><thead><tr>")
    H.append("<th>銘柄</th><th>戦略</th><th class='center'>Side</th>"
             "<th>Entry</th><th>Exit</th><th class='right'>保有</th>"
             "<th class='right'>買値</th><th class='right'>損切</th>"
             "<th class='right'>目標</th><th class='right'>決済</th>"
             "<th class='right'>損益</th><th class='right'>%</th>"
             "<th>理由</th></tr></thead><tbody>")
    for t in trades_sorted:
        pnl_c = "pos" if t["pnl"] >= 0 else "neg"
        held = _hold_min(t)
        side_s = "L" if t["side"] == "long" else "S"
        H.append(
            f"<tr>"
            f"<td>{html.escape(t['name'][:14])}<br>"
            f"<span class='small'>{html.escape(t['symbol'])}</span></td>"
            f"<td>{html.escape(t['strategy'])}</td>"
            f"<td class='center'>{side_s}</td>"
            f"<td>{_fmt_dt(t['entry_dt'])}</td>"
            f"<td>{_fmt_dt(t['exit_dt'])}</td>"
            f"<td class='right'>{held}分</td>"
            f"<td class='right'>{t['entry_p']:,.0f}</td>"
            f"<td class='right'>{t['stop_p']:,.0f}</td>"
            f"<td class='right'>{t['target_p']:,.0f}</td>"
            f"<td class='right'>{t['exit_p']:,.0f}</td>"
            f"<td class='right {pnl_c}'>{t['pnl']:+,.0f}</td>"
            f"<td class='right {pnl_c}'>{t['pct']:+.2f}%</td>"
            f"<td>{html.escape(t['reason'])}</td>"
            f"</tr>"
        )
    H.append("</tbody></table>")
    H.append("</body></html>")
    return "\n".join(H)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--watchlist", required=True,
                   help="watchlist .py (3-tuple SYMBOLS)")
    p.add_argument("--source", default="hybrid",
                   choices=["auto", "local", "yfinance", "hybrid"],
                   help="データソース (デフォルト hybrid)")
    p.add_argument("--days", type=int, default=540,
                   help="バックテスト対象期間 (デフォルト 540)")
    p.add_argument("--recent-days", type=int, default=0,
                   help="0より大きいとき、直近 N 日の取引のみ表示 "
                        "(0 = 全期間)")
    p.add_argument("--snapshot-date", default=None,
                   help="yfinance/hybrid 用スナップショット日")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--output", default=None,
                   help="出力 HTML パス (省略時 simple_report_<watchlist名>.html)")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    wl_path = Path(args.watchlist)
    if not wl_path.exists():
        print(f"[error] watchlist 不在: {wl_path}")
        return

    pairs = load_watchlist(wl_path)
    if not pairs:
        print(f"[error] {wl_path.name}: SYMBOLS 空")
        return

    syms = sorted(set(p[0] for p in pairs))
    print(f"watchlist: {wl_path.name} / {len(syms)}銘柄 / {len(pairs)}ペア",
          flush=True)

    print(f"[Step 1] データロード (source={args.source}, days={args.days})",
          flush=True)
    fetched = load_intraday_batch(
        syms, args.days, source=args.source,
        snapshot_date=args.snapshot_date,
    )
    print(f"  ロード: {len(fetched)}/{len(syms)}銘柄", flush=True)

    # バックテスト
    print(f"[Step 2] バックテスト ({len(pairs)}ペア)", flush=True)
    specs = []
    for sym, name, strat in pairs:
        df = fetched.get(sym)
        if df is None or df.empty:
            continue
        specs.append((sym, name, strat, df))

    all_trades = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_run_one, *s): s for s in specs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                _, _, trades = fut.result()
            except Exception as e:
                spec = futs[fut]
                print(f"  [error] {spec[0]}/{spec[2]}: {e}")
                continue
            all_trades.extend(trades)
            if done % 10 == 0 or done == len(specs):
                print(f"  {done}/{len(specs)} 完了", flush=True)
    print(f"  全取引: {len(all_trades):,}件", flush=True)

    # 期間フィルタ
    if args.recent_days > 0:
        cutoff = datetime.now(JST).date() - timedelta(days=args.recent_days)
        filtered = [t for t in all_trades
                    if hasattr(t.get("entry_dt"), "date")
                    and t["entry_dt"].date() >= cutoff]
        print(f"  直近{args.recent_days}日フィルタ: "
              f"{len(all_trades)} → {len(filtered)}件", flush=True)
        all_trades = filtered

    out = args.output or f"simple_report_{wl_path.stem}.html"
    html_text = render_html(
        watchlist_path=wl_path,
        trades=all_trades,
        recent_days=args.recent_days,
        pair_count=len(pairs),
        sym_count=len(syms),
        source=args.source,
    )
    Path(out).write_text(html_text, encoding="utf-8")
    print(f"\n[OK] 出力: {Path(out).resolve()}", flush=True)
    if not args.no_open:
        try:
            webbrowser.open(f"file://{Path(out).resolve()}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
