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
  # 最短: 何も指定しないと最新CSVを自動検出
  # (walkforward_daytrade_results/ から最新日付 + 優先modeを自動採用)
  python holdout_periods_report_daytrade.py

  # 強制再生成
  python holdout_periods_report_daytrade.py --force

  # 特定 mode を指定
  python holdout_periods_report_daytrade.py --mode swing
  python holdout_periods_report_daytrade.py --mode standard

  # CSVパス直接指定
  python holdout_periods_report_daytrade.py --from-csv path/to/walkforward_*.csv

  # 既存の3-tuple WATCHLIST を使う
  python holdout_periods_report_daytrade.py --universe winners

  # 強制prime全銘柄スキャン (CSVあっても無視)
  python holdout_periods_report_daytrade.py --universe prime --force --no-auto

  # ショート戦略のみ (逆指値 run_signals_holdout_all.py --short と同じ役割)
  python holdout_periods_report_daytrade.py --short --max-price 6000 --min-price 1000

  # ロング戦略のみ
  python holdout_periods_report_daytrade.py --long-only
"""

from __future__ import annotations

import argparse
import os as _os
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
def _pkl_signature(sym):
    """対象pklのmtime+sizeを返す (整合性チェック用)。"""
    try:
        from daytrade_data import DATA_DIR, yf_to_jquants
        pkl = DATA_DIR / f"{yf_to_jquants(sym)}.pkl"
        if pkl.exists():
            st = pkl.stat()
            return (st.st_mtime, st.st_size)
    except Exception:
        pass
    return (0.0, 0)


def apply_same_day_lock(results):
    """同日同銘柄に複数戦略がエントリーした場合、最も早い entry_dt の
    戦略のみ残す (DAYTRADE_SAME_DAY_LOCK=1 時のみ呼ぶこと)。

    results: {(sym, strat, name): [trade,...]}
    戻り: 同形式 (deduplicate 済み)
    """
    earliest = {}  # (sym, day) -> (entry_dt, strat)
    for (sym, strat, _name), trades in results.items():
        for t in trades:
            dt = t.get("entry_dt")
            if not hasattr(dt, "date"):
                continue
            d = dt.date()
            key = (sym, d)
            cur = earliest.get(key)
            if cur is None or dt < cur[0] or (dt == cur[0] and strat < cur[1]):
                earliest[key] = (dt, strat)
    new_results = {}
    for (sym, strat, name), trades in results.items():
        kept = []
        for t in trades:
            dt = t.get("entry_dt")
            if not hasattr(dt, "date"):
                continue
            key = (sym, dt.date())
            sel = earliest.get(key)
            if sel and sel[1] == strat:
                kept.append(t)
        new_results[(sym, strat, name)] = kept
    return new_results


def backtest_sym_strat(sym, name, df, strat_name, budget, max_risk,
                       cache):
    """1銘柄×1戦略の全期間 backtest (永続キャッシュ対応)。

    cache 内のエントリーは {trades, pkl_mtime, pkl_size} の dict。
    pkl が更新されている場合のみ再 backtest して整合性を保つ。
    """
    key = f"{strat_name}::{sym}"
    cur_sig = _pkl_signature(sym)
    if cache is not None and key in cache:
        entry = cache[key]
        # 旧形式 (list) との互換: list なら無視して再計算
        if isinstance(entry, dict):
            cached_sig = (entry.get("pkl_mtime", 0.0),
                          entry.get("pkl_size", 0))
            if cached_sig == cur_sig and cached_sig != (0.0, 0):
                return entry.get("trades", [])
    fn = ALL_STRATEGIES[strat_name]
    try:
        r = backtest_symbol_5m(sym, name, df, fn,
                                strategy_params={"name": strat_name},
                                budget=budget, max_risk=max_risk)
        trades = r["trades"] if r else []
    except Exception:
        trades = []
    if cache is not None:
        cache[key] = {"trades": trades,
                       "pkl_mtime": cur_sig[0],
                       "pkl_size": cur_sig[1]}
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
def build_period_tab(period_label, train_days, items, id_prefix=""):
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
        es = it["test_stats"]
        pf = es["pf"]
        pc = "profit" if es["total_pnl"] >= 0 else "loss"
        bg = "#0d4d2f" if it["test_pass"] else "#2d0a0a"
        mark = "★" if it["test_pass"] else "—"
        sid = f"{id_prefix}{it['symbol'].replace('.T','')}_{it['strategy']}"
        # 利益と損を実取引から分離計算
        test_trades = it.get("test_trades", [])
        gross_profit = sum(t["pnl"] for t in test_trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in test_trades if t["pnl"] <= 0))
        rows += f"""
