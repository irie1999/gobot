"""
replay_period.py  ―  期間指定ORB+Donchianバックテスト
==================================================================
WATCH_SYMBOLS (20銘柄) について、指定期間のORB+Donchian両戦略を
日次でバックテストし、全トレードをHTMLで表示する。

【使い方】
  python replay_period.py                                  # 直近30日
  python replay_period.py --from 2026-03-01 --to 2026-04-10  # 期間指定
  python replay_period.py --days 90                        # 直近90日
  python replay_period.py --strategy orb                   # ORBのみ
  python replay_period.py --strategy donchian              # Donchianのみ
"""

from __future__ import annotations

import argparse
import pickle
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# replay_yesterday.py から関数を再利用
from replay_yesterday import (
    WATCH_SYMBOLS, BUDGET, MAX_RISK,
    load_day_data, get_prev_close,
    run_orb, run_donchian,
)

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def get_all_dates_in_range(from_date: str, to_date: str) -> list[str]:
    """指定期間内にデータがある営業日を抽出。"""
    all_dates = set()
    for code4, _ in WATCH_SYMBOLS:
        code5 = code4 + "0"
        pkl_path = DATA_DIR / f"{code5}.pkl"
        if not pkl_path.exists():
            continue
        try:
            df = pickle.loads(pkl_path.read_bytes())
            if "Date" in df.columns:
                dates = df["Date"].astype(str).str[:10].unique()
                for d in dates:
                    if from_date <= d <= to_date:
                        all_dates.add(d)
        except Exception:
            continue
    return sorted(all_dates)


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=dd)


