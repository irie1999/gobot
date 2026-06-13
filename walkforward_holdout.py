"""
walkforward_holdout.py  ―  ホールドアウト方式WFテスト (完全カーブフィット対策)
==================================================================
直近30/60/90/120/150/180日を「テスト期間」とし、それ以前のデータだけで
銘柄を選定。テスト期間で実成績を評価する真のwalk-forward。

【ロジック】
  for holdout_days in [30, 60, 90, 120, 150, 180]:
      test_end   = 今日
      test_start = 今日 - holdout_days
      train_end  = test_start
      train_start = 730日前 (or pkl の最古日)

      for sym in prime_stocks:
        全730日 backtest → トレードを train/test に分割
        train_stats を計算: PF/取引数/損益
        if train_stats が条件クリア (PF≥1.3, 取引≥20):
            候補とする
            test_stats を計算
            合格基準 (test PF≥1.0, 損益≥0) なら winner

【出力】
  walkforward_holdout_{holdout}d.csv  各期間のwinners
  walkforward_holdout_report.html      タブ付き比較HTML

【使い方】
  python walkforward_holdout.py
  python walkforward_holdout.py --holdouts 30,60,90
  python walkforward_holdout.py --min-train-pf 1.5
"""

from __future__ import annotations

import argparse
import csv
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats

JST = timezone(timedelta(hours=9))

DEFAULT_HOLDOUTS = [30, 60, 90, 120, 150, 180]


def _pf(v):
    if v == float("inf"):
        return "∞"
    if v == 0 or v is None:
        return "0.00"
    return f"{v:.2f}"


def split_trades_by_date(trades, cutoff_date):
    """トレードを cutoff_date 前後で2分割。"""
    train = []
    test = []
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = dt.date()
        if d < cutoff_date:
            train.append(t)
        else:
            test.append(t)
    return train, test


def run_one_holdout(targets, fetched, holdout_days, budget, max_risk,
                    min_train_pf, min_train_trades, min_test_trades):
    """1つのホールドアウト期間でWFテスト。"""
    today = datetime.now(JST).date()
    cutoff = today - timedelta(days=holdout_days)

    rows = []
    t0 = time.time()
    for i, (sym, name) in enumerate(targets, 1):
        if sym not in fetched:
            continue
        df = fetched[sym]
        # 全期間 backtest
        r = backtest_symbol(sym, name, df, budget, max_risk)
        if not r or not r["trades"]:
            continue

        train_trades, test_trades = split_trades_by_date(r["trades"], cutoff)
        if len(train_trades) < min_train_trades:
            continue
        if len(test_trades) < min_test_trades:
            continue

        train_stats = calc_stats(train_trades, budget)
        test_stats = calc_stats(test_trades, budget)

        # 訓練期間で勝ててる銘柄のみ採用
        if train_stats["pf"] < min_train_pf:
            continue
        if train_stats["total_pnl"] <= 0:
            continue

        rows.append({
            "symbol": sym,
            "name": name,
            "train_n": train_stats["n"],
            "train_pf": train_stats["pf"],
            "train_winrate": train_stats["win_rate"],
            "train_pnl": train_stats["total_pnl"],
            "train_dd": train_stats["max_dd"],
            "test_n": test_stats["n"],
            "test_pf": test_stats["pf"],
            "test_winrate": test_stats["win_rate"],
            "test_pnl": test_stats["total_pnl"],
            "test_dd": test_stats["max_dd"],
        })
        if i % 100 == 0 or i == len(targets):
            print(f"    [{holdout_days}日HO] {i}/{len(targets)} 処理済み "
                  f"({time.time()-t0:.1f}s, 候補:{len(rows)})", flush=True)

    return rows


