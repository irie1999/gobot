"""
daytrade_donchian_compare.py  ―  Donchian の複数プリセット比較
==================================================================
同一データ・同一銘柄ユニバースに対して、Donchian の複数パラメータ
プリセット (高頻度/バランス/保守 など) を一括バックテストし、
取引頻度と PF のトレードオフを可視化する。

【プリセット】
  high_freq     : DON_PERIOD=10  ENTRY_CUTOFF=14:00 R:R=1.5  WARMUP=10
  balanced      : DON_PERIOD=15  ENTRY_CUTOFF=13:00 R:R=1.8  WARMUP=15
  conservative  : DON_PERIOD=20  ENTRY_CUTOFF=11:00 R:R=2.0  WARMUP=20
  ultra_freq    : DON_PERIOD=8   ENTRY_CUTOFF=14:30 R:R=1.3  WARMUP=8

【使い方】
  python daytrade_donchian_compare.py --universe n225 --days 60
  python daytrade_donchian_compare.py --universe watch --days 60
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 3_000   # 1取引あたり最大損失 (DD抑制のため 6k→3k に縮小)


# ─────────────────────────────────────────────────────────────
# プリセット定義
# ─────────────────────────────────────────────────────────────

PRESETS = {
    "high_freq": dict(
        don_period=10,
        target_r=1.5,
        gap_max_pct=3.0,
        stop_max_pct=2.5,
        trail_steps=[(0.33, 0.0), (0.67, 0.3)],
        force_close=dtime(14, 55),
        entry_cutoff=dtime(14, 0),
        warmup=10,
    ),
    "balanced": dict(
        don_period=15,
        target_r=1.8,
        gap_max_pct=2.5,
        stop_max_pct=2.5,
        trail_steps=[(0.30, 0.0), (0.60, 0.3), (0.85, 0.6)],
        force_close=dtime(14, 55),
        entry_cutoff=dtime(13, 0),
        warmup=15,
    ),
    "conservative": dict(
        don_period=20,
        target_r=2.0,
        gap_max_pct=2.0,
        stop_max_pct=2.5,
        trail_steps=[(0.25, 0.0), (0.50, 0.3), (0.75, 0.7)],
        force_close=dtime(14, 55),
        entry_cutoff=dtime(11, 0),
        warmup=20,
    ),
    # ★ メイン採用 (ultra_freq) ★
    # walk-forward 4ウィンドウ検証で唯一 PF > 1.0 を維持
    "ultra_freq": dict(
        don_period=8,
        target_r=1.3,
        gap_max_pct=4.0,
        stop_max_pct=2.0,
        trail_steps=[(0.40, 0.0), (0.75, 0.3)],
        force_close=dtime(14, 55),
        entry_cutoff=dtime(14, 30),
        warmup=8,
    ),
    # ultra_freq_v2: avg勝が小さい問題を改善
    #   TARGET_R 1.3→2.0 で利幅拡大
    #   trail_steps [(0.50, 0.2), (0.80, 0.5)] で早期建値撤退を抑制
    #   → 反転時も最低+200〜500ロック、目標到達で+2.0R確保
    "ultra_freq_v2": dict(
        don_period=8,
        target_r=2.0,
        gap_max_pct=4.0,
        stop_max_pct=2.0,
        trail_steps=[(0.50, 0.2), (0.80, 0.5)],
        force_close=dtime(14, 55),
        entry_cutoff=dtime(14, 30),
        warmup=8,
    ),
}


def _load_universe(name):
    if name == "watch":
        return DAYTRADE_SYMBOLS
    if name == "n225":
        from symbols_all import SYMBOLS
        return SYMBOLS
    if name == "winners":
        try:
            from daytrade_donchian_winners import SYMBOLS
            return SYMBOLS
        except ImportError as e:
            raise SystemExit(
                "daytrade_donchian_winners.py が存在しません。\n"
                "先に --extract-winners で生成してください:\n"
                "  python daytrade_donchian_compare.py --universe n225 --extract-winners"
            ) from e
    raise ValueError(f"unknown universe: {name}")


def extract_winners(target_result, budget, min_pf, min_trades, min_pnl):
    """指定プリセットの銘柄別結果から勝てている銘柄を抽出。"""
    winners = []
    for it in target_result["items"]:
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], budget)
        if (ist["n"] >= min_trades and
                ist["pf"] >= min_pf and
                ist["total_pnl"] >= min_pnl):
            winners.append((it["symbol"], it["name"], ist))
    winners.sort(key=lambda x: -x[2]["total_pnl"])
    return winners


def write_winners_file(winners, target_preset, params, out_path):
    """勝ち銘柄を Python リスト形式で書き出し。"""
    lines = [
        '"""',
        f'Donchian 高頻度プリセット ({target_preset}) で勝てた銘柄リスト',
        '',
        '抽出条件: 各銘柄バックテスト結果から PF/取引数/損益で絞り込み',
        '※ 過去の結果に基づくため、将来も同様にワークするとは限りません',
        '   (overfitting/survivorship bias に注意)',
        '"""',
        '',
        'import datetime',
        '',
        f'PRESET_NAME = {target_preset!r}',
        f'PRESET_PARAMS = dict(',
    ]
    for k, v in params.items():
        lines.append(f'    {k}={v!r},')
    lines.append(')')
    lines.append('')
    lines.append('SYMBOLS = [')
    for sym, name, ist in winners:
        pf_str = "inf" if ist["pf"] == float("inf") else f'{ist["pf"]:.2f}'
        lines.append(
            f'    ({sym!r}, {name!r}),  '
            f'# PF={pf_str} trades={ist["n"]} pnl={ist["total_pnl"]:+,.0f} '
            f'win={ist["win_rate"]:.0f}%'
        )
    lines.append(']')
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _t_str(t):
    return f"{t.hour:02d}:{t.minute:02d}"


