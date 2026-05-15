"""
指値エントリー バックテスト (MACD / A7 / RSI2)
────────────────────────────────────────────────────────────────────────
【概要】
  3つの戦略（MACD、A7/ストキャスATR、RSI2）に対して
  指値エントリーをシミュレーションするバックテスト。

  シグナル発生時に指値注文を設定し、
  ENTRY_EXPIRE 日以内に低値が指値以下になれば約定。
  約定後は目標価格 / ストップロス / 最大保有日数で決済。

使い方:
  python backtest_limit_entry.py                    # 全戦略・日経225・1年
  python backtest_limit_entry.py --macd             # MACDのみ
  python backtest_limit_entry.py --a7               # A7のみ
  python backtest_limit_entry.py --rsi2             # RSI2のみ
  python backtest_limit_entry.py --days 180         # 直近6ヶ月
  python backtest_limit_entry.py --days 365         # 直近1年（デフォルト）
  python backtest_limit_entry.py --top 30           # 上位30銘柄表示
  python backtest_limit_entry.py --no-browser       # ブラウザを開かない
  python backtest_limit_entry.py --workers 8        # 並列数
  python backtest_limit_entry.py 7203.T             # 特定銘柄詳細
"""

import io
import sys

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
import yfinance as yf

from symbols_all import SYMBOLS

# ── 定数 ────────────────────────────────────────────────────────
JST           = timezone(timedelta(hours=9))
_TODAY        = datetime.now(JST).date()
_CACHE_DIR    = Path(".rsi2_cache")

ENTRY_EXPIRE  = 3      # 指値有効日数
MAX_HOLD      = 15     # 最大保有日数
INITIAL_CASH  = 500_000
POSITION_SIZE = 100_000
FIXED_QTY     = 100         # 固定株数（常に100株）
BACKTEST_DAYS = 365
WORKERS       = 4
MAX_QTY       = 9999        # 最大株数（低価格株の過剰ポジション防止）
MIN_PRICE     = 100.0       # 最低株価（データ異常排除）
MAX_PRICE     = 100_000.0   # 最高株価（日本株最大~7万円、100万超はデータ異常）
MAX_ATR_RATIO = 0.20        # ATR/終値の上限（20%超は異常ボラ・データ異常として除外）

# ── 実運用コストモデル (Phase B) ─────────────────────────────────
# 逆指値 (stop buy) はギャップアップで不利な約定になりやすい前提。
# バックテストでは注文価格ちょうどで約定する仮定だったため、
# 実運用との乖離を埋めるための定数。
SLIPPAGE_STOP_PCT = 0.005   # 逆指値約定時の不利スリッページ (買い +0.5%, 売り -0.5%)
SLIPPAGE_LIMIT_PCT = 0.0    # 指値は注文価格で約定する前提 (または有利なら良化)
FEE_PCT_ONE_WAY   = 0.001   # 手数料 片道 0.1% → 往復 0.2%
                            # (SBI/楽天の現物格安プラン想定。信用取引はもっと低い)

# ── 逆指値→指値注文 の指値上限マージン (kabu発注用) ─────────────
# 逆指値→成行 の代替として 逆指値→指値 を使う場合、トリガー価格からどの程度
# 上までを指値として許容するか。ギャップアップが +MARGIN 以下なら約定、
# それ以上なら不約定となる。
# 0.01 (1%) は典型的な朝ギャップの 70% 程度をカバーする実用的な値。
LIMIT_ENTRY_MARGIN_PCT = 0.01

# ── MACD パラメータ ──────────────────────────────────────────────
MACD_FAST         = 8
MACD_SLOW         = 17
MACD_SIGNAL       = 5
VOL_MA_PERIOD     = 20
VOL_SPIKE_MULT    = 1.2
MA_TREND_PERIOD   = 10
ATR_PERIOD_MACD   = 14

# ── A7 パラメータ ────────────────────────────────────────────────
STOCH_K_PERIOD    = 14
STOCH_D_PERIOD    = 3
STOCH_SMOOTH      = 3
STOCH_OVERBOUGHT  = 70
MA_TREND_PERIOD_A7 = 75
ATR_PERIOD_A7     = 14

# ── RSI2 パラメータ ──────────────────────────────────────────────
RSI2_ENTRY        = 10.0
ATR_PERIOD_RSI2   = 14


# ── データ取得 ──────────────────────────────────────────────────
# ── ベンチマーク (日経平均) リターン ────────────────────────────
_N225_CACHE: dict[int, float] = {}


