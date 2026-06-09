"""
cpcv_eval.py  ―  CPCV (Combinatorial Purged Cross-Validation) バックテスト評価
=====================================================================================
特定銘柄 × 複数戦略に対して CPCV を実行し、戦略選定の信頼性を評価する。

【解決する問題】
1. 生存バイアス (直近の運で評価が歪む):
   N分割 × C(N,k)通りの組み合わせで全時期を網羅的にテストする。
   「たまたま好調な1期間」に依存した評価を排除できる。

2. ルックアヘッドバイアス (今日に近い評価が実質的に未来を使う):
   各グループ境界に「エンバーゴ期間」を挿入し、テクニカル指標の滲み出しを防ぐ。

3. 保有中ポジション問題: 各ウィンドウ末尾の保有中ポジションを集計から除外する。

【PBO (Probability of Backtest Overfitting) とは】
C(N,k) 通りの組合せそれぞれで:
  - IS (In-Sample, TRAIN) で最もパフォーマンスの良かった戦略を特定
  - その戦略の OOS (Out-of-Sample, TEST) 順位を確認
  - OOS順位が「中央値以下」(下半分) なら「過学習」と判定

PBO = 過学習と判定された組合せの割合
  PBO ≈ 0.0 → IS最良 = OOS最良 (過学習なし, 理想的)
  PBO ≈ 0.5 → ランダムと同等 (完全に過学習)
  PBO ≤ 0.3 → 戦略選定として信頼できる水準

参考文献: Bailey, Borwein, Lopez de Prado, Zhu (2014), "The Probability of Backtest Overfitting"
         Lopez de Prado (2018), "Advances in Financial Machine Learning" Ch.12

【使い方】
  python cpcv_eval.py --symbol 7203.T
  python cpcv_eval.py --symbol 7203.T --n-splits 6 --k-test 2
  python cpcv_eval.py --symbol 7203.T --embargo-days 14
  python cpcv_eval.py --symbol 7203.T --strategies MACD A7 RSI2
  python cpcv_eval.py --symbol 7203.T --days 600 --no-browser
  python cpcv_eval.py --symbol 7203.T --aggressive

【出力】
  cpcv_<symbol>_<date>.html  (自動的にブラウザで開く)
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

import pandas as pd

import os as _os_pre
if "--aggressive" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "conservative"

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    MAX_HOLD,
)
from risk_metrics import enrich_backtest_result

import os as _os
TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()

# STRATEGY_DEFS は check_signals_stop / check_signals_breakout から取得
# scan_walkforward と同一定義を再利用
from scan_walkforward import STRATEGY_DEFS

JST          = timezone(timedelta(hours=9))
TODAY        = datetime.now(JST).date()
INITIAL_CASH = 500_000


# ─────────────────────────────────────────────────────────────
# データ分割
# ─────────────────────────────────────────────────────────────

def split_into_groups(df: pd.DataFrame, n_splits: int,
                      embargo_days: int) -> list[dict]:
    """
    df を n_splits 個の等サイズグループに分割する。

    各グループ間に embargo_days 本の「空白バー」を挿入し、
    前グループの指標値が次グループへ滲み出すのを防ぐ。

    返り値: [{"idx": int, "start": date, "end": date}, ...]
    """
    dates = list(df.index)
    n     = len(dates)

    # グループ間に embargo 用のバーを確保した上でグループサイズを決める
    total_embargo = (n_splits - 1) * embargo_days
    usable        = n - total_embargo
    if usable < n_splits * 10:
        raise ValueError(
            f"データ {n} バーに対して n_splits={n_splits}, embargo={embargo_days} は小さすぎます。"
        )
    group_size = usable // n_splits

    groups   = []
    cur_idx  = 0
    for i in range(n_splits):
        start_idx = cur_idx
        end_idx   = start_idx + group_size - 1
        if i == n_splits - 1:          # 最後のグループは残り全部
            end_idx = n - 1
        groups.append({
            "idx":   i,
            "start": dates[start_idx].date(),
            "end":   dates[end_idx].date(),
        })
        cur_idx = end_idx + 1 + embargo_days   # 次グループ開始 (embargo スキップ)

    return groups


# ─────────────────────────────────────────────────────────────
# 1グループ × 1戦略 のバックテスト
# ─────────────────────────────────────────────────────────────

def _run_group(symbol: str, name: str, full_df: pd.DataFrame,
               calc_fn, em: float, sm: float, tm: float,
               group_start, group_end,
               strategy_name: str, entry_type: str) -> dict | None:
    """
    [group_start, group_end] 期間のバックテストを実行する。

    scan_walkforward._run_window と同じ技法:
      - df を group_end までトリミング (未来データを見ない)
      - backtest_days = (TODAY - group_start).days → cutoff が group_start になる
    """
    df_trimmed    = full_df[full_df.index <= pd.Timestamp(group_end)].copy()
    backtest_days = (TODAY - group_start).days
    if backtest_days <= 0 or len(df_trimmed) < 30:
        return None
    try:
        result = run_limit_backtest(
            symbol, name, df_trimmed, calc_fn,
            em, sm, tm, backtest_days,
            strategy_name, entry_type=entry_type,
        )
    except Exception:
        return None

    enriched = enrich_backtest_result(result, INITIAL_CASH)
    # 保有中ポジションを統計から除外 (保有中問題の解決)
    tl = [t for t in enriched.get("trade_log", [])
          if t.get("reason") != "保有中"]
    enriched["trades"]    = len([t for t in tl if t.get("pnl") is not None])
    enriched["trade_log"] = tl
    return enriched


# ─────────────────────────────────────────────────────────────
# PBO 計算
# ─────────────────────────────────────────────────────────────

def calc_pbo(combo_results: list[dict], strategy_names: list[str]) -> dict:
    """
    PBO (Probability of Backtest Overfitting) を計算する。

    各組合せで:
      1. IS (TRAIN) 成績が最良の戦略 S* を特定
      2. S* の OOS (TEST) 順位を確認 (1位が最良)
      3. OOS順位が全戦略中の中央値以下 (下位半分) なら「過学習」とみなす

    PBO = 過学習組合せ数 / 全組合せ数

    また、全組合せでの「戦略ごとの OOS 勝率」も返す。
    """
    n_strats = len(strategy_names)
    n_combos = len(combo_results)
    if n_combos == 0 or n_strats < 2:
        return {"pbo": float("nan"), "per_strategy": {}, "n_combos": n_combos}

    overfit_count = 0
    # 各戦略が「IS-best のときに OOS でも best だった回数」
    strat_is_best_oos_best: dict[str, int] = {s: 0 for s in strategy_names}
    strat_is_best_count:    dict[str, int] = {s: 0 for s in strategy_names}

    for combo in combo_results:
        sd = combo["strategies"]

        # IS最良 (TRAIN PnL 最大)
        is_best = max(strategy_names,
                      key=lambda s: sd[s].get("train_total_pnl", float("-inf")))
        strat_is_best_count[is_best] += 1

        # OOS順位 (TEST PnL で降順ソート → 小さいindexが良い)
        oos_ranked = sorted(strategy_names,
                            key=lambda s: -sd[s].get("test_total_pnl", float("-inf")))
        oos_rank_of_is_best = oos_ranked.index(is_best) + 1  # 1始まり

        if oos_rank_of_is_best == 1:
            strat_is_best_oos_best[is_best] += 1

        # OOS順位が中央値以下 (下位半分) → 過学習
        if oos_rank_of_is_best > n_strats / 2:
            overfit_count += 1

    pbo = overfit_count / n_combos

    per_strategy = {}
    for s in strategy_names:
        cnt = strat_is_best_count[s]
        per_strategy[s] = {
            "is_best_count":     cnt,
            "oos_best_when_is":  strat_is_best_oos_best[s],
            "consistency_pct":   (strat_is_best_oos_best[s] / cnt * 100) if cnt else 0.0,
        }

    return {"pbo": round(pbo, 3), "per_strategy": per_strategy, "n_combos": n_combos}


# ─────────────────────────────────────────────────────────────
# メイン CPCV ロジック
# ─────────────────────────────────────────────────────────────

def run_cpcv(symbol: str, name: str, full_df: pd.DataFrame,
             strategy_names: list[str],
             n_splits: int = 6, k_test: int = 2,
             embargo_days: int = 7) -> dict:
    """
    CPCV を実行して結果を返す。

    返り値:
      {
        "groups": [{idx, start, end}, ...],
        "group_results": {(group_idx, strategy_name): metrics_dict, ...},
        "combo_results": [{test_groups, train_groups, strategies: {strat: {...}}}, ...],
        "pbo_result": {pbo, per_strategy, n_combos},
        "strategy_summary": {strat: {oos_median_pnl, oos_pass_rate, ...}, ...},
        "n_splits": int, "k_test": int, "embargo_days": int,
      }
    """
    groups = split_into_groups(full_df, n_splits, embargo_days)
    n_groups = len(groups)
    print(f"  グループ分割完了: {n_groups}グループ × {len(strategy_names)}戦略 = "
          f"{n_groups * len(strategy_names)}バックテスト")

    # ── グループ × 戦略 の全バックテスト ──
    group_results: dict[tuple, dict | None] = {}
    total = n_groups * len(strategy_names)
    done  = 0
    for g in groups:
        for strat_name in strategy_names:
            calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strat_name]
            r = _run_group(symbol, name, full_df, calc_fn, em, sm, tm,
                           g["start"], g["end"], strat_name, entry_type)
            group_results[(g["idx"], strat_name)] = r
            done += 1
            if done % 5 == 0 or done == total:
                print(f"  進捗: {done}/{total}", end="\r", flush=True)
    print()

    # ── C(N, k) 組合せを生成 ──
    all_group_idx = list(range(n_groups))
    all_combos    = list(combinations(all_group_idx, k_test))

    combo_results = []
    for test_groups in all_combos:
        train_groups = tuple(i for i in all_group_idx if i not in test_groups)

        strategies_data: dict[str, dict] = {}
        for strat_name in strategy_names:
            # TEST: k グループの合計
            test_pnl = sum(
                (group_results.get((g, strat_name)) or {}).get("total_pnl", 0.0)
                for g in test_groups
            )
            test_trades = sum(
                (group_results.get((g, strat_name)) or {}).get("trades", 0)
                for g in test_groups
            )
            test_wins = sum(
                sum(1 for t in (group_results.get((g, strat_name)) or {}).get("trade_log", [])
                    if t.get("pnl", 0) > 0)
                for g in test_groups
            )
            test_wr = (test_wins / test_trades * 100) if test_trades > 0 else 0.0

            # TRAIN: N-k グループの合計
            train_pnl = sum(
                (group_results.get((g, strat_name)) or {}).get("total_pnl", 0.0)
                for g in train_groups
            )
            train_trades = sum(
                (group_results.get((g, strat_name)) or {}).get("trades", 0)
                for g in train_groups
            )

            # PF: TEST全体
            all_test_trades = []
            for g in test_groups:
                r = group_results.get((g, strat_name))
                if r:
                    all_test_trades.extend(r.get("trade_log", []))
            wins   = [t for t in all_test_trades if t.get("pnl", 0) > 0]
            losses = [t for t in all_test_trades if t.get("pnl", 0) < 0]
            gross_profit = sum(t["pnl"] for t in wins)
            gross_loss   = abs(sum(t["pnl"] for t in losses))
            test_pf = gross_profit / gross_loss if gross_loss > 0 else (
                10.0 if gross_profit > 0 else 0.0
            )

            strategies_data[strat_name] = {
                "test_total_pnl":  round(test_pnl, 0),
                "test_trades":     test_trades,
                "test_win_rate":   round(test_wr, 1),
                "test_pf":         round(min(test_pf, 10.0), 2),
                "train_total_pnl": round(train_pnl, 0),
                "train_trades":    train_trades,
            }

        combo_results.append({
            "test_groups":  test_groups,
            "train_groups": train_groups,
            "strategies":   strategies_data,
        })

    # ── PBO 計算 ──
    pbo_result = calc_pbo(combo_results, strategy_names)

    # ── 戦略別 OOS サマリー ──
    strategy_summary: dict[str, dict] = {}
    for strat_name in strategy_names:
        oos_pnls   = [c["strategies"][strat_name]["test_total_pnl"]  for c in combo_results]
        oos_trades = [c["strategies"][strat_name]["test_trades"]      for c in combo_results]
        oos_wr     = [c["strategies"][strat_name]["test_win_rate"]    for c in combo_results]
        oos_pf     = [c["strategies"][strat_name]["test_pf"]         for c in combo_results]

        n = len(oos_pnls)
        sorted_pnls = sorted(oos_pnls)
        median_pnl  = sorted_pnls[n // 2] if n else 0.0

        positive_combos = sum(1 for p in oos_pnls if p > 0)
        pass_rate = positive_combos / n * 100 if n else 0.0

        strategy_summary[strat_name] = {
            "oos_median_pnl":    round(median_pnl, 0),
            "oos_mean_pnl":      round(sum(oos_pnls) / n, 0) if n else 0.0,
            "oos_min_pnl":       round(min(oos_pnls), 0) if oos_pnls else 0.0,
            "oos_max_pnl":       round(max(oos_pnls), 0) if oos_pnls else 0.0,
            "oos_pass_rate":     round(pass_rate, 1),
            "oos_median_wr":     round(sorted(oos_wr)[n // 2], 1) if n else 0.0,
            "oos_median_pf":     round(sorted(oos_pf)[n // 2], 2) if n else 0.0,
            "oos_mean_trades":   round(sum(oos_trades) / n, 1) if n else 0.0,
        }

    return {
        "groups":           groups,
        "group_results":    group_results,
        "combo_results":    combo_results,
        "pbo_result":       pbo_result,
        "strategy_summary": strategy_summary,
        "strategy_names":   strategy_names,
        "n_splits":         n_splits,
        "k_test":           k_test,
        "embargo_days":     embargo_days,
    }


# ─────────────────────────────────────────────────────────────
# HTML レポート生成
# ─────────────────────────────────────────────────────────────

def _pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "#d4edda"  # 緑
    if pnl < 0:
        return "#f8d7da"  # 赤
    return "#ffffff"


def _pbo_badge(pbo: float) -> str:
    if pbo != pbo:   # NaN
        return '<span style="color:#888">N/A</span>'
    color = ("#28a745" if pbo <= 0.20 else
             "#5bc0de" if pbo <= 0.30 else
             "#f0ad4e" if pbo <= 0.50 else
             "#dc3545")
    label = ("優秀" if pbo <= 0.20 else
              "良好" if pbo <= 0.30 else
              "要注意" if pbo <= 0.50 else
              "過学習疑い")
    return (f'<span style="background:{color};color:#fff;'
            f'padding:2px 8px;border-radius:4px;font-size:0.85em">'
            f'{pbo:.3f} ({label})</span>')


def build_html(symbol: str, name: str, cpcv_result: dict) -> str:
    groups         = cpcv_result["groups"]
    combo_results  = cpcv_result["combo_results"]
    pbo_result     = cpcv_result["pbo_result"]
    strategy_summary = cpcv_result["strategy_summary"]
    strategy_names = cpcv_result["strategy_names"]
    n_splits       = cpcv_result["n_splits"]
    k_test         = cpcv_result["k_test"]
    embargo_days   = cpcv_result["embargo_days"]
    group_results  = cpcv_result["group_results"]
    pbo            = pbo_result["pbo"]
    n_combos       = pbo_result["n_combos"]

    from math import comb
    expected_combos = comb(n_splits, k_test)

    # ── セクション1: 設定サマリー ──
    sec_config = f"""
