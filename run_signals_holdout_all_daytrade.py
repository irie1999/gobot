"""
run_signals_holdout_all_daytrade.py  ―  デイトレ統合シグナル・損益レポート
==================================================================
逆指値ロング (run_signals_holdout_all.py) を5分足デイトレ用に移植。

【出力タブ】
  📋 シグナル: WATCHLIST 銘柄の総合スコア + 直近シグナル一覧
  💹 損益: 直近30/60/90/120/150/180日 + 全設定統合
  📊 銘柄詳細: シグナル銘柄の取引明細
  🎯 戦略別: 6戦略 × Top10 銘柄
  📰 戦略パラメータ: 各戦略の設定一覧

【キャッシュ】
- 当日生成済みHTMLがあれば再生成スキップ (--force で強制)
- backtest結果も .cache_daytrade_signals/ に保存

【使い方】
  python run_signals_holdout_all_daytrade.py
  python run_signals_holdout_all_daytrade.py --force
  python run_signals_holdout_all_daytrade.py --watchlist daytrade_watchlist_macd.py
  python run_signals_holdout_all_daytrade.py --workers 8
"""

from __future__ import annotations

import argparse
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_signals_daytrade import (
    backtest_one, ALL_STRATEGIES, SIGNAL_PERIODS, _load_watchlist,
)
from risk_metrics_5m import enrich_stats, calc_recommend_score
from bt_score_cache import (
    load_score_cache, save_score_cache, get_frozen_score,
    register_signal, BTCache,
)
from signal_risk_check_daytrade import (
    precompute_all as precompute_risks,
    render_nikkei_banner, render_risk_badges, check_risks,
)
from _signal_funds_daytrade import fund_html as render_fund_html
from _open_html import open_html

JST = timezone(timedelta(hours=9))


def _pf(v):
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"


def _color_pf(v):
    if v == float("inf"):
        return "#4ade80"
    if v >= 1.5:
        return "#4ade80"
    if v >= 1.0:
        return "#facc15"
    return "#f87171"


def _color_score(score):
    if score >= 80:
        return "#4ade80"
    if score >= 60:
        return "#facc15"
    if score >= 40:
        return "#f59e0b"
    return "#f87171"


