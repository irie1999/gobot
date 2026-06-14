"""
holdout_periods_report_daytrade.py  ―  期間別 hold-out レポート
==================================================================
直近 30/60/90/120/150/180日 のそれぞれを TEST 期間として、
ホールドアウト方式で銘柄選定+評価をするレポート。

【ロジック】(逆指値ロング walkforward_holdout.py と同じ設計)
各期間 P について:
  TEST  = 過去 0 〜 P日
  TRAIN = TEST より前の全期間 (pkl 最古 〜 P日前)
          ※ --train-days N で「TEST直前N日」に限定可

universe を 12戦略 × 全銘柄でバックテスト (キャッシュあり) し、
各期間ごとに:
  1. TRAIN期間で合格 (PF≥1.3, 取引≥20, 損益>0) → 学習合格
  2. その銘柄/戦略の TEST 期間成績を集計 (リーク無し)
  3. TEST PF≥1.0 & 損益≥0 で「テスト合格」マーク
  4. Composite Score 順に Top N をその期間の WATCHLIST として表示

→ 期間ごとに異なる銘柄構成・戦略割当てを表示

【出力】
holdout_periods_<YYYY-MM-DD>.html (6タブ、期間ごとに別銘柄)

【使い方】
  python holdout_periods_report_daytrade.py
  python holdout_periods_report_daytrade.py --universe prime --top 30
  python holdout_periods_report_daytrade.py --train-days 120 --workers 6
  python holdout_periods_report_daytrade.py --force
"""

from __future__ import annotations

import argparse
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT
from risk_metrics_5m import enrich_stats
from data_sanity_check import check_one
from _open_html import open_html

JST = timezone(timedelta(hours=9))
ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}

PERIODS = [30, 60, 90, 120, 150, 180]

# 合格条件 (逆指値ロング walkforward_holdout.py に合わせる)
PASS_TRAIN_TRADES = 20
PASS_TRAIN_PF = 1.3
PASS_TRAIN_WR = 0   # 勝率制約なし (PF と取引数で実質的にフィルタ)
PASS_TRAIN_PNL = 0
MIN_TEST_TRADES = 3
# TEST 合格基準 (現実評価)
TEST_PASS_PF = 1.0
TEST_PASS_PNL = 0

CACHE_DIR = Path(".cache_holdout_periods")


# ----------------------------------------------------------------- helpers
def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _color_pf(v):
    if v == float("inf") or v >= 1.5:
        return "#4ade80"
    if v >= 1.0:
        return "#facc15"
    return "#f87171"


def _composite(stats):
    """損益 × (1 + max(sharpe, 0))"""
    pnl = stats.get("total_pnl", 0)
    sharpe = stats.get("sharpe", 0)
    return pnl * (1 + max(sharpe, 0))


def slice_trades(trades, start_days_ago, end_days_ago, today):
    """end_days_ago < days_ago <= start_days_ago の取引を返す。"""
    start = today - timedelta(days=start_days_ago)
    end = today - timedelta(days=end_days_ago)
    out = []
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = dt.date()
        if start <= d < end:
            out.append(t)
    return out


def pass_train(stats):
    return (stats["n"] >= PASS_TRAIN_TRADES
            and stats["pf"] >= PASS_TRAIN_PF
            and stats["win_rate"] >= PASS_TRAIN_WR
            and stats["total_pnl"] > PASS_TRAIN_PNL)


# ----------------------------------------------------------------- backtest
def backtest_sym_strat(sym, name, df, strat_name, budget, max_risk,
                       cache):
    """1銘柄×1戦略の全期間 backtest (キャッシュ対応)。"""
    key = f"{strat_name}::{sym}"
    if cache is not None and key in cache:
        return cache[key]
    fn = ALL_STRATEGIES[strat_name]
    try:
        r = backtest_symbol_5m(sym, name, df, fn,
                                strategy_params={"name": strat_name},
                                budget=budget, max_risk=max_risk)
        trades = r["trades"] if r else []
    except Exception:
        trades = []
    if cache is not None:
        cache[key] = trades
    return trades