<section>
  <h2>CPCV設定</h2>
  <table class="summary-tbl">
    <tr><th>銘柄</th><td>{symbol} ({name})</td>
        <th>分割数 N</th><td>{n_splits}</td></tr>
    <tr><th>テスト組数 k</th><td>{k_test}</td>
        <th>組合せ数 C(N,k)</th><td>{expected_combos}</td></tr>
    <tr><th>エンバーゴ</th><td>{embargo_days}日 (各グループ間の空白バー)</td>
        <th>トレードモード</th><td>{TRADING_MODE}</td></tr>
    <tr><th>評価戦略</th><td colspan="3">{', '.join(strategy_names)}</td></tr>
    <tr><th>基準日</th><td colspan="3">{TODAY}</td></tr>
  </table>
</section>
"""

    # ── セクション2: グループ構造 ──
    group_rows = ""
    for g in groups:
        group_rows += (
            f"<tr><td>Group {g['idx']+1}</td>"
            f"<td>{g['start']}</td><td>{g['end']}</td></tr>\n"
        )
    sec_groups = f"""
<section>
  <h2>グループ分割</h2>
  <p>全データを {n_splits} 個の等サイズグループに分割 (グループ間エンバーゴ: {embargo_days}日)。</p>
  <table class="group-tbl">
    <thead><tr><th>グループ</th><th>開始日</th><th>終了日</th></tr></thead>
    <tbody>{group_rows}</tbody>
  </table>
