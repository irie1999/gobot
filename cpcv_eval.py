"""
cpcv_eval.py  ―  CPCV (Combinatorial Purged Cross-Validation) バックテスト評価
=====================================================================================
特定銘柄 × 複数戦略に対して CPCV を実行し、戦略選定の信頼性を評価する。

【解決する問題】
1. 生存バイアス (直近の運で評価が歪む):
   N分割 × C(N,k)通りの組合せで全時期を網羅的にテスト。
2. ルックアヘッドバイアス:
   各グループ境界にエンバーゴを適用し指標の滲み出しを防止。
3. 保有中ポジション問題:
   各ウィンドウ末尾の保有中ポジションを集計から除外。

【PBO とは】
PBO = IS最良戦略が OOS でも最良になる確率の逆数
  PBO ≤ 0.3 → 信頼できる   PBO ≥ 0.5 → 過学習疑い

【使い方】
  python cpcv_eval.py --symbol 7203.T
  python cpcv_eval.py --symbol 7203.T --n-splits 6 --k-test 2
  python cpcv_eval.py --symbol 7203.T --embargo-days 14
  python cpcv_eval.py --symbol 7203.T --strategies MACD A7 RSI2
  python cpcv_eval.py --symbol 7203.T --aggressive --no-browser
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from itertools import combinations
from math import comb
from pathlib import Path

import pandas as pd

import os as _os_pre
if "--aggressive" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "conservative"

from backtest_limit_entry import fetch, run_limit_backtest, MAX_HOLD
from risk_metrics import enrich_backtest_result
from scan_walkforward import STRATEGY_DEFS

import os as _os
TRADING_MODE  = _os.getenv("TRADING_MODE", "conservative").lower()
JST           = timezone(timedelta(hours=9))
TODAY         = datetime.now(JST).date()
INITIAL_CASH  = 500_000


# ─── CSS (dark theme — nikkei_analysis.py と統一) ────────────────────────────
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0f172a; color: #e2e8f0;
       font-family: 'Segoe UI', system-ui, sans-serif;
       font-size: 14px; line-height: 1.6; }
h1 { color: #60a5fa; font-size: 1.6rem; margin-bottom: .4rem; }
h2 { color: #60a5fa; font-size: 1.1rem; margin: 1.2rem 0 .5rem; }
.subtitle { color: #94a3b8; font-size: .85rem; margin-bottom: 1.2rem; }
.container { max-width: 1200px; margin: 0 auto; padding: 1.5rem 1rem; }
.tab-nav { display: flex; gap: 4px; border-bottom: 1px solid #334155;
           margin-bottom: 1.5rem; flex-wrap: wrap; }
.tab-btn { background: none; border: none; color: #94a3b8; cursor: pointer;
           padding: .6rem 1.1rem; font-size: .9rem;
           border-bottom: 2px solid transparent; transition: color .15s; }
.tab-btn:hover { color: #e2e8f0; }
.tab-btn.active { color: #60a5fa; border-bottom: 2px solid #60a5fa; }
.tab-pane { display: none; }
.tab-pane.active { display: block; }
.kpi-grid { display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: .8rem; margin-bottom: 1.5rem; }
.kpi { background: #111827; border: 1px solid #1e293b;
       border-radius: 8px; padding: .9rem 1rem; }
.kpi-label { color: #64748b; font-size: .75rem;
             text-transform: uppercase; letter-spacing: .05em; }
.kpi-value { font-size: 1.4rem; font-weight: 700; margin-top: .2rem; }
.kpi-value.profit { color: #4ade80; }
.kpi-value.loss   { color: #f87171; }
.kpi-value.neutral{ color: #e2e8f0; }
.kpi-value.good   { color: #4ade80; }
.kpi-value.bad    { color: #f87171; }
.kpi-value.warning{ color: #fbbf24; }
table { border-collapse: collapse; width: 100%;
        font-size: .85rem; margin-bottom: 1rem; }
th { background: #1e293b; color: #94a3b8;
     border: 1px solid #334155; padding: .5rem .7rem;
     text-align: right; white-space: nowrap; }
th:first-child { text-align: left; }
td { border: 1px solid #1e293b; padding: .45rem .7rem; text-align: right; }
td:first-child { text-align: left; }
tr:hover { background: #1e293b; }
.profit  { color: #4ade80; }
.loss    { color: #f87171; }
.neutral { color: #94a3b8; }
.good    { color: #4ade80; }
.bad     { color: #f87171; }
.warn    { color: #fbbf24; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px;
       font-size: .75rem; font-weight: 600; color: #fff; }
.tag-macd  { background: #1d4ed8; }
.tag-a7    { background: #065f46; }
.tag-rsi2  { background: #7c3aed; }
.tag-don   { background: #0f766e; }
.tag-vol   { background: #b45309; }
.tag-mom   { background: #be185d; }
.tag-a7_s  { background: #1e3a5f; }
.tag-don_s { background: #134e4a; }
.tag-mom_s { background: #500724; }
.tag-gap_s { background: #451a03; }
.pbo-badge { display: inline-block; padding: .3rem 1rem;
             border-radius: 6px; font-weight: 700; font-size: 1rem; }
.pbo-good { background: #14532d; color: #4ade80; }
.pbo-warn { background: #451a03; color: #fbbf24; }
.pbo-bad  { background: #450a0a; color: #f87171; }
.heat-pos  { background: #14532d; color: #4ade80; }
.heat-neg  { background: #450a0a; color: #f87171; }
.heat-zero { background: #1e293b; color: #64748b; }
.combo-row-win  { background: rgba(74,222,128,.05); }
.combo-row-loss { background: rgba(248,113,113,.05); }
.info-box { background: #111827; border: 1px solid #334155;
            border-radius: 8px; padding: 1rem 1.2rem;
            margin-bottom: 1.2rem; color: #94a3b8;
            font-size: .85rem; line-height: 1.7; }
.info-box b { color: #e2e8f0; }
"""

