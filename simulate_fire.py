"""
simulate_fire.py ― ロング6戦略 資産形成シミュレーター
==========================================================
backtest_limit_entry.py の実定数（スリッページ・手数料・ATR倍率）から
1トレードの期待値を計算し、モンテカルロ法で資産推移を試算します。

【使い方】
  python simulate_fire.py                    # デフォルト設定でHTML+表示
  python simulate_fire.py --target 10000000  # FIRE目標 1000万円（デフォルト）
  python simulate_fire.py --target 30000000  # FIRE目標 3000万円
  python simulate_fire.py --initial 1000000  # 初期資金 100万円
  python simulate_fire.py --simulations 5000 # シミュレーション回数
  python simulate_fire.py --years 20         # 最大シミュレーション年数
  python simulate_fire.py --no-browser       # HTML生成のみ
  python simulate_fire.py --text-only        # テキスト表示のみ（HTML生成なし）
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ── backtest_limit_entry.py の実定数をそのまま使用 ────────────────
SLIPPAGE_STOP_PCT = 0.005   # 逆指値スリッページ
FEE_PCT_ONE_WAY   = 0.001   # 片道手数料

# ── 戦略パラメータ (conservative mode デフォルト) ─────────────────
# (entry_atr_mult, stop_atr_mult, target_atr_mult, name)
STRATEGY_DEFS = {
    "MACD": (0.0, 1.5, 3.0),
    "A7":   (0.0, 1.5, 3.0),
    "RSI2": (0.0, 2.0, 4.0),
    "DON":  (0.0, 1.5, 3.0),
    "VOL":  (0.0, 1.5, 3.0),
    "MOM":  (0.0, 1.5, 3.0),
}

# ── 現実的な ATR 前提 ─────────────────────────────────────────────
# 日本株 1000-5000円帯のATR ≒ 終値の 1.5-2.5%
# 典型値として 2.0% を使用
ATR_RATIO = 0.020  # ATR / 終値

# ── WATCHLISTの戦略別典型的勝率（バックテスト実績の中央値推定）───
# ★★★相当の銘柄が中心: WR 50-60%, PF 1.5-2.5
# ★★相当が混在する現実を想定
SCENARIO_PARAMS = {
    "conservative": {
        "win_rate": 0.45,
        "label": "保守的 (WR=45%, ★相当)",
        "color": "#60a5fa",
        "trades_per_month": 15,   # 6戦略 × WATCHLIST × シグナル頻度
    },
    "moderate": {
        "win_rate": 0.50,
        "label": "中程度 (WR=50%, ★★相当)",
        "color": "#34d399",
        "trades_per_month": 18,
    },
    "optimistic": {
        "win_rate": 0.55,
        "label": "楽観的 (WR=55%, ★★★相当)",
        "color": "#fbbf24",
        "trades_per_month": 20,
    },
}

# ── 1トレードのP&L計算 ────────────────────────────────────────────
def calc_trade_stats(stock_price: float, em: float, sm: float, tm: float,
                     qty: int = 100) -> dict:
    """
    1トレードの期待損益・リスクリワード比を計算
    - order_p = close + atr * em  (逆指値価格)
    - stop    = order_p - atr * sm
    - target  = order_p + atr * tm
    - スリッページ・手数料込み
    """
    atr = stock_price * ATR_RATIO
    order_p = stock_price + atr * em  # em=0.0 → 終値ちょうど

    # スリッページ込みの実際の約定価格
    entry_p = order_p * (1.0 + SLIPPAGE_STOP_PCT)

    stop_p   = order_p - atr * sm
    target_p = order_p + atr * tm

    # 勝ちトレード（目標到達）: 指値売りなのでスリッページなし
    gross_win = (target_p - entry_p) * qty
    fee_win   = (entry_p + target_p) * qty * FEE_PCT_ONE_WAY
    net_win   = gross_win - fee_win

    # 負けトレード（損切り）: 逆指値売りなので不利約定
    stop_exit = stop_p * (1.0 - SLIPPAGE_STOP_PCT)
    gross_loss = (stop_exit - entry_p) * qty   # 負の値
    fee_loss   = (entry_p + stop_exit) * qty * FEE_PCT_ONE_WAY
    net_loss   = gross_loss - fee_loss   # 負

    rr = abs(net_win / net_loss) if net_loss != 0 else float('inf')

    return {
        "stock_price": stock_price,
        "atr": atr,
        "entry_p": entry_p,
        "stop_p": stop_p,
        "target_p": target_p,
        "net_win": net_win,
        "net_loss": net_loss,
        "rr": rr,
        "breakeven_wr": abs(net_loss) / (net_win + abs(net_loss)),
        "capital_deployed": entry_p * qty,
    }


# ── 損益期待値計算 ─────────────────────────────────────────────────
def calc_ev(trade_stats: dict, win_rate: float) -> float:
    return win_rate * trade_stats["net_win"] + (1 - win_rate) * trade_stats["net_loss"]


# ── モンテカルロ シミュレーション ─────────────────────────────────
def monte_carlo(
    initial_capital: float,
    target: float,
    years: int,
    win_rate: float,
    net_win: float,
    net_loss: float,
    trades_per_month: int,
    n_sim: int = 3000,
    max_concurrent: int = 3,
    position_size: float = 150_000,
) -> dict:
    """
    returns: {
        paths: list[list[float]],    # 全パスの月次資産 (間引き)
        p10/p50/p90: list[float],    # パーセンタイル
        fire_count: int,             # FIRE到達シミュ数
        fire_rate: float,
        fire_years_median: float,    # FIRE到達中央値年数
        ruin_count: int,             # 元本50%割れ
    }
    """
    total_months = years * 12
    all_finals = []
    fire_years_list = []
    ruin_count = 0
    # 間引き用に全パスではなく代表200本だけ記録
    sample_paths = []

    for sim_idx in range(n_sim):
        capital = initial_capital
        monthly = [capital]
        fired = False
        fire_year = None

        for month in range(total_months):
            n_trades = trades_per_month
            for _ in range(n_trades):
                if capital < position_size * 0.5:
                    break  # 資金不足で取引停止
                r = random.random()
                if r < win_rate:
                    capital += net_win
                else:
                    capital += net_loss  # 負の値を加算

            monthly.append(capital)
            if not fired and capital >= target:
                fired = True
                fire_year = (month + 1) / 12
                fire_years_list.append(fire_year)

        if capital < initial_capital * 0.5:
            ruin_count += 1

        all_finals.append(capital)
        if sim_idx < 200:
            sample_paths.append(monthly)

    # パーセンタイル
    all_finals.sort()
    months_arr = list(range(total_months + 1))

    def percentile_path(pct: float) -> list[float]:
        result = []
        for m in months_arr:
            vals = sorted(p[m] for p in sample_paths if m < len(p))
            if not vals:
                result.append(0.0)
                continue
            idx = int(len(vals) * pct)
            idx = min(idx, len(vals) - 1)
            result.append(vals[idx])
        return result

    return {
        "sample_paths": sample_paths,
        "months": months_arr,
        "p10": percentile_path(0.10),
        "p25": percentile_path(0.25),
        "p50": percentile_path(0.50),
        "p75": percentile_path(0.75),
        "p90": percentile_path(0.90),
        "fire_count": len(fire_years_list),
        "fire_rate": len(fire_years_list) / n_sim,
        "fire_years_median": (sorted(fire_years_list)[len(fire_years_list) // 2]
                              if fire_years_list else None),
        "ruin_count": ruin_count,
        "ruin_rate": ruin_count / n_sim,
        "final_median": all_finals[n_sim // 2],
        "final_mean": sum(all_finals) / n_sim,
    }


# ── テキストサマリー ──────────────────────────────────────────────
def print_summary(initial_capitals: list[float], target: float, years: int,
                  n_sim: int, avg_price: float = 2000.0) -> None:
    print("=" * 90)
    print(f"  ロング6戦略 資産形成シミュレーター")
    print(f"  FIRE目標: {target/1e4:.0f}万円  最大年数: {years}年  "
          f"試行回数: {n_sim:,}回/シナリオ")
    print("=" * 90)

    # 戦略別リスクリワード表示
    print("\n【戦略別 1トレード 損益モデル】 (想定株価 2,000円 × 100株)")
    print(f"  {'戦略':<6} {'sm':>4} {'tm':>4} {'勝ち益':>8} {'負け損':>8} "
          f"{'RR比':>6} {'損益分岐WR':>10}")
    print("  " + "-" * 60)
    for name, (em, sm, tm) in STRATEGY_DEFS.items():
        ts = calc_trade_stats(avg_price, em, sm, tm)
        bwr = ts["breakeven_wr"]
        print(f"  {name:<6} {sm:>4.1f} {tm:>4.1f}"
              f" {ts['net_win']:>8,.0f}円 {ts['net_loss']:>8,.0f}円"
              f" {ts['rr']:>6.2f}  {bwr*100:>8.1f}%")

    print("\n  ※ スリッページ+0.5%、手数料往復0.2%込み")
    print(f"  ※ ATR = 終値 × {ATR_RATIO*100:.1f}%（日本株中型株の典型値）")

    # 代表的株価でのコスト内訳
    print("\n【1トレード コスト内訳 (株価2,000円 × 100株)】")
    ts = calc_trade_stats(2000.0, 0.0, 1.5, 3.0)
    print(f"  資金投入額:    {ts['capital_deployed']:>10,.0f}円")
    print(f"  逆指値価格:    {ts['entry_p']:>10,.0f}円/株  (+スリッページ0.5%)")
    print(f"  目標価格:      {ts['target_p']:>10,.0f}円/株  (+ATR×3.0)")
    print(f"  損切り価格:    {ts['stop_p']:>10,.0f}円/株  (-ATR×1.5)")
    print(f"  勝ち時利益:    {ts['net_win']:>10,.0f}円  (コスト後)")
    print(f"  負け時損失:    {ts['net_loss']:>10,.0f}円  (コスト後)")

    # シナリオ × 初期資金 マトリクス
    print("\n" + "=" * 90)
    print("【資産形成シミュレーション】")

    for scenario_key, sp in SCENARIO_PARAMS.items():
        wr = sp["win_rate"]
        tpm = sp["trades_per_month"]
        print(f"\n  ■ {sp['label']}  月{tpm}回取引")

        # 代表的株価の損益期待値
        ts = calc_trade_stats(2000.0, 0.0, 1.5, 3.0)
        ev = calc_ev(ts, wr)
        print(f"    期待値/トレード: {ev:+,.0f}円  月間期待損益: {ev*tpm:+,.0f}円")

        print(f"\n    {'初期資金':>10}  {'FIRE確率':>8}  {'FIRE中央値':>10}  "
              f"{'元本50%割れ':>12}  {'20年後中央値':>12}")
        print("    " + "-" * 72)

        for cap in initial_capitals:
            result = monte_carlo(
                initial_capital=cap,
                target=target,
                years=years,
                win_rate=wr,
                net_win=ts["net_win"],
                net_loss=ts["net_loss"],
                trades_per_month=tpm,
                n_sim=n_sim,
            )
            fire_yr = (f"{result['fire_years_median']:.1f}年"
                       if result["fire_years_median"] else f">{years}年")
            print(f"    {cap/1e4:>8.0f}万円  "
                  f"{result['fire_rate']*100:>7.1f}%  "
                  f"{fire_yr:>10}  "
                  f"{result['ruin_rate']*100:>10.1f}%  "
                  f"{result['final_median']/1e4:>10.0f}万円")

    print("\n" + "=" * 90)
    print("【免責事項】")
    print("  本シミュレーションはバックテスト定数から算出した試算であり、")
    print("  将来の利益を保証するものではありません。")
    print("  実際の勝率・取引頻度は市場環境により大きく変動します。")
    print("=" * 90)


# ── HTML生成 ──────────────────────────────────────────────────────
def build_html(initial_capitals: list[float], target: float, years: int,
               n_sim: int) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d")

    # 全シナリオのシミュレーション実行
    avg_price = 2000.0
    ts_base = calc_trade_stats(avg_price, 0.0, 1.5, 3.0)
    ts_rsi2 = calc_trade_stats(avg_price, 0.0, 2.0, 4.0)

    scenarios_data = {}
    for sk, sp in SCENARIO_PARAMS.items():
        wr = sp["win_rate"]
        results_by_cap = {}
        for cap in initial_capitals:
            result = monte_carlo(
                initial_capital=cap,
                target=target,
                years=years,
                win_rate=wr,
                net_win=ts_base["net_win"],
                net_loss=ts_base["net_loss"],
                trades_per_month=sp["trades_per_month"],
                n_sim=n_sim,
            )
            results_by_cap[cap] = result
        scenarios_data[sk] = {"sp": sp, "results": results_by_cap,
                               "ev": calc_ev(ts_base, wr)}

    # グラフデータ: 代表初期資金・中程度シナリオ
    rep_cap = initial_capitals[len(initial_capitals) // 2]
    rep_result = scenarios_data["moderate"]["results"][rep_cap]
    months = rep_result["months"]
    x_years = [m / 12 for m in months]

    def fmt_path(vals: list[float]) -> str:
        return "[" + ",".join(f"{v/1e4:.1f}" for v in vals) + "]"

    p10_js = fmt_path(rep_result["p10"])
    p25_js = fmt_path(rep_result["p25"])
    p50_js = fmt_path(rep_result["p50"])
    p75_js = fmt_path(rep_result["p75"])
    p90_js = fmt_path(rep_result["p90"])
    x_js   = "[" + ",".join(f"{v:.2f}" for v in x_years) + "]"
    tgt_js = f"{target/1e4:.0f}"

    # 戦略テーブル行
    strat_rows = ""
    for name, (em, sm, tm) in STRATEGY_DEFS.items():
        ts = calc_trade_stats(avg_price, em, sm, tm)
        bwr = ts["breakeven_wr"]
        strat_rows += f"""
        <tr>
          <td class="td-name">{name}</td>
          <td>{sm:.1f}</td><td>{tm:.1f}</td>
          <td class="td-pos">+{ts['net_win']:,.0f}円</td>
          <td class="td-neg">{ts['net_loss']:,.0f}円</td>
          <td>{ts['rr']:.2f}</td>
          <td>{bwr*100:.1f}%</td>
        </tr>"""

    # シナリオ結果テーブル
    scenario_sections = ""
    for sk, sd in scenarios_data.items():
        sp = sd["sp"]
        ev = sd["ev"]
        rows = ""
        for cap in initial_capitals:
            r = sd["results"][cap]
            fire_yr = (f"{r['fire_years_median']:.1f}年"
                       if r["fire_years_median"] else f">{years}年")
            fire_cls = ("td-pos" if r["fire_rate"] >= 0.6
                        else ("td-warn" if r["fire_rate"] >= 0.3 else "td-neg"))
            ruin_cls = ("td-neg" if r["ruin_rate"] >= 0.1 else
                        ("td-warn" if r["ruin_rate"] >= 0.05 else "td-pos"))
            rows += f"""
            <tr>
              <td class="td-name">{cap/1e4:.0f}万円</td>
              <td class="{fire_cls}">{r['fire_rate']*100:.1f}%</td>
              <td>{fire_yr}</td>
              <td class="{ruin_cls}">{r['ruin_rate']*100:.1f}%</td>
              <td>{r['final_median']/1e4:.0f}万円</td>
              <td>{r['final_mean']/1e4:.0f}万円</td>
            </tr>"""

        scenario_sections += f"""
        <div class="scenario-block">
          <h3 class="scenario-title" style="color:{sp['color']}">■ {sp['label']}</h3>
          <p class="scenario-sub">月{sp['trades_per_month']}回取引 ／ 期待値/トレード: <span class="{"pos" if ev >= 0 else "neg"}">{ev:+,.0f}円</span> ／ 月間期待損益: <span class="{"pos" if ev >= 0 else "neg"}">{ev*sp['trades_per_month']:+,.0f}円</span></p>
          <table class="data-table">
            <thead><tr>
              <th>初期資金</th>
              <th>FIRE確率</th>
              <th>FIRE中央値</th>
              <th>元本半減確率</th>
              <th>{years}年後中央値</th>
              <th>{years}年後平均値</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    # FIRE到達確率バー（中程度シナリオ）
    bar_rows = ""
    for cap in initial_capitals:
        r = scenarios_data["moderate"]["results"][cap]
        pct = r["fire_rate"] * 100
        bar_w = min(pct, 100)
        bar_color = ("#22c55e" if pct >= 60 else
                     ("#f59e0b" if pct >= 30 else "#ef4444"))
        fire_yr = (f"{r['fire_years_median']:.1f}年"
                   if r["fire_years_median"] else f">{years}年")
        bar_rows += f"""
        <tr>
          <td class="td-name">{cap/1e4:.0f}万円</td>
          <td><div class="bar-wrap"><div class="bar-fill" style="width:{bar_w:.0f}%;background:{bar_color}"></div></div></td>
          <td style="color:{bar_color}">{pct:.1f}%</td>
          <td>{fire_yr}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>資産形成シミュレーター — {today}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#090b14;color:#dde1ec;font-family:'Segoe UI',sans-serif;font-size:14px;line-height:1.6;padding:20px}}
h1{{font-size:22px;color:#38bdf8;margin-bottom:4px}}
h2{{font-size:17px;color:#94a3b8;margin:28px 0 12px;border-bottom:1px solid #252840;padding-bottom:6px}}
h3.scenario-title{{font-size:15px;margin:18px 0 6px}}
.subtitle{{color:#64748b;font-size:12px;margin-bottom:24px}}
.scenario-sub{{color:#94a3b8;font-size:12px;margin-bottom:10px}}
.pos{{color:#4ade80}} .neg{{color:#f87171}} .td-pos{{color:#4ade80}} .td-neg{{color:#f87171}} .td-warn{{color:#fbbf24}} .td-name{{color:#e2e8f0;font-weight:600}}
.data-table{{width:100%;border-collapse:collapse;margin-bottom:12px}}
.data-table th{{background:#16192a;color:#94a3b8;padding:8px 12px;text-align:center;border:1px solid #252840;font-size:12px}}
.data-table td{{padding:7px 12px;text-align:center;border:1px solid #1e2235}}
.data-table tbody tr:hover{{background:#1a1d2e}}
.scenario-block{{background:#0d1017;border:1px solid #1e2235;border-radius:8px;padding:16px 20px;margin-bottom:20px}}
.info-box{{background:#0d1017;border:1px solid #252840;border-radius:8px;padding:16px 20px;margin-bottom:20px;font-size:13px}}
.info-box p{{color:#94a3b8;margin:3px 0}}
.info-box strong{{color:#e2e8f0}}
.bar-wrap{{background:#1e2235;border-radius:4px;height:18px;width:200px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px;transition:width .3s}}
.chart-wrap{{background:#0d1017;border:1px solid #1e2235;border-radius:8px;padding:16px;margin-bottom:20px}}
.disclaimer{{background:#1a0f0f;border:1px solid #7f1d1d;border-radius:8px;padding:14px 18px;margin-top:24px;color:#fca5a5;font-size:12px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:700px){{.grid2{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>ロング6戦略 資産形成シミュレーター</h1>
<p class="subtitle">生成日: {today} ／ FIRE目標: {target/1e4:.0f}万円 ／ 最大期間: {years}年 ／ 試行回数: {n_sim:,}回/シナリオ</p>

<div class="grid2">
<div class="info-box">
<p><strong>コストモデル (backtest_limit_entry.py 準拠)</strong></p>
<p>スリッページ: 逆指値 ±{SLIPPAGE_STOP_PCT*100:.1f}%</p>
<p>手数料: 片道 {FEE_PCT_ONE_WAY*100:.1f}% → 往復 {FEE_PCT_ONE_WAY*200:.1f}%</p>
<p>ATR想定: 終値の {ATR_RATIO*100:.1f}%（日本株中型株典型値）</p>
<p>取引数量: 固定100株</p>
</div>
<div class="info-box">
<p><strong>シミュレーション前提</strong></p>
<p>対象: MACD / A7 / RSI2 / DON / VOL / MOM（6戦略）</p>
<p>代表株価: 2,000円（WATCHLISTの中央値帯）</p>
<p>同時保有: 最大3ポジション想定</p>
<p>複利運用: なし（固定100株・固定損益）</p>
</div>
</div>

<h2>戦略別 1トレード 損益モデル（株価2,000円×100株）</h2>
<table class="data-table">
<thead><tr>
  <th>戦略</th><th>損切倍率(sm)</th><th>目標倍率(tm)</th>
  <th>勝ち益（コスト後）</th><th>負け損（コスト後）</th>
  <th>リスクリワード比</th><th>損益分岐勝率</th>
</tr></thead>
<tbody>{strat_rows}</tbody>
</table>

<h2>FIRE到達確率（中程度シナリオ: WR=50%）</h2>
<table class="data-table" style="max-width:600px">
<thead><tr><th>初期資金</th><th>FIRE確率バー</th><th>FIRE確率</th><th>FIRE中央値年数</th></tr></thead>
<tbody>{bar_rows}</tbody>
</table>

<h2>資産推移グラフ（中程度シナリオ: WR=50% / 初期{rep_cap/1e4:.0f}万円）</h2>
<div class="chart-wrap">
<canvas id="chart" height="300"></canvas>
</div>

<h2>シナリオ別 詳細シミュレーション</h2>
{scenario_sections}

<div class="disclaimer">
⚠️ <strong>免責事項</strong>: 本シミュレーションはバックテスト定数から算出した試算です。
将来の利益を保証するものではありません。実際の勝率・取引頻度は市場環境により
大きく変動し、元本を割り込む可能性があります。
バックテストの勝率はフォワードテストで10〜30%劣化することがあります。
投資判断は自己責任でお願いします。
</div>

<script>
const xData = {x_js};
const p10 = {p10_js};
const p25 = {p25_js};
const p50 = {p50_js};
const p75 = {p75_js};
const p90 = {p90_js};
const target = {tgt_js};

const ctx = document.getElementById('chart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: xData,
    datasets: [
      {{label:'90%ile',data:p90,borderColor:'#3b82f620',backgroundColor:'#3b82f608',borderWidth:1,pointRadius:0,fill:false}},
      {{label:'75%ile',data:p75,borderColor:'#34d39940',backgroundColor:'#34d39910',borderWidth:1,pointRadius:0,fill:'4'}},
      {{label:'中央値 (50%ile)',data:p50,borderColor:'#34d399',backgroundColor:'transparent',borderWidth:2.5,pointRadius:0,fill:false}},
      {{label:'25%ile',data:p25,borderColor:'#f59e0b40',backgroundColor:'#f59e0b08',borderWidth:1,pointRadius:0,fill:false}},
      {{label:'10%ile',data:p10,borderColor:'#ef444420',backgroundColor:'transparent',borderWidth:1,pointRadius:0,fill:false}},
      {{label:`FIRE目標 {target/1e4:.0f}万円`,data:xData.map(()=>target),borderColor:'#f87171',borderWidth:1.5,borderDash:[6,4],pointRadius:0,fill:false}},
    ]
  }},
  options: {{
    responsive:true,
    plugins:{{
      legend:{{labels:{{color:'#94a3b8',font:{{size:12}}}}}},
      tooltip:{{callbacks:{{label:(c)=>c.dataset.label+': '+c.parsed.y.toFixed(0)+'万円'}}}}
    }},
    scales:{{
      x:{{title:{{display:true,text:'年',color:'#64748b'}},ticks:{{color:'#64748b',callback:(v,i)=>i%12==0?xData[i].toFixed(0)+'年':''}},grid:{{color:'#1e2235'}}}},
      y:{{title:{{display:true,text:'資産(万円)',color:'#64748b'}},ticks:{{color:'#64748b',callback:(v)=>v+'万円'}},grid:{{color:'#1e2235'}}}}
    }}
  }}
}});
</script>
</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="ロング6戦略 資産形成シミュレーター")
    parser.add_argument("--target",      type=float, default=10_000_000,
                        help="FIRE目標金額（円）デフォルト 1000万円")
    parser.add_argument("--initial",     type=float, default=None,
                        help="初期資金（円）を1種類だけ指定する場合")
    parser.add_argument("--years",       type=int,   default=20)
    parser.add_argument("--simulations", type=int,   default=2000)
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--text-only",   action="store_true",
                        help="テキスト表示のみ（HTML生成なし）")
    args = parser.parse_args()

    if args.initial:
        initial_capitals = [args.initial]
    else:
        initial_capitals = [
            500_000,    # 50万
            1_000_000,  # 100万
            2_000_000,  # 200万
            3_000_000,  # 300万
            5_000_000,  # 500万
            10_000_000, # 1000万
        ]

    print_summary(initial_capitals, args.target, args.years, args.simulations)

    if args.text_only:
        return

    print("\nHTML生成中...", flush=True)
    html = build_html(initial_capitals, args.target, args.years, args.simulations)
    today = datetime.now(JST).strftime("%Y-%m-%d")
    out = Path(f"fire_simulation_{today}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML: {out.resolve()}")

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
