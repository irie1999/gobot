"""
recent_pnl.py  ―  直近N日 取引損益レポート
==================================================
全シグナルスクリプトの直近N日間に決済された取引を集計して表示する。

使い方:
  python recent_pnl.py              # 直近7日
  python recent_pnl.py --days 14    # 直近14日
  python recent_pnl.py --no-browser # HTML生成のみ
  python recent_pnl.py --workers 8  # 並列数

出力: recent_pnl_YYYY-MM-DD.html
"""
from __future__ import annotations

import argparse
import copy
import importlib
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ── conservative で初期化 ─────────────────────────────────────────────────────
os.environ["TRADING_MODE"] = "conservative"
import check_signals_stop     as _stop
import check_signals_breakout as _brk
_CON_STOP_PARAMS = copy.deepcopy(_stop.STRATEGY_PARAMS)
_CON_BRK_PARAMS  = copy.deepcopy(_brk.STRATEGY_PARAMS)

# ── aggressive params を取得 ──────────────────────────────────────────────────
os.environ["TRADING_MODE"] = "aggressive"
importlib.reload(_stop); importlib.reload(_brk)
_AGG_STOP_PARAMS = copy.deepcopy(_stop.STRATEGY_PARAMS)
_AGG_BRK_PARAMS  = copy.deepcopy(_brk.STRATEGY_PARAMS)

# ── conservative に戻す ───────────────────────────────────────────────────────
os.environ["TRADING_MODE"] = "conservative"
importlib.reload(_stop); importlib.reload(_brk)

from backtest_limit_entry import run_limit_backtest, WORKERS as _DEF_WORKERS

# ── 各スクリプトの watchlist を読み込む (import による STRATEGY_PARAMS 上書きは後で修正) ──
import run_signals_wf as _wf_mod
_WF_STOP = list(_wf_mod._STOP_WATCHLIST)
_WF_BRK  = list(_wf_mod._BRK_WATCHLIST)

import run_signals_prime as _prime_mod        # sets TRADING_MODE=aggressive + sm/tm override
_PRIME_STOP = list(_prime_mod.STOP_WATCHLIST)
_PRIME_BRK  = list(_prime_mod.BRK_WATCHLIST)

import run_signals_aggressive as _agg_mod     # sets TRADING_MODE=aggressive + sm/tm override
_AGGSCRIPT_STOP = list(_agg_mod.STOP_WATCHLIST)
_AGGSCRIPT_BRK  = list(_agg_mod.BRK_WATCHLIST)

import run_signals_nolimit as _nolimit_mod    # sets TRADING_MODE=aggressive + sm/tm override
_NOLIMIT_STOP = list(_nolimit_mod.STOP_WATCHLIST)
_NOLIMIT_BRK  = list(_nolimit_mod.BRK_WATCHLIST)

import run_signals_merged as _merged_mod      # _WF_STOP/_WF_BRK + existing merged
def _dedup_merge(a: list, b: list) -> list:
    seen: set = set()
    result: list = []
    for item in list(a) + list(b):
        key = (item[0], item[2])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

_MERGED_STOP = _dedup_merge(list(_stop.WATCHLIST), list(_merged_mod._WF_STOP))
_MERGED_BRK  = _dedup_merge(list(_brk.WATCHLIST),  list(_merged_mod._WF_BRK))

# ── import で上書きされた STRATEGY_PARAMS を conservative に戻す ──────────────
_stop.STRATEGY_PARAMS.update(_CON_STOP_PARAMS)
_brk.STRATEGY_PARAMS.update(_CON_BRK_PARAMS)

