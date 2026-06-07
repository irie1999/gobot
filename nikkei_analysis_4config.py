"""
nikkei_analysis_4config.py  ―  4config 統合レポート (既存タブ + トレンド×相性タブ拡張)

以下の4configを1枚のHTMLで比較分析する:
  v2新WL conservative  : 2026-06-04 WFスキャン (全価格帯, tm=3.0)
  v2新WL aggressive    : 2026-06-05 WFスキャン (全価格帯, tm=2.0)
  5k-WL conservative   : 2026-06-06 WFスキャン (≤5000円,  tm=3.0)
  5k-WL aggressive     : 2026-06-06 WFスキャン (≤5000円,  tm=2.0)

nikkei_analysis.py の既存5タブ（シグナル判定/トレンド期間/エントリー分析/シグナル/損益）を
そのまま保持したうえで、t1「シグナル判定」内のスクリプト判定後に4設定カードを注入し、
新規に「📊 トレンド×相性」タブを追加する。

使い方:
  python nikkei_analysis_4config.py                    # 過去365日 HTML & ブラウザ表示
  python nikkei_analysis_4config.py --days 180         # 直近180日
  python nikkei_analysis_4config.py --no-browser       # HTML生成のみ
  python nikkei_analysis_4config.py --date 2026-01-01  # 指定日分析
  python nikkei_analysis_4config.py --years 5          # 過去5年

  # 銘柄×戦略の個別履歴確認
  python nikkei_analysis_4config.py --symbol 8387.T --strategy A7
  python nikkei_analysis_4config.py --symbol 8387.T --strategy RSI2 --days 180
  python nikkei_analysis_4config.py --symbol 8387.T --strategy MACD --aggressive
"""
from __future__ import annotations

import argparse
import copy as _copy
import importlib as _importlib
import os
import re as _re
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))

# ── TRADING_MODE を import 前に設定 ─────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

# ── シグナル/バックテストモジュールをロード ─────────────────────────────────
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

# ══════════════════════════════════════════════════════════════════════════════
# 5k-WL conservative  ―  check_signals_stop/breakout の現 WATCHLIST
# ══════════════════════════════════════════════════════════════════════════════
_5K_STOP_CON: list[tuple[str, str, str]] = list(_stop.WATCHLIST)
_5K_BRK_CON:  list[tuple[str, str, str]] = list(_brk.WATCHLIST)

