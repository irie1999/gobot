"""
run_daytrade_holdout_all.py — デイトレ版 シグナル・損益レポート
==================================================================
run_signals_holdout_all.py のデイトレ対応版。
backtest_intraday.py + check_signals_daytrade.py を使用。

使い方:
  python run_daytrade_holdout_all.py
  python run_daytrade_holdout_all.py --both          # conservative + aggressive 統合
  python run_daytrade_holdout_all.py --workers 8
  python run_daytrade_holdout_all.py --no-browser
  python run_daytrade_holdout_all.py --date 2026-06-16
  python run_daytrade_holdout_all.py --days 90       # 最初に表示する期間 (デフォルト 90)
  python run_daytrade_holdout_all.py --max-price 6000 --min-price 1000
  python run_daytrade_holdout_all.py --force         # 当日キャッシュを無視して再生成
  python run_daytrade_holdout_all.py --aggressive    # 積極利確モード (tm=2.0)
  python run_daytrade_holdout_all.py --source local  # データソース指定

当日キャッシュ:
  同一パラメータの出力HTMLが当日すでに存在すれば、重いバックテストをスキップして
  そのファイルを開いて即終了する。--force で強制再生成。
"""
from __future__ import annotations

import argparse
import atexit
import os
import pickle
import subprocess
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 引数先読み ─────────────────────────────────────────────────
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--workers",    type=int,   default=4)
_pre.add_argument("--no-browser", action="store_true")
_pre.add_argument("--date",       type=str,   default=None)
_pre.add_argument("--min-score",  type=int,   default=0)
_pre.add_argument("--max-price",  type=float, default=0.0,
                  help="最新終値の上限 (円/株). 0=制限なし")
_pre.add_argument("--min-price",  type=float, default=0.0,
                  help="最新終値の下限 (円/株). 低位株除外 (例: 1000)")
_pre.add_argument("--days",       type=int,   default=90,
                  help="損益タブで最初に表示する期間 (30/60/90/120/150/180)")
_pre.add_argument("--source",     default="auto",
                  choices=["auto", "local", "yfinance"],
                  help="5分足データソース")
_pre.add_argument("--force",      action="store_true",
                  help="当日の生成済みHTMLがあっても無視して再生成する")
_pre.add_argument("--aggressive", action="store_true",
                  help="積極利確モード (tm=2.0)")
_pre.add_argument("--both",       action="store_true",
                  help="conservative + aggressive 両方を実行して1つのHTMLにまとめる")
