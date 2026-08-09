"""
nikkei_analysis_embargo2config.py  ―  embargo WATCHLIST 2config 統合レポート
=============================================================================
apply_watchlist.py で適用した embargo WATCHLIST の
conservative / aggressive を1枚のHTMLで比較分析する。

  conservative : check_signals_stop.py / check_signals_breakout.py の現 WATCHLIST
  aggressive   : 最新 watchlist_proposal_aggressive_*.py を自動検出

nikkei_analysis_4config.py と同一フォーマット (タブ1〜7)。

使い方:
  python nikkei_analysis_embargo2config.py
  python nikkei_analysis_embargo2config.py --days 180
  python nikkei_analysis_embargo2config.py --no-browser
  python nikkei_analysis_embargo2config.py --workers 8
  python nikkei_analysis_embargo2config.py --date 2026-01-01
  python nikkei_analysis_embargo2config.py --symbol 7203.T --strategy MACD

出力:
  nikkei_analysis_embargo2config_{date}.html
"""
from __future__ import annotations

import argparse
import copy as _copy
import importlib as _importlib
import importlib.util
import os
import re as _re
import sys
import webbrowser
from _open_html import open_html
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))

# ── TRADING_MODE を import 前に conservative に設定 ─────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop     as _stop
import check_signals_breakout as _brk

# ── モード別パラメータをスナップショット ──────────────────────────────────────
_CON_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_CON_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "aggressive"
_importlib.reload(_stop); _importlib.reload(_brk)
_AGG_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_AGG_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "conservative"
_importlib.reload(_stop); _importlib.reload(_brk)

# ── conservative WATCHLIST: 現 check_signals ファイルから ──────────────────
_CON_STOP_WL: list[tuple[str, str, str]] = list(_stop.WATCHLIST)
_CON_BRK_WL:  list[tuple[str, str, str]] = list(_brk.WATCHLIST)