# ── 設定定義 ──────────────────────────────────────────────────────────────────
CONFIGS: list[dict] = [
    {
        "script":   "run_signals.py",
        "label":    "既存版",
        "sublabel": "conservative / デフォルト",
        "color":    "#3498db",
        "mode":     "conservative",
        "sm_tm":    None,
        "stop_wl":  list(_stop.WATCHLIST),
        "brk_wl":   list(_brk.WATCHLIST),
    },
    {
        "script":   "run_signals_wf.py --aggressive",
        "label":    "WF 2026-05-19",
        "sublabel": "aggressive / WF選定 59銘柄",
        "color":    "#e74c3c",
        "mode":     "aggressive",
        "sm_tm":    None,
        "stop_wl":  _WF_STOP,
        "brk_wl":   _WF_BRK,
    },
    {
        "script":   "run_signals_prime.py",
        "label":    "プライム全銘柄",
        "sublabel": "aggressive / sm=1.5 tm=2.0",
        "color":    "#9b59b6",
        "mode":     "aggressive",
        "sm_tm":    (1.5, 2.0),
        "stop_wl":  _PRIME_STOP,
        "brk_wl":   _PRIME_BRK,
    },
    {
        "script":   "run_signals_nolimit.py",
        "label":    "株価制限なし",
        "sublabel": "aggressive / sm=1.5 tm=2.0",
        "color":    "#f39c12",
        "mode":     "aggressive",
        "sm_tm":    (1.5, 2.0),
        "stop_wl":  _NOLIMIT_STOP,
        "brk_wl":   _NOLIMIT_BRK,
    },
    {
        "script":   "run_signals_aggressive.py",
        "label":    "WF 2026-05-12 積極",
        "sublabel": "aggressive / sm=1.5 tm=2.0",
        "color":    "#e67e22",
        "mode":     "aggressive",
        "sm_tm":    (1.5, 2.0),
        "stop_wl":  _AGGSCRIPT_STOP,
        "brk_wl":   _AGGSCRIPT_BRK,
    },
    {
        "script":   "run_signals_merged.py",
        "label":    "WF+既存統合",
        "sublabel": "conservative / 統合WATCHLIST",
        "color":    "#27ae60",
        "mode":     "conservative",
        "sm_tm":    None,
        "stop_wl":  _MERGED_STOP,
        "brk_wl":   _MERGED_BRK,
    },
]


def _set_params(mode: str, sm_tm: tuple | None) -> None:
    if mode == "conservative":
        _stop.STRATEGY_PARAMS.update(_CON_STOP_PARAMS)
        _brk.STRATEGY_PARAMS.update(_CON_BRK_PARAMS)
    else:
        _stop.STRATEGY_PARAMS.update(_AGG_STOP_PARAMS)
        _brk.STRATEGY_PARAMS.update(_AGG_BRK_PARAMS)
    if sm_tm:
        sm, tm = sm_tm
        for k, v in list(_stop.STRATEGY_PARAMS.items()):
            _stop.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)
        for k, v in list(_brk.STRATEGY_PARAMS.items()):
            _brk.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)


def _run_config(cfg: dict, workers: int) -> list[dict]:
    """backtest_one パターンで各watchlistを並列バックテスト"""
    _set_params(cfg["mode"], cfg["sm_tm"])
    all_items: list[dict] = []
    futs = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, name, strat in cfg["stop_wl"]:
            futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
        for sym, name, strat in cfg["brk_wl"]:
            futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
        from concurrent.futures import as_completed
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass
    return all_items


def _collect_trades(items: list[dict], since: date, label: str, color: str) -> list[dict]:
    rows = []
    for it in items:
        sym   = it.get("symbol", "")
        name  = it.get("name", "")
        strat = it.get("strategy", "")
        for t in it.get("trade_log", []):
            exit_dt = t.get("exit_dt")
            if exit_dt is None:
                continue
            exit_d = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
            if exit_d < since:
                continue
            entry_dt = t.get("entry_dt")
            rows.append({
                "label":      label,
                "color":      color,
                "symbol":     sym,
                "name":       name,
                "strategy":   strat,
                "entry_dt":   entry_dt.strftime("%m/%d") if hasattr(entry_dt, "strftime") else str(entry_dt),
                "exit_dt":    exit_dt.strftime("%m/%d")  if hasattr(exit_dt,  "strftime") else str(exit_dt),
                "exit_d_raw": exit_d,
                "entry_p":    t.get("entry_p", 0),
                "exit_p":     t.get("exit_p", 0),
                "pnl":        t.get("pnl", 0),
                "hold_days":  t.get("hold_days", 0),
                "reason":     t.get("reason", ""),
            })
    return rows