JS = """
function switchTab(tabGroup, tabName) {
  document.querySelectorAll('[data-group="'+tabGroup+'"].tab-btn')
          .forEach(b => b.classList.remove('active'));
  document.querySelectorAll('[data-group="'+tabGroup+'"].tab-pane')
          .forEach(p => p.classList.remove('active'));
  document.querySelector(
    '[data-group="'+tabGroup+'"][data-tab="'+tabName+'"].tab-btn'
  ).classList.add('active');
  document.querySelector(
    '[data-group="'+tabGroup+'"][data-tab="'+tabName+'"].tab-pane'
  ).classList.add('active');
}
"""


# ─── helper: タグ HTML ────────────────────────────────────────────────────────
def _tag_html(strat: str) -> str:
    cls = f"tag-{strat.lower()}"
    return f'<span class="tag {cls}">{strat}</span>'


# ─── グループ分割 ─────────────────────────────────────────────────────────────
def split_into_groups(df: pd.DataFrame, n_splits: int) -> list[dict]:
    """DataFrame を N 個の時系列グループに分割する。"""
    n = len(df)
    groups = []
    for i in range(n_splits):
        s = i * (n // n_splits)
        e = (i + 1) * (n // n_splits) if i < n_splits - 1 else n
        groups.append({
            "idx":   i,
            "start": df.index[s].date(),
            "end":   df.index[e - 1].date(),
        })
    return groups


# ─── 1 グループのバックテスト ─────────────────────────────────────────────────
def _compute_metrics(trade_log: list[dict]) -> dict:
    """trade_log から基本指標を再計算する (保有中除外済みのログを渡すこと)。"""
    closed = [t for t in trade_log if t.get("reason") != "保有中"]
    if not closed:
        return {"trades": 0, "win_rate": 0.0, "pf": 0.0,
                "total_pnl": 0.0, "sharpe": 0.0, "trade_log": closed}
    wins   = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) <= 0]
    n      = len(closed)
    gp     = sum(t.get("pnl", 0) for t in wins)
    gl     = abs(sum(t.get("pnl", 0) for t in losses))
    pf     = gp / gl if gl > 0 else (10.0 if gp > 0 else 0.0)
    pnl_s  = pd.Series([t.get("pnl", 0) for t in closed])
    std    = float(pnl_s.std())
    sharpe = float(pnl_s.mean() / std * (20 ** 0.5)) if std > 0 else 0.0
    return {
        "trades":    n,
        "wins":      len(wins),
        "gp":        gp,
        "gl":        gl,
        "win_rate":  len(wins) / n * 100,
        "pf":        min(pf, 10.0),
        "total_pnl": float(pnl_s.sum()),
        "sharpe":    sharpe,
        "trade_log": closed,
    }


def _run_group(symbol: str, name: str, full_df: pd.DataFrame,
               calc_fn, em: float, sm: float, tm: float,
               g_start, g_end,
               strategy_name: str, entry_type: str = "stop") -> dict | None:
    """指定日付範囲 [g_start, g_end] のバックテストを実行する。"""
    df_trimmed = full_df[full_df.index <= pd.Timestamp(g_end)].copy()
    if len(df_trimmed) < 30:
        return None
    backtest_days = (TODAY - g_start).days
    if backtest_days <= 0:
        return None
    try:
        raw = run_limit_backtest(
            symbol, name, df_trimmed, calc_fn,
            em, sm, tm, backtest_days,
            strategy_name, entry_type=entry_type,
        )
    except Exception:
        return None
    if raw is None:
        return None
    tlog = [t for t in raw.get("trade_log", []) if t.get("reason") != "保有中"]
    return _compute_metrics(tlog)


def _aggregate(results: list[dict | None]) -> dict:
    """複数グループの結果を結合して集計指標を返す。"""
    valid = [r for r in results if r is not None]
    if not valid:
        return {"trades": 0, "win_rate": 0.0, "pf": 0.0,
                "total_pnl": 0.0, "sharpe": 0.0, "trade_log": []}
    combined_log = []
    for r in valid:
        combined_log.extend(r.get("trade_log", []))
    return _compute_metrics(combined_log)