# ── aggressive WATCHLIST: 最新 watchlist_proposal_aggressive_*.py から ─────
def _load_aggressive_wl() -> tuple[list, list]:
    """最新の aggressive 提案ファイルを自動検出して STOP/BRK を返す。"""
    date_pat = _re.compile(r"(\d{4}-\d{2}-\d{2})\.py$")
    dated = []
    for p in Path(".").glob("watchlist_proposal_aggressive_*.py"):
        m = date_pat.search(p.name)
        if m:
            dated.append((m.group(1), p))
    if not dated:
        print("[WARN] watchlist_proposal_aggressive_*.py が見つかりません。"
              " aggressive WATCHLIST は conservative と同じ内容を使います。")
        return list(_CON_STOP_WL), list(_CON_BRK_WL)

    dated.sort(key=lambda x: x[0], reverse=True)
    proposal_path = dated[0][1]
    print(f"aggressive WATCHLIST 自動検出: {proposal_path}")

    spec = importlib.util.spec_from_file_location("_agg_proposal", proposal_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stop_wl = getattr(mod, "STOP_WATCHLIST", [])
    brk_wl  = getattr(mod, "BRK_WATCHLIST",  [])
    return list(stop_wl), list(brk_wl)


_AGG_STOP_WL, _AGG_BRK_WL = _load_aggressive_wl()

# ── 2 CONFIG 定義 ──────────────────────────────────────────────────────────
_CON_LABEL = "embargo conservative"
_AGG_LABEL = "embargo aggressive"

_RISK = {
    _CON_LABEL: "低中",
    _AGG_LABEL: "中",
}
_NOTE = {
    _CON_LABEL: f"embargo WFスキャン × conservative (2R設定)。全相場で安定。逆指値B:{len(_CON_STOP_WL)}件 BRK:{len(_CON_BRK_WL)}件",
    _AGG_LABEL: f"embargo WFスキャン × aggressive (1.5R設定)。上昇相場で回転率重視。逆指値B:{len(_AGG_STOP_WL)}件 BRK:{len(_AGG_BRK_WL)}件",
}
RISK_COLOR = {"高": "#f87171", "中高": "#fb923c", "中": "#fbbf24", "低中": "#86efac", "低": "#4ade80"}
STATUS_META = {
    "✅ 推奨": ("推奨", "#4ade80", "#052e16", "#166534"),
    "⚠️ 注意": ("注意", "#fbbf24", "#2d1f00", "#92400e"),
    "❌ 停止": ("停止", "#f87171", "#2d0a0a", "#991b1b"),
}

# ── reload フック: WATCHLISTを conservative で保持 ───────────────────────────
_orig_reload = _importlib.reload

def _wl_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST = list(_CON_STOP_WL)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST = list(_CON_BRK_WL)
    return result

_importlib.reload = _wl_preserving_reload
import nikkei_analysis as _na
_importlib.reload = _orig_reload

# ── _PNL_CONFIGS を 2config に差し替え ────────────────────────────────────
_na._PNL_CONFIGS[:] = [
    {"label": _CON_LABEL, "color": "#3498db", "mode": "conservative",
     "sm_tm": None, "stop_wl": list(_CON_STOP_WL), "brk_wl": list(_CON_BRK_WL)},
    {"label": _AGG_LABEL, "color": "#e74c3c", "mode": "aggressive",
     "sm_tm": None, "stop_wl": list(_AGG_STOP_WL), "brk_wl": list(_AGG_BRK_WL)},
]

try:
    from backtest_limit_entry import WORKERS as _DEF_WORKERS
except ImportError:
    _DEF_WORKERS = 4

CONFIGS = _na._PNL_CONFIGS

print("=" * 70)
print("nikkei_analysis_embargo2config: 2設定分析レポート")
print(f"  {_CON_LABEL}: 逆指値B:{len(_CON_STOP_WL)} BRK:{len(_CON_BRK_WL)}")
print(f"  {_AGG_LABEL}: 逆指値B:{len(_AGG_STOP_WL)} BRK:{len(_AGG_BRK_WL)}")
print("=" * 70)


# ════════════════════════════════════════════════════════════════════════════
# 設定評価ヘルパー
# ════════════════════════════════════════════════════════════════════════════

def _judge_config(cfg: dict, r: dict) -> tuple[str, str, str]:
    trend = r["trend"]; vol = r["vol_level"]
    mom5  = r["mom5"];  mom20 = r["mom20"]
    above = r["above_ma200"]; drop = r["max_1d_drop"]
    mode  = cfg["mode"]

    if mode == "aggressive":
        if trend == "down" and vol == "high":
            return "❌ 停止", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "conservative に切替え"
        if trend == "down":
            return "⚠️ 注意", f"下落トレンド (5日{mom5:+.1f}%)", "conservative への切替えを検討"
        if not above:
            return "⚠️ 注意", "日経 < MA200 (長期下落)", "conservative を優先"
        if trend == "up" and mom5 >= 2.0 and mom20 >= 3.0:
            return "✅ 推奨", f"上昇×5日{mom5:+.1f}%/20日{mom20:+.1f}%", "最も効率が良い局面"
        if trend == "up":
            return "✅ 推奨", f"上昇トレンド (5日{mom5:+.1f}%)", "上昇継続なら標準運用"
        return "⚠️ 注意", f"横ばい (5日{mom5:+.1f}%)", "conservative 併用推奨"
    else:
        if trend == "down" and vol == "high" and drop < -3.0:
            return "⚠️ 注意", f"下落×高ボラ×急落 (最大1日{drop:+.1f}%)", "ポジション縮小を検討"
        if trend == "down" and vol == "high":
            return "⚠️ 注意", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "ポジション縮小を検討"
        return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "全相場で使用可能"


def _2cfg_section_html(r: dict) -> str:
    """t1タブのスクリプト判定後に挿入する2設定評価カードセクション。"""
    cards = ""
    for cfg in CONFIGS:
        status, reason, advice = _judge_config(cfg, r)
        lbl_ja, fg, bg, border = STATUS_META[status]
        label  = cfg["label"]; color = cfg["color"]
        risk   = _RISK.get(label, "中"); note = _NOTE.get(label, "")
        rc     = RISK_COLOR.get(risk, "#94a3b8")
        n_stop = len(cfg["stop_wl"]); n_brk = len(cfg["brk_wl"])
        cards += f"""
<div class="script-card" style="border-color:{border};background:{bg}">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="badge" style="background:{border};color:{fg}">{lbl_ja}</span>
    <span style="font-weight:700;font-size:1.05rem;color:{color}">{label}</span>
    <span style="color:#64748b;font-size:0.8rem">{cfg['mode']} / 逆指値B:{n_stop}件 BRK:{n_brk}件</span>
    <span style="margin-left:auto;font-size:0.78rem;color:{rc}">リスク: {risk}</span>
  </div>
  <div style="color:#94a3b8;font-size:0.82rem;margin-top:8px">{reason}</div>
  <div style="color:#64748b;font-size:0.78rem;margin-top:4px">→ {advice}</div>
  <div style="color:#475569;font-size:0.75rem;margin-top:6px;border-top:1px solid #1e293b;padding-top:6px">{note}</div>
</div>"""
    return f"\n<h2>2設定シグナル判定 (embargo WATCHLIST)</h2>\n{cards}\n"


# ════════════════════════════════════════════════════════════════════════════
# トレンド×相性バックテスト
# ════════════════════════════════════════════════════════════════════════════

def _set_params(mode: str) -> None:
    if mode == "conservative":
        _stop.STRATEGY_PARAMS.update(_CON_STOP)
        _brk.STRATEGY_PARAMS.update(_CON_BRK)
    else:
        _stop.STRATEGY_PARAMS.update(_AGG_STOP)
        _brk.STRATEGY_PARAMS.update(_AGG_BRK)


def _run_backtests(workers: int) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for cfg in CONFIGS:
        _set_params(cfg["mode"])
        trades: list[dict] = []
        with _TPE(max_workers=workers) as ex:
            futs: dict = {}
            for sym, name, strat in cfg["stop_wl"]:
                futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
            for sym, name, strat in cfg["brk_wl"]:
                futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
            for fut in _asc(futs):
                try:
                    r = fut.result()
                    if not r:
                        continue
                    period_results = r.get("period_results", {})
                    if not period_results:
                        continue
                    max_period = max(period_results.keys())
                    for t in period_results[max_period].get("trade_log", []):
                        if t.get("reason") in (None, "発注中"):
                            continue
                        entry_dt = t.get("entry_dt")
                        if entry_dt is None:
                            continue
                        trades.append({"entry_dt": entry_dt, "pnl": t.get("pnl", 0)})
                except Exception:
                    pass
        result[cfg["label"]] = trades
    _set_params("conservative")
    return result


def _add_nikkei_trend(trades_by_cfg: dict[str, list[dict]], nk_trend: pd.Series) -> None:
    idx = nk_trend.index
    for trades in trades_by_cfg.values():
        for t in trades:
            ts  = pd.Timestamp(t["entry_dt"]).normalize()
            pos = idx.searchsorted(ts, side="right") - 1
            t["nk_trend"] = nk_trend.iloc[pos] if 0 <= pos < len(nk_trend) else "sideways"


# ════════════════════════════════════════════════════════════════════════════
# タブ7: トレンド×相性 HTML
# ════════════════════════════════════════════════════════════════════════════

def _tab7_trend_analysis_html(trades_by_cfg: dict[str, list[dict]]) -> str:
    TREND_LABELS = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}
    TREND_COLORS = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}

    def _agg(trades):
        n = len(trades)
        if n == 0:
            return None
        wins  = [t for t in trades if t["pnl"] > 0]
        loses = [t for t in trades if t["pnl"] < 0]
        gp    = sum(t["pnl"] for t in wins)
        gl    = abs(sum(t["pnl"] for t in loses))
        pf    = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
        avg   = sum(t["pnl"] for t in trades) / n
        wr    = len(wins) / n * 100
        return {"n": n, "wr": wr, "pf": pf, "pf_s": pf_s, "avg": avg}

    def _cell(agg):
        if agg is None:
            return '<td style="color:#475569;text-align:center">—</td>'
        wr_c  = "#4ade80" if agg["wr"] >= 55 else ("#fbbf24" if agg["wr"] >= 45 else "#f87171")
        pf_c  = "#4ade80" if agg["pf"] >= 1.5 else ("#fbbf24" if agg["pf"] >= 1.0 else "#f87171")
        avg_c = "#4ade80" if agg["avg"] >= 0 else "#f87171"
        return (f'<td style="text-align:center;padding:8px 10px">'
                f'<div style="font-size:0.75rem;color:#94a3b8">{agg["n"]}取引</div>'
                f'<div><span style="color:{wr_c};font-weight:700">{agg["wr"]:.0f}%</span>'
                f'<span style="font-size:0.72rem;color:#64748b"> 勝率</span></div>'
                f'<div><span style="color:{pf_c};font-weight:700">PF {agg["pf_s"]}</span></div>'
                f'<div style="color:{avg_c};font-size:0.78rem">{agg["avg"]:+,.0f}円/取引</div>'
                f'</td>')

    rows = ""
    for trend in ["up", "sideways", "down"]:
        tc  = TREND_COLORS[trend]; tl = TREND_LABELS[trend]
        row = f'<tr><td style="color:{tc};font-weight:700;white-space:nowrap;padding:8px 12px">{tl}</td>'
        for cfg in CONFIGS:
            t_list = [t for t in trades_by_cfg.get(cfg["label"], []) if t.get("nk_trend") == trend]
            row += _cell(_agg(t_list))
        row += "</tr>"
        rows += row

    total_row = '<tr style="border-top:2px solid #334155"><td style="color:#94a3b8;padding:8px 12px;font-weight:700">全期間合計</td>'
    for cfg in CONFIGS:
        total_row += _cell(_agg(trades_by_cfg.get(cfg["label"], [])))
    total_row += "</tr>"

    headers = "".join(
        f'<th style="color:{cfg["color"]};padding:10px 12px">{cfg["label"]}</th>'
        for cfg in CONFIGS
    )

    def _breakdown_cards():
        TREND_JA = {"up": "上昇", "down": "下落", "sideways": "横ばい"}
        cards = ""
        for cfg in CONFIGS:
            trades = trades_by_cfg.get(cfg["label"], [])
            if not trades:
                continue
            by_t  = defaultdict(list)
            for t in trades:
                by_t[t.get("nk_trend", "sideways")].append(t)
            total = len(trades)
            bars  = ""
            for trend in ["up", "sideways", "down"]:
                n   = len(by_t[trend])
                pct = n / total * 100 if total else 0
                wins = sum(1 for t in by_t[trend] if t["pnl"] > 0)
                wr   = wins / n * 100 if n else 0
                tc   = TREND_COLORS[trend]
                bars += f"""
<div style="margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;margin-bottom:4px">
    <span style="color:{tc};font-size:0.82rem;font-weight:600">{TREND_JA[trend]}</span>
    <span style="font-size:0.78rem;color:#94a3b8">{n}件 ({pct:.0f}%) / 勝率{wr:.0f}%</span>
  </div>
  <div style="background:#1e293b;border-radius:4px;height:8px;overflow:hidden">
    <div style="background:{tc};height:100%;width:{pct:.1f}%;border-radius:4px"></div>
  </div>
</div>"""
            cards += f"""
<div style="background:#0d1424;border:1px solid #1e3a5f;border-radius:10px;padding:16px;flex:1;min-width:220px">
  <div style="color:{cfg['color']};font-weight:700;margin-bottom:12px">{cfg['label']}</div>
  <div style="font-size:0.75rem;color:#64748b;margin-bottom:8px">合計 {total}取引</div>
  {bars}
</div>"""
        return f'<div style="display:flex;flex-wrap:wrap;gap:12px">{cards}</div>'

    return f"""
<h2>トレンド × 設定相性</h2>
<p style="color:#94a3b8;font-size:0.85rem">
  各セルは日経平均が「そのトレンド」だった日にエントリーしたトレードの成績。
  <span style="color:#4ade80">緑</span>: 勝率55%以上 / PF1.5以上、
  <span style="color:#fbbf24">黄</span>: 勝率45〜55% / PF1〜1.5、
  <span style="color:#f87171">赤</span>: それ以下。
</p>
<div style="overflow-x:auto">
<table>
  <thead>
    <tr>
      <th style="text-align:left;padding:10px 12px">日経トレンド</th>
      {headers}
    </tr>
  </thead>
  <tbody>{rows}{total_row}</tbody>
</table>
</div>
<h2>設定別 トレンド構成比</h2>
{_breakdown_cards()}"""


