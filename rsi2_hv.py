"""
RSI(2) 平均回帰バックテスト 拡張版 — 高ボラ対応・自動レジーム切替
────────────────────────────────────────────────────────────────────
改善点（海外論文・過去事例に基づく）:
  1. 自動レジーム検知: 日経MA200 で「通常 / 高ボラ」を判定しパラメータ切替
     (Connors & Alvarez 2008; Sumitomo Mitsui DS AM 2025)
  2. IBSフィルター: Internal Bar Strength < 0.35 のみエントリー
     → 終値が日中安値圏にある銘柄のみ → 精度向上 (Pagonidis, NAAIM 2014)
  3. 連続RSI確認: 前日・前々日 両方とも RSI(2)≤閾値 の場合のみエントリー
     → +0.77% → +1.23%/トレード (Connors 2008)
  4. VIXフィルター（任意）: ^VIX の RSI(7)>50 の時のみエントリー
     → トレード数半減・平均利益2倍・DD大幅減 (Connors VIX Reversal II)

使い方:
  python rsi2_hv.py                    # 全銘柄スキャン（1年）
  python rsi2_hv.py --years 2          # 2年
  python rsi2_hv.py 7011.T             # 1銘柄詳細
  python rsi2_hv.py --mode normal      # 強制的に通常モード
  python rsi2_hv.py --mode hv          # 強制的に高ボラモード
  python rsi2_hv.py --no-ibs           # IBSフィルターなし
  python rsi2_hv.py --no-consec        # 連続RSI確認なし（1日でOK）
  python rsi2_hv.py --vix              # VIXフィルター有効化
"""

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# rsi2.py のデータ取得・指標計算を流用
from rsi2 import (
    SYMBOLS, _TODAY, WORKERS,
    calc,
    _market_info, _mkt_banner_html, _trade_table,
)

import yfinance as yf


def _period_str(backtest_days: int) -> str:
    """backtest_macd_scan.py と同じ period 選択ロジック。"""
    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)
    if   total_cal <= 180:  return "6mo"
    elif total_cal <= 365:  return "1y"
    elif total_cal <= 730:  return "2y"
    elif total_cal <= 1095: return "3y"
    elif total_cal <= 1825: return "5y"
    else:                   return "max"


