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
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import pickle
import webbrowser
from _open_html import open_html
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
FIXED_QTY     = 100         # 後方互換用 (廃止予定: calc_qty を使用)
TARGET_POSITION  = 1_000_000  # 目標ポジションサイズ (円)
MIN_QTY          = 200        # 最低取引株数
TRADE_LOT        = 100        # 単元株数
TRADE_MAX_PRICE  = 5_000.0    # 取引対象の最高株価 (WATCHLIST選定・WFスキャン時に使用)
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
# それ以上なら不約定となる。バックテストにも同じ条件を適用して実運用と整合。
# 0.03 (3%) は典型的な朝ギャップの 80% 程度をカバーしつつ、
# 5% 超の急騰(高値掴みリスク大)は除外できる実用的な値。
LIMIT_ENTRY_MARGIN_PCT = 0.03


# ── ボラ平準化サイジング (env var で切替, 既定は従来の100株固定) ─────
# VOL_PARITY=1 のとき、stop到達時の損失が RISK_PER_TRADE 円程度になる
# 単元株数を返す。低ボラ銘柄は株数を増やし、高ボラ銘柄は減らす(下限100株)。
# 既定(VOL_PARITY未設定)は従来通り FIXED_QTY=100 株固定なので過去CSVと比較可能。
VOL_PARITY       = os.getenv("VOL_PARITY", "0").lower() in ("1", "true", "yes", "on")
RISK_PER_TRADE   = float(os.getenv("RISK_PER_TRADE", "20000"))    # 1トレード目標リスク(円)
MAX_POSITION_YEN = float(os.getenv("MAX_POSITION_YEN", "1000000"))  # 1ポジション投入上限(円)


def calc_qty(order_price: float, stop_price: float | None = None) -> int:
    """株数決定。

    VOL_PARITY 有効かつ stop_price 指定時はボラ平準化:
      stop 到達時の 1株損失 = |order_price - stop_price| を使い、
      損失合計が RISK_PER_TRADE 円程度になる単元株数(100株単位)を返す。
      下限 100株 / 上限 MAX_QTY / 1ポジ投入額 MAX_POSITION_YEN でクリップ。
    無効時 or stop_price 未指定時は従来の FIXED_QTY (100株固定)。
    """
    if not VOL_PARITY or stop_price is None:
        return FIXED_QTY
    risk_per_share = abs(order_price - stop_price)
    if risk_per_share <= 0:
        return FIXED_QTY
    qty = int(round((RISK_PER_TRADE / risk_per_share) / TRADE_LOT) * TRADE_LOT)
    qty = max(TRADE_LOT, min(qty, MAX_QTY))
    # 予算上限: 1ポジションの投入額が MAX_POSITION_YEN を超えないよう丸める
    if order_price > 0 and order_price * qty > MAX_POSITION_YEN:
        qty = max(TRADE_LOT, int(MAX_POSITION_YEN / order_price / TRADE_LOT) * TRADE_LOT)
    return qty


# ── TSE 呼値 (tick size) 丸め ────────────────────────────────────
# 東証の呼値単位: https://www.jpx.co.jp/rules-participants/rules/tick-size/
_TICK_TABLE = [
    (3_000,     5),
    (5_000,     5),
    (30_000,   10),
    (50_000,   50),
    (300_000, 100),
    (500_000, 500),
    (3_000_000,   1_000),
    (5_000_000,   5_000),
    (float("inf"), 10_000),
]

def tick_size(price: float) -> int:
    """価格に対応する呼値単位を返す"""
    for threshold, tick in _TICK_TABLE:
        if price < threshold:
            return tick
    return 10_000

def round_to_tick(price: float) -> int:
    """TSE呼値単位に合わせて最近接値に丸める（発注価格エラー防止）"""
    t = tick_size(float(price))
    return int(round(price / t) * t)


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