def build_html(all_results, holdouts, today, budget, max_risk, min_train_pf,
                min_train_trades):
    """タブ付きHTMLレポート生成。"""

    def _pf_html(v):
        if v == float("inf"):
            return '<span style="color:#10b981">∞</span>'
        if v >= 1.5:
            color = "#4ade80"
        elif v >= 1.0:
            color = "#facc15"
        else:
            color = "#f87171"
        return f'<span style="color:{color};font-weight:600">{v:.2f}</span>'

    def _pnl_html(v):
        c = "profit" if v >= 0 else "loss"
        return f'<span class="{c}">{v:+,.0f}</span>'

    # タブナビ
    nav = ""
    panes = ""
    for i, holdout in enumerate(holdouts):
        active = "active" if i == 0 else ""
        display = "block" if i == 0 else "none"
        rows = all_results.get(holdout, [])
        nav += (f'<button class="tab-btn {active}" '
                f'onclick="switchTab(\'h{holdout}\')">{holdout}日</button>')

        # サマリ計算
        if rows:
            # テスト期間で勝てた銘柄 (PF >= 1.0 & PnL >= 0)
            winners = [r for r in rows
                       if r["test_pf"] >= 1.0 and r["test_pnl"] >= 0]
            losers = [r for r in rows
                      if not (r["test_pf"] >= 1.0 and r["test_pnl"] >= 0)]
            train_total = sum(r["train_pnl"] for r in rows)
            test_total = sum(r["test_pnl"] for r in rows)
            winner_pnl = sum(r["test_pnl"] for r in winners)

            # 銘柄テーブル (train PnL ソート上位50)
            sorted_rows = sorted(rows, key=lambda x: -x["test_pnl"])
            top_rows_html = ""
            for r in sorted_rows:
                is_winner = r["test_pf"] >= 1.0 and r["test_pnl"] >= 0
                bg = "#0d4d2f" if is_winner else "#2d0a0a"
                mark = "★" if is_winner else "—"
                top_rows_html += f"""
<tr style="background:{bg}">
  <td style="color:#4ade80">{mark}</td>
  <td class="sym">{r["name"]}<br><small class="code">{r["symbol"]}</small></td>
  <td>{r["train_n"]}</td>
  <td>{_pf_html(r["train_pf"])}</td>
  <td>{r["train_winrate"]:.0f}%</td>
  <td>{_pnl_html(r["train_pnl"])}</td>
  <td class="loss">{r["train_dd"]:+.1f}%</td>
  <td>{r["test_n"]}</td>
  <td>{_pf_html(r["test_pf"])}</td>
  <td>{r["test_winrate"]:.0f}%</td>
  <td>{_pnl_html(r["test_pnl"])}</td>
  <td class="loss">{r["test_dd"]:+.1f}%</td>
</tr>"""
            summary = f"""
<div class="box">
  <div class="it"><div class="lb">候補銘柄</div><div class="vl">{len(rows)}</div></div>
  <div class="it"><div class="lb">テスト合格</div>
    <div class="vl profit">{len(winners)}</div></div>
  <div class="it"><div class="lb">テスト不合格</div>
    <div class="vl loss">{len(losers)}</div></div>
  <div class="it"><div class="lb">勝率</div>
    <div class="vl">{len(winners)*100//max(len(rows),1)}%</div></div>
  <div class="it"><div class="lb">合格者のテスト損益</div>
    <div class="vl profit">{winner_pnl:+,.0f}円</div></div>
  <div class="it"><div class="lb">全体テスト損益</div>
    <div class="vl {('profit' if test_total >= 0 else 'loss')}">{test_total:+,.0f}円</div></div>
</div>

<p style="color:#94a3b8;font-size:0.82rem">
  💡 <strong>テスト合格</strong> = 「訓練期間で勝てた銘柄が、テスト期間でも PF≥1.0 で勝てた」
  ← 真の優位性ありの銘柄
</p>

<h3>銘柄別 訓練 → テスト 結果</h3>
<table>
  <thead><tr>
    <th>合</th><th>銘柄</th>
    <th colspan="5" style="background:#1e3a5f">訓練期間 (testより前)</th>
    <th colspan="5" style="background:#1e3a5f">テスト期間 (直近{holdout}日)</th>
  </tr><tr>
    <th></th><th></th>
    <th>取引</th><th>PF</th><th>勝率</th><th>損益</th><th>DD</th>
    <th>取引</th><th>PF</th><th>勝率</th><th>損益</th><th>DD</th>
  </tr></thead>
  <tbody>{top_rows_html}</tbody>
</table>
"""

            # CSV エクスポート用に winners を保存
            if winners:
                csv_path = Path(f"walkforward_holdout_{holdout}d.csv")
                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(winners[0].keys()))
                    w.writeheader()
                    w.writerows(winners)
        else:
            summary = '<p style="color:#64748b;padding:24px">候補なし</p>'

        panes += f"""
<div id="h{holdout}" class="tab-pane" style="display:{display}">
  <h2>ホールドアウト {holdout}日</h2>
  <p style="color:#94a3b8;font-size:0.85rem">
    訓練期間: pkl 最古 〜 {(datetime.now(JST).date() - timedelta(days=holdout)).strftime("%Y-%m-%d")}<br>
    テスト期間: {(datetime.now(JST).date() - timedelta(days=holdout)).strftime("%Y-%m-%d")} 〜 {datetime.now(JST).date().strftime("%Y-%m-%d")} ({holdout}日)<br>
    フィルタ: 訓練 PF≥{min_train_pf} & 取引≥{min_train_trades}
  </p>
  {summary}
</div>
"""

    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; margin: 0; }
