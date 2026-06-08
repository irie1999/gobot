"""
run_signals_holdout_all.py — 複数ホールドアウト設定のシグナルを1画面で確認

30/60/90/150/180日のホールドアウトWF選定WATCHLISTを横断して
今日のシグナルをまとめて表示する。

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

# ── ホールドアウト設定一覧 ────────────────────────────────────────────────────
# (holdout_days, label, color_conservative, color_aggressive)
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

def _int(v, default=0) -> int:
    try:    return int(v)
    except: return default

def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))

def _find_csv(strategy: str, holdout_days: int, wf_dir: Path,
              mode: str = "conservative") -> tuple[Path | None, str]:
    """CSVを優先順で探す。(path, source_tag) を返す。"""
    mode_suffix = f"_{mode}" if mode != "conservative" else ""

    # 1. 専用 holdout CSV
    suffix = f"{mode_suffix}_holdout{holdout_days}d"
    cands = sorted(wf_dir.glob(f"walkforward_{strategy}{suffix}_*.csv"), reverse=True)
    if cands:
        return cands[0], "holdout"

    # 2. 通常 WF CSV (holdout なし)
    fallback = [
        f for f in sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_*.csv"), reverse=True)
        if "holdout" not in f.name
    ]
    if fallback:
        return fallback[0], "standard"

    return None, "none"

def _load_watchlist_from_csv(csv_path: Path, max_price: float, strategy: str,
                              per_strategy: int = 10,
                              max_dd: float = 15.0) -> list[tuple[str, str, str]]:
    """CSVから銘柄リストを読んでフィルター・ランキング後に返す。"""
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    filtered = [r for r in rows if (
        _float(r.get("total_test_pnl", 0))          > 0
        and _float(r.get("max_drawdown_pct", 999))  <= max_dd
        and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
    )]
    filtered.sort(key=_composite_score, reverse=True)
    result = []
    for r in filtered[:per_strategy]:
        sym  = r.get("symbol", "")
        name = r.get("name", "")
        if sym:
            result.append((sym, name, strategy))
    return result

def _run_autoscan(strategy: str, holdout_days: int, max_price: float,
                  workers: int, wf_dir: Path, mode: str) -> list[tuple]:
    """scan_walkforward を直接呼んでホールドアウトCSVを生成。"""
    import scan_walkforward as _swf
    import copy
    folds_orig = list(_swf.FOLDS)
    _swf.FOLDS = [
        (n, ts + holdout_days, te + holdout_days, vs + holdout_days, ve + holdout_days)
        for n, ts, te, vs, ve in folds_orig
    ]
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    universe = _swf.load_universe()
    if max_price > 0:
        universe = [(s, n) for s, n in universe if True]  # フィルターはスキャン内で行う

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_swf.walkforward_one, sym, name, strategy, max_price): sym
            for sym, name in universe
        }
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    _swf.FOLDS = folds_orig

    holdout_suffix = f"_holdout{holdout_days}d"
    csv_path = wf_dir / f"walkforward_{strategy}{mode_suffix}{holdout_suffix}_{TODAY}.csv"
    if results:
        import csv as _csv
        keys = list(results[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(results)
        print(f"  [{strategy}/{mode}] CSV保存: {csv_path}")
    return _load_watchlist_from_csv(csv_path, max_price, strategy) if csv_path.exists() else []


# ── WATCHLISTの収集 ───────────────────────────────────────────────────────────
# (sym, strat) → [(label, color), ...]
source_map: dict[tuple, list] = defaultdict(list)
# (sym, strat) → (sym, name, is_stop)
item_meta:  dict[tuple, tuple] = {}
# CSVソース情報 (label/mode → source_tag)
csv_source_info: dict[str, str] = {}

print("=" * 65)
print(f"run_signals_holdout_all: {TODAY}")
print("=" * 65)

wf_dir = _args.wf_dir
wf_dir.mkdir(exist_ok=True)

for holdout_days, label, col_con, col_agg in HOLDOUT_CONFIGS:
    for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
        mode_short = "con" if mode == "conservative" else "agg"
        src_label  = f"{label}/{mode_short}"

        stop_wl: list[tuple] = []
        brk_wl:  list[tuple] = []

        for strategy in ["MACD", "A7", "RSI2"]:
            csv_path, src_tag = _find_csv(strategy, holdout_days, wf_dir, mode)
            if csv_path:
                items = _load_watchlist_from_csv(csv_path, _args.max_price, strategy)
                stop_wl.extend(items)
                csv_source_info[f"{src_label}/{strategy}"] = src_tag
            elif _args.auto_scan:
                items = _run_autoscan(strategy, holdout_days, _args.max_price,
                                      _args.workers, wf_dir, mode)
                stop_wl.extend(items)

        for strategy in ["DON", "VOL", "MOM"]:
            csv_path, src_tag = _find_csv(strategy, holdout_days, wf_dir, mode)
            if csv_path:
                items = _load_watchlist_from_csv(csv_path, _args.max_price, strategy)
                brk_wl.extend(items)
                csv_source_info[f"{src_label}/{strategy}"] = src_tag
            elif _args.auto_scan:
                items = _run_autoscan(strategy, holdout_days, _args.max_price,
                                      _args.workers, wf_dir, mode)
                brk_wl.extend(items)

        for sym, name, strat in stop_wl:
            k = (sym, strat)
            source_map[k].append((src_label, color))
            item_meta[k] = (sym, name, True)
        for sym, name, strat in brk_wl:
            k = (sym, strat)
            source_map[k].append((src_label, color))
            item_meta[k] = (sym, name, False)

# CSVが全くない場合は現在のハードコードWATCHLISTにフォールバック
if not source_map:
    print("[INFO] WFスキャンCSVなし → check_signals_stop/breakout の WATCHLIST を使用")
    FALLBACK_LABEL = "現行WL"
    FALLBACK_COLOR = "#6b7280"
    for sym, name, strat in _stop.WATCHLIST:
        k = (sym, strat)
        source_map[k].append((FALLBACK_LABEL, FALLBACK_COLOR))
        item_meta[k] = (sym, name, True)
    for sym, name, strat in _brk.WATCHLIST:
        k = (sym, strat)
        source_map[k].append((FALLBACK_LABEL, FALLBACK_COLOR))
        item_meta[k] = (sym, name, False)

print(f"ユニーク銘柄×戦略: {len(source_map)}件")

# ── シグナル確認 (並列) ───────────────────────────────────────────────────────
target_date = _args.date

def _check_one(k: tuple) -> dict | None:
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

    sig = mod.check_signal_on_date(sym, strat, target_date)
    if not sig:
        return None
    if rec_score < _args.min_score:
        return None

    from backtest_limit_entry import ENTRY_EXPIRE, MAX_HOLD
    import pandas as pd
    sig_dt = sig.get("signal_date")
    try:
        max_exit = pd.bdate_range(
            start=pd.to_datetime(sig_dt),
            periods=ENTRY_EXPIRE + MAX_HOLD + 1
        )[-1].strftime("%Y-%m-%d")
    except Exception:
        max_exit = "—"

    order_p  = sig.get("order_price", 0)
    stop_p   = sig.get("stop_price",  0)
    tgt_p    = sig.get("target_price", 0)
    stop_pct = (order_p - stop_p) / order_p * 100 if order_p else 0.0
    tgt_pct  = (tgt_p - order_p)  / order_p * 100 if order_p else 0.0

    return {
        "sym":          sym,
        "name":         name,
        "strat":        strat,
        "rec_score":    rec_score,
        "rec_rank":     rec_rank,
        "bt_type":      bt_type,
        "signal_date":  sig_dt,
        "signal_price": sig.get("signal_price", 0),
        "order_p":      order_p,
        "limit_p":      sig.get("limit_entry_price", round(order_p * 1.03) if order_p else 0),
        "stop_p":       stop_p,
        "stop_pct":     stop_pct,
        "tgt_p":        tgt_p,
        "tgt_pct":      tgt_pct,
        "max_exit":     max_exit,
        "sources":      source_map[(sym, strat)],
    }

print("シグナル確認中...", flush=True)
signals = []
with ThreadPoolExecutor(max_workers=_args.workers) as ex:
    futs = {ex.submit(_check_one, k): k for k in source_map}
    for fut in as_completed(futs):
        try:
            r = fut.result()
            if r:
                signals.append(r)
        except Exception:
            pass

signals.sort(key=lambda x: -x["rec_score"])
print(f"シグナル数: {len(signals)}件")

if not signals:
    print("本日のシグナルはありません。")
    sys.exit(0)

# ── HTML生成 ──────────────────────────────────────────────────────────────────
_BT_TYPE_COLORS = {"安定": "#10b981", "高WR": "#3b82f6", "高PF": "#f59e0b", "取引数": "#a855f7"}

def _type_badge(bt_type: str) -> str:
    c = _BT_TYPE_COLORS.get(bt_type, "#94a3b8")
    return (f'<span style="background:{c}22;color:{c};padding:1px 5px;'
            f'border-radius:3px;font-size:0.7rem">{bt_type}</span>')

def _atr_badge(stop_pct: float) -> str:
    if stop_pct > 10:
        return "<span style='background:#ef4444;color:white;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR高</span>"
    if stop_pct > 7:
        return "<span style='background:#f59e0b;color:#111;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR↑</span>"
    return ""

rows_html = ""
for i, s in enumerate(signals, 1):
    rank_cls = {"★★★": "rank-s", "★★": "rank-a", "★": "rank-b"}.get(s["rec_rank"], "rank-c")
    # 重複ソースを除去して表示
    seen_labels: set = set()
    src_html = ""
    for lbl, c in s["sources"]:
        if lbl not in seen_labels:
            seen_labels.add(lbl)
            src_html += (
                f'<span style="background:{c};color:#0f172a;font-size:0.65rem;font-weight:700;'
                f'padding:1px 6px;border-radius:3px;white-space:nowrap;display:inline-block;margin:1px 2px">'
                f'{lbl}</span>'
            )
    tag = f'<span class="tag tag-{s["strat"].lower()}">{s["strat"]}</span>{_atr_badge(s["stop_pct"])}'
    lim_pct = (s["limit_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
    pos_val = s["order_p"] * 100
    rows_html += f"""<tr>
  <td style="text-align:center;font-weight:700;color:#94a3b8">{i}</td>
  <td class="sym" style="text-align:left">
    {s["sym"]}<br>
    <span style="color:#64748b;font-size:0.75rem">{s["name"]}</span><br>
    <span style="display:inline-flex;flex-wrap:wrap;gap:2px;margin-top:3px">{src_html}</span>
  </td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">
    <span class="{rank_cls}">{s["rec_rank"]}</span><br>
    <span style="font-size:0.78rem;color:#94a3b8">BT:{s["rec_score"]}</span><br>
    {_type_badge(s["bt_type"])}
  </td>
  <td style="text-align:right;color:#94a3b8">
    {s["signal_date"]}<br>
    <span style="font-size:0.72rem">{s["signal_price"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#38bdf8;font-weight:700">{s["order_p"]:,.0f}円</td>
  <td style="text-align:right;color:#f59e0b">
    +{lim_pct:.1f}%<br>
    <span style="font-size:0.72rem">{s["limit_p"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#f87171">
    -{s["stop_pct"]:.1f}%<br>
    <span style="font-size:0.72rem">{s["stop_p"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#4ade80">
    +{s["tgt_pct"]:.1f}%<br>
    <span style="font-size:0.72rem">{s["tgt_p"]:,.0f}円</span>
  </td>
  <td style="text-align:right;color:#e2e8f0">
    100株<br>
    <span style="font-size:0.72rem;color:#94a3b8">{pos_val:,.0f}円</span>
  </td>
  <td style="text-align:center;color:#f59e0b;font-size:0.8rem">{s["max_exit"]}</td>
</tr>"""

sig_label = str(_args.date) if _args.date else str(TODAY)

# 設定サマリーバッジ
config_summary = ""
for holdout_days, label, col_con, col_agg in HOLDOUT_CONFIGS:
    for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
        mode_short = "con" if mode == "conservative" else "agg"
        src = f"{label}/{mode_short}"
        count = sum(1 for s in signals if any(lbl == src for lbl, _ in s["sources"]))
        if count > 0:
            config_summary += (
                f'<span style="background:{color};color:#0f172a;font-size:0.78rem;'
                f'font-weight:700;padding:3px 10px;border-radius:4px;margin:3px 4px;'
                f'display:inline-block">{src}: {count}件</span>'
            )
if not config_summary:
    config_summary = (
        '<span style="background:#6b7280;color:#fff;font-size:0.78rem;'
        'font-weight:700;padding:3px 10px;border-radius:4px">現行WATCHLIST フォールバック</span>'
    )

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ホールドアウト全設定シグナル {sig_label}</title>
<style>
  body {{ background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;margin:0;padding:20px }}
  h1   {{ color:#38bdf8;font-size:1.4rem;margin-bottom:4px }}
  h2   {{ color:#60a5fa;font-size:1.1rem;border-left:4px solid #3b82f6;padding-left:10px;margin-top:24px }}
  p.note {{ color:#94a3b8;font-size:0.78rem;margin:4px 0 10px }}
  table {{ width:100%;border-collapse:collapse;font-size:0.82rem }}
  th  {{ background:#1e293b;color:#94a3b8;padding:8px 10px;text-align:center;
         border-bottom:2px solid #334155;white-space:nowrap }}
  td  {{ padding:8px 10px;border-bottom:1px solid #1e293b;vertical-align:top }}
  tr:hover td {{ background:#1e293b55 }}
  .sym    {{ font-weight:700;color:#e2e8f0 }}
  .rank-s {{ background:#22c55e;color:#0f172a;padding:2px 8px;border-radius:4px;font-weight:700;font-size:0.85rem }}
  .rank-a {{ background:#3b82f6;color:#fff;padding:2px 8px;border-radius:4px;font-weight:700;font-size:0.85rem }}
  .rank-b {{ background:#f59e0b;color:#0f172a;padding:2px 8px;border-radius:4px;font-weight:700;font-size:0.85rem }}
  .rank-c {{ background:#475569;color:#e2e8f0;padding:2px 8px;border-radius:4px;font-weight:700;font-size:0.85rem }}
  .tag    {{ padding:1px 7px;border-radius:3px;font-size:0.78rem;font-weight:700 }}
  .tag-macd {{ background:#3b82f6;color:#fff }}
  .tag-a7   {{ background:#8b5cf6;color:#fff }}
  .tag-rsi2 {{ background:#06b6d4;color:#fff }}
  .tag-don  {{ background:#f59e0b;color:#0f172a }}
  .tag-vol  {{ background:#10b981;color:#fff }}
  .tag-mom  {{ background:#ec4899;color:#fff }}
</style>
</head>
<body>
<h1>ホールドアウト全設定 シグナル一覧</h1>
<p class="note">{sig_label} &nbsp;|&nbsp; {len(signals)}件 &nbsp;|&nbsp; workers={_args.workers}</p>
<div style="margin-bottom:16px">{config_summary}</div>
<p class="note">
  ※ 逆指値（青）= 翌日高値がこの価格以上になれば発動 &nbsp;
  ※ 指値上限（橙）= 逆指値→指値の上限。寄付ギャップ超えなら不約定<br>
  ※ HO30d〜HO180d = 各ホールドアウト期間で選定されたWATCHLISTに含まれる設定
</p>

<h2>{sig_label} のシグナル — BTスコア降順</h2>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>#</th>
    <th style="text-align:left">銘柄 / 設定</th>
    <th>戦略</th>
    <th>BTスコア</th>
    <th>シグナル日<br>時株価</th>
    <th>逆指値<br>(トリガー)</th>
    <th>指値上限<br>(+3%)</th>
    <th>損切り(-)</th>
    <th>目標(+)</th>
    <th>株数<br>想定額</th>
    <th>最大決済日</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</div>
</body>
</html>"""

date_str = _args.date or str(TODAY)
out_path = Path(f"signals_holdout_all_{date_str}.html")
out_path.write_text(html, encoding="utf-8")
print(f"\nレポート生成完了: {out_path.resolve()}")

if not _args.no_browser:
    from _open_html import open_html
    open_html(out_path.resolve())