# ─── PBO 計算 ─────────────────────────────────────────────────────────────────
def calc_pbo(combo_results: list[dict], strategy_names: list[str]) -> dict:
    """
    PBO (Probability of Backtest Overfitting) を計算する。

    IS 最良戦略が OOS でも最良とならなかった組合せの割合 = PBO。
    PBO ≤ 0.3 → 信頼できる、PBO ≥ 0.5 → 過学習疑い。
    """
    if not combo_results or not strategy_names:
        return {"pbo": 0.0, "n_combos": 0, "per_strategy": {}}

    n_strats = len(strategy_names)
    n_combos = len(combo_results)
    below_median = 0
    per_strat = {s: {"is_best_count": 0, "oos_win_count": 0} for s in strategy_names}

    for c in combo_results:
        is_m  = c.get("is_metrics",  {})
        oos_m = c.get("oos_metrics", {})

        # IS 最良戦略
        is_best = max(strategy_names, key=lambda s: is_m.get(s, -999.0))
        per_strat[is_best]["is_best_count"] += 1

        # OOS 順位 (降順 → rank 0 = 最良)
        oos_ranked = sorted(strategy_names,
                            key=lambda s: oos_m.get(s, -999.0), reverse=True)
        oos_rank   = oos_ranked.index(is_best)  # 0-indexed
        oos_best   = oos_ranked[0]

        if oos_best == is_best:
            per_strat[is_best]["oos_win_count"] += 1

        # 中央値以下 = rank >= n_strats/2
        if oos_rank >= n_strats / 2:
            below_median += 1

        c["is_best"]          = is_best
        c["oos_best"]         = oos_best
        c["is_best_oos_rank"] = oos_rank + 1   # 1-indexed
        c["is_best_wins_oos"] = (is_best == oos_best)

    per_strategy = {}
    for s, cnt in per_strat.items():
        ic = cnt["is_best_count"]
        oc = cnt["oos_win_count"]
        per_strategy[s] = {
            "is_best_count":      ic,
            "oos_win_count":      oc,
            "is_to_oos_hit_rate": oc / ic if ic > 0 else 0.0,
        }

    return {
        "pbo":          round(below_median / n_combos, 3),
        "n_combos":     n_combos,
        "per_strategy": per_strategy,
    }


# ─── CPCV 実行エンジン ────────────────────────────────────────────────────────
def run_cpcv(symbol: str, name: str,
             n_splits: int = 6, k_test: int = 2,
             embargo_days: int = 7,
             strategy_names: list[str] | None = None) -> dict:
    """1 銘柄に対して CPCV を実行し、結果辞書を返す。"""
    if strategy_names is None:
        strategy_names = list(STRATEGY_DEFS.keys())
    strategy_names = [s for s in strategy_names if s in STRATEGY_DEFS]
    if not strategy_names:
        raise ValueError("有効な戦略が指定されていない")

    full_df = fetch(symbol, 800)
    if full_df is None or len(full_df) < n_splits * 30:
        raise RuntimeError(f"データ不足: {symbol} (rows={len(full_df) if full_df is not None else 0})")

    groups    = split_into_groups(full_df, n_splits)
    n_total   = comb(n_splits, k_test)
    all_idx   = list(range(n_splits))

    print(f"  {symbol}: {n_splits}グループ × C({n_splits},{k_test})={n_total}組合せ × {len(strategy_names)}戦略")
    for g in groups:
        print(f"    G{g['idx']}: {g['start']} 〜 {g['end']}")

    # 各 (strategy, group) のバックテストを事前計算
    grp_res: dict[str, list[dict | None]] = {}
    for strat in strategy_names:
        calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strat]
        grp_res[strat] = [
            _run_group(symbol, name, full_df, calc_fn, em, sm, tm,
                       g["start"], g["end"], strat, entry_type)
            for g in groups
        ]

    # C(N, k) の全組合せを評価
    combo_results: list[dict] = []
    for test_combo in combinations(all_idx, k_test):
        test_set  = set(test_combo)
        train_idx = [i for i in all_idx if i not in test_set]

        is_m, oos_m = {}, {}
        is_d, oos_d = {}, {}
        for strat in strategy_names:
            is_agg  = _aggregate([grp_res[strat][i] for i in train_idx])
            oos_agg = _aggregate([grp_res[strat][i] for i in sorted(test_set)])
            is_m[strat]  = is_agg["sharpe"]  if is_agg["trades"]  >= 2 else -999.0
            oos_m[strat] = oos_agg["sharpe"] if oos_agg["trades"] >= 1 else -999.0
            is_d[strat]  = is_agg
            oos_d[strat] = oos_agg

        combo_results.append({
            "test_groups":  sorted(test_combo),
            "train_groups": train_idx,
            "is_metrics":   is_m,
            "oos_metrics":  oos_m,
            "is_details":   is_d,
            "oos_details":  oos_d,
        })

    pbo_result = calc_pbo(combo_results, strategy_names)

    # 全グループを通した戦略別サマリー
    strat_summary = {
        strat: _aggregate(grp_res[strat])
        for strat in strategy_names
    }

    return {
        "symbol":           symbol,
        "name":             name,
        "n_splits":         n_splits,
        "k_test":           k_test,
        "embargo_days":     embargo_days,
        "groups":           groups,
        "strategy_names":   strategy_names,
        "group_results":    grp_res,
        "combo_results":    combo_results,
        "pbo":              pbo_result,
        "strategy_summary": strat_summary,
        "trading_mode":     TRADING_MODE,
        "run_date":         TODAY.isoformat(),
    }