# ══════════════════════════════════════════════════════════════════════════════
# 5k-WL aggressive  ―  2026-06-06 WFスキャン --aggressive --max-price 5000
# ══════════════════════════════════════════════════════════════════════════════
_5K_STOP_AGG: list[tuple[str, str, str]] = [
    ("8061.T", "西華産業",                           "RSI2"),
    ("2540.T", "養命酒製造",                         "RSI2"),
    ("6770.T", "アルプスアルパイン",                 "RSI2"),
    ("8387.T", "四国銀行",                           "RSI2"),
    ("1930.T", "北陸電気工事",                       "RSI2"),
    ("5821.T", "平河ヒューテック",                   "RSI2"),
    ("8070.T", "東京産業",                           "RSI2"),
    ("6788.T", "日本トリム",                         "RSI2"),
    ("7167.T", "めぶきフィナンシャルグループ",       "RSI2"),
    ("4631.T", "ＤＩＣ",                             "RSI2"),
    ("8386.T", "百十四銀行",                         "MACD"),
    ("7322.T", "三十三フィナンシャルグループ",       "MACD"),
    ("1417.T", "ミライト・ワン",                     "MACD"),
    ("7181.T", "かんぽ生命保険",                     "MACD"),
    ("8387.T", "四国銀行",                           "MACD"),
    ("8877.T", "エスリード",                         "MACD"),
    ("9024.T", "西武ホールディングス",               "MACD"),
    ("1975.T", "朝日工業社",                         "MACD"),
    ("4205.T", "日本ゼオン",                         "MACD"),
    ("6914.T", "オプテックスグループ",               "MACD"),
    ("7003.T", "三井Ｅ＆Ｓ",                         "A7"),
    ("8173.T", "Ｊｏｓｈｉｎ",                       "A7"),
    ("8061.T", "西華産業",                           "A7"),
    ("6331.T", "三菱化工機",                         "A7"),
    ("1885.T", "東亜建設工業",                       "A7"),
    ("8336.T", "武蔵野銀行",                         "A7"),
    ("8346.T", "東邦銀行",                           "A7"),
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),
    ("8387.T", "四国銀行",                           "A7"),
    ("5482.T", "愛知製鋼",                           "A7"),
]
_5K_BRK_AGG: list[tuple[str, str, str]] = [
    ("3659.T", "ネクソン",                           "DON"),
    ("8237.T", "松屋",                               "MOM"),
    ("7242.T", "カヤバ",                             "MOM"),
    ("8016.T", "オンワードホールディングス",         "MOM"),
    ("1975.T", "朝日工業社",                         "MOM"),
    ("8011.T", "三陽商会",                           "MOM"),
    ("7013.T", "ＩＨＩ",                             "VOL"),
    ("1952.T", "新日本空調",                         "VOL"),
    ("7181.T", "かんぽ生命保険",                     "VOL"),
    ("3197.T", "すかいらーくホールディングス",       "VOL"),
    ("4390.T", "ＩＰＳ",                             "VOL"),
    ("6952.T", "カシオ計算機",                       "VOL"),
    ("8086.T", "ニプロ",                             "VOL"),
    ("7322.T", "三十三フィナンシャルグループ",       "VOL"),
    ("8016.T", "オンワードホールディングス",         "VOL"),
]

# ══════════════════════════════════════════════════════════════════════════════
# v2新WL conservative  ―  2026-06-04 WFスキャン (全価格帯, tm=3.0)
# ══════════════════════════════════════════════════════════════════════════════
_V2_STOP_CON: list[tuple[str, str, str]] = [
    ("7003.T", "三井Ｅ＆Ｓ",                           "A7"),
    ("6752.T", "パナソニックホールディングス",           "A7"),
    ("8360.T", "山梨中央銀行",                           "A7"),
    ("4506.T", "住友ファーマ",                           "A7"),
    ("6101.T", "ツガミ",                                 "A7"),
    ("1814.T", "大末建設",                               "A7"),
    ("8173.T", "上新電機",                               "A7"),
    ("9956.T", "バローホールディングス",                 "A7"),
    ("8544.T", "京葉銀行",                               "A7"),
    ("5831.T", "しずおかフィナンシャルグループ",         "A7"),
    ("3563.T", "ＦＯＯＤ＆ＬＩＦＥ ＣＯＭＰＡＮＩＥＳ", "RSI2"),
    ("8309.T", "三井住友トラスト・ホールディングス",     "RSI2"),
    ("8061.T", "西華産業",                               "RSI2"),
    ("5482.T", "愛知製鋼",                               "RSI2"),
    ("5821.T", "平河ヒューテック",                       "RSI2"),
    ("9960.T", "東テク",                                 "RSI2"),
    ("6237.T", "イワキポンプ",                           "RSI2"),
    ("2540.T", "養命酒製造",                             "RSI2"),
    ("8393.T", "宮崎銀行",                               "RSI2"),
    ("9531.T", "東京瓦斯",                               "MACD"),
    ("1515.T", "日鉄鉱業",                               "MACD"),
    ("9072.T", "ニッコンホールディングス",               "MACD"),
    ("8377.T", "ほくほくフィナンシャルグループ",         "MACD"),
    ("7322.T", "三十三フィナンシャルグループ",           "MACD"),
    ("5482.T", "愛知製鋼",                               "MACD"),
    ("1417.T", "ミライト・ワン",                         "MACD"),
    ("7157.T", "ライフネット生命保険",                   "MACD"),
    ("6914.T", "オプテックス",                           "MACD"),
]
_V2_BRK_CON: list[tuple[str, str, str]] = [
    ("7242.T", "カヤバ",                                 "MOM"),
    ("6762.T", "ＴＤＫ",                                 "MOM"),
    ("8237.T", "松屋",                                   "MOM"),
    ("7966.T", "リンテック",                             "MOM"),
    ("4554.T", "富士製薬工業",                           "MOM"),
    ("3964.T", "オークネット",                           "MOM"),
    ("1515.T", "日鉄鉱業",                               "DON"),
    ("4461.T", "第一工業製薬",                           "VOL"),
    ("7013.T", "ＩＨＩ",                                 "VOL"),
    ("4390.T", "ＩＰＳ",                                 "VOL"),
    ("9887.T", "松屋フーズホールディングス",             "VOL"),
    ("1803.T", "清水建設",                               "VOL"),
    ("1945.T", "東京エネシス",                           "VOL"),
    ("6952.T", "カシオ計算機",                           "VOL"),
]