def fetch_n225_return(days: int) -> float:
    """
    日経平均 (^N225) の直近 days 日間のリターン (%) を返す。
    プロセス内でキャッシュ。失敗時は 0.0。
    """
    if days in _N225_CACHE:
        return _N225_CACHE[days]
    try:
        # 直近 days 営業日のデータを取得 (買い注文マージン込みで +20)
        buf = max(int(days * 1.5) + 20, 40)
        now_jst  = datetime.now(JST)
        dl_start = (now_jst - timedelta(days=buf)).strftime("%Y-%m-%d")
        dl_end   = (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = yf.Ticker("^N225").history(
            start=dl_start, end=dl_end,
            interval="1d", auto_adjust=False, actions=False
        )
        if len(raw) < 2:
            _N225_CACHE[days] = 0.0
            return 0.0
        # days 営業日前以降のデータで計算
        cutoff = now_jst - timedelta(days=days)
        subset = raw[raw.index >= pd.Timestamp(cutoff, tz=raw.index.tz)] \
                 if raw.index.tz else raw[raw.index >= pd.Timestamp(cutoff)]
        if len(subset) < 2:
            subset = raw
        start_p = float(subset["Close"].iloc[0])
        end_p   = float(subset["Close"].iloc[-1])
        ret = (end_p - start_p) / start_p * 100.0
        _N225_CACHE[days] = ret
        return ret
    except Exception:
        _N225_CACHE[days] = 0.0
        return 0.0


def _expected_latest_bar_date():
    """
    現在時刻から「期待される最新の取引日」を返す。
      - 平日 15:00 JST 以降 → 今日 (引け済み)
      - 平日 15:00 JST より前 → 前営業日
      - 土曜日             → 金曜日
      - 日曜日             → 金曜日
      - 月曜日 15時前      → 前金曜日
    祝日は考慮しない (祝日でデータが無い場合は yfinance が前営業日を返すので
    結果オーライ)。
    """
    now    = datetime.now(JST)
    today  = now.date()
    wd     = today.weekday()  # 0=Mon ... 6=Sun

    if wd == 5:  # 土
        return today - timedelta(days=1)
    if wd == 6:  # 日
        return today - timedelta(days=2)

    # 平日
    if now.hour >= 15:
        return today  # 引け後 → 今日
    # 引け前 → 前営業日
    if wd == 0:  # 月
        return today - timedelta(days=3)  # 前金曜
    return today - timedelta(days=1)


def fetch(symbol: str, backtest_days: int = BACKTEST_DAYS) -> pd.DataFrame | None:
    """永続キャッシュ優先・フォールバックでダウンロード。

    キャッシュ判定:
      - キャッシュ内 df の最新バー日付 >= _expected_latest_bar_date() なら有効
      - そうでなければ再取得 (引け前作成 → 引け後実行 のパターンも自動更新)
    """
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            with open(persistent, "rb") as f:
                df = pickle.load(f)
            if len(df) >= 210:
                latest_bar = df.index[-1]
                latest_date = latest_bar.date() if hasattr(latest_bar, "date") else latest_bar
                expected    = _expected_latest_bar_date()
                price_range = float(df["close"].max() - df["close"].min())
                valid_range = price_range > 0.01 * float(df["close"].mean())
                if latest_date >= expected and valid_range:
                    # 株価異常値を除去
                    pct_chg = df["close"].pct_change().abs()
                    df = df[pct_chg <= 0.5].copy()
                    if len(df) >= 210:
                        return df
                # 古いキャッシュ → fall through で再取得
        except Exception:
            pass

    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)
    now_jst   = datetime.now(JST)
    dl_start  = (now_jst - timedelta(days=total_cal)).strftime("%Y-%m-%d")
    dl_end    = (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        raw = yf.Ticker(symbol).history(
            start=dl_start, end=dl_end,
            interval="1d", auto_adjust=False, actions=False
        )
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        raw = raw[cols]
        _last = raw.iloc[-1]
        if pd.isna(_last["close"]) and _last.get("volume", 0) > 0:
            try:
                lp = yf.Ticker(symbol).fast_info.last_price
                if lp and not pd.isna(lp):
                    raw.at[raw.index[-1], "close"] = float(lp)
            except Exception:
                pass
        raw = raw.dropna(subset=["close"])
        if len(raw) < 210:
            return None
        # 株価異常値を除去（前日比±50%超はデータエラーとして除外）
        pct_chg = raw["close"].pct_change().abs()
        raw = raw[pct_chg <= 0.5].copy()
        if len(raw) < 210:
            return None
        df_out = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
        try:
            _CACHE_DIR.mkdir(exist_ok=True)
            with open(persistent, "wb") as f:
                pickle.dump(df_out, f)
        except Exception:
            pass
        return df_out
    except Exception:
        return None


# ── MACD インジケーター計算 ──────────────────────────────────────
def calc_macd(df: pd.DataFrame) -> pd.DataFrame:
    """MACDブレイクアウト × 出来高急増 × トレンドフィルター。"""
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # ATR
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=ATR_PERIOD_MACD, adjust=False).mean()

    # MACD
    ema_fast    = c.ewm(span=MACD_FAST,   adjust=False).mean()
    ema_slow    = c.ewm(span=MACD_SLOW,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram   = macd_line - signal_line

    # 出来高・トレンドフィルター
    vol_ma = v.rolling(VOL_MA_PERIOD).mean()
    ma10   = c.rolling(MA_TREND_PERIOD).mean()

    prev_hist  = histogram.shift(1)
    prev2_hist = histogram.shift(2)

    vol_ok   = v > vol_ma * VOL_SPIKE_MULT
    trend_ok = c > ma10

    zero_cross_up = (histogram > 0) & (prev_hist <= 0)
    hist_accel    = (histogram > 0) & (histogram > prev_hist) & (prev_hist > prev2_hist)

    df["atr"]       = atr
    df["entry_sig"] = (zero_cross_up | hist_accel) & vol_ok & trend_ok

    return df


# ── A7 インジケーター計算 ────────────────────────────────────────
def calc_a7(df: pd.DataFrame) -> pd.DataFrame:
    """ストキャスティクスゴールデンクロス × MA75トレンドフィルター。"""
    df = df.copy()
    h = df["high"]
    l = df["low"]
    c = df["close"]

    lowest_low   = l.rolling(STOCH_K_PERIOD).min()
    highest_high = h.rolling(STOCH_K_PERIOD).max()
    denom        = highest_high - lowest_low
    fast_k = (c - lowest_low) / denom.replace(0, np.nan) * 100
    slow_k = fast_k.rolling(STOCH_SMOOTH).mean()
    slow_d = slow_k.rolling(STOCH_D_PERIOD).mean()

    ma75 = c.rolling(MA_TREND_PERIOD_A7).mean()

    prev_k = slow_k.shift(1)
    prev_d = slow_d.shift(1)
    golden_cross = (slow_k > slow_d) & (prev_k <= prev_d)

    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=ATR_PERIOD_A7, adjust=False).mean()

    df["stoch_k"]   = slow_k
    df["stoch_d"]   = slow_d
    df["ma75"]      = ma75
    df["atr"]       = atr
    df["entry_sig"] = golden_cross & (slow_k < STOCH_OVERBOUGHT) & (c > ma75)

    return df


