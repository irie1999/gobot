"""
daytrade_donchian_walkforward.py  ―  Donchianのウォークフォワード検証
==================================================================
過去N日(訓練期)で勝ち銘柄を抽出 → 次のM日(検証期)でその銘柄群の
パフォーマンスを測定。これを時系列でローリング実施することで、
overfitting / survivorship bias を排除した「本当に効くか」を検証。

【ロジック】
  i = train_days
  while i + test_days <= 営業日数:
    train_dates = all_dates[i - train_days : i]
    test_dates  = all_dates[i : i + test_days]
    ① train_dates で全銘柄バックテスト
    ② PF/取引数/損益で勝ち銘柄を抽出
    ③ test_dates で勝ち銘柄のみバックテスト
    ④ test_dates の取引を集計
    i += test_days

  最終: 全テスト期間の取引を集計してPF/損益/DDを評価

【使い方】
  python daytrade_donchian_walkforward.py --universe n225 --days 60 \\
      --train-days 30 --test-days 15
  python daytrade_donchian_walkforward.py --universe n225 \\
      --target-preset balanced --min-pf 1.3
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats
from daytrade_donchian_compare import PRESETS, _load_universe, extract_winners

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 6_000


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def slice_data_by_dates(fetched, allowed_dates):
    """allowed_dates のセットに含まれる日のデータだけ抽出。"""
    allowed = set(allowed_dates)
    out = {}
    for sym, df in fetched.items():
        mask = [d in allowed for d in df.index.date]
        sub = df[mask]
        if not sub.empty:
            out[sym] = sub
    return out


def run_pass(fetched, name_map, params, budget, max_risk, target_syms=None):
    """指定銘柄(target_syms=None なら全銘柄)でバックテスト → items 返却。"""
    items = []
    for sym, df in fetched.items():
        if target_syms is not None and sym not in target_syms:
            continue
        r = backtest_symbol(sym, name_map.get(sym, sym), df,
                            budget, max_risk, params=params)
        if r:
            items.append(r)
    return items


def run_walkforward(fetched, name_map, params, all_dates,
                    train_days, test_days,
                    budget, max_risk,
                    min_pf, min_trades, min_pnl):
    windows = []
    overall_test_trades = []

    i = train_days
    while i + test_days <= len(all_dates):
        train_dates = all_dates[i - train_days:i]
        test_dates = all_dates[i:i + test_days]

        # ① 訓練期: 全銘柄でバックテスト
        train_data = slice_data_by_dates(fetched, train_dates)
        train_items = run_pass(train_data, name_map, params, budget, max_risk)
        train_result = dict(items=train_items)

        # ② 勝ち銘柄抽出
        winners = extract_winners(train_result, budget, min_pf, min_trades, min_pnl)
        winner_syms = {s for s, _, _ in winners}

        # ③ 検証期: 勝ち銘柄のみでバックテスト
        test_data = slice_data_by_dates(fetched, test_dates)
        test_items = run_pass(test_data, name_map, params, budget, max_risk,
                              target_syms=winner_syms)
        test_trades = [t for it in test_items for t in it["trades"]]
        test_pnl = sum(t["pnl"] for t in test_trades)
        test_n = len(test_trades)
        test_wins = sum(1 for t in test_trades if t["pnl"] > 0)
        test_winrate = (test_wins / test_n * 100) if test_n > 0 else 0

        windows.append(dict(
            train_start=train_dates[0],
            train_end=train_dates[-1],
            test_start=test_dates[0],
            test_end=test_dates[-1],
            n_winners=len(winners),
            n_winners_traded=len(test_items),
            test_trades=test_n,
            test_winrate=test_winrate,
            test_pnl=test_pnl,
            winners=winners,
        ))
        overall_test_trades.extend(test_trades)
        i += test_days

    overall_stats = calc_stats(overall_test_trades, budget)
    return windows, overall_stats, overall_test_trades


def print_walkforward(windows, overall_stats, total_test_days, params, target_preset):
    print(f"\n{'='*110}")
    print(f"  ウォークフォワード検証 [preset={target_preset}]")
    print("=" * 110)
    print(f"{'#':>2} {'Train期間':<26} {'Test期間':<26} "
          f"{'勝銘柄':>6} {'取引':>5} {'勝率':>6} {'損益':>10}")
    print("-" * 110)
    for idx, w in enumerate(windows, 1):
        train_str = f"{w['train_start']}〜{w['train_end']}"
        test_str = f"{w['test_start']}〜{w['test_end']}"
        wr = f"{w['test_winrate']:.1f}%" if w["test_trades"] > 0 else "-"
        print(f"{idx:>2} {train_str:<26} {test_str:<26} "
              f"{w['n_winners']:>4}→{w['n_winners_traded']:<2} "
              f"{w['test_trades']:>5} {wr:>6} {w['test_pnl']:>+10,.0f}")
    print("-" * 110)

    s = overall_stats
    print(f"\n  【全テスト期間集計 ({total_test_days}営業日)】")
    print(f"  取引: {s['n']}")
    print(f"  勝率: {s['win_rate']:.1f}%")
    print(f"  PF:   {_pf(s['pf'])}")
    print(f"  損益: {s['total_pnl']:+,.0f}円")
    print(f"  DD:   {s['max_dd']:+.1f}%")
    print(f"  平均利益: {s['avg_win']:+,.0f}円  /  平均損失: {s['avg_loss']:+,.0f}円")
    print("=" * 110)

    # 勝率/PFの判定
    if s["pf"] >= 1.5:
        print("\n  ✓ ウォークフォワードでも PF ≥ 1.5 を達成。本番運用候補。")
    elif s["pf"] >= 1.0:
        print("\n  △ ウォークフォワードで PF ≥ 1.0 だが余裕は少ない。"
              "実費用(手数料/スプレッド)で負けに転じる可能性あり。")
    else:
        print("\n  ✗ ウォークフォワードで PF < 1.0。"
              "過去結果は overfitting だった可能性が高い。")


def build_html(windows, overall_stats, total_test_days, target_preset,
               params, days, budget, source, universe,
               train_days, test_days, min_pf, min_trades, min_pnl):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    s = overall_stats
    pf_str = _pf(s["pf"])

    if s["pf"] >= 1.5:
        verdict = '<div class="verdict ok">✓ ウォークフォワードでも PF ≥ 1.5 を達成。本番運用候補。</div>'
    elif s["pf"] >= 1.0:
        verdict = ('<div class="verdict warn">△ PF ≥ 1.0 だがマージン少。'
                   '実費用(手数料/スプレッド)で負けに転じる可能性あり。</div>')
    else:
        verdict = ('<div class="verdict ng">✗ PF &lt; 1.0。過去結果は overfitting '
                   'だった可能性が高い。別の選別軸が必要。</div>')

    # ウィンドウ別テーブル
    win_rows = ""
    for idx, w in enumerate(windows, 1):
        cls = "profit" if w["test_pnl"] >= 0 else "loss"
        wr = f"{w['test_winrate']:.1f}%" if w["test_trades"] > 0 else "-"
        win_rows += f"""<tr>
          <td>{idx}</td>
          <td>{w['train_start']}〜{w['train_end']}</td>
          <td>{w['test_start']}〜{w['test_end']}</td>
          <td>{w['n_winners']}→{w['n_winners_traded']}</td>
          <td>{w['test_trades']}</td>
          <td>{wr}</td>
          <td class="{cls}">{w['test_pnl']:+,.0f}</td></tr>"""

    # 各ウィンドウの抽出銘柄詳細
    winner_sections = ""
    for idx, w in enumerate(windows, 1):
        sym_rows = ""
        for sym, name, ist in w["winners"]:
            pf2 = "∞" if ist["pf"] == float("inf") else f"{ist['pf']:.2f}"
            cls = "profit" if ist["total_pnl"] >= 0 else "loss"
            sym_rows += f"""<tr>
              <td class="sym">{name}<br><small class="code">{sym}</small></td>
              <td>{ist['n']}</td><td>{ist['win_rate']:.0f}%</td>
              <td>{pf2}</td>
              <td class="{cls}">{ist['total_pnl']:+,.0f}</td></tr>"""
        winner_sections += f"""
        <h3>Window #{idx}: 訓練期 {w['train_start']}〜{w['train_end']} の抽出銘柄
            ({len(w['winners'])}銘柄)</h3>
        <table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th>
        <th>PF</th><th>損益(訓練期)</th></tr></thead>
        <tbody>{sym_rows}</tbody></table>"""

    # パラメータ表示
    trail_str = " / ".join(f"+{t[0]*100:.0f}%→{t[1]:.1f}R" for t in params["trail_steps"])
    param_html = f"""
    <table>
    <tr><th>preset</th><td>{target_preset}</td></tr>
    <tr><th>DON_PERIOD</th><td>{params['don_period']}</td></tr>
    <tr><th>R:R</th><td>{params['target_r']}</td></tr>
    <tr><th>GAP上限</th><td>{params['gap_max_pct']}%</td></tr>
    <tr><th>STOP上限</th><td>{params['stop_max_pct']}%</td></tr>
    <tr><th>ENTRY_CUTOFF</th><td>{params['entry_cutoff'].strftime('%H:%M')}</td></tr>
    <tr><th>WARMUP</th><td>{params['warmup']}</td></tr>
    <tr><th>トレーリング</th><td>{trail_str}</td></tr>
    </table>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Donchian ウォークフォワード — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.15rem;border-left:4px solid #10b981;padding-left:10px}}