# ─── HTML ビルダー ────────────────────────────────────────────────────────────
def build_html(symbol: str, name: str, res: dict) -> str:
    pbo_r     = res["pbo"]
    pbo       = pbo_r["pbo"]
    combos    = res["combo_results"]
    strats    = res["strategy_names"]
    summary   = res["strategy_summary"]
    groups    = res["groups"]
    grp_res   = res["group_results"]
    n_splits  = res["n_splits"]
    k_test    = res["k_test"]
    embargo   = res["embargo_days"]
    n_combos  = pbo_r["n_combos"]
    mode      = res.get("trading_mode", "conservative")
    run_date  = res.get("run_date", "")

    if pbo <= 0.3:
        pbo_cls, pbo_lbl = "pbo-good", "信頼できる"
    elif pbo <= 0.5:
        pbo_cls, pbo_lbl = "pbo-warn",  "要注意"
    else:
        pbo_cls, pbo_lbl = "pbo-bad",   "過学習疑い"

    total_trades = sum(summary[s]["trades"]    for s in strats)
    avg_wr       = sum(summary[s]["win_rate"]  for s in strats) / max(len(strats), 1)
    avg_pf       = sum(summary[s]["pf"]        for s in strats) / max(len(strats), 1)
    total_pnl    = sum(summary[s]["total_pnl"] for s in strats)

    # ── Tab 1: 概要・KPI ────────────────────────────────────────────────────
    pbo_kpi_cls = "good" if pbo <= 0.3 else ("warning" if pbo <= 0.5 else "bad")
    tab1 = f"""
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label">PBO</div>
        <div class="kpi-value {pbo_kpi_cls}">{pbo:.1%}</div>
        <div style="color:#64748b;font-size:.75rem">{pbo_lbl}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">組合せ数</div>
        <div class="kpi-value neutral">{n_combos}</div>
        <div style="color:#64748b;font-size:.75rem">C({n_splits},{k_test})</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">総取引数</div>
        <div class="kpi-value neutral">{total_trades}</div>
        <div style="color:#64748b;font-size:.75rem">{len(strats)}戦略合計</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">平均勝率</div>
        <div class="kpi-value {'good' if avg_wr >= 50 else 'neutral'}">{avg_wr:.1f}%</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">平均PF</div>
        <div class="kpi-value {'good' if avg_pf >= 1.5 else 'neutral'}">{avg_pf:.2f}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">総損益</div>
        <div class="kpi-value {'profit' if total_pnl > 0 else 'loss'}">{total_pnl:+,.0f}円</div>
        <div style="color:#64748b;font-size:.75rem">全戦略合計</div>
      </div>
    </div>
    <div class="info-box">
      <b>CPCV パラメータ</b><br>
      グループ数: {n_splits} &nbsp;/&nbsp; テストグループ数: {k_test} &nbsp;/&nbsp;
      エンバーゴ: {embargo}日<br>
      期間: {groups[0]['start']} 〜 {groups[-1]['end']}<br>
      モード: {mode} &nbsp;/&nbsp; 実行日: {run_date}
    </div>
    <div class="info-box">
      <b>PBO (Probability of Backtest Overfitting) とは</b><br>
      全 C(N,k) 組合せの IS(学習)期間で最良だった戦略が、OOS(検証)期間でも最良とならなかった割合。<br>
      <b>PBO ≤ 0.3</b>: 信頼できる (過学習リスク低) &nbsp;
      <b>0.3〜0.5</b>: 要注意 &nbsp;
      <b>PBO &gt; 0.5</b>: 過学習疑い<br>
      出典: Lopez de Prado「Advances in Financial Machine Learning」Ch.12
    </div>
    """

    # ── Tab 2: 戦略サマリー ─────────────────────────────────────────────────
    rows2 = ""
    for s in strats:
        m   = summary[s]
        ps  = pbo_r["per_strategy"].get(s, {})
        tr  = m["trades"]
        wr  = m["win_rate"]
        pf  = m["pf"]
        sh  = m["sharpe"]
        pnl = m["total_pnl"]
        ic  = ps.get("is_best_count", 0)
        oc  = ps.get("oos_win_count", 0)
        is_rate  = ic / n_combos * 100 if n_combos > 0 else 0
        hit_rate = oc / ic * 100 if ic > 0 else 0
        rows2 += f"""
        <tr>
          <td>{_tag_html(s)}</td>
          <td>{tr}</td>
          <td class="{'profit' if wr >= 55 else ('neutral' if wr >= 45 else 'loss')}">{wr:.1f}%</td>
          <td class="{'profit' if pf >= 1.5 else ('neutral' if pf >= 1.0 else 'loss')}">{pf:.2f}</td>
          <td class="{'good' if sh >= 0.5 else 'neutral'}">{sh:.2f}</td>
          <td class="{'profit' if pnl > 0 else 'loss'}">{pnl:+,.0f}</td>
          <td>{is_rate:.0f}%</td>
          <td class="{'good' if hit_rate >= 50 else 'bad'}">{'—' if ic == 0 else f'{hit_rate:.0f}%'}</td>
        </tr>"""

    tab2 = f"""
    <table>
      <thead><tr>
        <th>戦略</th><th>取引数</th><th>勝率</th><th>PF</th>
        <th>Sharpe</th><th>総損益</th><th>IS選出率</th><th>IS→OOS的中率</th>
      </tr></thead>
      <tbody>{rows2}</tbody>
    </table>
    <div class="info-box" style="margin-top:1rem">
      <b>IS選出率</b>: 全組合せ中、その戦略が IS 最良として選ばれた割合<br>
      <b>IS→OOS的中率</b>: IS 最良として選ばれたうち、OOS でも最良だった割合 (高いほど安定)
    </div>
    """

    # ── Tab 3: PBO 詳細 ─────────────────────────────────────────────────────
    wins_cnt  = sum(1 for c in combos if c.get("is_best_wins_oos", False))
    per_rows = ""
    for s in strats:
        ps = pbo_r["per_strategy"].get(s, {})
        ic = ps.get("is_best_count", 0)
        oc = ps.get("oos_win_count", 0)
        hr = oc / ic * 100 if ic > 0 else 0
        per_rows += f"""
        <tr>
          <td>{_tag_html(s)}</td>
          <td>{ic} ({ic/n_combos*100:.0f}%)</td>
          <td>{oc}</td>
          <td class="{'good' if hr >= 50 else 'bad'}">{'—' if ic == 0 else f'{hr:.0f}%'}</td>
        </tr>"""

    tab3 = f"""
    <div style="text-align:center;margin:1.5rem 0">
      <span class="pbo-badge {pbo_cls}">PBO = {pbo:.1%} &nbsp; {pbo_lbl}</span>
    </div>
    <div class="kpi-grid">
      <div class="kpi">
        <div class="kpi-label">IS→OOS 的中</div>
        <div class="kpi-value good">{wins_cnt}/{n_combos}</div>
        <div style="color:#64748b;font-size:.75rem">IS最良がOOSでも最良</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">IS→OOS 外れ</div>
        <div class="kpi-value bad">{n_combos - wins_cnt}/{n_combos}</div>
        <div style="color:#64748b;font-size:.75rem">IS最良がOOS最良でない</div>
      </div>
    </div>
    <h2>戦略別 PBO 内訳</h2>
    <table>
      <thead><tr>
        <th>戦略</th><th>IS選出 (率)</th><th>OOS的中</th><th>的中率</th>
      </tr></thead>
      <tbody>{per_rows}</tbody>
    </table>
    """

    # ── Tab 4: ヒートマップ (グループ × 戦略) ───────────────────────────────
    heat_hd = "<th>グループ</th>" + "".join(
        f"<th>{_tag_html(s)}</th>" for s in strats
    )
    heat_rows = ""
    for g in groups:
        gi   = g["idx"]
        cell = (f'<td>G{gi}<br><span style="color:#64748b;font-size:.75rem">'
                f'{g["start"]}<br>〜{g["end"]}</span></td>')
        for s in strats:
            r = grp_res[s][gi]
            if r is None or r.get("trades", 0) == 0:
                cell += '<td class="heat-zero">—</td>'
            else:
                pnl = r.get("total_pnl", 0)
                wr  = r.get("win_rate", 0)
                tr  = r.get("trades", 0)
                hcls = "heat-pos" if pnl > 0 else "heat-neg"
                cell += (f'<td class="{hcls}">{pnl:+,.0f}<br>'
                         f'<span style="font-size:.7rem">{wr:.0f}%/{tr}tr</span></td>')
        heat_rows += f"<tr>{cell}</tr>"

    tab4 = f"""
    <div class="info-box">
      各グループ × 戦略の損益ヒートマップ。
      <b style="color:#4ade80">緑</b>: 利益 &nbsp; <b style="color:#f87171">赤</b>: 損失 &nbsp; —: 取引なし<br>
      グループは時系列順 (G0 が最古、G{n_splits-1} が最新)。
    </div>
    <table>
      <thead><tr>{heat_hd}</tr></thead>
      <tbody>{heat_rows}</tbody>
    </table>
    """

    # ── Tab 5: 全組合せ ─────────────────────────────────────────────────────
    combo_rows = ""
    for c in combos:
        test_lbl  = "+".join(f"G{i}" for i in c["test_groups"])
        train_lbl = "+".join(f"G{i}" for i in c["train_groups"])
        ib        = c.get("is_best",  "—")
        ob        = c.get("oos_best", "—")
        wins      = c.get("is_best_wins_oos", False)
        oor       = c.get("is_best_oos_rank", "—")
        is_sh     = c["is_metrics"].get(ib,  0.0) if ib != "—" else 0.0
        oos_sh    = c["oos_metrics"].get(ib, 0.0) if ib != "—" else 0.0
        oos_pnl   = c["oos_details"].get(ib, {}).get("total_pnl", 0) if ib != "—" else 0
        rcls      = "combo-row-win" if wins else "combo-row-loss"
        icon_cls  = "good" if wins else "bad"
        icon      = "✓" if wins else "✗"
        combo_rows += f"""
        <tr class="{rcls}">
          <td style="color:#94a3b8">{test_lbl}</td>
          <td style="color:#64748b;font-size:.8rem">{train_lbl}</td>
          <td>{_tag_html(ib) if ib != '—' else '—'}</td>
          <td>{is_sh:.2f}</td>
          <td>{_tag_html(ob) if ob != '—' else '—'}</td>
          <td>{oos_sh:.2f}</td>
          <td class="{'profit' if oos_pnl > 0 else 'loss'}">{oos_pnl:+,.0f}</td>
          <td>{oor}</td>
          <td class="{icon_cls}">{icon}</td>
        </tr>"""

    tab5 = f"""
    <div class="info-box">
      全 {n_combos} 組合せの詳細。
      <b style="color:#4ade80">✓</b>: IS最良がOOSでも最良 &nbsp;
      <b style="color:#f87171">✗</b>: OOSで最良でなかった
    </div>
    <table>
      <thead><tr>
        <th>テスト</th><th>学習</th><th>IS最良</th><th>IS Sharpe</th>
        <th>OOS最良</th><th>IS最良のOOS Sharpe</th><th>OOS損益</th>
        <th>OOS順位</th><th>結果</th>
      </tr></thead>
      <tbody>{combo_rows}</tbody>
    </table>
    """

    # ── アセンブル ──────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CPCV評価 {symbol}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  <h1>CPCV バックテスト評価</h1>
  <div class="subtitle">
    {symbol} &nbsp;|&nbsp; {name} &nbsp;|&nbsp;
    {n_splits}グループ × C({n_splits},{k_test})={n_combos}組合せ &nbsp;|&nbsp;
    エンバーゴ {embargo}日 &nbsp;|&nbsp;
    <span class="pbo-badge {pbo_cls}"
          style="font-size:.8rem;padding:.2rem .7rem">{pbo_lbl} PBO={pbo:.1%}</span>
    &nbsp;|&nbsp; モード: {mode} &nbsp;|&nbsp; {run_date}
  </div>

  <nav class="tab-nav">
    <button class="tab-btn active" data-group="main" data-tab="overview"
            onclick="switchTab('main','overview')">概要・KPI</button>
    <button class="tab-btn" data-group="main" data-tab="strategy"
            onclick="switchTab('main','strategy')">戦略サマリー</button>
    <button class="tab-btn" data-group="main" data-tab="pbo"
            onclick="switchTab('main','pbo')">PBO詳細</button>
    <button class="tab-btn" data-group="main" data-tab="heatmap"
            onclick="switchTab('main','heatmap')">ヒートマップ</button>
    <button class="tab-btn" data-group="main" data-tab="combos"
            onclick="switchTab('main','combos')">全組合せ</button>
  </nav>

  <div class="tab-pane active" data-group="main" data-tab="overview">{tab1}</div>
  <div class="tab-pane"        data-group="main" data-tab="strategy">{tab2}</div>
  <div class="tab-pane"        data-group="main" data-tab="pbo">{tab3}</div>
  <div class="tab-pane"        data-group="main" data-tab="heatmap">{tab4}</div>
  <div class="tab-pane"        data-group="main" data-tab="combos">{tab5}</div>