def _trim_incomplete_bar(df: pd.DataFrame) -> pd.DataFrame:
    """未確定バーを末尾から除去する。

    yfinance は 15:00 JST より前 (場中) に叩くと「今日の未確定バー」を返すことが
    ある。その未確定バー (= 場中の暫定終値) をキャッシュに保存すると、引け後に
    実行しても暫定値が再利用され続けてしまう (現在値が場中の値で固定される)。

    `_expected_latest_bar_date()` より新しい日付のバーは「まだ確定していない」と
    みなして落とす。これにより:
      - 引け前実行: 今日の未確定バーを落とし、前営業日の確定終値までを使う
      - 引け後実行: 今日のバーは確定済みなので残る
    """
    if df is None or len(df) == 0:
        return df
    expected = _expected_latest_bar_date()
    idx_dates = df.index.date if hasattr(df.index, "date") else \
        [ix.date() if hasattr(ix, "date") else ix for ix in df.index]
    mask = [d <= expected for d in idx_dates]
    if all(mask):
        return df
    return df[mask].copy()


def fetch(symbol: str, backtest_days: int = BACKTEST_DAYS) -> pd.DataFrame | None:
    """永続キャッシュ優先・フォールバックでダウンロード。

    キャッシュ判定:
      - キャッシュ内 df の最新バー日付 >= _expected_latest_bar_date() なら有効
      - そうでなければ再取得 (引け前作成 → 引け後実行 のパターンも自動更新)
      - 未確定バー (15:00 JST 前の今日のバー) は読み込み時に除去する
    """
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            with open(persistent, "rb") as f:
                df = pickle.load(f)
            # 未確定バー (場中の今日のバー) を除去 → 前営業日確定終値までで判定
            df = _trim_incomplete_bar(df)
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
        # 未確定バー (場中の今日のバー) はキャッシュに保存しない
        # → 引け前に実行しても暫定終値が永続化されない
        df_out = _trim_incomplete_bar(df_out)
        if len(df_out) < 210:
            return None
        try:
            _CACHE_DIR.mkdir(exist_ok=True)
            with open(persistent, "wb") as f:
                pickle.dump(df_out, f)
        except Exception:
            pass
        return df_out
    except Exception:
        return None