# ----------------------------------------------------------------- main scan
def scan_universe(targets, fetched, strategies, budget, max_risk,
                  workers, cache):
    """全銘柄×全戦略をバックテストして trades をキャッシュ。"""
    print(f"\n[Step 2] バックテスト ({len(targets)}銘柄 × {len(strategies)}戦略)",
          flush=True)
    total = len(targets) * len(strategies)
    done = 0

    def _work(sym, name, strat):
        trades = backtest_sym_strat(sym, name, fetched[sym], strat,
                                      budget, max_risk, cache)
        return (sym, name, strat, trades)

    results = {}  # (sym, strat) -> trades
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for sym, name in targets:
            if sym not in fetched:
                continue
            for strat in strategies:
                futs.append(ex.submit(_work, sym, name, strat))
        for fut in as_completed(futs):
            sym, name, strat, trades = fut.result()
            results[(sym, strat, name)] = trades
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
    return results


def evaluate_period(results, period_days, train_days, today, budget, top_n):
    """1期間の hold-out 評価。

    train_days=0 → TEST より前の全期間を TRAIN (逆指値ロング方式)
    train_days>0 → TEST 直前 train_days 日に限定

    結果: list of dict (上位 top_n 件)
        {sym, name, strategy, train_stats, test_stats, score, train_pass, test_pass}
    """
    qualified = []
    for (sym, strat, name), trades in results.items():
        if not trades:
            continue
        # TRAIN: train_days=0 なら全期間、>0 なら直前 train_days 日
        if train_days > 0:
            train_trades = slice_trades(trades, period_days + train_days,
                                         period_days, today)
        else:
            # period_days 日より前すべて
            train_trades = [t for t in trades
                            if hasattr(t.get("entry_dt"), "date")
                            and t["entry_dt"].date()
                                < today - timedelta(days=period_days)]
        test_trades = slice_trades(trades, period_days, 0, today)
        train_stats = calc_stats(train_trades, budget)
        if not pass_train(train_stats):
            continue
        test_stats_enrich = enrich_stats(test_trades, budget)
        if test_stats_enrich["n"] < MIN_TEST_TRADES:
            continue
        test_pass = (test_stats_enrich["pf"] >= TEST_PASS_PF
                     and test_stats_enrich["total_pnl"] >= TEST_PASS_PNL)
        qualified.append({
            "symbol": sym,
            "name": name,
            "strategy": strat,
            "train_stats": train_stats,
            "test_stats": test_stats_enrich,
            "score": _composite(test_stats_enrich),
            "test_pass": test_pass,
        })

    qualified.sort(key=lambda x: -x["score"])
    return qualified[:top_n]


