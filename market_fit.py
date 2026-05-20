"""
market_fit.py  ―  相場適合度レポート
==================================================
直近のバックテストデータ（ローリングウィンドウ）と
日経225の相場環境を組み合わせて、今の時期に最も相性が
良いシグナルスクリプトを自動判定する。

【スコアリング方式】
  直近PnL順位  (40点) : 直近1週のPnLを全スクリプト相対ランク化
  勝率         (20点) : 直近1週の勝率
  PF           (15点) : 直近1週のプロフィットファクター (上限5倍)
  モメンタム   (15点) : 直近1週 vs 前週のPnL比較
  相場適合     (10点) : 日経225の環境に合ったスクリプト特性

【使い方】
  python market_fit.py              # 直近28日で分析
  python market_fit.py --workers 8  # 並列数
  python market_fit.py --no-browser # HTML生成のみ

出力: market_fit_YYYY-MM-DD.html
"""
from __future__ import annotations

import argparse
import copy
import importlib
import os
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ── モジュールセットアップ ────────────────────────────────────────────────────
os.environ["TRADING_MODE"] = "conservative"
import check_signals_stop     as _stop
import check_signals_breakout as _brk
_CON_STOP_PARAMS = copy.deepcopy(_stop.STRATEGY_PARAMS)
_CON_BRK_PARAMS  = copy.deepcopy(_brk.STRATEGY_PARAMS)

os.environ["TRADING_MODE"] = "aggressive"
importlib.reload(_stop); importlib.reload(_brk)
_AGG_STOP_PARAMS = copy.deepcopy(_stop.STRATEGY_PARAMS)
_AGG_BRK_PARAMS  = copy.deepcopy(_brk.STRATEGY_PARAMS)

os.environ["TRADING_MODE"] = "conservative"
importlib.reload(_stop); importlib.reload(_brk)

from backtest_limit_entry import WORKERS as _DEF_WORKERS

# ── watchlist 読み込み ───────────────────────────────────────────────────────
import run_signals_wf as _wf_mod
_WF_STOP = list(_wf_mod._STOP_WATCHLIST)
_WF_BRK  = list(_wf_mod._BRK_WATCHLIST)

import run_signals_prime as _prime_mod
_PRIME_STOP = list(_prime_mod.STOP_WATCHLIST)
_PRIME_BRK  = list(_prime_mod.BRK_WATCHLIST)

import run_signals_nolimit as _nolimit_mod
_NOLIMIT_STOP = list(_nolimit_mod.STOP_WATCHLIST)
_NOLIMIT_BRK  = list(_nolimit_mod.BRK_WATCHLIST)

import run_signals_aggressive as _agg_mod
_AGGSCRIPT_STOP = list(_agg_mod.STOP_WATCHLIST)
_AGGSCRIPT_BRK  = list(_agg_mod.BRK_WATCHLIST)

import run_signals_merged as _merged_mod

def _dedup(a: list, b: list) -> list:
    seen: set = set()
    out: list = []
    for item in list(a) + list(b):
        key = (item[0], item[2])
        if key not in seen:
            seen.add(key); out.append(item)
    return out

_MERGED_STOP = _dedup(list(_stop.WATCHLIST), list(_merged_mod._WF_STOP))
_MERGED_BRK  = _dedup(list(_brk.WATCHLIST),  list(_merged_mod._WF_BRK))

_stop.STRATEGY_PARAMS.update(_CON_STOP_PARAMS)
_brk.STRATEGY_PARAMS.update(_CON_BRK_PARAMS)