def build_signal_tab(items, score_cache=None):
    """シグナルタブ HTML (BTスコア凍結対応)。"""
    if not items:
        return '<p style="color:#64748b;padding:24px">シグナルなし</p>'

    # 全体サマリ
    total_signals = sum(len(it["recent_signals"]) for it in items)
    avg_score = sum(it["score"] for it in items) / len(items) if items else 0
    top3 = sorted(items, key=lambda x: -x["score"])[:3]
    top3_html = " / ".join(
        f'<span style="color:{_color_score(it["score"])}">'
        f'{it["name"]}({it["symbol"]}) {it["rank"]}{it["score"]}'
        f'</span>' for it in top3
    )

    # BTスコア凍結対応 (signal_date が同じならキャッシュのスコアを使う)
    if score_cache is not None:
        for it in items:
            if not it["recent_signals"]:
                continue
            latest_sig_date = str(it["recent_signals"][-1].get("entry_dt", ""))[:10]
            if latest_sig_date:
                frozen = get_frozen_score(score_cache,
                                            it["symbol"], it["strategy"],
                                            latest_sig_date, current_bt=it["score"])
                it["frozen_score"] = frozen
                it["score_frozen"] = (frozen != it["score"])
            else:
                it["frozen_score"] = it["score"]
                it["score_frozen"] = False

    summary = f"""
<div class="box">
  <div class="it"><div class="lb">監視銘柄</div><div class="vl">{len(items)}</div></div>
  <div class="it"><div class="lb">平均スコア</div>
    <div class="vl" style="color:{_color_score(avg_score)}">{avg_score:.0f}</div></div>
  <div class="it"><div class="lb">直近7日シグナル</div>
    <div class="vl profit">{total_signals}</div></div>
</div>
<p style="color:#94a3b8;font-size:0.85rem">Top 3: {top3_html}</p>
"""

    # メインテーブル (コンパクト版: 重要情報のみ)
    rows = ""
    for it in items:
        last_p = it.get("last_price", 0)
        rec_count = len(it["recent_signals"])
        rec_text = str(rec_count) if rec_count > 0 else "—"
        display_score = it.get("frozen_score", it["score"])
        # 3期間別 PF + 損益 (30日, 90日, 180日)
        pf_cells = ""
        for days in [30, 90, 180]:
            s = it["period_stats"].get(days, {})
            pf = s.get("pf", 0)
            pnl = s.get("total_pnl", 0)
            n = s.get("n", 0)
            if n == 0:
                pf_cells += '<td>—</td><td>—</td>'
            else:
                c = _color_pf(pf)
                pc = "profit" if pnl >= 0 else "loss"
                pf_cells += (f'<td style="color:{c}">{_pf(pf)}</td>'
                             f'<td class="{pc}">{pnl:+,.0f}</td>')

        rows += f"""
<tr>
  <td><strong style="color:{_color_score(display_score)}">{it['rank']}{display_score}</strong></td>
  <td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>
  <td>{it["strategy"]}</td>
  <td>{last_p:,.0f}</td>
  {pf_cells}
  <td>{rec_text}</td>
</tr>"""

    table = f"""
<h3>WATCHLIST 銘柄 (スコア順) — 30/90/180日 PF+損益</h3>
<table>
  <thead><tr>
    <th>スコア</th><th>銘柄</th><th>戦略</th><th>現値</th>
    <th>30日PF</th><th>30日損益</th>
    <th>90日PF</th><th>90日損益</th>
    <th>180日PF</th><th>180日損益</th>
    <th>直近7日</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
"""

    # 直近シグナル一覧
    all_recent = []
    for it in items:
        for sig in it["recent_signals"]:
            all_recent.append((it["name"], it["symbol"], it["strategy"], sig))
    all_recent.sort(key=lambda x: str(x[3].get("entry_dt", "")), reverse=True)

    sig_rows = ""
    for name, sym, strat, sig in all_recent[:50]:
        entry_dt = sig.get("entry_dt")
        exit_dt = sig.get("exit_dt")
        ed = entry_dt.strftime("%m-%d %H:%M") if hasattr(entry_dt, "strftime") else "?"
        xd = exit_dt.strftime("%m-%d %H:%M") if hasattr(exit_dt, "strftime") else "?"
        pnl = sig.get("pnl", 0)
        pc = "profit" if pnl >= 0 else "loss"
        sig_rows += f"""
<tr>
  <td>{ed}</td><td>{xd}</td>
  <td class="sym">{name}<br><small class="code">{sym}</small></td>
  <td>{strat}</td>
  <td>{sig.get('entry_p',0):,.0f}</td>
  <td>{sig.get('exit_p',0):,.0f}</td>
  <td class="{pc}">{pnl:+,.0f}</td>
  <td>{sig.get('reason','?')}</td>
</tr>"""

    sig_section = f"""
<h3>直近7日 発生シグナル ({len(all_recent)}件)</h3>
<table>
  <thead><tr>
    <th>Entry</th><th>Exit</th><th>銘柄</th><th>戦略</th>
    <th>買値</th><th>決済</th><th>損益</th><th>理由</th>
  </tr></thead>
  <tbody>{sig_rows}</tbody>
</table>
"""

    return summary + table + sig_section