# ─── HTML生成 ─────────────────────────────────────────────────────────────────

def _reason_badge(reason: str) -> str:
    badges = {"目標達成": ("✅", "#27ae60"), "損切り": ("🔴", "#e74c3c"), "タイムカット": ("⏱", "#95a5a6")}
    icon, col = badges.get(reason, ("?", "#999"))
    return f'<span style="color:{col};font-weight:bold">{icon} {reason}</span>'


def _summary_rows(all_trades: list[dict]) -> str:
    from collections import defaultdict
    by_label: dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_label[t["label"]].append(t)

    rows_html = ""
    for cfg in CONFIGS:
        label = cfg["label"]
        trades = by_label.get(label, [])
        n      = len(trades)
        wins   = sum(1 for t in trades if t["pnl"] > 0)
        pnl    = sum(t["pnl"] for t in trades)
        wr     = wins / n * 100 if n else 0.0
        gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        pnl_cls = "profit" if pnl >= 0 else "loss"
        dot = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{cfg["color"]};margin-right:6px"></span>'
        rows_html += f"""
        <tr>
          <td>{dot}{label}<br><small style="color:#888">{cfg["sublabel"]}</small></td>
          <td style="text-align:center">{n}</td>
          <td style="text-align:center">{wins}勝 {n-wins}敗</td>
          <td style="text-align:center">{wr:.1f}%</td>
          <td style="text-align:center">{pf_str}</td>
          <td style="text-align:right" class="{pnl_cls}">{pnl:+,.0f}円</td>
        </tr>"""
    return rows_html


def _detail_rows(all_trades: list[dict]) -> str:
    rows_html = ""
    for t in sorted(all_trades, key=lambda x: x["exit_d_raw"], reverse=True):
        pnl_cls = "profit" if t["pnl"] > 0 else "loss"
        dot = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{t["color"]};margin-right:4px;vertical-align:middle"></span>'
        rows_html += f"""
        <tr>
          <td>{dot}<small>{t["label"]}</small></td>
          <td>{t["exit_dt"]}</td>
          <td>{t["symbol"]}<br><small>{t["name"]}</small></td>
          <td><span class="tag tag-{t['strategy'].lower()}">{t["strategy"]}</span></td>
          <td style="text-align:right">{t["entry_p"]:,.0f}</td>
          <td style="text-align:right">{t["exit_p"]:,.0f}</td>
          <td style="text-align:center">{t["hold_days"]}日</td>
          <td style="text-align:right" class="{pnl_cls}">{t["pnl"]:+,.0f}円</td>
          <td>{_reason_badge(t["reason"])}</td>
          <td><small>{t["entry_dt"]}</small></td>
        </tr>"""
    return rows_html