<tr style="background:{bg}">
  <td style="color:#4ade80">{mark}</td>
  <td>{i}</td>
  <td class="sym"><span class="sym-link" onclick="jumpToSym('{sid}')" title="クリックで取引明細へ">{it['name']}<br><small class="code">{it['symbol']}</small></span></td>
  <td>{it['strategy']}</td>
  <td>{es['n']}</td>
  <td>{es['win_rate']:.0f}%</td>
  <td style="color:{_color_pf(pf)}">{_pf(pf)}</td>
  <td class="profit">+{gross_profit:,.0f}</td>
  <td class="loss">-{gross_loss:,.0f}</td>
  <td class="{pc}">{es['total_pnl']:+,.0f}</td>
  <td class="loss">{es['max_dd']:+.1f}%</td>
  <td>{es['sharpe']:.2f}</td>
</tr>"""

    table = f"""
<h3>TEST {test_label} 取引結果 (構成銘柄 {len(items)}件)</h3>
<table>
  <thead>
    <tr>
      <th>合</th>
      <th>#</th>
      <th>銘柄</th>
      <th>戦略</th>
      <th>取引数</th>
      <th>勝率</th>
      <th>PF</th>
      <th>利益<br><small>(勝ち合計)</small></th>
      <th>損<br><small>(負け合計)</small></th>
      <th>損益<br><small>(差引)</small></th>
      <th>DD</th>
      <th>Sharpe</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
"""
    return sum_box + table


def _fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "strftime") else "?"


def _analyze_patterns(trades):
    """勝敗パターンを次元別に集計。

    返り値: {hour:{}, hold:{}, dow:{}, reason:{}}
        各値は {bucket_name: {win, loss, pnl}}
    """
    def _add(d, key, t):
        b = d.setdefault(key, {"win": 0, "loss": 0, "pnl": 0.0})
        if t["pnl"] > 0:
            b["win"] += 1
        else:
            b["loss"] += 1
        b["pnl"] += t["pnl"]

    by_hour = {}
    by_hold = {}
    by_dow = {}
    by_reason = {}
    hold_buckets = [
        ("<15分", lambda m: m < 15),
        ("15-30分", lambda m: 15 <= m < 30),
        ("30-60分", lambda m: 30 <= m < 60),
        ("60-120分", lambda m: 60 <= m < 120),
        (">=120分", lambda m: m >= 120),
    ]
    for t in trades:
        edt = t.get("entry_dt")
        if hasattr(edt, "hour"):
            _add(by_hour, f"{edt.hour:02d}:00台", t)
        if hasattr(edt, "weekday"):
            dow_names = ["月", "火", "水", "木", "金", "土", "日"]
            _add(by_dow, dow_names[edt.weekday()], t)
        try:
            mins = (t["exit_dt"] - t["entry_dt"]).total_seconds() // 60
            for name, fn in hold_buckets:
                if fn(mins):
                    _add(by_hold, name, t)
                    break
        except Exception:
            pass
        _add(by_reason, t.get("reason", "?"), t)

    return {"hour": by_hour, "hold": by_hold,
            "dow": by_dow, "reason": by_reason}


def _build_pattern_table(label, data, order=None):
    """1次元の勝敗パターンを HTML テーブル化。"""
    if not data:
        return f"<h4>{label}</h4><p style='color:#64748b'>データなし</p>"
    keys = order if order else sorted(data.keys())
    keys = [k for k in keys if k in data]
    rows = ""
    for k in keys:
        b = data[k]
        n = b["win"] + b["loss"]
        if n == 0:
            continue
        wr = b["win"] / n * 100
        avg = b["pnl"] / n
        # 勝率に色付け
        if wr >= 60:
            wr_color = "#4ade80"
        elif wr >= 40:
            wr_color = "#facc15"
        else:
            wr_color = "#f87171"
        pnl_cls = "profit" if b["pnl"] >= 0 else "loss"
        avg_cls = "profit" if avg >= 0 else "loss"
        # バーチャート (勝率)
        bar_w = int(wr)
        bar = (f'<div style="display:inline-block;width:80px;background:#334155;'
               f'height:8px;border-radius:4px;overflow:hidden;vertical-align:middle">'
               f'<div style="width:{bar_w}%;background:{wr_color};height:100%"></div>'
               f'</div>')
        rows += (f'<tr><td>{k}</td><td>{n}</td>'
                 f'<td><span style="color:{wr_color};font-weight:700">{wr:.0f}%</span> {bar} '
                 f'<small style="color:#94a3b8">({b["win"]}勝{b["loss"]}敗)</small></td>'
                 f'<td class="{pnl_cls}">{b["pnl"]:+,.0f}</td>'
                 f'<td class="{avg_cls}">{avg:+,.0f}</td></tr>')
    return f"""
<h4 style="color:#60a5fa;margin:8px 0 4px;font-size:0.9rem">{label}</h4>
<table style="font-size:0.78rem">
  <thead><tr>
    <th>区分</th><th>件数</th><th style="min-width:200px">勝率</th>
    <th>合計PnL</th><th>平均/取引</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