h1 { color: #10b981; margin: 0 0 4px; font-size: 1.5rem; }
h2 { color: #10b981; margin: 16px 0 10px; font-size: 1.15rem;
     border-left: 3px solid #10b981; padding-left: 10px; }
h3 { color: #10b981; margin: 14px 0 8px; font-size: 1rem;
     border-left: 3px solid #10b981; padding-left: 10px; }
.subtitle { color: #94a3b8; margin-bottom: 20px; font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; margin-bottom: 14px;
        font-size: 0.78rem; }
th { background: #1e293b; color: #94a3b8; padding: 6px 8px;
     text-align: center; border: 1px solid #334155; white-space: nowrap; }
td { padding: 5px 8px; border: 1px solid #1e293b;
     text-align: right; white-space: nowrap; }
.sym { text-align: left; font-weight: 600; min-width: 140px; }
.code { color: #64748b; font-weight: 400; font-size: 0.72rem; }
.profit { color: #4ade80; }
.loss { color: #f87171; }
.box { background: #1e293b; padding: 14px; border-radius: 8px;
       margin-bottom: 14px; display: flex; gap: 24px; flex-wrap: wrap; }
.box .it { text-align: center; }
.box .lb { color: #94a3b8; font-size: 0.72rem; }
.box .vl { font-size: 1.2rem; font-weight: 700; }
.tab-nav { display: flex; gap: 0; margin: 16px 0 0;
           border-bottom: 2px solid #1e293b; padding-bottom: 0; }
.tab-btn { padding: 9px 22px; background: #1e293b; border: none;
           border-radius: 6px 6px 0 0; color: #94a3b8; cursor: pointer;
           font-size: 0.9rem; transition: all .15s;
           border-bottom: 2px solid transparent; margin-bottom: -2px; }
.tab-btn:hover:not(.active) { background: #263349; color: #e2e8f0; }
.tab-btn.active { background: #0f172a; color: #60a5fa;
                  border-bottom: 2px solid #60a5fa; font-weight: 700; }
.tab-pane { display: none; padding: 12px 0; }
"""

    js = """
function switchTab(tab) {
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tab).style.display = 'block';
  (event.target.closest('.tab-btn') || event.target).classList.add('active');
}
"""

    nav_html = f'<div class="tab-nav">{nav}</div>'

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>WF Holdout Report — {today}</title>
<style>{css}</style></head><body>
<h1>📊 ホールドアウト方式 Walk-Forward レポート</h1>
<p class="subtitle">生成: {today} / 予算: {budget:,}円 / リスク: {max_risk:,}円 /
  フィルタ: 訓練 PF≥{min_train_pf} 取引≥{min_train_trades}</p>
<p style="color:#fbbf24;font-size:0.85rem;background:#1e293b;padding:10px;border-radius:6px">
  💡 <strong>真のWalk-Forward</strong>: テスト期間のデータは銘柄選定に使用しない (カーブフィット排除)
</p>
{nav_html}
{panes}
<script>{js}</script>
</body></html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="ホールドアウトWFテスト")
    parser.add_argument("--holdouts", default="30,60,90,120,150,180",
                        help="ホールドアウト日数 (カンマ区切り)")
    parser.add_argument("--days", type=int, default=730,
                        help="全期間 (デフォルト730日)")
    parser.add_argument("--universe", default="prime",
                        choices=["prime", "winners"])
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--min-train-pf", type=float, default=1.3)
    parser.add_argument("--min-train-trades", type=int, default=20)
    parser.add_argument("--min-test-trades", type=int, default=3,
                        help="テスト期間の最低取引数")
    parser.add_argument("--max-price", type=int, default=10000)
    parser.add_argument("--min-price", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    holdouts = sorted(int(x) for x in args.holdouts.split(",") if x.strip())

    # ユニバース読み込み
    if args.universe == "prime":
        try:
            from symbols_listed_all import SYMBOLS
            targets = SYMBOLS
        except ImportError:
            print("[error] symbols_listed_all.py がありません")
            return
    else:
        try:
            from daytrade_donchian_winners import SYMBOLS
            targets = SYMBOLS
        except ImportError:
            print("[error] daytrade_donchian_winners.py がありません")
            return

    print(f"=" * 70)
    print(f"  ホールドアウト方式 Walk-Forward テスト")
    print(f"=" * 70)
    print(f"  ユニバース: {args.universe} ({len(targets)}銘柄)")
    print(f"  ホールドアウト: {holdouts}")
    print(f"  全期間: {args.days}日")
    print(f"  訓練フィルタ: PF≥{args.min_train_pf}, 取引≥{args.min_train_trades}")

    # データロード (1回だけ)
    print(f"\n[Step 1] データロード中...", flush=True)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    print(f"  ロード成功: {len(fetched)}銘柄")

    # 価格フィルタ
    before = len(fetched)
    if args.max_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) <= args.max_price}
    if args.min_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) >= args.min_price}
    if args.max_price > 0 or args.min_price > 0:
        print(f"  価格フィルタ: {before}→{len(fetched)}銘柄")
    targets = [(s, n) for s, n in targets if s in fetched]

    # 各ホールドアウトで実行
    print(f"\n[Step 2] WF backtest 実行中 (各銘柄を1回backtest → "
          f"トレードを期間別分割)", flush=True)
    all_results = {}
    for ho in holdouts:
        print(f"\n  --- ホールドアウト {ho}日 ---")
        rows = run_one_holdout(targets, fetched, ho,
                                args.budget, args.max_risk,
                                args.min_train_pf, args.min_train_trades,
                                args.min_test_trades)
        all_results[ho] = rows
        if rows:
            winners = [r for r in rows
                       if r["test_pf"] >= 1.0 and r["test_pnl"] >= 0]
            print(f"  → 候補:{len(rows)} 合格:{len(winners)} "
                  f"勝率:{len(winners)*100//max(len(rows),1)}%")

    # HTML生成
    today = datetime.now(JST).strftime("%Y-%m-%d")
    html = build_html(all_results, holdouts, today, args.budget,
                      args.max_risk, args.min_train_pf,
                      args.min_train_trades)
    out = Path(f"walkforward_holdout_report_{today}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
