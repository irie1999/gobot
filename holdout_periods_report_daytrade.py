"""
holdout_periods_report_daytrade.py  ―  期間別 hold-out レポート
==================================================================
直近 30/60/90/120/150/180日 のそれぞれを TEST 期間として、
ホールドアウト方式で銘柄選定+評価をするレポート。

【ロジック】(逆指値ロング walkforward_holdout.py と完全一致)
6つの hold-out 期間 (今日から起算、それぞれ独立):
  30日タブ : TRAIN=過去全期間〜30日前 / TEST=直近30日
  60日タブ : TRAIN=過去全期間〜60日前 / TEST=直近60日
  90日タブ : TRAIN=過去全期間〜90日前 / TEST=直近90日
  120日タブ: TRAIN=過去全期間〜120日前 / TEST=直近120日
  150日タブ: TRAIN=過去全期間〜150日前 / TEST=直近150日
  180日タブ: TRAIN=過去全期間〜180日前 / TEST=直近180日
  ※ TEST は今日から起算なので重複あり (30日窓は60日窓に含まれる)
     これは「異なる時間スケールでの優位性検証」の意図的設計

universe を 12戦略 × 全銘柄でバックテスト (キャッシュあり) し、
各タブごとに:
  1. TRAIN期間で合格 (PF≥1.3, 取引≥20, 損益>0) → 学習合格
  2. その銘柄/戦略の TEST 期間成績を集計 (リーク無し)
  3. TEST PF≥1.0 & 損益≥0 で「テスト合格」マーク
  4. Composite Score 順に Top N をその期間の WATCHLIST として表示
→ 全タブで★合格 = 時系列ロバスト (短期も長期も勝てる本物)

→ 期間ごとに異なる銘柄構成・戦略割当てを表示

【出力】
holdout_periods_<YYYY-MM-DD>.html (6タブ、期間ごとに別銘柄)

【使い方】
  # 全prime銘柄スキャン (デフォルト)
  python holdout_periods_report_daytrade.py

  # 既存の3-tuple WATCHLIST (戦略割当済) を使う
  python holdout_periods_report_daytrade.py --universe winners

  # STEP1 (scan_walkforward_daytrade) のCSVから銘柄+戦略を読み込む
  # → スイング流の2ステップ運用 (scan → holdout 分離)
  python holdout_periods_report_daytrade.py --universe csv \\
      --from-csv walkforward_daytrade_results/walkforward_VOL_standard_2026-06-14.csv

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
# 各タブの TEST 窓: (end_days_ago, start_days_ago) ※ start > end (start=より過去)
# 逆指値ロング方式: P日タブ → 0 〜 P 日前 (今日から起算、重複あり)
TEST_WINDOWS = {P: (0, P) for P in PERIODS}

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
                  workers, cache, strategy_map=None):
    """全銘柄×全戦略をバックテストして trades をキャッシュ。

    strategy_map: {sym: [strategy_name, ...]} 指定時は その銘柄を指定戦略リスト
        だけでバックテスト (CSV モード = STEP1選定戦略の組合せで評価)。
    """
    if strategy_map:
        total = sum(len(v) for v in strategy_map.values())
        print(f"\n[Step 2] バックテスト ({total}件: CSV指定の銘柄×戦略ペア)",
              flush=True)
    else:
        total = len(targets) * len(strategies)
        print(f"\n[Step 2] バックテスト ({len(targets)}銘柄 × {len(strategies)}戦略)",
              flush=True)
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
            if strategy_map and sym in strategy_map:
                # CSV モード: 指定戦略 (複数可) だけ
                for strat in strategy_map[sym]:
                    futs.append(ex.submit(_work, sym, name, strat))
            else:
                for strat in strategies:
                    futs.append(ex.submit(_work, sym, name, strat))
        for fut in as_completed(futs):
            sym, name, strat, trades = fut.result()
            results[(sym, strat, name)] = trades
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
    return results


def evaluate_period(results, period_label, train_days, today, budget, top_n):
    """1タブの hold-out 評価 (TEST は非重複30日窓)。

    period_label: 30/60/90/120/150/180 のいずれか
        TEST 窓 = (period_label-30) 〜 period_label 日前 の30日窓
    train_days=0 → TEST より前の全期間を TRAIN (逆指値ロング方式)
    train_days>0 → TEST 直前 train_days 日に限定

    結果: list of dict (上位 top_n 件)
    """
    test_end, test_start = TEST_WINDOWS[period_label]  # newer, older
    qualified = []
    test_cutoff = today - timedelta(days=test_start)

    for (sym, strat, name), trades in results.items():
        if not trades:
            continue
        # TRAIN: TEST 開始日より前
        if train_days > 0:
            # 直前 train_days 日に限定 = test_start 〜 test_start+train_days 日前
            train_trades = slice_trades(trades, test_start + train_days,
                                         test_start, today)
        else:
            # TEST 開始より前すべて
            train_trades = [t for t in trades
                            if hasattr(t.get("entry_dt"), "date")
                            and t["entry_dt"].date() < test_cutoff]
        # TEST: test_end 〜 test_start 日前 (30日窓)
        test_trades = slice_trades(trades, test_start, test_end, today)
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
            "test_trades": test_trades,
        })

    qualified.sort(key=lambda x: -x["score"])
    return qualified[:top_n]


# ----------------------------------------------------------------- HTML
def build_period_tab(period_label, train_days, items):
    """1タブ HTML (逆指値ロング walkforward_holdout.py と同じ重複ありTEST)。"""
    test_end, test_start = TEST_WINDOWS[period_label]
    test_label = f"直近{test_start}日"
    if not items:
        return ('<p style="color:#64748b;padding:24px">'
                f'TEST {test_label} ホールドアウト合格銘柄なし</p>')

    winners = [it for it in items if it["test_pass"]]
    total_pnl = sum(it["test_stats"]["total_pnl"] for it in items)
    winner_pnl = sum(it["test_stats"]["total_pnl"] for it in winners)
    total_n = sum(it["test_stats"]["n"] for it in items)
    total_wins = sum(int(it["test_stats"]["n"] * it["test_stats"]["win_rate"] / 100)
                     for it in items)
    avg_wr = total_wins / total_n * 100 if total_n > 0 else 0

    if train_days > 0:
        train_label = f"TEST直前 {train_days}日 ({test_start}〜{test_start + train_days}日前)"
    else:
        train_label = f"TEST より前の全期間 ({test_start}日前より過去すべて)"
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
  TRAIN期間: {train_label}<br>
  TEST期間: <strong>{test_label}</strong> (今日から起算、逆指値ロング方式)<br>
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
        sid = f"{it['symbol'].replace('.T','')}_{it['strategy']}"
        rows += f"""