# ── CONFIGS ───────────────────────────────────────────────────────────────────
CONFIGS: list[dict] = [
    {"label": "既存版",        "color": "#3498db", "mode": "conservative", "sm_tm": None,
     "cmd": "python run_signals.py",
     "stop_wl": list(_stop.WATCHLIST), "brk_wl": list(_brk.WATCHLIST)},
    {"label": "WF版",          "color": "#2ecc71", "mode": "aggressive",   "sm_tm": None,
     "cmd": "python run_signals_wf.py --aggressive",
     "stop_wl": _WF_STOP,            "brk_wl": _WF_BRK},
    {"label": "prime版",       "color": "#9b59b6", "mode": "aggressive",   "sm_tm": (1.5, 2.0),
     "cmd": "python run_signals_prime.py",
     "stop_wl": _PRIME_STOP,         "brk_wl": _PRIME_BRK},
    {"label": "nolimit版",     "color": "#e67e22", "mode": "aggressive",   "sm_tm": (1.5, 2.0),
     "cmd": "python run_signals_nolimit.py",
     "stop_wl": _NOLIMIT_STOP,       "brk_wl": _NOLIMIT_BRK},
    {"label": "積極版",        "color": "#e74c3c", "mode": "aggressive",   "sm_tm": (1.5, 2.0),
     "cmd": "python run_signals_aggressive.py",
     "stop_wl": _AGGSCRIPT_STOP,     "brk_wl": _AGGSCRIPT_BRK},
    {"label": "merged版",      "color": "#f39c12", "mode": "conservative", "sm_tm": None,
     "cmd": "python run_signals_merged.py",
     "stop_wl": _MERGED_STOP,        "brk_wl": _MERGED_BRK},
]

# ── パラメータ設定 ────────────────────────────────────────────────────────────
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

# ── バックテスト実行 ──────────────────────────────────────────────────────────
def _run_config(cfg: dict, workers: int) -> list[dict]:
    _set_params(cfg["mode"], cfg["sm_tm"])
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs: dict = {}
        for sym, name, strat in cfg["stop_wl"]:
            futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
        for sym, name, strat in cfg["brk_wl"]:
            futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    items.append(r)
            except Exception:
                pass
    return items

def _extract_trades(items: list[dict], label: str, color: str) -> list[dict]:
    trades: list[dict] = []
    for it in items:
        pr = it.get("period_results", {})
        if not pr:
            continue
        tlog = pr[max(pr.keys())].get("trade_log", [])
        for tr in tlog:
            ds = tr.get("exit_date", "")
            if not ds:
                continue
            try:
                exit_d = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            trades.append({
                "label":    label,
                "color":    color,
                "symbol":   it.get("symbol", ""),
                "name":     it.get("name", ""),
                "strategy": it.get("strategy", ""),
                "exit_d":   exit_d,
                "pnl":      tr.get("pnl", 0),
                "result":   tr.get("result", ""),
            })
    return trades

# ── 市場環境取得 ─────────────────────────────────────────────────────────────
def get_market_regime() -> dict | None:
    try:
        import yfinance as yf
        df = yf.download("^N225", period="3mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 25:
            return None
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]

        rets  = close.pct_change().dropna()
        vol   = float(rets.tail(14).std() * 100)
        ma10  = float(close.rolling(10).mean().iloc[-1])
        ma25  = float(close.rolling(25).mean().iloc[-1])
        cur   = float(close.iloc[-1])
        mom5  = (cur / float(close.iloc[-6])  - 1) * 100
        mom20 = (cur / float(close.iloc[-21]) - 1) * 100

        if vol > 1.5:
            vol_level, vol_label, vol_color = "high", "高ボラ", "#f87171"
        elif vol > 0.8:
            vol_level, vol_label, vol_color = "mid",  "中ボラ", "#fbbf24"
        else:
            vol_level, vol_label, vol_color = "low",  "低ボラ", "#4ade80"

        if cur > ma10 and ma10 > ma25:
            trend_dir, trend_label, trend_color = "up",       "上昇",   "#4ade80"
        elif cur < ma10 and ma10 < ma25:
            trend_dir, trend_label, trend_color = "down",     "下落",   "#f87171"
        else:
            trend_dir, trend_label, trend_color = "sideways", "レンジ", "#fbbf24"

        return {
            "cur": cur, "ma10": ma10, "ma25": ma25,
            "vol": vol, "vol_level": vol_level, "vol_label": vol_label, "vol_color": vol_color,
            "trend_dir": trend_dir, "trend_label": trend_label, "trend_color": trend_color,
            "mom5": float(mom5), "mom20": float(mom20),
        }
    except Exception as e:
        print(f"[WARN] 市場データ取得失敗: {e}")
        return None