def fetch(symbol: str, backtest_days: int) -> pd.DataFrame | None:
    """backtest_macd_scan.py と同じ方式でダウンロード。
    キャッシュなし・auto_adjust=True・period= 指定。"""
    period = _period_str(backtest_days)
    try:
        raw = yf.download(symbol, period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        if len(raw) < 210:
            return None
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


def fetch_nikkei(backtest_days: int) -> pd.DataFrame | None:
    """backtest_macd_scan.py と同じ方式で日経平均を取得。"""
    period = _period_str(backtest_days)
    try:
        raw = yf.download("^N225", period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["close"]].dropna().copy()
        raw["ma25"]  = raw["close"].rolling(25).mean()
        raw["ma200"] = raw["close"].rolling(200).mean()
        return raw
    except Exception:
        return None

# ── パラメーター: 通常モード ─────────────────────────────────
NORMAL = dict(
    RSI2_ENTRY      = 10.0,   # RSI(2) ≤ 閾値 → 翌日買い
    RSI2_EXIT       = 65.0,   # RSI(2) ≥ 閾値 → 翌日売り
    HARD_STOP_PCT   =  3.0,   # 即損切り %
    HALF_PROFIT_PCT =  5.0,   # 半分利確 %
    ATR_TRAIL_MULT  =  2.0,   # ATR トレイリング係数
)

# ── パラメーター: 高ボラモード（日経MA200割れ）────────────────
# 根拠:
#   RSI2_ENTRY 5  : Connors — RSI≤5 のシグナルが≤10より高精度
#   RSI2_EXIT  75 : VIX高騰時は反発幅が大きい (Leung & Li 2016)
#   HARD_STOP   5 : ATR=5%/日の相場で3%ストップはノイズ発動
#   HALF_PROFIT 3 : 急落初動20日はリバウンドを早取り (ScienceDirect 2023)
#   ATR_TRAIL 1.5 : ATRが大きいので2倍は遠すぎる
HV = dict(
    RSI2_ENTRY      =  5.0,
    RSI2_EXIT       = 75.0,
    HARD_STOP_PCT   =  5.0,
    HALF_PROFIT_PCT =  3.0,
    ATR_TRAIL_MULT  =  1.5,
)

POSITION_SIZE = 100_000  # 1回あたりの投資金額（円）
BACKTEST_DAYS = 365


# ── VIX データ取得 ───────────────────────────────────────────
def fetch_vix(backtest_days: int) -> pd.Series | None:
    """^VIX を取得して RSI(7) を返す（失敗時は None）。"""
    period = _period_str(backtest_days)
    try:
        raw = yf.download("^VIX", period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        c = raw["close"].dropna().astype(float)
        d    = c.diff()
        gain = d.clip(lower=0).ewm(com=6, adjust=False).mean()   # Wilder RSI(7)
        loss = (-d).clip(lower=0).ewm(com=6, adjust=False).mean()
        rsi7 = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        return rsi7
    except Exception:
        return None


# ── 改良バックテスト ─────────────────────────────────────────
def backtest_hv(
    df: pd.DataFrame,
    backtest_days: int,
    params: dict,
    use_ibs: bool = True,
    use_consec: bool = True,
    vix_rsi: pd.Series | None = None,
) -> list[dict]:
    """
    改良 RSI(2) バックテスト。

    追加フィルター:
      use_ibs    : IBS = (close-low)/(high-low) < 0.35 の時のみエントリー
      use_consec : 前日・前々日 両方 RSI(2)≤閾値 を要求（連続確認）
      vix_rsi    : ^VIX の RSI(7)。Seriesを渡すと >50 の日のみエントリー
    """
    p = params
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()

    trades    = []
    in_pos    = False
    entry_p   = trail = 0.0
    entry_dt  = None
    half_done = False
    qty       = 0

    for i in range(2, len(df)):   # i=2 から (前々日 i-2 が必要)
        row   = df.iloc[i]
        prev  = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        dt    = df.index[i]

        if pd.isna(prev["rsi2"]) or pd.isna(prev["ma200"]):
            continue

        op = float(row["open"])
        lo = float(row["low"])

        # ── ポジションあり: 決済判定 ──────────────────────────
        if in_pos:
            exit_p = reason = None

            if lo <= entry_p * (1 - p["HARD_STOP_PCT"] / 100):
                exit_p = min(op, entry_p * (1 - p["HARD_STOP_PCT"] / 100))
                reason = f"損切り(-{p['HARD_STOP_PCT']:.0f}%)"
            elif lo <= trail:
                exit_p = min(op, trail)
                reason = "トレイリング"
            elif float(prev["rsi2"]) >= p["RSI2_EXIT"]:
                exit_p = op
                reason = "RSI2回復"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p, qty=qty,
                    pnl=pnl, pct=pct, hold=(dt - entry_dt).days, reason=reason,
                ))
                in_pos = half_done = False
                continue

            # 半分利確
            if not half_done:
                cl = float(row["close"])
                if (cl - entry_p) / entry_p * 100 >= p["HALF_PROFIT_PCT"]:
                    hq = qty // 2
                    if hq > 0:
                        pct_h = (cl - entry_p) / entry_p * 100
                        trades.append(dict(
                            entry_dt=entry_dt, exit_dt=dt,
                            entry_p=entry_p, exit_p=cl, qty=hq,
                            pnl=(cl - entry_p) * hq, pct=pct_h,
                            hold=(dt - entry_dt).days, reason="半分利確",
                        ))
                        qty -= hq
                        half_done = True

            cand = float(row["close"]) - float(row["atr"]) * p["ATR_TRAIL_MULT"]
            if cand > trail:
                trail = cand

        # ── ポジションなし: エントリー判定 ───────────────────
        if not in_pos:
            rsi_prev  = float(prev["rsi2"])
            rsi_prev2 = float(prev2["rsi2"])
            close_p   = float(prev["close"])
            ma200_p   = float(prev["ma200"])

            # 基本条件: RSI(2)≤閾値 + MA200上
            if not (rsi_prev <= p["RSI2_ENTRY"] and close_p > ma200_p and op > 0):
                continue

            # 追加フィルター 1: 連続RSI確認（前々日も閾値以下）
            if use_consec and not (pd.notna(rsi_prev2) and rsi_prev2 <= p["RSI2_ENTRY"]):
                continue

            # 追加フィルター 2: IBS < 0.35（終値が日中安値圏）
            if use_ibs:
                h_p = float(prev["high"])
                l_p = float(prev["low"])
                bar_range = h_p - l_p
                if bar_range > 0:
                    ibs = (close_p - l_p) / bar_range
                    if ibs >= 0.35:
                        continue

            # 追加フィルター 3: VIX RSI > 50（恐怖が高まっている局面）
            if vix_rsi is not None:
                try:
                    vr = vix_rsi.asof(prev.name)
                    if pd.isna(vr) or float(vr) <= 50:
                        continue
                except Exception:
                    pass  # VIXデータなければスキップしない

            qty      = max(int(POSITION_SIZE / op), 1)
            entry_p  = op
            trail    = op - float(row["atr"]) * p["ATR_TRAIL_MULT"]
            entry_dt = dt
            half_done = False
            in_pos   = True

    # 未決済ポジション
    if in_pos:
        lp = float(df.iloc[-1]["close"])
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=lp, qty=qty,
            pnl=(lp - entry_p) * qty,
            pct=(lp - entry_p) / entry_p * 100,
            hold=(df.index[-1] - entry_dt).days, reason="保有中★",
        ))

    return trades