def build_pnl_tab(items):
    """損益タブ HTML (期間別切替)。"""
    period_btns = ""
    period_panes = ""
    for i, days in enumerate(SIGNAL_PERIODS):
        active = "active" if i == 2 else ""  # デフォルト 90日
        display = "block" if i == 2 else "none"
        period_btns += (f'<button class="p-btn {active}" '
                        f'onclick="switchPnl({days})">{days}日</button>')
        # 集計
        rows = ""
        total_pnl = total_n = total_wins = 0
        for it in items:
            s = it["period_stats"].get(days, {})
            n = s.get("n", 0)
            if n == 0:
                continue
            total_pnl += s.get("total_pnl", 0)
            total_n += n
            total_wins += int(n * s.get("win_rate", 0) / 100)
            pf = s.get("pf", 0)
            pc = "profit" if s.get("total_pnl", 0) >= 0 else "loss"
            rows += f"""
<tr>
  <td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>
  <td>{it["strategy"]}</td>
  <td>{n}</td>
  <td>{s.get("win_rate", 0):.0f}%</td>
  <td style="color:{_color_pf(pf)}">{_pf(pf)}</td>
  <td class="{pc}">{s.get("total_pnl", 0):+,.0f}</td>
  <td class="loss">{s.get("max_dd", 0):+.1f}%</td>
  <td>{s.get("sharpe", 0):.2f}</td>
  <td>{s.get("max_losing_streak", 0)}</td>
</tr>"""

        avg_wr = total_wins / total_n * 100 if total_n > 0 else 0
        sum_box = f"""
<div class="box">
  <div class="it"><div class="lb">取引数</div><div class="vl">{total_n}</div></div>
  <div class="it"><div class="lb">勝率</div><div class="vl">{avg_wr:.0f}%</div></div>
  <div class="it"><div class="lb">総損益</div>
    <div class="vl {'profit' if total_pnl >= 0 else 'loss'}">{total_pnl:+,.0f}円</div></div>
</div>
"""

        pane = f"""
{sum_box}
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>取引</th><th>勝率</th><th>PF</th>
    <th>損益</th><th>DD</th><th>Sharpe</th><th>最大連敗</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
"""
        period_panes += (f'<div id="pnl{days}" class="pnl-pane" '
                         f'style="display:{display}">{pane}</div>')

    return f"""
<div style="margin:12px 0 16px">
  <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
  {period_btns}
</div>
{period_panes}
"""


def build_strategy_tab(items):
    """戦略別タブ HTML。"""
    by_strat = {}
    for it in items:
        by_strat.setdefault(it["strategy"], []).append(it)

    if not by_strat:
        return '<p style="color:#64748b;padding:24px">データなし</p>'

    html = "<h3>戦略別 Top 10 (90日PF順)</h3>"
    for strat in sorted(by_strat.keys()):
        strat_items = sorted(by_strat[strat],
                              key=lambda x: -x["period_stats"][90].get("pf", 0))
        rows = ""
        for it in strat_items[:10]:
            s = it["period_stats"][90]
            pf = s.get("pf", 0)
            pc = "profit" if s.get("total_pnl", 0) >= 0 else "loss"
            rows += f"""
<tr>
  <td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>
  <td>{s.get("n", 0)}</td>
  <td>{s.get("win_rate", 0):.0f}%</td>
  <td style="color:{_color_pf(pf)}">{_pf(pf)}</td>
  <td class="{pc}">{s.get("total_pnl", 0):+,.0f}</td>
  <td class="loss">{s.get("max_dd", 0):+.1f}%</td>
</tr>"""
        html += f"""
<h3>{strat} ({len(by_strat[strat])}銘柄)</h3>
<table>
  <thead><tr>
    <th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th><th>損益</th><th>DD</th>
  </tr></thead><tbody>{rows}</tbody></table>
"""
    return html