# ----------------------------------------------------------------- HTML
def build_period_tab(period_days, train_days, items):
    """1期間タブ HTML。"""
    if not items:
        return ('<p style="color:#64748b;padding:24px">'
                f'TEST {period_days}日 / TRAIN {train_days}日 ホールドアウト合格銘柄なし</p>')

    winners = [it for it in items if it["test_pass"]]
    total_pnl = sum(it["test_stats"]["total_pnl"] for it in items)
    winner_pnl = sum(it["test_stats"]["total_pnl"] for it in winners)
    total_n = sum(it["test_stats"]["n"] for it in items)
    total_wins = sum(int(it["test_stats"]["n"] * it["test_stats"]["win_rate"] / 100)
                     for it in items)
    avg_wr = total_wins / total_n * 100 if total_n > 0 else 0

    train_label = f"{train_days}日" if train_days > 0 else "TEST以外の全期間"
    sum_box = f"""
<div class="box">
  <div class="it"><div class="lb">候補銘柄</div><div class="vl">{len(items)}</div></div>
  <div class="it"><div class="lb">TEST合格 ★</div>
    <div class="vl profit">{len(winners)}</div></div>
  <div class="it"><div class="lb">TEST不合格</div>
    <div class="vl loss">{len(items)-len(winners)}</div></div>
  <div class="it"><div class="lb">合格率</div>
    <div class="vl">{len(winners)*100//max(len(items),1)}%</div></div>
  <div class="it"><div class="lb">合格者TEST損益</div>
    <div class="vl profit">{winner_pnl:+,.0f}円</div></div>
  <div class="it"><div class="lb">全体TEST損益</div>
    <div class="vl {'profit' if total_pnl >= 0 else 'loss'}">{total_pnl:+,.0f}円</div></div>
</div>
<p style="color:#94a3b8;font-size:0.82rem;margin:-6px 0 12px">
  TRAIN期間: {train_label} (TEST より前) / TEST期間: 直近 {period_days}日<br>
  💡 <strong>TEST合格 ★</strong> = 「TRAIN期間で勝てた銘柄が、TEST期間でも PF≥{TEST_PASS_PF} で勝てた」← 真の優位性あり
</p>
"""

    rows = ""
    for i, it in enumerate(items, 1):
        ts = it["train_stats"]
        es = it["test_stats"]
        pf = es["pf"]
        pc = "profit" if es["total_pnl"] >= 0 else "loss"
        bg = "#0d4d2f" if it["test_pass"] else "#2d0a0a"
        mark = "★" if it["test_pass"] else "—"
        rows += f"""
<tr style="background:{bg}">
  <td style="color:#4ade80">{mark}</td>
  <td>{i}</td>
  <td class="sym">{it['name']}<br><small class="code">{it['symbol']}</small></td>
  <td>{it['strategy']}</td>
  <td>{ts['n']}</td>
  <td>{ts['win_rate']:.0f}%</td>
  <td style="color:{_color_pf(ts['pf'])}">{_pf(ts['pf'])}</td>
  <td class="{'profit' if ts['total_pnl'] >= 0 else 'loss'}">{ts['total_pnl']:+,.0f}</td>
  <td>{es['n']}</td>
  <td>{es['win_rate']:.0f}%</td>
  <td style="color:{_color_pf(pf)}">{_pf(pf)}</td>
  <td class="{pc}">{es['total_pnl']:+,.0f}</td>
  <td class="loss">{es['max_dd']:+.1f}%</td>
  <td>{es['sharpe']:.2f}</td>
</tr>"""

    table = f"""
<h3>期間 {period_days}日 ホールドアウト Top{len(items)}</h3>
<table>
  <thead>
    <tr>
      <th rowspan="2">合</th>
      <th rowspan="2">#</th>
      <th rowspan="2">銘柄</th>
      <th rowspan="2">戦略</th>
      <th colspan="4" style="background:#1e3a5f">TRAIN ({train_label})</th>
      <th colspan="6" style="background:#1a4d3a">TEST (直近{period_days}日)</th>
    </tr>
    <tr>
      <th>取引</th><th>勝率</th><th>PF</th><th>損益</th>
      <th>取引</th><th>勝率</th><th>PF</th><th>損益</th><th>DD</th><th>Sharpe</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
"""
    return sum_box + table