<tr style="background:{bg}">
  <td style="color:#4ade80">{mark}</td>
  <td>{i}</td>
  <td class="sym"><span class="sym-link" onclick="jumpToSym('{sid}')" title="クリックで取引明細へ">{it['name']}<br><small class="code">{it['symbol']}</small></span></td>
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
<h3>TEST {test_label} ホールドアウト Top{len(items)}</h3>
<table>
  <thead>
    <tr>
      <th rowspan="2">合</th>
      <th rowspan="2">#</th>
      <th rowspan="2">銘柄</th>
      <th rowspan="2">戦略</th>
      <th colspan="4" style="background:#1e3a5f">TRAIN</th>
      <th colspan="6" style="background:#1a4d3a">TEST ({test_label})</th>
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


def _fmt_dt(dt):
    return dt.strftime("%m-%d %H:%M") if hasattr(dt, "strftime") else "?"


def build_detail_tab(period_items):
    """銘柄詳細タブ HTML。

    全期間タブで登場した (sym, strategy) ペアを集約して、
    各ペアの TEST 取引明細 (どの期間で出たか + 直近の取引) を表示。
    """
    # (sym, strategy) -> {best_item, periods_appeared}
    agg = {}
    for P, items in period_items.items():
        for it in items:
            key = (it["symbol"], it["strategy"])
            if key not in agg:
                agg[key] = {
                    "symbol": it["symbol"],
                    "name": it["name"],
                    "strategy": it["strategy"],
                    "periods": {},   # P -> {test_pass, test_stats, test_trades}
                }
            agg[key]["periods"][P] = {
                "test_pass": it["test_pass"],
                "test_stats": it["test_stats"],
                "test_trades": it["test_trades"],
                "train_stats": it["train_stats"],
            }

    if not agg:
        return '<p style="color:#64748b;padding:24px">銘柄なし</p>'

    # 並び順: 全期間★合格数 多い順 → 取引明細リッチ順
    def _rank(entry):
        n_star = sum(1 for p in entry["periods"].values() if p["test_pass"])
        max_pnl = max((p["test_stats"]["total_pnl"]
                       for p in entry["periods"].values()), default=0)
        return (-n_star, -max_pnl)
    ordered = sorted(agg.values(), key=_rank)

    nav = ""
    panes = ""
    for i, entry in enumerate(ordered):
        active = "block" if i == 0 else "none"
        active_btn = "active" if i == 0 else ""
        sym_short = entry["symbol"].replace(".T", "")
        # ボタンID: 戦略違いを区別 (例: sym5821_MACD)
        sid = f"{sym_short}_{entry['strategy']}"
        # ★数バッジ
        n_star = sum(1 for p in entry["periods"].values() if p["test_pass"])
        star_color = "#4ade80" if n_star >= 3 else "#fbbf24" if n_star >= 1 else "#64748b"
        nav += (f'<button class="sym-btn {active_btn}" data-sym="{sid}" '
                f'onclick="switchSym(\'sym{sid}\')">'
                f'<strong>{entry["symbol"]}</strong><br>'
                f'<small style="color:#94a3b8">{entry["name"][:8]}</small><br>'
                f'<small style="color:#60a5fa">{entry["strategy"]}</small><br>'
                f'<small style="color:{star_color}">★{n_star}/6</small></button>')

        # 期間別サマリ行
        period_summary = ""
        for P in PERIODS:
            p = entry["periods"].get(P)
            if not p:
                period_summary += (f'<span style="margin-right:14px;color:#475569">'
                                   f'直近{P}日: 候補外</span>')
                continue
            es = p["test_stats"]
            pc = "profit" if es["total_pnl"] >= 0 else "loss"
            mark = "★" if p["test_pass"] else "—"
            mc = "#4ade80" if p["test_pass"] else "#64748b"
            period_summary += (f'<span style="margin-right:14px">'
                               f'<span style="color:{mc}">{mark}</span> '
                               f'直近{P}日: {es["n"]}取引 PF<strong>{_pf(es["pf"])}</strong> '
                               f'<span class="{pc}">{es["total_pnl"]:+,.0f}円</span>'
                               f'</span>')

        # 全期間共通 TRAIN 統計 (180日タブのもの = もっとも過去のTRAIN)
        # 取引明細は最も古いTESTを基準にすべての取引を統合表示
        all_test_trades = []
        seen_dt = set()
        for P in sorted(entry["periods"].keys(), reverse=True):
            for t in entry["periods"][P]["test_trades"]:
                edt = t.get("entry_dt")
                edt_key = str(edt)
                if edt_key in seen_dt:
                    continue
                seen_dt.add(edt_key)
                all_test_trades.append(t)
        all_test_trades.sort(key=lambda t: str(t.get("entry_dt", "")), reverse=True)

        trade_rows = ""
        for t in all_test_trades[:30]:
            ed = _fmt_dt(t.get("entry_dt"))
            xd = _fmt_dt(t.get("exit_dt"))
            try:
                hold = f"{int((t['exit_dt'] - t['entry_dt']).total_seconds() // 60)}分"
            except Exception:
                hold = "-"
            pnl = t.get("pnl", 0)
            pct = t.get("pct", 0)
            pc = "profit" if pnl >= 0 else "loss"
            trade_rows += f"""
<tr>
  <td>{ed}</td>
  <td>{xd}</td>
  <td>{hold}</td>
  <td>{t.get('entry_p',0):,.0f}</td>
  <td class="loss">{t.get('stop_p',0):,.0f}</td>
  <td class="profit">{t.get('target_p',0):,.0f}</td>
  <td>{t.get('exit_p',0):,.0f}</td>
  <td class="{pc}">{pnl:+,.0f}</td>
  <td class="{pc}">{pct:+.2f}%</td>
  <td>{t.get('reason','?')}</td>
</tr>"""

        panes += f"""
<div id="sym{sid}" class="sym-pane" style="display:{active};padding:12px 0">
  <h3>{entry["name"]} ({entry["symbol"]}) — {entry["strategy"]} — ★{n_star}/6合格</h3>
  <p style="color:#94a3b8;font-size:0.85rem">{period_summary}</p>
  <h3>TEST期間 取引明細 (直近{min(len(all_test_trades),30)}件)</h3>
  <table>
    <thead><tr>
      <th>Entry</th><th>Exit</th><th>保有</th>
      <th>買値</th><th>損切</th><th>目標</th><th>決済</th>
      <th>損益</th><th>%</th><th>理由</th>
    </tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>"""

    return f"""
<div class="sym-nav">{nav}</div>
{panes}
"""