# ── ローリングウィンドウ分析 ──────────────────────────────────────────────────
def _win_stats(trades: list[dict], start: date, end: date) -> dict:
    wt = [t for t in trades if start <= t["exit_d"] <= end]
    n  = len(wt)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0, "per_trade": 0.0}
    wins = sum(1 for t in wt if t["pnl"] > 0)
    gp   = sum(t["pnl"] for t in wt if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in wt if t["pnl"] < 0))
    pnl  = sum(t["pnl"] for t in wt)
    pf   = gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0)
    return {"n": n, "wr": wins/n*100, "pf": min(pf, 99.0),
            "pnl": pnl, "per_trade": pnl/n}

def calc_rolling(trades_map: dict[str, list[dict]], today: date) -> dict:
    # W1: 直近7日 (today-7 〜 today)  ← recent_pnl.py --days 7 と同じ範囲
    # W2: 前7日  (today-14 〜 today-8)
    # W3: 2〜4週前 (today-28 〜 today-15)
    periods = {
        "W1": (today - timedelta(days=7),  today),
        "W2": (today - timedelta(days=14), today - timedelta(days=8)),
        "W3": (today - timedelta(days=28), today - timedelta(days=15)),
    }
    return {
        label: {k: _win_stats(trades, s, e) for k, (s, e) in periods.items()}
        for label, trades in trades_map.items()
    }

# ── スコア計算 ────────────────────────────────────────────────────────────────
def calc_scores(rolling: dict, regime: dict | None) -> list[tuple]:
    labels    = list(rolling.keys())
    w1_pnls   = [rolling[l]["W1"]["pnl"] for l in labels]
    min_p, max_p = min(w1_pnls), max(w1_pnls)
    rng = max(max_p - min_p, 1)

    entries = []
    for label in labels:
        w1, w2, w3 = rolling[label]["W1"], rolling[label]["W2"], rolling[label]["W3"]

        # 1. PnL順位スコア (0〜40点)
        pnl_sc = (w1["pnl"] - min_p) / rng * 40

        # 2. 勝率スコア (0〜20点)
        wr_sc = w1["wr"] / 100 * 20

        # 3. PFスコア (0〜15点)
        pf_sc = min(w1["pf"], 5.0) / 5.0 * 15

        # 4. モメンタム (0〜15点): W1>W2>W3で加点
        mom_pts = 0
        if w1["pnl"] > 0:
            mom_pts += 8
        if w1["pnl"] > w2["pnl"]:
            mom_pts += 4
        if w2["pnl"] > w3["pnl"]:
            mom_pts += 3
        if w1["pnl"] < 0 and w2["pnl"] < 0:
            mom_pts = -5  # 連続マイナスはペナルティ

        if w1["pnl"] > w2["pnl"]:
            mom_label = "↑↑ 急改善" if w2["pnl"] < 0 else "↑ 改善中"
        elif w1["pnl"] > 0:
            mom_label = "→ 横ばい"
        elif w2["pnl"] > 0:
            mom_label = "↓ 悪化"
        else:
            mom_label = "↓↓ 連続マイナス"

        # 5. 相場適合ボーナス (0〜10点)
        reg_bonus = 0
        reg_reason = ""
        if regime:
            vl = regime["vol_level"]
            td = regime["trend_dir"]
            is_large  = any(k in label for k in ["prime", "nolimit"])
            is_stable = any(k in label for k in ["WF版", "既存版"])
            is_merged = "merged" in label

            if td == "up" and vl in ("mid", "high"):
                reg_bonus = 10 if is_large else (6 if is_merged else 3)
                reg_reason = "上昇×高ボラ → 銘柄数が多い方が有利"
            elif td == "down":
                reg_bonus = 10 if is_stable else (5 if is_merged else 0)
                reg_reason = "下落相場 → 厳選銘柄が安定"
            elif vl == "low":
                reg_bonus = 8 if is_stable else (4 if is_merged else 2)
                reg_reason = "低ボラ → シグナル精度重視型が有利"
            elif td == "sideways" and vl == "high":
                reg_bonus = 5
                reg_reason = "高ボラレンジ → 逆指値は刺さりやすいがノイズ多"

        score = max(0.0, min(100.0, pnl_sc + wr_sc + pf_sc + mom_pts + reg_bonus))

        entries.append((label, {
            "score": score,
            "pnl_sc": pnl_sc, "wr_sc": wr_sc, "pf_sc": pf_sc,
            "mom_pts": mom_pts, "mom_label": mom_label,
            "reg_bonus": reg_bonus, "reg_reason": reg_reason,
            "w1": w1, "w2": w2, "w3": w3,
        }))

    entries.sort(key=lambda x: x[1]["score"], reverse=True)
    for i, (_, info) in enumerate(entries):
        info["rank"] = i + 1
    return entries

