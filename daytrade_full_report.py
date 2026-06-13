"""
daytrade_full_report.py  ―  デイトレ包括バックテストレポート
==================================================================
run_signals_holdout_all.py を参考に、デイトレード戦略のバックテスト結果を
タブ付きHTMLで包括的に表示。

【表示内容】
  📊 サマリタブ: 現在winners全体の概況
  💹 期間別タブ: 30/60/90/180/365/730日の成績比較
  📋 銘柄別タブ: 各銘柄の取引明細
  🎯 レジーム別タブ: Bull/Sideways/Bear winners比較

【キャッシュ】
  当日生成済みHTML があればスキップ (--force で再生成)
  backtest結果も .cache_daytrade/ に保存して2回目以降高速化

【使い方】
  python daytrade_full_report.py                  # デフォルト (現在winners)
  python daytrade_full_report.py --force          # キャッシュ無視
  python daytrade_full_report.py --no-browser     # ブラウザ非表示
  python daytrade_full_report.py --regime bull    # Bull winners 使用
"""

from __future__ import annotations

import argparse
import pickle
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_donchian import (
    backtest_symbol, calc_stats,
    DON_PERIOD, TARGET_R, GAP_MAX_PCT, STOP_MAX_PCT,
)

JST = timezone(timedelta(hours=9))

PERIODS = [30, 60, 90, 180, 365, 730]
DEFAULT_PERIOD = 90

REGIME_FILES = {
    "current": "daytrade_donchian_winners.py",
    "bull": "daytrade_donchian_winners_bull_top30.py",
    "sideways": "daytrade_donchian_winners_sideways_top30.py",
    "bear": "daytrade_donchian_winners_bear_top30.py",
}


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _fmt_dt(dt):
    if hasattr(dt, "strftime"):
        return dt.strftime("%m-%d %H:%M")
    return str(dt)[:16] if dt else "-"


def load_symbols(file_path):
    """winners ファイルから SYMBOLS をロード。"""
    import importlib.util
    p = Path(file_path)
    if not p.exists():
        return []
    spec = importlib.util.spec_from_file_location("wmod", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "SYMBOLS", [])


def run_backtest_cached(symbol, name, df, budget, max_risk, cache_dir, days):
    """backtest を実行 (キャッシュ対応)。"""
    cache_key = f"{symbol}_{days}d.pkl"
    cache_path = cache_dir / cache_key
    if cache_path.exists():
        try:
            return pickle.loads(cache_path.read_bytes())
        except Exception:
            pass
    r = backtest_symbol(symbol, name, df, budget, max_risk)
    try:
        cache_path.write_bytes(pickle.dumps(r))
    except Exception:
        pass
    return r


def build_summary_card(stats, label, color="#10b981"):
    """サマリカード生成。"""
    cls = "profit" if stats["total_pnl"] >= 0 else "loss"
    pf = _pf(stats["pf"])
    return f"""
<div style="background:#1e293b;padding:14px 18px;border-radius:8px;
            border-left:3px solid {color};min-width:160px;flex:1">
  <div style="color:#94a3b8;font-size:0.72rem;margin-bottom:4px">{label}</div>
  <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:0.85rem">
    <span>取引 <strong>{stats['n']}</strong></span>
    <span>勝率 <strong>{stats['win_rate']:.0f}%</strong></span>
    <span>PF <strong>{pf}</strong></span>
    <span class="{cls}">{stats['total_pnl']:+,.0f}円</span>
    <span class="loss">{stats['max_dd']:+.1f}%</span>
  </div>
</div>
"""


