"""
RSI(2) 平均回帰バックテスト 拡張版 V2 — 高ボラ対応・自動レジーム切替
────────────────────────────────────────────────────────────────────
【V2 コンセプト】
  パラメータは V1 と同じ（シグナル品質を維持）
  デフォルト対象を日経225 全銘柄に拡大 → 高品質シグナルをより多く獲得
  ※ パラメータを緩めると利益率が低下するため、対象拡大で取引回数を増やす

改善点（海外論文・過去事例に基づく）:
  1. 自動レジーム検知: 日経MA200 で「通常 / 高ボラ」を判定しパラメータ切替
  2. IBSフィルター: Internal Bar Strength < 0.35 のみエントリー（維持）
  3. 連続RSI確認: V2ではデフォルト無効（--consec で有効化）
  4. VIXフィルター（任意）: ^VIX の RSI(7)>50 の時のみエントリー

使い方:
  python rsi2_hv_v2.py                    # 監視20銘柄スキャン（1年）
  python rsi2_hv_v2.py --all              # 日経225全銘柄スキャン（1年）
  python rsi2_hv_v2.py --years 2          # 2年
  python rsi2_hv_v2.py 7011.T             # 1銘柄詳細
  python rsi2_hv_v2.py --mode normal      # 強制的に通常モード
  python rsi2_hv_v2.py --mode hv          # 強制的に高ボラモード
  python rsi2_hv_v2.py --no-ibs           # IBSフィルターなし
  python rsi2_hv_v2.py --consec           # 連続RSI確認を有効化（V2デフォルトは無効）
  python rsi2_hv_v2.py --vix              # VIXフィルター有効化
"""

import io
import sys

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# rsi2.py のデータ取得・指標計算を流用
from rsi2 import (
    SYMBOLS as _ALL_SYMBOLS, _TODAY, WORKERS, _CACHE_DIR,
    calc,
    _market_info, _mkt_banner_html, _trade_table,
)