# ── ターミナル表示（1銘柄詳細）───────────────────────────────
def print_result(symbol: str, trades: list[dict],
                 backtest_days: int, label: str,
                 params: dict, mode_label: str,
                 flags: str) -> None:
    since = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = _TODAY.strftime("%Y-%m-%d")
    p     = params

    if not trades:
        print(f"\n  [{symbol}]  シグナルなし（{since} ～ {today}）\n")
        return

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / len(trades) * 100
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0
    pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"

    print()
    print("═" * 70)
    print(f"  RSI(2)拡張版  [{symbol}]  直近{label}  【{mode_label}】")
    print(f"  期間: {since} ～ {today}  フィルター: {flags}")
    print(f"  【条件】RSI(2)≤{p['RSI2_ENTRY']:.0f} + MA200上 → 翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{p['RSI2_EXIT']:.0f} / ATR×{p['ATR_TRAIL_MULT']}トレイル / "
          f"-{p['HARD_STOP_PCT']:.0f}%損切り / +{p['HALF_PROFIT_PCT']:.0f}%半分利確")
    print("═" * 70)
    print(f"  トレード: {len(trades)}回  勝: {len(wins)}  負: {len(loss)}")
    print(f"  勝率: {wr:.1f}%   PF: {pf_s}   損益: {total:+,.0f}円")
    print(f"  平均保有: 勝ち {wh:.1f}日 / 負け {lh:.1f}日")
    print()
    print(f"  {'#':<3} {'エントリー':>10} {'エグジット':>10} "
          f"{'買値':>8} {'売値':>8} {'株数':>4} {'損益':>10} 保有  決済理由")
    print("  " + "─" * 66)
    for i, t in enumerate(trades, 1):
        pct  = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        mark = "★" if "保有中" in t["reason"] else " "
        print(f" {mark}{i:<3} "
              f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} "
              f"{t['qty']:>4} {t['pnl']:>+10,.0f} "
              f"{t['hold']:>3}日  {t['reason']}({pct:+.1f}%)")
    print("  " + "─" * 66)
    print(f"  合計: {total:+,.0f}円")
    print()


