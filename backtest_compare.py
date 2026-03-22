"""
アルゴリズム比較バックテスト

7種類のエントリー/エグジット戦略を同一条件で検証し、
最も利益が出る手法を選定する。

共通ルール:
  - シグナルは前日終値で判定 → 翌日始値で執行（既存と同一）
  - ストップロス: ATR × 1.5
  - ポジションサイジング: リスク3%
  - 検証銘柄: WATCH_LIST（10銘柄）
  - 期間: 過去5年

使い方:
  python backtest_compare.py
"""

import base64
import io
import logging
import os
import subprocess
import sys
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import yfinance as yf

# ── 共通パラメータ ────────────────────────────────────────────
WATCH_LIST = [
    ("8604.T", "野村HD"),
    ("2802.T", "味の素"),
    ("2914.T", "JT"),
    ("7203.T", "トヨタ自動車"),
    ("8001.T", "伊藤忠商事"),
    ("8306.T", "三菱UFJ"),
    ("6752.T", "パナソニックHD"),
    ("8002.T", "丸紅"),
    ("9020.T", "JR東日本"),
    ("6902.T", "デンソー"),
]

ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PER_TRADE = 0.03
INITIAL_CASH   = 500_000
MAX_COST_RATIO = 0.10
MAX_QTY        = 3000


# ════════════════════════════════════════════════════════════════
# データ取得・共通インジケーター
# ════════════════════════════════════════════════════════════════

def fetch_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].dropna()


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """全アルゴリズムが使うインジケーターをまとめて計算"""
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # ── EMA 各種 ────────────────────────────────────────────
    for span in (5, 10, 20, 25, 50, 200):
        df[f"ema{span}"] = c.ewm(span=span, adjust=False).mean()

    # EMA cross signals
    df["ema5_cross_up25"]  = (df["ema5"]  > df["ema25"]) & (df["ema5"].shift(1)  <= df["ema25"].shift(1))
    df["ema5_cross_dn25"]  = (df["ema5"]  < df["ema25"]) & (df["ema5"].shift(1)  >= df["ema25"].shift(1))
    df["ema5_cross_up20"]  = (df["ema5"]  > df["ema20"]) & (df["ema5"].shift(1)  <= df["ema20"].shift(1))
    df["ema5_cross_dn20"]  = (df["ema5"]  < df["ema20"]) & (df["ema5"].shift(1)  >= df["ema20"].shift(1))

    # ── RSI(14) ─────────────────────────────────────────────
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    # ── ボリンジャーバンド(20,2) ────────────────────────────
    sma20      = c.rolling(20).mean()
    std20      = c.rolling(20).std(ddof=0)
    df["bb_upper"] = sma20 + 2.0 * std20
    df["bb_lower"] = sma20 - 2.0 * std20
    df["bb_mid"]   = sma20
    df["bb_band"]  = 2.0 * std20

    # ── MACD(12,26,9) ───────────────────────────────────────
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]
    df["macd_cross_up"] = (df["macd_hist"] > 0) & (df["macd_hist"].shift(1) <= 0)
    df["macd_cross_dn"] = (df["macd_hist"] < 0) & (df["macd_hist"].shift(1) >= 0)

    # ── Williams %R(14) ──────────────────────────────────────
    hh14  = h.rolling(14).max()
    ll14  = l.rolling(14).min()
    df["willr"] = (hh14 - c) / (hh14 - ll14).replace(0, np.nan) * -100

    # ── Stochastic(14, 3) ────────────────────────────────────
    df["stoch_k_raw"] = (c - ll14) / (hh14 - ll14).replace(0, np.nan) * 100
    df["stoch_k"]     = df["stoch_k_raw"].rolling(3).mean()
    df["stoch_d"]     = df["stoch_k"].rolling(3).mean()
    df["stoch_k_cross_up"] = (df["stoch_k"] > df["stoch_d"]) & (df["stoch_k"].shift(1) <= df["stoch_d"].shift(1))
    df["stoch_k_cross_dn"] = (df["stoch_k"] < df["stoch_d"]) & (df["stoch_k"].shift(1) >= df["stoch_d"].shift(1))

    # ── ATR(14) ─────────────────────────────────────────────
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ════════════════════════════════════════════════════════════════
# アルゴリズム定義
# 各関数は df に entry_sig / exit_sig カラムを追加して返す
# ════════════════════════════════════════════════════════════════

