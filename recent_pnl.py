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
        # backtest_one は period_results[days]["trade_log"] に格納
        period_results = it.get("period_results", {})
        if not period_results:
            continue
        # 最長期間のtrade_logを使って exit_dt でフィルター
        max_period = max(period_results.keys())
        trade_log  = period_results[max_period].get("trade_log", [])

        seen = set()  # 同一取引の重複排除
        for t in trade_log:
            exit_dt = t.get("exit_dt")
            if exit_dt is None:
                continue
            exit_d = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
            if exit_d < since:
                continue
            entry_dt = t.get("entry_dt")
            key = (sym, strat, entry_dt, exit_dt)
            if key in seen:
                continue
            seen.add(key)
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
                "reason":     t.get("reason", "") or "保有中",
            })
    return rows


# ─── HTML生成 ─────────────────────────────────────────────────────────────────

def _reason_cell(reason: str) -> str:
    if reason == "目標達成":
        return '<span style="color:#4ade80;font-weight:600">目標達成</span>'
    if reason == "損切り":
        return '<span style="color:#f87171;font-weight:600">損切り</span>'
    if reason == "タイムカット":
        return '<span style="color:#94a3b8">タイムカット</span>'
    return f'<span style="color:#fbbf24">{reason}</span>'


def _summary_rows(all_trades: list[dict]) -> str:
    from collections import defaultdict
    by_label: dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_label[t["label"]].append(t)

    rows_html = ""
    for cfg in CONFIGS:
        label  = cfg["label"]
        trades = by_label.get(label, [])
        n      = len(trades)
        wins   = sum(1 for t in trades if t["pnl"] > 0)
        pnl    = sum(t["pnl"] for t in trades)
        wr     = wins / n * 100 if n else 0.0
        gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        pnl_cls = "profit" if pnl >= 0 else "loss"
        dot = f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{cfg["color"]};margin-right:6px;vertical-align:middle"></span>'
        rows_html += f"""
        <tr>
          <td class="sym">{dot}{label}<br><span style="color:#64748b;font-size:0.75rem;font-weight:400">{cfg["sublabel"]}</span></td>
          <td>{n}</td><td>{wins}</td>
          <td>{"—" if n == 0 else f"{wr:.1f}%"}</td>
          <td>{"—" if n == 0 else pf_str}</td>
          <td class="{pnl_cls}">{"—" if n == 0 else f"{pnl:+,.0f}円"}</td>
        </tr>"""
    return rows_html


def _trade_table(trades: list[dict], colspan: int = 9) -> str:
    if not trades:
        return f'<tr><td colspan="{colspan}" style="text-align:center;color:#64748b;padding:16px">該当取引なし</td></tr>'
    rows = ""
    for t in sorted(trades, key=lambda x: x["exit_d_raw"], reverse=True):
        pnl_cls = "profit" if t["pnl"] > 0 else "loss"
        tag = f'<span class="tag tag-{t["strategy"].lower()}">{t["strategy"]}</span>'
        rows += f"""
        <tr>
          <td>{t["exit_dt"]}</td>
          <td class="sym">{t["symbol"]}<br><span style="color:#64748b;font-size:0.75rem">{t["name"]}</span></td>
          <td>{tag}</td>
          <td>{t["entry_p"]:,.0f}</td>
          <td>{t["exit_p"]:,.0f}</td>
          <td>{t["hold_days"]}日</td>
          <td class="{pnl_cls}">{t["pnl"]:+,.0f}円</td>
          <td>{_reason_cell(t["reason"])}</td>
          <td style="color:#94a3b8">{t["entry_dt"]}</td>
        </tr>"""
    return rows


