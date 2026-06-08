"""
run_signals_holdout_all.py — nikkei_analysis.py のシグナル・損益タブと
完全同一フォーマットで、複数ホールドアウト設定を1画面で確認する。

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
  python run_signals_holdout_all.py --days 180   # 最初に表示する期間 (デフォルト180)
"""
from __future__ import annotations

import argparse
import copy as _copy
import csv
import importlib as _importlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 引数先読み ────────────────────────────────────────────────────────────────
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--workers",    type=int,   default=4)
_pre.add_argument("--no-browser", action="store_true")
_pre.add_argument("--date",       type=str,   default=None)
_pre.add_argument("--min-score",  type=int,   default=0)
_pre.add_argument("--wf-dir",     type=Path,  default=Path("walkforward_results"))
_pre.add_argument("--auto-scan",  action="store_true")
_pre.add_argument("--max-price",  type=float, default=10000.0)
_pre.add_argument("--days",       type=int,   default=180,
                  help="損益タブで最初に表示する期間 (30/60/90/150/180)")
_args, _ = _pre.parse_known_args()

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

_PNL_PERIODS  = [30, 60, 90, 150, 180]
_DEFAULT_DAYS = _args.days if _args.days in _PNL_PERIODS else 180

HOLDOUT_CONFIGS = [
    (30,  "HO30d",  "#3b82f6", "#60a5fa"),
    (60,  "HO60d",  "#06b6d4", "#67e8f9"),
    (90,  "HO90d",  "#10b981", "#6ee7b7"),
    (150, "HO150d", "#f59e0b", "#fcd34d"),
    (180, "HO180d", "#ef4444", "#fca5a5"),
]

# ── TRADING_MODE を import 前に設定 ───────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

# ── CSV ヘルパー ──────────────────────────────────────────────────────────────
def _float(v, default=0.0) -> float:
    try:    return float(v)
    except: return default

def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))

def _find_csv(strategy: str, holdout_days: int, wf_dir: Path,
              mode: str = "conservative") -> tuple[Path | None, str]:
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    cands = sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_holdout{holdout_days}d_*.csv"), reverse=True)
    if cands:
        return cands[0], "holdout"
    fallback = [f for f in sorted(wf_dir.glob(f"walkforward_{strategy}{mode_suffix}_*.csv"), reverse=True)
                if "holdout" not in f.name]
    if fallback:
        return fallback[0], "standard"
    return None, "none"

def _load_wl_from_csv(csv_path: Path, max_price: float, strategy: str,
                       per_strategy: int = 10) -> list[tuple]:
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    filtered = [r for r in rows if (
        _float(r.get("total_test_pnl", 0)) > 0
        and _float(r.get("max_drawdown_pct", 999)) <= 15.0
        and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
    )]
    filtered.sort(key=_composite_score, reverse=True)
    return [(r.get("symbol", ""), r.get("name", ""), strategy)
            for r in filtered[:per_strategy] if r.get("symbol")]

# ── PNL_CONFIGS 構築 ──────────────────────────────────────────────────────────
print("=" * 65)
print(f"run_signals_holdout_all: {TODAY}")
print("=" * 65)

wf_dir = _args.wf_dir
wf_dir.mkdir(exist_ok=True)

# period_days → list of config dicts
_period_configs: dict[int, list[dict]] = {d: [] for d in _PNL_PERIODS}

for holdout_days, ho_label, col_con, col_agg in HOLDOUT_CONFIGS:
    for mode, color in [("conservative", col_con), ("aggressive", col_agg)]:
        mode_short = "con" if mode == "conservative" else "agg"
        stop_wl: list[tuple] = []
        brk_wl:  list[tuple] = []
        has_data = False
        for strat in ["MACD", "A7", "RSI2"]:
            p, _ = _find_csv(strat, holdout_days, wf_dir, mode)
            if p:
                stop_wl.extend(_load_wl_from_csv(p, _args.max_price, strat))
                has_data = True
        for strat in ["DON", "VOL", "MOM"]:
            p, _ = _find_csv(strat, holdout_days, wf_dir, mode)
            if p:
                brk_wl.extend(_load_wl_from_csv(p, _args.max_price, strat))
                has_data = True
        if stop_wl or brk_wl:   # 実際にアイテムがある場合のみ登録
            _period_configs[holdout_days].append({
                "label": f"{ho_label}/{mode_short}",
                "color": color,
                "mode":  mode,
                "sm_tm": None,
                "stop_wl": stop_wl,
                "brk_wl":  brk_wl,
            })

# フォールバック: CSV なし → 現行 WATCHLIST を全期間で共通使用
import check_signals_stop     as _stop
import check_signals_breakout as _brk

_using_fallback = all(len(v) == 0 for v in _period_configs.values())
if _using_fallback:
    print("[INFO] WFスキャンCSVなし → 現行WATCHLISTを全期間で使用")
    _fb_cfg = {
        "label":   "現行WL con",
        "color":   "#3b82f6",
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": list(_stop.WATCHLIST),
        "brk_wl":  list(_brk.WATCHLIST),
    }
    for days in _PNL_PERIODS:
        _period_configs[days] = [_fb_cfg]