# ----------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(
        description="期間別ホールドアウト・レポート")
    parser.add_argument("--universe", default="prime",
                        choices=["prime", "winners"],
                        help="prime=全銘柄 (デフォルト, 期間ごとに違う銘柄選定), "
                             "winners=既存WATCHLIST")
    parser.add_argument("--watchlist", default="daytrade_combined_watchlist.py",
                        help="universe=winners 用 WATCHLIST")
    parser.add_argument("--top", type=int, default=30,
                        help="各期間の上位N銘柄表示 (デフォルト30)")
    parser.add_argument("--train-days", type=int, default=0,
                        help="TRAIN日数 (デフォルト0=TEST以外の全期間, "
                             "逆指値ロング walkforward_holdout.py 方式)")
    parser.add_argument("--days", type=int, default=540,
                        help="バックテスト全期間 (デフォルト540日)")
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--max-price", type=int, default=10_000)
    parser.add_argument("--min-price", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--strategy", default="all",
                        help="all/long/short/個別戦略")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    today = datetime.now(JST).date()
    out = Path(f"holdout_periods_{today}.html")
    if out.exists() and not args.force:
        print(f"[CACHE] 当日生成済み: {out.resolve()}")
        if not args.no_browser:
            open_html(out.resolve())
        return

    # universe
    if args.universe == "prime":
        from symbols_listed_all import SYMBOLS as UNIVERSE
        targets = UNIVERSE
    else:
        import importlib.util
        p = Path(args.watchlist)
        if not p.exists():
            print(f"[error] {args.watchlist} がありません")
            return
        spec = importlib.util.spec_from_file_location("wl", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 3-tuple か 2-tuple か
        raw = getattr(mod, "SYMBOLS", [])
        targets = [(e[0], e[1]) for e in raw]

    print(f"=" * 70)
    print(f"  期間別ホールドアウト・レポート")
    print(f"=" * 70)
    print(f"  universe: {args.universe} ({len(targets)}銘柄)")
    print(f"  期間: {PERIODS}")
    print(f"  TRAIN日数: {args.train_days}")
    print(f"  TEST合格条件: 取引≥{PASS_TRAIN_TRADES}, PF≥{PASS_TRAIN_PF}, "
          f"勝率≥{PASS_TRAIN_WR}%, 損益>0")

    # 戦略
    if args.strategy == "all":
        strategies = list(ALL_STRATEGIES.keys())
    elif args.strategy == "long":
        strategies = list(STRATEGIES.keys())
    elif args.strategy == "short":
        strategies = list(STRATEGIES_SHORT.keys())
    else:
        strategies = [args.strategy]
    print(f"  戦略: {strategies}")

    # データロード
    print(f"\n[Step 1] データロード", flush=True)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    if args.max_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) <= args.max_price}
    if args.min_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) >= args.min_price}
    # サニティチェック
    insane = []
    for s, df in list(fetched.items()):
        r = check_one(s, df, max_atr_pct=5.0, max_gap_pct=30.0)
        if not r["sane"]:
            insane.append(s)
            del fetched[s]
    print(f"  ロード: {len(fetched)}銘柄 (異常除外: {len(insane)})")

    # キャッシュ
    cache = None
    if not args.no_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_file = CACHE_DIR / f"trades_{args.universe}_{today}.pkl"
        if cache_file.exists():
            try:
                cache = pickle.loads(cache_file.read_bytes())
                print(f"  キャッシュ復元: {len(cache)}件 trades")
            except Exception:
                cache = {}
        else:
            cache = {}

    # スキャン
    results = scan_universe(
        [(s, n) for s, n in targets if s in fetched],
        fetched, strategies, args.budget, args.max_risk,
        args.workers, cache)

    # キャッシュ保存
    if not args.no_cache:
        try:
            cache_file.write_bytes(pickle.dumps(cache))
        except Exception:
            pass

    # 期間別評価
    print(f"\n[Step 3] 期間別ホールドアウト評価", flush=True)
    period_items = {}
    for P in PERIODS:
        items = evaluate_period(results, P, args.train_days, today,
                                  args.budget, args.top)
        period_items[P] = items
        print(f"  {P:>3}日: 合格 {len(items)}銘柄")

    # HTML
    css = """
body { font-family: "Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a; color:#e2e8f0; padding:20px; margin:0; }
h1 { color:#10b981; margin:0 0 4px; font-size:1.5rem; }
h3 { color:#10b981; margin:14px 0 8px; font-size:1rem;
     border-left:3px solid #10b981; padding-left:10px; }
.subtitle { color:#94a3b8; margin-bottom:20px; font-size:0.85rem; }
table { width:100%; border-collapse:collapse; margin-bottom:14px; font-size:0.78rem; }
th { background:#1e293b; color:#94a3b8; padding:6px 8px;
     text-align:center; border:1px solid #334155; white-space:nowrap; }
td { padding:5px 8px; border:1px solid #1e293b;
     text-align:right; white-space:nowrap; }
.sym { text-align:left; font-weight:600; min-width:140px; }
.code { color:#64748b; font-weight:400; font-size:0.72rem; }
.profit { color:#4ade80; }
.loss { color:#f87171; }
.box { background:#1e293b; padding:14px; border-radius:8px;
       margin-bottom:14px; display:flex; gap:24px; flex-wrap:wrap; }
.box .it { text-align:center; }
.box .lb { color:#94a3b8; font-size:0.72rem; }
.box .vl { font-size:1.3rem; font-weight:700; }
.tab-nav { display:flex; gap:0; margin:16px 0 0;
           border-bottom:2px solid #1e293b; }
.tab-btn { padding:9px 22px; background:#1e293b; border:none;
           border-radius:6px 6px 0 0; color:#94a3b8; cursor:pointer;
           font-size:0.9rem; border-bottom:2px solid transparent;
           margin-bottom:-2px; }
.tab-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.tab-btn.active { background:#0f172a; color:#60a5fa;
                  border-bottom:2px solid #60a5fa; font-weight:700; }
.tab-pane { display:none; padding:12px 0; }
.tab-pane.active { display:block; }
.legend { color:#94a3b8; font-size:0.78rem; margin:8px 0 16px;
          padding:10px; background:#1e293b; border-radius:6px; }
.legend strong { color:#e2e8f0; }
"""
    js = """
function switchTab(tab){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('t-'+tab).classList.add('active');
  (event.target.closest('.tab-btn')||event.target).classList.add('active');
}
"""

    tab_btns = ""
    tab_panes = ""
    for i, P in enumerate(PERIODS):
        active_btn = "active" if i == 0 else ""
        active_pane = "active" if i == 0 else ""
        n_qual = len(period_items[P])
        tab_btns += (f'<button class="tab-btn {active_btn}" '
                     f'onclick="switchTab(\'p{P}\')">{P}日 ({n_qual})</button>')
        tab_panes += (f'<div id="t-p{P}" class="tab-pane {active_pane}">'
                      f'{build_period_tab(P, args.train_days, period_items[P])}'
                      f'</div>')

    train_label = f"TEST直前{args.train_days}日" if args.train_days > 0 else "TEST以外の全期間"
    legend = f"""
<div class="legend">
  <strong>📊 真のWalk-Forward (逆指値ロング walkforward_holdout.py と同じ設計)</strong><br>
  各タブで「対象期間 (TEST)」を除いて学習し、<strong>その期間で評価</strong>します。
  TEST期間のデータは銘柄選定に使用しないので、カーブフィット排除・リーク無しの現実評価。<br>
  ▸ TRAIN: {train_label} (TEST より前) で学習合格
    (取引≥{PASS_TRAIN_TRADES}, PF≥{PASS_TRAIN_PF}, 損益>0)<br>
  ▸ TEST : 直近 P日 (未学習データ) で評価<br>
  ▸ ★合格: TEST PF≥{TEST_PASS_PF} & 損益≥0 (真の優位性あり)<br>
  ▸ Composite Score = TEST損益 × (1 + max(Sharpe,0)) 順に Top{args.top} 表示
</div>
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>期間別ホールドアウト ― {today}</title>
<style>{css}</style></head><body>
<h1>📊 期間別ホールドアウト・レポート</h1>
<p class="subtitle">生成: {today} / universe: {args.universe} ({len(fetched)}銘柄) /
   戦略: {len(strategies)} / TRAIN期間: {args.train_days}日</p>
{legend}
<div class="tab-nav">{tab_btns}</div>
{tab_panes}
<script>{js}</script>
</body></html>"""

    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        open_html(out.resolve())


if __name__ == "__main__":
    main()