# ── HTML生成 ─────────────────────────────────────────────────────────────────
_RANK_BADGE = {1: ("🥇", "#fbbf24"), 2: ("🥈", "#94a3b8"), 3: ("🥉", "#cd7f32")}

def _fmt_pnl(v: int) -> str:
    cls = "profit" if v >= 0 else "loss"
    return f'<span class="{cls}">{v:+,.0f}円</span>'

def _fmt_wr(v: float, n: int) -> str:
    if n == 0:
        return '<span style="color:#475569">—</span>'
    cls = "profit" if v >= 50 else "loss"
    return f'<span class="{cls}">{v:.1f}%</span>'

def _fmt_pf(pf: float, n: int) -> str:
    if n == 0:
        return '<span style="color:#475569">—</span>'
    cls = "profit" if pf >= 1.0 else "loss"
    s = f"{pf:.2f}" if pf < 99 else "∞"
    return f'<span class="{cls}">{s}</span>'

def _score_bar(score: float, color: str) -> str:
    w = int(score)
    return (f'<div style="display:flex;align-items:center;gap:8px">'
            f'<div style="background:#1e293b;border-radius:4px;height:10px;width:120px;overflow:hidden">'
            f'<div style="background:{color};height:100%;width:{w}%"></div></div>'
            f'<span style="font-weight:700;font-size:1rem">{score:.0f}</span></div>')

def _regime_panel(regime: dict | None) -> str:
    if not regime:
        return '<p style="color:#64748b">市場データ取得失敗 (日経225)</p>'
    mom5_cls  = "profit" if regime["mom5"]  >= 0 else "loss"
    mom20_cls = "profit" if regime["mom20"] >= 0 else "loss"
    return f"""
<div class="regime-panel">
  <div class="regime-item">
    <span class="regime-label">日経225</span>
    <span class="regime-val">{regime['cur']:,.0f}円</span>
  </div>
  <div class="regime-item">
    <span class="regime-label">ボラ(14日)</span>
    <span class="regime-val" style="color:{regime['vol_color']}">{regime['vol_label']} ({regime['vol']:.2f}%)</span>
  </div>
  <div class="regime-item">
    <span class="regime-label">トレンド</span>
    <span class="regime-val" style="color:{regime['trend_color']}">{regime['trend_label']}</span>
  </div>
  <div class="regime-item">
    <span class="regime-label">5日騰落</span>
    <span class="regime-val {mom5_cls}">{regime['mom5']:+.2f}%</span>
  </div>
  <div class="regime-item">
    <span class="regime-label">20日騰落</span>
    <span class="regime-val {mom20_cls}">{regime['mom20']:+.2f}%</span>
  </div>
  <div class="regime-item">
    <span class="regime-label">MA10 / MA25</span>
    <span class="regime-val">{regime['ma10']:,.0f} / {regime['ma25']:,.0f}</span>
  </div>
</div>"""