def build_html(all_trades: list[dict], recent_days: int, today_str: str) -> str:
    n_total  = len(all_trades)
    n_win    = sum(1 for t in all_trades if t["pnl"] > 0)
    pnl_sum  = sum(t["pnl"] for t in all_trades)
    wr       = n_win / n_total * 100 if n_total else 0.0
    pnl_cls  = "profit" if pnl_sum >= 0 else "loss"

    summary_rows = _summary_rows(all_trades)
    detail_rows  = _detail_rows(all_trades)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>直近{recent_days}日 取引損益レポート {today_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background: #f0f2f5; color: #333; padding: 20px; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .subtitle {{ color: #888; font-size: 0.85rem; margin-bottom: 20px; }}
  .kpi-bar {{ display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }}
  .kpi {{ background: #fff; border-radius: 10px; padding: 14px 22px; box-shadow: 0 1px 4px rgba(0,0,0,.08); min-width: 140px; }}
  .kpi-label {{ font-size: 0.75rem; color: #888; margin-bottom: 4px; }}
  .kpi-value {{ font-size: 1.5rem; font-weight: 700; }}
  .profit {{ color: #e74c3c; }}
  .loss   {{ color: #3498db; }}
  .card {{ background: #fff; border-radius: 12px; box-shadow: 0 1px 6px rgba(0,0,0,.08); padding: 20px; margin-bottom: 24px; }}
  h2 {{ font-size: 1rem; font-weight: 700; margin-bottom: 14px; border-left: 4px solid #3498db; padding-left: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ background: #f7f8fa; padding: 9px 10px; text-align: left; font-weight: 600; border-bottom: 2px solid #eee; white-space: nowrap; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
  tr:hover td {{ background: #fafbfc; }}
  .tag {{ display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 0.72rem; font-weight: 700; color: #fff; }}
  .tag-macd {{ background: #3498db; }}
  .tag-a7   {{ background: #9b59b6; }}
  .tag-rsi2 {{ background: #e67e22; }}
  .tag-don  {{ background: #27ae60; }}
  .tag-vol  {{ background: #16a085; }}
  .tag-mom  {{ background: #c0392b; }}
  .tag-short{{ background: #7f8c8d; }}
  small {{ color: #888; }}
</style>
</head>
<body>
<h1>📊 直近{recent_days}日 取引損益レポート</h1>
<p class="subtitle">生成日: {today_str} ／ 対象: 全シグナルスクリプト ({len(CONFIGS)}本)</p>

<div class="kpi-bar">
  <div class="kpi"><div class="kpi-label">総取引数</div><div class="kpi-value">{n_total}件</div></div>
  <div class="kpi"><div class="kpi-label">勝率</div><div class="kpi-value">{wr:.1f}%</div></div>
  <div class="kpi"><div class="kpi-label">合計損益</div><div class="kpi-value {pnl_cls}">{pnl_sum:+,.0f}円</div></div>
  <div class="kpi"><div class="kpi-label">勝ち / 負け</div><div class="kpi-value">{n_win}W / {n_total - n_win}L</div></div>
</div>

<div class="card">
  <h2>スクリプト別サマリー</h2>
  <table>
    <thead><tr>
      <th>スクリプト</th><th>取引数</th><th>勝敗</th><th>勝率</th><th>PF</th><th>損益</th>
    </tr></thead>
    <tbody>{summary_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>取引明細（決済日降順）</h2>
  {'<p style="color:#888;padding:10px 0">直近' + str(recent_days) + '日間に決済された取引はありません。</p>' if not all_trades else f'''
  <table>
    <thead><tr>
      <th>スクリプト</th><th>決済日</th><th>銘柄</th><th>戦略</th>
      <th style="text-align:right">約定値</th><th style="text-align:right">決済値</th>
      <th style="text-align:center">保有</th><th style="text-align:right">損益</th>
      <th>理由</th><th>エントリー</th>
    </tr></thead>
    <tbody>{detail_rows}</tbody>
  </table>'''}
</div>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="直近N日 取引損益レポート")
    parser.add_argument("--days",       type=int, default=7,  help="集計する直近日数 (デフォルト: 7)")
    parser.add_argument("--workers",    type=int, default=_DEF_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    today     = datetime.now(JST).date()
    since     = today - timedelta(days=args.days)
    today_str = today.strftime("%Y-%m-%d")

    print(f"直近{args.days}日 取引損益レポート (集計開始: {since})", flush=True)
    print(f"並列数: {args.workers}", flush=True)

    all_trades: list[dict] = []
    for cfg in CONFIGS:
        print(f"  処理中: {cfg['script']} ...", end="", flush=True)
        items  = _run_config(cfg, args.workers)
        trades = _collect_trades(items, since, cfg["label"], cfg["color"])
        all_trades.extend(trades)
        pnl = sum(t["pnl"] for t in trades)
        print(f" {len(trades)}取引  {pnl:+,.0f}円", flush=True)

    print(f"\n合計: {len(all_trades)}取引  損益: {sum(t['pnl'] for t in all_trades):+,.0f}円")

    html = build_html(all_trades, args.days, today_str)
    out  = Path(f"recent_pnl_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML: {out}", flush=True)

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