def _fmt_dt(dt):
    """datetime → 'MM-DD HH:MM' 文字列。"""
    if hasattr(dt, "strftime"):
        return dt.strftime("%m-%d %H:%M")
    return str(dt)[:16] if dt else "-"


# ─────────────────────────────────────────────────────────────
# 同時保有数制限フィルタ (実取引のbot設定と合わせるため)
# ─────────────────────────────────────────────────────────────

def apply_concurrent_limit(result, max_concurrent, budget):
    """
    バックテスト結果に同時保有数制限を適用。
    全trades をエントリー時刻順にソートし、保有中ポジ数 < max_concurrent
    のときだけ採用。それ以外はスキップ(現実的な約定能力をシミュレート)。
    """
    # 全trades に銘柄情報を付与
    all_trades = []
    for it in result["items"]:
        for t in it["trades"]:
            tt = dict(t)
            tt["__symbol__"] = it["symbol"]
            tt["__name__"] = it["name"]
            all_trades.append(tt)

    all_trades.sort(key=lambda t: str(t.get("entry_dt", "")))

    accepted = []
    open_exits = []  # 現在オープン中のポジの exit_dt リスト

    for t in all_trades:
        entry_dt = t.get("entry_dt")
        if entry_dt is None:
            continue
        # entry より前に exit したポジを open_exits から外す
        open_exits = [e for e in open_exits if e is not None and e > entry_dt]
        if len(open_exits) < max_concurrent:
            accepted.append(t)
            exit_dt = t.get("exit_dt")
            if exit_dt is not None:
                open_exits.append(exit_dt)

    # items を再構築
    items_by_sym = {}
    for t in accepted:
        sym = t["__symbol__"]
        if sym not in items_by_sym:
            items_by_sym[sym] = {
                "symbol": sym,
                "name": t["__name__"],
                "trades": [],
            }
        clean_t = {k: v for k, v in t.items() if not k.startswith("__")}
        items_by_sym[sym]["trades"].append(clean_t)
    new_items = list(items_by_sym.values())

    # stats 再計算
    clean_trades = [{k: v for k, v in t.items() if not k.startswith("__")}
                    for t in accepted]
    new_stats = calc_stats(clean_trades, budget)

    trade_dates = set()
    for t in clean_trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date"):
            trade_dates.add(dt.date())

    return dict(
        name=result["name"],
        params=result["params"],
        items=new_items,
        stats=new_stats,
        all_trades=clean_trades,
        trade_days=len(trade_dates),
    )