# ════════════════════════════════════════════════════════════════════════════
# HTML 注入 (nikkei_analysis_4config.py と同一)
# ════════════════════════════════════════════════════════════════════════════

def _inject_tabs(html: str, cfg_section_html: str, tab7_html: str) -> str:
    html = _re.sub(
        r'(<h2>[^<]*時点の推奨コマンド)',
        cfg_section_html.replace('\\', '\\\\') + r'\1',
        html, count=1,
    )
    TAB_NAV_END = '\n</div>\n\n<div id="t1"'
    if TAB_NAV_END in html:
        new_btn = '\n  <button class="tab-btn" data-tab="t7" onclick="switchTab(\'t7\')">📊 トレンド×相性</button>'
        html = html.replace(TAB_NAV_END, new_btn + TAB_NAV_END, 1)
    SCRIPT_TAG = '\n\n<script>'
    if SCRIPT_TAG in html:
        html = html.replace(SCRIPT_TAG,
                            f'\n<div id="t7" class="tab-pane">{tab7_html}</div>' + SCRIPT_TAG, 1)
    css_fix = (
        '\n<style>'
        '\n.tab-nav { flex-wrap: nowrap !important; overflow-x: auto; -webkit-overflow-scrolling: touch; }'
        '\n.tab-btn { padding: 7px 13px !important; font-size: 0.8rem !important; white-space: nowrap; }'
        '\n</style>'
    )
    html = html.replace('</head>', css_fix + '\n</head>', 1)
    return html