</section>
"""

    # ── セクション3: 戦略別 OOS サマリー ──
    def _recsym(summary: dict) -> str:
        pr = summary["oos_pass_rate"]
        pf = summary["oos_median_pf"]
        if pr >= 60 and pf >= 1.3:
            return '<span class="badge-green">✅ 推奨</span>'
        if pr >= 40 and pf >= 1.0:
            return '<span class="badge-yellow">⚠️ 要確認</span>'
        return '<span class="badge-red">❌ 非推奨</span>'

    summary_rows = ""
    for strat_name in strategy_names:
        s = strategy_summary[strat_name]
        ps = pbo_result["per_strategy"].get(strat_name, {})
        summary_rows += (
            f"<tr>"
            f"<td><strong>{strat_name}</strong></td>"
            f"<td style='background:{_pnl_color(s['oos_median_pnl'])}'>"
            f"{s['oos_median_pnl']:+,.0f}円</td>"
            f"<td style='background:{_pnl_color(s['oos_mean_pnl'])}'>"
            f"{s['oos_mean_pnl']:+,.0f}円</td>"
            f"<td>{s['oos_min_pnl']:+,.0f} / {s['oos_max_pnl']:+,.0f}</td>"
            f"<td>{s['oos_pass_rate']}%</td>"
            f"<td>{s['oos_median_wr']}%</td>"
            f"<td>{s['oos_median_pf']:.2f}</td>"
            f"<td>{s['oos_mean_trades']:.1f}</td>"
            f"<td>{ps.get('is_best_count', 0)}/{n_combos}</td>"
            f"<td>{ps.get('consistency_pct', 0.0):.0f}%</td>"
            f"<td>{_recsym(s)}</td>"
            f"</tr>\n"
        )

    sec_summary = f"""