def build_html(orb_all, don_all, first_orb_all, first_don_all,
               dates, from_date, to_date, strategy_mode):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # 集計
    orb_stats = calc_stats(orb_all)
    don_stats = calc_stats(don_all)
    first_orb_stats = calc_stats(first_orb_all)
    first_don_stats = calc_stats(first_don_all)

    # 戦略別サマリー
    def _summary_box(label, stats, color):
        cls = "profit" if stats["total_pnl"] >= 0 else "loss"
        return f"""
        <div class="it"><div class="lb" style="color:{color}">{label}</div>
        <div class="vl {cls}">{stats["total_pnl"]:+,.0f}円</div>
        <div class="lb">{stats["n"]}取引 / 勝率{stats["win_rate"]:.1f}% / PF{_pf(stats["pf"])}</div>
        <div class="lb">DD {stats["max_dd"]:+.1f}%</div></div>"""

    # 銘柄別集計
    def _by_symbol(trades):
        by_sym = {}
        for t in trades:
            s = t.get("symbol", "?")
            by_sym.setdefault(s, []).append(t)
        return by_sym

    orb_by_sym = _by_symbol(orb_all)
    don_by_sym = _by_symbol(don_all)

    # 銘柄テーブル (全シグナル合計)
    sym_rows = ""
    all_syms = sorted(set(list(orb_by_sym.keys()) + list(don_by_sym.keys())),
                      key=lambda s: -(sum(t["pnl"] for t in orb_by_sym.get(s, []))
                                     + sum(t["pnl"] for t in don_by_sym.get(s, []))))
    for sym in all_syms:
        name = next((n for c, n in WATCH_SYMBOLS if c == sym), sym)
        orb_ts = orb_by_sym.get(sym, [])
        don_ts = don_by_sym.get(sym, [])

        def _cell(ts):
            if not ts:
                return '<td>-</td><td>-</td><td>-</td>'
            total = sum(t["pnl"] for t in ts)
            wins = sum(1 for t in ts if t["pnl"] > 0)
            cls = "profit" if total >= 0 else "loss"
            return (f'<td>{len(ts)}</td>'
                    f'<td>{wins/len(ts)*100:.0f}%</td>'
                    f'<td class="{cls}">{total:+,.0f}</td>')

        total_all = sum(t["pnl"] for t in orb_ts) + sum(t["pnl"] for t in don_ts)
        tc = "profit" if total_all >= 0 else "loss"
        sym_rows += f"""<tr>
          <td class="sym">{name}<br><small class="code">{sym}.T</small></td>
          {_cell(orb_ts)}
          {_cell(don_ts)}
          <td class="{tc}">{total_all:+,.0f}</td>
        </tr>"""

    # 日次損益テーブル
    daily_rows = ""
    for date in dates:
        orb_day = [t for t in orb_all if t.get("date") == date]
        don_day = [t for t in don_all if t.get("date") == date]
        orb_first_day = [t for t in first_orb_all if t.get("date") == date]
        don_first_day = [t for t in first_don_all if t.get("date") == date]

        orb_sum = sum(t["pnl"] for t in orb_day)
        don_sum = sum(t["pnl"] for t in don_day)
        orb_first_sum = sum(t["pnl"] for t in orb_first_day)
        don_first_sum = sum(t["pnl"] for t in don_first_day)

        def _c(v):
            return "profit" if v >= 0 else "loss"

        daily_rows += f"""<tr>
          <td>{date}</td>
          <td>{len(orb_day)}</td><td class="{_c(orb_sum)}">{orb_sum:+,.0f}</td>
          <td>{len(don_day)}</td><td class="{_c(don_sum)}">{don_sum:+,.0f}</td>
          <td class="{_c(orb_first_sum)}">{orb_first_sum:+,.0f}</td>
          <td class="{_c(don_first_sum)}">{don_first_sum:+,.0f}</td>
        </tr>"""

    # 個別トレード (折りたたみ)
    def _trade_details(trades, color, label):
        if not trades:
            return ""
        rows_t = ""
        for t in sorted(trades, key=lambda x: str(x.get("entry_dt", ""))):
            pc = "profit" if t["pnl"] > 0 else "loss"
            name = next((n for c, n in WATCH_SYMBOLS if c == t.get("symbol")), t.get("symbol"))
            date_str = t.get("date", "-")
            ed = t["entry_dt"].strftime("%H:%M") if hasattr(t["entry_dt"], "strftime") else "-"
            xd = t["exit_dt"].strftime("%H:%M") if hasattr(t["exit_dt"], "strftime") else "-"
            rows_t += f"""<tr>
              <td>{date_str}</td>
              <td class="sym">{name}<br><small class="code">{t.get('symbol', '')}.T</small></td>
              <td>{ed}</td><td>{xd}</td>
              <td>{t['entry_p']:,.0f}</td><td class="loss">{t['stop_p']:,.0f}</td>
              <td class="profit">{t['target_p']:,.0f}</td><td>{t['exit_p']:,.0f}</td>
              <td>{t['qty']}</td>
              <td class="{pc}">{t['pnl']:+,.0f}</td>
              <td class="{pc}">{t['pct']:+.2f}%</td>
              <td>{t['reason']}</td></tr>"""
        return f"""<details class="ts" open><summary><strong style="color:{color}">{label}</strong>
          ({len(trades)}取引 / 損益 {sum(t["pnl"] for t in trades):+,.0f}円)</summary>
          <table><thead><tr>
          <th>日付</th><th>銘柄</th><th>Entry</th><th>Exit</th>
          <th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
          <th>株数</th><th>損益</th><th>%</th><th>理由</th>
          </tr></thead><tbody>{rows_t}</tbody></table></details>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>ORB+Donchian 期間バックテスト {from_date}〜{to_date}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#60a5fa;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#60a5fa;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #60a5fa;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600}}.code{{color:#64748b;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center;min-width:180px}}.box .lb{{color:#94a3b8;font-size:.75rem}}
.box .vl{{font-size:1.5rem;font-weight:700}}
details.ts{{margin-bottom:6px;background:#1e293b;border-radius:6px;padding:8px}}
details.ts summary{{cursor:pointer;padding:4px 8px;font-size:.9rem}}
details.ts table{{margin-top:8px}}
</style></head><body>
<h1>ORB + Donchian 期間バックテスト</h1>
<p class="sub">期間: <strong>{from_date} 〜 {to_date}</strong> ({len(dates)}営業日) /
  監視: {len(WATCH_SYMBOLS)}銘柄 / 予算: {BUDGET:,}円 / モード: {strategy_mode}</p>

<h2>全シグナル採用 (同時複数ポジション可)</h2>
<div class="box">
{_summary_box("ORB", orb_stats, "#fbbf24") if strategy_mode in ("both", "orb") else ""}
{_summary_box("Donchian", don_stats, "#10b981") if strategy_mode in ("both", "donchian") else ""}
{_summary_box("2戦略合計", calc_stats(orb_all + don_all), "#60a5fa") if strategy_mode == "both" else ""}
</div>

<h2>実運用想定 (最初のシグナル1銘柄/日)</h2>
<div class="box">
{_summary_box("ORB (1日1銘柄)", first_orb_stats, "#fbbf24") if strategy_mode in ("both", "orb") else ""}
{_summary_box("Donchian (1日1銘柄)", first_don_stats, "#10b981") if strategy_mode in ("both", "donchian") else ""}
</div>

<h2>日次損益</h2>
<table><thead><tr>
<th rowspan="2">日付</th>
<th colspan="2" style="color:#fbbf24">ORB 全採用</th>
<th colspan="2" style="color:#10b981">Don 全採用</th>
<th style="color:#fbbf24">ORB 1銘柄</th>
<th style="color:#10b981">Don 1銘柄</th>
</tr>
<tr><th>N</th><th>損益</th><th>N</th><th>損益</th><th>損益</th><th>損益</th></tr>
</thead><tbody>{daily_rows}</tbody></table>

<h2>銘柄別集計</h2>
<table><thead><tr>
<th rowspan="2">銘柄</th>
<th colspan="3" style="color:#fbbf24">ORB</th>
<th colspan="3" style="color:#10b981">Donchian</th>
<th rowspan="2">合計損益</th>
</tr>
<tr><th>取引</th><th>勝率</th><th>損益</th>
<th>取引</th><th>勝率</th><th>損益</th></tr>
</thead><tbody>{sym_rows}</tbody></table>

<h2>個別トレード明細</h2>
{_trade_details(orb_all, "#fbbf24", "ORB") if strategy_mode in ("both", "orb") else ""}
{_trade_details(don_all, "#10b981", "Donchian") if strategy_mode in ("both", "donchian") else ""}
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="期間指定ORB+Donchianバックテスト")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="開始日 (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="終了日 (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=30,
                        help="直近何日 (--from/--to未指定時)")
    parser.add_argument("--strategy", choices=["both", "orb", "donchian"],
                        default="both")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # 日付範囲決定
    if args.from_date and args.to_date:
        from_date = args.from_date
        to_date = args.to_date
    else:
        # 最新営業日を取得
        from replay_yesterday import _find_latest_date
        to_date = args.to_date or _find_latest_date()
        from_dt = datetime.strptime(to_date, "%Y-%m-%d") - timedelta(days=args.days)
        from_date = args.from_date or from_dt.strftime("%Y-%m-%d")

    dates = get_all_dates_in_range(from_date, to_date)
    print(f"期間: {from_date} 〜 {to_date} ({len(dates)}営業日)")
    print(f"監視: {len(WATCH_SYMBOLS)}銘柄  戦略: {args.strategy}")
    print("=" * 70)

    orb_all = []
    don_all = []
    first_orb_all = []
    first_don_all = []

    for di, date in enumerate(dates, 1):
        day_orb_trades = []
        day_don_trades = []

        for code4, name in WATCH_SYMBOLS:
            code5 = code4 + "0"
            day_df = load_day_data(code5, date)
            if day_df is None or day_df.empty:
                continue
            prev_close = get_prev_close(code5, date)

            if args.strategy in ("both", "orb"):
                orb_trade, _ = run_orb(day_df, prev_close)
                if orb_trade:
                    orb_trade["date"] = date
                    orb_trade["symbol"] = code4
                    orb_trade["name"] = name
                    day_orb_trades.append(orb_trade)

            if args.strategy in ("both", "donchian"):
                don_trade, _ = run_donchian(day_df, prev_close)
                if don_trade:
                    don_trade["date"] = date
                    don_trade["symbol"] = code4
                    don_trade["name"] = name
                    day_don_trades.append(don_trade)

        # 全採用
        orb_all.extend(day_orb_trades)
        don_all.extend(day_don_trades)

        # 最初のシグナル1銘柄だけ採用
        if day_orb_trades:
            day_orb_trades.sort(key=lambda t: str(t["entry_dt"]))
            first_orb_all.append(day_orb_trades[0])
        if day_don_trades:
            day_don_trades.sort(key=lambda t: str(t["entry_dt"]))
            first_don_all.append(day_don_trades[0])

        if di % 10 == 0 or di == len(dates):
            orb_sum = sum(t["pnl"] for t in orb_all)
            don_sum = sum(t["pnl"] for t in don_all)
            print(f"  {di}/{len(dates)} 完了  "
                  f"ORB: {len(orb_all)}取引 {orb_sum:+,.0f}円  "
                  f"Don: {len(don_all)}取引 {don_sum:+,.0f}円")

    # サマリー
    print()
    print("=" * 70)
    print(f"  全シグナル採用 (同時複数ポジ可)")
    print("=" * 70)
    orb_s = calc_stats(orb_all)
    don_s = calc_stats(don_all)
    print(f"  ORB     : {orb_s['n']:>4}取引  勝率{orb_s['win_rate']:>5.1f}%  "
          f"PF{_pf(orb_s['pf']):>6}  {orb_s['total_pnl']:>+12,.0f}円  DD{orb_s['max_dd']:>+6.1f}%")
    print(f"  Donchian: {don_s['n']:>4}取引  勝率{don_s['win_rate']:>5.1f}%  "
          f"PF{_pf(don_s['pf']):>6}  {don_s['total_pnl']:>+12,.0f}円  DD{don_s['max_dd']:>+6.1f}%")

    print()
    print(f"  実運用想定 (1日1銘柄)")
    print("=" * 70)
    fo_s = calc_stats(first_orb_all)
    fd_s = calc_stats(first_don_all)
    print(f"  ORB     : {fo_s['n']:>4}取引  勝率{fo_s['win_rate']:>5.1f}%  "
          f"PF{_pf(fo_s['pf']):>6}  {fo_s['total_pnl']:>+12,.0f}円  DD{fo_s['max_dd']:>+6.1f}%")
    print(f"  Donchian: {fd_s['n']:>4}取引  勝率{fd_s['win_rate']:>5.1f}%  "
          f"PF{_pf(fd_s['pf']):>6}  {fd_s['total_pnl']:>+12,.0f}円  DD{fd_s['max_dd']:>+6.1f}%")

    # HTML
    out = Path(f"replay_period_{from_date.replace('-','')}_{to_date.replace('-','')}.html")
    out.write_text(build_html(orb_all, don_all, first_orb_all, first_don_all,
                              dates, from_date, to_date, args.strategy),
                   encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