def build_trades_table(trades, max_rows=None):
    """取引明細テーブル。"""
    if not trades:
        return '<p style="color:#64748b;padding:12px">取引なし</p>'
    sorted_trades = sorted(trades, key=lambda x: str(x.get("entry_dt", "")))
    if max_rows:
        sorted_trades = sorted_trades[-max_rows:]  # 直近 N 件
    rows = ""
    for t in sorted_trades:
        pc = "profit" if t["pnl"] > 0 else "loss"
        ed = _fmt_dt(t.get("entry_dt"))
        xd = _fmt_dt(t.get("exit_dt"))
        try:
            delta = t["exit_dt"] - t["entry_dt"]
            hold = f"{int(delta.total_seconds() // 60)}分"
        except Exception:
            hold = "-"
        rows += f"""<tr>
          <td>{ed}</td><td>{xd}</td><td>{hold}</td>
          <td>{t.get('entry_p',0):,.0f}</td>
          <td class="loss">{t.get('stop_p',0):,.0f}</td>
          <td class="profit">{t.get('target_p',0):,.0f}</td>
          <td>{t.get('exit_p',0):,.0f}</td>
          <td>{t.get('qty',0)}</td>
          <td class="{pc}">{t['pnl']:+,.0f}</td>
          <td class="{pc}">{t.get('pct',0):+.2f}%</td>
          <td>{t.get('reason','?')}</td></tr>"""
    return f"""
<table><thead><tr>
  <th>Entry</th><th>Exit</th><th>保有</th>
  <th>買値</th><th>損切</th><th>目標</th><th>決済値</th><th>株数</th>
  <th>損益</th><th>%</th><th>決済理由</th>
</tr></thead><tbody>{rows}</tbody></table>
"""


def process_period(targets, days, budget, max_risk, cache_dir):
    """指定期間で全銘柄backtest → 集計。"""
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, days, source="local")
    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = run_backtest_cached(sym, name, fetched[sym], budget, max_risk,
                                cache_dir, days)
        if r and r["trades"]:
            items.append(r)
    all_trades = [t for it in items for t in it["trades"]]
    overall = calc_stats(all_trades, budget) if all_trades else None
    return items, overall


def build_period_pane(items, overall, days, budget):
    """1期間分のHTML パネル。"""
    if overall is None:
        return f'<p style="color:#64748b;padding:24px">直近{days}日: 取引なし</p>'

    # 全体サマリ
    cls = "profit" if overall["total_pnl"] >= 0 else "loss"
    summary = f"""
<div class="box">
  <div class="it"><div class="lb">総損益</div><div class="vl {cls}">{overall["total_pnl"]:+,.0f}円</div></div>
  <div class="it"><div class="lb">取引</div><div class="vl">{overall["n"]}</div></div>
  <div class="it"><div class="lb">勝率</div><div class="vl">{overall["win_rate"]:.1f}%</div></div>
  <div class="it"><div class="lb">PF</div><div class="vl">{_pf(overall["pf"])}</div></div>
  <div class="it"><div class="lb">DD</div><div class="vl loss">{overall["max_dd"]:+.1f}%</div></div>
  <div class="it"><div class="lb">平均利益</div><div class="vl profit">{overall["avg_win"]:+,.0f}</div></div>
  <div class="it"><div class="lb">平均損失</div><div class="vl loss">{overall["avg_loss"]:+,.0f}</div></div>
</div>
"""

    # 銘柄別サマリ表
    sorted_items = sorted(items,
                          key=lambda x: -sum(t["pnl"] for t in x["trades"]))
    sym_rows = ""
    for it in sorted_items:
        stats = calc_stats(it["trades"], budget)
        c = "profit" if stats["total_pnl"] >= 0 else "loss"
        sym_rows += (
            f'<tr><td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>'
            f'<td>{stats["n"]}</td><td>{stats["win_rate"]:.0f}%</td>'
            f'<td>{_pf(stats["pf"])}</td>'
            f'<td class="{c}">{stats["total_pnl"]:+,.0f}</td>'
            f'<td class="profit">{stats["avg_win"]:+,.0f}</td>'
            f'<td class="loss">{stats["avg_loss"]:+,.0f}</td>'
            f'<td class="loss">{stats["max_dd"]:+.1f}%</td></tr>'
        )
    sym_table = f"""
<h3>銘柄別サマリ ({len(items)}銘柄, 損益順)</h3>
<table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
<tbody>{sym_rows}</tbody></table>
"""

    return summary + sym_table


