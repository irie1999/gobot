"""
simple_report_all_shifts.py - 6 shift OOS 結果を1つの HTML にまとめ

各 shift 用 watchlist で backtest し、対応する OOS 期間 (直近 N日) の取引のみを
表示。Long / Short タブと、各 shift のサブタブで切替。

【使い方】
  # 自動検出 (最新日付の daytrade_winners_<date>_shift*.py を全部読む)
  python simple_report_all_shifts.py

  # 日付指定
  python simple_report_all_shifts.py --date 2026-06-16

  # データソース
  python simple_report_all_shifts.py --source hybrid
"""

from __future__ import annotations

import argparse
import glob
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
LONG_STRATS = set(STRATEGIES.keys())
SHORT_STRATS = set(STRATEGIES_SHORT.keys())

SHIFTS = [30, 60, 90, 120, 150, 180]


def load_watchlist(path: Path) -> list[tuple[str, str, str]]:
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


def detect_latest_date() -> str | None:
    dates = set()
    for fp in glob.glob("daytrade_winners_*_shift*d.py"):
        stem = Path(fp).stem
        # daytrade_winners_<date>_shift<N>d
        parts = stem.split("_")
        for p in parts:
            if len(p) == 10 and p.count("-") == 2:
                try:
                    datetime.strptime(p, "%Y-%m-%d")
                    dates.add(p)
                except Exception:
                    pass
    return max(dates) if dates else None


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


def aggregate_daily(trades):
    by_date = defaultdict(list)
    for t in trades:
        d = t["entry_dt"].date()
        by_date[d].append(t)
    out = []
    cumulative = 0
    for d in sorted(by_date.keys()):
        days = by_date[d]
        pnl = sum(t["pnl"] for t in days)
        wins = sum(1 for t in days if t["pnl"] > 0)
        cumulative += pnl
        out.append({
            "date": d, "n": len(days), "wins": wins,
            "win_rate": wins / len(days) * 100,
            "pnl": pnl, "cumulative": cumulative,
        })
    return out


def render_section(trades, label):
    """1セクション分: サマリ + 日付別損益 + 全取引."""
    overall = calc_stats(trades)
    pf = overall["pf"]
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
    pf_color = "#4ade80" if pf >= 1.5 else ("#facc15" if pf >= 1.0
                                              else "#f87171")
    daily = aggregate_daily(trades)
    daily_sorted = list(reversed(daily))
    trades_sorted = sorted(trades, key=lambda x: x["entry_dt"], reverse=True)

    H = []
    # サマリーカード
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

    if not trades:
        H.append("<p style='color:#94a3b8'>取引なし</p>")
        return "\n".join(H)

    # 日付別損益
    H.append("<h3>📅 日付別損益</h3>")
    H.append("<table><thead><tr>")
    H.append("<th>日付</th><th class='right'>取引</th>"
             "<th class='right'>勝率</th><th class='right'>損益</th>"
             "<th class='right'>累積</th></tr></thead><tbody>")
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
    H.append(f"<h3>📋 全取引 ({len(trades_sorted):,}件)</h3>")
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
    return "\n".join(H)