# ── RSI2 インジケーター計算 ──────────────────────────────────────
def calc_rsi2(df: pd.DataFrame) -> pd.DataFrame:
    """RSI(2) 平均回帰 × MA200トレンドフィルター × IBS。"""
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # RSI(2) — Wilder α=1/2
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    rsi2  = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # MA200
    ma200 = c.rolling(200).mean()

    # ATR(14)
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=ATR_PERIOD_RSI2, adjust=False).mean()

    # IBS（Internal Bar Strength）
    bar_range = h - l
    ibs = np.where(bar_range > 0, (c - l) / bar_range, 0.5)

    df["rsi2"]      = rsi2
    df["ma200"]     = ma200
    df["atr"]       = atr
    df["ibs"]       = ibs
    df["entry_sig"] = (rsi2 <= RSI2_ENTRY) & (c > ma200) & (pd.Series(ibs, index=c.index) < 0.35)

    return df


# ── 指値エントリー バックテスト ─────────────────────────────────
def run_limit_backtest(
    symbol: str,
    name: str,
    df: pd.DataFrame,
    calc_fn,
    entry_atr_mult: float,
    stop_atr_mult: float,
    target_atr_mult: float,
    backtest_days: int,
    strategy_name: str,
    entry_type: str = "limit",   # "limit"=指値（下がれば買う） / "stop"=逆指値（上がれば買う） / "stop_sell"=逆指値売り
) -> dict:
    """
    指値 or 逆指値エントリー + OCO決済 バックテスト。

    entry_type="limit"    : 安値 <= order_price で約定（押し目買い）
    entry_type="stop"     : 高値 >= order_price で約定（ブレイクアウト買い）
    entry_type="stop_sell": 安値 <= order_price で約定（逆指値売り・空売り）
                            order_p = close - atr*em, stop (損切り) = order_p + atr*sm (上方)
                            target (利確) = order_p - atr*tm (下方), PnL=(entry-exit)*qty

    Returns: per-symbol result dict
    """
    # 指標計算
    try:
        df = calc_fn(df)
    except Exception as e:
        return _empty_result(symbol, name, strategy_name)

    # バックテスト期間にカット
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()

    if len(df) < 5:
        return _empty_result(symbol, name, strategy_name)

    trades: list[dict] = []
    signals = 0
    is_short = (entry_type == "stop_sell")

    # 状態変数
    state         = "idle"    # idle / pending / in_pos
    limit_price   = 0.0
    stop_price    = 0.0
    target_price  = 0.0
    expire_idx    = 0
    signal_idx    = 0         # シグナル発生バー番号（約定日数計算用）
    signal_dt     = None      # シグナル発生日
    signal_price  = 0.0       # シグナル発生時の終値
    days_to_fill  = 0
    hold_start    = 0
    entry_p       = 0.0
    entry_dt: pd.Timestamp | None = None
    qty           = 0

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]

        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        atr_prev = float(prev.get("atr", np.nan))
        if pd.isna(atr_prev) or atr_prev <= 0:
            continue

        # ── idle: シグナル判定 ─────────────────────────────────────
        # 注意: idle を先に処理することで、シグナル翌日（当日 bar）も
        # 即座に pending チェックへ落ちて約定できる。
        # 実運用セマンティクス: シグナル日 T の引け後に逆指値注文を置き、
        # T+1 の始値から発動可能 → バックテストも同じ T+1 の hi/lo で判定。
        if state == "idle":
            entry_sig = bool(prev.get("entry_sig", False))
            if not entry_sig:
                continue
            if pd.isna(atr_prev) or atr_prev <= 0:
                continue

            close_prev = float(prev["close"])

            # データ異常・低価格株・高価格異常を除外
            if close_prev < MIN_PRICE or close_prev > MAX_PRICE:
                continue
            if atr_prev / close_prev > MAX_ATR_RATIO:
                continue

            if entry_type == "stop":
                # 逆指値（買い）：終値 + ATR×mult を上抜けたら買う
                lp = close_prev + atr_prev * entry_atr_mult
                sp = lp - atr_prev * stop_atr_mult
                tp = lp + atr_prev * target_atr_mult
                if lp <= 0 or sp <= 0 or tp <= lp:
                    continue
            elif entry_type == "stop_sell":
                # 逆指値（売り）：終値 - ATR×mult を下抜けたら売る
                lp = close_prev - atr_prev * entry_atr_mult
                sp = lp + atr_prev * stop_atr_mult   # 損切り (ABOVE entry)
                tp = lp - atr_prev * target_atr_mult  # 目標   (BELOW entry)
                if lp <= 0 or tp <= 0 or tp >= lp or sp <= lp:
                    continue
            else:
                # 指値：終値 - ATR×mult まで下がれば買う
                lp = close_prev - atr_prev * entry_atr_mult
                sp = lp - atr_prev * stop_atr_mult
                tp = lp + atr_prev * target_atr_mult
                if lp <= 0 or sp <= 0 or tp <= lp:
                    continue

            signals += 1

            limit_price   = lp
            stop_price    = sp
            target_price  = tp
            expire_idx    = i + ENTRY_EXPIRE
            signal_idx    = i
            days_to_fill  = 0
            signal_dt     = df.index[i - 1]   # entry_sig が立った足（prev）の日付
            signal_price  = close_prev         # その日の終値
            state         = "pending"
            # fall through to pending check below (same bar = T+1)

        # ── pending: 注文の約定チェック ──────────────────────────
        if state == "pending":
            if i > expire_idx:
                # 有効期限切れ → キャンセル
                state = "idle"

            elif (entry_type == "stop"      and hi >= limit_price) or \
                 (entry_type == "stop_sell" and lo <= limit_price) or \
                 (entry_type == "limit"     and lo <= limit_price):
                # 約定
                if entry_type == "stop":
                    entry_p = limit_price * (1.0 + SLIPPAGE_STOP_PCT)
                elif entry_type == "stop_sell":
                    entry_p = limit_price * (1.0 - SLIPPAGE_STOP_PCT)
                else:
                    entry_p = limit_price * (1.0 + SLIPPAGE_LIMIT_PCT)
                entry_dt      = dt
                hold_start    = i
                days_to_fill  = i - signal_idx
                qty           = FIXED_QTY
                state         = "in_pos"

                # 約定と同日に決済が発生するか確認
                if is_short:
                    hit_tgt = lo <= target_price
                    hit_stp = hi >= stop_price
                else:
                    hit_tgt = hi >= target_price
                    hit_stp = lo <= stop_price

                if hit_tgt and hit_stp:
                    exit_p      = target_price
                    exit_reason = "目標達成"
                elif hit_tgt:
                    exit_p      = target_price
                    exit_reason = "目標達成"
                elif hit_stp:
                    exit_p      = stop_price
                    exit_reason = "損切り"
                else:
                    exit_p = None
                    exit_reason = None

                if exit_p is not None:
                    if exit_reason == "損切り":
                        if is_short:
                            exit_p = stop_price * (1.0 + SLIPPAGE_STOP_PCT)
                        else:
                            exit_p = exit_p * (1.0 - SLIPPAGE_STOP_PCT)
                    if entry_p * 0.1 <= exit_p <= entry_p * 10.0:
                        fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
                        if is_short:
                            pnl = (entry_p - exit_p) * qty - fee
                            pct = (entry_p - exit_p) / entry_p * 100
                        else:
                            pnl = (exit_p - entry_p) * qty - fee
                            pct = (exit_p - entry_p) / entry_p * 100
                        trades.append(dict(
                            entry_dt=entry_dt, exit_dt=dt,
                            entry_p=entry_p, exit_p=exit_p, qty=qty,
                            pnl=pnl, pct=pct,
                            fee=round(fee, 0),
                            hold_days=0, days_to_fill=days_to_fill,
                            signal_dt=signal_dt, signal_price=signal_price,
                            order_limit=limit_price, order_stop=stop_price,
                            order_target=target_price,
                            reason=exit_reason,
                        ))
                    state = "idle"
                continue

        # ── in_pos: OCO決済チェック ───────────────────────────────
        if state == "in_pos":
            hold_days = i - hold_start
            exit_p    = None
            exit_reason = None

            if is_short:
                hit_tgt = lo <= target_price
                hit_stp = hi >= stop_price
            else:
                hit_tgt = hi >= target_price
                hit_stp = lo <= stop_price

            if hit_tgt and hit_stp:
                exit_p      = target_price
                exit_reason = "目標達成"
            elif hit_tgt:
                exit_p      = target_price
                exit_reason = "目標達成"
            elif hit_stp:
                exit_p      = stop_price
                exit_reason = "損切り"
            elif hold_days >= MAX_HOLD:
                exit_p      = cl
                exit_reason = "タイムカット"

            if exit_p is not None:
                if not (entry_p * 0.1 <= exit_p <= entry_p * 10.0):
                    state = "idle"
                    continue
                if exit_reason == "損切り":
                    if is_short:
                        exit_p = stop_price * (1.0 + SLIPPAGE_STOP_PCT)
                    else:
                        exit_p = exit_p * (1.0 - SLIPPAGE_STOP_PCT)
                fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
                if is_short:
                    pnl = (entry_p - exit_p) * qty - fee
                    pct = (entry_p - exit_p) / entry_p * 100
                else:
                    pnl = (exit_p - entry_p) * qty - fee
                    pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p, qty=qty,
                    pnl=pnl, pct=pct,
                    fee=round(fee, 0),
                    hold_days=hold_days, days_to_fill=days_to_fill,
                    signal_dt=signal_dt, signal_price=signal_price,
                    order_limit=limit_price, order_stop=stop_price,
                    order_target=target_price,
                    reason=exit_reason,
                ))
                state = "idle"
            continue

    # 未決済ポジション
    if state == "in_pos" and entry_dt is not None:
        cl_last = float(df.iloc[-1]["close"])
        hold_days = len(df) - 1 - hold_start
        fee = (entry_p + cl_last) * qty * FEE_PCT_ONE_WAY
        if is_short:
            pnl = (entry_p - cl_last) * qty - fee
            pct = (entry_p - cl_last) / entry_p * 100
        else:
            pnl = (cl_last - entry_p) * qty - fee
            pct = (cl_last - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=cl_last, qty=qty,
            pnl=pnl, pct=pct,
            fee=round(fee, 0),
            hold_days=hold_days, days_to_fill=days_to_fill,
            signal_dt=signal_dt, signal_price=signal_price,
            order_limit=limit_price, order_stop=stop_price,
            order_target=target_price,
            reason="保有中",
        ))

    # 異常トレードをデバッグ出力
    for t in trades:
        if abs(t["pnl"]) > 500_000:
            import sys
            print(f"[DEBUG] {strategy_name} {symbol}: entry={t['entry_p']:.0f} exit={t['exit_p']:.0f} "
                  f"qty={t['qty']} pnl={t['pnl']:+,.0f} reason={t['reason']} "
                  f"entry_dt={t['entry_dt']} exit_dt={t['exit_dt']}", file=sys.stderr)

    # 統計計算
    filled  = len(trades)
    wins    = sum(1 for t in trades if t["pnl"] > 0)
    losses  = sum(1 for t in trades if t["pnl"] <= 0)
    win_rate = wins / filled * 100 if filled > 0 else 0.0
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf           = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    total_pnl    = sum(t["pnl"] for t in trades)
    total_fee    = sum(t.get("fee", 0) for t in trades)
    avg_hold     = sum(t["hold_days"] for t in trades) / filled if filled > 0 else 0.0
    fill_rate    = filled / signals * 100 if signals > 0 else 0.0

    return dict(
        symbol=symbol, name=name, strategy=strategy_name,
        signals=signals, filled=filled,
        trades=filled, wins=wins, losses=losses,
        win_rate=win_rate, pf=pf, total_pnl=total_pnl,
        total_fee=total_fee,
        slippage_pct=SLIPPAGE_STOP_PCT, fee_pct_one_way=FEE_PCT_ONE_WAY,
        avg_hold=avg_hold, fill_rate=fill_rate,
        trade_log=trades,
    )