# ── ランキング表示 ───────────────────────────────────────────
def print_ranking(results: list[dict], days: int, label: str,
                  top: int | None, params: dict,
                  mode_label: str, flags: str,
                  mkt: dict | None) -> None:
    since   = (_TODAY - timedelta(days=days)).strftime("%Y-%m-%d")
    today   = _TODAY.strftime("%Y-%m-%d")
    ranked  = sorted(results, key=lambda x: (-x["total_pct"], x["symbol"]))
    disp    = ranked[:top] if top else ranked
    p       = params

    total_pnl = sum(r["total"] for r in results)
    total_tr  = sum(r["trades"] for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)

    print()
    print("═" * 74)
    print(f"  RSI(2)拡張版  {len(SYMBOLS)}銘柄スキャン  直近{label}  【{mode_label}】"
          + (f"  上位{top}銘柄" if top else ""))
    print(f"  期間: {since} ～ {today}  フィルター: {flags}")
    if mkt and mkt.get("ok"):
        a25  = "▲" if mkt["above25"]  else "▼"
        a200 = "▲" if mkt["above200"] else "▼"
        print(f"  【地合い】日経 {mkt['close']:,.0f}円  "
              f"MA25 {mkt['ma25']:,.0f}{a25}  MA200 {mkt['ma200']:,.0f}{a200}  "
              f"→ {mkt['phase']}")
    print(f"  【条件】RSI(2)≤{p['RSI2_ENTRY']:.0f} + MA200上 → 翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{p['RSI2_EXIT']:.0f} / ATR×{p['ATR_TRAIL_MULT']}トレイル / "
          f"-{p['HARD_STOP_PCT']:.0f}%損切り / +{p['HALF_PROFIT_PCT']:.0f}%半分利確")
    print("═" * 74)
    print(f"  スキャン: {len(SYMBOLS)}銘柄  シグナルあり: {len(results)}銘柄  "
          f"トレード計: {total_tr}回  プラス銘柄: {plus_cnt}/{len(results)}")
    print()
    print(f"  {'順位':<4} {'銘柄':<22} {'損益':>10} {'累積%':>7} {'勝率':>6} "
          f"{'PF':>5} {'取引':>4} {'平均保有':>7}")
    print("  " + "─" * 70)
    for rank, r in enumerate(disp, 1):
        pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        sign  = "+" if r["total"] >= 0 else ""
        psign = "+" if r["total_pct"] >= 0 else ""
        bar   = ("▲" if r["total_pct"] >= 0 else "▽") * min(int(abs(r["total_pct"]) / 3), 8)
        label2 = f"{r['name']}({r['symbol']})"
        print(f"  {rank:<4} {label2:<22} "
              f"{sign}{r['total']:>9,.0f}円  "
              f"{psign}{r['total_pct']:>5.1f}%  "
              f"{r['wr']:>5.1f}%  {pf_s:>5}  "
              f"{r['trades']:>3}回  {r['avg_hold']:>5.1f}日  {bar}")
    print("  " + "─" * 62)
    sign = "+" if total_pnl >= 0 else ""
    print(f"  合計損益（全銘柄・重複あり）: {sign}{total_pnl:,.0f}円")
    print()