# ─────────────────────────────────────────────────────────────
# 実行
# ─────────────────────────────────────────────────────────────

def run_preset(preset_name, params, fetched, targets, budget, max_risk):
    items = []
    for sym, sname in targets:
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, sname, fetched[sym], budget, max_risk,
                           params=params)
        if r:
            items.append(r)
    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt", "")))
    stats = calc_stats(all_trades, budget)
    trade_dates = set()
    for t in all_trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date"):
            trade_dates.add(dt.date())
    return dict(name=preset_name, params=params, items=items,
                stats=stats, all_trades=all_trades,
                trade_days=len(trade_dates))


def print_param_table(preset_results):
    print(f"\n{'='*100}")
    print(f"  Donchian プリセット パラメータ")
    print("=" * 100)
    print(f"{'プリセット':<14} {'DON':>4} {'R:R':>5} {'GAP':>5} "
          f"{'STOP%':>6} {'CUT':>6} {'WUP':>4} {'TRAIL':>20}")
    print("-" * 100)
    for r in preset_results:
        p = r["params"]
        trail = "/".join(f"{t[0]:.2f}@{t[1]:.1f}" for t in p["trail_steps"])
        print(f"{r['name']:<14} {p['don_period']:>4} {p['target_r']:>5.1f} "
              f"{p['gap_max_pct']:>4.1f}% {p['stop_max_pct']:>5.1f}% "
              f"{_t_str(p['entry_cutoff']):>6} {p['warmup']:>4} {trail:>20}")
    print("=" * 100)


def print_comparison(preset_results, total_days):
    print(f"\n{'='*100}")
    print(f"  Donchian プリセット比較 ({total_days}営業日)")
    print("=" * 100)
    print(f"{'プリセット':<14} {'取引':>5} {'勝率':>6} {'PF':>7} "
          f"{'損益':>11} {'DD':>7} {'取引日':>5} {'日率':>5} {'avg勝':>8} {'avg負':>8}")
    print("-" * 100)
    for r in preset_results:
        s = r["stats"]
        day_rate = f"{r['trade_days']/total_days*100:.0f}%" if total_days > 0 else "-"
        print(f"{r['name']:<14} {s['n']:>5} {s['win_rate']:>5.1f}% "
              f"{_pf(s['pf']):>7} {s['total_pnl']:>+11,.0f} "
              f"{s['max_dd']:>+6.1f}% {r['trade_days']:>5} {day_rate:>5} "
              f"{s['avg_win']:>+8,.0f} {s['avg_loss']:>+8,.0f}")
    print("=" * 100)

    if preset_results:
        best_pnl = max(preset_results, key=lambda x: x["stats"]["total_pnl"])
        best_pf = max(preset_results, key=lambda x: x["stats"]["pf"]
                      if x["stats"]["pf"] < 100 else 0)
        most_freq = max(preset_results, key=lambda x: x["trade_days"])
        pnl_label = "最高利益" if best_pnl["stats"]["total_pnl"] >= 0 else "最少損失"
        print(f"\n  {pnl_label}: {best_pnl['name']} ({best_pnl['stats']['total_pnl']:+,.0f}円)")
        print(f"  最高PF:   {best_pf['name']} (PF {_pf(best_pf['stats']['pf'])})")
        print(f"  最高頻度: {most_freq['name']} ({most_freq['trade_days']}/{total_days}日)")