<section>
  <h2>戦略別 OOS (Out-of-Sample) 成績サマリー</h2>
  <p>全 {n_combos} 通りの組合せでテストされた OOS 成績の統計。</p>
  <table class="main-tbl">
    <thead>
      <tr>
        <th>戦略</th>
        <th>OOS中央値損益</th><th>OOS平均損益</th><th>最小/最大損益</th>
        <th>プラス率</th><th>中央値WR</th><th>中央値PF</th>
        <th>平均取引数</th>
        <th>IS最良回数</th><th>一貫性</th><th>推奨</th>
      </tr>
    </thead>
    <tbody>{summary_rows}</tbody>
  </table>
  <p style="font-size:0.85em;color:#666">
    プラス率 = OOS損益がプラスだった組合せの割合 (60%以上が望ましい)<br>
    一貫性 = IS最良だったときにOOSでも最良だった割合 (高いほど安定)
  </p>
</section>
"""

    # ── セクション4: PBO ──
    pbo_rows = ""
    for strat_name in strategy_names:
        ps = pbo_result["per_strategy"].get(strat_name, {})
        pbo_rows += (
            f"<tr><td>{strat_name}</td>"
            f"<td>{ps.get('is_best_count', 0)}</td>"
            f"<td>{ps.get('oos_best_when_is', 0)}</td>"
            f"<td>{ps.get('consistency_pct', 0.0):.0f}%</td></tr>\n"
        )

    pbo_interpretation = (
        "過学習の懸念が低く、戦略選定の信頼性が高い" if pbo <= 0.20 else
        "許容範囲。実運用では注意して監視を" if pbo <= 0.30 else
        "過学習の可能性あり。WATCHLISTへの採用は慎重に" if pbo <= 0.50 else
        "過学習の強い疑い。この銘柄への戦略適用は再検討を"
    )

    sec_pbo = f"""