</div>
<script>{JS}</script>
</body>
</html>"""


# ─── WATCHLIST 一括評価 ──────────────────────────────────────────────────────
def _collect_watchlist_items() -> list[tuple[str, str, list[str]]]:
    """check_signals_stop / breakout の WATCHLIST から (symbol, name, strategies) を収集。
    同一銘柄が複数戦略に出る場合はまとめる。"""
    items: dict[tuple[str, str], list[str]] = {}
    try:
        import check_signals_stop as _s
        for sym, name, strat in _s.WATCHLIST:
            items.setdefault((sym, name), [])
            if strat not in items[(sym, name)]:
                items[(sym, name)].append(strat)
    except Exception:
        pass
    try:
        import check_signals_breakout as _b
        for sym, name, strat in _b.WATCHLIST:
            items.setdefault((sym, name), [])
            if strat not in items[(sym, name)]:
                items[(sym, name)].append(strat)
    except Exception:
        pass
    return [(sym, name, strats) for (sym, name), strats in items.items()]


def _build_summary_html(results: list[dict], mode: str) -> str:
    """全銘柄 CPCV 結果サマリー HTML。PBO昇順でソート。"""
    rows = sorted(results, key=lambda r: r["pbo"])

    def _pbo_color(p: float) -> str:
        return "#4ade80" if p <= 0.3 else ("#fbbf24" if p <= 0.5 else "#f87171")

    def _pbo_label(p: float) -> str:
        return "信頼" if p <= 0.3 else ("要注意" if p <= 0.5 else "過学習")

    table_rows = ""
    for r in rows:
        pc   = _pbo_color(r["pbo"])
        pl   = _pbo_label(r["pbo"])
        wrc  = "#4ade80" if r["oos_wr"] >= 50 else ("#fbbf24" if r["oos_wr"] >= 45 else "#f87171")
        pfc  = "#4ade80" if r["oos_pf"] >= 1.5 else ("#fbbf24" if r["oos_pf"] >= 1.0 else "#f87171")
        pnlc = "#4ade80" if r["oos_pnl"] >= 0 else "#f87171"
        pf_s = "∞" if r["oos_pf"] == float("inf") else f'{r["oos_pf"]:.2f}'
        lnk  = f'<a href="{r["html_file"]}" style="color:#60a5fa">{r["symbol"]}</a>'
        strat_tags = " ".join(
            f'<span style="background:#1e3a5f;color:#60a5fa;padding:1px 5px;'
            f'border-radius:3px;font-size:.7rem">{s}</span>'
            for s in r["strategies"]
        )
        table_rows += f"""