def algo_A0_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """
    A0: ベースライン（現行戦略）
    Entry: EMA50 上昇トレンド + (RSI<55 OR EMA5クロスアップ OR BB下限付近)
    Exit : RSI>60 OR BB上限到達 OR EMA5<EMA20
    """
    trend_up = df["close"] > df["ema50"]
    df["entry_sig"] = trend_up & (
        (df["rsi"] < 55) |
        df["ema5_cross_up20"] |
        (df["close"] <= df["bb_lower"] + df["bb_band"])
    )
    df["exit_sig"] = (
        (df["rsi"]   > 60) |
        (df["close"] >= df["bb_upper"]) |
        df["ema5_cross_dn20"]
    )
    return df


def algo_A1_rsi_mr(df: pd.DataFrame) -> pd.DataFrame:
    """
    A1: RSI 逆張り
    Entry: EMA200 上昇トレンド AND RSI < 30（強い売られすぎ）
    Exit : RSI > 65
    """
    df["entry_sig"] = (df["close"] > df["ema200"]) & (df["rsi"] < 30)
    df["exit_sig"]  = df["rsi"] > 65
    return df


def algo_A2_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    A2: MACD クロス
    Entry: MACD ヒストグラムがマイナスからプラスへ転換（ゼロクロス）
           かつ EMA50 上昇トレンド
    Exit : MACD ヒストグラムがプラスからマイナスへ転換
    """
    df["entry_sig"] = df["macd_cross_up"] & (df["close"] > df["ema50"])
    df["exit_sig"]  = df["macd_cross_dn"]
    return df


def algo_A3_bb_mr(df: pd.DataFrame) -> pd.DataFrame:
    """
    A3: ボリンジャーバンド 逆張り（平均回帰）
    Entry: 終値が BB 下限を下回る（−2σ タッチ）
    Exit : 終値が BB 中心線（SMA20）を超える
    """
    df["entry_sig"] = df["close"] < df["bb_lower"]
    df["exit_sig"]  = df["close"] > df["bb_mid"]
    return df


def algo_A4_ema_cross(df: pd.DataFrame) -> pd.DataFrame:
    """
    A4: EMA ゴールデン/デッドクロス
    Entry: EMA5 が EMA25 を上抜け（ゴールデンクロス）
    Exit : EMA5 が EMA25 を下抜け（デッドクロス）
    """
    df["entry_sig"] = df["ema5_cross_up25"]
    df["exit_sig"]  = df["ema5_cross_dn25"]
    return df


def algo_A5_willr(df: pd.DataFrame) -> pd.DataFrame:
    """
    A5: Williams %R 逆張り
    Entry: Williams %R < -80（売られすぎ）AND EMA50 上昇トレンド
    Exit : Williams %R > -25（買われすぎ）
    """
    df["entry_sig"] = (df["willr"] < -80) & (df["close"] > df["ema50"])
    df["exit_sig"]  = df["willr"] > -25
    return df


def algo_A6_stoch(df: pd.DataFrame) -> pd.DataFrame:
    """
    A6: ストキャスティクス クロス
    Entry: %K が %D を 20 以下から上抜け
    Exit : %K が %D を 70 以上で下抜け OR %K が %D を上から下抜け
    """
    df["entry_sig"] = df["stoch_k_cross_up"] & (df["stoch_k"].shift(1) < 30)
    df["exit_sig"]  = df["stoch_k_cross_dn"] & (df["stoch_k"].shift(1) > 60)
    return df


def algo_A7_triple(df: pd.DataFrame) -> pd.DataFrame:
    """
    A7: トリプルフィルター複合戦略
    Entry: RSI<40 AND 終値<BB下限 AND EMA50 上昇トレンド（3条件重複）
    Exit : RSI>62 OR 終値>BB上限
    厳しい条件で高精度を狙う
    """
    df["entry_sig"] = (
        (df["rsi"] < 40) &
        (df["close"] < df["bb_lower"]) &
        (df["close"] > df["ema50"])
    )
    df["exit_sig"] = (df["rsi"] > 62) | (df["close"] >= df["bb_upper"])
    return df


ALGORITHMS = [
    ("A0: ベースライン",           algo_A0_baseline),
    ("A1: RSI逆張り",              algo_A1_rsi_mr),
    ("A2: MACDクロス",             algo_A2_macd),
    ("A3: BB逆張り（平均回帰）",    algo_A3_bb_mr),
    ("A4: EMAゴールデンクロス",     algo_A4_ema_cross),
    ("A5: Williams%%R逆張り",       algo_A5_willr),
    ("A6: ストキャスティクス",      algo_A6_stoch),
    ("A7: トリプルフィルター",      algo_A7_triple),
]


# ════════════════════════════════════════════════════════════════
# バックテストエンジン（既存と同一ロジック）
# ════════════════════════════════════════════════════════════════

def calc_qty(cash: float, atr: float, close: float) -> int:
    stop_dist = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    qty_by_risk = int(cash * RISK_PER_TRADE / stop_dist)
    qty_by_cost = int(cash * MAX_COST_RATIO / close) if close > 0 else qty_by_risk
    qty = min(qty_by_risk, qty_by_cost, MAX_QTY)
    return max(qty, 1)


def backtest_symbol(df: pd.DataFrame, symbol: str, name: str) -> dict:
    cash = INITIAL_CASH
    in_pos = False
    entry_price = stop_price = 0.0
    qty = 0
    entry_dt = None
    trades = []

    for i in range(1, len(df)):
        today = df.iloc[i]
        prev  = df.iloc[i - 1]

        if in_pos:
            exit_price  = None
            exit_reason = None
            if today["low"] <= stop_price:
                exit_price  = min(today["open"], stop_price)
                exit_reason = "stop"
            elif prev["exit_sig"]:
                exit_price  = today["open"]
                exit_reason = "signal"

            if exit_price is not None:
                pnl   = (exit_price - entry_price) * qty
                cash += exit_price * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     df.index[i],
                    "symbol":      symbol,
                    "name":        name,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl,
                    "reason":      exit_reason,
                })
                in_pos = False

        if not in_pos and prev["entry_sig"]:
            q    = calc_qty(cash, prev["atr"], prev["close"])
            cost = today["open"] * q
            if q > 0 and cost <= cash:
                entry_price = today["open"]
                stop_price  = entry_price - prev["atr"] * ATR_STOP_MULT
                qty         = q
                cash       -= cost
                entry_dt    = df.index[i]
                in_pos      = True

    if in_pos:
        exit_price = df.iloc[-1]["close"]
        pnl        = (exit_price - entry_price) * qty
        cash      += exit_price * qty
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     df.index[-1],
            "symbol":      symbol,
            "name":        name,
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "qty":         qty,
            "pnl":         pnl,
            "reason":      "force_close",
        })

    total_pnl = cash - INITIAL_CASH
    n_trades  = len(trades)
    wins      = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses    = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf        = (sum(wins) / abs(sum(losses))
                 if losses and sum(losses) != 0 else float("inf"))
    return {
        "symbol":        symbol,
        "name":          name,
        "trades":        n_trades,
        "win_rate":      len(wins) / n_trades * 100 if n_trades else 0.0,
        "total_pnl":     total_pnl,
        "return_pct":    total_pnl / INITIAL_CASH * 100,
        "profit_factor": pf,
        "trades_detail": trades,
    }


def portfolio_metrics(all_trades: list, n_symbols: int) -> dict:
    if not all_trades:
        empty = {k: 0.0 for k in ["n_trades","win_rate","profit_factor",
                                   "max_drawdown","avg_hold_days","avg_pnl",
                                   "best_trade","worst_trade","total_pnl",
                                   "total_ret_pct"]}
        empty["cum_pnl_series"] = pd.DataFrame(columns=["exit_dt","cum"])
        return empty
    td          = pd.DataFrame(all_trades).sort_values("exit_dt").reset_index(drop=True)
    td["cum"]   = td["pnl"].cumsum()
    init_total  = INITIAL_CASH * n_symbols
    equity      = init_total + td["cum"]
    peak        = equity.cummax()
    dd          = (peak - equity) / peak * 100
    wins        = td[td["pnl"] > 0]
    losses      = td[td["pnl"] <= 0]
    pf          = (wins["pnl"].sum() / abs(losses["pnl"].sum())
                   if len(losses) > 0 and losses["pnl"].sum() != 0 else float("inf"))
    td["hold"]  = (td["exit_dt"] - td["entry_dt"]).dt.days
    total_pnl   = td["pnl"].sum()
    total_ret   = total_pnl / init_total
    return {
        "n_trades":      len(td),
        "win_rate":      len(wins) / len(td) * 100,
        "profit_factor": pf,
        "max_drawdown":  dd.max(),
        "avg_hold_days": td["hold"].mean(),
        "avg_pnl":       td["pnl"].mean(),
        "best_trade":    td["pnl"].max(),
        "worst_trade":   td["pnl"].min(),
        "total_pnl":     total_pnl,
        "total_ret_pct": total_ret * 100,
        "cum_pnl_series": td[["exit_dt","cum"]].copy(),
    }


# ════════════════════════════════════════════════════════════════
# HTML レポート生成
# ════════════════════════════════════════════════════════════════

def fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def export_compare_html(algo_results: list, start: str, end: str,
                        path: str = "backtest_compare.html") -> str:
    """
    algo_results: list of (algo_name, metrics_dict)
    """
    # ソート（総損益 降順）
    sorted_res = sorted(algo_results, key=lambda x: x[1]["total_pnl"], reverse=True)
    winner     = sorted_res[0]

    names  = [r[0] for r in sorted_res]
    pnls   = [r[1]["total_pnl"] for r in sorted_res]
    wrs    = [r[1]["win_rate"]   for r in sorted_res]
    dds    = [r[1]["max_drawdown"] for r in sorted_res]
    pfs    = [r[1]["profit_factor"] for r in sorted_res]

    plt.rcParams["font.family"] = ["IPAexGothic", "Noto Sans CJK JP",
                                   "Hiragino Sans", "MS Gothic", "sans-serif"]

    # ── グラフ① 総損益比較 ──────────────────────────────────
    fig1, ax1 = plt.subplots(figsize=(12, 5), facecolor="#f8f9fa")
    ax1.set_facecolor("#ffffff")
    bar_colors = ["#276221" if v >= 0 else "#9c0006" for v in pnls]
    bar_colors[0] = "#ffd700"   # winner を金色に
    bars = ax1.bar(names, pnls, color=bar_colors, width=0.6, edgecolor="white")
    for b, v in zip(bars, pnls):
        ax1.text(b.get_x() + b.get_width()/2,
                 v + max(abs(x) for x in pnls) * 0.015 * (1 if v >= 0 else -1),
                 f"{v:+,.0f}", ha="center",
                 va="bottom" if v >= 0 else "top", fontsize=8)
    ax1.axhline(0, color="#888", linewidth=0.8)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x,_: f"{x:+,.0f}"))
    ax1.set_title("アルゴリズム別 総損益比較 (円)  ★=優勝", fontsize=13, pad=10)
    ax1.grid(axis="y", linestyle=":", alpha=0.5)
    ax1.spines[["top","right"]].set_visible(False)
    plt.xticks(rotation=20, ha="right")
    img_pnl = fig_to_b64(fig1)

    # ── グラフ② 累積損益推移（全アルゴ重ね） ────────────────
    fig2, ax2 = plt.subplots(figsize=(12, 5), facecolor="#f8f9fa")
    ax2.set_facecolor("#ffffff")
    cmap   = plt.cm.tab10
    for idx, (aname, m) in enumerate(algo_results):
        ser = m.get("cum_pnl_series")
        if ser is None or len(ser) == 0:
            continue
        lw   = 2.5 if aname == winner[0] else 1.2
        ls   = "-"  if aname == winner[0] else "--"
        col  = cmap(idx % 10)
        ax2.plot(ser["exit_dt"], ser["cum"], linewidth=lw, linestyle=ls,
                 color=col, label=aname, alpha=0.85)
    ax2.axhline(0, color="#888", linewidth=0.8, linestyle=":")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x,_: f"{x:+,.0f}"))
    ax2.set_title("累積損益推移 比較（実線=最優秀）", fontsize=13, pad=10)
    ax2.legend(fontsize=7, ncol=2, loc="upper left")
    ax2.grid(axis="y", linestyle=":", alpha=0.4)
    ax2.spines[["top","right"]].set_visible(False)
    img_cum = fig_to_b64(fig2)

    # ── グラフ③ 勝率 & 最大DD ────────────────────────────────
    fig3, (ax3a, ax3b) = plt.subplots(1, 2, figsize=(12, 4), facecolor="#f8f9fa")
    for ax in (ax3a, ax3b):
        ax.set_facecolor("#ffffff")
        ax.spines[["top","right"]].set_visible(False)

    ax3a.bar(names, wrs, color="#2e75b6", width=0.6, edgecolor="white")
    ax3a.axhline(50, color="#e06c00", linewidth=1, linestyle="--")
    ax3a.set_ylim(0, 100)
    ax3a.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x,_: f"{x:.0f}%"))
    ax3a.set_title("勝率", fontsize=11, pad=8)
    ax3a.grid(axis="y", linestyle=":", alpha=0.5)
    plt.sca(ax3a); plt.xticks(rotation=20, ha="right")

    ax3b.bar(names, dds, color="#c00000", width=0.6, edgecolor="white")
    ax3b.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x,_: f"{x:.1f}%"))
    ax3b.set_title("最大ドローダウン", fontsize=11, pad=8)
    ax3b.grid(axis="y", linestyle=":", alpha=0.5)
    plt.sca(ax3b); plt.xticks(rotation=20, ha="right")

    fig3.tight_layout(pad=2)
    img_wr_dd = fig_to_b64(fig3)

    # ── テーブル HTML ────────────────────────────────────────
    def pf_str(v):
        return "∞" if v == float("inf") else f"{v:.2f}"

    rows_html = ""
    for rank, (aname, m) in enumerate(sorted_res, start=1):
        is_winner = rank == 1
        bg  = ' style="background:#fffde7;font-weight:700"' if is_winner else ""
        sgn = "+" if m["total_pnl"] >= 0 else ""
        pc  = "profit" if m["total_pnl"] >= 0 else "loss"
        rows_html += f"""<tr{bg}>
  <td class="num">{'🥇 ' if is_winner else ''}{rank}</td>
  <td>{aname}</td>
  <td class="{pc}">{sgn}{m['total_pnl']:,.0f}</td>
  <td class="num">{sgn}{m['total_ret_pct']:.1f}%</td>
  <td class="num">{m['win_rate']:.1f}%</td>
  <td class="num">{pf_str(m['profit_factor'])}</td>
  <td class="num">{m['max_drawdown']:.1f}%</td>
  <td class="num">{m['n_trades']}</td>
  <td class="num">{m['avg_hold_days']:.1f}</td>
  <td class="num">{m['avg_pnl']:+,.0f}</td>