"""


def _build_patterns_section(all_test_trades):
    """銘柄詳細用、勝敗パターンの統合HTML。"""
    if not all_test_trades:
        return ""
    p = _analyze_patterns(all_test_trades)
    # 時刻バケットの順序
    hour_order = [f"{h:02d}:00台" for h in range(9, 16)]
    dow_order = ["月", "火", "水", "木", "金"]
    hold_order = ["<15分", "15-30分", "30-60分", "60-120分", ">=120分"]
    reason_order = ["目標達成", "損切り", "引け強制"]

    tables = [
        _build_pattern_table("⏰ Entry時刻別 (寄付き〜引け)", p["hour"], hour_order),
        _build_pattern_table("⏳ 保有時間別 (損切は短時間、勝ちは長め)", p["hold"], hold_order),
        _build_pattern_table("📅 曜日別", p["dow"], dow_order),
        _build_pattern_table("🎯 決済理由別", p["reason"], reason_order),
    ]
    return f"""
<h3>📊 勝敗パターン分析</h3>
<p style="color:#94a3b8;font-size:0.8rem;margin:-4px 0 8px">
  どの<strong>時間</strong>・<strong>保有時間</strong>・<strong>曜日</strong>で勝ちやすいか/負けやすいかを集計。
  勝率バー: <span style="color:#4ade80">緑=60%+</span> /
  <span style="color:#facc15">黄=40-60%</span> /
  <span style="color:#f87171">赤=&lt;40%</span>
</p>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
  <div>{tables[0]}</div>
  <div>{tables[1]}</div>
  <div>{tables[2]}</div>
  <div>{tables[3]}</div>
</div>
"""


def build_detail_tab(period_items, id_prefix=""):
    """銘柄詳細タブ HTML。

    全期間タブで登場した (sym, strategy) ペアを集約して、
    各ペアの TEST 取引明細 (どの期間で出たか + 直近の取引) を表示。

    id_prefix: --both モード時の ID 衝突回避用 ('L_' or 'S_')
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
        sid = f"{id_prefix}{sym_short}_{entry['strategy']}"
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

        # 取引明細は「最も長い期間」の trades を使う (重複バグ回避)
        # 例: 60日タブの trades = 30日タブの trades の super set なので
        #     最長期間 (max P) を採用すれば全取引網羅
        all_test_trades = []
        if entry["periods"]:
            max_P = max(entry["periods"].keys())
            all_test_trades = list(entry["periods"][max_P]["test_trades"])
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
  {_build_patterns_section(all_test_trades)}
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