def _empty_result(symbol: str, name: str, strategy: str) -> dict:
    return dict(
        symbol=symbol, name=name, strategy=strategy,
        signals=0, filled=0, trades=0, wins=0, losses=0,
        win_rate=0.0, pf=0.0, total_pnl=0.0,
        total_fee=0.0,
        slippage_pct=SLIPPAGE_STOP_PCT, fee_pct_one_way=FEE_PCT_ONE_WAY,
        avg_hold=0.0, fill_rate=0.0, trade_log=[],
    )


# ── 全銘柄バックテスト ───────────────────────────────────────────
def backtest_all_symbols(
    symbols: list[tuple[str, str]],
    calc_fn,
    entry_atr_mult: float,
    stop_atr_mult: float,
    target_atr_mult: float,
    backtest_days: int,
    workers: int,
    strategy_name: str,
) -> list[dict]:
    """全銘柄のデータ取得とバックテストを並列実行。"""
    results: list[dict] = []

    def _process(sym_name: tuple[str, str]) -> dict | None:
        symbol, name = sym_name
        df = fetch(symbol, backtest_days)
        if df is None:
            return None
        return run_limit_backtest(
            symbol, name, df, calc_fn,
            entry_atr_mult, stop_atr_mult, target_atr_mult,
            backtest_days, strategy_name,
        )

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process, sn): sn for sn in symbols}
        done = 0
        total = len(symbols)
        for fut in as_completed(futures):
            done += 1
            sym, _ = futures[fut]
            try:
                res = fut.result()
                if res is not None and res["signals"] > 0:
                    results.append(res)
            except Exception as e:
                pass
            if done % 20 == 0 or done == total:
                print(f"  {strategy_name}: {done}/{total} 処理済み", flush=True)

    results.sort(key=lambda r: r["total_pnl"], reverse=True)
    return results