def _recommendation_cards(scores: list[tuple], cfg_map: dict) -> str:
    cards = []
    for label, info in scores[:3]:
        rank = info["rank"]
        badge_emoji, badge_color = _RANK_BADGE.get(rank, ("", "#64748b"))
        cfg   = cfg_map[label]
        color = cfg["color"]
        score = info["score"]

        if score >= 65:
            bdr = "#4ade80"; status = "今の相場と相性◎"
        elif score >= 45:
            bdr = "#fbbf24"; status = "今の相場と相性○"
        else:
            bdr = "#f87171"; status = "やや相性△"

        mom_color = "#4ade80" if "↑" in info["mom_label"] else ("#fbbf24" if "→" in info["mom_label"] else "#f87171")
        reg_html  = (f'<div class="card-sub">📊 {info["reg_reason"]}</div>'
                     if info["reg_reason"] else "")

        cards.append(f"""
<div class="rec-card" style="border-left:4px solid {bdr}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start">
    <div>
      <span style="font-size:1.4rem">{badge_emoji}</span>
      <span class="rec-label" style="color:{color}">{label}</span>
      <span class="rec-status" style="color:{bdr}">{status}</span>
    </div>
    <div style="text-align:right">
      {_score_bar(score, color)}
    </div>
  </div>
  <code class="rec-cmd">{cfg['cmd']}</code>
  <div style="display:flex;gap:24px;margin-top:8px;font-size:0.82rem">
    <span>直近1週: {_fmt_pnl(info['w1']['pnl'])} ({info['w1']['n']}件)</span>
    <span>勝率: {_fmt_wr(info['w1']['wr'], info['w1']['n'])}</span>
    <span>PF: {_fmt_pf(info['w1']['pf'], info['w1']['n'])}</span>
    <span style="color:{mom_color}">トレンド: {info['mom_label']}</span>
  </div>
  {reg_html}
</div>""")
    return "\n".join(cards)

def _ranking_table(scores: list[tuple], cfg_map: dict) -> str:
    rows = []
    for label, info in scores:
        cfg   = cfg_map[label]
        rank  = info["rank"]
        badge = _RANK_BADGE.get(rank, ("", "#64748b"))[0]
        color = cfg["color"]
        score = info["score"]

        bar = (f'<div style="background:#1e293b;border-radius:3px;height:8px;width:100px;overflow:hidden">'
               f'<div style="background:{color};height:100%;width:{int(score)}%"></div></div>')

        # スコア内訳ツールチップ的に表示
        breakdown = (f'PnL:{info["pnl_sc"]:.0f} 勝率:{info["wr_sc"]:.0f} '
                     f'PF:{info["pf_sc"]:.0f} Mom:{info["mom_pts"]:+.0f} 相場:{info["reg_bonus"]:+.0f}')

        mom_color = "#4ade80" if "↑" in info["mom_label"] else ("#fbbf24" if "→" in info["mom_label"] else "#f87171")

        rows.append(f"""<tr>
  <td style="text-align:center;font-size:1.1rem">{badge} {rank}</td>
  <td class="sym" style="color:{color}">{label}</td>
  <td>{bar}<div style="font-size:0.7rem;color:#64748b;margin-top:2px">{breakdown}</div></td>
  <td style="text-align:center;font-weight:700;color:{color}">{score:.0f}点</td>
  <td>{_fmt_pnl(info['w1']['pnl'])}</td>
  <td style="text-align:center">{info['w1']['n']}</td>
  <td>{_fmt_wr(info['w1']['wr'], info['w1']['n'])}</td>
  <td>{_fmt_pf(info['w1']['pf'], info['w1']['n'])}</td>
  <td style="color:{mom_color};text-align:center">{info['mom_label']}</td>
</tr>""")
    return "\n".join(rows)