def render_html(*, shift_trades_by_side: dict, date_label: str,
                source: str, watchlist_summary: dict) -> str:
    """完全な HTML 生成 (Long/Short タブ + shift サブタブ)."""
    H = []
    H.append("<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>")
    H.append(f"<title>6シフト統合レポート ({date_label})</title>")
    H.append("""
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a;color:#e2e8f0;padding:24px;margin:0;}
  h1{font-size:20px;margin:0 0 4px;color:#10b981;}
  h2{font-size:16px;margin:18px 0 8px;color:#cbd5e1;
     border-left:3px solid #10b981;padding-left:10px;}
  h3{font-size:14px;margin:12px 0 6px;color:#a3a3a3;}
  .meta{color:#94a3b8;font-size:13px;margin-bottom:16px;}
  .summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
  .card{background:#1e293b;padding:10px 14px;border-radius:6px;
        min-width:90px;}
  .card .lbl{color:#94a3b8;font-size:11px;}
  .card .val{font-size:16px;font-weight:bold;margin-top:3px;}
  table{width:100%;border-collapse:collapse;background:#1e293b;
        font-size:12px;margin-bottom:14px;}
  th{background:#334155;color:#cbd5e1;padding:6px 8px;text-align:left;
     font-weight:600;}
  td{padding:4px 8px;border-top:1px solid #334155;}
  tr:hover td{background:#293548;}
  .pos{color:#4ade80;} .neg{color:#f87171;}
  .small{font-size:10px;color:#94a3b8;}
  .right{text-align:right;}
  .center{text-align:center;}

  /* タブ */
  .tabs{display:flex;gap:4px;margin:14px 0 12px;border-bottom:1px solid #334155;
        flex-wrap:wrap;}
  .tab{padding:8px 14px;background:#1e293b;color:#94a3b8;border:none;
       cursor:pointer;font-size:13px;border-radius:6px 6px 0 0;}
  .tab.active{background:#10b981;color:#fff;}
  .tab-content{display:none;}
  .tab-content.active{display:block;}
  .sub-tabs{display:flex;gap:3px;margin:8px 0;flex-wrap:wrap;}
  .sub-tab{padding:5px 10px;background:#334155;color:#cbd5e1;border:none;
           cursor:pointer;font-size:12px;border-radius:4px;}
  .sub-tab.active{background:#0ea5e9;color:#fff;}
  .sub-content{display:none;}
  .sub-content.active{display:block;}
</style>
<script>
function showSide(side){
  document.querySelectorAll('.side-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.side-content').forEach(c=>c.classList.remove('active'));
  document.getElementById('side-tab-'+side).classList.add('active');
  document.getElementById('side-content-'+side).classList.add('active');
}
function showShift(side, shift){
  document.querySelectorAll('.shift-tab-'+side).forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.shift-content-'+side).forEach(c=>c.classList.remove('active'));
  document.getElementById('shift-tab-'+side+'-'+shift).classList.add('active');
  document.getElementById('shift-content-'+side+'-'+shift).classList.add('active');
}
</script>""")
    H.append("</head><body>")
    H.append(f"<h1>6 シフト統合レポート — OOS 評価</h1>")
    H.append(f"<div class='meta'>")
    H.append(f"  watchlist 日付: {date_label} / source: {source} / "
             f"各 shift の OOS 期間 (直近 N日) の取引のみ表示 / "
             f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    H.append("</div>")

    # watchlist サマリ
    H.append("<h2>📊 各 shift の watchlist 構成</h2>")
    H.append("<table><thead><tr>")
    H.append("<th>shift</th><th class='right'>銘柄数</th>"
             "<th class='right'>(銘柄×戦略)ペア</th>"
             "<th class='right'>Long ペア</th>"
             "<th class='right'>Short ペア</th>"
             "</tr></thead><tbody>")
    for s in SHIFTS:
        info = watchlist_summary.get(s, {})
        H.append(f"<tr>"
                 f"<td>shift={s}日</td>"
                 f"<td class='right'>{info.get('syms', 0)}</td>"
                 f"<td class='right'>{info.get('pairs', 0)}</td>"
                 f"<td class='right'>{info.get('long_pairs', 0)}</td>"
                 f"<td class='right'>{info.get('short_pairs', 0)}</td>"
                 f"</tr>")
    H.append("</tbody></table>")

    # 上位タブ: Long / Short
    H.append("<h2>取引結果 (OOS)</h2>")
    H.append("<div class='tabs'>")
    H.append("<button class='tab side-tab active' id='side-tab-long' "
             "onclick=\"showSide('long')\">📈 ロング</button>")
    H.append("<button class='tab side-tab' id='side-tab-short' "
             "onclick=\"showSide('short')\">📉 ショート</button>")
    H.append("</div>")

    for side in ("long", "short"):
        active_cls = "active" if side == "long" else ""
        side_label = "ロング" if side == "long" else "ショート"
        H.append(f"<div class='tab-content side-content {active_cls}' "
                 f"id='side-content-{side}'>")

        # サブタブ: shift 30/60/90/120/150/180
        H.append("<div class='sub-tabs'>")
        for i, s in enumerate(SHIFTS):
            ac = "active" if i == 0 else ""
            H.append(f"<button class='sub-tab shift-tab-{side} {ac}' "
                     f"id='shift-tab-{side}-{s}' "
                     f"onclick=\"showShift('{side}', {s})\">"
                     f"直近{s}日</button>")
        H.append("</div>")

        # 各 shift の内容
        for i, s in enumerate(SHIFTS):
            ac = "active" if i == 0 else ""
            H.append(f"<div class='sub-content shift-content-{side} {ac}' "
                     f"id='shift-content-{side}-{s}'>")
            trades = shift_trades_by_side.get((s, side), [])
            label = f"shift={s}日 / {side_label} / OOS 直近{s}日"
            H.append(render_section(trades, label))
            H.append("</div>")

        H.append("</div>")

    H.append("</body></html>")
    return "\n".join(H)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None,
                   help="watchlist 日付 (YYYY-MM-DD)。省略時自動検出")
    p.add_argument("--source", default="hybrid",
                   choices=["auto", "local", "yfinance", "hybrid"],
                   help="データソース (デフォルト hybrid)")
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--snapshot-date", default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--output", default=None)
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    date_label = args.date or detect_latest_date()
    if not date_label:
        print("[error] watchlist 日付を検出できません。--date 指定してください")
        return

    # 6 watchlist 読込
    watchlist_summary = {}
    all_specs_by_shift = {}  # shift -> list of (sym, name, strat)
    all_syms = set()
    for s in SHIFTS:
        wl_path = Path(f"daytrade_winners_{date_label}_shift{s}d.py")
        if not wl_path.exists():
            print(f"  [warn] {wl_path.name} 不在 → shift={s} スキップ")
            watchlist_summary[s] = {"syms": 0, "pairs": 0,
                                      "long_pairs": 0, "short_pairs": 0}
            continue
        pairs = load_watchlist(wl_path)
        syms = set(p[0] for p in pairs)
        long_pairs = sum(1 for _, _, strat in pairs if strat in LONG_STRATS)
        short_pairs = sum(1 for _, _, strat in pairs if strat in SHORT_STRATS)
        watchlist_summary[s] = {
            "syms": len(syms), "pairs": len(pairs),
            "long_pairs": long_pairs, "short_pairs": short_pairs,
        }
        all_specs_by_shift[s] = pairs
        all_syms.update(syms)

    if not all_syms:
        print("[error] watchlist 銘柄なし")
        return

    print(f"検出日付: {date_label}")
    print(f"ユニーク銘柄 (全 shift 合計): {len(all_syms)}", flush=True)

    # データロード (全銘柄を一度に)
    print(f"\n[Step 1] データロード (source={args.source})", flush=True)
    fetched = load_intraday_batch(
        sorted(all_syms), args.days, source=args.source,
        snapshot_date=args.snapshot_date,
    )
    print(f"  ロード: {len(fetched)}/{len(all_syms)}銘柄", flush=True)

    # バックテスト (全 shift の全ペアを一度に評価; 同じ pair は1回だけ)
    # 重複排除
    unique_pairs = set()
    for s, pairs in all_specs_by_shift.items():
        for sym, name, strat in pairs:
            if sym in fetched:
                unique_pairs.add((sym, name, strat))

    print(f"\n[Step 2] バックテスト ({len(unique_pairs)}ペア "
          f"ユニーク)", flush=True)
    pair_trades = {}  # (sym, strat) -> trades list
    specs = [(sym, name, strat, fetched[sym])
             for sym, name, strat in unique_pairs]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_run_one, *spec): spec for spec in specs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                strat_key, sym, trades = fut.result()
            except Exception as e:
                spec = futs[fut]
                print(f"  [error] {spec[0]}/{spec[2]}: {e}")
                continue
            pair_trades[(sym, strat_key)] = trades
            if done % 20 == 0 or done == len(specs):
                print(f"  {done}/{len(specs)} 完了", flush=True)

    # shift 別 + side 別に集計
    today = datetime.now(JST).date()
    shift_trades_by_side = defaultdict(list)
    for s in SHIFTS:
        cutoff = today - timedelta(days=s)
        pairs = all_specs_by_shift.get(s, [])
        for sym, name, strat in pairs:
            trades = pair_trades.get((sym, strat), [])
            # 直近 s 日にフィルタ
            for t in trades:
                if not hasattr(t.get("entry_dt"), "date"):
                    continue
                if t["entry_dt"].date() < cutoff:
                    continue
                side = "short" if strat in SHORT_STRATS else "long"
                shift_trades_by_side[(s, side)].append(t)

    # HTML 出力
    out = args.output or f"simple_report_all_shifts_{date_label}.html"
    html_text = render_html(
        shift_trades_by_side=shift_trades_by_side,
        date_label=date_label,
        source=args.source,
        watchlist_summary=watchlist_summary,
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