_args, _ = _pre.parse_known_args()

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# ── --both モード: conservative + aggressive を統合HTML に ────
if _args.both and not _args.aggressive:
    _bd   = _args.date or str(TODAY)
    _bout = Path(f"daytrade_signals_both_{_bd}.html")

    if _bout.exists() and not _args.force:
        print(f"[CACHE] 当日生成済み(both): {_bout.resolve()}")
        print(f"        再生成するには --force を付けてください。")
        if not _args.no_browser:
            from _open_html import open_html
            open_html(_bout.resolve())
        sys.exit(0)

    _cargs = [a for a in sys.argv[1:] if a not in ("--both", "--aggressive", "--no-browser")]
    if "--force" not in _cargs:
        _cargs.append("--force")
    _cargs.append("--no-browser")

    print("=" * 65)
    print("=== conservative シグナル生成中 ===")
    print("=" * 65)
    subprocess.run([sys.executable, __file__] + _cargs)

    print("=" * 65)
    print("=== aggressive シグナル生成中 ===")
    print("=" * 65)
    subprocess.run([sys.executable, __file__] + _cargs + ["--aggressive"])

    _cf = Path(f"daytrade_signals_{_bd}.html")
    _af = Path(f"daytrade_signals_aggressive_{_bd}.html")
    if not _cf.exists() or not _af.exists():
        print("[ERROR] conservative/aggressive HTML の生成に失敗しました")
        sys.exit(1)

    _bout.write_text(f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>デイトレ シグナル Conservative+Aggressive {_bd}</title>
<style>
body{{margin:0;padding:0;background:#0f172a;font-family:sans-serif}}
.ls-nav{{display:flex;gap:0;border-bottom:2px solid #1e293b;background:#0f172a;
  position:sticky;top:0;z-index:9999;padding:8px 16px 0}}
.ls-btn{{padding:11px 40px;background:#1e293b;border:none;border-radius:6px 6px 0 0;
  color:#94a3b8;cursor:pointer;font-size:1.05rem;font-weight:600;
  border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}}
.ls-btn:hover:not(.active){{background:#263349;color:#e2e8f0}}
.ls-btn.active.con{{color:#34d399;border-bottom:2px solid #34d399;background:#0f172a}}
.ls-btn.active.agg{{color:#f87171;border-bottom:2px solid #f87171;background:#0f172a}}
.ls-frame{{display:none;width:100%;border:none;height:calc(100vh - 54px)}}
.ls-frame.active{{display:block}}
</style>
</head>
<body>
<div class="ls-nav">
  <button class="ls-btn con active" onclick="switchLs('con')">📊 Conservative</button>
  <button class="ls-btn agg" onclick="switchLs('agg')">⚡ Aggressive</button>
</div>
<iframe id="ls-con" class="ls-frame active" src="{_cf.name}"></iframe>
<iframe id="ls-agg" class="ls-frame"        src="{_af.name}"></iframe>
<script>
function switchLs(t){{
  document.querySelectorAll('.ls-frame').forEach(f=>f.classList.remove('active'));
  document.querySelectorAll('.ls-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('ls-'+t).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body>
</html>""", encoding="utf-8")
    print(f"\n統合レポート生成完了: {_bout.resolve()}")
    if not _args.no_browser:
        from _open_html import open_html
        open_html(_bout.resolve())
    sys.exit(0)

# ── モード設定 ─────────────────────────────────────────────────
if _args.aggressive:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

_PNL_PERIODS  = [30, 60, 90, 120, 150, 180]
_DEFAULT_DAYS = _args.days if _args.days in _PNL_PERIODS else 90

# ── 当日キャッシュチェック ──────────────────────────────────────
_cache_date  = _args.date or str(TODAY)
_mode_suffix = "_aggressive" if _args.aggressive else ""
_cached_out  = Path(f"daytrade_signals{_mode_suffix}_{_cache_date}.html")

if _cached_out.exists() and not _args.force:
    print(f"[CACHE] 当日生成済み: {_cached_out.resolve()}")
    print(f"        再生成するには --force を付けてください。")
    if not _args.no_browser:
        from _open_html import open_html
        open_html(_cached_out.resolve())
    sys.exit(0)

# ── インポート ─────────────────────────────────────────────────
from backtest_intraday import (
    BACKTEST_DAYS, FIXED_QTY, FEE_PCT_ONE_WAY, SLIPPAGE_STOP_PCT,
    PERIODS, run_intraday_backtest, calc_recommend_score, apply_atr_penalty,
)
from check_signals_daytrade import (
    WATCHLIST, STRATEGY_PARAMS, check_signal_on_date,
)
from daytrade_data import load_intraday_batch

# ── 価格フィルター適用 WATCHLIST ───────────────────────────────
_WATCHLIST = list(WATCHLIST)

_T0 = _time.time()
def _phase(msg: str):
    print(f"  [⏱ {_time.time() - _T0:5.1f}s] {msg}", flush=True)

print("=" * 65)
print(f"run_daytrade_holdout_all: {TODAY}  mode={os.environ.get('TRADING_MODE')}")
print("=" * 65)

# ── BTキャッシュ (ディスク永続) ────────────────────────────────
_bt_cache_dir  = Path(".daytrade_bt_cache")
_bt_cache_dir.mkdir(exist_ok=True)
_bt_cache_file = _bt_cache_dir / f"bt{_mode_suffix}_{_cache_date}.pkl"

_bt_cache: dict[str, dict | None] = {}

if _bt_cache_file.exists() and _args.force:
    try:
        _bt_cache_file.unlink()
        print(f"[BTキャッシュ] --force により削除: {_bt_cache_file}")
    except Exception:
        pass

if _bt_cache_file.exists():
    try:
        with open(_bt_cache_file, "rb") as _bf:
            _bt_cache = pickle.load(_bf)
        print(f"[BTキャッシュ] {len(_bt_cache)}件をディスクから復元")
    except Exception:
        _bt_cache = {}

_bt_cache_dirty = {"n": 0}

def _save_bt_cache():
    if _bt_cache_dirty["n"] == 0:
        return
    try:
        with open(_bt_cache_file, "wb") as _bf:
            pickle.dump(_bt_cache, _bf, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[BTキャッシュ] {len(_bt_cache)}件を保存 ({_bt_cache_file})")
    except Exception as e:
        print(f"[BTキャッシュ] 保存失敗: {e}")

atexit.register(_save_bt_cache)

# ── 5分足データ取得 ────────────────────────────────────────────
print(f"5分足データ取得中... ({len(_WATCHLIST)}銘柄  source={_args.source})")
_sym_codes = [s for s, _ in _WATCHLIST]
_dfs = load_intraday_batch(_sym_codes, days=BACKTEST_DAYS, source=_args.source)
_phase(f"データ取得完了 {len(_dfs)}/{len(_WATCHLIST)}銘柄")

# ── バックテスト実行 ───────────────────────────────────────────
def _run_one_bt(sym: str, name: str, strategy: str) -> dict | None:
    """1銘柄×1戦略のバックテスト (キャッシュ付き)。"""
    mode = os.environ.get("TRADING_MODE", "conservative")
    key  = f"{sym}|{strategy}|{mode}"
    if key not in _bt_cache:
        df = _dfs.get(sym)
        if df is None or df.empty:
            _bt_cache[key] = None
        else:
            from check_signals_daytrade import STRATEGY_PARAMS as SP
            em, sm, tm = SP[strategy]

            # 価格フィルター: 最新終値で判定
            latest_p = float(df["close"].iloc[-1]) if not df.empty else 0.0
            if _args.max_price > 0 and latest_p > _args.max_price:
                _bt_cache[key] = None
            elif _args.min_price > 0 and latest_p < _args.min_price:
                _bt_cache[key] = None
            else:
                result = run_intraday_backtest(
                    sym, name, df,
                    entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
                    backtest_days=BACKTEST_DAYS, strategy_name=strategy,
                )
                score, rank = calc_recommend_score(result["period_results"])
                trades_30 = result["period_results"].get(30, {}).get("trade_log", [])
                avg_sl = (sum(t.get("stop_loss_pct", 0) for t in trades_30) / len(trades_30)
                          if trades_30 else 0.0)
                adj_score, atr_note = apply_atr_penalty(score, avg_sl)
                result["bt_score"]  = adj_score
                result["bt_rank"]   = rank
                result["atr_note"]  = atr_note
                result["latest_p"]  = latest_p
                _bt_cache[key] = result
        _bt_cache_dirty["n"] += 1
        if _bt_cache_dirty["n"] % 20 == 0:
            _save_bt_cache()
    return _bt_cache[key]

strategies = list(STRATEGY_PARAMS.keys())
tasks = [(sym, name, strat)
         for sym, name in _WATCHLIST
         for strat in strategies]

print(f"バックテスト実行: {len(tasks)}タスク  workers={_args.workers}")
_all_results: list[dict] = []
with ThreadPoolExecutor(max_workers=_args.workers) as ex:
    futures = {ex.submit(_run_one_bt, sym, name, strat): (sym, name, strat)
               for sym, name, strat in tasks}
    for i, fut in enumerate(as_completed(futures), 1):
        sym, name, strat = futures[fut]
        try:
            res = fut.result()
            if res is not None:
                _all_results.append(res)
        except Exception as e:
            print(f"  [warn] {sym} {strat}: {e}", file=sys.stderr)
        if i % 5 == 0 or i == len(tasks):
            print(f"  {i}/{len(tasks)} 完了", flush=True)

_all_results.sort(key=lambda r: r.get("bt_score", 0), reverse=True)
_phase(f"バックテスト完了 {len(_all_results)}件")

# ── シグナル収集 ────────────────────────────────────────────────
from datetime import date as _date_cls
target_date = None
if _args.date:
    try:
        target_date = _date_cls.fromisoformat(_args.date)
    except ValueError:
        pass
date_str = _args.date or str(TODAY)

_signals: dict[tuple[str, str], dict | None] = {}
for res in _all_results:
    sym   = res["symbol"]
    strat = res["strategy"]
    sig   = check_signal_on_date(sym, strat, _dfs.get(sym), target_date)
    _signals[(sym, strat)] = sig

_phase("シグナル収集完了")

# ══════════════════════════════════════════════════════════════
# HTML 生成
# ══════════════════════════════════════════════════════════════

_TRADING_MODE = os.environ.get("TRADING_MODE", "conservative")
_mode_label   = "Conservative (標準)" if _TRADING_MODE == "conservative" else "Aggressive (積極)"

# ── 共通ユーティリティ ──────────────────────────────────────────
def _pf_str(pf: float) -> str:
    if pf == float("inf"): return "∞"
    if pf == 0.0:          return "-"
    return f"{pf:.2f}"

def _pnl_color(v: float) -> str:
    return "#4ade80" if v > 0 else "#f87171" if v < 0 else "#94a3b8"

def _rank_color(rank: str) -> str:
    return {"★★★": "#fbbf24", "★★": "#34d399", "★": "#60a5fa"}.get(rank, "#94a3b8")

# ── シグナルタブ HTML ──────────────────────────────────────────
def _build_signal_tab(results: list[dict], min_score: int) -> str:
    rows = []
    has_sig = False
    for res in results:
        sym   = res["symbol"]
        strat = res["strategy"]
        score = res.get("bt_score", 0)
        rank  = res.get("bt_rank", "-")
        if score < min_score:
            continue

        sig = _signals.get((sym, strat))
        if sig is None:
            continue
        has_sig = True

        pr30 = res["period_results"].get(30, {})
        pr90 = res["period_results"].get(90, {})
        wr30  = pr30.get("win_rate", 0)
        pf30  = pr30.get("pf", 0)
        pnl30 = pr30.get("total_pnl", 0)
        wr90  = pr90.get("win_rate", 0)
        pf90  = pr90.get("pf", 0)

        op = sig["order_price"]
        sp = sig["stop_price"]
        tp = sig["target_price"]
        sl = sig["stop_loss_pct"]

        rank_c  = _rank_color(rank)
        score_s = f'<span style="background:#fef3c722;padding:1px 6px;border-radius:4px">{score}</span>' if score >= 60 else str(score)
        pnl30_c = _pnl_color(pnl30)

        rows.append(f"""
<tr>
  <td style="text-align:center;color:{rank_c};font-weight:700">{rank}<br><small>{score_s}</small></td>
  <td><b style="color:#e2e8f0">{sym.replace('.T','')}</b></td>
  <td style="color:#cbd5e1">{res.get('name','')}</td>
  <td style="color:#60a5fa;font-size:0.8rem">{strat}</td>
  <td style="color:#fbbf24;font-weight:700">{op:,.0f}</td>
  <td style="color:#f87171">{sp:,.0f}</td>
  <td style="color:#4ade80">{tp:,.0f}</td>
  <td style="color:#94a3b8">{sl:.1f}%</td>
  <td>{wr30:.0f}%</td>
  <td>{_pf_str(pf30)}</td>
  <td style="color:{_pnl_color(pnl30)}">{pnl30:+,.0f}</td>
  <td>{wr90:.0f}%</td>
  <td>{_pf_str(pf90)}</td>
</tr>""")

    if not rows:
        body = '<tr><td colspan="13" style="text-align:center;color:#64748b;padding:32px">シグナルなし (データ不足またはmin-score未達)</td></tr>'
    else:
        body = "\n".join(rows)

    return f"""
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:0.82rem">
<thead>
<tr style="background:#1e293b;color:#94a3b8;font-size:0.72rem">
  <th style="padding:8px 10px">BTスコア</th>
  <th>コード</th><th>銘柄名</th><th>戦略</th>
  <th style="color:#fbbf24">発注価格</th>
  <th style="color:#f87171">損切り</th>
  <th style="color:#4ade80">目標</th>
  <th>損切幅</th>
  <th>勝率(30d)</th><th>PF(30d)</th><th>損益(30d)</th>
  <th>勝率(90d)</th><th>PF(90d)</th>
</tr>
</thead>
<tbody>
{body}
</tbody>
</table>
</div>"""

# ── 損益タブ HTML (1期間分) ────────────────────────────────────
def _build_pnl_tab(results: list[dict], days: int) -> str:
    # 全銘柄合算
    all_trades = []
    for res in results:
        pr = res["period_results"].get(days, {})
        for t in pr.get("trade_log", []):
            t2 = dict(t)
            t2["symbol"]   = res["symbol"]
            t2["name"]     = res.get("name", "")
            t2["strategy"] = res.get("strategy", "")
            all_trades.append(t2)
    all_trades.sort(key=lambda t: t["entry_dt"], reverse=True)

    total_n   = len(all_trades)
    total_pnl = sum(t["pnl"] for t in all_trades)
    total_w   = sum(1 for t in all_trades if t["pnl"] > 0)
    total_wr  = total_w / total_n * 100 if total_n > 0 else 0
    total_gp  = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    total_gl  = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
    total_pf  = total_gp / total_gl if total_gl > 0 else (float("inf") if total_gp > 0 else 0.0)

    # ① サマリーバナー
    summary = f"""
<div style="display:flex;gap:24px;flex-wrap:wrap;background:#1e293b;
            padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:0.9rem">
  <div><span style="color:#64748b">期間</span>
       <b style="color:#e2e8f0;margin-left:6px">直近{days}日</b></div>
  <div><span style="color:#64748b">取引数</span>
       <b style="color:#e2e8f0;margin-left:6px">{total_n}</b></div>
  <div><span style="color:#64748b">勝率</span>
       <b style="color:#e2e8f0;margin-left:6px">{total_wr:.0f}%</b></div>
  <div><span style="color:#64748b">PF</span>
       <b style="color:#e2e8f0;margin-left:6px">{_pf_str(total_pf)}</b></div>
  <div><span style="color:#64748b">総損益</span>
       <b style="color:{_pnl_color(total_pnl)};margin-left:6px">{total_pnl:+,.0f}円</b></div>
</div>"""

    # ② 銘柄別サマリーテーブル
    sym_rows = []
    for res in results:
        pr = res["period_results"].get(days, {})
        n   = pr.get("trades", 0)
        if n == 0:
            continue
        wr  = pr.get("win_rate", 0)
        pf  = pr.get("pf", 0)
        pnl = pr.get("total_pnl", 0)
        score = res.get("bt_score", 0)
        rank  = res.get("bt_rank", "-")
        rank_c = _rank_color(rank)
        sym_rows.append(f"""
<tr>
  <td><b style="color:#e2e8f0">{res['symbol'].replace('.T','')}</b></td>
  <td style="color:#cbd5e1">{res.get('name','')}</td>
  <td style="color:#60a5fa;font-size:0.8rem">{res.get('strategy','')}</td>
  <td style="color:{rank_c};font-weight:700">{rank} {score}</td>
  <td>{n}</td>
  <td>{wr:.0f}%</td>
  <td>{_pf_str(pf)}</td>
  <td style="color:{_pnl_color(pnl)};font-weight:700">{pnl:+,.0f}</td>
</tr>""")

    sym_table = f"""
<div style="overflow-x:auto;margin-bottom:24px">
<table style="width:100%;border-collapse:collapse;font-size:0.82rem">
<thead>
<tr style="background:#1e293b;color:#94a3b8;font-size:0.72rem">
  <th style="padding:8px 10px;text-align:left">コード</th>
  <th style="text-align:left">銘柄名</th>
  <th style="text-align:left">戦略</th>
  <th>BTスコア</th>
  <th>取引</th><th>勝率</th><th>PF</th><th>損益</th>
</tr>
</thead>
<tbody>{"".join(sym_rows) if sym_rows else "<tr><td colspan='8' style='text-align:center;color:#64748b;padding:24px'>取引なし</td></tr>"}</tbody>
</table>
</div>"""

    # ③ 取引明細テーブル (最新200件)
    detail_rows = []
    for t in all_trades[:200]:
        reason_c = ("#4ade80" if t["reason"] == "目標達成" else
                    "#f87171" if t["reason"] == "損切り"  else "#94a3b8")
        detail_rows.append(f"""
<tr>
  <td>{t['entry_dt'].strftime('%m/%d %H:%M')}</td>
  <td>{t['exit_dt'].strftime('%H:%M')}</td>
  <td><b>{t.get('symbol','').replace('.T','')}</b></td>
  <td style="color:#94a3b8;font-size:0.78rem">{t.get('name','')[:8]}</td>
  <td>{t['entry_p']:,.0f}</td>
  <td>{t['exit_p']:,.0f}</td>
  <td style="color:{reason_c}">{t['reason']}</td>
  <td style="color:{_pnl_color(t['pnl'])};font-weight:700">{t['pnl']:+,.0f}</td>
</tr>""")

    detail_table = f"""
<h3 style="color:#94a3b8;font-size:0.85rem;margin:16px 0 8px">取引明細 (最新{min(len(all_trades),200)}件)</h3>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:0.8rem">
<thead>
<tr style="background:#1e293b;color:#94a3b8;font-size:0.72rem">
  <th style="padding:7px 10px">エントリー</th><th>決済時刻</th>
  <th>コード</th><th>銘柄</th>
  <th>買値</th><th>売値</th><th>理由</th><th>損益</th>
</tr>
</thead>
<tbody>{"".join(detail_rows) if detail_rows else "<tr><td colspan='8' style='text-align:center;color:#64748b;padding:24px'>取引なし</td></tr>"}</tbody>
</table>
</div>"""

    return summary + sym_table + detail_table

# ── 銘柄詳細タブ HTML ──────────────────────────────────────────
def _build_sym_detail_tab(results: list[dict], signals: dict) -> tuple[str, str]:
    """シグナルが出ている銘柄の詳細 (全期間損益)。"""
    # シグナルが出た銘柄のみ
    sig_results = [r for r in results if signals.get((r["symbol"], r["strategy"])) is not None]
    if not sig_results:
        return "", ""

    sym_nav   = ""
    sym_panes = ""
    for i, res in enumerate(sig_results):
        sym   = res["symbol"]
        strat = res["strategy"]
        score = res.get("bt_score", 0)
        rank  = res.get("bt_rank", "-")
        tid   = f"sym_{sym.replace('.','_')}_{strat}"
        active  = "active" if i == 0 else ""
        display = "block"  if i == 0 else "none"
        short   = res.get("name", "")[:8]

        sym_nav += (
            f'<button class="sym-tab-btn {active}" onclick="switchSymTab(\'{tid}\')">'
            f'<span style="font-size:0.8rem;font-weight:700">{sym}</span>'
            f'<br><span style="font-size:0.68rem;color:#94a3b8">{short}</span>'
            f'<br><span style="font-size:0.7rem;color:#fbbf24">BT:{score} {rank}</span>'
            f'</button>\n'
        )

        # 全期間取引明細
        all_trades = res.get("all_trades", [])
        detail_rows = []
        for t in sorted(all_trades, key=lambda x: x["entry_dt"], reverse=True)[:100]:
            rc = ("#4ade80" if t["reason"] == "目標達成" else
                  "#f87171" if t["reason"] == "損切り"  else "#94a3b8")
            detail_rows.append(
                f'<tr>'
                f'<td>{t["entry_dt"].strftime("%m/%d %H:%M")}</td>'
                f'<td>{t["exit_dt"].strftime("%H:%M")}</td>'
                f'<td>{t["entry_p"]:,.0f}</td><td>{t["exit_p"]:,.0f}</td>'
                f'<td style="color:{rc}">{t["reason"]}</td>'
                f'<td style="color:{_pnl_color(t["pnl"])};font-weight:700">{t["pnl"]:+,.0f}</td>'
                f'</tr>\n'
            )

        # 期間別サマリー
        period_rows = []
        for d in PERIODS:
            pr = res["period_results"].get(d, {})
            n  = pr.get("trades", 0)
            if n == 0:
                continue
            wr  = pr.get("win_rate", 0)
            pf  = pr.get("pf", 0)
            pnl = pr.get("total_pnl", 0)
            period_rows.append(
                f'<tr><td>{d}日</td><td>{n}</td>'
                f'<td>{wr:.0f}%</td><td>{_pf_str(pf)}</td>'
                f'<td style="color:{_pnl_color(pnl)}">{pnl:+,.0f}</td></tr>\n'
            )

        sym_panes += f"""
<div id="{tid}" class="sym-tab-pane" style="display:{display}">
  <div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:16px">
    <div>
      <h3 style="color:#94a3b8;font-size:0.85rem;margin:0 0 8px">期間別成績</h3>
      <table style="border-collapse:collapse;font-size:0.82rem">
        <thead><tr style="background:#1e293b;color:#94a3b8;font-size:0.72rem">
          <th style="padding:6px 10px">期間</th><th>取引</th><th>勝率</th><th>PF</th><th>損益</th>
        </tr></thead>
        <tbody>{"".join(period_rows) if period_rows else "<tr><td colspan='5' style='color:#64748b'>取引なし</td></tr>"}</tbody>
      </table>
    </div>
  </div>
  <h3 style="color:#94a3b8;font-size:0.85rem;margin:0 0 8px">取引明細 (最新100件)</h3>
  <div style="overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:0.8rem">
    <thead><tr style="background:#1e293b;color:#94a3b8;font-size:0.72rem">
      <th style="padding:7px 10px">エントリー</th><th>決済時刻</th>
      <th>買値</th><th>売値</th><th>理由</th><th>損益</th>
    </tr></thead>
    <tbody>{"".join(detail_rows) if detail_rows else "<tr><td colspan='6' style='color:#64748b;padding:24px;text-align:center'>取引なし</td></tr>"}</tbody>
  </table>
  </div>
</div>"""

    return sym_nav, sym_panes

# ── タブ HTML 生成 ─────────────────────────────────────────────
_sig_html = _build_signal_tab(_all_results, _args.min_score)
_phase("シグナルタブ完了")

# 損益タブ: 全設定 (180日) + 期間別
_all_pnl_html  = _build_pnl_tab(_all_results, 180)
_period_pane_htmls = {}
for _d in _PNL_PERIODS:
    _period_pane_htmls[_d] = _build_pnl_tab(_all_results, _d)
    _phase(f"損益タブ({_d}日)完了")

_sym_nav, _sym_panes = _build_sym_detail_tab(_all_results, _signals)
_phase("銘柄詳細タブ完了")

# 期間セレクターのボタン
_period_btns = (
    '<button class="ho-period-btn active" data-days="all" '
    "onclick=\"switchHoPeriod('all')\">全設定 (180日)</button>\n"
)
_period_panes = (
    f'<div id="hdall" class="ho-period-pane" style="display:block">'
    f'{_all_pnl_html}</div>\n'
)
for _d in _PNL_PERIODS:
    active = "active" if _d == _DEFAULT_DAYS else ""
    _period_btns += (
        f'<button class="ho-period-btn" '
        f'data-days="{_d}" onclick="switchHoPeriod({_d})">{_d}日</button>\n'
    )
    _period_panes += (
        f'<div id="hd{_d}" class="ho-period-pane" style="display:none">'
        f'{_period_pane_htmls[_d]}</div>\n'
    )

# シグナル数を数える
_n_signals = sum(1 for v in _signals.values() if v is not None)
_n_sym_detail = sum(1 for r in _all_results if _signals.get((r["symbol"], r["strategy"])))

# ── フル HTML ─────────────────────────────────────────────────
_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Hiragino Sans', 'Yu Gothic', 'Segoe UI', sans-serif;
       font-size: 13px; background: #0f172a; color: #e2e8f0; padding: 16px; }
h1 { font-size: 17px; font-weight: 700; margin-bottom: 4px; color: #f1f5f9; }
.subtitle { color: #64748b; font-size: 12px; margin-bottom: 12px; }
table td, table th { padding: 6px 10px; border-bottom: 1px solid #1e293b; }
table tbody tr:hover td { background: #1e293b55; }

/* outer tabs */
.ho-outer-nav {
  display: flex; gap: 0; margin: 12px 0 0;
  border-bottom: 2px solid #1e293b; padding-bottom: 0;
}
.ho-outer-btn {
  padding: 9px 22px; background: #1e293b; border: none;
  border-radius: 6px 6px 0 0; color: #94a3b8;
  cursor: pointer; font-size: 0.9rem; transition: all .15s;
  border-bottom: 2px solid transparent; margin-bottom: -2px;
}
.ho-outer-btn:hover:not(.active) { background: #263349; color: #e2e8f0; }
.ho-outer-btn.active { background: #0f172a; color: #60a5fa;
  border-bottom: 2px solid #60a5fa; font-weight: 700; }
.ho-outer-pane { display: none; padding: 12px 0; }
.ho-outer-pane.active { display: block; }

/* period buttons */
.ho-period-btn {
  background: #1e293b; border: 1px solid #334155; color: #94a3b8;
  padding: 5px 14px; border-radius: 4px; cursor: pointer;
  font-size: 0.82rem; margin-right: 4px; transition: all .2s;
}
.ho-period-btn:hover { color: #e2e8f0; border-color: #64748b; }
.ho-period-btn.active { background: #3b82f6; color: #fff;
  border-color: #3b82f6; font-weight: 700; }
.ho-period-pane { display: none; }

/* symbol tabs */
.sym-tab-nav {
  display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 16px;
  padding: 10px; background: #0f172a; border-radius: 8px;
}
.sym-tab-btn {
  padding: 6px 14px; background: #1e293b; border: 1px solid #334155;
  color: #e2e8f0; border-radius: 6px; cursor: pointer;
  font-size: 0.82rem; text-align: center; line-height: 1.5;
  transition: all .2s; min-width: 90px;
}
.sym-tab-btn:hover { background: #263349; border-color: #64748b; }
.sym-tab-btn.active { background: #1d4ed8; border-color: #3b82f6; }
.sym-tab-pane { display: none; }
"""

_JS = """
function switchHoTab(tab) {
  document.querySelectorAll('.ho-outer-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.ho-outer-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('ho-' + tab).classList.add('active');
  (event.target.closest('.ho-outer-btn') || event.target).classList.add('active');
}
function switchHoPeriod(days) {
  document.querySelectorAll('.ho-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.ho-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('hd' + days).style.display = 'block';
  (event.target.closest('.ho-period-btn') || event.target).classList.add('active');
}
function switchSymTab(tabId) {
  document.querySelectorAll('.sym-tab-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.sym-tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tabId).style.display = 'block';
  (event.target.closest('.sym-tab-btn') || event.target).classList.add('active');
}
"""

_sym_detail_section = ""
if _sym_nav:
    _sym_detail_section = f"""
<div id="ho-sym" class="ho-outer-pane">
  <p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 12px">
    本日シグナルが出た銘柄の取引履歴
  </p>
  <div class="sym-tab-nav">{_sym_nav}</div>
  {_sym_panes}
</div>"""
    _sym_tab_btn = f'\n  <button class="ho-outer-btn" onclick="switchHoTab(\'sym\')">📊 銘柄詳細 ({_n_sym_detail}件)</button>'
else:
    _sym_tab_btn = ""

_filter_info = []
if _args.max_price > 0: _filter_info.append(f"上限{_args.max_price:,.0f}円")
if _args.min_price > 0: _filter_info.append(f"下限{_args.min_price:,.0f}円")
_filter_str = " / ".join(_filter_info) if _filter_info else "なし"

html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>デイトレ シグナル・損益 {date_str}</title>
<style>
{_CSS}
</style>
</head>
<body>
<h1>デイトレ シグナル・損益レポート</h1>
<p class="subtitle">
  基準日: {date_str} &nbsp;|&nbsp;
  モード: {_mode_label} &nbsp;|&nbsp;
  銘柄数: {len(_all_results)}件 &nbsp;|&nbsp;
  シグナル: {_n_signals}件 &nbsp;|&nbsp;
  価格フィルター: {_filter_str} &nbsp;|&nbsp;
  スリッページ: {SLIPPAGE_STOP_PCT*100:.1f}% / 手数料: {FEE_PCT_ONE_WAY*100:.3f}%片道
</p>

<div class="ho-outer-nav">
  <button class="ho-outer-btn active" onclick="switchHoTab('sig')">📋 シグナル</button>
  <button class="ho-outer-btn"        onclick="switchHoTab('pnl')">💹 損益</button>{_sym_tab_btn}
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
{_sym_detail_section}

<script>
{_JS}
</script>
</body>
</html>"""

# ── 出力 ─────────────────────────────────────────────────────
_cached_out.write_text(html, encoding="utf-8")
_phase(f"HTML生成完了: {_cached_out.resolve()}")
print(f"\nレポート生成完了: {_cached_out.resolve()}")

if not _args.no_browser:
    try:
        from _open_html import open_html
        open_html(_cached_out.resolve())
    except ImportError:
        import webbrowser
        webbrowser.open(_cached_out.resolve().as_uri())