# ─────────────────────────────────────────────────────────────
# HTML出力
# ─────────────────────────────────────────────────────────────

def build_html(preset_results, total_days, days, budget, source, universe):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # パラメータテーブル
    param_rows = ""
    for r in preset_results:
        p = r["params"]
        trail = "<br>".join(f"+{t[0]*100:.0f}%→{t[1]:.1f}R" for t in p["trail_steps"])
        param_rows += f"""<tr>
          <td class="sym">{r['name']}</td>
          <td>{p['don_period']}</td>
          <td>{p['target_r']:.1f}</td>
          <td>{p['gap_max_pct']:.1f}%</td>
          <td>{p['stop_max_pct']:.1f}%</td>
          <td>{_t_str(p['entry_cutoff'])}</td>
          <td>{p['warmup']}</td>
          <td>{trail}</td></tr>"""

    # 比較サマリ
    comp_rows = ""
    for r in preset_results:
        s = r["stats"]
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        day_rate = f"{r['trade_days']/total_days*100:.0f}%" if total_days > 0 else "-"
        comp_rows += f"""<tr>
          <td class="sym">{r['name']}</td>
          <td>{s['n']}</td><td>{s['win_rate']:.1f}%</td>
          <td>{_pf(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.1f}%</td>
          <td>{r['trade_days']}/{total_days}</td>
          <td>{day_rate}</td>
          <td class="profit">{s['avg_win']:+,.0f}</td>
          <td class="loss">{s['avg_loss']:+,.0f}</td></tr>"""

    # 銘柄別 (損益最良プリセットのみ)
    detail_html = ""
    if preset_results:
        best = max(preset_results, key=lambda x: x["stats"]["total_pnl"])
        best_label = "最高利益" if best["stats"]["total_pnl"] >= 0 else "最少損失"
        rows = []
        for it in best["items"]:
            if not it["trades"]:
                continue
            ist = calc_stats(it["trades"], budget)
            rows.append((it["name"], it["symbol"], ist))
        rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
        sym_rows = ""
        for name, sym, ist in rows[:30]:
            c = "profit" if ist["total_pnl"] >= 0 else "loss"
            sym_rows += f"""<tr>
              <td class="sym">{name}<br><small class="code">{sym}</small></td>
              <td>{ist['n']}</td><td>{ist['win_rate']:.0f}%</td>
              <td>{_pf(ist['pf'])}</td>
              <td class="{c}">{ist['total_pnl']:+,.0f}</td>
              <td class="profit">{ist['avg_win']:+,.0f}</td>
              <td class="loss">{ist['avg_loss']:+,.0f}</td>
              <td class="loss">{ist['max_dd']:+.1f}%</td></tr>"""
        detail_html = f"""
        <h2>銘柄別 ({best_label}プリセット: {best['name']}) — TOP30</h2>
        <table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
        <th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
        <tbody>{sym_rows}</tbody></table>"""

    # 個別トレード明細 (各プリセット、時系列順)
    trade_html = ""
    for r in preset_results:
        all_trades = []
        for it in r["items"]:
            for t in it["trades"]:
                tt = dict(t)
                tt["symbol"] = it["symbol"]
                tt["name"] = it["name"]
                all_trades.append(tt)
        if not all_trades:
            continue
        all_trades.sort(key=lambda x: str(x.get("entry_dt", "")))

        rows = ""
        for t in all_trades:
            pc = "profit" if t["pnl"] > 0 else "loss"
            ed = _fmt_dt(t.get("entry_dt"))
            xd = _fmt_dt(t.get("exit_dt"))
            try:
                delta = t["exit_dt"] - t["entry_dt"]
                hold = f"{int(delta.total_seconds() // 60)}分"
            except Exception:
                hold = "-"
            rows += f"""<tr>
              <td class="sym">{t['name']}<br><small class="code">{t['symbol']}.T</small></td>
              <td>{ed}</td><td>{xd}</td><td>{hold}</td>
              <td>{t['entry_p']:,.1f}</td>
              <td class="loss">{t['stop_p']:,.1f}</td>
              <td class="profit">{t['target_p']:,.1f}</td>
              <td>{t['exit_p']:,.1f}</td>
              <td>{t['qty']:,}</td>
              <td class="{pc}">{t['pnl']:+,.0f}</td>
              <td class="{pc}">{t['pct']:+.2f}%</td>
              <td>{t['reason']}</td></tr>"""

        trade_html += f"""
        <details>
        <summary><h2 style="display:inline">{r['name']} 個別トレード明細
            ({len(all_trades)}件 / 時系列順) ▼</h2></summary>
        <table><thead><tr>
        <th>銘柄</th><th>Entry</th><th>Exit</th><th>保有</th>
        <th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
        <th>株数</th><th>損益</th><th>%</th><th>決済理由</th>
        </tr></thead><tbody>{rows}</tbody></table>
        </details>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Donchian プリセット比較 — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.15rem;border-left:4px solid #10b981;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:100px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
</style></head><body>
<h1>Donchian プリセット比較</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円 /
 universe:{universe} / {total_days}営業日</p>
<h2>プリセット パラメータ</h2>
<table><thead><tr><th>プリセット</th><th>DON_PERIOD</th><th>R:R</th>
<th>GAP上限</th><th>STOP上限</th><th>ENTRY_CUTOFF</th><th>WARMUP</th>
<th>トレーリング</th></tr></thead>
<tbody>{param_rows}</tbody></table>
<h2>パフォーマンス比較</h2>
<table><thead><tr><th>プリセット</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>DD</th><th>取引日</th><th>日率</th>
<th>平均利益</th><th>平均損失</th></tr></thead>
<tbody>{comp_rows}</tbody></table>
{detail_html}
{trade_html}
</body></html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Donchian プリセット比較")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--universe", choices=["watch", "n225", "winners"], default="watch",
                        help="watch / n225 / winners (要 --extract-winners で先に生成)")
    parser.add_argument("--presets", default="all",
                        help="カンマ区切りでプリセット指定 (例: high_freq,balanced) / "
                             "'all' で全プリセット (デフォルト)")
    parser.add_argument("--extract-winners", action="store_true",
                        help="ターゲットプリセットの結果から勝ち銘柄を抽出し "
                             "daytrade_donchian_winners.py に保存")
    parser.add_argument("--target-preset", default="ultra_freq",
                        choices=list(PRESETS.keys()),
                        help="勝ち銘柄抽出の対象プリセット (デフォルト: ultra_freq)")
    parser.add_argument("--min-pf", type=float, default=1.5,
                        help="勝ち銘柄の最低PF (デフォルト: 1.5)")
    parser.add_argument("--min-trades", type=int, default=10,
                        help="勝ち銘柄の最低取引数 (デフォルト: 10)")
    parser.add_argument("--min-pnl", type=int, default=0,
                        help="勝ち銘柄の最低損益 (デフォルト: 0)")
    parser.add_argument("--max-concurrent", type=int, default=0,
                        help="同時保有銘柄数の上限 (0=制限なし、実botと合わせる場合は1)")
    parser.add_argument("--max-risk", type=int, default=MAX_RISK,
                        help=f"1取引あたり最大損失 (デフォルト: {MAX_RISK}, "
                             "実botと合わせる場合は1000)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # プリセット選択
    if args.presets == "all":
        preset_names = list(PRESETS.keys())
    else:
        preset_names = [n.strip() for n in args.presets.split(",")]
        for n in preset_names:
            if n not in PRESETS:
                raise SystemExit(f"unknown preset: {n}. Valid: {list(PRESETS.keys())}")

    targets = _load_universe(args.universe)
    symbols = [s for s, _ in targets]
    print(f"Donchian プリセット比較 [universe={args.universe}]: "
          f"{len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円 / "
          f"presets={','.join(preset_names)}",
          flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄\n", flush=True)

    all_dates = set()
    for df in fetched.values():
        all_dates.update(df.index.date)
    total_days = len(all_dates)

    preset_results = []
    for pname in preset_names:
        params = PRESETS[pname]
        print(f"[{pname}] 実行中...", flush=True)
        r = run_preset(pname, params, fetched, targets, args.budget, args.max_risk)
        s = r["stats"]
        print(f"  → 取引:{s['n']}  勝率:{s['win_rate']:.1f}%  "
              f"PF:{_pf(s['pf'])}  損益:{s['total_pnl']:+,.0f}  "
              f"DD:{s['max_dd']:+.1f}%  取引日:{r['trade_days']}/{total_days}")
        preset_results.append(r)

    # 同時保有数制限の適用 (--max-concurrent N で実botと合わせる)
    if args.max_concurrent > 0:
        print(f"\n[同時保有数制限を適用: max_concurrent={args.max_concurrent}]",
              flush=True)
        for i, r in enumerate(preset_results):
            before_n = r["stats"]["n"]
            before_pnl = r["stats"]["total_pnl"]
            preset_results[i] = apply_concurrent_limit(r, args.max_concurrent, args.budget)
            after_n = preset_results[i]["stats"]["n"]
            after_pnl = preset_results[i]["stats"]["total_pnl"]
            print(f"  {r['name']:<14} 取引:{before_n}→{after_n}  "
                  f"損益:{before_pnl:+,.0f}→{after_pnl:+,.0f}",
                  flush=True)

    print_param_table(preset_results)
    print_comparison(preset_results, total_days)

    # 勝ち銘柄抽出
    if args.extract_winners:
        target_result = next((r for r in preset_results if r["name"] == args.target_preset), None)
        if target_result is None:
            print(f"\n[warn] target preset '{args.target_preset}' was not run; "
                  f"勝ち銘柄抽出をスキップ", flush=True)
        else:
            winners = extract_winners(target_result, args.budget,
                                      args.min_pf, args.min_trades, args.min_pnl)
            print(f"\n{'='*78}")
            print(f"  勝ち銘柄抽出 [{args.target_preset}] "
                  f"PF≥{args.min_pf} & 取引≥{args.min_trades} & 損益≥{args.min_pnl:,}")
            print("=" * 78)
            print(f"{'銘柄':<22} {'コード':<8} {'取引':>4} {'勝率':>5} "
                  f"{'PF':>7} {'損益':>10} {'DD':>6}")
            print("-" * 78)
            total_pnl = 0
            for sym, name, ist in winners:
                disp = name[:20] if len(name) <= 20 else name[:19] + "…"
                pf_str = "∞" if ist["pf"] == float("inf") else f'{ist["pf"]:.2f}'
                print(f"{disp:<22} {sym:<8} {ist['n']:>4} {ist['win_rate']:>4.0f}% "
                      f"{pf_str:>7} {ist['total_pnl']:>+10,.0f} "
                      f"{ist['max_dd']:>+5.1f}%")
                total_pnl += ist["total_pnl"]
            print("-" * 78)
            print(f"  {len(winners)}銘柄抽出 / 合計損益: {total_pnl:+,.0f}円")
            print("=" * 78)

            out_winners = Path("daytrade_donchian_winners.py")
            write_winners_file(winners, args.target_preset,
                              PRESETS[args.target_preset], out_winners)
            print(f"\n勝ち銘柄リスト: {out_winners.resolve()}")
            print(f"使用例: python daytrade_donchian_compare.py "
                  f"--universe winners --presets {args.target_preset}")

    # HTML
    stamp = datetime.now(JST).strftime('%Y%m%d')
    out = Path(f"daytrade_donchian_compare_{args.universe}_{stamp}.html")
    out.write_text(build_html(preset_results, total_days, args.days,
                              args.budget, args.source, args.universe),
                   encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
