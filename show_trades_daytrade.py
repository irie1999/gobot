"""
show_trades_daytrade.py  ―  WATCHLIST 銘柄の取引詳細をテキスト出力
==================================================================
HTMLレポート無しで、各銘柄の取引明細をテキストファイルに出力。
チャット貼付け用にコンパクト整形。

【使い方】
  python show_trades_daytrade.py                  # 全WATCHLIST 30日
  python show_trades_daytrade.py --days 60
  python show_trades_daytrade.py --top 5          # 上位5銘柄のみ
  python show_trades_daytrade.py --symbol 5715.T  # 単一銘柄
  python show_trades_daytrade.py --recent-only    # 直近10取引のみ
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_signals_daytrade import backtest_one, _load_watchlist
from risk_metrics_5m import enrich_stats

JST = timezone(timedelta(hours=9))


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _fmt_dt(dt):
    if hasattr(dt, "strftime"):
        return dt.strftime("%m-%d %H:%M")
    return str(dt)[:11] if dt else "?"


def main():
    parser = argparse.ArgumentParser(description="WATCHLIST 取引詳細出力")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--days", type=int, default=30,
                        help="表示期間 (デフォルト30日)")
    parser.add_argument("--top", type=int, default=0,
                        help="上位N銘柄のみ (0=全部)")
    parser.add_argument("--symbol", default=None,
                        help="単一銘柄のみ (例: 5715.T)")
    parser.add_argument("--recent-only", action="store_true",
                        help="直近10取引のみ表示")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    watchlist = _load_watchlist(args.watchlist)
    if not watchlist:
        print("[error] WATCHLIST 空")
        return

    if args.symbol:
        watchlist = [e for e in watchlist if e[0] == args.symbol]
        if not watchlist:
            print(f"[error] {args.symbol} が WATCHLIST にありません")
            return

    print(f"取引明細出力: {len(watchlist)}銘柄 / 直近{args.days}日", flush=True)

    today = datetime.now(JST).date()
    cutoff = today - timedelta(days=args.days)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    items = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for entry in watchlist:
            if len(entry) >= 3:
                sym, name, strat = entry[:3]
            else:
                sym, name = entry[:2]
                strat = "DON"
            futs[ex.submit(backtest_one, sym, name, strat)] = (sym, strat)
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                items.append(r)
            if i % 10 == 0 or i == len(watchlist):
                print(f"  {i}/{len(watchlist)}", flush=True)

    # 損益順 (期間内)
    def period_pnl(it):
        period_trades = [t for t in it["trades"]
                          if hasattr(t.get("entry_dt"), "date")
                          and t["entry_dt"].date() >= cutoff]
        return sum(t["pnl"] for t in period_trades)

    items.sort(key=lambda x: -period_pnl(x))
    if args.top > 0:
        items = items[:args.top]

    # テキスト生成
    lines = []
    lines.append(f"=== WATCHLIST 取引詳細 (直近{args.days}日) ===")
    lines.append(f"生成: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"対象: {len(items)}銘柄")
    lines.append("")

    # 銘柄別サマリ
    lines.append("【銘柄別サマリ】")
    lines.append(f"{'銘柄':<22} {'戦略':<7} {'取引':>4} {'勝率':>5} {'PF':>5} "
                 f"{'損益':>10} {'DD':>6} {'平均保有':>8}")
    lines.append("-" * 88)
    for it in items:
        period_trades = [t for t in it["trades"]
                          if hasattr(t.get("entry_dt"), "date")
                          and t["entry_dt"].date() >= cutoff]
        if not period_trades:
            continue
        s = enrich_stats(period_trades)
        name_disp = it["name"][:20]
        lines.append(
            f"{name_disp:<22} {it['strategy']:<7} {s['n']:>4} "
            f"{s['win_rate']:>4.0f}% {_pf(s['pf']):>5} "
            f"{s['total_pnl']:>+10,.0f} {s['max_dd']:>+5.1f}% "
            f"{s['avg_hold_min']:>6.0f}分"
        )

    total_pnl = sum(period_pnl(it) for it in items)
    total_trades = sum(len([t for t in it["trades"]
                            if hasattr(t.get("entry_dt"), "date")
                            and t["entry_dt"].date() >= cutoff]) for it in items)
    lines.append("-" * 88)
    lines.append(f"{'合計':<22} {' ':<7} {total_trades:>4} {' ':>5} {' ':>5} "
                 f"{total_pnl:>+10,.0f}")
    lines.append("")

    # 銘柄別 取引詳細
    lines.append("【銘柄別 取引明細】")
    lines.append("")
    for it in items:
        period_trades = [t for t in it["trades"]
                          if hasattr(t.get("entry_dt"), "date")
                          and t["entry_dt"].date() >= cutoff]
        if not period_trades:
            continue

        s = enrich_stats(period_trades)
        lines.append(f"── {it['name']} ({it['symbol']}) [{it['strategy']}] "
                     f"取引{s['n']} PF{_pf(s['pf'])} {s['total_pnl']:+,.0f}円 ──")
        period_trades.sort(key=lambda x: str(x.get("entry_dt", "")), reverse=True)
        display = period_trades[:10] if args.recent_only else period_trades

        lines.append(f"  {'Entry':<11} {'Exit':<11} {'保有':>4} "
                     f"{'買値':>7} {'損切':>7} {'目標':>7} {'決済':>7} "
                     f"{'損益':>9} {'%':>6} {'理由'}")
        for t in display:
            ed = _fmt_dt(t.get("entry_dt"))
            xd = _fmt_dt(t.get("exit_dt"))
            try:
                delta = t["exit_dt"] - t["entry_dt"]
                hold = f"{int(delta.total_seconds()//60)}分"
            except Exception:
                hold = "-"
            ep = t.get("entry_p", 0)
            sp = t.get("stop_p", 0)
            tp = t.get("target_p", 0)
            xp = t.get("exit_p", 0)
            pnl = t.get("pnl", 0)
            pct = t.get("pct", 0)
            reason = t.get("reason", "?")
            lines.append(
                f"  {ed:<11} {xd:<11} {hold:>4} "
                f"{ep:>7,.0f} {sp:>7,.0f} {tp:>7,.0f} {xp:>7,.0f} "
                f"{pnl:>+9,.0f} {pct:>+5.2f}% {reason}"
            )
        if args.recent_only and len(period_trades) > 10:
            lines.append(f"  ... 他 {len(period_trades)-10}取引")
        lines.append("")

    # ファイル出力
    out = Path(f"trade_details_{datetime.now(JST).strftime('%Y-%m-%d_%H%M')}.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    print(f"  銘柄数: {len(items)}, 合計取引: {total_trades}, 合計損益: {total_pnl:+,.0f}")
    print(f"\n  type {out.name}     ← 表示+貼付け")


if __name__ == "__main__":
    main()