</tr>"""

    winner_name = winner[0]
    winner_pnl  = winner[1]["total_pnl"]
    winner_wr   = winner[1]["win_rate"]
    winner_dd   = winner[1]["max_drawdown"]

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>アルゴリズム比較バックテスト</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:"Yu Gothic UI","Hiragino Sans","Noto Sans JP",sans-serif;
        background:#f0f2f5;color:#222;font-size:14px}}
  .header{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);
           color:#fff;padding:24px 32px}}
  .header h1{{font-size:1.5em;margin-bottom:6px}}
  .header p{{opacity:.85;font-size:.88em}}
  .winner-box{{background:linear-gradient(135deg,#ffd700,#ffa500);
               border-radius:12px;padding:20px 28px;margin:24px 32px;
               box-shadow:0 4px 12px rgba(255,165,0,.4)}}
  .winner-box h2{{font-size:1.3em;color:#1a1a2e;margin-bottom:8px}}
  .winner-box p{{color:#333;font-size:.95em;line-height:1.6}}
  .section{{margin:24px 32px}}
  h2.sec{{font-size:1.05em;color:#1f3864;border-left:4px solid #2e75b6;
           padding-left:10px;margin-bottom:14px}}
  table{{width:100%;border-collapse:collapse;background:#fff;
         border-radius:8px;overflow:hidden;
         box-shadow:0 1px 4px rgba(0,0,0,.1)}}
  thead th{{background:#1f3864;color:#fff;padding:10px 14px;
            text-align:center;font-size:.86em;white-space:nowrap}}
  tbody tr:hover{{background:#eaf1fb!important}}
  td{{padding:9px 14px;border-bottom:1px solid #e8edf3;white-space:nowrap}}
  .num{{text-align:right}}
  .profit{{text-align:right;color:#276221;font-weight:600}}
  .loss{{text-align:right;color:#9c0006;font-weight:600}}
  .chart-card{{background:#fff;border-radius:8px;padding:16px;
               box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:20px}}
  .chart-card img{{width:100%;height:auto}}
  .chart-grid{{display:grid;grid-template-columns:1fr;gap:20px}}
</style>
</head>
<body>
<div class="header">
  <h1>📊 アルゴリズム比較バックテスト — {len(ALGORITHMS)}戦略</h1>
  <p>期間: {start} ～ {end}（約5年）　銘柄数: {len(WATCH_LIST)}
     初期資金: {INITIAL_CASH:,}円/銘柄　ストップATR×{ATR_STOP_MULT}
     生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>

<div class="winner-box">
  <h2>🏆 最優秀アルゴリズム: {winner_name}</h2>
  <p>総損益: <strong>{'+' if winner_pnl>=0 else ''}{winner_pnl:,.0f} 円</strong>
     ／ 勝率: <strong>{winner_wr:.1f}%</strong>
     ／ 最大DD: <strong>{winner_dd:.1f}%</strong></p>
</div>

<div class="section">
  <h2 class="sec">順位表（総損益 降順）</h2>
  <table>
    <thead><tr>
      <th>順位</th><th>アルゴリズム</th><th>総損益(円)</th><th>収益率</th>
      <th>勝率</th><th>PF</th><th>最大DD</th>
      <th>取引数</th><th>平均保有日</th><th>平均損益(円)</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<div class="section chart-grid">
  <div class="chart-card">
    <img src="data:image/png;base64,{img_pnl}" alt="総損益比較">
  </div>
  <div class="chart-card">
    <img src="data:image/png;base64,{img_cum}" alt="累積損益推移">
  </div>
  <div class="chart-card">
    <img src="data:image/png;base64,{img_wr_dd}" alt="勝率・最大DD">
  </div>
</div>

</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ════════════════════════════════════════════════════════════════
# メイン
# ════════════════════════════════════════════════════════════════

def main():
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * 5 + 60)).strftime("%Y-%m-%d")

    W = 70
    print(f"\n{'='*W}")
    print(f"  アルゴリズム比較バックテスト  {len(ALGORITHMS)} 戦略")
    print(f"  期間: {start} ～ {end}   銘柄数: {len(WATCH_LIST)}")
    print(f"{'='*W}")

    # ── データ取得（各銘柄1回だけ）───────────────────────────
    print("\n[1/3] データ取得中 ...")
    raw_data = {}
    for symbol, name in WATCH_LIST:
        print(f"  {name}({symbol}) ...", end="\r", flush=True)
        try:
            df = fetch_data(symbol, start, end)
            df = add_all_indicators(df)
            raw_data[(symbol, name)] = df
        except Exception as e:
            print(f"\n  ⚠ {name}({symbol}) スキップ: {e}")
    print(f"  取得完了: {len(raw_data)} / {len(WATCH_LIST)} 銘柄{'　'*20}")

    # ── 全アルゴ × 全銘柄 バックテスト ──────────────────────
    print("\n[2/3] バックテスト実行中 ...")
    algo_results = []   # [(algo_name, metrics_dict), ...]

    for algo_name, algo_fn in ALGORITHMS:
        all_trades = []
        for (symbol, name), df_base in raw_data.items():
            df = df_base.copy()
            df = algo_fn(df)
            res = backtest_symbol(df, symbol, name)
            all_trades.extend(res["trades_detail"])

        m = portfolio_metrics(all_trades, len(raw_data))
        algo_results.append((algo_name, m))
        sgn = "+" if m["total_pnl"] >= 0 else ""
        pf_s = "∞" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        print(f"  {algo_name:<24}  損益: {sgn}{m['total_pnl']:>10,.0f}円  "
              f"勝率:{m['win_rate']:>5.1f}%  PF:{pf_s:>5}  DD:{m['max_drawdown']:>5.1f}%")

    # ── 結果表示 ─────────────────────────────────────────────
    print(f"\n{'='*W}")
    print("  順位表（総損益 降順）")
    print(f"  {'順位':<3} {'アルゴリズム':<26} {'総損益':>12} {'勝率':>7} {'PF':>6} {'最大DD':>7}")
    print(f"  {'-'*68}")

    sorted_res = sorted(algo_results, key=lambda x: x[1]["total_pnl"], reverse=True)
    for rank, (aname, m) in enumerate(sorted_res, start=1):
        sgn  = "+" if m["total_pnl"] >= 0 else ""
        pf_s = "∞" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        mark = " ★" if rank == 1 else ""
        print(f"  {rank:<3} {aname:<26} {sgn}{m['total_pnl']:>11,.0f}円  "
              f"{m['win_rate']:>5.1f}%  {pf_s:>5}  {m['max_drawdown']:>5.1f}%{mark}")

    winner = sorted_res[0]
    print(f"\n{'='*W}")
    print(f"  🏆 最優秀: {winner[0]}")
    print(f"     総損益 {'+' if winner[1]['total_pnl']>=0 else ''}{winner[1]['total_pnl']:,.0f}円"
          f"  勝率 {winner[1]['win_rate']:.1f}%"
          f"  最大DD {winner[1]['max_drawdown']:.1f}%")
    print(f"{'='*W}")

    # ── HTML 出力 ─────────────────────────────────────────────
    print("\n[3/3] HTML レポート生成中 ...")
    html_path = "backtest_compare.html"
    export_compare_html(algo_results, start, end, html_path)
    abs_path  = os.path.abspath(html_path)
    print(f"  保存完了: {abs_path}")

    try:
        if sys.platform == "win32":
            os.startfile(abs_path)
        elif sys.platform == "darwin":
            subprocess.run(["open", abs_path], check=False)
        else:
            subprocess.run(["xdg-open", abs_path], check=False)
    except Exception:
        pass
    print()

    return winner[0]   # 最優秀アルゴ名を返す


if __name__ == "__main__":
    main()