<section>
  <h2>PBO (Probability of Backtest Overfitting)</h2>
  <div class="pbo-box">
    <div class="pbo-value">PBO = {_pbo_badge(pbo)}</div>
    <p class="pbo-interp">→ {pbo_interpretation}</p>
    <p style="font-size:0.85em;color:#555">
      PBO = IS (訓練データ) で最良だった戦略が、OOS (検証データ) で下位半分に入った組合せの割合。<br>
      {pbo_result['n_combos']} 通りの組合せのうち、IS最良がOOS下位半分になったのは
      {round(pbo * pbo_result['n_combos'])} 回。
    </p>
  </div>
  <h3>戦略ごとの IS vs OOS 一貫性</h3>
  <table class="main-tbl">
    <thead>
      <tr>
        <th>戦略</th><th>IS最良回数</th><th>OOS最良回数(IS最良時)</th><th>一貫性</th>
      </tr>
    </thead>
    <tbody>{pbo_rows}</tbody>
  </table>
</section>
"""

    # ── セクション5: 全グループ × 全戦略 ヒートマップ ──
    heat_header = "<tr><th>グループ</th>" + "".join(
        f"<th>{s}</th>" for s in strategy_names
    ) + "</tr>"

    heat_rows = ""
    for g in groups:
        heat_rows += f"<tr><td>G{g['idx']+1}<br><small>{g['start']}<br>〜{g['end']}</small></td>"
        for strat_name in strategy_names:
            r = group_results.get((g["idx"], strat_name))
            if r is None:
                heat_rows += '<td style="background:#eee;color:#999">N/A</td>'
            else:
                pnl    = r.get("total_pnl", 0)
                trades = r.get("trades", 0)
                pf     = r.get("pf", 0)
                wr     = r.get("win_rate", 0)
                bg     = _pnl_color(pnl)
                heat_rows += (
                    f'<td style="background:{bg}">'
                    f"<strong>{pnl:+,.0f}</strong><br>"
                    f"<small>{trades}件 WR{wr:.0f}% PF{pf:.1f}</small></td>"
                )
        heat_rows += "</tr>\n"

    sec_heat = f"""