# ══════════════════════════════════════════════════════════════════════════════
# v2新WL aggressive  ―  2026-06-05 WFスキャン (全価格帯, tm=2.0)
# ══════════════════════════════════════════════════════════════════════════════
_V2_STOP_AGG: list[tuple[str, str, str]] = [
    ("8014.T", "蝶理",                                   "RSI2"),
    ("4631.T", "ＤＩＣ",                                 "RSI2"),
    ("8316.T", "三井住友フィナンシャルグループ",         "RSI2"),
    ("2540.T", "養命酒製造",                             "RSI2"),
    ("8061.T", "西華産業",                               "RSI2"),
    ("6770.T", "アルプスアルパイン",                     "RSI2"),
    ("5821.T", "平河ヒューテック",                       "RSI2"),
    ("6788.T", "日本トリム",                             "RSI2"),
    ("7003.T", "三井Ｅ＆Ｓ",                             "A7"),
    ("8360.T", "山梨中央銀行",                           "A7"),
    ("8522.T", "名古屋銀行",                             "A7"),
    ("1885.T", "東亜建設工業",                           "A7"),
    ("8061.T", "西華産業",                               "A7"),
    ("4229.T", "群栄化学工業",                           "A7"),
    ("4506.T", "住友ファーマ",                           "A7"),
    ("1814.T", "大末建設",                               "A7"),
    ("8877.T", "エスリード",                             "MACD"),
    ("4205.T", "日本ゼオン",                             "MACD"),
    ("9024.T", "西武ホールディングス",                   "MACD"),
    ("1417.T", "ミライト・ワン",                         "MACD"),
    ("9072.T", "ニッコンホールディングス",               "MACD"),
    ("1975.T", "朝日工業社",                             "MACD"),
    ("8386.T", "百十四銀行",                             "MACD"),
    ("7322.T", "三十三フィナンシャルグループ",           "MACD"),
    ("7181.T", "かんぽ生命保険",                         "MACD"),
    ("8387.T", "四国銀行",                               "MACD"),
]
_V2_BRK_AGG: list[tuple[str, str, str]] = [
    ("3659.T", "ネクソン",                               "DON"),
    ("8386.T", "百十四銀行",                             "DON"),
    ("7419.T", "ノジマ",                                 "DON"),
    ("9830.T", "トラスコ中山",                           "DON"),
    ("7013.T", "ＩＨＩ",                                 "VOL"),
    ("6779.T", "日本電波工業",                           "VOL"),
    ("1952.T", "新日本空調",                             "VOL"),
    ("3197.T", "すかいらーくホールディングス",           "VOL"),
    ("4390.T", "ＩＰＳ",                                 "VOL"),
    ("7181.T", "かんぽ生命保険",                         "VOL"),
    ("6284.T", "日精エー・エス・ビー機械",               "VOL"),
    ("6952.T", "カシオ計算機",                           "VOL"),
    ("8086.T", "ニプロ",                                 "VOL"),
    ("7242.T", "カヤバ",                                 "MOM"),
    ("8011.T", "三陽商会",                               "MOM"),
    ("1975.T", "朝日工業社",                             "MOM"),
    ("8016.T", "オンワードホールディングス",             "MOM"),
]

