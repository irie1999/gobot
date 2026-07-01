"""
check_signals_breakout.py  ―  ブレイクアウト戦略 逆指値エントリー バックテスト
=================================================================
scan_breakout_entry.py のスキャン上位銘柄を監視対象として、
3つのブレイクアウト戦略でシグナルを検知。
エントリー条件: 高値 ≥ 前日終値（上がれば買う・逆指値）

【戦略】
  DON: ドンチャン高値ブレイク — 終値 > 20日高値 + MA50上位
  VOL: 出来高急増ブレイク     — 10日高値更新 + 出来高2×平均
  MOM: モメンタムブレイク     — ROC(10d)>5% + MA25>MA75

【使い方】
  python check_signals_breakout.py               # 全期間(365日) HTMLレポート
  python check_signals_breakout.py --days 90     # 直近90日
  python check_signals_breakout.py --date 2026-03-28  # 任意日シグナル確認
  python check_signals_breakout.py --no-browser
  python check_signals_breakout.py --signal-only
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf
import pandas as pd

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    fetch_n225_return,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, LIMIT_ENTRY_MARGIN_PCT,
    MAX_HOLD, ENTRY_EXPIRE,
    INITIAL_CASH as _INITIAL_CASH,
    WORKERS as _DEFAULT_WORKERS,
    compute_period_result,
    round_to_tick,
    calc_qty,
)
from risk_metrics import enrich_backtest_result, calc_hold_stats
from scan_breakout_entry import (calc_donchian, calc_vol_breakout,
                                 calc_vol_breakout_tf, calc_momentum)
from compute_wf_scores import build_wf_scores, calc_wf_score, wf_rank

JST     = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]

import json as _json
_WF_SCORES_PATH = Path("wf_scores.json")
if _WF_SCORES_PATH.exists():
    with open(_WF_SCORES_PATH, encoding="utf-8") as _f:
        _WF_SCORES: dict = _json.load(_f)
else:
    _WF_SCORES: dict = build_wf_scores()

def get_wf_score(symbol: str, strategy: str) -> tuple[int, str] | None:
    """WFスコアとランクを返す。データなしの場合はNone。"""
    v = _WF_SCORES.get(f"{symbol}_{strategy}")
    if v is None:
        return None
    return v["score"], v["rank"]


def _load_cpcv_flags() -> dict:
    """cpcv_flags*.py を全て読み込んでマージする (danger > warning 優先)。"""
    import importlib.util as _ilu, os as _os2
    _lvl = {"danger": 2, "warning": 1}
    aggressive = _os2.getenv("TRADING_MODE", "").lower() == "aggressive"
    merged: dict = {}
    for p in sorted(Path(".").glob("cpcv_flags*.py")):
        is_agg = "aggressive" in p.name
        if is_agg and not aggressive:
            continue
        if not is_agg and aggressive and "holdout_all" not in p.name and p.name != "cpcv_flags.py":
            continue
        try:
            spec = _ilu.spec_from_file_location(f"_cpcv_{p.stem}", p)
            mod  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for sym, info in getattr(mod, "CPCV_FLAGS", {}).items():
                existing = merged.get(sym)
                if existing is None or _lvl.get(info["level"], 0) > _lvl.get(existing["level"], 0):
                    merged[sym] = info
        except Exception:
            pass
    return merged

CPCV_FLAGS: dict = _load_cpcv_flags()

try:
    from signal_risk_check import (
        RISK_FLAGS as _RISK_FLAGS,
        render_risk_badges as _render_risk_badges,
        render_earnings_date as _render_earnings_date,
    )
except Exception:
    _RISK_FLAGS = {}
    def _render_risk_badges(_sym): return ""
    def _render_earnings_date(_sym, _td=None): return ""


def _fetch_live_price(symbol: str, fallback: float) -> float:
    """最新の日足終値をキャッシュを使わず直接取得。失敗時はフォールバック。"""
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1d",
                                       auto_adjust=False, actions=False)
        if df is not None and not df.empty:
            p = float(df["Close"].iloc[-1])
            return p if p > 0 else fallback
    except Exception:
        pass
    return fallback

# ── scan_breakout_entry.py --max-price 5000（緩和パラメータ）スキャン上位銘柄 ──
# DON:15日高値 / VOL:5日高値+出来高1.5x / MOM:ROC>3%
# 複数期間で安定した成績を示した銘柄を優先選定
WATCHLIST: list[tuple[str, str, str]] = [
    # ── DON: Walk-forward 選定 (2026-06-06, max-price≤5000円) ──
    ("8091.T", "ニチモウ",                           "DON"),  # 2,211円 400株≒884k  folds=2 PF1.62 WR59.0% DD=8.0%  Shrp0.96
    # ── MOM: Walk-forward 選定 (2026-06-06, max-price≤5000円) ──
    ("6762.T", "ＴＤＫ",                             "MOM"),  # 4,111円 200株≒822k  folds=2 PF6.42 WR69.0% DD=12.4% Shrp2.83
    ("8237.T", "松屋",                               "MOM"),  # 1,524円 600株≒914k  folds=2 PF3.07 WR75.0% DD=14.9% Shrp2.34
    ("7242.T", "カヤバ",                             "MOM"),  # 3,970円 200株≒794k  folds=2 PF2.99 WR71.7% DD=10.1% Shrp2.52
    ("1951.T", "エクシオグループ",                   "MOM"),  # 2,830円 300株≒849k  folds=2 PF5.69 WR77.4% DD=10.5% Shrp3.29
    ("4554.T", "富士製薬工業",                       "MOM"),  # 2,184円 400株≒874k  folds=2 PF2.72 WR71.8% DD=10.5% Shrp1.77
    # ── VOL: Walk-forward 選定 (2026-06-06, max-price≤5000円) ──
    ("7013.T", "ＩＨＩ",                             "VOL"),  # 2,574円 300株≒772k  folds=2 PF3.6  WR77.4% DD=11.9% Shrp2.78
    ("4390.T", "ＩＰＳ",                             "VOL"),  # 4,000円 200株≒800k  folds=2 PF4.33 WR77.5% DD=11.5% Shrp3.26
    ("1803.T", "清水建設",                           "VOL"),  # 2,514円 300株≒754k  folds=2 PF4.29 WR62.5% DD=10.5% Shrp2.84
    ("1952.T", "新日本空調",                         "VOL"),  # 3,315円 300株≒994k  folds=2 PF5.65 WR65.0% DD=7.5%  Shrp2.66
    ("9887.T", "松屋フーズホールディングス",         "VOL"),  # 4,415円 200株≒883k  folds=2 PF7.73 WR90.0% DD=6.1%  Shrp3.09
    ("6952.T", "カシオ計算機",                       "VOL"),  # 1,836円 500株≒918k  folds=2 PF6.79 WR70.8% DD=6.4%  Shrp3.01
    ("6250.T", "やまびこ",                           "VOL"),  # 3,650円 200株≒730k  folds=2 PF5.88 WR63.3% DD=13.5% Shrp1.14
]

# ── パラメータ (プリセット切替) ───────────────────────────────────
# TRADING_MODE 環境変数 or --aggressive CLI で aggressive を選択
# デフォルトは conservative (現行踏襲)
STRATEGY_PARAMS_CONSERVATIVE = {
    "DON":   (calc_donchian,       0.0, 1.5, 3.0),
    "VOL":   (calc_vol_breakout,   0.0, 1.5, 3.0),
    "VOLTF": (calc_vol_breakout_tf, 0.0, 1.5, 3.0),  # VOL+MA50 (falling knife除外)
    "MOM":   (calc_momentum,       0.0, 1.5, 3.0),
}
# aggressive: sm=1.5/tm=2.0 (run_signals_prime.py / scan_walkforward と統一)
STRATEGY_PARAMS_AGGRESSIVE = {
    "DON":   (calc_donchian,       0.0, 1.5, 2.0),   # 目標 +6% / 損切 -4.5% (1.33R)
    "VOL":   (calc_vol_breakout,   0.0, 1.5, 2.0),
    "VOLTF": (calc_vol_breakout_tf, 0.0, 1.5, 2.0),
    "MOM":   (calc_momentum,       0.0, 1.5, 2.0),
}

import os as _os
# デフォルトは conservative (標準)。--aggressive で積極利確。
TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()
if TRADING_MODE == "aggressive":
    STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
else:
    STRATEGY_PARAMS = STRATEGY_PARAMS_CONSERVATIVE
    TRADING_MODE = "conservative"

ENTRY_TYPE = "stop"   # 逆指値（高値 ≥ 前日終値 で約定）


def calc_recommend_score(period_results: dict) -> tuple[int, str]:
    """
    バックテスト成績からおすすめスコア(0-100)とランクを計算。
      勝率     : 最大40点
      PF       : 最大30点（PF=10でキャップ、∞は10扱い）
      期間安定性: 最大20点（プラス期間数 / 有効期間数）
      取引回数  : 最大10点（20取引で満点）
    """
    results = [r for r in period_results.values() if r and r.get("trades", 0) > 0]
    if not results:
        return 0, "-"

    avg_wr   = sum(r["win_rate"] for r in results) / len(results)
    avg_pf   = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                   for r in results) / len(results)
    stable   = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = max((r["trades"] for r in results), default=0)  # 重複窓を足さず実数(180日)で数える

    score = round(
        avg_wr * 0.4
        + (avg_pf / 10) * 30
        + stable * 20
        + min(t_trades / 20, 1) * 10
    )
    rank = "★★★" if score >= 80 else "★★" if score >= 60 else "★" if score >= 40 else "△"
    return score, rank


_BT_TYPE_COLORS = {"安定": "#10b981", "高WR": "#3b82f6", "高PF": "#f59e0b", "取引数": "#a855f7"}

def calc_bt_type(period_results: dict) -> str:
    """BTスコアの支配要素タイプを返す: 安定 / 高WR / 高PF / 取引数"""
    results = [r for r in period_results.values() if r and r.get("trades", 0) > 0]
    if not results:
        return "?"
    avg_wr   = sum(r["win_rate"] for r in results) / len(results)
    avg_pf   = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10) for r in results) / len(results)
    stable   = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = max((r["trades"] for r in results), default=0)  # 重複窓を足さず実数(180日)で数える
    components = {
        "安定":  stable,
        "高WR":  avg_wr / 100,
        "高PF":  avg_pf / 10,
        "取引数": min(t_trades / 20, 1),
    }
    return max(components, key=components.get)


def _bt_type_badge(period_results: dict) -> str:
    """小さなインラインバッジ HTML を返す。"""
    bt_type = calc_bt_type(period_results)
    color = _BT_TYPE_COLORS.get(bt_type, "#94a3b8")
    return (f'<br><small style="background:{color}22;color:{color};'
            f'padding:1px 5px;border-radius:3px;font-size:10px">{bt_type}</small>')


def apply_atr_penalty(score: int, stop_loss_pct: float) -> tuple[int, str]:
    """
    損切り幅(ATR幅)が広い時にスコアを減点。
    基準7%以下: ペナルティなし。7%超: 線形減点、最大50%減(37%超で頭打ち)。
    Returns: (adjusted_score, atr_note)  atr_noteは "" (ペナルティなし) or "17.8%" 形式
    """
    if stop_loss_pct <= 7.0:
        return score, ""
    multiplier = max(0.5, 1.0 - (stop_loss_pct - 7.0) / 30.0)
    return round(score * multiplier), f"{stop_loss_pct:.1f}%"


def check_signal_on_date(symbol: str, strategy: str,
                         target_date=None) -> dict | None:
    """target_date の前営業日にシグナルが出ているか確認。"""
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, 365)
    if df is None or len(df) < 5:
        return None
    try:
        df = calc_fn(df)
    except Exception:
        return None

    if target_date is None:
        next_idx = -1
        prev_idx = -1   # 最新足のみ判定（連続シグナルでも当日分だけ表示）
    else:
        ts = pd.Timestamp(target_date)
        cands = df.index[df.index <= ts]
        if len(cands) < 1:
            return None
        prev_idx = df.index.get_loc(cands[-1])  # 指定日そのものを判定
        next_idx = prev_idx

    prev      = df.iloc[prev_idx]
    next_row  = df.iloc[next_idx]
    entry_sig = bool(prev.get("entry_sig", False))
    atr_v     = float(prev.get("atr", 0))
    if not entry_sig or atr_v <= 0:
        return None

    close_prev = float(prev["close"])
    current_p  = float(next_row["close"])
    if target_date is None:
        current_p = _fetch_live_price(symbol, current_p)

    # 逆指値: 終値 + ATR×em（em=0.0なら終値ちょうど）
    order_p     = close_prev + atr_v * em
    sl          = order_p - atr_v * sm
    tp          = order_p + atr_v * tm
    # 逆指値→指値注文の指値上限 (kabu 発注時 AfterHitPrice 用)
    limit_entry = order_p * (1.0 + LIMIT_ENTRY_MARGIN_PCT)
    stop_loss_pct = (order_p - sl) / order_p * 100 if order_p > 0 else 0.0

    sig_dt   = df.index[prev_idx]
    sig_date = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else str(sig_dt)

    qty        = calc_qty(order_p, sl)
    position_v = round(order_p * qty)

    return dict(
        order_price=round_to_tick(order_p),
        limit_entry_price=round_to_tick(limit_entry),
        stop_price=round_to_tick(sl),
        target_price=round_to_tick(tp),
        current_price=current_p,
        signal_date=sig_date,
        signal_price=round(close_prev, 0),
        stop_loss_pct=round(stop_loss_pct, 1),
        qty=qty,
        position_value=position_v,
    )


def backtest_one(symbol: str, name: str, strategy: str,
                 max_hold: int | None = None) -> dict | None:
    calc_fn, em, sm, tm = STRATEGY_PARAMS[strategy]
    df = fetch(symbol, max(PERIODS))
    if df is None:
        return None

    full_r = run_limit_backtest(symbol, name, df, calc_fn,
                                em, sm, tm, max(PERIODS), strategy,
                                entry_type=ENTRY_TYPE,
                                max_hold=max_hold)
    if not full_r:
        return None

    today  = datetime.now(JST).date()
    period_results: dict[int, dict] = {}
    for days in PERIODS:
        cutoff = today - timedelta(days=days)
        # 表示用: 発注中も含める（銘柄詳細タブで当日シグナルを可視化するため）
        sub_display = [t for t in full_r["trade_log"]
                       if t["signal_dt"].date() >= cutoff]
        # 統計・スコア計算用: 発注中・保有中を除外（未決済ポジションはスコアに影響させない）
        sub = [t for t in sub_display if t.get("reason") not in ("発注中", "保有中")]
        if not sub_display:
            continue
        filled = len(sub)
        wins   = sum(1 for t in sub if t["pnl"] > 0)
        losses = sum(1 for t in sub if t["pnl"] <= 0)
        gp     = sum(t["pnl"] for t in sub if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        period_results[days] = dict(
            symbol=symbol, name=name, strategy=strategy,
            signals=full_r["signals"], filled=filled,
            trades=filled, wins=wins, losses=losses,
            win_rate=wins / filled * 100 if filled else 0.0,
            pf=pf, total_pnl=sum(t["pnl"] for t in sub),
            total_fee=sum(t.get("fee", 0) for t in sub),
            slippage_pct=full_r["slippage_pct"],
            fee_pct_one_way=full_r["fee_pct_one_way"],
            avg_hold=sum(t["hold_days"] for t in sub) / filled if filled else 0.0,
            fill_rate=full_r["fill_rate"],
            trade_log=sub_display,  # 表示用は保有中を含む
        )

    return dict(symbol=symbol, name=name, strategy=strategy,
                period_results=period_results, today_sig=None)


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def build_html(all_items: list[dict], show_days: int,
               date_label: str = "本日", run_cmd: str = "") -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # サマリー (戦略別に trade_log を結合してリスク指標計算)
    strategy_summary: dict[str, dict] = {}
    for item in all_items:
        strat = item["strategy"]
        if strat not in strategy_summary:
            strategy_summary[strat] = dict(
                trades=0, wins=0, pnl=0.0, gp=0.0, gl=0.0, trade_log=[])
        pr = compute_period_result(item, show_days)
        if pr:
            strategy_summary[strat]["trades"] += pr["trades"]
            strategy_summary[strat]["wins"]   += pr["wins"]
            strategy_summary[strat]["pnl"]    += pr["total_pnl"]
            for t in pr.get("trade_log", []):
                if t["pnl"] > 0:
                    strategy_summary[strat]["gp"] += t["pnl"]
                else:
                    strategy_summary[strat]["gl"] += abs(t["pnl"])
            strategy_summary[strat]["trade_log"].extend(pr.get("trade_log", []))

    # ベンチマーク (日経平均) リターン
    n225_ret = fetch_n225_return(show_days)

    summary_rows = ""
    for strat in ["DON", "VOL", "MOM"]:
        s = strategy_summary.get(strat)
        if not s or s["trades"] == 0:
            continue
        wr  = s["wins"] / s["trades"] * 100
        pf  = s["gp"] / s["gl"] if s["gl"] > 0 else (float("inf") if s["gp"] > 0 else 0)
        cls = "profit" if s["pnl"] >= 0 else "loss"
        # リスク指標
        enriched = enrich_backtest_result({"trade_log": s["trade_log"]}, _INITIAL_CASH)
        max_dd_pct = enriched.get("max_drawdown_pct", 0.0)
        max_cl     = enriched.get("max_consecutive_losses", 0)
        sharpe     = enriched.get("sharpe", 0.0)
        hs         = enriched.get("hold_stats", {})
        # ベンチマーク相対 α
        strat_ret_pct = s["pnl"] / _INITIAL_CASH * 100 if _INITIAL_CASH > 0 else 0
        alpha         = strat_ret_pct - n225_ret
        alpha_cls     = "profit" if alpha >= 0 else "loss"
        dd_cls        = "profit" if max_dd_pct < 10 else ("loss" if max_dd_pct > 20 else "")
        # 平均保有日数の表示 (メイン + 理由別内訳)
        hold_break    = []
        if hs.get("target_n", 0):
            hold_break.append(f"目標{hs['target_avg']:.1f}({hs['target_n']})")
        if hs.get("stop_n", 0):
            hold_break.append(f"損切{hs['stop_avg']:.1f}({hs['stop_n']})")
        if hs.get("tc_n", 0):
            hold_break.append(f"TC{hs['tc_avg']:.0f}({hs['tc_n']})")
        if hs.get("same_day_n", 0):
            hold_break.append(f"同日({hs['same_day_n']})")
        hold_break_str = " / ".join(hold_break) if hold_break else ""
        summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td>{s['trades']}</td><td>{s['wins']}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf)}</td>
          <td class="{cls}">{s['pnl']:+,.0f}円</td>
          <td class="{dd_cls}">{max_dd_pct:.1f}%</td>
          <td>{max_cl}</td>
          <td>{sharpe:.2f}</td>
          <td class="{alpha_cls}">{alpha:+.1f}%</td>
          <td>{hs.get('avg', 0):.1f}日<br><small class="hold-break">{hold_break_str}</small></td>
        </tr>"""

    # シグナル行（当日新規のみ。ルックバック継続/保有中は除外）
    def _signal_sort_key(item):
        wf = get_wf_score(item["symbol"], item["strategy"])
        return wf[0] if wf else calc_recommend_score(item["period_results"])[0]

    def _atr_badge(stop_pct: float) -> str:
        if stop_pct > 15:
            return "<span style='background:#ef4444;color:white;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR大</span>"
        if stop_pct > 10:
            return "<span style='background:#f97316;color:white;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR高</span>"
        if stop_pct > 7:
            return "<span style='background:#eab308;color:#111;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR↑</span>"
        return ""

    signal_items = [item for item in all_items
                    if item["today_sig"]
                    and not item["today_sig"].get("_pending_lookback")
                    and not item["today_sig"].get("_filled_holding")]
    signal_items.sort(key=_signal_sort_key, reverse=True)

    signal_rows = ""
    for item in signal_items:
        sig      = item["today_sig"]
        strat    = item["strategy"]
        stop_pct = sig.get("stop_loss_pct", 0.0)
        wf = get_wf_score(item["symbol"], strat)
        if wf:
            score, rank = wf
            score_label = f"{score}点<br><small style='color:#94a3b8;font-size:10px'>WF</small>"
        else:
            score, rank = calc_recommend_score(item["period_results"])
            score_label = f"{score}点<br><small style='color:#f59e0b;font-size:10px'>参考</small>"
        score_label += _bt_type_badge(item["period_results"])
        rank_cls = {"★★★": "rank-s", "★★": "rank-a", "★": "rank-b"}.get(rank, "rank-c")
        if wf and score >= 70:
            row_style = "border-left:3px solid #22c55e"
        elif wf and score >= 55:
            row_style = "border-left:3px solid #f59e0b"
        elif wf:
            row_style = "border-left:3px solid #ef4444"
        else:
            row_style = ""
        _sig_dt = pd.to_datetime(sig['signal_date'])
        _max_exit = pd.bdate_range(start=_sig_dt, periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
        max_exit_str = _max_exit.strftime("%Y-%m-%d")
        cpcv_info = CPCV_FLAGS.get(item["symbol"])
        if cpcv_info:
            _lvl = cpcv_info["level"]
            _rsn = cpcv_info["reason"].replace('"', "&quot;")
            if _lvl == "danger":
                cpcv_badge = (f'<br><span title="{_rsn}" style="cursor:help;'
                              f'color:#ef4444;font-size:10px;font-weight:700">'
                              f'❌ CPCV警告</span>')
            else:
                cpcv_badge = (f'<br><span title="{_rsn}" style="cursor:help;'
                              f'color:#fbbf24;font-size:10px;font-weight:700">'
                              f'⚠️ CPCV要注意</span>')
        else:
            cpcv_badge = ""
        risk_badges    = _render_risk_badges(item["symbol"])
        earnings_badge = _render_earnings_date(item["symbol"])
        _sym_code = item['symbol'].split('.')[0]
        _reg_qty  = sig.get('qty', 100)
        _reg_url  = (f"http://127.0.0.1:8765/?prefill=1"
                     f"&symbol={_sym_code}"
                     f"&entry={sig['order_price']:.0f}"
                     f"&stop={sig['stop_price']:.0f}"
                     f"&target={sig['target_price']:.0f}"
                     f"&strategy={strat}"
                     f"&qty={_reg_qty}")
        signal_rows += f"""
        <tr style="{row_style}">
          <td class="sym">{item['symbol']}<br><small>{item['name']}</small>{cpcv_badge}{risk_badges}{earnings_badge}</td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span>{_atr_badge(stop_pct)}</td>
          <td class="score-cell"><span class="{rank_cls}">{rank}</span><br>{score_label}</td>
          <td>{sig['signal_date']}</td>
          <td>{sig['signal_price']:,.0f}</td>
          <td>{sig['current_price']:,.0f}</td>
          <td class="stop">{sig['order_price']:,.0f}</td>
          <td class="limit-entry">{sig.get('limit_entry_price', sig['order_price']):,.0f}</td>
          <td class="loss">{sig['stop_price']:,.0f}<br><small style="color:#94a3b8;font-size:10px">-{stop_pct:.1f}%</small></td>
          <td class="profit">{sig['target_price']:,.0f}</td>
          <td style="color:#e2e8f0;text-align:right">{sig.get('qty', '-')}株<br><small style="color:#94a3b8;font-size:10px">{sig.get('position_value', 0):,.0f}円</small></td>
          <td style="color:#94a3b8">{MAX_HOLD}日</td>
          <td style="color:#f59e0b;font-size:12px">{max_exit_str}</td>
          <td><a href="{_reg_url}" target="_blank" class="reg-btn">📥 登録</a></td>
        </tr>"""
    if not signal_rows:
        signal_rows = f'<tr><td colspan="14" style="text-align:center;color:#94a3b8">{date_label} シグナルなし</td></tr>'

    # 4期間比較
    period_headers  = "".join(f"<th colspan='4'>{p}日</th>" for p in PERIODS)
    period_subheads = "<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(PERIODS)

    stock_rows = ""
    for strat in ["DON", "VOL", "MOM"]:
        items = [i for i in all_items if i["strategy"] == strat]
        items.sort(key=lambda x: (compute_period_result(x, show_days)).get("total_pnl", -999999), reverse=True)
        for item in items:
            cells = ""
            for p in PERIODS:
                r = item["period_results"].get(p)
                if not r:
                    cells += "<td>-</td><td>-</td><td>-</td><td>-</td>"
                else:
                    pc = "profit" if r["total_pnl"] >= 0 else "loss"
                    cells += (f"<td>{r['trades']}</td>"
                              f"<td>{r['win_rate']:.0f}%</td>"
                              f"<td>{_pf_str(r['pf'])}</td>"
                              f"<td class='{pc}'>{r['total_pnl']:+,.0f}</td>")
            # show_days 期間の平均保有日数
            pr_show   = compute_period_result(item, show_days)
            hs_item   = calc_hold_stats(pr_show.get("trade_log", []))
            hold_cell = f"{hs_item['avg']:.1f}日" if hs_item["count"] > 0 else "-"
            sig_m = item["today_sig"]
            mark = ("🔔" if sig_m
                         and not sig_m.get("_pending_lookback")
                         and not sig_m.get("_filled_holding")
                    else "")
            wf_item = get_wf_score(item["symbol"], strat)
            if wf_item:
                wf_s, wf_r = wf_item
                wf_cls  = "profit" if wf_s >= 70 else ("" if wf_s >= 55 else "loss")
                wf_cell = f'<span class="{wf_cls}">{wf_r} {wf_s}</span>'
            else:
                wf_cell = '<span style="color:#64748b">-</span>'
            wf_cell += _bt_type_badge(item["period_results"])
            stock_rows += f"""
        <tr>
          <td class="sym">{item['symbol']}{mark}<br><small>{item['name']}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td class="score-cell">{wf_cell}</td>
          {cells}
          <td>{hold_cell}</td>
        </tr>"""

    # 個別トレード
    trade_sections = ""
    for item in all_items:
        pr   = compute_period_result(item, show_days)
        logs = pr.get("trade_log") or []
        if not logs and not item.get("today_sig"):
            continue
        trade_rows     = ""
        fill_days_list = []
        for t in logs:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            e_str   = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            x_str   = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
            sig_dt  = t.get("signal_dt")
            s_str   = sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else (str(sig_dt) if sig_dt else "-")
            # 最大決済日: シグナル日 + ENTRY_EXPIRE + MAX_HOLD 営業日
            if sig_dt is not None:
                _max_exit = pd.bdate_range(start=pd.to_datetime(sig_dt), periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
                max_exit_str = _max_exit.strftime("%Y-%m-%d")
            else:
                max_exit_str = "-"
            s_p     = t.get("signal_price", "-")
            s_p_str = f"{s_p:,.0f}" if isinstance(s_p, float) else str(s_p)
            dtf     = t.get("days_to_fill", "-")
            fill_days_list.append(dtf) if isinstance(dtf, int) else None
            ol  = t.get("order_limit")
            osl = t.get("order_stop")
            otg = t.get("order_target")
            ole = ol * (1.0 + LIMIT_ENTRY_MARGIN_PCT) if isinstance(ol, float) else None
            ol_str  = f"{ol:,.0f}"  if isinstance(ol,  float) else str(ol  or "-")
            osl_str = f"{osl:,.0f}" if isinstance(osl, float) else str(osl or "-")
            otg_str = f"{otg:,.0f}" if isinstance(otg, float) else str(otg or "-")
            ole_str = f"{ole:,.0f}" if isinstance(ole, float) else "-"
            trade_rows += f"""
              <tr>
                <td>{s_str}</td><td class="stop">{s_p_str}</td>
                <td>{e_str}</td><td>{x_str}</td>
                <td class="stop">{ol_str}</td>
                <td class="limit-entry">{ole_str}</td>
                <td class="loss">{osl_str}</td>
                <td class="profit">{otg_str}</td>
                <td>{t['entry_p']:,.0f}</td><td>{t['exit_p']:,.0f}</td>
                <td>{t['qty']}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['hold_days']}日</td>
                <td class="stop">{dtf}日</td>
                <td style="color:#f59e0b;font-size:12px">{max_exit_str}</td>
                <td>{t['reason']}</td>
              </tr>"""
        # 未約定/保有中シグナルを取引詳細の末尾に追記
        if item.get("today_sig"):
            sig = item["today_sig"]
            # バックテスト trade_log に同シグナル日のトレードが既存なら重複しない
            _sig_date_str = sig["signal_date"]
            _already = any(
                t.get("signal_dt") is not None
                and t["signal_dt"].strftime("%Y-%m-%d") == _sig_date_str
                for t in logs
            )
            if not _already:
                _sig_dt_p   = pd.to_datetime(sig["signal_date"])
                _max_exit_p = pd.bdate_range(start=_sig_dt_p, periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
                max_exit_pending = _max_exit_p.strftime("%Y-%m-%d")
                ol_p  = sig["order_price"]
                ole_p = sig.get("limit_entry_price", round(ol_p * (1 + LIMIT_ENTRY_MARGIN_PCT), 0))
                if sig.get("_filled_holding"):
                    row_bg    = "background:rgba(34,197,94,0.12)"
                    ep        = sig.get("_entry_price", ol_p)
                    cp        = sig.get("_current_latest", sig["current_price"])
                    upnl      = sig.get("_unreal_pnl", 0)
                    upct      = sig.get("_unreal_pct", 0)
                    hd        = sig.get("_hold_days", 0)
                    fd        = sig.get("_fill_days", "-")
                    qty       = sig.get("_qty", 100)
                    fill_dt   = sig.get("_fill_date", "-")
                    pnl_cls   = "profit" if upnl >= 0 else "loss"
                    reason_td = '<td style="color:#4ade80">保有中</td>'
                    trade_rows += f"""
              <tr style="{row_bg}">
                <td>{sig['signal_date']}</td><td class="stop">{sig['signal_price']:,.0f}</td>
                <td>{fill_dt}</td><td>-</td>
                <td class="stop">{ol_p:,.0f}</td>
                <td class="limit-entry">{ole_p:,.0f}</td>
                <td class="loss">{sig['stop_price']:,.0f}</td>
                <td class="profit">{sig['target_price']:,.0f}</td>
                <td>{ep:,.0f}</td><td>{cp:,.0f}</td>
                <td>{qty}</td>
                <td class="{pnl_cls}">{upnl:+,.0f}</td>
                <td class="{pnl_cls}">{upct:+.2f}%</td>
                <td>{hd}日</td><td>{fd}日</td>
                <td style="color:#f59e0b;font-size:12px">{max_exit_pending}</td>
                {reason_td}
              </tr>"""
                elif sig.get("_pending_lookback"):
                    row_bg    = "background:rgba(245,158,11,0.12)"
                    reason_td = '<td style="color:#f59e0b">⏳ 未約定（継続中）</td>'
                    trade_rows += f"""
              <tr style="{row_bg}">
                <td>{sig['signal_date']}</td><td class="stop">{sig['signal_price']:,.0f}</td>
                <td>-</td><td>-</td>
                <td class="stop">{ol_p:,.0f}</td>
                <td class="limit-entry">{ole_p:,.0f}</td>
                <td class="loss">{sig['stop_price']:,.0f}</td>
                <td class="profit">{sig['target_price']:,.0f}</td>
                <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                <td style="color:#f59e0b;font-size:12px">{max_exit_pending}</td>
                {reason_td}
              </tr>"""
                else:
                    row_bg    = "background:rgba(245,158,11,0.12)"
                    reason_td = '<td style="color:#f59e0b">⏳ 未約定</td>'
                    trade_rows += f"""
              <tr style="{row_bg}">
                <td>{sig['signal_date']}</td><td class="stop">{sig['signal_price']:,.0f}</td>
                <td>-</td><td>-</td>
                <td class="stop">{ol_p:,.0f}</td>
                <td class="limit-entry">{ole_p:,.0f}</td>
                <td class="loss">{sig['stop_price']:,.0f}</td>
                <td class="profit">{sig['target_price']:,.0f}</td>
                <td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>
                <td style="color:#f59e0b;font-size:12px">{max_exit_pending}</td>
                {reason_td}
              </tr>"""
        strat     = item["strategy"]
        pnl_total = pr.get("total_pnl", 0)
        pc2       = "profit" if pnl_total >= 0 else "loss"
        if fill_days_list:
            avg_f    = sum(fill_days_list) / len(fill_days_list)
            dist     = {d: fill_days_list.count(d) for d in sorted(set(fill_days_list))}
            dist_str = " / ".join(f"{d}日:{n}回" for d, n in dist.items())
            fill_stat = f'<p class="fill-stat">約定日数 — 平均:{avg_f:.1f}日 最短:{min(fill_days_list)}日 最長:{max(fill_days_list)}日 | 分布: {dist_str}</p>'
        else:
            fill_stat = ""
        # 保有日数統計 (理由別内訳付き)
        hs_sec = calc_hold_stats(logs)
        hold_stat = ""
        if hs_sec["count"] > 0:
            hold_break = []
            if hs_sec["target_n"]:
                hold_break.append(f"目標{hs_sec['target_avg']:.1f}日({hs_sec['target_n']})")
            if hs_sec["stop_n"]:
                hold_break.append(f"損切{hs_sec['stop_avg']:.1f}日({hs_sec['stop_n']})")
            if hs_sec["tc_n"]:
                hold_break.append(f"TC{hs_sec['tc_avg']:.0f}日({hs_sec['tc_n']})")
            if hs_sec["same_day_n"]:
                hold_break.append(f"同日({hs_sec['same_day_n']})")
            if hs_sec["held_n"]:
                hold_break.append(f"保有中{hs_sec['held_avg']:.1f}日({hs_sec['held_n']})")
            brk = " / ".join(hold_break)
            hold_stat = f'<p class="hold-stat">保有日数 — 平均:{hs_sec["avg"]:.1f}日 | 内訳: {brk}</p>'
        trade_sections += f"""
      <div class="trade-section">
        <h3>{item['symbol']} {item['name']}
          <span class="tag tag-{strat.lower()}">{strat}</span>
          <span class="{pc2}">{pnl_total:+,.0f}円</span>
          <small>（{show_days}日）</small>
        </h3>
        {fill_stat}
        {hold_stat}
        <table>
          <thead><tr>
            <th>シグナル日</th><th>シグナル時株価</th>
            <th>エントリー</th><th>エグジット</th>
            <th>逆指値</th><th>指値上限<br><small>(+{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%)</small></th><th>損切り</th><th>目標価格</th>
            <th>エントリー価格</th><th>エグジット価格</th>
            <th>数量</th><th>損益(円)</th><th>損益(%)</th>
            <th>保有日数</th><th>約定日数</th><th>最大決済日</th><th>理由</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>ブレイクアウト 逆指値バックテスト — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym    {{ text-align:left; font-weight:600; min-width:120px; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .stop   {{ color:#38bdf8; }}
  .limit-entry {{ color:#fb923c; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-don {{ background:#0e7490; color:#cffafe; }}
  .tag-vol {{ background:#92400e; color:#fef3c7; }}
  .tag-mom {{ background:#4d7c0f; color:#ecfccb; }}
  .signal-badge {{ background:#38bdf8; color:#000; padding:2px 8px; border-radius:4px; font-size:0.8rem; }}
  .trade-section {{ margin-bottom:20px; }}
  .fill-stat {{ color:#38bdf8; font-size:0.82rem; margin-bottom:6px; }}
  .hold-stat {{ color:#a5b4fc; font-size:0.82rem; margin-bottom:6px; }}
  .hold-break {{ color:#94a3b8; font-size:0.70rem; font-weight:normal; white-space:nowrap; }}
  .reg-btn {{ display:inline-block; padding:4px 8px; background:#2d6cdf; color:#fff;
              border-radius:5px; font-size:12px; text-decoration:none; white-space:nowrap; }}
  .reg-btn:hover {{ background:#1e4fc0; }}
  .rank-s {{ background:#fbbf24; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-a {{ background:#4ade80; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-b {{ background:#38bdf8; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .rank-c {{ background:#94a3b8; color:#000; padding:2px 6px; border-radius:4px; font-weight:700; }}
  .score-cell {{ text-align:center; }}
</style>
</head>
<body>
<h1>ブレイクアウト戦略 逆指値エントリー バックテスト</h1>
<p class="subtitle">
  生成日: {today_str} ／ シグナル確認日: {date_label} ／ 表示期間: {show_days}日 ／
  <span style="color:#fbbf24">モード: <strong>{TRADING_MODE}</strong></span><br>
  エントリー: <strong>逆指値</strong>（高値 ≥ 前日終値 で約定 ＝ 上がれば買う）<br>
  コストモデル: スリッページ <strong>{SLIPPAGE_STOP_PCT*100:.2f}%</strong>（逆指値買い+/損切り売り-）／
  手数料 <strong>片道 {FEE_PCT_ONE_WAY*100:.2f}%</strong>（往復 {FEE_PCT_ONE_WAY*200:.2f}%）／
  ベンチマーク: 日経平均 ({show_days}日) <strong>{n225_ret:+.1f}%</strong><br>
  <span style="color:#cffafe">DON: ドンチャン高値ブレイク</span> ／
  <span style="color:#fef3c7">VOL: 出来高急増ブレイク</span> ／
  <span style="color:#ecfccb">MOM: モメンタムブレイク</span>
  {f'<br>▶ 実行: <code style="background:#0f172a;padding:2px 8px;border-radius:4px;color:#38bdf8;font-size:0.88rem">{run_cmd}</code>' if run_cmd else ""}
</p>

<h2>戦略サマリー（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益合計</th>
    <th>MaxDD%</th><th>連敗</th><th>Sharpe</th><th>α vs 日経</th>
    <th>平均保有<br><small style="font-weight:normal">日数（内訳：件数）</small></th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>シグナル ({date_label}) <span class="signal-badge">要確認</span></h2>
<p style="color:#94a3b8;font-size:0.82rem;margin-bottom:8px">
  ※ 逆指値注文（青色）= 翌日高値がこの価格以上になれば発動<br>
  ※ 指値上限（橙色, +{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%）= 逆指値→指値発注時の指値。寄付ギャップがこれ以下なら約定、超えたら不約定
</p>
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>スコア</th><th>シグナル日</th><th>シグナル時株価</th>
    <th>現在値</th><th>逆指値<br><small>(トリガー)</small></th><th>指値上限<br><small>(+{LIMIT_ENTRY_MARGIN_PCT*100:.1f}%)</small></th><th>損切り</th><th>目標</th><th>株数<br><small>想定額</small></th><th>最大保有日</th><th>最大決済日<br><small>(約定期限+保有)</small></th><th>登録</th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
</table>

<h2>銘柄別バックテスト（4期間比較）</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">銘柄</th><th rowspan="2">戦略</th>
      <th rowspan="2" title="Walk-forwardスコア: out-of-sampleデータで評価した将来勝率の指標">WF<br>スコア</th>
      {period_headers}
      <th rowspan="2">平均<br>保有<br><small>({show_days}日)</small></th>
    </tr>
    <tr>{period_subheads}</tr>
  </thead>
  <tbody>{stock_rows}</tbody>
</table>

<h2>個別トレード一覧（{show_days}日）</h2>
{trade_sections}

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="ブレイクアウト戦略 逆指値バックテスト")
    parser.add_argument("--days",        type=int,  default=365)
    parser.add_argument("--date",        type=str,  default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int,  default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    print(f"ブレイクアウトバックテスト開始 ({len(WATCHLIST)}銘柄) シグナル確認日: {date_label}...", flush=True)

    all_items: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in WATCHLIST}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass
            if done % 6 == 0 or done == len(WATCHLIST):
                print(f"  {done}/{len(WATCHLIST)} 完了", flush=True)

    order = {(s, st): i for i, (s, _, st) in enumerate(WATCHLIST)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    print(f"  シグナル確認中 ({date_label})...", flush=True)
    for item in all_items:
        item["today_sig"] = check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 85)
    print(f"  ブレイクアウト 逆指値バックテスト  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 85)

    signals_today = [(i, calc_recommend_score(i["period_results"]))
                     for i in all_items if i["today_sig"]]
    signals_today.sort(key=lambda x: x[1][0], reverse=True)
    print(f"\n【シグナル ({date_label})】 {len(signals_today)}件")
    if signals_today:
        print(f"  {'銘柄':<12} {'名前':<24} {'戦略':<5} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値':>8} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 111)
        for item, (score, rank) in signals_today:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12} {item['name']:<24} {item['strategy']:<5}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    show_days = args.days
    print(f"\n【銘柄別バックテスト ({show_days}日)】")
    print(f"  {'銘柄':<12} {'名前':<24} {'戦略':<5} {'取引':>4} {'勝率':>6} {'PF':>6} {'損益':>10}")
    print("  " + "-" * 72)
    for strat in ["DON", "VOL", "MOM"]:
        for item in [i for i in all_items if i["strategy"] == strat]:
            r = compute_period_result(item, show_days)
            if not r:
                print(f"  {item['symbol']:<12} {item['name']:<24} {strat:<5} データなし")
                continue
            print(f"  {item['symbol']:<12} {item['name']:<24} {strat:<5}"
                  f" {r['trades']:>4} {r['win_rate']:>5.1f}% {_pf_str(r['pf']):>6}"
                  f" {r['total_pnl']:>+10,.0f}円")

    date_suffix = args.date if args.date else today
    out_path    = Path(f"watchlist_breakout_{date_suffix}.html")
    out_path.write_text(build_html(all_items, show_days, date_label), encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        open_html(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
