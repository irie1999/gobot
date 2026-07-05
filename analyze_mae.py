"""
analyze_mae.py — 約定後の含み損推移分析 (MAE/MFE)

「目標達成トレードは最初どれだけ含み損を抱えていたか」を可視化する。
ロング/ショート両対応 (--short)。

MAE (Maximum Adverse Excursion):  保有中の最大含み損率
MFE (Maximum Favorable Excursion): 保有中の最大含み益率
days_neg (含み損日数): 終値が約定価格より不利だった日数
  ロング = 終値 < 約定価格 / ショート = 終値 > 約定価格
  ※ backtest_limit_entry が方向対応で計算するため、ショートでも正しく出る

Usage:
  python analyze_mae.py                       # ロング (check_signals_stop/breakout)
  python analyze_mae.py --short               # ショート (check_signals_short/short_breakout)
  python analyze_mae.py --short --days 365 --workers 8
  python analyze_mae.py --short --watchlist watchlist_proposal_2026-06-10.py
  python analyze_mae.py --no-browser
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ── TRADING_MODE は import 前に確定させる ────────────────────────────────────
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

_IS_SHORT = "--short" in sys.argv

import check_signals_stop     as _stop
import check_signals_breakout as _brk
if _IS_SHORT:
    import check_signals_short          as _sstop
    import check_signals_short_breakout as _sbrk
from backtest_limit_entry import fetch, run_limit_backtest, WORKERS, JST

# ロング/ショートでモジュールを切替
_STOP_MOD = _sstop if _IS_SHORT else _stop
_BRK_MOD  = _sbrk  if _IS_SHORT else _brk

# watchlist_proposal_*.py の属性名
_WL_ATTR_STOP = "SHORT_WATCHLIST"     if _IS_SHORT else "STOP_WATCHLIST"
_WL_ATTR_BRK  = "SHORT_BRK_WATCHLIST" if _IS_SHORT else "BRK_WATCHLIST"


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int, default=365)
    p.add_argument("--workers",    type=int, default=WORKERS)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--short",      action="store_true",
                   help="ショート戦略 (A7_S/RSI2_S/MACD_S/DON_S/MOM_S/GAP_S) を分析")
    p.add_argument("--aggressive", action="store_true",
                   help="aggressive プリセットで分析")
    p.add_argument("--watchlist",  type=str, default=None,
                   help="watchlist_proposal_*.py からWATCHLISTを読み込む")
    return p.parse_args()


def _load_watchlist_proposal(path: str):
    """watchlist_proposal_*.py から (stop_wl, brk_wl) を返す。"""
    p = Path(path)
    if not p.exists():
        print(f"[ERROR] ファイルが見つかりません: {path}", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stop_wl = list(getattr(mod, _WL_ATTR_STOP, []) or [])
    brk_wl  = list(getattr(mod, _WL_ATTR_BRK,  []) or [])
    return stop_wl, brk_wl


def _run_one(sym, name, strat, calc_fn, em, sm, tm, days, entry_type):
    try:
        df = fetch(sym, days + 120)
        if df is None or len(df) < 30:
            return None
        r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm, days,
                               strat, entry_type=entry_type)
        if not r or not r.get("trade_log"):
            return None
        return sym, name, strat, r["trade_log"]
    except Exception:
        return None


def _fmt_dt(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def collect_trades(days: int, workers: int,
                   stop_wl=None, brk_wl=None) -> list[dict]:
    stop_wl = stop_wl if stop_wl is not None else _STOP_MOD.WATCHLIST
    brk_wl  = brk_wl  if brk_wl  is not None else _BRK_MOD.WATCHLIST

    items = []
    for sym, name, strat in stop_wl:
        if strat not in _STOP_MOD.STRATEGY_PARAMS:
            continue
        p = _STOP_MOD.STRATEGY_PARAMS[strat]
        items.append((sym, name, strat, p[0], p[1], p[2], p[3], days, _STOP_MOD.ENTRY_TYPE))
    for sym, name, strat in brk_wl:
        if strat not in _BRK_MOD.STRATEGY_PARAMS:
            continue
        p = _BRK_MOD.STRATEGY_PARAMS[strat]
        items.append((sym, name, strat, p[0], p[1], p[2], p[3], days, _BRK_MOD.ENTRY_TYPE))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, *it): it for it in items}
        for fut in as_completed(futs):
            r = fut.result()
            if not r:
                continue
            sym, name, strat, trade_log = r
            for t in trade_log:
                if t.get("exit_dt") is None:
                    continue
                if "mae_pct" not in t:
                    continue  # 旧バックテスト結果はスキップ
                results.append({
                    "symbol":   sym,
                    "name":     name,
                    "strategy": strat,
                    "reason":   t["reason"],
                    "entry_dt": _fmt_dt(t.get("entry_dt")),
                    "exit_dt":  _fmt_dt(t.get("exit_dt")),
                    "mae_pct":  t["mae_pct"],
                    "mfe_pct":  t["mfe_pct"],
                    "days_neg": t["days_neg"],
                    "hold_days": t.get("hold_days", 0),
                    "pnl":      t["pnl"],
                    "pct":      t["pct"],
                })
    return results


# ── 集計ヘルパー ──────────────────────────────────────────────────────────────

def _mae_bucket(mae: float) -> str:
    if mae >= 0:       return "① 含み損なし (MAE≥0%)"
    if mae >= -1:      return "② -1%以内"
    if mae >= -3:      return "③ -1〜-3%"
    if mae >= -5:      return "④ -3〜-5%"
    return                    "⑤ -5%超"


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {}
    n = len(trades)
    avg_mae     = sum(t["mae_pct"]  for t in trades) / n
    avg_mfe     = sum(t["mfe_pct"]  for t in trades) / n
    avg_days_neg = sum(t["days_neg"] for t in trades) / n
    avg_hold    = sum(t["hold_days"] for t in trades) / n
    avg_pct     = sum(t["pct"]      for t in trades) / n
    return dict(n=n, avg_mae=avg_mae, avg_mfe=avg_mfe,
                avg_days_neg=avg_days_neg, avg_hold=avg_hold, avg_pct=avg_pct)


# ── CSS / HTML ────────────────────────────────────────────────────────────────

_CSS = """
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:24px}
h1{color:#38bdf8;font-size:1.4rem}
h2{color:#94a3b8;font-size:1.05rem;margin-top:2.5rem}
p.note{color:#64748b;font-size:0.8rem;margin:-0.4rem 0 1rem}
table{border-collapse:separate;border-spacing:0;width:100%;margin-bottom:0.5rem;font-size:0.84rem}
thead th{position:sticky;top:0;z-index:10}
th{background:#1e293b;color:#94a3b8;padding:8px 12px;text-align:right;white-space:nowrap;
   box-shadow:inset 0 -1px 0 #334155, inset 0 1px 0 #334155}
th:first-child{text-align:left}
td{padding:7px 12px;border-bottom:1px solid #1e293b;text-align:right}
td:first-child{text-align:left;color:#e2e8f0;font-weight:600}
tr:hover td{background:#1e293b55}
.good{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}.gray{color:#64748b}
.bar-wrap{display:flex;align-items:center;gap:6px}
.bar{height:12px;border-radius:3px;min-width:2px}
"""

def _bar(pct: float, total: int, color: str) -> str:
    w = max(2, int(pct / 100 * 200))
    n = int(pct / 100 * total)
    return (f'<div class="bar-wrap">'
            f'<div class="bar" style="width:{w}px;background:{color}"></div>'
            f'<span>{pct:.1f}% ({n}件)</span></div>')


def _row(label: str, s: dict, total: int, color: str = "#60a5fa") -> str:
    if not s:
        return f"<tr><td>{label}</td><td colspan='6' class='gray'>データなし</td></tr>"
    mae_c = "bad" if s["avg_mae"] < -3 else ("warn" if s["avg_mae"] < -1 else "good")
    pct = s["n"] / total * 100
    return (f"<tr>"
            f"<td>{label}</td>"
            f"<td>{_bar(pct, total, color)}</td>"
            f"<td class='{mae_c}'>{s['avg_mae']:+.2f}%</td>"
            f"<td class='good'>{s['avg_mfe']:+.2f}%</td>"
            f"<td class='warn'>{s['avg_days_neg']:.1f}日</td>"
            f"<td class='gray'>{s['avg_hold']:.1f}日</td>"
            f"<td class='good'>{s['avg_pct']:+.2f}%</td>"
            f"</tr>")


def _table_head(title: str, note: str = "") -> str:
    return (f"<h2>{title}</h2>"
            + (f"<p class='note'>{note}</p>" if note else "")
            + "<table><thead><tr>"
            "<th style='text-align:left'>区分</th>"
            "<th style='text-align:left'>割合</th>"
            "<th>平均MAE<br><span style='font-size:0.75em;color:#64748b'>最大含み損</span></th>"
            "<th>平均MFE<br><span style='font-size:0.75em;color:#64748b'>最大含み益</span></th>"
            "<th>含み損日数<br><span style='font-size:0.75em;color:#64748b'>平均</span></th>"
            "<th>保有日数<br><span style='font-size:0.75em;color:#64748b'>平均</span></th>"
            "<th>最終損益%<br><span style='font-size:0.75em;color:#64748b'>平均</span></th>"
            "</tr></thead><tbody>")


def build_html(trades: list[dict], days: int) -> str:
    target_trades  = [t for t in trades if "目標" in t["reason"]]
    stop_trades    = [t for t in trades if "損切" in t["reason"]]
    timeout_trades = [t for t in trades if "タイム" in t["reason"]]

    # ── テーブル1: 目標達成トレードのMAE分布 ──────────────────────────
    buckets: dict[str, list] = defaultdict(list)
    for t in target_trades:
        buckets[_mae_bucket(t["mae_pct"])].append(t)

    t1_rows = ""
    for k in sorted(buckets):
        s = _stats(buckets[k])
        colors = {"①": "#4ade80", "②": "#86efac", "③": "#fbbf24", "④": "#f97316", "⑤": "#ef4444"}
        c = colors.get(k[0], "#60a5fa")
        t1_rows += _row(k, s, len(target_trades), c)
    t1 = (_table_head(
        "① 目標達成トレード — 約定後の最大含み損（MAE）分布",
        "目標達成した取引が、途中でどれだけ含み損を抱えていたか。")
        + t1_rows + "</tbody></table>")

    # ── テーブル2: 結果別の比較（目標 / 損切り / タイムカット） ──────
    outcomes = [
        ("🎯 目標達成",   target_trades,  "#4ade80"),
        ("🛑 損切り",     stop_trades,    "#ef4444"),
        ("⏱ タイムカット", timeout_trades, "#94a3b8"),
    ]
    t2_rows = ""
    for label, grp, color in outcomes:
        s = _stats(grp)
        t2_rows += _row(label, s, len(trades), color)
    t2 = (_table_head(
        "② 結果別 MAE / MFE 比較",
        "損切りと目標達成で、最大含み損の深さがどう違うか。")
        + t2_rows + "</tbody></table>")

    # ── テーブル3: 戦略別（目標達成トレードのみ） ───────────────────
    strat_buckets: dict[str, list] = defaultdict(list)
    for t in target_trades:
        strat_buckets[t["strategy"]].append(t)

    t3_rows = ""
    for strat in sorted(strat_buckets):
        s = _stats(strat_buckets[strat])
        t3_rows += _row(strat, s, len(target_trades))
    t3 = (_table_head(
        "③ 戦略別 MAE 比較（目標達成トレードのみ）",
        "RSI2は逆張りのため含み損が大きいはず。DON/MOMは順張りで小さいはず。")
        + t3_rows + "</tbody></table>")

    # ── テーブル4: 含み損日数別（目標達成トレード） ─────────────────
    def _day_bucket(d: int) -> str:
        if d == 0: return "① 0日（一度も含み損なし）"
        if d <= 2: return "② 1〜2日"
        if d <= 5: return "③ 3〜5日"
        if d <= 10: return "④ 6〜10日"
        return             "⑤ 11日以上"

    day_buckets: dict[str, list] = defaultdict(list)
    for t in target_trades:
        day_buckets[_day_bucket(t["days_neg"])].append(t)

    t4_rows = ""
    for k in sorted(day_buckets):
        s = _stats(day_buckets[k])
        t4_rows += _row(k, s, len(target_trades))
    t4 = (_table_head(
        "④ 目標達成トレード — 含み損だった日数の分布",
        "長期間含み損を抱えていても最終的に目標達成するトレードがどれだけあるか。")
        + t4_rows + "</tbody></table>")

    # ── テーブル5: 銘柄ごとの詳細 ───────────────────────────────────
    sym_buckets: dict[tuple, list] = defaultdict(list)
    for t in trades:
        sym_buckets[(t["symbol"], t["name"], t["strategy"])].append(t)

    sym_rows_data = []
    for (sym, name, strat), grp in sym_buckets.items():
        n = len(grp)
        wins = sum(1 for t in grp if "目標" in t["reason"])
        stops = sum(1 for t in grp if "損切" in t["reason"])
        worst_mae = min(t["mae_pct"] for t in grp)
        avg_mae   = sum(t["mae_pct"] for t in grp) / n
        best_mfe  = max(t["mfe_pct"] for t in grp)
        total_pnl = sum(t["pnl"] for t in grp)
        max_days_neg = max(t["days_neg"] for t in grp)
        sym_rows_data.append({
            "sym": sym, "name": name, "strat": strat, "n": n,
            "wr": wins / n * 100, "stops": stops,
            "worst_mae": worst_mae, "avg_mae": avg_mae,
            "best_mfe": best_mfe, "total_pnl": total_pnl,
            "max_days_neg": max_days_neg,
        })
    # 最悪MAE(深い順)でソート
    sym_rows_data.sort(key=lambda r: r["worst_mae"])

    t5_body = ""
    for r in sym_rows_data:
        wr_c  = "good" if r["wr"] >= 55 else ("warn" if r["wr"] >= 45 else "bad")
        mae_c = "bad" if r["worst_mae"] < -5 else ("warn" if r["worst_mae"] < -3 else "good")
        pnl_c = "good" if r["total_pnl"] >= 0 else "bad"
        t5_body += (
            f"<tr>"
            f"<td>{r['sym']}<br><span style='color:#64748b;font-size:0.78em'>{r['name']}</span></td>"
            f"<td class='gray'>{r['strat']}</td>"
            f"<td>{r['n']}</td>"
            f"<td class='{wr_c}'>{r['wr']:.0f}%</td>"
            f"<td class='bad'>{r['worst_mae']:+.1f}%</td>"
            f"<td class='{mae_c}'>{r['avg_mae']:+.1f}%</td>"
            f"<td class='good'>{r['best_mfe']:+.1f}%</td>"
            f"<td class='warn'>{r['max_days_neg']}日</td>"
            f"<td class='{pnl_c}'>{r['total_pnl']:+,.0f}円</td>"
            f"</tr>"
        )
    t5 = (
        "<h2>⑤ 銘柄ごとの含み損詳細（最悪MAE 深い順）</h2>"
        "<p class='note'>銘柄別。最悪MAE = その銘柄の全トレード中で最も深かった含み損。"
        "深い銘柄ほど精神的にきつい / 損切りに引っかかりやすい。</p>"
        "<table><thead><tr>"
        "<th style='text-align:left'>銘柄</th><th style='text-align:left'>戦略</th>"
        "<th>取引数</th><th>勝率</th>"
        "<th>最悪MAE<br><span style='font-size:0.75em;color:#64748b'>最深含み損</span></th>"
        "<th>平均MAE</th>"
        "<th>最良MFE<br><span style='font-size:0.75em;color:#64748b'>最大含み益</span></th>"
        "<th>最長含み損<br><span style='font-size:0.75em;color:#64748b'>日数</span></th>"
        "<th>合計損益</th>"
        "</tr></thead><tbody>" + t5_body + "</tbody></table>"
    )

    # ── テーブル6: 個別トレード全明細 ───────────────────────────────
    detail = sorted(trades, key=lambda t: t["entry_dt"], reverse=True)
    t6_body = ""
    for t in detail:
        if "目標" in t["reason"]:
            r_icon, r_c = "🎯", "good"
        elif "損切" in t["reason"]:
            r_icon, r_c = "🛑", "bad"
        else:
            r_icon, r_c = "⏱", "gray"
        mae_c = "bad" if t["mae_pct"] < -5 else ("warn" if t["mae_pct"] < -3 else "good")
        pnl_c = "good" if t["pnl"] >= 0 else "bad"
        t6_body += (
            f"<tr>"
            f"<td>{t['symbol']}<br><span style='color:#64748b;font-size:0.78em'>{t['name']}</span></td>"
            f"<td class='gray'>{t['strategy']}</td>"
            f"<td class='gray' style='text-align:left'>{t['entry_dt']}<br>"
            f"<span style='font-size:0.78em'>→ {t['exit_dt']}</span></td>"
            f"<td class='{r_c}'>{r_icon}</td>"
            f"<td class='{mae_c}'>{t['mae_pct']:+.1f}%</td>"
            f"<td class='good'>{t['mfe_pct']:+.1f}%</td>"
            f"<td class='warn'>{t['days_neg']}日</td>"
            f"<td class='gray'>{t['hold_days']}日</td>"
            f"<td class='{pnl_c}'>{t['pct']:+.1f}%</td>"
            f"<td class='{pnl_c}'>{t['pnl']:+,.0f}円</td>"
            f"</tr>"
        )
    t6 = (
        f"<h2>⑥ 個別トレード全明細（{len(detail)}件 / エントリー日 新しい順）</h2>"
        "<p class='note'>1取引ごとの含み損(MAE)・含み益(MFE)・含み損日数・最終結果。"
        "「目標達成したが途中で深い含み損だった」具体的なトレードを確認できる。</p>"
        "<table><thead><tr>"
        "<th style='text-align:left'>銘柄</th><th style='text-align:left'>戦略</th>"
        "<th style='text-align:left'>エントリー→決済</th><th>結果</th>"
        "<th>MAE<br><span style='font-size:0.75em;color:#64748b'>最大含み損</span></th>"
        "<th>MFE<br><span style='font-size:0.75em;color:#64748b'>最大含み益</span></th>"
        "<th>含み損<br><span style='font-size:0.75em;color:#64748b'>日数</span></th>"
        "<th>保有<br><span style='font-size:0.75em;color:#64748b'>日数</span></th>"
        "<th>損益%</th><th>損益</th>"
        "</tr></thead><tbody>" + t6_body + "</tbody></table>"
    )

    # ── サマリー数字 ─────────────────────────────────────────────────
    n_all    = len(trades)
    n_target = len(target_trades)
    n_stop   = len(stop_trades)
    n_timeout = len(timeout_trades)
    never_neg = sum(1 for t in target_trades if t["mae_pct"] >= 0)
    pct_never = never_neg / n_target * 100 if n_target else 0
    avg_mae_t = sum(t["mae_pct"] for t in target_trades) / n_target if n_target else 0
    avg_mae_s = sum(t["mae_pct"] for t in stop_trades)   / n_stop   if n_stop   else 0

    summary = f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 24px">
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">総トレード数</div>
    <div style="color:#e2e8f0;font-size:1.4rem;font-weight:700">{n_all}件</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">目標達成中「含み損ゼロ」</div>
    <div style="color:#4ade80;font-size:1.4rem;font-weight:700">{pct_never:.1f}%</div>
    <div style="color:#64748b;font-size:0.75rem">({never_neg}/{n_target}件)</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">目標達成の平均MAE</div>
    <div style="color:#fbbf24;font-size:1.4rem;font-weight:700">{avg_mae_t:+.2f}%</div>
    <div style="color:#64748b;font-size:0.75rem">途中の最大含み損</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <div style="color:#94a3b8;font-size:0.78rem">損切りの平均MAE</div>
    <div style="color:#ef4444;font-size:1.4rem;font-weight:700">{avg_mae_s:+.2f}%</div>
    <div style="color:#64748b;font-size:0.75rem">途中の最大含み損</div>
  </div>
</div>"""

    _side = "ショート" if _IS_SHORT else "ロング"
    _uw   = ("ショート＝株価が約定価格より<strong>上</strong>にある期間"
             if _IS_SHORT else
             "ロング＝株価が約定価格より<strong>下</strong>にある期間")
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>MAE/MFE分析（{_side}）— 約定後含み損推移</title>
<style>{_CSS}</style></head>
<body>
<h1>約定後の含み損推移分析（MAE / MFE）— {_side}</h1>
<p class="note">
  分析期間: 直近{days}日 / 全WATCHLIST /
  目標達成: {n_target}件 / 損切り: {n_stop}件 / タイムカット: {n_timeout}件<br>
  MAE = Maximum Adverse Excursion（保有中の最大含み損率）<br>
  MFE = Maximum Favorable Excursion（保有中の最大含み益率）<br>
  含み損日数 = 含み損を抱えていた日数（{_uw}）
</p>
{summary}
{t1}
{t2}
{t3}
{t4}
{t5}
{t6}
</body></html>"""


if __name__ == "__main__":
    args = _args()
    _side = "ショート" if _IS_SHORT else "ロング"
    print(f"MAE/MFE分析（{_side}）: 直近{args.days}日 / workers={args.workers}", flush=True)

    stop_wl = brk_wl = None
    if args.watchlist:
        stop_wl, brk_wl = _load_watchlist_proposal(args.watchlist)
        print(f"WATCHLIST: {args.watchlist} (stop={len(stop_wl)} / brk={len(brk_wl)})", flush=True)

    print("トレードデータ収集中...", flush=True)
    trades = collect_trades(args.days, args.workers, stop_wl, brk_wl)
    print(f"収集完了: {len(trades)}件", flush=True)

    if not trades:
        print("トレードデータなし（backtest_limit_entry.py を更新して再実行してください）")
        sys.exit(1)

    html = build_html(trades, args.days)
    _suffix = "_short" if _IS_SHORT else ""
    out  = Path(f"mae_analysis{_suffix}_{datetime.now(JST).date()}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML出力: {out}", flush=True)
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())