print("=" * 70)
print("nikkei_analysis_4config: 4設定分析レポート")
print(f"  v2新WL conservative  逆指値B:{len(_V2_STOP_CON)} BRK:{len(_V2_BRK_CON)}")
print(f"  v2新WL aggressive    逆指値B:{len(_V2_STOP_AGG)} BRK:{len(_V2_BRK_AGG)}")
print(f"  5k-WL conservative   逆指値B:{len(_5K_STOP_CON)} BRK:{len(_5K_BRK_CON)}")
print(f"  5k-WL aggressive     逆指値B:{len(_5K_STOP_AGG)} BRK:{len(_5K_BRK_AGG)}")
print("=" * 70)

# ── reload フック: WATCHLISTを5k-conservative で保持 ─────────────────────────
_orig_reload = _importlib.reload

def _wl_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST = list(_5K_STOP_CON)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST = list(_5K_BRK_CON)
    return result

_importlib.reload = _wl_preserving_reload
import nikkei_analysis as _na
_importlib.reload = _orig_reload

# ── _PNL_CONFIGS を 4config に差し替え ────────────────────────────────────────
_na._PNL_CONFIGS[:] = [
    {"label": "v2新WL conservative", "color": "#06b6d4", "mode": "conservative",
     "sm_tm": None, "stop_wl": list(_V2_STOP_CON), "brk_wl": list(_V2_BRK_CON)},
    {"label": "v2新WL aggressive",   "color": "#f39c12", "mode": "aggressive",
     "sm_tm": None, "stop_wl": list(_V2_STOP_AGG), "brk_wl": list(_V2_BRK_AGG)},
    {"label": "5k-WL conservative",  "color": "#3498db", "mode": "conservative",
     "sm_tm": None, "stop_wl": list(_5K_STOP_CON), "brk_wl": list(_5K_BRK_CON)},
    {"label": "5k-WL aggressive",    "color": "#e74c3c", "mode": "aggressive",
     "sm_tm": None, "stop_wl": list(_5K_STOP_AGG), "brk_wl": list(_5K_BRK_AGG)},
]

try:
    from backtest_limit_entry import WORKERS as _DEF_WORKERS
except ImportError:
    _DEF_WORKERS = 4

CONFIGS = _na._PNL_CONFIGS

# ════════════════════════════════════════════════════════════════════════════
# 設定評価ヘルパー
# ════════════════════════════════════════════════════════════════════════════

_RISK = {"v2新WL conservative": "低中", "v2新WL aggressive": "中",
         "5k-WL conservative":  "低",   "5k-WL aggressive": "低中"}
_NOTE = {
    "v2新WL conservative": "全価格帯WFスキャン (2026-06-04) × conservative。横ばい・下落でも安定。",
    "v2新WL aggressive":   "全価格帯WFスキャン (2026-06-05) × aggressive。上昇相場で最大効率。",
    "5k-WL conservative":  "5000円以下WFスキャン (2026-06-06) × conservative。1取引~100万円。全相場対応。",
    "5k-WL aggressive":    "5000円以下WFスキャン (2026-06-06) × aggressive。上昇相場で高回転。",
}
RISK_COLOR  = {"高": "#f87171", "中高": "#fb923c", "中": "#fbbf24", "低中": "#86efac", "低": "#4ade80"}
STATUS_META = {
    "✅ 推奨": ("推奨", "#4ade80", "#052e16", "#166534"),
    "⚠️ 注意": ("注意", "#fbbf24", "#2d1f00", "#92400e"),
    "❌ 停止": ("停止", "#f87171", "#2d0a0a", "#991b1b"),
}