def compute_period_result(item: dict, days: int) -> dict:
    """period_results から指定日数の結果を返す。PERIODS 外の日数は最大期間から動的計算。"""
    pr = item.get("period_results", {}).get(days)
    if pr is not None:
        return pr
    # days が PERIODS にない場合、最大期間の trade_log からスライスして計算
    all_days = [d for d in item.get("period_results", {}) if isinstance(d, int)]
    if not all_days:
        return {}
    max_d = max(all_days)
    if max_d < days:
        return {}
    base = item["period_results"][max_d]
    full_log = base.get("trade_log", [])
    today_d = datetime.now(JST).date()
    cutoff = today_d - timedelta(days=days)
    sub = [t for t in full_log if t["signal_dt"].date() >= cutoff]
    if not sub:
        return {}
    wins = sum(1 for t in sub if t["pnl"] > 0)
    gp   = sum(t["pnl"] for t in sub if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
    pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    n    = len(sub)
    return dict(
        symbol=item.get("symbol"), name=item.get("name"), strategy=item.get("strategy"),
        signals=base.get("signals", 0),
        filled=n, trades=n, wins=wins, losses=n - wins,
        win_rate=wins / n * 100,
        pf=pf,
        total_pnl=sum(t["pnl"] for t in sub),
        total_fee=sum(t.get("fee", 0) for t in sub),
        slippage_pct=base.get("slippage_pct", SLIPPAGE_STOP_PCT),
        fee_pct_one_way=base.get("fee_pct_one_way", FEE_PCT_ONE_WAY),
        avg_hold=sum(t["hold_days"] for t in sub) / n,
        fill_rate=base.get("fill_rate", 0),
        trade_log=sub,
    )


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


# ── 損切り評価モード ポリシー ───────────────────────────────────
# "intraday" = ザラ場の安値/高値が損切り価格にタッチで約定 (ヒゲでも発火)
# "close"    = 終値が損切り価格を超えたときだけ約定 (引け判定・引け成行)
# 運用方針 (2026-06, ストップ狩り実測 analyze_stop_hunt.py に基づく):
#   既定は close (ヒゲ刈りを回避し勝率/PF/総損益が改善)。
#   ただし MOM ロングのみ close で成績悪化するため intraday 据え置き。
def default_stop_mode(strategy_name: str, is_short: bool) -> str:
    if strategy_name == "MOM" and not is_short:
        return "intraday"
    return "close"


# ── 戦略別 最大保有日数 (改善④) ─────────────────────────────────
# 戦略の性質に応じてタイムカット日数を変える。
#   RSI2 (売られすぎ反発): 反発は数日で完結 → 短く保有してタイムカット損失を減らす
#   MOM  (モメンタム継続) : トレンドが伸びる → 長く保有して利を伸ばす
#   その他               : 現状維持 (MAX_HOLD)
# 環境変数 MAX_HOLD_OVERRIDE で全戦略一括上書き可 (検証用)。
_MAX_HOLD_BY_STRATEGY = {
    "RSI2": 7,
    "MOM":  20,
}


def default_max_hold(strategy_name: str) -> int:
    ovr = os.getenv("MAX_HOLD_OVERRIDE")
    if ovr:
        try:
            return int(ovr)
        except ValueError:
            pass
    return _MAX_HOLD_BY_STRATEGY.get((strategy_name or "").upper(), MAX_HOLD)


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
    entry_risk_adjust: bool = False,  # True=ギャップ約定時に sp/tp を ep ベースに再計算 (R:R 維持)
    stop_mode: str | None = None,  # "intraday"/"close"。None=default_stop_mode で自動決定
    max_hold: int | None = None,   # 最大保有日数。None=default_max_hold(戦略別)で自動決定
    entry_delay: int = 0,          # シグナル発生から何日後から注文を受け付けるか
) -> dict:
    """
    指値 or 逆指値エントリー + OCO決済 バックテスト。

    entry_type="limit"    : 安値 <= order_price で約定（押し目買い）
    entry_type="stop"     : 高値 >= order_price で約定（ブレイクアウト買い）
    entry_type="stop_sell": 安値 <= order_price で約定（逆指値売り・空売り）
                            order_p = close - atr*em, stop (損切り) = order_p + atr*sm (上方)
                            target (利確) = order_p - atr*tm (下方), PnL=(entry-exit)*qty

    entry_risk_adjust=True の場合:
      逆指値(stop)でギャップアップ約定 (ep > lp) 時、
      sp / tp を (ep - lp) だけシフトして R:R を維持する。
      例: lp=1000, ep=1030, sp=850, tp=1300
        → sp_adj=880, tp_adj=1330 (損切幅・利確幅ともに atr*sm / atr*tm を維持)
      False(デフォルト)は既存動作と同一。

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
    signals  = 0
    is_short = (entry_type == "stop_sell")
    if stop_mode is None:
        stop_mode = default_stop_mode(strategy_name, is_short)
    if max_hold is None:
        max_hold = default_max_hold(strategy_name)

    # 複数ポジション並行対応: pending / active をリストで管理
    pending_orders:   list[dict] = []   # 発注待ち
    active_positions: list[dict] = []   # 保有中

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]

        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        atr_prev = float(prev.get("atr", np.nan))
        if pd.isna(atr_prev) or atr_prev <= 0:
            continue

        # ── 1. 新シグナル → pending に追加 (既存ポジション数に依存しない) ──
        # 実運用セマンティクス: シグナル日 T の引け後に逆指値注文を置き、
        # T+1 の始値から発動可能 → バックテストも同じ T+1 の hi/lo で判定。
        entry_sig = bool(prev.get("entry_sig", False))
        if entry_sig:
            close_prev = float(prev["close"])
            if MIN_PRICE <= close_prev <= MAX_PRICE and atr_prev / close_prev <= MAX_ATR_RATIO:
                if entry_type == "stop":
                    lp = close_prev + atr_prev * entry_atr_mult
                    sp = lp - atr_prev * stop_atr_mult
                    tp = lp + atr_prev * target_atr_mult
                    valid = lp > 0 and sp > 0 and tp > lp
                elif entry_type == "stop_sell":
                    lp = close_prev - atr_prev * entry_atr_mult
                    sp = lp + atr_prev * stop_atr_mult
                    tp = lp - atr_prev * target_atr_mult
                    valid = lp > 0 and tp > 0 and tp < lp and sp > lp
                else:
                    lp = close_prev - atr_prev * entry_atr_mult
                    sp = lp - atr_prev * stop_atr_mult
                    tp = lp + atr_prev * target_atr_mult
                    valid = lp > 0 and sp > 0 and tp > lp
                if valid:
                    signals += 1
                    pending_orders.append({
                        "lp": lp, "sp": sp, "tp": tp,
                        "qty": calc_qty(lp, sp),
                        "expire_idx":   i + ENTRY_EXPIRE,
                        "fill_start_idx": i + entry_delay,
                        "signal_idx":   i,
                        "signal_dt":    df.index[i - 1],
                        "signal_price": close_prev,
                    })

        # ── 2. pending orders: 約定チェック (新規追加分も同バーで判定) ──
        remaining_pending: list[dict] = []
        new_active:        list[dict] = []

        for po in pending_orders:
            if i > po["expire_idx"]:
                continue  # 期限切れ → 破棄
            if i < po.get("fill_start_idx", 0):  # 遅延期間中はスキップ
                remaining_pending.append(po)
                continue

            triggered = (
                (entry_type == "stop"      and hi >= po["lp"]) or
                (entry_type == "stop_sell" and lo <= po["lp"]) or
                (entry_type == "limit"     and lo <= po["lp"])
            )
            if not triggered:
                remaining_pending.append(po)
                continue

            # 約定価格計算 (整数円に丸める: 表示と計算の一致)
            fill_type = "normal"
            if entry_type == "stop":
                limit_upper = po["lp"] * (1.0 + LIMIT_ENTRY_MARGIN_PCT)
                if op >= po["lp"]:
                    if op > limit_upper:
                        # ギャップアップ超過: 日中に指値上限以下に戻れば指値上限で約定
                        if lo <= limit_upper:
                            ep = round(limit_upper)  # 戻り約定
                            fill_type = "gap_comeback"
                        else:
                            continue  # 終日 limit_upper を下回らず → 不約定
                    else:
                        ep = round(op)
                        fill_type = "gap_open"
                else:
                    ep = round(po["lp"] * (1.0 + SLIPPAGE_STOP_PCT))
            elif entry_type == "stop_sell":
                limit_lower = po["lp"] * (1.0 - LIMIT_ENTRY_MARGIN_PCT)
                if op <= po["lp"]:  # ギャップダウン（寄り付きがトリガー以下）
                    if op < limit_lower:
                        # ギャップダウン超過: 日中に指値下限以上に戻れば指値下限で約定
                        if hi >= limit_lower:
                            ep = round(limit_lower)
                            fill_type = "gap_comeback"
                        else:
                            continue  # 終日 limit_lower を上回らず → 不約定
                    else:
                        ep = round(op)
                        fill_type = "gap_open"
                else:
                    ep = round(po["lp"] * (1.0 - SLIPPAGE_STOP_PCT))
            else:
                ep = round(po["lp"] * (1.0 + SLIPPAGE_LIMIT_PCT))

            # entry_risk_adjust: ギャップ約定時に sp/tp を ep ベースに再計算して R:R を維持
            use_sp = po["sp"]
            use_tp = po["tp"]
            if entry_risk_adjust and entry_type == "stop" and ep > po["lp"]:
                delta  = ep - po["lp"]
                use_sp = po["sp"] + delta
                use_tp = po["tp"] + delta
            elif entry_risk_adjust and entry_type == "stop_sell" and ep < po["lp"]:
                delta  = po["lp"] - ep
                use_sp = po["sp"] - delta
                use_tp = po["tp"] - delta

            dtf = i - po["signal_idx"]

            # 約定と同日の決済チェック (target は常にザラ場指値、stop は stop_mode 依存)
            hit_tgt = (hi >= use_tp) if not is_short else (lo <= use_tp)
            if stop_mode == "close":
                hit_stp = (cl <= use_sp) if not is_short else (cl >= use_sp)
            else:
                hit_stp = (lo <= use_sp) if not is_short else (hi >= use_sp)

            if hit_tgt or hit_stp:
                xp      = use_tp if hit_tgt else use_sp
                xreason = "目標達成" if hit_tgt else "損切り"
                if xreason == "損切り":
                    if stop_mode == "close":
                        # 引け成行: 終値にスリッページ
                        xp = cl * (1.0 + SLIPPAGE_STOP_PCT) if is_short else cl * (1.0 - SLIPPAGE_STOP_PCT)
                    elif is_short:
                        xp = op if op >= use_sp else use_sp * (1.0 + SLIPPAGE_STOP_PCT)
                    else:
                        xp = op if op <= use_sp else use_sp * (1.0 - SLIPPAGE_STOP_PCT)
                if ep * 0.1 <= xp <= ep * 10.0:
                    _qty = po["qty"]
                    fee = (ep + xp) * _qty * FEE_PCT_ONE_WAY
                    if is_short:
                        pnl = (ep - xp) * _qty - fee
                        pct = (ep - xp) / ep * 100
                    else:
                        pnl = (xp - ep) * _qty - fee
                        pct = (xp - ep) / ep * 100
                    trades.append(dict(
                        entry_dt=dt, exit_dt=dt,
                        entry_p=ep, exit_p=xp, qty=_qty,
                        pnl=pnl, pct=pct, fee=round(fee, 0),
                        hold_days=0, days_to_fill=dtf,
                        signal_dt=po["signal_dt"], signal_price=po["signal_price"],
                        order_limit=po["lp"], order_stop=po["sp"], order_target=po["tp"],
                        fill_type=fill_type, reason=xreason,
                        # MAE=最悪含み損 / MFE=最大含み益 を方向対応で計算
                        # (ショートは株価上昇=含み損なので high/low を反転)
                        mae_pct=round(((ep - hi) if is_short else (lo - ep)) / ep * 100, 2),
                        mfe_pct=round(((ep - lo) if is_short else (hi - ep)) / ep * 100, 2),
                        days_neg=0,
                    ))
                # 同日決済: active に入れない
            else:
                # 当日決済なし → active に追加
                new_active.append({
                    "entry_dt": dt, "entry_p": ep,
                    "sp": use_sp, "tp": use_tp,
                    "qty": po["qty"],
                    "fill_type": fill_type,
                    "hold_start": i, "days_to_fill": dtf,
                    "signal_dt":    po["signal_dt"],
                    "signal_price": po["signal_price"],
                    "order_limit":  po["lp"],
                    "order_stop":   po["sp"],
                    "order_target": po["tp"],
                    "min_lo": lo, "max_hi": hi, "days_neg": 0,
                })

        pending_orders = remaining_pending
        active_positions.extend(new_active)

        # ── 3. active positions: OCO決済チェック ──
        still_active: list[dict] = []
        for pos in active_positions:
            ep        = pos["entry_p"]
            hold_days = i - pos["hold_start"]

            hit_tgt = (hi >= pos["tp"]) if not is_short else (lo <= pos["tp"])
            if stop_mode == "close":
                hit_stp = (cl <= pos["sp"]) if not is_short else (cl >= pos["sp"])
            else:
                hit_stp = (lo <= pos["sp"]) if not is_short else (hi >= pos["sp"])

            exit_p_pos    = None
            exit_reason_pos = None

            if hit_tgt and hit_stp:
                exit_p_pos = pos["tp"]; exit_reason_pos = "目標達成"
            elif hit_tgt:
                exit_p_pos = pos["tp"]; exit_reason_pos = "目標達成"
            elif hit_stp:
                exit_p_pos = pos["sp"]; exit_reason_pos = "損切り"
            elif hold_days >= max_hold:
                exit_p_pos = cl;        exit_reason_pos = "タイムカット"

            if exit_p_pos is None:
                pos["min_lo"] = min(pos.get("min_lo", lo), lo)
                pos["max_hi"] = max(pos.get("max_hi", hi), hi)
                # 含み損日数: ロングは終値<約定、ショートは終値>約定で含み損
                underwater = (cl > pos["entry_p"]) if is_short else (cl < pos["entry_p"])
                if underwater:
                    pos["days_neg"] = pos.get("days_neg", 0) + 1
                still_active.append(pos)
                continue

            if not (ep * 0.1 <= exit_p_pos <= ep * 10.0):
                continue  # 異常価格 → ポジション破棄

            if exit_reason_pos == "損切り":
                if stop_mode == "close":
                    # 引け成行: 終値にスリッページ
                    exit_p_pos = cl * (1.0 + SLIPPAGE_STOP_PCT) if is_short else cl * (1.0 - SLIPPAGE_STOP_PCT)
                elif is_short:
                    exit_p_pos = op if op >= pos["sp"] else pos["sp"] * (1.0 + SLIPPAGE_STOP_PCT)
                else:
                    exit_p_pos = op if op <= pos["sp"] else pos["sp"] * (1.0 - SLIPPAGE_STOP_PCT)

            _qty = pos["qty"]
            fee = (ep + exit_p_pos) * _qty * FEE_PCT_ONE_WAY
            if is_short:
                pnl = (ep - exit_p_pos) * _qty - fee
                pct = (ep - exit_p_pos) / ep * 100
            else:
                pnl = (exit_p_pos - ep) * _qty - fee
                pct = (exit_p_pos - ep) / ep * 100
            _fin_min = min(pos.get("min_lo", lo), lo)
            _fin_max = max(pos.get("max_hi", hi), hi)
            trades.append(dict(
                entry_dt=pos["entry_dt"], exit_dt=dt,
                entry_p=ep, exit_p=exit_p_pos, qty=_qty,
                pnl=pnl, pct=pct, fee=round(fee, 0),
                hold_days=hold_days, days_to_fill=pos["days_to_fill"],
                signal_dt=pos["signal_dt"], signal_price=pos["signal_price"],
                order_limit=pos["order_limit"], order_stop=pos["order_stop"],
                order_target=pos["order_target"],
                fill_type=pos.get("fill_type", "normal"),
                reason=exit_reason_pos,
                # MAE=最悪含み損 / MFE=最大含み益 を方向対応で計算
                # (ショートは株価上昇=含み損なので min/max を反転)
                mae_pct=round(((ep - _fin_max) if is_short else (_fin_min - ep)) / ep * 100, 2),
                mfe_pct=round(((ep - _fin_min) if is_short else (_fin_max - ep)) / ep * 100, 2),
                days_neg=pos.get("days_neg", 0),
            ))

        active_positions = still_active

    # ループ終端: 最終バーのシグナルは次バー(T+1)が存在しないため未処理→ pending に追加
    if len(df) >= 1:
        last_bar = df.iloc[-1]
        last_entry_sig = bool(last_bar.get("entry_sig", False))
        last_atr = float(last_bar.get("atr", np.nan))
        last_cl  = float(last_bar["close"])
        if last_entry_sig and not pd.isna(last_atr) and last_atr > 0:
            if MIN_PRICE <= last_cl <= MAX_PRICE and last_atr / last_cl <= MAX_ATR_RATIO:
                if entry_type == "stop":
                    lp = last_cl + last_atr * entry_atr_mult
                    sp = lp - last_atr * stop_atr_mult
                    tp = lp + last_atr * target_atr_mult
                    valid = lp > 0 and sp > 0 and tp > lp
                elif entry_type == "stop_sell":
                    lp = last_cl - last_atr * entry_atr_mult
                    sp = lp + last_atr * stop_atr_mult
                    tp = lp - last_atr * target_atr_mult
                    valid = lp > 0 and tp > 0 and tp < lp and sp > lp
                else:
                    lp = last_cl - last_atr * entry_atr_mult
                    sp = lp - last_atr * stop_atr_mult
                    tp = lp + last_atr * target_atr_mult
                    valid = lp > 0 and sp > 0 and tp > lp
                if valid:
                    signals += 1
                    pending_orders.append({
                        "lp": lp, "sp": sp, "tp": tp,
                        "qty": calc_qty(lp, sp),
                        "expire_idx":   len(df) + ENTRY_EXPIRE,
                        "signal_idx":   len(df),
                        "signal_dt":    df.index[-1],
                        "signal_price": last_cl,
                    })

    # 未決済ポジションを「保有中」として記録 (手数料は含めない: 未実現損益なので)
    cl_last = float(df.iloc[-1]["close"])
    for pos in active_positions:
        ep        = pos["entry_p"]
        _qty      = pos["qty"]
        hold_days = len(df) - 1 - pos["hold_start"]
        if is_short:
            pnl = (ep - cl_last) * _qty
            pct = (ep - cl_last) / ep * 100
        else:
            pnl = (cl_last - ep) * _qty
            pct = (cl_last - ep) / ep * 100
        trades.append(dict(
            entry_dt=pos["entry_dt"], exit_dt=df.index[-1],
            entry_p=ep, exit_p=cl_last, qty=_qty,
            pnl=pnl, pct=pct, fee=0.0,
            hold_days=hold_days, days_to_fill=pos["days_to_fill"],
            signal_dt=pos["signal_dt"], signal_price=pos["signal_price"],
            order_limit=pos["order_limit"], order_stop=pos["order_stop"],
            order_target=pos["order_target"],
            fill_type=pos.get("fill_type", "normal"),
            reason="保有中",
        ))

    # 未発動 pending_orders を「発注中」として記録 (損益・勝率計算は除外)
    for po in pending_orders:
        trades.append(dict(
            entry_dt=df.index[-1], exit_dt=df.index[-1],
            entry_p=po["lp"], exit_p=cl_last, qty=po["qty"],
            pnl=0.0, pct=0.0, fee=0.0,
            hold_days=0, days_to_fill=0,
            signal_dt=po["signal_dt"], signal_price=po["signal_price"],
            order_limit=po["lp"], order_stop=po["sp"], order_target=po["tp"],
            reason="発注中",
        ))

    # 統計計算 (発注中は除外)
    stat_trades = [t for t in trades if t.get("reason") != "発注中"]
    filled  = len(stat_trades)
    wins    = sum(1 for t in stat_trades if t["pnl"] > 0)
    losses  = sum(1 for t in stat_trades if t["pnl"] <= 0)
    win_rate = wins / filled * 100 if filled > 0 else 0.0
    gross_profit = sum(t["pnl"] for t in stat_trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in stat_trades if t["pnl"] < 0))
    pf           = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    total_pnl    = sum(t["pnl"] for t in stat_trades)
    total_fee    = sum(t.get("fee", 0) for t in stat_trades)
    avg_hold     = sum(t["hold_days"] for t in stat_trades) / filled if filled > 0 else 0.0
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
        open_html(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