def _rolling_table(scores: list[tuple], cfg_map: dict, today: date) -> str:
    w1s = (today - timedelta(days=7)).strftime("%m/%d")
    w1e = today.strftime("%m/%d")
    w2s = (today - timedelta(days=14)).strftime("%m/%d")
    w2e = (today - timedelta(days=8)).strftime("%m/%d")
    w3s = (today - timedelta(days=28)).strftime("%m/%d")
    w3e = (today - timedelta(days=15)).strftime("%m/%d")

    header = f"""<tr>
  <th style="text-align:left">スクリプト</th>
  <th colspan="4">直近1週 ({w1s}〜{w1e})</th>
  <th colspan="4">前週 ({w2s}〜{w2e})</th>
  <th colspan="4">2〜4週前 ({w3s}〜{w3e})</th>
</tr>
<tr>
  <th></th>
  <th>件数</th><th>勝率</th><th>PF</th><th>損益</th>
  <th>件数</th><th>勝率</th><th>PF</th><th>損益</th>
  <th>件数</th><th>勝率</th><th>PF</th><th>損益</th>
</tr>"""

    rows = []
    for label, info in scores:
        color = cfg_map[label]["color"]
        w1, w2, w3 = info["w1"], info["w2"], info["w3"]
        rows.append(f"""<tr>
  <td class="sym" style="color:{color}">{label}</td>
  <td style="text-align:center">{w1['n']}</td>
  <td>{_fmt_wr(w1['wr'], w1['n'])}</td>
  <td>{_fmt_pf(w1['pf'], w1['n'])}</td>
  <td>{_fmt_pnl(w1['pnl'])}</td>
  <td style="text-align:center">{w2['n']}</td>
  <td>{_fmt_wr(w2['wr'], w2['n'])}</td>
  <td>{_fmt_pf(w2['pf'], w2['n'])}</td>
  <td>{_fmt_pnl(w2['pnl'])}</td>
  <td style="text-align:center">{w3['n']}</td>
  <td>{_fmt_wr(w3['wr'], w3['n'])}</td>
  <td>{_fmt_pf(w3['pf'], w3['n'])}</td>
  <td>{_fmt_pnl(w3['pnl'])}</td>
</tr>""")
    return f"<thead>{header}</thead><tbody>{''.join(rows)}</tbody>"