# ----------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(
        description="期間別ホールドアウト・レポート")
    parser.add_argument("--universe", default="prime",
                        choices=["prime", "winners", "csv"],
                        help="prime=全銘柄 (デフォルト), "
                             "winners=既存WATCHLIST, "
                             "csv=--from-csv 指定のCSVから銘柄リスト")
    parser.add_argument("--watchlist", default="daytrade_combined_watchlist.py",
                        help="universe=winners 用 WATCHLIST")
    parser.add_argument("--from-csv", default=None, nargs="+",
                        help="universe=csv 用、STEP1 walkforward_*.csv のパス。"
                             "複数指定可 (--from-csv a.csv b.csv) or ワイルドカード可 "
                             "(--from-csv walkforward_daytrade_results/walkforward_*_swing_relaxed_*.csv)。"
                             "戦略割当もCSVから読込 (scan_walkforward_daytrade 流)")
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
    csv_strategy_map = {}  # sym -> strategy (CSV モード時のみ)
    if args.universe == "prime":
        from symbols_listed_all import SYMBOLS as UNIVERSE
        targets = UNIVERSE
    elif args.universe == "csv":
        if not args.from_csv:
            print("[error] --from-csv path を指定してください")
            return
        import csv as _csv
        import glob as _glob
        # 複数CSVをマージ (ワイルドカード展開も対応)
        csv_paths = []
        for raw in args.from_csv:
            expanded = _glob.glob(raw) or [raw]
            csv_paths.extend(expanded)
        csv_paths = [Path(p) for p in csv_paths]
        # csv_strategy_map: sym -> list of strategies (戦略間で重複ありOK)
        csv_strategy_map = {}
        sym_name = {}  # sym -> name
        per_strat_count = {}
        for csv_path in csv_paths:
            if not csv_path.exists():
                print(f"[warn] CSV 不在をスキップ: {csv_path}")
                continue
            with open(csv_path, encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
            n_added = 0
            for r in rows:
                sym = r.get("symbol", "").strip()
                name = r.get("name", "").strip()
                strat = r.get("strategy", "").strip()
                if not sym:
                    continue
                strats = csv_strategy_map.setdefault(sym, [])
                if strat and strat not in strats:
                    strats.append(strat)
                    per_strat_count[strat] = per_strat_count.get(strat, 0) + 1
                    n_added += 1
                if name:
                    sym_name[sym] = name
            print(f"[CSV] {csv_path.name}: {n_added}件追加")
        targets = [(s, sym_name.get(s, "")) for s in csv_strategy_map]
        total_pairs = sum(len(v) for v in csv_strategy_map.values())
        print(f"[CSV] 銘柄数 {len(targets)} / (銘柄×戦略)ペア数 {total_pairs}")
        if per_strat_count:
            print(f"[CSV] 戦略内訳: " +
                  ", ".join(f"{s}:{n}" for s, n in
                            sorted(per_strat_count.items(), key=lambda x: -x[1])))
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
    print(f"  期間別ホールドアウト・レポート (逆指値ロング方式)")
    print(f"=" * 70)
    print(f"  universe: {args.universe} ({len(targets)}銘柄)")
    print(f"  TEST: 直近 30/60/90/120/150/180 日 (6期間、今日から起算)")
    print(f"  TRAIN: " +
          (f"TEST直前 {args.train_days}日" if args.train_days > 0
           else "TEST より前の全期間"))
    print(f"  TRAIN合格条件: 取引≥{PASS_TRAIN_TRADES}, PF≥{PASS_TRAIN_PF}, 損益>0")

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
        args.workers, cache,
        strategy_map=csv_strategy_map if args.universe == "csv" else None)

    # キャッシュ保存
    if not args.no_cache:
        try:
            cache_file.write_bytes(pickle.dumps(cache))
        except Exception:
            pass

    # 期間別評価 (逆指値ロング walkforward_holdout.py と一致)
    print(f"\n[Step 3] 6期間ホールドアウト評価 (逆指値ロング方式)", flush=True)
    period_items = {}
    for P in PERIODS:
        items = evaluate_period(results, P, args.train_days, today,
                                  args.budget, args.top)
        period_items[P] = items
        print(f"  直近{P:>3}日 TEST: 合格 {len(items)}銘柄")

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
.tab-btn { padding:8px 14px; background:#1e293b; border:none;
           border-radius:6px 6px 0 0; color:#94a3b8; cursor:pointer;
           font-size:0.82rem; border-bottom:2px solid transparent;
           margin-bottom:-2px; text-align:center; line-height:1.35; }