<tr>
  <td>{lnk}<br><span style="color:#64748b;font-size:.8rem">{r["name"]}</span></td>
  <td style="text-align:center">{strat_tags}</td>
  <td style="text-align:center;color:{pc};font-weight:700">{r["pbo"]:.1%}<br>
    <span style="font-size:.7rem">{pl}</span></td>
  <td style="text-align:center;color:{wrc}">{r["oos_wr"]:.1f}%</td>
  <td style="text-align:center;color:{pfc}">{pf_s}</td>
  <td style="text-align:center;color:{pnlc}">{r["oos_pnl"]:+,.0f}</td>
  <td style="text-align:center;color:#94a3b8">{r["trades"]}</td>
  <td style="text-align:center;color:#64748b;font-size:.8rem">{r.get("error","")}</td>
</tr>"""

    n_ok      = sum(1 for r in rows if not r.get("error"))
    n_trust   = sum(1 for r in rows if r["pbo"] <= 0.3)
    n_warn    = sum(1 for r in rows if 0.3 < r["pbo"] <= 0.5)
    n_overfit = sum(1 for r in rows if r["pbo"] > 0.5)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CPCV WATCHLIST サマリー</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0f172a; color:#e2e8f0; font-family:'Segoe UI',system-ui,sans-serif;
       font-size:14px; line-height:1.6; }}
.container {{ max-width:1100px; margin:0 auto; padding:1.5rem 1rem; }}
h1 {{ color:#60a5fa; font-size:1.5rem; margin-bottom:.4rem; }}
.subtitle {{ color:#94a3b8; font-size:.85rem; margin-bottom:1.4rem; }}
.kpi-grid {{ display:flex; gap:.8rem; flex-wrap:wrap; margin-bottom:1.4rem; }}
.kpi {{ background:#111827; border:1px solid #1e293b; border-radius:8px;
        padding:.8rem 1.2rem; text-align:center; min-width:130px; }}
.kpi-label {{ color:#64748b; font-size:.72rem; text-transform:uppercase; }}
.kpi-val {{ font-size:1.3rem; font-weight:700; margin-top:.2rem; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ background:#1e293b; color:#94a3b8; padding:8px 10px; text-align:left;
      font-size:.8rem; }}
td {{ border-bottom:1px solid #1e293b; padding:7px 10px; }}
tr:hover td {{ background:#0d1424; }}
</style>
</head>
<body>
<div class="container">
  <h1>CPCV WATCHLIST サマリー</h1>
  <div class="subtitle">
    全 {len(rows)} 銘柄 &nbsp;|&nbsp; モード: {mode} &nbsp;|&nbsp; 実行日: {TODAY}
    &nbsp;|&nbsp; PBO昇順
  </div>
  <div class="kpi-grid">
    <div class="kpi"><div class="kpi-label">評価完了</div>
      <div class="kpi-val" style="color:#e2e8f0">{n_ok} / {len(rows)}</div></div>
    <div class="kpi"><div class="kpi-label">✅ 信頼 PBO≤30%</div>
      <div class="kpi-val" style="color:#4ade80">{n_trust}</div></div>
    <div class="kpi"><div class="kpi-label">⚠️ 要注意 30-50%</div>
      <div class="kpi-val" style="color:#fbbf24">{n_warn}</div></div>
    <div class="kpi"><div class="kpi-label">❌ 過学習 PBO>50%</div>
      <div class="kpi-val" style="color:#f87171">{n_overfit}</div></div>
  </div>
  <table>
    <thead><tr>
      <th>銘柄</th><th>戦略</th><th>PBO</th>
      <th>OOS勝率</th><th>OOS PF</th><th>OOS損益</th>
      <th>取引数</th><th>備考</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
  <p style="color:#475569;font-size:.75rem;margin-top:1rem">
    ※ 銘柄名リンクをクリックすると個別詳細HTMLを開きます。
    OOS = アウトオブサンプル（未来データ）での成績。
  </p>
</div>
</body>
</html>"""