def _tab_detail_section(all_trades: list[dict], recent_days: int) -> str:
    from collections import defaultdict
    by_label: dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_label[t["label"]].append(t)

    thead = """<thead><tr>
      <th>決済日</th>
      <th style="text-align:left">銘柄</th>
      <th>戦略</th>
      <th>約定値</th><th>決済値</th><th>保有</th><th>損益</th><th>理由</th><th>エントリー</th>
    </tr></thead>"""

    # タブナビ
    nav = '<div class="tab-nav">'
    nav += '<button class="tab-btn active" onclick="switchTab(this,\'tab-all\')">全体</button>'
    for cfg in CONFIGS:
        tid   = f"tab-{cfg['label'].replace(' ', '_').replace('(', '').replace(')', '').replace('-', '_').replace('.', '_')}"
        label = cfg["label"]
        trades = by_label.get(label, [])
        n      = len(trades)
        pnl    = sum(t["pnl"] for t in trades)
        pnl_cls = "profit" if pnl >= 0 else "loss"
        badge  = f'<span style="margin-left:6px;font-size:0.72rem;color:#94a3b8">{n}件</span>'
        badge += f' <span style="font-size:0.72rem" class="{pnl_cls}">{"—" if n==0 else f"{pnl:+,.0f}"}</span>'
        dot    = f'<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{cfg["color"]};margin-right:5px;vertical-align:middle"></span>'
        nav += f'<button class="tab-btn" onclick="switchTab(this,\'{tid}\')">{dot}{label}{badge}</button>'
    nav += '</div>'

    # 全体タブ
    panes = f'<div id="tab-all" class="tab-pane active"><table>{thead}<tbody>{_trade_table(all_trades)}</tbody></table></div>'

    # スクリプト別タブ
    for cfg in CONFIGS:
        tid    = f"tab-{cfg['label'].replace(' ', '_').replace('(', '').replace(')', '').replace('-', '_').replace('.', '_')}"
        trades = by_label.get(cfg["label"], [])
        n      = len(trades)
        wins   = sum(1 for t in trades if t["pnl"] > 0)
        pnl    = sum(t["pnl"] for t in trades)
        pnl_cls = "profit" if pnl >= 0 else "loss"
        gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
        wr     = wins / n * 100 if n else 0.0
        stat   = (f'<div class="fill-stat">{n}取引 ／ 勝率 {wr:.1f}% ／ PF {pf_str} ／ '
                  f'損益 <span class="{pnl_cls}">{"—" if n==0 else f"{pnl:+,.0f}円"}</span></div>')
        panes += f'<div id="{tid}" class="tab-pane">{stat}<table>{thead}<tbody>{_trade_table(trades)}</tbody></table></div>'

    return nav + panes


def build_html(all_trades: list[dict], recent_days: int, today_str: str) -> str:
    n_total = len(all_trades)
    n_win   = sum(1 for t in all_trades if t["pnl"] > 0)
    pnl_sum = sum(t["pnl"] for t in all_trades)
    wr      = n_win / n_total * 100 if n_total else 0.0
    pnl_cls = "profit" if pnl_sum >= 0 else "loss"

    summary_rows    = _summary_rows(all_trades)
    tab_detail_html = _tab_detail_section(all_trades, recent_days)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>直近{recent_days}日 取引損益レポート — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym  {{ text-align:left; font-weight:600; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-macd  {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7    {{ background:#065f46; color:#a7f3d0; }}
  .tag-rsi2  {{ background:#7c3aed; color:#ddd6fe; }}
  .tag-don   {{ background:#0f766e; color:#99f6e4; }}
  .tag-vol   {{ background:#b45309; color:#fde68a; }}
  .tag-mom   {{ background:#be185d; color:#fbcfe8; }}
  .tag-short {{ background:#374151; color:#d1d5db; }}
  .kpi-bar {{ display:flex; gap:16px; margin-bottom:28px; flex-wrap:wrap; }}
  .kpi {{ background:#1e293b; border:1px solid #334155; border-radius:10px; padding:14px 22px; min-width:140px; }}
  .kpi-label {{ font-size:0.75rem; color:#94a3b8; margin-bottom:4px; }}
  .kpi-value {{ font-size:1.5rem; font-weight:700; color:#e2e8f0; }}
  .tab-nav {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:16px; border-bottom:1px solid #334155; padding-bottom:10px; }}
  .tab-btn {{ background:#1e293b; color:#94a3b8; border:1px solid #334155; border-radius:6px; padding:5px 14px;
              cursor:pointer; font-size:0.82rem; transition:all .15s; white-space:nowrap; }}
  .tab-btn:hover {{ background:#273549; color:#e2e8f0; }}
  .tab-btn.active {{ background:#1d4ed8; color:#fff; border-color:#1d4ed8; }}
  .tab-pane {{ display:none; }}
  .tab-pane.active {{ display:block; }}
  .fill-stat {{ color:#38bdf8; font-size:0.82rem; margin-bottom:8px; }}
</style>
</head>
<body>
<h1>直近{recent_days}日 取引損益レポート</h1>
<p class="subtitle">生成日: {today_str} ／ 対象: 全シグナルスクリプト ({len(CONFIGS)}本)</p>

<div class="kpi-bar">
  <div class="kpi"><div class="kpi-label">総取引数</div><div class="kpi-value">{n_total}件</div></div>
  <div class="kpi"><div class="kpi-label">勝率</div><div class="kpi-value">{"—" if n_total==0 else f"{wr:.1f}%"}</div></div>
  <div class="kpi"><div class="kpi-label">合計損益</div><div class="kpi-value {pnl_cls}">{"—" if n_total==0 else f"{pnl_sum:+,.0f}円"}</div></div>
  <div class="kpi"><div class="kpi-label">勝ち / 負け</div><div class="kpi-value">{n_win}W / {n_total - n_win}L</div></div>
</div>

<h2>スクリプト別サマリー（直近{recent_days}日）</h2>
<table>
  <thead><tr>
    <th style="text-align:left">スクリプト</th>
    <th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>取引明細（決済日降順）</h2>
{tab_detail_html}

<script>
function switchTab(btn, id) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
}}
</script>
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