def build_html(scores: list[tuple], regime: dict | None,
               today_str: str, today: date, cfg_map: dict) -> str:
    top_label, top_info = scores[0]
    top_score = top_info["score"]
    regime_panel    = _regime_panel(regime)
    rec_cards       = _recommendation_cards(scores, cfg_map)
    ranking_rows    = _ranking_table(scores, cfg_map)
    rolling_content = _rolling_table(scores, cfg_map, today)

    regime_summary = ""
    if regime:
        regime_summary = (f'相場環境: <strong style="color:{regime["vol_color"]}">'
                          f'{regime["vol_label"]}</strong> / '
                          f'<strong style="color:{regime["trend_color"]}">'
                          f'{regime["trend_label"]}</strong>')

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>相場適合度レポート — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:24px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.15rem;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; margin-bottom:20px; font-size:0.9rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 10px; border:1px solid #334155;
        white-space:nowrap; text-align:center; }}
  td {{ padding:5px 10px; border:1px solid #1e293b; white-space:nowrap; text-align:right; }}
  tr:hover td {{ background:#1e293b44; }}
  .sym {{ text-align:left; font-weight:600; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}

  /* 市場環境パネル */
  .regime-panel {{ display:flex; flex-wrap:wrap; gap:12px; background:#0d1424;
                   border:1px solid #1e3a5f; border-radius:10px; padding:16px; margin-bottom:20px; }}
  .regime-item  {{ display:flex; flex-direction:column; min-width:120px; }}
  .regime-label {{ font-size:0.72rem; color:#64748b; margin-bottom:3px; }}
  .regime-val   {{ font-size:0.95rem; font-weight:600; }}

  /* 推奨カード */
  .rec-card {{ background:#111827; border-radius:10px; padding:16px 20px;
               margin-bottom:12px; transition:background .15s; }}
  .rec-card:hover {{ background:#1e293b; }}
  .rec-label  {{ font-size:1.15rem; font-weight:700; margin:0 10px; }}
  .rec-status {{ font-size:0.78rem; background:#1e293b; padding:2px 8px;
                 border-radius:99px; vertical-align:middle; }}
  .rec-cmd    {{ display:block; margin-top:8px; background:#0f172a; padding:4px 10px;
                 border-radius:6px; color:#38bdf8; font-size:0.82rem; }}
  .card-sub   {{ margin-top:6px; font-size:0.78rem; color:#94a3b8; }}
</style>
</head>
<body>
<h1>相場適合度レポート</h1>
<p class="subtitle">生成日: {today_str}　{regime_summary}</p>

<h2>現在の相場環境（日経225）</h2>
{regime_panel}

<h2>推奨シグナル（上位3件）</h2>
{rec_cards}

<h2>スコアランキング</h2>
<table>
<thead><tr>
  <th>順位</th>
  <th style="text-align:left">スクリプト</th>
  <th style="text-align:left">スコアバー</th>
  <th>総合</th>
  <th>直近1週損益</th>
  <th>取引数</th>
  <th>勝率</th>
  <th>PF</th>
  <th>トレンド</th>
</tr></thead>
<tbody>{ranking_rows}</tbody>
</table>

<h2>ローリングウィンドウ詳細</h2>
<p style="color:#64748b;font-size:0.8rem;margin-bottom:8px">
  各期間のバックテスト決済データ（スリッページ・手数料込み）
</p>
<table>
{rolling_content}
</table>

<p style="color:#334155;font-size:0.72rem;margin-top:20px">
  ※ スコア = PnL順位(40)+勝率(20)+PF(15)+モメンタム(15)+相場適合(10)。
  バックテスト値であり将来の利益を保証しません。
</p>
</body>
</html>"""

# ── メイン ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="相場適合度レポート")
    parser.add_argument("--workers",    type=int, default=_DEF_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    today     = datetime.now(JST).date()
    today_str = today.strftime("%Y-%m-%d")

    print(f"相場適合度レポート — {today_str}", flush=True)
    print(f"並列数: {args.workers}", flush=True)

    # 1. 市場環境取得
    print("日経225 相場環境を取得中...", flush=True)
    regime = get_market_regime()
    if regime:
        print(f"  相場環境: {regime['vol_label']} / {regime['trend_label']} "
              f"(ボラ {regime['vol']:.2f}%, 5日騰落 {regime['mom5']:+.2f}%)")
    else:
        print("  相場環境: 取得失敗")

    # 2. 各スクリプトのバックテスト
    cfg_map = {cfg["label"]: cfg for cfg in CONFIGS}
    trades_map: dict[str, list[dict]] = {}

    for cfg in CONFIGS:
        label = cfg["label"]
        print(f"  処理中: {label} ...", end="", flush=True)
        items  = _run_config(cfg, args.workers)
        trades = _extract_trades(items, label, cfg["color"])
        trades_map[label] = trades
        w1 = _win_stats(trades, today - timedelta(days=7), today)
        print(f" 全{len(trades)}件 / 直近1週: {w1['n']}件 {w1['pnl']:+,.0f}円")

    # 3. ローリング分析
    rolling = calc_rolling(trades_map, today)

    # 4. スコア計算
    scores = calc_scores(rolling, regime)

    # 5. 結果表示
    print()
    print("=" * 60)
    print("  相場適合度ランキング")
    print("=" * 60)
    for label, info in scores:
        badge = _RANK_BADGE.get(info["rank"], ("", ""))[0]
        print(f"  {badge} {info['rank']}位 {label:<12} "
              f"スコア:{info['score']:5.0f}点  "
              f"直近1週:{info['w1']['pnl']:+9,.0f}円  "
              f"{info['mom_label']}")

    # 6. HTML生成
    out = Path(f"market_fit_{today_str}.html")
    out.write_text(
        build_html(scores, regime, today_str, today, cfg_map),
        encoding="utf-8"
    )
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