def build_params_tab():
    """戦略パラメータ説明タブ。"""
    return """
<h3>戦略パラメータ一覧 (5分足デイトレ版)</h3>
<table>
  <thead><tr>
    <th>戦略</th><th>em</th><th>sm</th><th>tm</th><th>条件</th>
  </tr></thead>
  <tbody>
    <tr><td><strong>DON</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>過去8本(40分)高値ブレイク + 陽線</td></tr>
    <tr><td><strong>MACD</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>MACD(8,17,5) ゴールデンクロス + 出来高1.2× + MA10上</td></tr>
    <tr><td><strong>RSI2</strong></td><td>0.0</td><td>2.0</td><td>4.0</td>
      <td>RSI(2)<10 + MA20上 + IBS<0.35 (押し目買い)</td></tr>
    <tr><td><strong>A7</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>Stochastic(14,3,3) 過売り反発 + MA75上</td></tr>
    <tr><td><strong>VOL</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>5本高値 + 出来高>20本平均×1.5</td></tr>
    <tr><td><strong>MOM</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>ROC10>3% + MA25>MA75</td></tr>
    <tr style="border-top:2px solid #334155">
      <td colspan="5" style="color:#94a3b8;padding-top:8px"><strong>空売り戦略</strong></td></tr>
    <tr><td><strong>DON_S</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>過去8本安値ブレイクダウン + 陰線</td></tr>
    <tr><td><strong>MACD_S</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>MACD デッドクロス + 出来高 + MA10下</td></tr>
    <tr><td><strong>RSI2_S</strong></td><td>0.0</td><td>2.0</td><td>4.0</td>
      <td>RSI(2)>90 + MA20下 + IBS>0.65 (戻り売り)</td></tr>
    <tr><td><strong>A7_S</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>Stochastic 過買い反落 + MA75下</td></tr>
    <tr><td><strong>VOL_S</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>5本安値 + 出来高>20本平均×1.5</td></tr>
    <tr><td><strong>MOM_S</strong></td><td>0.0</td><td>1.5</td><td>3.0</td>
      <td>ROC10<-3% + MA25<MA75</td></tr>
  </tbody>
</table>

<h3>共通設定</h3>
<table>
  <thead><tr><th>項目</th><th>値</th></tr></thead>
  <tbody>
    <tr><td>エントリー時刻</td><td>9:30 - 14:30</td></tr>
    <tr><td>引け強制決済</td><td>14:55</td></tr>
    <tr><td>ウォームアップ</td><td>20バー (100分)</td></tr>
    <tr><td>ATR期間</td><td>14バー</td></tr>
    <tr><td>スリッページ</td><td>0.3% (買い+0.3%/売り-0.3%)</td></tr>
    <tr><td>手数料</td><td>0.1%/片道 (0.2%往復)</td></tr>
  </tbody>
</table>
"""