<section>
  <h2>グループ別バックテスト成績 (ヒートマップ)</h2>
  <p>各グループ (行) × 各戦略 (列) の OOS バックテスト結果。
     緑=プラス、赤=マイナス。</p>
  <div style="overflow-x:auto">
    <table class="heat-tbl">
      <thead>{heat_header}</thead>
      <tbody>{heat_rows}</tbody>
    </table>
  </div>
</section>
"""

    # ── セクション6: 全組合せ結果テーブル (上位20件) ──
    sorted_combos = sorted(
        combo_results,
        key=lambda c: -sum(c["strategies"][s]["test_total_pnl"] for s in strategy_names),
    )

    combo_header = (
        "<tr><th>組合せ</th><th>TEST期間</th>"
        + "".join(f"<th>{s}<br>TEST損益</th>" for s in strategy_names)
        + "<th>IS最良</th><th>OOS最良</th><th>一致?</th></tr>"
    )
    combo_rows_html = ""
    for c in sorted_combos[:30]:
        test_g_labels = ", ".join(
            f"G{i+1}({groups[i]['start']}〜{groups[i]['end']})"
            for i in c["test_groups"]
        )
        is_best  = max(strategy_names,
                       key=lambda s: c["strategies"][s]["train_total_pnl"])
        oos_best = max(strategy_names,
                       key=lambda s: c["strategies"][s]["test_total_pnl"])
        match_icon = "✅" if is_best == oos_best else "❌"

        cells = ""
        for strat_name in strategy_names:
            pnl = c["strategies"][strat_name]["test_total_pnl"]
            cells += f'<td style="background:{_pnl_color(pnl)}">{pnl:+,.0f}</td>'

        combo_rows_html += (
            f"<tr><td>{','.join(f'G{i+1}' for i in c['test_groups'])}</td>"
            f"<td style='font-size:0.8em'>{test_g_labels}</td>"
            f"{cells}"
            f"<td><strong>{is_best}</strong></td>"
            f"<td><strong>{oos_best}</strong></td>"
            f"<td style='font-size:1.2em'>{match_icon}</td></tr>\n"
        )

    sec_combos = f"""
<section>
  <h2>全組合せ結果 (上位 {min(30, n_combos)} / {n_combos} 件)</h2>
  <p>各行が1つの CPCV 組合せ。IS最良戦略と OOS最良戦略の一致/不一致を示す。</p>
  <div style="overflow-x:auto">
    <table class="main-tbl">
      <thead>{combo_header}</thead>
      <tbody>{combo_rows_html}</tbody>
    </table>
  </div>