# ── HTML生成（1銘柄詳細）────────────────────────────────────
def generate_html_hv(symbol: str, trades: list[dict],
                     backtest_days: int, label: str,
                     params: dict, mode_label: str, flags: str) -> Path:
    since = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = _TODAY.strftime("%Y-%m-%d")
    p     = params

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades) if trades else 0
    wr    = len(wins) / len(trades) * 100 if trades else 0
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0
    total_pct = sum(t["pct"] for t in trades) if trades else 0
    total_cls = "pos" if total >= 0 else "neg"

    cum, chart_dates, chart_cum = 0.0, [], []
    for t in trades:
        cum += t["pnl"]
        chart_dates.append(t["exit_dt"].strftime("%Y-%m-%d"))
        chart_cum.append(round(cum, 0))

    rows = ""
    for i, t in enumerate(trades, 1):
        cls = "hold" if "保有中" in t["reason"] else ("win" if t["pnl"] > 0 else "lose")
        rows += (
            f'<tr class="{cls}">'
            f'<td>{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["entry_p"]:,.0f}</td><td>{t["exit_p"]:,.0f}</td>'
            f'<td>{t["qty"]}</td>'
            f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
            f'<td class="{"pos" if t["pct"]>=0 else "neg"}">{t["pct"]:+.2f}%</td>'
            f'<td>{t["hold"]}日</td><td>{t["reason"]}</td>'
            f'</tr>\n'
        )

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI(2)拡張版 [{symbol}] {label}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #a78bfa;padding-left:12px;margin-bottom:6px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 0 16px}}
.mode-badge{{display:inline-block;background:#1e1b4b;border:1px solid #a78bfa;
             color:#a78bfa;border-radius:6px;padding:2px 10px;font-size:0.8em;margin-left:8px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.chart-wrap{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:16px;margin:20px 0;max-width:900px}}
table{{width:100%;border-collapse:collapse;font-size:0.86em;margin-top:16px}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3),th:last-child{{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3),td:last-child{{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
</style>
</head>
<body>
<h1>RSI(2)拡張版 [{symbol}] 直近{label}<span class="mode-badge">{mode_label}</span></h1>
<div class="meta">期間: {since} ～ {today} &nbsp;|&nbsp; フィルター: {flags}</div>
<div class="meta">【条件】RSI(2)≤{p['RSI2_ENTRY']:.0f} + MA200上 &nbsp;
  【決済】RSI(2)≥{p['RSI2_EXIT']:.0f} / ATR×{p['ATR_TRAIL_MULT']}トレイル /
  -{p['HARD_STOP_PCT']:.0f}%損切り / +{p['HALF_PROFIT_PCT']:.0f}%半分利確</div>

<div class="cards">
  <div class="card"><div class="clabel">損益合計</div>
    <div class="cval {total_cls}">{total:+,.0f}円</div></div>
  <div class="card"><div class="clabel">累積リターン</div>
    <div class="cval {total_cls}">{total_pct:+.2f}%</div></div>
  <div class="card"><div class="clabel">勝率</div>
    <div class="cval neu">{wr:.1f}%</div></div>
  <div class="card"><div class="clabel">プロフィットF</div>
    <div class="cval neu">{pf_s}</div></div>
  <div class="card"><div class="clabel">トレード数</div>
    <div class="cval neu">{len(trades)}回</div></div>
  <div class="card"><div class="clabel">勝/負</div>
    <div class="cval neu">{len(wins)}勝{len(loss)}負</div></div>
  <div class="card"><div class="clabel">平均保有(勝)</div>
    <div class="cval pos">{wh:.1f}日</div></div>
  <div class="card"><div class="clabel">平均保有(負)</div>
    <div class="cval neg">{lh:.1f}日</div></div>
</div>

<div class="chart-wrap"><canvas id="cumChart" height="80"></canvas></div>

<table>
<thead><tr>
  <th>#</th><th>エントリー</th><th>エグジット</th>
  <th>買値</th><th>売値</th><th>株数</th>
  <th>損益</th><th>変化率</th><th>保有日数</th><th>決済理由</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

<script>
new Chart(document.getElementById('cumChart'), {{
  type:'line',
  data:{{labels:{chart_dates},datasets:[{{label:'累積損益（円）',data:{chart_cum},
    borderColor:'#a78bfa',backgroundColor:'rgba(167,139,250,0.08)',
    borderWidth:2,pointRadius:3,fill:true,tension:0.3}}]}},
  options:{{responsive:true,
    plugins:{{legend:{{labels:{{color:'#aaa'}}}},
      tooltip:{{callbacks:{{label:c=>c.parsed.y.toLocaleString()+'円'}}}}}},
    scales:{{x:{{ticks:{{color:'#666',maxTicksLimit:12}},grid:{{color:'#1e2030'}}}},
      y:{{ticks:{{color:'#666',callback:v=>v.toLocaleString()+'円'}},grid:{{color:'#1e2030'}}}}}}}}
}});
</script>
</body></html>"""

    path = Path(f"rsi2hv_{symbol.replace('.','_')}_{_TODAY.strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── HTML生成（スキャン）──────────────────────────────────────
def generate_html_scan_hv(results: list[dict], days: int, label: str,
                           top: int | None, params: dict,
                           mode_label: str, flags: str,
                           mkt: dict | None) -> Path:
    since   = (_TODAY - timedelta(days=days)).strftime("%Y-%m-%d")
    today   = _TODAY.strftime("%Y-%m-%d")
    ranked  = sorted(results, key=lambda x: (-x["total_pct"], x["symbol"]))
    disp    = ranked[:top] if top else ranked
    p       = params

    total_pnl = sum(r["total"] for r in results)
    total_tr  = sum(r["trades"] for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)
    total_cls = "pos" if total_pnl >= 0 else "neg"
    top_label = f"  上位{top}銘柄" if top else ""

    rank_rows = ""
    for rank, r in enumerate(disp, 1):
        pf_s = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        cls  = "win" if r["total"] >= 0 else "lose"
        pcls = "pos" if r["total"] >= 0 else "neg"
        rank_rows += (
            f'<tr class="{cls}" onclick="toggleDetail(\'{r["symbol"]}\')" style="cursor:pointer">'
            f'<td>{rank}</td><td>{r["symbol"]}</td><td>{r["name"]}</td>'
            f'<td class="{pcls}">{r["total"]:+,.0f}円</td>'
            f'<td class="{pcls}">{r["total_pct"]:+.2f}%</td>'
            f'<td>{r["wr"]:.1f}%</td><td>{pf_s}</td>'
            f'<td>{r["trades"]}</td><td>{r["avg_hold"]:.1f}日</td>'
            f'</tr>\n'
            f'<tr id="detail-{r["symbol"]}" style="display:none">'
            f'<td colspan="9" style="padding:0">{_trade_table(r["trade_log"])}</td>'
            f'</tr>\n'
        )

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI(2)拡張版 {len(SYMBOLS)}銘柄スキャン {label}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #a78bfa;padding-left:12px;margin-bottom:6px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 0 16px}}
.mode-badge{{display:inline-block;background:#1e1b4b;border:1px solid #a78bfa;
             color:#a78bfa;border-radius:6px;padding:2px 10px;font-size:0.8em;margin-left:8px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.chart-wrap{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:16px;margin:20px 0}}
table{{width:100%;border-collapse:collapse;font-size:0.86em}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3){{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.inner-table{{width:100%;font-size:0.84em;background:#0d0f1a}}
.inner-table th{{background:#0d0f1a;font-size:0.8em}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
.mkt-banner{{border-radius:8px;padding:10px 16px;margin:14px 0;
             font-size:0.9em;display:flex;align-items:center;gap:18px;flex-wrap:wrap}}
.mkt-on{{background:#0f2a1a;border:1px solid #166534;color:#4ade80}}
.mkt-caution{{background:#2a1f0a;border:1px solid #92400e;color:#fbbf24}}
.mkt-off{{background:#2a0f0f;border:1px solid #7f1d1d;color:#f87171}}
.mkt-unknown{{background:#16192a;border:1px solid #252840;color:#666}}
.mkt-item{{display:flex;flex-direction:column;align-items:center;gap:2px}}
.mkt-lbl{{font-size:0.75em;opacity:.7}}
.mkt-val{{font-size:1.1em;font-weight:700}}
</style>
</head>
<body>
<h1>RSI(2)拡張版  {len(SYMBOLS)}銘柄スキャン  直近{label}{top_label}<span class="mode-badge">{mode_label}</span></h1>
<div class="meta">期間: {since} ～ {today} &nbsp;|&nbsp; フィルター: {flags}</div>
<div class="meta">【条件】RSI(2)≤{p['RSI2_ENTRY']:.0f} + MA200上 &nbsp;
  【決済】RSI(2)≥{p['RSI2_EXIT']:.0f} / ATR×{p['ATR_TRAIL_MULT']}トレイル /
  -{p['HARD_STOP_PCT']:.0f}%損切り / +{p['HALF_PROFIT_PCT']:.0f}%半分利確</div>
{_mkt_banner_html(mkt)}
<div class="cards">
  <div class="card"><div class="clabel">合計損益</div>
    <div class="cval {total_cls}">{total_pnl:+,.0f}円</div></div>
  <div class="card"><div class="clabel">スキャン銘柄</div>
    <div class="cval neu">{len(SYMBOLS)}銘柄</div></div>
  <div class="card"><div class="clabel">シグナルあり</div>
    <div class="cval neu">{len(results)}銘柄</div></div>
  <div class="card"><div class="clabel">トレード計</div>
    <div class="cval neu">{total_tr}回</div></div>
  <div class="card"><div class="clabel">プラス銘柄</div>
    <div class="cval pos">{plus_cnt}/{len(results)}</div></div>
</div>

<div class="chart-wrap"><canvas id="barChart" height="60"></canvas></div>

<p style="color:#666;font-size:0.82em;margin-bottom:8px">▼ 行をクリックするとトレード明細を展開</p>
<table>
<thead><tr>
  <th>#</th><th>コード</th><th>銘柄名</th>
  <th>損益</th><th>累積%</th><th>勝率</th><th>PF</th><th>取引数</th><th>平均保有</th>
</tr></thead>
<tbody>{rank_rows}</tbody>
</table>
<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

<script>
const labels = {[r['name'] for r in disp]};
const vals   = {[round(r['total_pct'], 2) for r in disp]};
const colors = vals.map(v => v >= 0 ? 'rgba(167,139,250,0.7)' : 'rgba(248,113,113,0.7)');
new Chart(document.getElementById('barChart'), {{
  type:'bar',
  data:{{labels,datasets:[{{label:'累積リターン(%)',data:vals,backgroundColor:colors,borderRadius:3}}]}},
  options:{{responsive:true,
    plugins:{{legend:{{display:false}},
      tooltip:{{callbacks:{{label:c=>c.parsed.y.toFixed(2)+'%'}}}}}},
    scales:{{
      x:{{ticks:{{color:'#555',font:{{size:10}},maxRotation:45}},grid:{{color:'#1e2030'}}}},
      y:{{ticks:{{color:'#555',callback:v=>v.toFixed(1)+'%'}},grid:{{color:'#1e2030'}}}}}}}}
}});
function toggleDetail(sym){{
  const el=document.getElementById('detail-'+sym);
  el.style.display=el.style.display==='none'?'':'none';
}}
</script>
</body></html>"""

    path = Path(f"rsi2hv_scan_{_TODAY.strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン ──────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="RSI(2) 拡張版バックテスト（高ボラ対応・自動レジーム切替）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python rsi2_hv.py                    # 全銘柄スキャン（1年）
  python rsi2_hv.py --years 2          # 2年スキャン
  python rsi2_hv.py 7011.T             # 三菱重工 詳細
  python rsi2_hv.py --mode normal      # 強制的に通常モード
  python rsi2_hv.py --mode hv          # 強制的に高ボラモード
  python rsi2_hv.py --no-ibs           # IBSフィルターなし
  python rsi2_hv.py --no-consec        # 連続RSI確認なし（1日でOK）
  python rsi2_hv.py --vix              # VIXフィルター有効化
""")
    parser.add_argument("symbol",    nargs="?", default=None)
    parser.add_argument("--days",    type=int, default=None)
    parser.add_argument("--months",  type=int, default=None)
    parser.add_argument("--years",   type=int, default=None)
    parser.add_argument("--top",     type=int, default=None)
    parser.add_argument("--mode",    choices=["auto", "normal", "hv"], default="auto",
                        help="パラメーターモード（default: auto=日経MA200で自動判定）")
    parser.add_argument("--no-ibs",   dest="use_ibs",   action="store_false",
                        help="IBSフィルターを無効化")
    parser.add_argument("--no-consec", dest="use_consec", action="store_false",
                        help="連続RSI確認を無効化（1日シグナルでOK）")
    parser.add_argument("--vix",      dest="use_vix",   action="store_true",
                        help="VIX RSI(7)>50 フィルターを有効化")
    args = parser.parse_args()

    if args.days is not None:
        days, label = args.days, f"{args.days}日"
    elif args.months is not None:
        days, label = args.months * 30, f"{args.months}ヶ月"
    elif args.years is not None:
        days, label = args.years * 365, f"{args.years}年"
    else:
        days, label = BACKTEST_DAYS, "1年"

    # ── 日経・VIX を並行取得 ─────────────────────────────────
    print(f"\n  データ準備中 ...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        nk_fut  = ex.submit(fetch_nikkei, days)
        vix_fut = ex.submit(fetch_vix, days) if args.use_vix else None

        if args.symbol:
            sym = args.symbol.upper()
            if not sym.endswith(".T"):
                sym += ".T"
            stock_fut = ex.submit(fetch, sym, days)
        else:
            stock_data: dict = {}
            futs = {ex.submit(fetch, s, days): (s, n) for s, n in SYMBOLS}

        nk_df  = nk_fut.result()
        vix_rsi = vix_fut.result() if vix_fut else None

    mkt = _market_info(nk_df)

    # ── モード判定 ───────────────────────────────────────────
    if args.mode == "hv":
        params     = HV
        mode_label = "高ボラモード（手動指定）"
    elif args.mode == "normal":
        params     = NORMAL
        mode_label = "通常モード（手動指定）"
    else:  # auto
        if mkt.get("ok") and mkt["above200"]:
            params     = NORMAL
            mode_label = "通常モード（自動: 日経MA200上）"
        else:
            params     = HV
            mode_label = "高ボラモード（自動: 日経MA200割れ）"

    # フィルター説明文
    f_parts = []
    if args.use_consec: f_parts.append("連続RSI")
    if args.use_ibs:    f_parts.append("IBS<0.35")
    if args.use_vix and vix_rsi is not None:
        f_parts.append("VIX-RSI(7)>50")
    elif args.use_vix:
        f_parts.append("VIX取得失敗→スキップ")
    flags = " + ".join(f_parts) if f_parts else "なし"

    # ── 1銘柄詳細モード ─────────────────────────────────────
    if args.symbol:
        df = stock_fut.result()
        if df is None:
            print(f"  エラー: {sym} のデータ取得に失敗しました\n")
            return
        df     = calc(df)
        trades = backtest_hv(df, days, params,
                             use_ibs=args.use_ibs,
                             use_consec=args.use_consec,
                             vix_rsi=vix_rsi)
        print_result(sym, trades, days, label, params, mode_label, flags)
        path = generate_html_hv(sym, trades, days, label, params, mode_label, flags)
        print(f"  HTMLレポート保存: {path}")
        webbrowser.open(f"file://{path.resolve()}")
        return

    # ── 全銘柄スキャンモード ─────────────────────────────────
    print(f"  {len(SYMBOLS)}銘柄データ取得中 ...")
    done = 0
    for fut in as_completed(futs):
        sym2, name = futs[fut]
        done += 1
        print(f"\r  取得中 {done}/{len(SYMBOLS)} ...", end="", flush=True)
        df2 = fut.result()
        if df2 is not None:
            stock_data[sym2] = (name, df2)
    print(f"\r  取得完了: {len(stock_data)}/{len(SYMBOLS)} 銘柄              ")

    results = []
    for sym2, (name, df2) in stock_data.items():
        trades = backtest_hv(calc(df2), days, params,
                             use_ibs=args.use_ibs,
                             use_consec=args.use_consec,
                             vix_rsi=vix_rsi)
        if not trades:
            continue
        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        wr    = len(wins) / len(trades) * 100
        pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                 if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        total_pct = sum(t["pct"] for t in trades)
        results.append(dict(
            symbol=sym2, name=name,
            trades=len(trades), total=total, total_pct=total_pct,
            wr=wr, pf=pf,
            avg_hold=sum(t["hold"] for t in trades) / len(trades),
            trade_log=trades,
        ))

    if not results:
        print("  シグナルが発生した銘柄がありませんでした。")
        return

    print_ranking(results, days, label, args.top, params, mode_label, flags, mkt)
    path = generate_html_scan_hv(results, days, label, args.top, params,
                                  mode_label, flags, mkt)
    print(f"  HTMLレポート保存: {path}")
    webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