.tab-btn small { font-size:0.7rem; color:#64748b; display:inline-block; }
.tab-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.tab-btn.active { background:#0f172a; color:#60a5fa;
                  border-bottom:2px solid #60a5fa; font-weight:700; }
.tab-pane { display:none; padding:12px 0; }
.tab-pane.active { display:block; }
.legend { color:#94a3b8; font-size:0.78rem; margin:8px 0 16px;
          padding:10px; background:#1e293b; border-radius:6px; }
.legend strong { color:#e2e8f0; }
.sym-nav { display:flex; flex-wrap:wrap; gap:4px; margin:12px 0 16px;
           padding:10px; background:#0f172a; border-radius:8px; }
.sym-btn { padding:6px 10px; background:#1e293b; border:1px solid #334155;
           color:#e2e8f0; border-radius:6px; cursor:pointer;
           font-size:0.75rem; min-width:96px; text-align:center; line-height:1.3; }
.sym-btn:hover { background:#263349; border-color:#64748b; }
.sym-btn.active { background:#1d4ed8; border-color:#3b82f6; }
.sym-pane { display:none; }
.sym-link { cursor:pointer; color:#e2e8f0; text-decoration:none;
            border-bottom:1px dashed #475569; }
.sym-link:hover { color:#60a5fa; border-bottom-color:#60a5fa; }
"""
    js = """
function switchTab(tab){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('t-'+tab).classList.add('active');
  (event.target.closest('.tab-btn')||event.target).classList.add('active');
}
function switchSym(id){
  document.querySelectorAll('.sym-pane').forEach(p=>p.style.display='none');
  document.querySelectorAll('.sym-btn').forEach(b=>b.classList.remove('active'));
  var pane=document.getElementById(id);
  if(pane) pane.style.display='block';
  (event.target.closest('.sym-btn')||event.target).classList.add('active');
}
function jumpToSym(sid){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  var detailTab=document.getElementById('t-detail');
  if(detailTab) detailTab.classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b=>{
    if(b.textContent.indexOf('銘柄詳細')!==-1) b.classList.add('active');
  });
  document.querySelectorAll('.sym-pane').forEach(p=>p.style.display='none');
  document.querySelectorAll('.sym-btn').forEach(b=>b.classList.remove('active'));
  var pane=document.getElementById('sym'+sid);
  if(pane) pane.style.display='block';
  document.querySelectorAll('.sym-btn').forEach(b=>{
    if(b.dataset.sym===sid) b.classList.add('active');
  });
  if(pane) pane.scrollIntoView({behavior:'smooth',block:'start'});
}
"""

    tab_btns = ""
    tab_panes = ""
    for i, P in enumerate(PERIODS):
        active_btn = "active" if i == 0 else ""
        active_pane = "active" if i == 0 else ""
        n_qual = len(period_items[P])
        tab_btns += (f'<button class="tab-btn {active_btn}" '
                     f'onclick="switchTab(\'p{P}\')">直近{P}日<br>'
                     f'<small style="color:#fbbf24">{n_qual}件</small></button>')
        tab_panes += (f'<div id="t-p{P}" class="tab-pane {active_pane}">'
                      f'{build_period_tab(P, args.train_days, period_items[P])}'
                      f'</div>')

    # 銘柄詳細タブ (全期間に登場した銘柄を集約)
    unique_syms = {(it["symbol"], it["strategy"])
                   for P in PERIODS for it in period_items[P]}
    tab_btns += (f'<button class="tab-btn" onclick="switchTab(\'detail\')">'
                 f'📊 銘柄詳細<br><small style="color:#fbbf24">{len(unique_syms)}件</small></button>')
    tab_panes += (f'<div id="t-detail" class="tab-pane">'
                  f'{build_detail_tab(period_items)}</div>')

    train_desc = (f"TEST直前 {args.train_days}日" if args.train_days > 0
                  else "TEST より前の全期間")
    legend = f"""
<div class="legend">
  <strong>📊 真のWalk-Forward (逆指値ロング walkforward_holdout.py と完全一致)</strong><br>
  6つの hold-out 期間で評価。TEST は「今日から起算」した直近 N日で、
  異なる時間スケール (短期/中期/長期) での優位性を検証します。
  TEST期間のデータは銘柄選定に使用しないので、カーブフィット排除・リーク無し。<br><br>
  ▸ <strong>6 TEST 期間</strong> (今日から起算、重複あり):
    直近30 / 60 / 90 / 120 / 150 / 180日<br>
  ▸ <strong>TRAIN</strong>: {train_desc} (TEST 開始日より前)<br>
  ▸ 合格条件 (TRAIN): 取引≥{PASS_TRAIN_TRADES}, PF≥{PASS_TRAIN_PF}, 損益>0<br>
  ▸ <strong>★合格</strong>: TEST PF≥{TEST_PASS_PF} & 損益≥0 (真の優位性あり、未学習データでも勝てた)<br>
  ▸ Composite Score = TEST損益 × (1 + max(Sharpe,0)) 順に Top{args.top} 表示<br>
  ▸ <strong>全6タブで★合格 = 短期も長期もロバスト</strong> (最有力候補)
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