def build_all_trades_tab(period_items):
    """全銘柄・全期間の取引明細を1テーブルにまとめる。

    各 (sym, strategy) ペアの max_P (最長期間) の test_trades を採用。
    日付降順に並べて1つの大テーブルに出力。
    コピー&貼付けで他ツール (Excel/Notion等) に移行可能。
    """
    # 集約
    agg = {}
    for P, items in period_items.items():
        for it in items:
            key = (it["symbol"], it["strategy"])
            if key not in agg:
                agg[key] = {"name": it["name"], "periods": {}}
            agg[key]["periods"][P] = it.get("test_trades", [])

    # 全取引展開
    all_rows = []
    for (sym, strat), entry in agg.items():
        if not entry["periods"]:
            continue
        max_P = max(entry["periods"].keys())
        for t in entry["periods"][max_P]:
            all_rows.append({
                "sym": sym,
                "name": entry["name"],
                "strat": strat,
                "entry_dt": t.get("entry_dt"),
                "exit_dt": t.get("exit_dt"),
                "entry_p": t.get("entry_p", 0),
                "stop_p": t.get("stop_p", 0),
                "target_p": t.get("target_p", 0),
                "exit_p": t.get("exit_p", 0),
                "pnl": t.get("pnl", 0),
                "pct": t.get("pct", 0),
                "reason": t.get("reason", "?"),
            })

    # 日付降順
    all_rows.sort(key=lambda r: str(r.get("entry_dt", "")), reverse=True)

    if not all_rows:
        return '<p style="color:#64748b;padding:24px">取引なし</p>'

    # 集計
    total_pnl = sum(r["pnl"] for r in all_rows)
    wins = sum(1 for r in all_rows if r["pnl"] > 0)
    win_rate = wins / len(all_rows) * 100
    gross_profit = sum(r["pnl"] for r in all_rows if r["pnl"] > 0)
    gross_loss = abs(sum(r["pnl"] for r in all_rows if r["pnl"] <= 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # サマリ
    sum_box = f"""
<div class="box">
  <div class="it"><div class="lb">総取引数</div><div class="vl">{len(all_rows):,}</div></div>
  <div class="it"><div class="lb">勝率</div><div class="vl">{win_rate:.0f}%</div></div>
  <div class="it"><div class="lb">PF</div>
    <div class="vl">{_pf(pf)}</div></div>
  <div class="it"><div class="lb">利益</div>
    <div class="vl profit">+{gross_profit:,.0f}</div></div>
  <div class="it"><div class="lb">損</div>
    <div class="vl loss">-{gross_loss:,.0f}</div></div>
  <div class="it"><div class="lb">損益</div>
    <div class="vl {'profit' if total_pnl >= 0 else 'loss'}">{total_pnl:+,.0f}円</div></div>
</div>
"""

    # 全取引テーブル行
    rows_html = ""
    for r in all_rows:
        ed = _fmt_dt(r.get("entry_dt"))
        xd = _fmt_dt(r.get("exit_dt"))
        try:
            hold = f"{int((r['exit_dt'] - r['entry_dt']).total_seconds() // 60)}分"
        except Exception:
            hold = "-"
        pnl = r["pnl"]
        pct = r["pct"]
        pc = "profit" if pnl >= 0 else "loss"
        rows_html += f"""
<tr>
  <td class="sym">{r['name']}<br><small class="code">{r['sym']}</small></td>
  <td>{r['strat']}</td>
  <td>{ed}</td>
  <td>{xd}</td>
  <td>{hold}</td>
  <td>{r['entry_p']:,.0f}</td>
  <td class="loss">{r['stop_p']:,.0f}</td>
  <td class="profit">{r['target_p']:,.0f}</td>
  <td>{r['exit_p']:,.0f}</td>
  <td class="{pc}">{pnl:+,.0f}</td>
  <td class="{pc}">{pct:+.2f}%</td>
  <td>{r['reason']}</td>
</tr>"""

    return f"""
{sum_box}
<p style="color:#94a3b8;font-size:0.85rem;margin:-6px 0 8px">
  📋 <strong>全銘柄・全期間 取引明細</strong> ({len(all_rows):,}件 / 日付降順)<br>
  💡 テーブル全選択 ({"Ctrl+A".replace("Ctrl","Ctrl/⌘")}) → コピーで Excel/Notion 等に貼り付け可能
</p>
<table id="all-trades-table" style="font-size:0.72rem">
  <thead><tr>
    <th>銘柄</th><th>戦略</th>
    <th>Entry</th><th>Exit</th><th>保有</th>
    <th>買値</th><th>損切</th><th>目標</th><th>決済</th>
    <th>損益</th><th>%</th><th>理由</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
"""


def _check_data_freshness(min_age_days=4, auto_update=True):
    """pkl データが最新か確認。古ければ yfinance_update.py を自動実行。

    実行時間の節約のためサンプル30銘柄で判定。
    """
    import subprocess
    import sys as _sys
    try:
        from daytrade_data import DATA_DIR, yf_to_jquants
    except Exception:
        print("[warn] daytrade_data からのインポート失敗、鮮度チェックスキップ")
        return
    # サンプル銘柄 (prime先頭30銘柄)
    try:
        from symbols_listed_all import SYMBOLS
        sample = [s for s, _ in SYMBOLS[:30]]
    except Exception:
        print("[warn] symbols_listed_all なし、鮮度チェックスキップ")
        return

    today = datetime.now(JST).date()
    latest_dates = []
    for sym in sample:
        code5 = yf_to_jquants(sym)
        pkl = DATA_DIR / f"{code5}.pkl"
        if not pkl.exists():
            continue
        try:
            import pandas as pd
            df = pickle.loads(pkl.read_bytes())
            if "DateTime" in df.columns:
                last_dt = pd.to_datetime(df["DateTime"].iloc[-1])
            elif "Date" in df.columns:
                last_dt = pd.to_datetime(df["Date"].iloc[-1])
            else:
                continue
            latest_dates.append(last_dt.date())
        except Exception:
            continue

    if not latest_dates:
        print("[データ鮮度] 判定不能、続行")
        return

    # 中央値で判定 (一部の壊れたpklに引っ張られない)
    latest_dates.sort()
    median_latest = latest_dates[len(latest_dates) // 2]
    age = (today - median_latest).days
    # 週末オフセット (土日は更新無し)
    weekday = today.weekday()
    if weekday == 0:    # 月曜なら金曜=3日前まで許容
        adj_age = max(0, age - 2)
    elif weekday == 6:  # 日曜=金曜=2日前まで許容
        adj_age = max(0, age - 1)
    else:
        adj_age = age

    if adj_age > min_age_days:
        print(f"[データ鮮度] 最新 {median_latest} ({age}日前) → 古いので自動更新します")
        if auto_update:
            try:
                result = subprocess.run(
                    [_sys.executable, "yfinance_update.py"],
                    timeout=1800  # 30分タイムアウト
                )
                if result.returncode == 0:
                    print(f"[データ鮮度] 更新完了")
                else:
                    print(f"[warn] yfinance_update.py 終了コード {result.returncode}, 続行")
            except subprocess.TimeoutExpired:
                print(f"[warn] yfinance_update.py タイムアウト、続行")
            except Exception as e:
                print(f"[warn] yfinance_update.py 失敗: {e} (続行)")
        else:
            print(f"[info] 自動更新無効、続行")
    else:
        print(f"[データ鮮度] 最新 {median_latest} ({age}日前) ✓")


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
                             "複数指定可 + ワイルドカード対応。"
                             "--mode を使えば短く書ける")
    parser.add_argument("--mode", default=None,
                        help="STEP1のモード名 (swing_relaxed/swing/standard/lenient/strict) "
                             "から最新CSV群を自動展開。例: --mode swing_relaxed → "
                             "walkforward_daytrade_results/walkforward_*_swing_relaxed_<最新>.csv")
    parser.add_argument("--no-auto", action="store_true",
                        help="CSV自動検出を無効化 (universe=prime のまま全銘柄スキャン)")
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
    parser.add_argument("--short", action="store_true",
                        help="ショート戦略 (DON_S/MACD_S/RSI2_S/A7_S/VOL_S/MOM_S) のみで評価。"
                             "逆指値ロング run_signals_holdout_all.py --short と同じ役割。"
                             "出力ファイル名も holdout_periods_short_<日付>.html に切替")
    parser.add_argument("--long-only", action="store_true",
                        help="ロング戦略 (DON/MACD/RSI2/A7/VOL/MOM) のみで評価")
    parser.add_argument("--both", action="store_true",
                        help="ロング/ショート両方を上タブで切替表示 (1HTML、推奨)")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-fresh-check", action="store_true",
                        help="データ鮮度チェック+自動更新をスキップ")
    parser.add_argument("--no-auto-update", action="store_true",
                        help="鮮度チェックは行うが yfinance 自動更新はしない")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    # データ鮮度チェック + 自動更新
    if not args.no_fresh_check:
        _check_data_freshness(auto_update=not args.no_auto_update)

    # --short と --long-only の整合チェック + 戦略フィルタ
    if args.short and args.long_only:
        print("[error] --short と --long-only は同時指定できません")
        return
    if args.short:
        args.strategy = "short"
        variant_label = "short"
    elif args.long_only:
        args.strategy = "long"
        variant_label = "long"
    elif args.both:
        # 両方分離表示: 銘柄は両戦略でバックテストするので strategy=all
        args.strategy = "all"
        variant_label = "both"
    else:
        variant_label = ""

    today = datetime.now(JST).date()
    out_suffix = f"_{variant_label}" if variant_label else ""
    out = Path(f"holdout_periods{out_suffix}_{today}.html")
    if out.exists() and not args.force:
        print(f"[CACHE] 当日生成済み: {out.resolve()}")
        if not args.no_browser:
            open_html(out.resolve())
        return

    # --mode/--from-csv 未指定 & universe デフォルトのとき → 最新CSVを自動検出
    if not args.from_csv and args.universe == "prime" and not args.mode and not args.no_auto:
        import glob as _g
        all_csvs = _g.glob("walkforward_daytrade_results/walkforward_*_*.csv")
        if all_csvs:
            # 最新日付グループ + その中で最頻出mode
            by_date = {}
            for c in all_csvs:
                date = Path(c).stem.split("_")[-1]
                by_date.setdefault(date, []).append(c)
            latest_date = max(by_date.keys())
            # 同日の中で mode別に分類 (戦略名は可変なので mode はファイル名から推定)
            mode_files = {}
            for c in by_date[latest_date]:
                # walkforward_<strat>_<mode>_<date>.csv
                # ※ mode が "swing_relaxed" だと _ 区切りで複数parts
                stem = Path(c).stem
                # 末尾 _<date> を除去、先頭 walkforward_<strat>_ を除去
                m = stem[len("walkforward_"):-len(latest_date)-1]
                # m = "<strat>_<mode>" の形式 → strat を先頭部分とみなす
                # 戦略一覧と照合して mode を切り出す
                detected_mode = None
                for known in ["standard", "lenient", "strict", "swing", "swing_relaxed"]:
                    if m.endswith("_" + known):
                        detected_mode = known
                        break
                detected_mode = detected_mode or "unknown"
                mode_files.setdefault(detected_mode, []).append(c)
            # 最多mode (swing_relaxed > swing > standard などの優先順)
            priority = ["swing_relaxed", "swing", "standard", "strict", "lenient"]
            chosen = None
            for p in priority:
                if p in mode_files:
                    chosen = p
                    break
            if not chosen:
                chosen = max(mode_files, key=lambda k: len(mode_files[k]))
            args.mode = chosen
            args.from_csv = mode_files[chosen]
            print(f"[auto] 最新CSV検出: mode={chosen} / "
                  f"{len(args.from_csv)}件 / 日付={latest_date}")
    # --mode 自動展開: walkforward_daytrade_results/walkforward_*_<mode>_<最新>.csv
    if args.mode and not args.from_csv:
        import glob as _g
        pattern = f"walkforward_daytrade_results/walkforward_*_{args.mode}_*.csv"
        cands = _g.glob(pattern)
        if not cands:
            print(f"[error] --mode {args.mode} の CSV が見つかりません ({pattern})")
            return
        by_date = {}
        for c in cands:
            date = Path(c).stem.split("_")[-1]
            by_date.setdefault(date, []).append(c)
        latest = max(by_date.keys())
        args.from_csv = by_date[latest]
        print(f"[auto] --mode {args.mode}: {len(args.from_csv)}件 ({latest})")
    # --from-csv あれば universe を csv に自動切替
    if args.from_csv and args.universe != "csv":
        args.universe = "csv"

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
        # --short/--long-only による戦略フィルタ
        if args.strategy in ("short", "long"):
            allowed = (set(STRATEGIES_SHORT.keys()) if args.strategy == "short"
                       else set(STRATEGIES.keys()))
            before_syms = len(csv_strategy_map)
            before_pairs = sum(len(v) for v in csv_strategy_map.values())
            filtered = {}
            filtered_count = {}
            for s, strats in csv_strategy_map.items():
                ks = [x for x in strats if x in allowed]
                if ks:
                    filtered[s] = ks
                    for k in ks:
                        filtered_count[k] = filtered_count.get(k, 0) + 1
            csv_strategy_map = filtered
            after_syms = len(csv_strategy_map)
            after_pairs = sum(len(v) for v in csv_strategy_map.values())
            print(f"[フィルタ] --{args.strategy}: 銘柄 {before_syms}→{after_syms}, "
                  f"ペア {before_pairs}→{after_pairs}")
            per_strat_count = filtered_count
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
    variant_disp = ({"short": "ショート", "long": "ロング",
                     "both": "ロング/ショート分離"}
                    .get(variant_label, "ロング+ショート"))
    print(f"  期間別ホールドアウト・レポート [{variant_disp}] (逆指値ロング方式)")
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
        # ENTRY_START と 改善ENV を cache key に含める (env変えると結果が変わるため)
        _es_label = _os.environ.get("DAYTRADE_ENTRY_START", "0930").replace(":", "")
        _imp_parts = []
        if _os.environ.get("DAYTRADE_STOP_CONFIRM", "0") != "0":
            _imp_parts.append(f"sc{_os.environ['DAYTRADE_STOP_CONFIRM']}")
        if _os.environ.get("DAYTRADE_MAX_STOPS", "0") != "0":
            _imp_parts.append(f"ms{_os.environ['DAYTRADE_MAX_STOPS']}")
        if _os.environ.get("DAYTRADE_TRAIL_BE", "0") != "0":
            _imp_parts.append("tb")
        if _os.environ.get("DAYTRADE_STRAT_TIMES", ""):
            _imp_parts.append("st")
        if _os.environ.get("DAYTRADE_MIN_VOL_RATIO", "0") not in ("0", "0.0"):
            _imp_parts.append(f"vr{_os.environ['DAYTRADE_MIN_VOL_RATIO']}")
        if _os.environ.get("DAYTRADE_MIN_ATR_PCT", "0") not in ("0", "0.0"):
            _imp_parts.append(f"a{_os.environ['DAYTRADE_MIN_ATR_PCT']}")
        if _os.environ.get("DAYTRADE_MAX_ATR_PCT", "0") not in ("0", "0.0"):
            _imp_parts.append(f"A{_os.environ['DAYTRADE_MAX_ATR_PCT']}")
        if _os.environ.get("DAYTRADE_PAUSE_LOSSES", "0") != "0":
            _imp_parts.append(f"pl{_os.environ['DAYTRADE_PAUSE_LOSSES']}")
        _imp_label = "_".join(_imp_parts) or "base"
        # 永続キャッシュ: 日付なし。各エントリーが pkl_mtime/size で整合性確認
        # (旧キャッシュ trades_..._{date}.pkl は engine_version で実質無効化済)
        cache_file = CACHE_DIR / f"trades_{args.universe}_{_es_label}_{_imp_label}.pkl"
        # エンジンロジック変更時はキャッシュ無効化したいため、wrapperでversion確認
        engine_version = "v2_persistent_2026-06-15"
        if cache_file.exists():
            try:
                raw = pickle.loads(cache_file.read_bytes())
                # 新形式 (engine_version 付き) のみ受け入れる
                if (isinstance(raw, dict) and
                        raw.get("engine_version") == engine_version and
                        isinstance(raw.get("entries"), dict)):
                    cache = raw["entries"]
                    print(f"  永続キャッシュ復元: {len(cache)}件 (engine={engine_version})")
                else:
                    print(f"  [warn] キャッシュ形式不一致 → 破棄して新規作成")
                    cache = {}
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
            # 永続キャッシュ形式で保存 (engine_version + entries)
            payload = {"engine_version": engine_version, "entries": cache}
            cache_file.write_bytes(pickle.dumps(payload))
            hits = sum(1 for v in cache.values()
                       if isinstance(v, dict) and v.get("pkl_mtime", 0) > 0)
            print(f"  永続キャッシュ保存: {hits}件 (次回は pkl 更新銘柄のみ再計算)")
        except Exception:
            pass

    # 期間別評価 (逆指値ロング walkforward_holdout.py と一致)
    print(f"\n[Step 3] 6期間ホールドアウト評価 (逆指値ロング方式)", flush=True)
    long_strats = set(STRATEGIES.keys())
    short_strats = set(STRATEGIES_SHORT.keys())

    # 同日同銘柄ロック (DAYTRADE_SAME_DAY_LOCK=1 時)
    # ロング側とショート側でそれぞれ独立に適用 (両建ては妨げない)
    if _os.environ.get("DAYTRADE_SAME_DAY_LOCK", "0") == "1":
        before = sum(len(v) for v in results.values())
        long_part = {k: v for k, v in results.items() if k[1] in long_strats}
        short_part = {k: v for k, v in results.items() if k[1] in short_strats}
        long_part = apply_same_day_lock(long_part)
        short_part = apply_same_day_lock(short_part)
        results = {**long_part, **short_part}
        after = sum(len(v) for v in results.values())
        print(f"  [SAME_DAY_LOCK] 取引数 {before:,} → {after:,} "
              f"({before-after:,}件 重複排除)")

    if args.both:
        # --both: ロング/ショートを分離して2セット作成
        long_results = {k: v for k, v in results.items() if k[1] in long_strats}
        short_results = {k: v for k, v in results.items() if k[1] in short_strats}
        print(f"  [両方モード] ロング: {len(long_results)}ペア, "
              f"ショート: {len(short_results)}ペア")
        period_items_long = {}
        period_items_short = {}
        for P in PERIODS:
            items_l = evaluate_period(long_results, P, args.train_days, today,
                                       args.budget, args.top)
            items_s = evaluate_period(short_results, P, args.train_days, today,
                                       args.budget, args.top)
            period_items_long[P] = items_l
            period_items_short[P] = items_s
            print(f"  直近{P:>3}日 TEST: ロング {len(items_l)} / "
                  f"ショート {len(items_s)} 合格")
        period_items = period_items_long  # 後方互換 (使われなければ無視)
    else:
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
.side-nav { display:flex; gap:8px; margin:12px 0 16px;
            border-bottom:2px solid #1e293b; padding-bottom:0; }
.side-btn { padding:10px 28px; background:#1e293b; border:2px solid #334155;
            border-bottom:none; border-radius:8px 8px 0 0;
            color:#94a3b8; cursor:pointer; font-size:1rem;
            font-weight:600; margin-bottom:-2px; }
.side-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.side-btn.active { background:#0f172a; color:#60a5fa;
                   border-color:#60a5fa; border-bottom:2px solid #0f172a; }
.side-container { padding-top:8px; }
"""
    js = """
function switchSide(side){
  document.querySelectorAll('.side-container').forEach(d=>d.style.display='none');
  document.querySelectorAll('.side-btn').forEach(b=>b.classList.remove('active'));
  var el=document.getElementById('side-'+side);
  if(el) el.style.display='block';
  (event.target.closest('.side-btn')||event.target).classList.add('active');
}
function switchTab(tab){
  // 同一side-container内のタブだけ切替 (--both モード対応)
  var btn = event.target.closest('.tab-btn');
  var container = btn ? btn.closest('.side-container') : null;
  var scope = container || document;
  scope.querySelectorAll('.tab-pane').forEach(p=>{
    p.classList.remove('active');
    p.style.display='none';
  });
  scope.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  var pane = document.getElementById('t-'+tab);
  if(pane){
    pane.classList.add('active');
    pane.style.display='block';
    // 銘柄詳細タブの中の最初の sym-pane を確実に表示
    var firstSymPane = pane.querySelector('.sym-pane');
    if(firstSymPane){
      // すべての sym-pane を一旦非表示
      pane.querySelectorAll('.sym-pane').forEach(sp=>sp.style.display='none');
      firstSymPane.style.display='block';
      // 最初の sym-btn を active に
      var firstBtn = pane.querySelector('.sym-btn');
      pane.querySelectorAll('.sym-btn').forEach(b=>b.classList.remove('active'));
      if(firstBtn) firstBtn.classList.add('active');
    }
  }
  if(btn) btn.classList.add('active');
}
function switchSym(id){
  document.querySelectorAll('.sym-pane').forEach(p=>p.style.display='none');
  document.querySelectorAll('.sym-btn').forEach(b=>b.classList.remove('active'));
  var pane=document.getElementById(id);
  if(pane) pane.style.display='block';
  (event.target.closest('.sym-btn')||event.target).classList.add('active');
}
function jumpToSym(sid){
  // sid は "L_5715_DON" や "S_5715_MOM_S" のような prefix付き
  // 銘柄詳細タブの id を判定 (L_detail or S_detail)
  var pref = '';
  if(sid.indexOf('L_')===0) pref='L_';
  else if(sid.indexOf('S_')===0) pref='S_';
  var detailId = pref+'detail';
  var detailPane = document.getElementById('t-'+detailId);
  // 該当 side-container の他タブを全部閉じる
  var container = detailPane ? detailPane.closest('.side-container') : document;
  container.querySelectorAll('.tab-pane').forEach(p=>{
    p.classList.remove('active');
    p.style.display='none';
  });
  container.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  if(detailPane){
    detailPane.classList.add('active');
    detailPane.style.display='block';
    // 該当銘柄ペインだけ表示
    detailPane.querySelectorAll('.sym-pane').forEach(p=>p.style.display='none');
    detailPane.querySelectorAll('.sym-btn').forEach(b=>b.classList.remove('active'));
    var pane=document.getElementById('sym'+sid);
    if(pane){
      pane.style.display='block';
      pane.scrollIntoView({behavior:'smooth',block:'start'});
    }
    detailPane.querySelectorAll('.sym-btn').forEach(b=>{
      if(b.dataset.sym===sid) b.classList.add('active');
    });
    // 銘柄詳細タブの button を active に
    container.querySelectorAll('.tab-btn').forEach(b=>{
      if(b.textContent.indexOf('銘柄詳細')!==-1) b.classList.add('active');
    });
  }
}
"""

    def _build_side(items_by_period, side_pref):
        """1side のタブ+ペインHTMLを返す (ID prefix で衝突回避)。"""
        btns = ""
        panes = ""
        for i, P in enumerate(PERIODS):
            ab = "active" if i == 0 else ""
            n_qual = len(items_by_period[P])
            pid = f"{side_pref}p{P}"
            btns += (f'<button class="tab-btn {ab}" '
                     f'onclick="switchTab(\'{pid}\')">直近{P}日<br>'
                     f'<small style="color:#fbbf24">{n_qual}件</small></button>')
            panes += (f'<div id="t-{pid}" class="tab-pane {ab}">'
                      f'{build_period_tab(P, args.train_days, items_by_period[P], id_prefix=side_pref)}'
                      f'</div>')
        # 銘柄詳細
        uniq = {(it["symbol"], it["strategy"])
                for P in PERIODS for it in items_by_period[P]}
        did = f"{side_pref}detail"
        btns += (f'<button class="tab-btn" onclick="switchTab(\'{did}\')">'
                 f'📊 銘柄詳細<br><small style="color:#fbbf24">{len(uniq)}件</small></button>')
        panes += (f'<div id="t-{did}" class="tab-pane">'
                  f'{build_detail_tab(items_by_period, id_prefix=side_pref)}</div>')
        # 全取引一覧 (コピー&貼付け用)
        aid = f"{side_pref}all"
        btns += (f'<button class="tab-btn" onclick="switchTab(\'{aid}\')">'
                 f'📋 全取引一覧<br><small style="color:#fbbf24">貼付用</small></button>')
        panes += (f'<div id="t-{aid}" class="tab-pane">'
                  f'{build_all_trades_tab(items_by_period)}</div>')
        return f'<div class="tab-nav">{btns}</div>{panes}'

    if args.both:
        long_html = _build_side(period_items_long, "L_")
        short_html = _build_side(period_items_short, "S_")
        side_section = f"""
<div class="side-nav">
  <button class="side-btn active" onclick="switchSide('long')">📈 ロング</button>
  <button class="side-btn" onclick="switchSide('short')">📉 ショート</button>
</div>
<div id="side-long" class="side-container" style="display:block">{long_html}</div>
<div id="side-short" class="side-container" style="display:none">{short_html}</div>
"""
        tab_btns = ""  # 個別タブナビは side_section 内
        tab_panes = ""
    else:
        side_section = ""
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
        unique_syms = {(it["symbol"], it["strategy"])
                       for P in PERIODS for it in period_items[P]}
        tab_btns += (f'<button class="tab-btn" onclick="switchTab(\'detail\')">'
                     f'📊 銘柄詳細<br><small style="color:#fbbf24">{len(unique_syms)}件</small></button>')
        tab_panes += (f'<div id="t-detail" class="tab-pane">'
                      f'{build_detail_tab(period_items)}</div>')
        # 全取引一覧 (コピー&貼付け用)
        tab_btns += (f'<button class="tab-btn" onclick="switchTab(\'all\')">'
                     f'📋 全取引一覧<br><small style="color:#fbbf24">貼付用</small></button>')
        tab_panes += (f'<div id="t-all" class="tab-pane">'
                      f'{build_all_trades_tab(period_items)}</div>')

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
  ▸ <strong>各タブで構成銘柄が変わります</strong>: TRAIN期間が違うので選定銘柄も期間ごとに変動<br>
  ▸ TRAIN: {train_desc} で取引≥{PASS_TRAIN_TRADES}/PF≥{PASS_TRAIN_PF}/損益>0 を満たす銘柄を選定<br>
  ▸ <strong>★合格</strong>: 選定銘柄が TEST期間でも PF≥{TEST_PASS_PF} & 損益≥0 で勝てた (真の優位性あり)<br>
  ▸ Composite Score = TEST損益 × (1 + max(Sharpe,0)) 順に Top{args.top} 表示<br>
  ▸ <strong>全6タブで★合格 = 短期も長期もロバスト</strong> (最有力候補)
</div>
"""

    # body 構成
    if args.both:
        body_main = side_section
    else:
        body_main = f'<div class="tab-nav">{tab_btns}</div>{tab_panes}'

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>期間別ホールドアウト ― {today}</title>
<style>{css}</style></head><body>
<h1>📊 期間別ホールドアウト・レポート [{variant_disp}]</h1>
<p class="subtitle">生成: {today} / universe: {args.universe} ({len(fetched)}銘柄) /
   戦略: {len(strategies)} ({variant_disp}) / TRAIN期間: {args.train_days}日</p>
{legend}
{body_main}
<script>{js}</script>
</body></html>"""

    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        open_html(out.resolve())


if __name__ == "__main__":
    main()