h3{{color:#a7f3d0;margin:16px 0 6px;font-size:.95rem}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.85rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:160px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.summary{{background:#1e293b;border-radius:8px;padding:14px;margin:10px 0;display:flex;gap:20px;flex-wrap:wrap}}
.summary .item{{text-align:center;min-width:100px}}
.summary .lb{{color:#94a3b8;font-size:.75rem}}
.summary .vl{{font-size:1.4rem;font-weight:700}}
.verdict{{padding:14px;border-radius:8px;margin:14px 0;font-size:.95rem;font-weight:600}}
.verdict.ok{{background:#1e3a2f;border:1px solid #10b981;color:#a7f3d0}}
.verdict.warn{{background:#3a2a1e;border:1px solid #f59e0b;color:#fde68a}}
.verdict.ng{{background:#3a1e1e;border:1px solid #f87171;color:#fca5a5}}
</style></head><body>
<h1>Donchian ウォークフォワード検証</h1>
<p class="sub">生成:{today} / source:{source} / universe:{universe} /
 全期間:{days}日 / 訓練:{train_days}日 / 検証:{test_days}日 / 予算:{budget:,}円<br>
 抽出基準: PF≥{min_pf} & 取引≥{min_trades} & 損益≥{min_pnl:,} /
 windows:{len(windows)} / 全テスト:{total_test_days}営業日</p>

{verdict}

<h2>全テスト期間 集計</h2>
<div class="summary">
  <div class="item"><div class="lb">取引</div>
    <div class="vl">{s['n']}</div></div>
  <div class="item"><div class="lb">勝率</div>
    <div class="vl">{s['win_rate']:.1f}%</div></div>
  <div class="item"><div class="lb">PF</div>
    <div class="vl">{pf_str}</div></div>
  <div class="item"><div class="lb">損益</div>
    <div class="vl {('profit' if s['total_pnl']>=0 else 'loss')}">{s['total_pnl']:+,.0f}円</div></div>
  <div class="item"><div class="lb">DD</div>
    <div class="vl loss">{s['max_dd']:+.1f}%</div></div>
  <div class="item"><div class="lb">avg勝</div>
    <div class="vl profit">{s['avg_win']:+,.0f}</div></div>
  <div class="item"><div class="lb">avg負</div>
    <div class="vl loss">{s['avg_loss']:+,.0f}</div></div>
</div>

<h2>使用パラメータ</h2>
{param_html}

<h2>ウィンドウ別 テスト結果</h2>
<table><thead><tr><th>#</th><th>訓練期</th><th>検証期</th>
<th>勝銘柄→取引銘柄</th><th>取引</th><th>勝率</th>
<th>損益(検証期)</th></tr></thead>
<tbody>{win_rows}</tbody></table>

<h2>ウィンドウ別 抽出銘柄(訓練期で勝った銘柄)</h2>
{winner_sections}
</body></html>"""


def build_summary_html(all_results, days, budget, source, universe,
                       train_days, test_days, min_pf, min_trades, min_pnl):
    """全プリセット横断のサマリHTML。"""
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # サマリテーブル
    rows = ""
    for pname, params, windows, stats, _, total_test_days in all_results:
        s = stats
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        if s["pf"] >= 1.5:
            verdict = '<span class="profit">✓ 本番候補</span>'
        elif s["pf"] >= 1.0:
            verdict = '<span style="color:#f59e0b">△ マージン少</span>'
        else:
            verdict = '<span class="loss">✗ overfit</span>'
        rows += f"""<tr>
          <td class="sym">{pname}</td>
          <td>{s['n']}</td><td>{s['win_rate']:.1f}%</td>
          <td>{_pf(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.1f}%</td>
          <td>{len(windows)} window / {total_test_days}日</td>
          <td>{verdict}</td></tr>"""

    # 各プリセットの window 詳細
    detail = ""
    for pname, params, windows, stats, _, total_test_days in all_results:
        win_rows = ""
        for idx, w in enumerate(windows, 1):
            cls = "profit" if w["test_pnl"] >= 0 else "loss"
            wr = f"{w['test_winrate']:.1f}%" if w["test_trades"] > 0 else "-"
            win_rows += f"""<tr>
              <td>{idx}</td>
              <td>{w['train_start']}〜{w['train_end']}</td>
              <td>{w['test_start']}〜{w['test_end']}</td>
              <td>{w['n_winners']}→{w['n_winners_traded']}</td>
              <td>{w['test_trades']}</td>
              <td>{wr}</td>
              <td class="{cls}">{w['test_pnl']:+,.0f}</td></tr>"""
        detail += f"""
        <h3>{pname}</h3>
        <table><thead><tr><th>#</th><th>訓練期</th><th>検証期</th>
        <th>勝銘柄→取引</th><th>取引</th><th>勝率</th>
        <th>損益(検証期)</th></tr></thead>
        <tbody>{win_rows}</tbody></table>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Donchian ウォークフォワード 全プリセット — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.15rem;border-left:4px solid #10b981;padding-left:10px}}
h3{{color:#a7f3d0;margin:16px 0 6px;font-size:.95rem}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.85rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:140px}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
</style></head><body>
<h1>Donchian ウォークフォワード - 全プリセット横断検証</h1>
<p class="sub">生成:{today} / source:{source} / universe:{universe} /
 全期間:{days}日 / 訓練:{train_days}日 / 検証:{test_days}日 / 予算:{budget:,}円<br>
 抽出基準: PF≥{min_pf} & 取引≥{min_trades} & 損益≥{min_pnl:,}</p>

<h2>プリセット横断サマリ</h2>
<table><thead><tr><th>preset</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益(検証期合計)</th><th>DD</th><th>window/日数</th><th>判定</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>プリセット別 ウィンドウ詳細</h2>
{detail}
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Donchian ウォークフォワード検証")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--universe", choices=["watch", "n225"], default="n225")
    parser.add_argument("--target-preset", default="ultra_freq",
                        choices=list(PRESETS.keys()) + ["all"],
                        help="検証対象プリセット (all で全4プリセット一括実行)")
    parser.add_argument("--train-days", type=int, default=30,
                        help="訓練期間(日数, デフォルト: 30)")
    parser.add_argument("--test-days", type=int, default=15,
                        help="検証期間(日数, デフォルト: 15)")
    parser.add_argument("--min-pf", type=float, default=1.5)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--min-pnl", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true",
                        help="HTML を自動でブラウザ表示しない")
    args = parser.parse_args()

    targets = _load_universe(args.universe)
    symbols = [s for s, _ in targets]
    preset_label = "all (4プリセット一括)" if args.target_preset == "all" else args.target_preset
    print(f"ウォークフォワード検証 [universe={args.universe} / "
          f"preset={preset_label} / "
          f"train={args.train_days}日 / test={args.test_days}日]: "
          f"{len(targets)}銘柄 / {args.days}日", flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    name_map = dict(targets)
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    all_dates = sorted({d for df in fetched.values() for d in df.index.date})
    print(f"  営業日: {len(all_dates)}日 ({all_dates[0]} 〜 {all_dates[-1]})", flush=True)

    if len(all_dates) < args.train_days + args.test_days:
        raise SystemExit(
            f"営業日が不足: {len(all_dates)}日 < "
            f"train({args.train_days}) + test({args.test_days})"
        )

    print(f"\n  抽出基準: PF≥{args.min_pf} / 取引≥{args.min_trades} / 損益≥{args.min_pnl:,}\n",
          flush=True)

    preset_names = list(PRESETS.keys()) if args.target_preset == "all" else [args.target_preset]

    all_results = []  # list of (preset_name, params, windows, stats, trades)
    for pname in preset_names:
        params = PRESETS[pname]
        print(f"\n=== プリセット [{pname}] 検証中 ===", flush=True)
        windows, overall_stats, test_trades = run_walkforward(
            fetched, name_map, params, all_dates,
            args.train_days, args.test_days,
            args.budget, MAX_RISK,
            args.min_pf, args.min_trades, args.min_pnl,
        )
        total_test_days = sum(args.test_days for _ in windows)
        print_walkforward(windows, overall_stats, total_test_days, params, pname)
        all_results.append((pname, params, windows, overall_stats, test_trades, total_test_days))

    # 全プリセット集計 (all モード時)
    if len(all_results) > 1:
        print(f"\n{'='*100}")
        print(f"  ウォークフォワード プリセット横断比較")
        print("=" * 100)
        print(f"{'preset':<14} {'取引':>5} {'勝率':>6} {'PF':>7} "
              f"{'損益':>11} {'DD':>7} {'判定':>10}")
        print("-" * 100)
        for pname, _, _, stats, _, _ in all_results:
            verdict = "✓本番候補" if stats["pf"] >= 1.5 \
                      else "△マージン少" if stats["pf"] >= 1.0 \
                      else "✗ overfit"
            print(f"{pname:<14} {stats['n']:>5} {stats['win_rate']:>5.1f}% "
                  f"{_pf(stats['pf']):>7} {stats['total_pnl']:>+11,.0f} "
                  f"{stats['max_dd']:>+6.1f}% {verdict:>10}")
        print("=" * 100)

    # HTML出力 (プリセットごとに1ファイル)
    stamp = datetime.now(JST).strftime('%Y%m%d_%H%M')
    out_paths = []
    for pname, params, windows, overall_stats, _, total_test_days in all_results:
        out = Path(f"daytrade_donchian_walkforward_{args.universe}_{pname}_{stamp}.html")
        out.write_text(build_html(
            windows, overall_stats, total_test_days, pname,
            params, args.days, args.budget, args.source, args.universe,
            args.train_days, args.test_days,
            args.min_pf, args.min_trades, args.min_pnl,
        ), encoding="utf-8")
        out_paths.append(out)
        print(f"\nHTML [{pname}]: {out.resolve()}")

    # all モード: 横断比較用のサマリHTMLも作成
    if len(all_results) > 1:
        out_summary = Path(f"daytrade_donchian_walkforward_{args.universe}_all_{stamp}.html")
        out_summary.write_text(build_summary_html(
            all_results, args.days, args.budget, args.source, args.universe,
            args.train_days, args.test_days,
            args.min_pf, args.min_trades, args.min_pnl,
        ), encoding="utf-8")
        print(f"\nHTML [all_summary]: {out_summary.resolve()}")
        if not args.no_browser:
            webbrowser.open(out_summary.resolve().as_uri())
    else:
        if not args.no_browser and out_paths:
            webbrowser.open(out_paths[0].resolve().as_uri())


if __name__ == "__main__":
    main()