# ════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str, default=None)
    _p.add_argument("--no-browser", action="store_true")
    _p.add_argument("--days",       type=int, default=365)
    _p.add_argument("--years",      type=int, default=5)
    _p.add_argument("--workers",    type=int, default=_DEF_WORKERS)
    _known, _ = _p.parse_known_args()

    # ── nikkei_analysis.main() を呼び出して base HTML を生成 ─────────────────
    _orig_argv = list(sys.argv)
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")
    sys.argv = [a for a in sys.argv if a not in ("--aggressive",)
                and not a.startswith("--workers=")]
    if _known.workers != _DEF_WORKERS:
        sys.argv.append(f"--workers={_known.workers}")

    _na.main()

    sys.argv[:] = _orig_argv

    JST_     = timezone(timedelta(hours=9))
    date_str = _known.date if _known.date else str(datetime.now(JST_).date())
    base_path = Path(f"nikkei_analysis_{date_str}.html")
    if not base_path.exists():
        print(f"[WARN] {base_path} が見つかりません"); return

    base_html = base_path.read_text(encoding="utf-8")

    # ── トレンドデータ取得 ──────────────────────────────────────────────────
    print(f"2設定バックテスト実行中 (workers={_known.workers})...", flush=True)
    try:
        close    = _na.fetch_n225(_known.years, end_date=None)
        r        = _na.get_regime(close)
        nk_trend = _na.label_trend(close)
    except Exception as e:
        print(f"[WARN] 日経データ取得失敗 ({e})")
        close, r, nk_trend = None, None, None

    if r is not None:
        trades_by_cfg = _run_backtests(_known.workers)
        _add_nikkei_trend(trades_by_cfg, nk_trend)
        cfg_section = _2cfg_section_html(r)
        tab7_html   = _tab7_trend_analysis_html(trades_by_cfg)
    else:
        cfg_section = ""
        tab7_html   = "<p style='color:#64748b;padding:20px'>データ取得失敗のためスキップ</p>"

    new_html = _inject_tabs(base_html, cfg_section, tab7_html)
    new_path = Path(f"nikkei_analysis_embargo2config_{date_str}.html")
    new_path.write_text(new_html, encoding="utf-8")
    print(f"\n2config レポート生成完了: {new_path.resolve()}")

    if not _known.no_browser:
        open_html(new_path.resolve().as_uri())


if __name__ == "__main__":
    main()