def _run_watchlist(args) -> None:
    """WATCHLIST 全銘柄を一括 CPCV 評価して個別 HTML + サマリー HTML を生成。"""
    items = _collect_watchlist_items()
    if not items:
        print("[ERROR] WATCHLIST が空です。check_signals_stop.py / breakout.py を確認してください。",
              file=sys.stderr)
        sys.exit(1)

    mode_suf = f"_{TRADING_MODE}" if TRADING_MODE != "conservative" else ""
    print("=" * 70)
    print(f"CPCV WATCHLIST 一括評価: {len(items)} 銘柄  モード: {TRADING_MODE}")
    print(f"  グループ: {args.n_splits}  テスト: {args.k_test}  エンバーゴ: {args.embargo_days}日")
    print("=" * 70)

    summary_records: list[dict] = []

    for i, (symbol, name, strats) in enumerate(items, 1):
        print(f"\n[{i}/{len(items)}] {symbol} {name}  戦略: {strats}")
        fname = f"cpcv_{symbol.replace('.', '_')}{mode_suf}_{TODAY}.html"
        rec: dict = {"symbol": symbol, "name": name, "strategies": strats,
                     "html_file": fname, "pbo": 1.0, "oos_wr": 0.0,
                     "oos_pf": 0.0, "oos_pnl": 0.0, "trades": 0, "error": ""}
        try:
            res = run_cpcv(
                symbol, name,
                n_splits=args.n_splits,
                k_test=args.k_test,
                embargo_days=args.embargo_days,
                strategy_names=strats,
            )
            pbo = res["pbo"]["pbo"]
            lbl = "信頼できる" if pbo <= 0.3 else ("要注意" if pbo <= 0.5 else "過学習疑い")
            print(f"  PBO={pbo:.1%} ({lbl})")

            # OOS 集計 (全戦略合算)
            oos_trades = 0; oos_wins = 0; oos_pnl = 0.0; oos_gp = 0.0; oos_gl = 0.0
            for s in strats:
                sm = res["strategy_summary"].get(s)
                if sm:
                    oos_trades += sm.get("trades", 0)
                    oos_wins   += sm.get("wins", 0)
                    oos_pnl    += sm.get("total_pnl", 0.0)
                    oos_gp     += sm.get("gp", 0.0)
                    oos_gl     += sm.get("gl", 0.0)
            oos_wr = oos_wins / oos_trades * 100 if oos_trades else 0.0
            oos_pf = oos_gp / oos_gl if oos_gl > 0 else (float("inf") if oos_gp > 0 else 0.0)
            rec.update(pbo=pbo, oos_wr=oos_wr, oos_pf=oos_pf,
                       oos_pnl=oos_pnl, trades=oos_trades)

            html = build_html(symbol, name, res)
            Path(fname).write_text(html, encoding="utf-8")
            print(f"  → {fname}")
        except Exception as e:
            rec["error"] = str(e)[:60]
            print(f"  [SKIP] {e}")

        summary_records.append(rec)

    # サマリー HTML
    summary_fname = f"cpcv_watchlist_summary{mode_suf}_{TODAY}.html"
    summary_html  = _build_summary_html(summary_records, TRADING_MODE)
    Path(summary_fname).write_text(summary_html, encoding="utf-8")

    n_trust   = sum(1 for r in summary_records if r["pbo"] <= 0.3)
    n_warn    = sum(1 for r in summary_records if 0.3 < r["pbo"] <= 0.5)
    n_overfit = sum(1 for r in summary_records if r["pbo"] > 0.5)
    print(f"\n{'='*70}")
    print(f"完了: {len(items)} 銘柄")
    print(f"  ✅ 信頼できる (PBO≤30%): {n_trust} 銘柄")
    print(f"  ⚠️ 要注意    (30-50%): {n_warn} 銘柄")
    print(f"  ❌ 過学習疑い (>50%):   {n_overfit} 銘柄")
    print(f"\nサマリー → {Path(summary_fname).resolve()}")

    if not args.no_browser:
        from _open_html import open_html
        open_html(Path(summary_fname).resolve().as_uri())


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="CPCV バックテスト評価",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python cpcv_eval.py --symbol 7203.T
  python cpcv_eval.py --symbol 7203.T --n-splits 6 --k-test 2 --embargo-days 14
  python cpcv_eval.py --symbol 7203.T --strategies MACD A7 RSI2 --aggressive
  python cpcv_eval.py --watchlist                # WATCHLIST全銘柄を一括評価
  python cpcv_eval.py --watchlist --aggressive   # aggressive モードで全銘柄
        """,
    )
    sym_grp = parser.add_mutually_exclusive_group(required=True)
    sym_grp.add_argument("--symbol",    help="銘柄コード (例: 7203.T)")
    sym_grp.add_argument("--watchlist", action="store_true",
                         help="WATCHLIST全銘柄を一括評価してサマリーHTMLを生成")
    parser.add_argument("--name",         default="",    help="銘柄名 (--symbol 時のみ)")
    parser.add_argument("--n-splits",     type=int, default=6)
    parser.add_argument("--k-test",       type=int, default=2)
    parser.add_argument("--embargo-days", type=int, default=7)
    parser.add_argument("--strategies",   nargs="*", default=None,
                        help="評価戦略 (--symbol 時のみ有効)")
    parser.add_argument("--no-browser",   action="store_true")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument("--aggressive",   action="store_true")
    mode_grp.add_argument("--conservative", action="store_true")
    args = parser.parse_args()

    if args.watchlist:
        _run_watchlist(args)
        return

    symbol   = args.symbol
    name     = args.name or symbol
    strat_ns = args.strategies or ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]

    print("=" * 70)
    print(f"CPCV 評価開始: {symbol} ({name})")
    print(f"  グループ: {args.n_splits}  テスト: {args.k_test}  エンバーゴ: {args.embargo_days}日")
    print(f"  戦略: {', '.join(strat_ns)}")
    print(f"  モード: {TRADING_MODE}")
    print("=" * 70)

    try:
        res = run_cpcv(
            symbol, name,
            n_splits=args.n_splits,
            k_test=args.k_test,
            embargo_days=args.embargo_days,
            strategy_names=strat_ns,
        )
    except (RuntimeError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    pbo    = res["pbo"]["pbo"]
    lbl    = "信頼できる" if pbo <= 0.3 else ("要注意" if pbo <= 0.5 else "過学習疑い")
    combos = res["pbo"]["n_combos"]
    print(f"\n── 結果 ──────────────────────────────────────")
    print(f"  PBO = {pbo:.1%}  ({lbl})  組合せ数: {combos}")
    for s in strat_ns:
        if s not in res["strategy_summary"]:
            continue
        m = res["strategy_summary"][s]
        print(f"  {s:8}: {m['trades']:3}取引  WR={m['win_rate']:.1f}%  "
              f"PF={m['pf']:.2f}  PnL={m['total_pnl']:+,.0f}")

    html = build_html(symbol, name, res)

    mode_suf = f"_{TRADING_MODE}" if TRADING_MODE != "conservative" else ""
    fname    = f"cpcv_{symbol.replace('.', '_')}{mode_suf}_{TODAY}.html"
    out      = Path(fname)
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML → {out.resolve()}")

    if not args.no_browser:
        from _open_html import open_html
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