def build_symbol_pane(items, budget):
    """銘柄別 詳細ペイン (各銘柄の取引明細)。"""
    if not items:
        return '<p style="color:#64748b;padding:24px">取引なし</p>'

    sorted_items = sorted(items,
                          key=lambda x: -sum(t["pnl"] for t in x["trades"]))
    # 銘柄ジャンプリンク
    nav = ""
    for it in sorted_items:
        sym_short = it["symbol"].replace(".T", "")
        nav += (f'<a href="#sym-{sym_short}" style="color:#10b981;'
                f'margin:0 6px;font-size:.82rem;text-decoration:none">'
                f'{it["name"]}</a>')

    detail = f'<div class="nav-block">{nav}</div>'

    for it in sorted_items:
        sym_short = it["symbol"].replace(".T", "")
        stats = calc_stats(it["trades"], budget)
        c = "profit" if stats["total_pnl"] >= 0 else "loss"
        detail += f"""
<h3 id="sym-{sym_short}">{it["name"]} ({it["symbol"]})</h3>
<div class="box">
  <div class="it"><div class="lb">総損益</div>
    <div class="vl {c}">{stats["total_pnl"]:+,.0f}円</div></div>
  <div class="it"><div class="lb">取引</div><div class="vl">{stats["n"]}</div></div>
  <div class="it"><div class="lb">勝率</div><div class="vl">{stats["win_rate"]:.1f}%</div></div>
  <div class="it"><div class="lb">PF</div><div class="vl">{_pf(stats["pf"])}</div></div>
  <div class="it"><div class="lb">DD</div><div class="vl loss">{stats["max_dd"]:+.1f}%</div></div>
</div>
{build_trades_table(it["trades"], max_rows=50)}
"""
    return detail


def build_regime_pane(regime, days, budget, max_risk, cache_dir):
    """レジーム別ペイン。"""
    file_path = REGIME_FILES[regime]
    targets = load_symbols(file_path)
    if not targets:
        return f'<p style="color:#64748b;padding:24px">{file_path} なし</p>'
    items, overall = process_period(targets, days, budget, max_risk, cache_dir)
    return build_period_pane(items, overall, days, budget)


