"""
run_signals_holdout_all.py — 複数ホールドアウト設定のシグナル＋損益を1画面で確認

30/60/90/150/180日のホールドアウトWF選定WATCHLISTを横断して
今日のシグナルと期間別損益をまとめて表示する。

WATCHLISTの優先順:
  1. ホールドアウト専用CSV  walkforward_{strat}_holdout{N}d_*.csv
  2. 標準WF CSV            walkforward_{strat}_*.csv  (holdoutなし)
  3. フォールバック         check_signals_stop/breakout の WATCHLIST

使い方:
  python run_signals_holdout_all.py
  python run_signals_holdout_all.py --workers 8
  python run_signals_holdout_all.py --no-browser
  python run_signals_holdout_all.py --date 2026-06-09
  python run_signals_holdout_all.py --min-score 60
  python run_signals_holdout_all.py --auto-scan   # CSVなければWFスキャンを自動実行
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 引数先読み ────────────────────────────────────────────────────────────────
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--workers",    type=int,  default=4)
_pre.add_argument("--no-browser", action="store_true")
_pre.add_argument("--date",       type=str,  default=None)
_pre.add_argument("--min-score",  type=int,  default=0)
_pre.add_argument("--wf-dir",     type=Path, default=Path("walkforward_results"))
_pre.add_argument("--auto-scan",  action="store_true",
                  help="ホールドアウトCSVなければWFスキャンを自動実行")
_pre.add_argument("--max-price",  type=float, default=10000.0)
_args, _ = _pre.parse_known_args()

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# 損益分析期間（ホールドアウト設定と対応）
_PNL_PERIODS = [30, 60, 90, 150, 180]

# ── ホールドアウト設定一覧 ────────────────────────────────────────────────────
HOLDOUT_CONFIGS = [
    (30,  "HO30d",  "#3b82f6", "#60a5fa"),
    (60,  "HO60d",  "#06b6d4", "#67e8f9"),
    (90,  "HO90d",  "#10b981", "#6ee7b7"),
    (150, "HO150d", "#f59e0b", "#fcd34d"),
    (180, "HO180d", "#ef4444", "#fca5a5"),
]

# ── シグナルモジュール読み込み ────────────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")
import check_signals_stop     as _stop
import check_signals_breakout as _brk

_STOP_STRATS = {"MACD", "A7", "RSI2"}
_BRK_STRATS  = {"DON", "VOL", "MOM"}

# ── CSVヘルパー ───────────────────────────────────────────────────────────────
def _float(v, default=0.0) -> float:
    try:    return float(v)
    except: return default

def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))

def _find_csv(strategy: str, holdout_days: int, wf_dir: Path,
              mode: str = "conservative") -> tuple[Path | None, str]:
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    suffix = f"{mode_suffix}_holdout{holdout_days}d"
    cands = sorted(wf_dir.glob(f"walkforward_{strategy}{suffix}_*.csv"), reverse=True)
    if cands:
        return cands[0], "holdout"
    fallback = [
        f for f in sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_*.csv"), reverse=True)
        if "holdout" not in f.name
    ]
    if fallback:
        return fallback[0], "standard"
    return None, "none"

def _load_watchlist_from_csv(csv_path: Path, max_price: float, strategy: str,
                              per_strategy: int = 10, max_dd: float = 15.0) -> list[tuple]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    filtered = [r for r in rows if (
        _float(r.get("total_test_pnl", 0)) > 0
        and _float(r.get("max_drawdown_pct", 999)) <= max_dd
        and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
    )]
    filtered.sort(key=_composite_score, reverse=True)
    return [(r.get("symbol", ""), r.get("name", ""), strategy)
            for r in filtered[:per_strategy] if r.get("symbol")]

# ── WATCHLISTの収集 ───────────────────────────────────────────────────────────
source_map: dict[tuple, list] = defaultdict(list)
item_meta:  dict[tuple, tuple] = {}

print("=" * 65)
print(f"run_signals_holdout_all: {TODAY}")
print("=" * 65)

wf_dir = _args.wf_dir
wf_dir.mkdir(exist_ok=True)

for holdout_days, label, col_con, col_agg in HOLDOUT_CONFIGS:
    for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
        mode_short = "con" if mode == "conservative" else "agg"
        src_label  = f"{label}/{mode_short}"

        for strategy in ["MACD", "A7", "RSI2"]:
            csv_path, _ = _find_csv(strategy, holdout_days, wf_dir, mode)
            if csv_path:
                for sym, name, strat in _load_watchlist_from_csv(csv_path, _args.max_price, strategy):
                    k = (sym, strat)
                    source_map[k].append((src_label, color))
                    item_meta[k] = (sym, name, True)

        for strategy in ["DON", "VOL", "MOM"]:
            csv_path, _ = _find_csv(strategy, holdout_days, wf_dir, mode)
            if csv_path:
                for sym, name, strat in _load_watchlist_from_csv(csv_path, _args.max_price, strategy):
                    k = (sym, strat)
                    source_map[k].append((src_label, color))
                    item_meta[k] = (sym, name, False)

if not source_map:
    print("[INFO] WFスキャンCSVなし → check_signals_stop/breakout の WATCHLIST を使用")
    for sym, name, strat in _stop.WATCHLIST:
        k = (sym, strat)
        source_map[k].append(("現行WL", "#6b7280"))
        item_meta[k] = (sym, name, True)
    for sym, name, strat in _brk.WATCHLIST:
        k = (sym, strat)
        source_map[k].append(("現行WL", "#6b7280"))
        item_meta[k] = (sym, name, False)

print(f"ユニーク銘柄×戦略: {len(source_map)}件")

# ── バックテスト＋シグナル確認 (並列) ────────────────────────────────────────
target_date = _args.date

def _collect_one(k: tuple) -> dict | None:
    """全銘柄に対してバックテスト実行＋シグナル確認。シグナルがなくても返す。"""
    sym, strat = k
    sym, name, is_stop = item_meta[k]
    mod = _stop if is_stop else _brk
    try:
        bt = mod.backtest_one(sym, name, strat)
    except Exception:
        return None
    if not bt:
        return None

    rec_score, rec_rank = _stop.calc_recommend_score(bt["period_results"])
    bt_type_fn = getattr(_stop, "calc_bt_type", None)
    bt_type    = bt_type_fn(bt["period_results"]) if bt_type_fn else "?"

    # 365日のトレードログから5期間分を切り出す
    pr_all = bt["period_results"]
    max_key = max(pr_all.keys()) if pr_all else None
    raw_trades = pr_all[max_key].get("trade_log", []) if max_key else []

    ext_periods: dict[int, dict] = {}
    for days in _PNL_PERIODS:
        cutoff = TODAY - timedelta(days=days)
        sub = [t for t in raw_trades if t["signal_dt"].date() >= cutoff]
        if sub:
            wins = sum(1 for t in sub if t["pnl"] > 0)
            gp   = sum(t["pnl"] for t in sub if t["pnl"] > 0)
            gl   = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
            ext_periods[days] = {
                "trades":    len(sub),
                "wins":      wins,
                "win_rate":  wins / len(sub) * 100,
                "pf":        gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0),
                "total_pnl": sum(t["pnl"] for t in sub),
                "trade_log": sub,
            }

    # シグナル確認
    sig_data = None
    sig = mod.check_signal_on_date(sym, strat, target_date)
    if sig and rec_score >= _args.min_score:
        from backtest_limit_entry import ENTRY_EXPIRE, MAX_HOLD
        import pandas as pd
        sig_dt  = sig.get("signal_date")
        order_p = sig.get("order_price", 0)
        stop_p  = sig.get("stop_price",  0)
        tgt_p   = sig.get("target_price", 0)
        try:
            max_exit = pd.bdate_range(
                start=pd.to_datetime(sig_dt),
                periods=ENTRY_EXPIRE + MAX_HOLD + 1
            )[-1].strftime("%Y-%m-%d")
        except Exception:
            max_exit = "—"
        sig_data = {
            "signal_date":  sig_dt,
            "signal_price": sig.get("signal_price", 0),
            "order_p":      order_p,
            "limit_p":      sig.get("limit_entry_price", round(order_p * 1.03) if order_p else 0),
            "stop_p":       stop_p,
            "stop_pct":     (order_p - stop_p) / order_p * 100 if order_p else 0.0,
            "tgt_p":        tgt_p,
            "tgt_pct":      (tgt_p - order_p)  / order_p * 100 if order_p else 0.0,
            "max_exit":     max_exit,
        }

    return {
        "k":           k,
        "sym":         sym,
        "name":        name,
        "strat":       strat,
        "is_stop":     is_stop,
        "rec_score":   rec_score,
        "rec_rank":    rec_rank,
        "bt_type":     bt_type,
        "ext_periods": ext_periods,
        "signal":      sig_data,
        "sources":     source_map[k],
    }

print("バックテスト＋シグナル確認中...", flush=True)
all_results: list[dict] = []
with ThreadPoolExecutor(max_workers=_args.workers) as ex:
    futs = {ex.submit(_collect_one, k): k for k in source_map}
    for fut in as_completed(futs):
        try:
            r = fut.result()
            if r:
                all_results.append(r)
        except Exception:
            pass

signals = sorted([r for r in all_results if r["signal"]], key=lambda x: -x["rec_score"])
print(f"全銘柄バックテスト: {len(all_results)}件 / シグナル: {len(signals)}件")

# ── HTML生成ヘルパー ──────────────────────────────────────────────────────────
_BT_TYPE_COLORS = {"安定": "#10b981", "高WR": "#3b82f6", "高PF": "#f59e0b", "取引数": "#a855f7"}

def _type_badge(bt_type: str, small: bool = False) -> str:
    c = _BT_TYPE_COLORS.get(bt_type, "#94a3b8")
    sz = "0.65rem" if small else "0.75rem"
    return (f'<span style="background:{c}22;color:{c};padding:1px 5px;'
            f'border-radius:3px;font-size:{sz}">{bt_type}</span>')

def _atr_badge(stop_pct: float) -> str:
    if stop_pct > 10:
        return "<span style='background:#ef4444;color:white;padding:1px 4px;border-radius:3px;font-size:9px;margin-left:3px'>ATR高</span>"
    if stop_pct > 7:
        return "<span style='background:#f59e0b;color:#111;padding:1px 4px;border-radius:3px;font-size:9px;margin-left:3px'>ATR↑</span>"
    return ""

def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"

def _pnl_color(v: float) -> str:
    return "#4ade80" if v >= 0 else "#f87171"

def _src_badges(sources: list) -> str:
    seen: set = set()
    html = ""
    for lbl, c in sources:
        if lbl not in seen:
            seen.add(lbl)
            html += (f'<span style="background:{c};color:#0f172a;font-size:0.65rem;font-weight:700;'
                     f'padding:1px 5px;border-radius:3px;white-space:nowrap;display:inline-block;margin:1px 2px">'
                     f'{lbl}</span>')
    return html

# ── タブ1: シグナル ───────────────────────────────────────────────────────────
def _signals_tab_html(signals: list, sig_label: str) -> str:
    if not signals:
        return '<p style="color:#94a3b8;padding:20px">本日のシグナルはありません。</p>'

    # 設定サマリーバッジ
    config_badges = ""
    for holdout_days, label, col_con, col_agg in HOLDOUT_CONFIGS:
        for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
            mode_short = "con" if mode == "conservative" else "agg"
            src = f"{label}/{mode_short}"
            count = sum(1 for s in signals if any(lbl == src for lbl, _ in s["sources"]))
            if count > 0:
                config_badges += (
                    f'<span style="background:{color};color:#0f172a;font-size:0.78rem;font-weight:700;'
                    f'padding:3px 10px;border-radius:4px;margin:2px 3px;display:inline-block">'
                    f'{src}: {count}件</span>'
                )
    if not config_badges:
        config_badges = (
            '<span style="background:#6b7280;color:#fff;font-size:0.78rem;font-weight:700;'
            'padding:3px 10px;border-radius:4px">現行WATCHLIST</span>'
        )

    rows = ""
    for i, s in enumerate(signals, 1):
        rank_cls = {"★★★": "rank-s", "★★": "rank-a", "★": "rank-b"}.get(s["rec_rank"], "rank-c")
        tag = f'<span class="tag tag-{s["strat"].lower()}">{s["strat"]}</span>{_atr_badge(s["signal"]["stop_pct"])}'
        sig = s["signal"]
        lim_pct = (sig["limit_p"] - sig["order_p"]) / sig["order_p"] * 100 if sig["order_p"] else 0
        rows += f"""<tr>
  <td style="text-align:center;font-weight:700;color:#94a3b8">{i}</td>
  <td style="text-align:left">
    <span style="font-weight:700">{s["sym"]}</span><br>
    <span style="color:#64748b;font-size:0.75rem">{s["name"]}</span><br>
    <span style="display:inline-flex;flex-wrap:wrap;gap:2px;margin-top:2px">{_src_badges(s["sources"])}</span>
  </td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">
    <span class="{rank_cls}">{s["rec_rank"]}</span><br>
    <span style="font-size:0.78rem;color:#94a3b8">BT:{s["rec_score"]}</span><br>
    {_type_badge(s["bt_type"], small=True)}
  </td>
  <td style="text-align:right;color:#94a3b8">
    {sig["signal_date"]}<br>
    <span style="font-size:0.72rem">{sig["signal_price"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#38bdf8;font-weight:700">{sig["order_p"]:,.0f}円</td>
  <td style="text-align:right;color:#f59e0b">
    +{lim_pct:.1f}%<br>
    <span style="font-size:0.72rem">{sig["limit_p"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#f87171">
    -{sig["stop_pct"]:.1f}%<br>
    <span style="font-size:0.72rem">{sig["stop_p"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#4ade80">
    +{sig["tgt_pct"]:.1f}%<br>
    <span style="font-size:0.72rem">{sig["tgt_p"]:,.0f}円</span>
  </td>
  <td style="text-align:center;color:#f59e0b;font-size:0.8rem">{sig["max_exit"]}</td>
</tr>"""

    return f"""
<div style="margin-bottom:12px">{config_badges}</div>
<p style="color:#94a3b8;font-size:0.78rem;margin:4px 0 12px">
  ※ 逆指値（青）= 翌日高値がこの価格以上になれば発動 &nbsp;
  ※ 指値上限（橙）= 逆指値→指値の上限。寄付ギャップ超えなら不約定
</p>
<h2 style="color:#60a5fa;font-size:1.05rem;border-left:4px solid #3b82f6;padding-left:10px">
  {sig_label} のシグナル — BTスコア降順
</h2>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>#</th><th style="text-align:left">銘柄 / 設定</th><th>戦略</th><th>BTスコア</th>
    <th>シグナル日<br>時株価</th><th>逆指値<br>(トリガー)</th><th>指値上限<br>(+3%)</th>
    <th>損切り(-)</th><th>目標(+)</th><th>最大決済日</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>"""


# ── タブ2: 損益 ───────────────────────────────────────────────────────────────
def _period_summary(days: int, items: list) -> str:
    """1期間分の損益HTMLを生成。"""
    from datetime import date
    cutoff = TODAY - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    today_str  = TODAY.strftime("%Y-%m-%d")

    # 全トレードを収集（rec_score/bt_typeをアノテーション）
    all_trades: list[dict] = []
    for r in items:
        pd_data = r["ext_periods"].get(days)
        if not pd_data:
            continue
        for t in pd_data.get("trade_log", []):
            all_trades.append({**t, "_sym": r["sym"], "_name": r["name"],
                               "_strat": r["strat"], "_score": r["rec_score"],
                               "_bt_type": r["bt_type"]})

    if not all_trades:
        return f'<p style="color:#94a3b8;padding:8px">この期間（直近{days}日）の取引データがありません。</p>'

    total = len(all_trades)
    wins  = sum(1 for t in all_trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in all_trades)
    gp = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
    total_pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    wr = wins / total * 100

    pnl_c  = _pnl_color(total_pnl)
    wr_c   = "#4ade80" if wr >= 50 else "#f87171"
    pf_c   = "#4ade80" if total_pf >= 1.0 else "#f87171"

    # ── サマリーカード
    summary = f"""
<div style="display:flex;flex-wrap:wrap;gap:12px;margin:10px 0 20px">
  <div class="card"><div class="card-lbl">取引数</div><div class="card-val">{total}件</div></div>
  <div class="card"><div class="card-lbl">勝率</div><div class="card-val" style="color:{wr_c}">{wr:.1f}%</div></div>
  <div class="card"><div class="card-lbl">損益合計</div><div class="card-val" style="color:{pnl_c}">{total_pnl:+,.0f}円</div></div>
  <div class="card"><div class="card-lbl">PF</div><div class="card-val" style="color:{pf_c}">{_pf_str(total_pf)}</div></div>
  <div class="card"><div class="card-lbl">対象銘柄</div><div class="card-val">{len(items)}件</div></div>
</div>"""

    # ── 戦略別サマリー
    strat_agg: dict[str, dict] = {}
    for t in all_trades:
        s = t["_strat"]
        if s not in strat_agg:
            strat_agg[s] = {"trades": 0, "wins": 0, "gp": 0.0, "gl": 0.0}
        strat_agg[s]["trades"] += 1
        strat_agg[s]["wins"]   += 1 if t["pnl"] > 0 else 0
        if t["pnl"] > 0: strat_agg[s]["gp"] += t["pnl"]
        else:             strat_agg[s]["gl"] += abs(t["pnl"])

    strat_order = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    strat_rows = ""
    for s in strat_order:
        d = strat_agg.get(s)
        if not d:
            continue
        s_wr  = d["wins"] / d["trades"] * 100
        s_pf  = d["gp"] / d["gl"] if d["gl"] > 0 else (float("inf") if d["gp"] > 0 else 0.0)
        s_pnl = d["gp"] - d["gl"]
        strat_rows += f"""<tr>
  <td><span class="tag tag-{s.lower()}">{s}</span></td>
  <td style="text-align:right">{d["trades"]}</td>
  <td style="text-align:right">{d["wins"]}</td>
  <td style="text-align:right;color:{_pnl_color(s_wr-50)}">{s_wr:.1f}%</td>
  <td style="text-align:right;color:{_pnl_color(s_pf-1)}">{_pf_str(s_pf)}</td>
  <td style="text-align:right;color:{_pnl_color(s_pnl)}">{s_pnl:+,.0f}円</td>
</tr>"""

    strat_table = f"""
<h3 class="sec-h3">戦略別サマリー</h3>
<div style="overflow-x:auto">
<table style="max-width:600px">
  <thead><tr>
    <th style="text-align:left">戦略</th><th>取引数</th><th>勝数</th>
    <th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{strat_rows}</tbody>
</table>
</div>"""

    # ── BTスコア帯別実績
    BT_BANDS = [(80, 101, "80-100"), (70, 80, "70-79"), (60, 70, "60-69"),
                (40, 60, "40-59"),   (0,  40, "<40")]
    band_rows = ""
    for lo, hi, lbl in BT_BANDS:
        bt = [t for t in all_trades if lo <= t["_score"] < hi]
        if not bt:
            continue
        b_wins  = sum(1 for t in bt if t["pnl"] > 0)
        b_gp    = sum(t["pnl"] for t in bt if t["pnl"] > 0)
        b_gl    = abs(sum(t["pnl"] for t in bt if t["pnl"] < 0))
        b_pnl   = b_gp - b_gl
        b_pf    = b_gp / b_gl if b_gl > 0 else (float("inf") if b_gp > 0 else 0.0)
        b_wr    = b_wins / len(bt) * 100
        # 帯の色
        band_color = ("#4ade80" if lo >= 80 else "#86efac" if lo >= 70
                      else "#fbbf24" if lo >= 60 else "#f87171")
        band_rows += f"""<tr>
  <td><span style="background:{band_color}33;color:{band_color};padding:1px 6px;
      border-radius:3px;font-size:0.78rem;font-weight:700">BT {lbl}</span></td>
  <td style="text-align:right">{len(bt)}</td>
  <td style="text-align:right">{b_wins}</td>
  <td style="text-align:right;color:{_pnl_color(b_wr-50)}">{b_wr:.1f}%</td>
  <td style="text-align:right;color:{_pnl_color(b_pf-1)}">{_pf_str(b_pf)}</td>
  <td style="text-align:right;color:{_pnl_color(b_pnl)}">{b_pnl:+,.0f}円</td>
</tr>"""

    band_table = f"""
<h3 class="sec-h3">BTスコア帯別実績</h3>
<div style="overflow-x:auto">
<table style="max-width:600px">
  <thead><tr>
    <th style="text-align:left">BTスコア帯</th><th>取引数</th><th>勝数</th>
    <th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{band_rows}</tbody>
</table>
</div>"""

    # ── 銘柄別損益
    sym_agg: dict[tuple, dict] = {}
    for t in all_trades:
        key = (t["_sym"], t["_strat"])
        if key not in sym_agg:
            sym_agg[key] = {"sym": t["_sym"], "name": t["_name"], "strat": t["_strat"],
                            "score": t["_score"], "bt_type": t["_bt_type"],
                            "trades": 0, "wins": 0, "gp": 0.0, "gl": 0.0}
        sym_agg[key]["trades"] += 1
        sym_agg[key]["wins"]   += 1 if t["pnl"] > 0 else 0
        if t["pnl"] > 0: sym_agg[key]["gp"] += t["pnl"]
        else:             sym_agg[key]["gl"] += abs(t["pnl"])

    sym_rows_sorted = sorted(sym_agg.values(), key=lambda x: -(x["gp"] - x["gl"]))
    sym_rows = ""
    for i, d in enumerate(sym_rows_sorted, 1):
        s_pnl = d["gp"] - d["gl"]
        s_wr  = d["wins"] / d["trades"] * 100 if d["trades"] > 0 else 0
        s_pf  = d["gp"] / d["gl"] if d["gl"] > 0 else (float("inf") if d["gp"] > 0 else 0.0)
        # 強調枠
        border = ""
        if s_pnl >= 50000:
            border = "border-left:3px solid #4ade80"
        elif s_pnl <= -30000:
            border = "border-left:3px solid #f87171"
        sym_rows += f"""<tr style="{border}">
  <td style="text-align:center;color:#94a3b8">{i}</td>
  <td style="text-align:left">
    <span style="font-weight:700">{d["sym"]}</span><br>
    <span style="color:#64748b;font-size:0.72rem">{d["name"]}</span>
  </td>
  <td style="text-align:center"><span class="tag tag-{d["strat"].lower()}">{d["strat"]}</span></td>
  <td style="text-align:center">
    <span style="font-size:0.8rem;color:#94a3b8">BT:{d["score"]}</span><br>
    {_type_badge(d["bt_type"], small=True)}
  </td>
  <td style="text-align:right">{d["trades"]}</td>
  <td style="text-align:right;color:{_pnl_color(s_wr-50)}">{s_wr:.1f}%</td>
  <td style="text-align:right;color:{_pnl_color(s_pf-1)}">{_pf_str(s_pf)}</td>
  <td style="text-align:right;color:{_pnl_color(s_pnl)};font-weight:700">{s_pnl:+,.0f}円</td>
</tr>"""

    sym_table = f"""
<h3 class="sec-h3">銘柄別損益 <span style="font-size:0.75rem;color:#94a3b8;font-weight:400">（損益降順 / 緑枠=+5万超 / 赤枠=-3万超）</span></h3>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>#</th><th style="text-align:left">銘柄</th><th>戦略</th><th>BTスコア</th>
    <th>取引数</th><th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{sym_rows}</tbody>
</table>
</div>"""

    return f"""
<div style="color:#94a3b8;font-size:0.78rem;margin-bottom:8px">
  期間: {cutoff_str} ～ {today_str} （直近{days}日 / 対象{len(items)}銘柄）
</div>
{summary}
{strat_table}
{band_table}
{sym_table}"""


def _pnl_tab_html(all_results: list) -> str:
    """損益タブ全体HTML。"""
    btns = ""
    panes = ""
    for i, days in enumerate(_PNL_PERIODS):
        active_btn   = "period-btn-active" if i == 0 else ""
        pane_display = "block" if i == 0 else "none"
        label = f"HO{days}d"
        # HO{N}d 専用アイテムがあればそれを使い、なければ全件（フォールバック）
        matched = [r for r in all_results
                   if any(lbl.startswith(label) for lbl, _ in r["sources"])]
        items = matched if matched else all_results

        btns  += (f'<button class="period-btn {active_btn}" '
                  f'onclick="switchPeriod({days})">{days}日</button>\n')
        panes += (f'<div id="pd{days}" class="period-pane" style="display:{pane_display}">'
                  f'{_period_summary(days, items)}</div>\n')

    return f"""
<div style="margin-bottom:16px">
  <span style="color:#94a3b8;font-size:0.8rem;margin-right:10px">分析期間:</span>
  {btns}
</div>
<div id="pnl-panes">
{panes}
</div>"""


# ── メインHTML ────────────────────────────────────────────────────────────────
sig_label = str(_args.date) if _args.date else str(TODAY)
signals_html = _signals_tab_html(signals, sig_label)
pnl_html     = _pnl_tab_html(all_results)

css = """
body { background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;margin:0;padding:20px }
h1   { color:#38bdf8;font-size:1.4rem;margin-bottom:4px }
h3.sec-h3 { color:#60a5fa;font-size:0.95rem;border-left:3px solid #3b82f6;
             padding-left:8px;margin:20px 0 8px }
p.note { color:#94a3b8;font-size:0.78rem;margin:4px 0 }

/* タブ */
.tab-nav { display:flex;gap:4px;margin:16px 0 0;border-bottom:2px solid #334155 }
.tab-btn { background:transparent;border:none;color:#94a3b8;padding:8px 20px;
           cursor:pointer;font-size:0.9rem;border-bottom:2px solid transparent;
           margin-bottom:-2px;transition:color .2s }
.tab-btn:hover { color:#e2e8f0 }
.tab-btn.active { color:#38bdf8;border-bottom-color:#38bdf8;font-weight:700 }
.tab-pane { padding:16px 0 }

/* 期間セレクター */
.period-btn { background:#1e293b;border:1px solid #334155;color:#94a3b8;
              padding:5px 14px;border-radius:4px;cursor:pointer;font-size:0.82rem;
              margin-right:4px;transition:all .2s }
.period-btn:hover { color:#e2e8f0;border-color:#64748b }
.period-btn-active { background:#3b82f6;color:#fff;border-color:#3b82f6;font-weight:700 }

/* テーブル */
table { width:100%;border-collapse:collapse;font-size:0.82rem }
th  { background:#1e293b;color:#94a3b8;padding:8px 10px;text-align:center;
      border-bottom:2px solid #334155;white-space:nowrap }
td  { padding:8px 10px;border-bottom:1px solid #1e293b;vertical-align:top }
tr:hover td { background:#1e293b55 }

/* ランク */
.rank-s { background:#22c55e;color:#0f172a;padding:2px 7px;border-radius:4px;font-weight:700;font-size:0.85rem }
.rank-a { background:#3b82f6;color:#fff;padding:2px 7px;border-radius:4px;font-weight:700;font-size:0.85rem }
.rank-b { background:#f59e0b;color:#0f172a;padding:2px 7px;border-radius:4px;font-weight:700;font-size:0.85rem }
.rank-c { background:#475569;color:#e2e8f0;padding:2px 7px;border-radius:4px;font-weight:700;font-size:0.85rem }

/* 戦略タグ */
.tag { padding:1px 7px;border-radius:3px;font-size:0.78rem;font-weight:700 }
.tag-macd { background:#3b82f6;color:#fff }
.tag-a7   { background:#8b5cf6;color:#fff }
.tag-rsi2 { background:#06b6d4;color:#fff }
.tag-don  { background:#f59e0b;color:#0f172a }
.tag-vol  { background:#10b981;color:#fff }
.tag-mom  { background:#ec4899;color:#fff }

/* サマリーカード */
.card { background:#1e293b;border:1px solid #334155;border-radius:8px;
        padding:12px 18px;min-width:100px }
.card-lbl { color:#94a3b8;font-size:0.75rem;margin-bottom:4px }
.card-val { font-size:1.15rem;font-weight:700;color:#e2e8f0 }
"""

js = """
function switchTab(tab) {
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).style.display = 'block';
  document.getElementById('tbtn-' + tab).classList.add('active');
}
function switchPeriod(days) {
  document.querySelectorAll('.period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('period-btn-active'));
  document.getElementById('pd' + days).style.display = 'block';
  event.target.classList.add('period-btn-active');
}
"""

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ホールドアウト全設定 {sig_label}</title>
<style>{css}</style>
</head>
<body>
<h1>ホールドアウト全設定 シグナル・損益レポート</h1>
<p class="note">{sig_label} &nbsp;|&nbsp; 対象銘柄×戦略: {len(all_results)}件 &nbsp;|&nbsp; シグナル: {len(signals)}件 &nbsp;|&nbsp; workers={_args.workers}</p>

<div class="tab-nav">
  <button id="tbtn-signals" class="tab-btn active" onclick="switchTab('signals')">📋 シグナル ({len(signals)}件)</button>
  <button id="tbtn-pnl"     class="tab-btn"        onclick="switchTab('pnl')">📊 損益分析</button>
</div>

<div id="tab-signals" class="tab-pane">{signals_html}</div>
<div id="tab-pnl"     class="tab-pane" style="display:none">{pnl_html}</div>

<script>{js}</script>
</body>
</html>"""

date_str = _args.date or str(TODAY)
out_path = Path(f"signals_holdout_all_{date_str}.html")
out_path.write_text(html, encoding="utf-8")
print(f"\nレポート生成完了: {out_path.resolve()}")

if not _args.no_browser:
    from _open_html import open_html
    open_html(out_path.resolve())