def build_symbol_detail_tab(items):
    """シグナル銘柄詳細タブ HTML。"""
    if not items:
        return '<p style="color:#64748b;padding:24px">銘柄なし</p>'

    # 各銘柄ごとに直近取引明細
    sorted_items = sorted(items, key=lambda x: -x["score"])
    nav = ""
    panes = ""
    for i, it in enumerate(sorted_items):
        active = "block" if i == 0 else "none"
        active_btn = "active" if i == 0 else ""
        sym_short = it["symbol"].replace(".T", "")
        score = it.get("frozen_score", it["score"])
        nav += (f'<button class="sym-btn {active_btn}" '
                f'onclick="switchSym(\'sym{sym_short}\')">'
                f'<strong>{it["symbol"]}</strong><br>'
                f'<small style="color:#94a3b8">{it["name"][:8]}</small><br>'
                f'<small style="color:#fbbf24">BT:{score}</small></button>')

        # 直近取引
        trades = sorted(it["trades"][-30:],
                         key=lambda x: str(x.get("entry_dt", "")),
                         reverse=True)
        trade_rows = ""
        for t in trades:
            entry_dt = t.get("entry_dt")
            ed = entry_dt.strftime("%m-%d %H:%M") if hasattr(entry_dt, "strftime") else "?"
            pnl = t.get("pnl", 0)
            pc = "profit" if pnl >= 0 else "loss"
            trade_rows += f"""
<tr>
  <td>{ed}</td>
  <td>{t.get('entry_p',0):,.0f}</td>
  <td>{t.get('exit_p',0):,.0f}</td>
  <td class="{pc}">{pnl:+,.0f}</td>
  <td>{t.get('reason','?')}</td>
</tr>"""

        # 期間別統計
        period_summary = ""
        for days in SIGNAL_PERIODS:
            s = it["period_stats"].get(days, {})
            n = s.get("n", 0)
            if n == 0:
                continue
            pf = s.get("pf", 0)
            pnl = s.get("total_pnl", 0)
            pc = "profit" if pnl >= 0 else "loss"
            period_summary += (f'<span style="margin-right:14px">'
                               f'{days}日: <strong>{n}取引</strong> '
                               f'PF<strong>{_pf(pf)}</strong> '
                               f'<span class="{pc}">{pnl:+,.0f}円</span>'
                               f'</span>')

        panes += f"""
<div id="sym{sym_short}" class="sym-pane" style="display:{active};padding:12px 0">
  <h3>{it["name"]} ({it["symbol"]}) — {it["strategy"]} — BTスコア {score} {it["rank"]}</h3>
  <p style="color:#94a3b8;font-size:0.85rem">{period_summary}</p>
  <h3>直近30取引</h3>
  <table>
    <thead><tr>
      <th>Entry</th><th>買値</th><th>決済</th><th>損益</th><th>理由</th>
    </tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>"""

    return f"""
<div class="sym-nav">{nav}</div>
{panes}
"""


def main():
    parser = argparse.ArgumentParser(
        description="デイトレ統合シグナル・損益レポート")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-risk-check", action="store_true")
    parser.add_argument("--budget", type=int, default=300_000)
    args = parser.parse_args()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out = Path(f"daytrade_signals_holdout_all_{today}.html")
    if out.exists() and not args.force:
        print(f"[CACHE] 当日生成済み: {out.resolve()}")
        if not args.no_browser:
            webbrowser.open(out.resolve().as_uri())
        return

    # WATCHLIST
    watchlist = _load_watchlist(args.watchlist)
    if not watchlist:
        print(f"[error] WATCHLIST 空")
        print(f"  先に: python build_watchlist_daytrade.py --combined")
        return
    print(f"WATCHLIST: {len(watchlist)}銘柄")

    # backtest
    print(f"\nbacktest 実行 (workers={args.workers})...", flush=True)
    items = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for entry in watchlist:
            if len(entry) == 3:
                sym, name, strat = entry
            else:
                sym, name = entry[:2]
                strat = "DON"
            futs[ex.submit(backtest_one, sym, name, strat)] = (sym, strat)
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                items.append(r)
            if i % 20 == 0 or i == len(watchlist):
                print(f"  {i}/{len(watchlist)}", flush=True)

    items.sort(key=lambda x: -x["score"])

    # BTスコアキャッシュ読込
    score_cache = load_score_cache()
    # シグナル銘柄のリスク事前計算
    if not args.no_risk_check:
        sig_syms = [(it["symbol"], it["name"]) for it in items
                    if it["recent_signals"]]
        if sig_syms:
            print(f"リスクチェック中 ({len(sig_syms)}件)...", flush=True)
            precompute_risks(sig_syms, workers=args.workers)
    # シグナル登録
    for it in items:
        for sig in it["recent_signals"]:
            sig_date = str(sig.get("entry_dt", ""))[:10]
            if sig_date:
                register_signal(score_cache, it["symbol"], it["strategy"],
                                sig_date, it["score"])
    save_score_cache(score_cache)

    # HTML
    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; margin: 0; }