# シグナルタブ用: 全設定を重複なしで結合
_seen_cfg_labels: set = set()
_all_configs: list[dict] = []
for cfgs in _period_configs.values():
    for cfg in cfgs:
        if cfg["label"] not in _seen_cfg_labels:
            _seen_cfg_labels.add(cfg["label"])
            _all_configs.append(cfg)

n_items_total = sum(len(c["stop_wl"]) + len(c["brk_wl"]) for c in _all_configs)
print(f"設定数: {len(_all_configs)}件 / アイテム合計: {n_items_total}件")

# ── nikkei_analysis をインポートして PNL_CONFIGS を注入 ───────────────────────
# nikkei_analysis は import 時に _stop/_brk を reload する。
# WATCHLIST が上書きされないよう、reload 後に元に戻す。
_orig_stop_wl = list(_stop.WATCHLIST)
_orig_brk_wl  = list(_brk.WATCHLIST)

import nikkei_analysis as _na

# reload で上書きされた WATCHLIST を元に戻す
_stop.WATCHLIST[:] = _orig_stop_wl
_brk.WATCHLIST[:]  = _orig_brk_wl

# nikkei_analysis にホールドアウト設定を注入
_na._SIGNALS_AVAILABLE = True
_na._PNL_CONFIGS[:] = _all_configs

# ── バックテストキャッシュ (5期間 × 同一銘柄の重複実行を防ぐ) ─────────────────
_bt_cache: dict[tuple, dict | None] = {}

def _make_cached_bt(orig_fn):
    def wrapper(symbol, name, strategy):
        mode = os.environ.get("TRADING_MODE", "conservative")
        key  = (symbol, strategy, mode)
        if key not in _bt_cache:
            _bt_cache[key] = orig_fn(symbol, name, strategy)
        return _bt_cache[key]
    return wrapper

_stop.backtest_one = _make_cached_bt(_stop.backtest_one)
_brk.backtest_one  = _make_cached_bt(_brk.backtest_one)

# ── シグナルタブ HTML ─────────────────────────────────────────────────────────
target_date = None
if _args.date:
    from datetime import date as _date_cls
    try:
        target_date = _date_cls.fromisoformat(_args.date)
    except ValueError:
        pass

date_str = _args.date or str(TODAY)
print("シグナル収集中...", flush=True)
_na._PNL_CONFIGS[:] = _all_configs
_sig_html = _na._tab4_signals_html(
    workers=_args.workers,
    min_score=_args.min_score,
    target_date=target_date,
)

# ── 損益タブ HTML (5期間) ─────────────────────────────────────────────────────
_period_pane_htmls: dict[int, str] = {}
for days in _PNL_PERIODS:
    cfgs = _period_configs.get(days) or _all_configs
    _na._PNL_CONFIGS[:] = cfgs
    print(f"損益集計中 (直近{days}日 / {len(cfgs)}設定)...", flush=True)
    _period_pane_htmls[days] = _na._tab5_pnl_html(days, _args.workers)

# ── 銘柄詳細タブ HTML (シグナル銘柄ごと) ──────────────────────────────────────
# _last_signals はシグナルタブ生成時に _na 側で設定される
_signal_stocks: list[tuple] = []
_seen_sym: set = set()
for _sig in _na._last_signals:
    _s = _sig.get("symbol", "")
    if _s and _s not in _seen_sym:
        _seen_sym.add(_s)
        _signal_stocks.append((_s, _sig.get("name", ""), _sig.get("rec_score") or 0))

_sym_tab_nav   = ""
_sym_tab_panes = ""
for _i, (_sym, _sname, _bt) in enumerate(_signal_stocks):
    _tid     = f"sym_{_sym.replace('.','_')}"
    _active  = "active" if _i == 0 else ""
    _display = "block"  if _i == 0 else "none"
    _short   = _sname[:8] if len(_sname) > 8 else _sname
    _sym_tab_nav += (
        f'<button class="sym-tab-btn {_active}" onclick="switchSymTab(\'{_tid}\')">'
        f'<span style="font-size:0.8rem;font-weight:700">{_sym}</span>'
        f'<br><span style="font-size:0.68rem;color:#94a3b8">{_short}</span>'
        f'<br><span style="font-size:0.7rem;color:#fbbf24">BT:{_bt}</span>'
        f'</button>\n'
    )
    _na._PNL_CONFIGS[:] = _all_configs
    print(f"銘柄詳細生成中: {_sym} {_sname}...", flush=True)
    _sym_pnl = _na._tab5_pnl_html(365, _args.workers, symbol_filter=[_sym])
    _sym_tab_panes += (
        f'<div id="{_tid}" class="sym-tab-pane" style="display:{_display}">'
        f'{_sym_pnl}</div>\n'
    )

# 後片付け
_na._PNL_CONFIGS[:] = _all_configs