# V2: デフォルトは日経225全銘柄（--watch で監視20銘柄に切替）
from symbols_watch_rsi2 import SYMBOLS as _WATCH_SYMBOLS_RSI2
SYMBOLS = _ALL_SYMBOLS

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
    """永続キャッシュ優先（fetch_all.py 作成分）・フォールバックでダウンロード。"""
    # ── 永続キャッシュ確認 ──────────────────────────────────────
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            with open(persistent, "rb") as f:
                df = pickle.load(f)
            last_date = df.index[-1]
            _is_weekday = _TODAY.weekday() < 5  # 月〜金
            _now_jst = datetime.now(timezone(timedelta(hours=9)))
            if _is_weekday and (_now_jst.hour, _now_jst.minute) >= (15, 30):
                _required = _TODAY
            else:
                _prev = _TODAY - pd.Timedelta(days=1)
                while _prev.weekday() >= 5:
                    _prev -= pd.Timedelta(days=1)
                _required = _prev
            stale = pd.Timestamp(last_date.date()) < _required
            # キャッシュ検証: 価格変動があるか確認（汚染されたキャッシュを除外）
            price_range = float(df["close"].max() - df["close"].min())
            valid = price_range > 0.01 * float(df["close"].mean())
            if len(df) >= 210 and not stale and valid:
                return df
            persistent.unlink(missing_ok=True)
        except Exception:
            persistent.unlink(missing_ok=True)

    # ── フォールバック: 直接ダウンロード ──────────────────────────
    # period= はyfinanceサーバー基準で切り捨てが起こるため、明示的なstart/endを使用
    buf_days = 200 + 30
    _total_cal = int((backtest_days + buf_days) * 1.5)
    _now_jst = datetime.now(timezone(timedelta(hours=9)))
    _dl_start = (_now_jst - timedelta(days=_total_cal)).strftime("%Y-%m-%d")
    _dl_end   = (_now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.Ticker(symbol).history(start=_dl_start, end=_dl_end, interval="1d",
                                         auto_adjust=False)
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_convert(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        raw = raw[cols].dropna()
        if len(raw) < 210:
            return None
        df = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
        # キャッシュに保存
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(persistent, "wb") as f:
                pickle.dump(df, f)
        except Exception:
            pass
        return df
    except Exception:
        return None


def fetch_nikkei(backtest_days: int) -> pd.DataFrame | None:
    """日経平均を取得。auto_adjust=False（実際の終値）。"""
    buf = 200 + 30
    dl_start = (_TODAY - timedelta(days=backtest_days + buf)).strftime("%Y-%m-%d")
    dl_end   = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.Ticker("^N225").history(start=dl_start, end=dl_end,
                                          interval="1d", auto_adjust=False)
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_convert(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        raw = raw[["close"]].dropna().copy()
        raw["ma25"]  = raw["close"].rolling(25).mean()
        raw["ma200"] = raw["close"].rolling(200).mean()
        return raw
    except Exception:
        return None

# ── パラメーター: 通常モード ─────────────────────────────────
NORMAL = dict(
    RSI2_ENTRY      = 10.0,   # V1 と同じ（品質維持）
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
    RSI2_ENTRY      =  5.0,   # V1 と同じ（品質維持）
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
    buf_days = 200 + 30
    _total_cal = int((backtest_days + buf_days) * 1.5)
    _now_jst = datetime.now(timezone(timedelta(hours=9)))
    _dl_start = (_now_jst - timedelta(days=_total_cal)).strftime("%Y-%m-%d")
    _dl_end   = (_now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.Ticker("^VIX").history(start=_dl_start, end=_dl_end, interval="1d",
                                         auto_adjust=False)
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_convert(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
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
                exit_p = op   # 当日始値（実際の市場データ）で決済
                reason = "損切り"
            elif lo <= trail:
                exit_p = op   # 当日始値（実際の市場データ）で決済
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

    path = Path(f"rsi2hv_v2_{symbol.replace('.','_')}_{_TODAY.strftime('%Y%m%d')}_{label}.html")
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

    path = Path(f"rsi2hv_v2_scan_{_TODAY.strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── シグナルスキャン ─────────────────────────────────────────
def scan_signals_rsi2(
    stock_data_map: dict,
    results_90d: list[dict],
    params: dict,
    use_ibs: bool,
    use_consec: bool,
    vix_rsi,
) -> dict:
    """本日終値ベースで翌日の買い/売りシグナルを判定する。
    stock_data_map : {sym: (name, df_raw)}
    results_90d    : run_backtest(90日) の結果（保有中銘柄の検出に使用）
    """
    today = _TODAY.strftime("%Y-%m-%d")
    p     = params

    # 90日バックテストで保有中の銘柄マップ
    open_pos_map: dict[str, dict] = {}
    for r in results_90d:
        for t in r.get("trade_log", []):
            if "保有中" in t.get("reason", ""):
                open_pos_map[r["symbol"]] = {"result": r, "trade": t}
                break

    buy: list[dict]  = []
    sell: list[dict] = []
    hold: list[dict] = []

    for sym, (name, df_raw) in stock_data_map.items():
        df = calc(df_raw)
        if len(df) < 3:
            continue

        last  = df.iloc[-1]   # 本日
        prev  = df.iloc[-2]   # 前日（エントリー判定に使う）
        prev2 = df.iloc[-3]   # 前々日（連続RSI確認）

        if pd.isna(prev["rsi2"]) or pd.isna(prev["ma200"]) or pd.isna(prev2["rsi2"]):
            continue

        last_close  = float(last["close"])
        last_rsi2   = float(last["rsi2"])
        prev_rsi2   = float(prev["rsi2"])
        prev2_rsi2  = float(prev2["rsi2"])
        last_ma200  = float(last["ma200"]) if not pd.isna(last["ma200"]) else 0.0
        last_atr    = float(last["atr"])   if not pd.isna(last["atr"])   else 0.0
        atr_pct     = last_atr / last_close * 100 if last_close > 0 else 0.0
        above_ma200 = last_close > last_ma200
        h_l = float(last["high"]) - float(last["low"])
        ibs = (last_close - float(last["low"])) / h_l if h_l > 0 else 1.0
        signal_dt = str(df.index[-1].date())

        # ── 買いシグナル判定 ─────────────────────────────────
        ok_rsi    = last_rsi2 <= p["RSI2_ENTRY"]
        ok_ma200  = above_ma200
        ok_consec = (not use_consec) or (prev_rsi2 <= p["RSI2_ENTRY"])
        ok_ibs    = (not use_ibs) or (ibs < 0.35)
        ok_vix    = True
        if vix_rsi is not None:
            try:
                vr = float(vix_rsi.asof(df.index[-1]))
                ok_vix = (not pd.isna(vr)) and vr > 50
            except Exception:
                ok_vix = True

        if ok_rsi and ok_ma200 and ok_consec and ok_ibs and ok_vix:
            filters = []
            if use_consec: filters.append(f"連続RSI({prev_rsi2:.1f}→{last_rsi2:.1f})")
            if use_ibs:    filters.append(f"IBS={ibs:.2f}")
            buy.append({
                "symbol":     sym,
                "name":       name,
                "open":       float(df.iloc[-1]["open"]),
                "close":      last_close,
                "rsi2":       last_rsi2,
                "rsi2_prev":  prev_rsi2,
                "ma200":      last_ma200,
                "atr_pct":    atr_pct,
                "ibs":        ibs,
                "above_ma200": above_ma200,
                "signal_dt":  signal_dt,
                "filters":    " / ".join(filters) if filters else "基本のみ",
            })

        # ── 売り/継続保有 ─────────────────────────────────────
        if sym in open_pos_map:
            t          = open_pos_map[sym]["trade"]
            entry_p    = t["entry_p"]
            entry_dt   = t["entry_dt"]
            hold_days  = (pd.Timestamp(_TODAY) - entry_dt).days
            unrealized = (last_close - entry_p) / entry_p * 100

            common = {
                "symbol":      sym,
                "name":        name,
                "open":        float(df.iloc[-1]["open"]),
                "close":       last_close,
                "entry_price": entry_p,
                "entry_dt":    str(entry_dt.date()),
                "hold_days":   hold_days,
                "unrealized":  unrealized,
                "rsi2":        last_rsi2,
                "atr_pct":     atr_pct,
            }
            # RSI(2) ≥ 出口閾値 → 売りシグナル
            if last_rsi2 >= p["RSI2_EXIT"]:
                common["exit_reason"] = f"RSI(2)={last_rsi2:.1f}≥{p['RSI2_EXIT']}"
                sell.append(common)
            else:
                hold.append(common)

    buy.sort(key=lambda x: x["rsi2"])  # RSI(2) が低い順（最も売られた銘柄を優先）
    return {"buy": buy, "sell": sell, "hold": hold, "today": today}


def print_signals_rsi2(sig: dict, mode_label: str, params: dict) -> None:
    """scan_signals_rsi2 の結果をターミナルに表示。"""
    today = sig["today"]
    buy   = sig["buy"]
    sell  = sig["sell"]
    hold  = sig["hold"]
    p     = params

    print()
    print("═" * 68)
    print(f"  RSI(2)シグナル【{mode_label}】  {today} 引け後")
    print(f"  エントリー: RSI(2)≤{p['RSI2_ENTRY']} + MA200上 / エグジット: RSI(2)≥{p['RSI2_EXIT']}")
    print("═" * 68)

    # ── 買いシグナル ──────────────────────────────────────────
    print(f"\n  ◆ 買いシグナル  ({len(buy)} 銘柄)  ← 明日の始値で購入候補")
    if not buy:
        print("    なし")
    else:
        print(f"  {'#':<3} {'銘柄':<22} {'始値':>8} {'終値':>8} {'RSI2':>5} {'MA200':>8} {'IBS':>5} {'ATR%':>5}  フィルター")
        print("  " + "─" * 82)
        for i, c in enumerate(buy, 1):
            label = f"{c['name']}({c['symbol']})"
            ma_mark = "↑" if c["above_ma200"] else "↓"
            print(f"  {i:<3} {label:<22} {c['open']:>8,.0f} {c['close']:>8,.0f} "
                  f"{c['rsi2']:>5.1f} {c['ma200']:>8,.0f}{ma_mark} "
                  f"{c['ibs']:>5.2f} {c['atr_pct']:>5.1f}%  {c['filters']}")

    # ── 売りシグナル ──────────────────────────────────────────
    print(f"\n  ◆ 売りシグナル  ({len(sell)} 銘柄)  ← 明日の始値で売却候補")
    if not sell:
        print("    なし")
    else:
        print(f"  {'銘柄':<22} {'始値':>8} {'終値':>8} {'買値':>8} {'含み損益':>9} "
              f"{'保有日':>5} {'RSI2':>5}  出口理由")
        print("  " + "─" * 80)
        for c in sell:
            label = f"{c['name']}({c['symbol']})"
            sign  = "+" if c["unrealized"] >= 0 else ""
            print(f"  {label:<22} {c['open']:>8,.0f} {c['close']:>8,.0f} {c['entry_price']:>8,.0f} "
                  f"{sign}{c['unrealized']:>+8.1f}% {c['hold_days']:>4}日 "
                  f"{c['rsi2']:>5.1f}  {c['exit_reason']}")

    # ── 継続保有 ──────────────────────────────────────────────
    print(f"\n  ◆ 継続保有  ({len(hold)} 銘柄)  ← RSI(2)回復待ち・保有継続")
    if not hold:
        print("    なし")
    else:
        print(f"  {'銘柄':<22} {'始値':>8} {'終値':>8} {'買値':>8} {'含み損益':>9} "
              f"{'保有日':>5} {'RSI2':>5}")
        print("  " + "─" * 70)
        for c in hold:
            label = f"{c['name']}({c['symbol']})"
            sign  = "+" if c["unrealized"] >= 0 else ""
            print(f"  {label:<22} {c['open']:>8,.0f} {c['close']:>8,.0f} {c['entry_price']:>8,.0f} "
                  f"{sign}{c['unrealized']:>+8.1f}% {c['hold_days']:>4}日 "
                  f"{c['rsi2']:>5.1f}")

    print()
    print(f"  ※ 翌営業日の始値で執行（成行 or 寄付指値）")
    print(f"  ※ 保有中銘柄は直近90日バックテストで検出（実際のポジションと異なる場合あり）")
    print()


def generate_signal_html_rsi2(sig: dict, mode_label: str, params: dict) -> Path:
    """RSI(2)シグナルをHTMLレポートに出力する。"""
    today = sig["today"]
    buy   = sig["buy"]
    sell  = sig["sell"]
    hold  = sig["hold"]
    p     = params

    def _buy_rows(items: list[dict]) -> str:
        if not items:
            return '<tr><td colspan="8" style="text-align:center;color:#888">なし</td></tr>'
        rows = ""
        for c in items:
            ma_cls  = "pos" if c["above_ma200"] else "neg"
            ibs_cls = "pos" if c["ibs"] < 0.35 else ""
            rows += f"""<tr>
  <td>{c['name']}<br><small>{c['symbol']}</small></td>
  <td class="num">{c['open']:,.0f}</td>
  <td class="num">{c['close']:,.0f}</td>
  <td class="num pos">{c['rsi2']:.1f}</td>
  <td class="num {ma_cls}">{c['ma200']:,.0f}</td>
  <td class="num {ibs_cls}">{c['ibs']:.2f}</td>
  <td class="num">{c['atr_pct']:.1f}%</td>
  <td>{c['filters']}</td>
  <td>{c['signal_dt']}</td>
</tr>"""
        return rows

    def _pos_rows(items: list[dict], show_reason: bool) -> str:
        if not items:
            return '<tr><td colspan="8" style="text-align:center;color:#888">なし</td></tr>'
        rows = ""
        for c in items:
            sign = "+" if c["unrealized"] >= 0 else ""
            cls  = "pos" if c["unrealized"] >= 0 else "neg"
            extra = f"<td>{c.get('exit_reason','')}</td>" if show_reason else ""
            rows += f"""<tr>
  <td>{c['name']}<br><small>{c['symbol']}</small></td>
  <td class="num">{c['open']:,.0f}</td>
  <td class="num">{c['close']:,.0f}</td>
  <td class="num">{c['entry_price']:,.0f}</td>
  <td class="num {cls}">{sign}{c['unrealized']:.1f}%</td>
  <td class="num">{c['hold_days']}日</td>
  <td class="num">{c['rsi2']:.1f}</td>
  {extra}
</tr>"""
        return rows

    buy_head_extra  = "<th>フィルター</th><th>シグナル日</th>"
    sell_head_extra = "<th>出口理由</th>"
    colspan_buy  = 8
    colspan_pos  = 7

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>RSI(2)シグナル {today}</title>
<style>
  body {{ background:#1a1a2e; color:#e0e0e0; font-family:'Meiryo',sans-serif; padding:20px; }}
  h1   {{ color:#00d4ff; font-size:1.3em; border-bottom:1px solid #444; padding-bottom:8px; }}
  h2   {{ color:#ffd700; font-size:1.1em; margin-top:28px; }}
  table {{ border-collapse:collapse; width:100%; margin-top:8px; }}
  th   {{ background:#2a2a4a; color:#aaa; padding:8px 12px; text-align:left; font-size:.85em; }}
  td   {{ padding:7px 12px; border-bottom:1px solid #2a2a3a; font-size:.9em; }}
  tr:hover td {{ background:#252540; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .pos {{ color:#4caf50; }}
  .neg {{ color:#f44336; }}
  .note {{ color:#888; font-size:.8em; margin-top:16px; }}
</style>
</head>
<body>
<h1>RSI(2)シグナル【{mode_label}】― {today} 引け後</h1>
<p style="color:#aaa;font-size:.85em">
  エントリー: RSI(2)≤{p['RSI2_ENTRY']} + MA200上 ／ エグジット: RSI(2)≥{p['RSI2_EXIT']}
  ／ ATRトレイル×{p['ATR_TRAIL_MULT']} ／ 損切り-{p['HARD_STOP_PCT']}% ／ 半分利確+{p['HALF_PROFIT_PCT']}%
</p>

<h2>◆ 買いシグナル（{len(buy)} 銘柄）― 明日の始値で購入候補</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>始値</th><th>終値</th><th>RSI(2)</th><th>MA200</th><th>IBS</th>
    <th>ATR%</th>{buy_head_extra}
  </tr></thead>
  <tbody>{_buy_rows(buy)}</tbody>
</table>

<h2>◆ 売りシグナル（{len(sell)} 銘柄）― 明日の始値で売却候補</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>始値</th><th>終値</th><th>買値</th><th>含み損益</th>
    <th>保有日</th><th>RSI(2)</th>{sell_head_extra}
  </tr></thead>
  <tbody>{_pos_rows(sell, show_reason=True)}</tbody>
</table>

<h2>◆ 継続保有（{len(hold)} 銘柄）― RSI(2)回復待ち・保有継続</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>始値</th><th>終値</th><th>買値</th><th>含み損益</th><th>保有日</th><th>RSI(2)</th>
  </tr></thead>
  <tbody>{_pos_rows(hold, show_reason=False)}</tbody>
</table>

<p class="note">※ 保有中銘柄は直近90日バックテストで検出（実際のポジションとは異なる場合があります）</p>
<p class="note">生成: {today}</p>
</body>
</html>"""

    path = Path(f"signal_rsi2_v2_{today}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン ──────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="RSI(2) 拡張版バックテスト（高ボラ対応・自動レジーム切替）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python rsi2_hv.py --signal           # 明日の売買シグナル（監視20銘柄）
  python rsi2_hv.py --signal --all     # 明日の売買シグナル（日経225全銘柄）
  python rsi2_hv.py                    # 監視20銘柄スキャン（1年）
  python rsi2_hv.py --all              # 日経225全銘柄スキャン（1年）
  python rsi2_hv.py --years 2          # 2年スキャン
  python rsi2_hv.py 7011.T             # 三菱重工 詳細
  python rsi2_hv.py --mode normal      # 強制的に通常モード
  python rsi2_hv.py --mode hv          # 強制的に高ボラモード
  python rsi2_hv.py --no-ibs           # IBSフィルターなし
  python rsi2_hv.py --no-consec        # 連続RSI確認なし（1日でOK）
  python rsi2_hv.py --vix              # VIXフィルター有効化
""")
    parser.add_argument("symbol",    nargs="?", default=None)
    parser.add_argument("--signal",  action="store_true",
                        help="明日の売買シグナルをスキャンしてHTML出力")
    parser.add_argument("--watch",   action="store_true",
                        help="監視対象20銘柄に絞る（デフォルト: 日経225全銘柄）")
    parser.add_argument("--all",     action="store_true",
                        help="日経225全銘柄をスキャン（V2ではデフォルト）")
    parser.add_argument("--days",    type=int, default=None)
    parser.add_argument("--months",  type=int, default=None)
    parser.add_argument("--years",   type=int, default=None)
    parser.add_argument("--top",     type=int, default=None)
    parser.add_argument("--mode",    choices=["auto", "normal", "hv"], default="auto",
                        help="パラメーターモード（default: auto=日経MA200で自動判定）")
    parser.add_argument("--no-ibs",   dest="use_ibs",   action="store_false",
                        help="IBSフィルターを無効化")
    parser.add_argument("--no-consec", dest="use_consec", action="store_false",
                        help="連続RSI確認を無効化（V2デフォルト: 無効）")
    parser.add_argument("--consec",    dest="use_consec", action="store_true",
                        help="連続RSI確認を有効化（V2ではデフォルト無効）")
    parser.add_argument("--vix",      dest="use_vix",   action="store_true",
                        help="VIX RSI(7)>50 フィルターを有効化")
    # V2: 連続RSI確認をデフォルト無効に変更
    parser.set_defaults(use_ibs=True, use_consec=False)
    args = parser.parse_args()

    # V2: デフォルトは225全銘柄。--watch で監視20銘柄に切替。
    # symbols_listed_*.py が存在すれば全上場銘柄を自動使用。
    global SYMBOLS
    if args.watch:
        SYMBOLS = _WATCH_SYMBOLS_RSI2
    else:
        # 全上場銘柄ファイルを自動検出（prime → standard → all の順）
        _listed_symbols = None
        for _candidate in ["symbols_listed_prime.py",
                           "symbols_listed_standard.py",
                           "symbols_listed_all.py"]:
            _p = Path(_candidate)
            if _p.exists():
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location("_listed_rsi2", _p)
                _mod  = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                _listed_symbols = _mod.SYMBOLS
                print(f"  銘柄ユニバース: {_candidate} ({len(_listed_symbols)}銘柄)")
                break
        if _listed_symbols:
            SYMBOLS = _listed_symbols
        # else: SYMBOLS はすでに _ALL_SYMBOLS (モジュール初期値)

    if args.days is not None:
        days, label = args.days, f"{args.days}日"
    elif args.months is not None:
        days, label = args.months * 30, f"{args.months}ヶ月"
    elif args.years is not None:
        days, label = args.years * 365, f"{args.years}年"
    else:
        days, label = BACKTEST_DAYS, "1年"

    if args.signal:
        # ── シグナルスキャンモード ───────────────────────────────
        mode_label_s = "日経225全銘柄" if args.all else f"監視対象{len(SYMBOLS)}銘柄"
        print(f"\n  RSI(2)シグナルスキャン  ({mode_label_s})")
        print(f"  シグナル日: {_TODAY.strftime('%Y-%m-%d')}\n")

        # 日経・VIX取得でモード確定
        print(f"  日経・VIXデータ取得中...")
        with ThreadPoolExecutor(max_workers=2) as ex:
            nk_fut  = ex.submit(fetch_nikkei, 90)
            vix_fut = ex.submit(fetch_vix, 90) if args.use_vix else None
            nk_df   = nk_fut.result()
            vix_rsi = vix_fut.result() if vix_fut else None

        mkt = _market_info(nk_df)
        if args.mode == "hv":
            params, mode_label = HV, "高ボラモード（手動指定）"
        elif args.mode == "normal":
            params, mode_label = NORMAL, "通常モード（手動指定）"
        else:
            if mkt.get("ok") and mkt["above200"]:
                params, mode_label = NORMAL, "通常モード（自動: 日経MA200上）"
            else:
                params, mode_label = HV, "高ボラモード（自動: 日経MA200割れ）"

        # 株価データ取得
        total      = len(SYMBOLS)
        stock_data: dict = {}
        print(f"  [Phase 1] データ取得中 ({total}銘柄)  ※キャッシュ済みは高速スキップ")
        skipped = 0
        for i, (sym, name) in enumerate(SYMBOLS, 1):
            df = fetch(sym, 90)
            if df is None:
                skipped += 1
            else:
                stock_data[sym] = (name, df)
            print(f"  {i}/{total} 取得済  (スキップ: {skipped})", end="\r", flush=True)
        print(f"  {total}/{total} 完了  成功: {len(stock_data)}銘柄  スキップ: {skipped}銘柄      ")

        # 90日バックテストで保有中銘柄を検出
        print(f"\n  [Phase 2] 保有中銘柄の検出（直近90日バックテスト）...")
        tasks_90 = [(s, n, d) for s, (n, d) in stock_data.items()]
        results_90: list[dict] = []

        def _bt90(task):
            trades = backtest_hv(
                calc(task[2]), 90, params,
                use_ibs=args.use_ibs, use_consec=args.use_consec, vix_rsi=vix_rsi,
            )
            if not trades:
                return None
            wins  = [t for t in trades if t["pnl"] > 0]
            loss  = [t for t in trades if t["pnl"] <= 0]
            total_pnl = sum(t["pnl"] for t in trades)
            wr    = len(wins) / len(trades) * 100
            pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                     if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
            return dict(
                symbol=task[0], name=task[1],
                trades=len(trades), total=total_pnl, total_pct=sum(t["pct"] for t in trades),
                wr=wr, pf=pf,
                avg_hold=sum(t["hold"] for t in trades) / len(trades),
                trade_log=trades,
            )

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(_bt90, t): t[0] for t in tasks_90}
            done = 0
            for fut in as_completed(futures):
                done += 1
                r = fut.result()
                if r:
                    results_90.append(r)
                print(f"  計算中 {done}/{len(tasks_90)}", end="\r", flush=True)
        print()

        print(f"\n  [Phase 3] シグナル解析中...")
        sig = scan_signals_rsi2(stock_data, results_90, params,
                                 use_ibs=args.use_ibs, use_consec=args.use_consec,
                                 vix_rsi=vix_rsi)
        print_signals_rsi2(sig, mode_label, params)

        try:
            from portfolio import print_positions_for_signal
            print_positions_for_signal("RSI2")
        except ImportError:
            pass

        html_path = generate_signal_html_rsi2(sig, mode_label, params)
        print(f"  HTMLレポート: {html_path.resolve()}")
        webbrowser.open(html_path.resolve().as_uri())
        print()
        return

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