</section>
"""

    # ── セクション7: 読み方ガイド ──
    sec_guide = """
<section class="guide">
  <h2>読み方ガイド</h2>
  <dl>
    <dt>PBO (Probability of Backtest Overfitting)</dt>
    <dd>IS (訓練データ) で最良だった戦略が、OOS (検証データ) でも最良かどうかの確率指標。
        PBO ≤ 0.3 → 信頼できる。PBO ≥ 0.5 → 過学習の疑い。</dd>
    <dt>OOS プラス率</dt>
    <dd>その戦略が C(N,k) 全組合せのうちプラスになった割合。
        60%以上が望ましい。これが高い戦略はどの時期でも安定して勝てる。</dd>
    <dt>一貫性 (IS vs OOS)</dt>
    <dd>「IS (訓練) で最良と判断したとき、OOS でも最良だった割合」。
        高いほど、訓練での評価が実際の将来性能を正しく反映している。</dd>
    <dt>グループヒートマップ</dt>
    <dd>銘柄 × 戦略がどの時期でも安定しているか一覧できる。
        特定の時期だけ緑 (プラス) の場合は「その期間だけ運が良かった」可能性が高い。</dd>
  </dl>
</section>
"""

    # ── HTML 組み立て ──
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>CPCV評価: {symbol} ({name})</title>
<style>
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; margin: 20px;
         background: #f8f9fa; color: #333; }}
  h1   {{ font-size: 1.6em; border-bottom: 3px solid #007bff; padding-bottom: 8px; }}
  h2   {{ font-size: 1.2em; margin-top: 30px; color: #0056b3; }}
  h3   {{ font-size: 1.0em; color: #555; }}
  section {{ background: #fff; border-radius: 6px; padding: 20px;
             margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.88em; }}
  th    {{ background: #343a40; color: #fff; padding: 6px 10px; }}
  td    {{ border: 1px solid #dee2e6; padding: 5px 10px; }}
  tr:hover {{ background: #f1f3f5 !important; }}
  .summary-tbl th {{ background: #495057; width: 120px; }}
  .group-tbl   {{ max-width: 400px; }}
  .heat-tbl td {{ text-align: center; min-width: 100px; }}
  .main-tbl td {{ text-align: right; }}
  .main-tbl td:first-child {{ text-align: left; font-weight: bold; }}
  .pbo-box  {{ background: #f8f9fa; border-left: 4px solid #007bff;
               padding: 15px 20px; border-radius: 4px; margin: 10px 0; }}
  .pbo-value {{ font-size: 1.4em; margin-bottom: 5px; }}
  .pbo-interp {{ margin: 5px 0; font-weight: bold; }}
  .badge-green  {{ background:#28a745;color:#fff;padding:2px 8px;
                   border-radius:4px;font-size:0.85em; }}
  .badge-yellow {{ background:#f0ad4e;color:#fff;padding:2px 8px;
                   border-radius:4px;font-size:0.85em; }}
  .badge-red    {{ background:#dc3545;color:#fff;padding:2px 8px;
                   border-radius:4px;font-size:0.85em; }}
  .guide dt {{ font-weight: bold; margin-top: 10px; }}
  .guide dd {{ margin-left: 15px; color: #555; }}
</style>
</head>
<body>
<h1>CPCV評価レポート: {symbol} ({name})</h1>
<p>生成日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M')} ／
   N={n_splits}分割, k={k_test}テスト, C(N,k)={expected_combos}通り,
   エンバーゴ={embargo_days}日</p>

{sec_config}
{sec_groups}
{sec_summary}
{sec_pbo}
{sec_heat}
{sec_combos}
{sec_guide}
</body>
</html>"""
    return html


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CPCV バックテスト評価 (PBO 計算付き)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python cpcv_eval.py --symbol 7203.T
  python cpcv_eval.py --symbol 7203.T --n-splits 6 --k-test 2
  python cpcv_eval.py --symbol 7203.T --embargo-days 14
  python cpcv_eval.py --symbol 7203.T --strategies MACD A7 RSI2
  python cpcv_eval.py --symbol 7203.T --no-browser
        """,
    )
    parser.add_argument("--symbol",       required=True, help="銘柄コード (例: 7203.T)")
    parser.add_argument("--name",         default="",   help="銘柄名 (省略時は symbol と同じ)")
    parser.add_argument("--n-splits",     type=int, default=6,
                        help="分割数 N (デフォルト: 6, 推奨: 5〜8)")
    parser.add_argument("--k-test",       type=int, default=2,
                        help="テストに使うグループ数 k (デフォルト: 2)")
    parser.add_argument("--embargo-days", type=int, default=7,
                        help="グループ間のバッファー日数 (デフォルト: 7)")
    parser.add_argument("--days",         type=int, default=730,
                        help="取得する最大日数 (デフォルト: 730)")
    parser.add_argument("--strategies",   nargs="+",
                        default=["MACD", "A7", "RSI2", "DON", "VOL", "MOM"],
                        choices=list(STRATEGY_DEFS.keys()),
                        help="評価する戦略 (デフォルト: 全6戦略)")
    parser.add_argument("--no-browser",   action="store_true")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument("--aggressive",   action="store_true")
    mode_grp.add_argument("--conservative", action="store_true")
    args = parser.parse_args()

    symbol   = args.symbol
    name     = args.name or symbol
    n_splits = args.n_splits
    k_test   = args.k_test

    if k_test >= n_splits:
        print(f"[ERROR] k-test ({k_test}) は n-splits ({n_splits}) より小さくしてください",
              file=sys.stderr)
        sys.exit(1)

    from math import comb
    n_combos = comb(n_splits, k_test)

    print("=" * 60)
    print(f"CPCV評価: {symbol} ({name})")
    print(f"  N={n_splits} 分割, k={k_test} テスト, "
          f"C(N,k)={n_combos} 組合せ")
    print(f"  エンバーゴ: {args.embargo_days}日")
    print(f"  戦略: {', '.join(args.strategies)}")
    print(f"  モード: {TRADING_MODE}")
    print("=" * 60)

    print(f"データ取得中: {symbol}...")
    full_df = fetch(symbol, args.days)
    if full_df is None or len(full_df) < 100:
        print(f"[ERROR] {symbol} のデータ取得に失敗しました", file=sys.stderr)
        sys.exit(1)
    print(f"  取得完了: {len(full_df)} バー "
          f"({full_df.index[0].date()} 〜 {full_df.index[-1].date()})")

    print("CPCV実行中...")
    result = run_cpcv(
        symbol, name, full_df,
        strategy_names=args.strategies,
        n_splits=n_splits,
        k_test=k_test,
        embargo_days=args.embargo_days,
    )

    # ── 結果サマリー表示 ──
    print()
    print("=" * 60)
    print("戦略別 OOS サマリー:")
    print(f"  {'戦略':<8} {'中央値PnL':>12} {'プラス率':>8} {'WR':>7} {'PF':>6}")
    print("  " + "-" * 50)
    for strat_name in args.strategies:
        s = result["strategy_summary"][strat_name]
        print(f"  {strat_name:<8} "
              f"{s['oos_median_pnl']:>+12,.0f}円 "
              f"{s['oos_pass_rate']:>7.1f}% "
              f"{s['oos_median_wr']:>6.1f}% "
              f"{s['oos_median_pf']:>6.2f}")

    pbo = result["pbo_result"]["pbo"]
    print()
    pbo_label = (
        "優秀 (過学習なし)" if pbo <= 0.20 else
        "良好 (許容範囲)"  if pbo <= 0.30 else
        "要注意"          if pbo <= 0.50 else
        "過学習疑い"
    )
    print(f"PBO = {pbo:.3f}  → {pbo_label}")
    print("=" * 60)

    # HTML 生成
    html_str  = build_html(symbol, name, result)
    date_str  = TODAY.isoformat()
    mode_suf  = f"_{TRADING_MODE}" if TRADING_MODE != "conservative" else ""
    html_path = Path(f"cpcv_{symbol.replace('.', '_')}{mode_suf}_{date_str}.html")
    html_path.write_text(html_str, encoding="utf-8")
    print(f"\nHTML: {html_path.resolve()}")

    if not args.no_browser:
        try:
            from _open_html import open_html
            open_html(str(html_path))
        except ImportError:
            webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