# ── 期間セレクターのHTML部品 ──────────────────────────────────────────────────
_period_btns = ""
_period_panes = ""
for days in _PNL_PERIODS:
    active   = "active" if days == _DEFAULT_DAYS else ""
    btn_bg   = "#3b82f6" if days == _DEFAULT_DAYS else ""
    _period_btns += (
        f'<button class="ho-period-btn {active}" '
        f'data-days="{days}" onclick="switchHoPeriod({days})">{days}日</button>\n'
    )
    display = "block" if days == _DEFAULT_DAYS else "none"
    _period_panes += (
        f'<div id="hd{days}" class="ho-period-pane" style="display:{display}">'
        f'{_period_pane_htmls[days]}</div>\n'
    )

# ── フル HTML ─────────────────────────────────────────────────────────────────
_extra_css = """
/* run_signals_holdout_all: outer tab overrides */
.ho-outer-nav {
  display:flex; gap:0; margin:16px 0 0;
  border-bottom:2px solid #1e293b; padding-bottom:0;
}
.ho-outer-btn {
  padding:9px 22px; background:#1e293b; border:none;
  border-radius:6px 6px 0 0; color:#94a3b8;
  cursor:pointer; font-size:0.9rem; transition:all .15s;
  border-bottom:2px solid transparent; margin-bottom:-2px;
}
.ho-outer-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.ho-outer-btn.active { background:#0f172a; color:#60a5fa;
  border-bottom:2px solid #60a5fa; font-weight:700; }
.ho-outer-pane { display:none; padding:12px 0; }
.ho-outer-pane.active { display:block; }

/* 期間セレクター */
.ho-period-btn {
  background:#1e293b; border:1px solid #334155; color:#94a3b8;
  padding:5px 14px; border-radius:4px; cursor:pointer;
  font-size:0.82rem; margin-right:4px; transition:all .2s;
}
.ho-period-btn:hover { color:#e2e8f0; border-color:#64748b; }
.ho-period-btn.active { background:#3b82f6; color:#fff;
  border-color:#3b82f6; font-weight:700; }

/* 銘柄別タブ */
.sym-tab-nav {
  display:flex; flex-wrap:wrap; gap:6px; margin:12px 0 16px;
  padding:10px; background:#0f172a; border-radius:8px;
}
.sym-tab-btn {
  padding:6px 14px; background:#1e293b; border:1px solid #334155;
  color:#e2e8f0; border-radius:6px; cursor:pointer;
  font-size:0.82rem; text-align:center; line-height:1.5;
  transition:all .2s; min-width:90px;
}
.sym-tab-btn:hover { background:#263349; border-color:#64748b; }
.sym-tab-btn.active { background:#1d4ed8; border-color:#3b82f6; }
.sym-tab-pane { display:none; }
"""

_extra_js = """
function switchHoTab(tab) {
  document.querySelectorAll('.ho-outer-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.ho-outer-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('ho-' + tab).classList.add('active');
  event.target.classList.add('active');
}
function switchHoPeriod(days) {
  document.querySelectorAll('.ho-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.ho-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('hd' + days).style.display = 'block';
  event.target.classList.add('active');
}
function switchSymTab(tabId) {
  document.querySelectorAll('.sym-tab-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sym-tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tabId).style.display = 'block';
  event.target.classList.add('active');
}
"""

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ホールドアウト全設定 シグナル・損益 {date_str}</title>
<style>
{_na.CSS}
{_extra_css}
</style>
</head>
<body>
<h1>ホールドアウト全設定 シグナル・損益レポート</h1>
<p class="subtitle">
  基準日: {date_str} &nbsp;|&nbsp;
  設定数: {len(_all_configs)}件 &nbsp;|&nbsp;
  workers={_args.workers}
</p>

<div class="ho-outer-nav">
  <button class="ho-outer-btn active" onclick="switchHoTab('sig')">📋 シグナル</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('pnl')">💹 損益</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('sym')">📊 銘柄詳細（{len(_signal_stocks)}件）</button>
</div>

<div id="ho-sig" class="ho-outer-pane active">
{_sig_html}
</div>

<div id="ho-pnl" class="ho-outer-pane">
  <div style="margin:12px 0 16px">
    <span style="color:#94a3b8;font-size:0.8rem;margin-right:8px">分析期間:</span>
    {_period_btns}
  </div>
  {_period_panes}
</div>

<div id="ho-sym" class="ho-outer-pane">
  <p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 0">
    本日シグナルが出た {len(_signal_stocks)} 銘柄の過去365日取引履歴（BTスコア降順）
  </p>
  <div class="sym-tab-nav">
{_sym_tab_nav}
  </div>
{_sym_tab_panes}
</div>

<script>
{_na.JS}
{_extra_js}
</script>
</body>
</html>"""

out_path = Path(f"signals_holdout_all_{date_str}.html")
out_path.write_text(html, encoding="utf-8")
print(f"\nレポート生成完了: {out_path.resolve()}")

if not _args.no_browser:
    from _open_html import open_html
    open_html(out_path.resolve())