h1 { color: #10b981; margin: 0 0 4px; font-size: 1.5rem; }
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
.box .vl { font-size: 1.3rem; font-weight: 700; }
.tab-nav { display: flex; gap: 0; margin: 16px 0 0;
           border-bottom: 2px solid #1e293b; }
.tab-btn { padding: 9px 22px; background: #1e293b; border: none;
           border-radius: 6px 6px 0 0; color: #94a3b8; cursor: pointer;
           font-size: 0.9rem; border-bottom: 2px solid transparent;
           margin-bottom: -2px; }
.tab-btn:hover:not(.active) { background: #263349; color: #e2e8f0; }
.tab-btn.active { background: #0f172a; color: #60a5fa;
                  border-bottom: 2px solid #60a5fa; font-weight: 700; }
.tab-pane { display: none; padding: 12px 0; }
.tab-pane.active { display: block; }
.p-btn { background: #1e293b; border: 1px solid #334155; color: #94a3b8;
         padding: 5px 14px; border-radius: 4px; cursor: pointer;
         font-size: 0.82rem; margin-right: 4px; }
.p-btn:hover { color: #e2e8f0; }
.p-btn.active { background: #3b82f6; color: #fff;
                border-color: #3b82f6; font-weight: 700; }
.pnl-pane { display: none; }
.sym-nav { display: flex; flex-wrap: wrap; gap: 4px; margin: 12px 0 16px;
           padding: 10px; background: #0f172a; border-radius: 8px; }
.sym-btn { padding: 6px 12px; background: #1e293b; border: 1px solid #334155;
           color: #e2e8f0; border-radius: 6px; cursor: pointer;
           font-size: 0.78rem; min-width: 90px; text-align: center; }
.sym-btn:hover { background: #263349; border-color: #64748b; }
.sym-btn.active { background: #1d4ed8; border-color: #3b82f6; }
.sym-pane { display: none; }
"""

    js = """
function switchTab(tab){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('t-'+tab).classList.add('active');
  (event.target.closest('.tab-btn')||event.target).classList.add('active');
}
function switchPnl(days){
  document.querySelectorAll('.pnl-pane').forEach(p=>p.style.display='none');
  document.querySelectorAll('.p-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('pnl'+days).style.display='block';
  (event.target.closest('.p-btn')||event.target).classList.add('active');
}
function switchSym(id){
  document.querySelectorAll('.sym-pane').forEach(p=>p.style.display='none');
  document.querySelectorAll('.sym-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).style.display='block';
  (event.target.closest('.sym-btn')||event.target).classList.add('active');
}
"""

    # 日経バナー
    banner = render_nikkei_banner() if not args.no_risk_check else ""

    sig_html = build_signal_tab(items, score_cache=score_cache)
    sig_html = banner + sig_html

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>デイトレ統合レポート — {today}</title>
<style>{css}</style></head><body>
<h1>📊 デイトレ統合シグナル・損益レポート</h1>
<p class="subtitle">生成: {today} / WATCHLIST: {len(watchlist)}銘柄 / 実成績: {len(items)}銘柄 /
   キャッシュ: {len(score_cache)}件</p>
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab('sig')">📋 シグナル</button>
  <button class="tab-btn" onclick="switchTab('pnl')">💹 損益</button>
  <button class="tab-btn" onclick="switchTab('sym')">📊 銘柄詳細</button>
  <button class="tab-btn" onclick="switchTab('strat')">🎯 戦略別</button>
  <button class="tab-btn" onclick="switchTab('params')">📰 戦略パラメータ</button>
</div>
<div id="t-sig" class="tab-pane active">{sig_html}</div>
<div id="t-pnl" class="tab-pane">{build_pnl_tab(items)}</div>
<div id="t-sym" class="tab-pane">{build_symbol_detail_tab(items)}</div>
<div id="t-strat" class="tab-pane">{build_strategy_tab(items)}</div>
<div id="t-params" class="tab-pane">{build_params_tab()}</div>
<script>{js}</script>
</body></html>"""

    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        open_html(out.resolve())


if __name__ == "__main__":
    main()