def main():
    parser = argparse.ArgumentParser(
        description="デイトレ包括バックテストレポート")
    parser.add_argument("--regime", default="current",
                        choices=["current", "bull", "sideways", "bear"],
                        help="使用する winners (デフォルト: current)")
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--default-days", type=int, default=DEFAULT_PERIOD,
                        choices=PERIODS)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="キャッシュ無視で再生成")
    args = parser.parse_args()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out_path = Path(f"daytrade_full_report_{args.regime}_{today}.html")

    # 当日キャッシュ
    if out_path.exists() and not args.force:
        print(f"[CACHE] 当日生成済み: {out_path.resolve()}")
        print(f"        再生成は --force")
        if not args.no_browser:
            webbrowser.open(out_path.resolve().as_uri())
        return

    # BTキャッシュディレクトリ
    cache_dir = Path(f".cache_daytrade/{args.regime}_{today}")
    cache_dir.mkdir(parents=True, exist_ok=True)

    # === ターゲット読み込み ===
    file_path = REGIME_FILES[args.regime]
    targets = load_symbols(file_path)
    if not targets:
        print(f"[error] {file_path} がありません")
        return
    print(f"対象: {len(targets)}銘柄 ({file_path})")

    # === 期間別 backtest 実行 ===
    period_results = {}
    for days in PERIODS:
        print(f"\n[期間 {days}日] backtest 中...", flush=True)
        items, overall = process_period(targets, days, args.budget,
                                         args.max_risk, cache_dir)
        period_results[days] = (items, overall)
        if overall:
            print(f"  → 取引{overall['n']} 勝率{overall['win_rate']:.1f}% "
                  f"PF{_pf(overall['pf'])} 損益{overall['total_pnl']:+,.0f}円")
        else:
            print(f"  → 取引なし")

    # === HTML構築 ===
    period_btns = ""
    period_panes = ""
    for days in PERIODS:
        active = "active" if days == args.default_days else ""
        display = "block" if days == args.default_days else "none"
        period_btns += (f'<button class="period-btn {active}" '
                        f'onclick="switchPeriod({days})">{days}日</button>\n')
        items, overall = period_results[days]
        period_panes += (f'<div id="p{days}" class="period-pane" '
                         f'style="display:{display}">'
                         f'{build_period_pane(items, overall, days, args.budget)}'
                         f'</div>\n')

    # 銘柄別ペイン (デフォルト期間)
    items_default, _ = period_results[args.default_days]
    symbol_pane = build_symbol_pane(items_default, args.budget)

    # サマリタブ (デフォルト期間の概要)
    items_summary, overall_summary = period_results[args.default_days]
    if overall_summary:
        cls = "profit" if overall_summary["total_pnl"] >= 0 else "loss"
        summary_top = f"""
<h2>{file_path} (直近{args.default_days}日)</h2>
<div class="box">
  <div class="it"><div class="lb">監視銘柄</div><div class="vl">{len(targets)}</div></div>
  <div class="it"><div class="lb">総損益</div><div class="vl {cls}">{overall_summary["total_pnl"]:+,.0f}円</div></div>
  <div class="it"><div class="lb">取引</div><div class="vl">{overall_summary["n"]}</div></div>
  <div class="it"><div class="lb">勝率</div><div class="vl">{overall_summary["win_rate"]:.1f}%</div></div>
  <div class="it"><div class="lb">PF</div><div class="vl">{_pf(overall_summary["pf"])}</div></div>
  <div class="it"><div class="lb">DD</div><div class="vl loss">{overall_summary["max_dd"]:+.1f}%</div></div>
</div>
"""
    else:
        summary_top = f'<p style="color:#64748b">直近{args.default_days}日: 取引なし</p>'

    # 戦略パラメータ表示
    strat_info = f"""
<div class="strat-info">
  <strong style="color:#10b981">戦略パラメータ:</strong>
  Donchian {DON_PERIOD}本高値ブレイク / R:R {TARGET_R}:1 / GAP上限 {GAP_MAX_PCT}% / STOP上限 {STOP_MAX_PCT}%
</div>
"""

    # === レジーム別ペイン (Bull/Sideways/Bear) ===
    regime_panes_html = ""
    for regime_key, regime_label in [("bull", "Bull (上昇)"),
                                       ("sideways", "Sideways (横ばい)"),
                                       ("bear", "Bear (下落)")]:
        active = "block" if regime_key == "bull" else "none"
        regime_panes_html += f"""
<div id="reg_{regime_key}" class="regime-pane" style="display:{active}">
  <h3>{regime_label} winners (直近{args.default_days}日)</h3>
  {build_regime_pane(regime_key, args.default_days, args.budget, args.max_risk, cache_dir)}
</div>
"""

    # === CSS / JS ===
    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; margin: 0; }