# ── HTML レポート生成 ────────────────────────────────────────────
def build_html(
    all_results: dict[str, list[dict]],
    backtest_days: int,
    top_n: int,
) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── 戦略サマリー ──────────────────────────────────────────────
    strategy_rows = ""
    for strat, results in all_results.items():
        all_trades = [t for r in results for t in r["trade_log"]]
        n_trades   = len(all_trades)
        n_wins     = sum(1 for t in all_trades if t["pnl"] > 0)
        n_losses   = sum(1 for t in all_trades if t["pnl"] <= 0)
        wr         = n_wins / n_trades * 100 if n_trades > 0 else 0
        gp         = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
        gl         = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
        pf         = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
        total_pnl  = sum(t["pnl"] for t in all_trades)
        n_signals  = sum(r["signals"] for r in results)
        n_filled   = sum(r["filled"] for r in results)
        fill_pct   = n_filled / n_signals * 100 if n_signals > 0 else 0

        pnl_cls = "profit" if total_pnl >= 0 else "loss"
        pf_str  = f"{pf:.2f}" if pf != float("inf") else "∞"
        strategy_rows += f"""
        <tr>
          <td class="sym">{strat}</td>
          <td>{n_signals}</td>
          <td>{n_filled}</td>
          <td>{n_trades}</td>
          <td>{n_wins}</td>
          <td>{n_losses}</td>
          <td>{wr:.1f}%</td>
          <td>{pf_str}</td>
          <td class="{pnl_cls}">{total_pnl:+,.0f}円</td>
          <td>{fill_pct:.1f}%</td>
        </tr>"""

    # ── 銘柄ランキング ────────────────────────────────────────────
    all_sym_results: list[dict] = []
    for results in all_results.values():
        all_sym_results.extend(results)
    all_sym_results.sort(key=lambda r: r["total_pnl"], reverse=True)

    show_results = all_sym_results[:top_n] if top_n > 0 else all_sym_results

    symbol_rows = ""
    for rank, r in enumerate(show_results, 1):
        pnl_cls = "profit" if r["total_pnl"] >= 0 else "loss"
        pf_str  = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
        symbol_rows += f"""
        <tr>
          <td>{rank}</td>
          <td class="sym">{r['symbol']}<br><small>{r['name']}</small></td>
          <td><span class="tag tag-{r['strategy'].lower()}">{r['strategy']}</span></td>
          <td>{r['signals']}</td>
          <td>{r['trades']}</td>
          <td>{r['win_rate']:.1f}%</td>
          <td>{pf_str}</td>
          <td class="{pnl_cls}">{r['total_pnl']:+,.0f}円</td>
          <td>{r['avg_hold']:.1f}日</td>
          <td>{r['fill_rate']:.1f}%</td>
        </tr>"""

    # ── 個別トレード一覧 ──────────────────────────────────────────
    trade_sections = ""
    for r in show_results:
        if not r["trade_log"]:
            continue
        trade_rows = ""
        for t in r["trade_log"]:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            entry_str = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            exit_str  = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
            trade_rows += f"""
              <tr>
                <td>{entry_str}</td>
                <td>{exit_str}</td>
                <td>{t['entry_p']:,.0f}</td>
                <td>{t['exit_p']:,.0f}</td>
                <td>{t['qty']}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td class="{pnl_cls}">{t['pct']:+.2f}%</td>
                <td>{t['hold_days']}日</td>
                <td>{t['reason']}</td>
              </tr>"""
        trade_sections += f"""
        <div class="trade-section">
          <h3>{r['symbol']} {r['name']}
            <span class="tag tag-{r['strategy'].lower()}">{r['strategy']}</span>
            <span class="{('profit' if r['total_pnl']>=0 else 'loss')}">{r['total_pnl']:+,.0f}円</span>
          </h3>
          <table>
            <thead><tr>
              <th>エントリー</th><th>エグジット</th>
              <th>エントリー価格</th><th>エグジット価格</th>
              <th>数量</th><th>損益(円)</th><th>損益(%)</th>
              <th>保有日数</th><th>理由</th>
            </tr></thead>
            <tbody>{trade_rows}</tbody>
          </table>
        </div>"""

    # ── HTML組立 ──────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>指値エントリー バックテスト — {today_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Segoe UI", "Hiragino Sans", sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    padding: 20px;
  }}
  h1 {{ color: #60a5fa; margin-bottom: 4px; font-size: 1.6rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 24px; font-size: 0.9rem; }}
  h2 {{ color: #60a5fa; margin: 28px 0 12px; font-size: 1.2rem; border-left: 3px solid #60a5fa; padding-left: 10px; }}
  h3 {{ color: #cbd5e1; margin: 0 0 10px; font-size: 1rem; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .card {{
    background: #1e293b;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 24px;
    overflow-x: auto;
  }}
  .trade-section {{
    background: #1e293b;
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }}
  th {{
    background: #0f172a;
    color: #94a3b8;
    padding: 8px 12px;
    text-align: right;
    white-space: nowrap;
    border-bottom: 1px solid #334155;
  }}
  th:first-child, th:nth-child(2) {{ text-align: left; }}
  td {{
    padding: 7px 12px;
    border-bottom: 1px solid #1e293b;
    text-align: right;
    white-space: nowrap;
  }}
  td:first-child, td:nth-child(2) {{ text-align: left; }}
  tr:hover td {{ background: #263045; }}
  .sym {{ color: #60a5fa; font-weight: 600; }}
  .profit {{ color: #4ade80; }}
  .loss   {{ color: #f87171; }}
  .tag {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 700;
  }}
  .tag-macd  {{ background: #1e40af; color: #93c5fd; }}
  .tag-a7    {{ background: #5b21b6; color: #c4b5fd; }}
  .tag-rsi2  {{ background: #065f46; color: #6ee7b7; }}
  small {{ color: #64748b; font-size: 0.8rem; }}
  .params-note {{ color: #64748b; font-size: 0.8rem; margin-bottom: 8px; }}
</style>
</head>
<body>
<h1>指値エントリー バックテスト</h1>
<p class="subtitle">
  対象期間: 直近 {backtest_days}日 &nbsp;|&nbsp;
  指値有効: {ENTRY_EXPIRE}日 &nbsp;|&nbsp;
  最大保有: {MAX_HOLD}日 &nbsp;|&nbsp;
  生成日時: {today_str}
</p>

<h2>戦略別サマリー</h2>
<div class="card">
  <table>
    <thead><tr>
      <th>戦略</th><th>シグナル数</th><th>約定数</th><th>取引数</th>
      <th>勝</th><th>負</th><th>勝率</th><th>PF</th><th>合計損益</th><th>フィル率</th>
    </tr></thead>
    <tbody>{strategy_rows}</tbody>
  </table>
</div>

<h2>銘柄ランキング（合計損益順）</h2>
<div class="card">
  <table>
    <thead><tr>
      <th>順位</th><th>銘柄</th><th>戦略</th><th>シグナル</th>
      <th>取引数</th><th>勝率</th><th>PF</th><th>合計損益</th>
      <th>平均保有日数</th><th>フィル率(%)</th>
    </tr></thead>
    <tbody>{symbol_rows}</tbody>
  </table>
</div>

<h2>個別トレード明細</h2>
{trade_sections}

</body>
</html>"""
    return html


# ── 1銘柄詳細表示 ────────────────────────────────────────────────
def print_single_symbol(symbol: str, results: list[dict]) -> None:
    """特定銘柄のトレード結果を端末に表示。"""
    matched = [r for r in results if r["symbol"] == symbol]
    if not matched:
        print(f"{symbol} のデータが見つかりません")
        return
    for r in matched:
        print(f"\n{'='*60}")
        print(f" {r['symbol']} {r['name']}  [{r['strategy']}]")
        print(f"{'='*60}")
        print(f"  シグナル数: {r['signals']}  約定数: {r['filled']}  フィル率: {r['fill_rate']:.1f}%")
        print(f"  取引数: {r['trades']}  勝: {r['wins']}  負: {r['losses']}  勝率: {r['win_rate']:.1f}%")
        pf_str = f"{r['pf']:.2f}" if r['pf'] != float("inf") else "∞"
        print(f"  PF: {pf_str}  合計損益: {r['total_pnl']:+,.0f}円  平均保有: {r['avg_hold']:.1f}日")
        if r["trade_log"]:
            print(f"\n  {'エントリー':<12} {'エグジット':<12} {'EP':>8} {'XP':>8} {'PnL':>10} {'%':>7}  理由")
            print(f"  {'-'*75}")
            for t in r["trade_log"]:
                ed = t["entry_dt"].strftime("%Y-%m-%d") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
                xd = t["exit_dt"].strftime("%Y-%m-%d")  if hasattr(t["exit_dt"],  "strftime") else str(t["exit_dt"])
                sign = "+" if t["pnl"] >= 0 else ""
                print(f"  {ed:<12} {xd:<12} {t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} {sign}{t['pnl']:>9,.0f} {t['pct']:>+6.2f}%  {t['reason']}")


# ── main ─────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="指値エントリー バックテスト (MACD / A7 / RSI2)"
    )
    parser.add_argument("symbol", nargs="?", help="特定銘柄コード（例: 7203.T）")
    parser.add_argument("--macd",       action="store_true", help="MACD戦略のみ")
    parser.add_argument("--a7",         action="store_true", help="A7戦略のみ")
    parser.add_argument("--rsi2",       action="store_true", help="RSI2戦略のみ")
    parser.add_argument("--days",       type=int, default=BACKTEST_DAYS, help="バックテスト日数")
    parser.add_argument("--top",        type=int, default=50,            help="上位N銘柄表示")
    parser.add_argument("--workers",    type=int, default=WORKERS,       help="並列数")
    parser.add_argument("--no-browser", action="store_true",             help="ブラウザを開かない")
    args = parser.parse_args()

    # どの戦略を実行するか
    run_macd = args.macd or (not args.macd and not args.a7 and not args.rsi2)
    run_a7   = args.a7   or (not args.macd and not args.a7 and not args.rsi2)
    run_rsi2 = args.rsi2 or (not args.macd and not args.a7 and not args.rsi2)

    # 特定銘柄モード
    if args.symbol:
        sym = args.symbol
        name_map = {s: n for s, n in SYMBOLS}
        name = name_map.get(sym, sym)
        sym_list = [(sym, name)]
    else:
        sym_list = SYMBOLS

    backtest_days = args.days
    workers       = args.workers

    # 戦略定義: (name, calc_fn, entry_atr_mult, stop_atr_mult, target_atr_mult)
    strategies = []
    if run_macd:
        # MACD: limit = close (entry_atr_mult=0), stop = limit - ATR*1.5, target = limit + ATR*3.0
        strategies.append(("MACD", calc_macd, 0.0, 1.5, 3.0))
    if run_a7:
        # A7: limit = close (entry_atr_mult=0), stop = limit - ATR*1.5, target = limit + ATR*3.0
        strategies.append(("A7", calc_a7, 0.0, 1.5, 3.0))
    if run_rsi2:
        # RSI2: limit = close - ATR*0.5, stop = limit - ATR*2.0, target = limit + ATR*4.0
        strategies.append(("RSI2", calc_rsi2, 0.5, 2.0, 4.0))

    all_results: dict[str, list[dict]] = {}

    for strat_name, calc_fn, entry_mult, stop_mult, target_mult in strategies:
        print(f"\n[{strat_name}] バックテスト開始... ({len(sym_list)}銘柄, {backtest_days}日)", flush=True)
        results = backtest_all_symbols(
            sym_list, calc_fn, entry_mult, stop_mult, target_mult,
            backtest_days, workers, strat_name,
        )
        all_results[strat_name] = results

        n_trades = sum(r["trades"] for r in results)
        n_wins   = sum(r["wins"]   for r in results)
        total_pnl = sum(r["total_pnl"] for r in results)
        print(f"  => 銘柄数: {len(results)}  取引数: {n_trades}  勝: {n_wins}  合計損益: {total_pnl:+,.0f}円")

    if not all_results:
        print("結果がありません")
        return

    # 特定銘柄モード: 端末に詳細表示
    if args.symbol:
        flat = [r for rs in all_results.values() for r in rs]
        print_single_symbol(args.symbol, flat)

    # HTML レポート生成
    html = build_html(all_results, backtest_days, args.top)
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    out_path  = Path(f"limit_entry_report_{today_str}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"\nレポート保存: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