def _judge_config(cfg: dict, r: dict) -> tuple[str, str, str]:
    trend = r["trend"]; vol = r["vol_level"]
    mom5  = r["mom5"];  mom20 = r["mom20"]
    above = r["above_ma200"]; drop = r["max_1d_drop"]
    mode  = cfg["mode"]; crash = drop < -3.0

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
        if trend == "down" and vol == "high" and crash:
            return "⚠️ 注意", f"下落×高ボラ×急落 (最大1日{drop:+.1f}%)", "ポジション縮小を検討"
        if trend == "down" and vol == "high":
            return "⚠️ 注意", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "ポジション縮小を検討"
        return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "全相場で使用可能"


def _4cfg_section_html(r: dict) -> str:
    """t1タブのスクリプト判定後に挿入する4設定評価カードセクション。"""
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
    return f"\n<h2>4設定シグナル判定</h2>\n{cards}\n"


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
                        trades.append({
                            "entry_dt": entry_dt,
                            "pnl":      t.get("pnl", 0),
                        })
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
# HTML 注入
# ════════════════════════════════════════════════════════════════════════════

def _inject_tabs(html: str, cfg_section_html: str, tab7_html: str) -> str:
    """t1に4設定セクションを注入、t7(トレンド×相性)タブを追加、タブナビ折り返し修正。"""

    # 1. t1タブ内の「推奨コマンド」見出し直前に4設定セクションを挿入
    html = _re.sub(
        r'(<h2>[^<]*時点の推奨コマンド)',
        cfg_section_html.replace('\\', '\\\\') + r'\1',
        html,
        count=1,
    )

    # 2. tab-nav に t7 ボタンを追加
    TAB_NAV_END = '\n</div>\n\n<div id="t1"'
    if TAB_NAV_END in html:
        new_btn = '\n  <button class="tab-btn" data-tab="t7" onclick="switchTab(\'t7\')">📊 トレンド×相性</button>'
        html = html.replace(TAB_NAV_END, new_btn + TAB_NAV_END, 1)

    # 3. t7 ペインを <script> 直前に追加
    SCRIPT_TAG = '\n\n<script>'
    if SCRIPT_TAG in html:
        html = html.replace(SCRIPT_TAG,
                            f'\n<div id="t7" class="tab-pane">{tab7_html}</div>' + SCRIPT_TAG, 1)

    # 4. タブナビが2行にならないよう CSS を上書き
    css_fix = (
        '\n<style>'
        '\n.tab-nav { flex-wrap: nowrap !important; overflow-x: auto; -webkit-overflow-scrolling: touch; }'
        '\n.tab-btn { padding: 7px 13px !important; font-size: 0.8rem !important; white-space: nowrap; }'
        '\n</style>'
    )
    html = html.replace('</head>', css_fix + '\n</head>', 1)

    return html


# ════════════════════════════════════════════════════════════════════════════
# 銘柄×戦略 個別履歴確認
# ════════════════════════════════════════════════════════════════════════════