h1 { color: #10b981; margin: 0 0 4px; font-size: 1.5rem; }
h2 { color: #10b981; margin: 20px 0 10px; font-size: 1.15rem;
     border-left: 3px solid #10b981; padding-left: 10px; }
h3 { color: #10b981; margin: 16px 0 8px; font-size: 1rem;
     border-left: 3px solid #10b981; padding-left: 10px; }
.subtitle { color: #94a3b8; margin-bottom: 20px; font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; margin-bottom: 14px;
        font-size: 0.82rem; }
th { background: #1e293b; color: #94a3b8; padding: 6px 8px;
     text-align: center; border: 1px solid #334155; white-space: nowrap; }
td { padding: 5px 8px; border: 1px solid #1e293b;
     text-align: right; white-space: nowrap; }
.sym { text-align: left; font-weight: 600; min-width: 160px; }
.code { color: #64748b; font-weight: 400; font-size: 0.75rem; }
.profit { color: #4ade80; }
.loss { color: #f87171; }
.box { background: #1e293b; padding: 14px; border-radius: 8px;
       margin-bottom: 14px; display: flex; gap: 24px; flex-wrap: wrap; }
.box .it { text-align: center; }
.box .lb { color: #94a3b8; font-size: 0.75rem; }
.box .vl { font-size: 1.3rem; font-weight: 700; }
.strat-info { background: #1e293b; padding: 10px 14px;
              border-radius: 6px; margin-bottom: 16px;
              font-size: 0.82rem; color: #94a3b8; }
.nav-block { background: #1e293b; padding: 12px; border-radius: 8px;
             margin-bottom: 16px; line-height: 1.8; }

/* タブナビ */
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
.tab-pane.active { display: block; }

/* 期間セレクター */
.period-btn { background: #1e293b; border: 1px solid #334155;
              color: #94a3b8; padding: 5px 14px; border-radius: 4px;
              cursor: pointer; font-size: 0.82rem; margin-right: 4px;
              transition: all .2s; }
.period-btn:hover { color: #e2e8f0; border-color: #64748b; }
.period-btn.active { background: #3b82f6; color: #fff;
                     border-color: #3b82f6; font-weight: 700; }
.period-pane { display: none; }
.regime-pane { display: none; }
"""

    js = """
function switchTab(tab) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  (event.target.closest('.tab-btn') || event.target).classList.add('active');
}
function switchPeriod(days) {
  document.querySelectorAll('.period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('p' + days).style.display = 'block';
  (event.target.closest('.period-btn') || event.target).classList.add('active');
}
function switchRegime(regime) {
  document.querySelectorAll('.regime-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.regime-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('reg_' + regime).style.display = 'block';
  (event.target.closest('.regime-btn') || event.target).classList.add('active');
}
"""

    # === HTML 完成 ===
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>デイトレ バックテストレポート — {today}</title>
<style>{css}</style></head><body>
<h1>📊 デイトレ バックテストレポート ({args.regime})</h1>
<p class="subtitle">生成: {today} / winners: {file_path} / 監視: {len(targets)}銘柄 /
   予算: {args.budget:,}円 / リスク: {args.max_risk:,}円/取引</p>
{strat_info}

<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab('summary')">📊 サマリ</button>
  <button class="tab-btn" onclick="switchTab('periods')">💹 期間別</button>
  <button class="tab-btn" onclick="switchTab('symbols')">📋 銘柄別</button>
  <button class="tab-btn" onclick="switchTab('regimes')">🎯 レジーム別</button>
</div>

<div id="tab-summary" class="tab-pane active">
  {summary_top}
  <p style="color:#94a3b8;font-size:0.82rem;margin-top:16px">
    💡 期間別タブで30/60/90/180/365/730日の成績比較が見られます。<br>
    💡 銘柄別タブで個別の取引明細が見られます。<br>
    💡 レジーム別タブで Bull/Sideways/Bear winners の比較が見られます。
  </p>
</div>

<div id="tab-periods" class="tab-pane">
  <div style="margin:12px 0 16px">
    <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
    {period_btns}
  </div>
  {period_panes}
</div>

<div id="tab-symbols" class="tab-pane">
  <p style="color:#94a3b8;font-size:0.82rem">
    各銘柄の取引明細 (直近{args.default_days}日、損益降順、直近50取引まで)
  </p>
  {symbol_pane}
</div>

<div id="tab-regimes" class="tab-pane">
  <div style="margin:12px 0 16px">
    <button class="regime-btn period-btn active" onclick="switchRegime('bull')">🟢 Bull</button>
    <button class="regime-btn period-btn" onclick="switchRegime('sideways')">🟡 Sideways</button>
    <button class="regime-btn period-btn" onclick="switchRegime('bear')">🔴 Bear</button>
  </div>
  {regime_panes_html}
</div>

<script>{js}</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    print(f"\nレポート生成完了: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