def _build_history_html(symbol: str, name: str, strategy: str, days: int, mode: str,
                        trade_log: list) -> str:
    wins      = sum(1 for t in trade_log if t.get("pnl", 0) > 0)
    losses    = len(trade_log) - wins
    total_pnl = sum(t.get("pnl", 0) for t in trade_log)
    gross_p   = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) > 0)
    gross_l   = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) <= 0)
    wr        = wins / len(trade_log) * 100 if trade_log else 0
    pf        = abs(gross_p / gross_l) if gross_l != 0 else float("inf")
    avg       = total_pnl / len(trade_log) if trade_log else 0
    pf_str    = f"{pf:.2f}" if pf != float("inf") else "∞"
    reasons: dict[str, int] = {}
    for t in trade_log:
        reasons[t.get("reason", "?")] = reasons.get(t.get("reason", "?"), 0) + 1
    reason_chips = "".join(
        f'<span class="chip">{k}: {v}件</span>'
        for k, v in sorted(reasons.items())
    )
    rows = []
    for t in sorted(trade_log, key=lambda x: str(x.get("exit_dt", "")), reverse=True):
        pnl = t.get("pnl", 0)
        rsn = t.get("reason", "?")
        cls = "win" if pnl > 0 else "loss"
        rsn_cls = {"target": "tag-target", "stop": "tag-stop",
                   "timeout": "tag-timeout"}.get(rsn, "")
        rows.append(f"""
        <tr class="{cls}">
          <td>{str(t.get("exit_dt","?"))[:10]}</td>
          <td>{str(t.get("signal_dt","?"))[:10]}</td>
          <td class="num">{t.get("entry_p",0):,.0f}</td>
          <td class="num">{t.get("exit_p",0):,.0f}</td>
          <td class="num">{t.get("qty",0):,}</td>
          <td class="num">{t.get("hold_days",0)}</td>
          <td class="num pnl">{pnl:+,.0f}円</td>
          <td><span class="tag {rsn_cls}">{rsn}</span></td>
        </tr>""")
    mode_color = "#e74c3c" if mode == "aggressive" else "#3498db"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{symbol} × {strategy} バックテスト履歴</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Helvetica Neue",Arial,sans-serif; background:#0f172a; color:#e2e8f0; padding:24px; }}
  h1 {{ font-size:1.5rem; margin-bottom:4px; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:24px; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.75rem;
            font-weight:bold; color:#fff; background:{mode_color}; margin-left:8px; vertical-align:middle; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }}
  .card {{ background:#1e293b; border-radius:10px; padding:16px 24px; min-width:140px; }}
  .card .label {{ font-size:0.75rem; color:#64748b; margin-bottom:4px; }}
  .card .value {{ font-size:1.4rem; font-weight:bold; }}
  .pos {{ color:#22c55e; }}  .neg {{ color:#ef4444; }}
  .chip {{ display:inline-block; background:#334155; border-radius:6px; padding:2px 8px;
           font-size:0.78rem; margin:2px; }}
  .reasons {{ margin-bottom:20px; color:#94a3b8; font-size:0.85rem; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.88rem; }}
  th {{ background:#1e293b; padding:8px 12px; text-align:left; color:#94a3b8; font-weight:600;
        border-bottom:1px solid #334155; position:sticky; top:0; }}
  td {{ padding:7px 12px; border-bottom:1px solid #1e293b; }}
  tr.win td {{ background:#0f2a1a; }}  tr.loss td {{ background:#2a0f0f; }}
  tr:hover td {{ filter:brightness(1.15); }}
  .num {{ text-align:right; }}  .pnl {{ font-weight:bold; }}
  tr.win .pnl {{ color:#22c55e; }}  tr.loss .pnl {{ color:#ef4444; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:4px; font-size:0.75rem; font-weight:bold; }}
  .tag-target {{ background:#14532d; color:#86efac; }}
  .tag-stop   {{ background:#450a0a; color:#fca5a5; }}
  .tag-timeout {{ background:#27272a; color:#a1a1aa; }}
  .footer {{ margin-top:20px; color:#64748b; font-size:0.82rem; text-align:right; }}
</style>
</head>
<body>
<h1>{name} ({symbol}) × {strategy} <span class="badge">{mode}</span></h1>
<div class="subtitle">直近 {days} 日間のバックテスト履歴</div>
<div class="cards">
  <div class="card"><div class="label">取引数</div><div class="value">{len(trade_log)}件</div>
    <div style="font-size:0.8rem;color:#64748b">{wins}勝 / {losses}敗</div></div>
  <div class="card"><div class="label">勝率</div>
    <div class="value {'pos' if wr >= 50 else 'neg'}">{wr:.1f}%</div></div>
  <div class="card"><div class="label">PF</div>
    <div class="value {'pos' if pf >= 1.0 else 'neg'}">{pf_str}</div></div>
  <div class="card"><div class="label">合計損益</div>
    <div class="value {'pos' if total_pnl >= 0 else 'neg'}">{total_pnl:+,.0f}円</div></div>
  <div class="card"><div class="label">平均損益</div>
    <div class="value {'pos' if avg >= 0 else 'neg'}">{avg:+,.0f}円</div></div>
</div>
<div class="reasons">決済理由: {reason_chips}</div>
<table>
  <thead><tr>
    <th>決済日</th><th>シグナル日</th><th class="num">約定値</th><th class="num">決済値</th>
    <th class="num">株数</th><th class="num">保有日</th><th class="num">損益</th><th>決済理由</th>
  </tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
<div class="footer">合計: {total_pnl:+,.0f}円 / {len(trade_log)}取引 &nbsp;|&nbsp; 生成: {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
</body></html>"""


def _run_symbol_history(symbol: str, strategy: str, days: int, mode: str,
                        open_browser: bool = True) -> None:
    from backtest_limit_entry import fetch, run_limit_backtest
    all_params = {**_CON_STOP, **_CON_BRK} if mode == "conservative" else {**_AGG_STOP, **_AGG_BRK}
    if strategy not in all_params:
        print(f"ERROR: 戦略 '{strategy}' は不明。利用可能: {', '.join(all_params)}")
        sys.exit(1)
    print(f"データ取得中: {symbol} ...")
    df = fetch(symbol, days + 60)
    if df is None or df.empty:
        print(f"ERROR: {symbol} のデータを取得できませんでした。"); sys.exit(1)
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        name = info.get("longName") or info.get("shortName") or symbol
    except Exception:
        name = symbol
    calc_fn, em, sm, tm = all_params[strategy]
    print("バックテスト実行中...")
    result    = run_limit_backtest(
        symbol=symbol, name=name, df=df, calc_fn=calc_fn,
        entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
        backtest_days=days, strategy_name=strategy, entry_type="stop",
    )
    trade_log = result.get("trade_log", [])
    if not trade_log:
        print("この期間にトレードはありませんでした。"); return
    html     = _build_history_html(symbol, name, strategy, days, mode, trade_log)
    date_str = datetime.now().strftime("%Y-%m-%d")
    sfx      = "_aggressive" if mode == "aggressive" else ""
    out      = Path(f"history_{symbol}_{strategy}{sfx}_{date_str}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML生成完了: {out.resolve()}")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())


# ════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str, default=None)
    _p.add_argument("--no-browser", action="store_true")
    _p.add_argument("--symbol",     type=str, default=None)
    _p.add_argument("--strategy",   type=str, default=None)
    _p.add_argument("--days",       type=int, default=365)
    _p.add_argument("--years",      type=int, default=5)
    _p.add_argument("--workers",    type=int, default=_DEF_WORKERS)
    _p.add_argument("--aggressive", action="store_true")
    _known, _ = _p.parse_known_args()

    # ── 銘柄×戦略 個別履歴確認モード ──────────────────────────────────────────
    if _known.symbol and _known.strategy:
        mode = "aggressive" if _known.aggressive else os.environ.get("TRADING_MODE", "conservative")
        _run_symbol_history(
            symbol=_known.symbol.upper(), strategy=_known.strategy.upper(),
            days=_known.days, mode=mode, open_browser=not _known.no_browser,
        )
        return

    # ── 4config 統合レポートモード ─────────────────────────────────────────────
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

    print(f"4設定バックテスト実行中 (workers={_known.workers})...", flush=True)
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
        cfg_section = _4cfg_section_html(r)
        tab7_html   = _tab7_trend_analysis_html(trades_by_cfg)
    else:
        cfg_section = ""
        tab7_html   = "<p style='color:#64748b;padding:20px'>データ取得失敗のためスキップ</p>"

    new_html = _inject_tabs(base_html, cfg_section, tab7_html)
    new_path = Path(f"nikkei_analysis_4config_{date_str}.html")
    new_path.write_text(new_html, encoding="utf-8")
    print(f"\n4config レポート生成完了: {new_path.resolve()}")

    if not _known.no_browser:
        webbrowser.open(new_path.resolve().as_uri())


if __name__ == "__main__":
    main()
