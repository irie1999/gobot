"""
Adaptive Mean Reversion Backtest for Japanese Equities
=======================================================
アダプティブ平均回帰バックテスト（IBS + RSI(2) + ボラティリティレジーム）

学術研究に基づくアルゴリズム設計:
  1. arXiv 2023 IBS論文: IBS < 0.15 で勝率75-78%、1日保有が最適
  2. Connors RSI(2): RSI(2) < 5 の方が < 10 より高精度、5日SMAクロスで決済
  3. ScienceDirect 2023 レジーム切替: ATRが上位30パーセンタイル時はサイズ50%縮小
  4. Stanford LOB論文: ボラティリティレジームに応じたアダプティブATRオフセット
  5. Lipton/Lopez de Prado: 固定比率ではなく2.0 ATRでストップ（高ボラ時は2.5 ATR）

シグナル（OR条件）: IBS < 0.15 OR RSI(2) < 5、両方とも終値 > MA200が必要
エントリー: 終値 - ATR * regime_mult（低ボラ: 0.5、通常: 1.0、高ボラ: 1.5）の指値
  レジームはATRの直近90日パーセンタイルランクで決定
エントリー有効期限: 2日
決済（シグナルベース）:
  * 翌日IBSが0.75超（反転確認）
  * 翌日RSI(2)が75超（平均回帰完了）
  * 終値が5日SMAを上抜け
  * ハードストップ: エントリー - 2.0 * ATR（高ボラ時は2.5 ATR）
  * 最大保有: 7日後に終値で強制決済
ポジションサイズ: ATR < 70パーセンタイル時はフルサイズ、>=70パーセンタイル時はハーフサイズ

使い方:
  python backtest_adaptive_mr.py                    # 監視銘柄スキャン（1年）
  python backtest_adaptive_mr.py --universe all     # 日経225全銘柄スキャン
  python backtest_adaptive_mr.py --symbol 7011.T    # 1銘柄詳細
  python backtest_adaptive_mr.py --days 180         # 180日バックテスト
  python backtest_adaptive_mr.py --no-browser       # ブラウザ自動起動なし
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
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── 定数 ─────────────────────────────────────────────────────
WORKERS        = 4
LOT_SIZE       = 100       # 1回あたり株数（固定100株）
BACKTEST_DAYS  = 365       # デフォルトのバックテスト日数
JST            = timezone(timedelta(hours=9))
_TODAY         = pd.Timestamp(datetime.now(tz=JST).date())
_CACHE_DIR     = Path(".rsi2_cache")
_BT_CACHE      = _CACHE_DIR / "bt_results_adaptive_mr.pkl"

# レジーム判定パラメーター
ATR_LOOKBACK   = 90    # ATRパーセンタイル算出期間（日）
ATR_HIGH_PCT   = 70    # 高ボラ判定パーセンタイル（>=70 → ハーフサイズ）
ATR_REGIME_LO  = 33    # 低ボラ判定パーセンタイル（<33 → 低ボラレジーム）
ATR_REGIME_HI  = 67    # 高ボラ判定パーセンタイル（>=67 → 高ボラレジーム）

# エントリー・決済パラメーター
IBS_ENTRY      = 0.15   # IBSエントリー閾値（arXiv 2023）
RSI2_ENTRY     = 5.0    # RSI(2)エントリー閾値（Connors）
IBS_EXIT       = 0.75   # IBS決済閾値
RSI2_EXIT      = 75.0   # RSI(2)決済閾値
STOP_ATR_NORM  = 2.0    # 通常時ストップ（ATR倍率）
STOP_ATR_HIGH  = 2.5    # 高ボラ時ストップ（ATR倍率）
TARGET_ATR_MULT = 2.0   # 利確目標（ATR倍率）
ENTRY_EXPIRE   = 2      # エントリー指値の有効期限（日数）
MAX_HOLD       = 7      # 最大保有日数
SMA_PERIOD     = 5      # 決済用SMA期間

# レジーム別エントリーATRオフセット倍率
REGIME_MULT = {"low": 0.5, "normal": 1.0, "high": 1.5}


# ── 銘柄ユニバース ────────────────────────────────────────────
from symbols_watch_rsi2 import SYMBOLS as _WATCH_SYMBOLS

try:
    from rsi2 import SYMBOLS as _ALL_SYMBOLS
except ImportError:
    _ALL_SYMBOLS = _WATCH_SYMBOLS


def _load_symbols(universe: str) -> list[tuple[str, str]]:
    """ユニバース名に応じて銘柄リストを返す。
    watch : 監視銘柄（symbols_watch_rsi2.py）
    225   : 日経225（symbols_all.py）
    all   : 全上場銘柄（symbols_listed_all.py、なければ日経225）
    """
    if universe == "225":
        from symbols_all import SYMBOLS as _S225
        return list(_S225)
    if universe == "all":
        _p = Path("symbols_listed_all.py")
        if _p.exists():
            import importlib.util
            _spec = importlib.util.spec_from_file_location("_listed_all", _p)
            _mod  = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            return list(_mod.SYMBOLS)
        print("  ※ symbols_listed_all.py が見つかりません。日経225を使用します。")
        from symbols_all import SYMBOLS as _S225
        return list(_S225)
    return list(_WATCH_SYMBOLS)


# ── データ取得 ────────────────────────────────────────────────
def fetch(symbol: str, backtest_days: int) -> "pd.DataFrame | None":
    """永続キャッシュ優先（.rsi2_cache/*.pkl）・フォールバックでyfinanceダウンロード。"""
    _CACHE_DIR.mkdir(exist_ok=True)

    # ── 永続キャッシュ確認 ──────────────────────────────────
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            _mtime = datetime.fromtimestamp(persistent.stat().st_mtime, tz=JST)
            if _mtime.date() == datetime.now(JST).date():
                with open(persistent, "rb") as f:
                    df = pickle.load(f)
                if len(df) >= 210:
                    return df
        except Exception:
            pass

    # ── フォールバック: yfinanceダウンロード ──────────────────
    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)
    now_jst   = datetime.now(JST)
    dl_start  = (now_jst - timedelta(days=total_cal)).strftime("%Y-%m-%d")
    dl_end    = (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.Ticker(symbol).history(
            start=dl_start, end=dl_end, interval="1d",
            auto_adjust=False, actions=False
        )
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        raw  = raw[cols]
        # 最終行のCloseがNaN・出来高あり → fast_infoで補完
        if len(raw) > 0:
            _last = raw.iloc[-1]
            if pd.isna(_last.get("close", float("nan"))) and _last.get("volume", 0) > 0:
                try:
                    _lp = yf.Ticker(symbol).fast_info.last_price
                    if _lp and not pd.isna(_lp):
                        raw.at[raw.index[-1], "close"] = float(_lp)
                except Exception:
                    pass
        raw = raw.dropna(subset=["close"])
        if len(raw) < 210:
            return None
        df_out = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw.get("volume", pd.Series(0, index=raw.index)).to_numpy(dtype=float),
        }, index=raw.index)
        try:
            with open(persistent, "wb") as f:
                pickle.dump(df_out, f)
        except Exception:
            pass
        return df_out
    except Exception:
        return None


# ── 指標計算 ──────────────────────────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    """
    RSI(2)、MA200、ATR14、IBS、5日SMA、ATR90日パーセンタイルランクを計算する。

    追加列:
      rsi2      : RSI period=2 (Wilder smoothing)
      ma200     : 200日移動平均
      atr       : ATR period=14 (Wilder smoothing)
      ibs       : Internal Bar Strength = (close - low) / (high - low)
      sma5      : 5日単純移動平均
      atr_pct   : ATR の直近90日パーセンタイルランク（0-100）
      regime    : ATRパーセンタイルに基づくレジーム ("low"/"normal"/"high")
    """
    df  = df.copy()
    c   = df["close"]
    h   = df["high"]
    l   = df["low"]
    prev = c.shift(1)

    # RSI(2) — Wilder smoothing (alpha = 1/2, com=1)
    d    = c.diff()
    gain = d.clip(lower=0).ewm(com=1, adjust=False).mean()
    loss = (-d).clip(lower=0).ewm(com=1, adjust=False).mean()
    df["rsi2"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # MA200
    df["ma200"] = c.rolling(200).mean()

    # ATR14 — Wilder smoothing (alpha = 1/14, com=13)
    tr  = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13, adjust=False).mean()

    # IBS = (close - low) / (high - low)
    bar_range    = (h - l).replace(0, np.nan)
    df["ibs"]    = (c - l) / bar_range

    # 5日SMA
    df["sma5"] = c.rolling(SMA_PERIOD).mean()

    # ATRパーセンタイルランク（直近90日）
    atr_arr = df["atr"].to_numpy(dtype=float)
    pct_arr = np.full(len(atr_arr), np.nan)
    for i in range(len(atr_arr)):
        if i < ATR_LOOKBACK - 1:
            continue
        window = atr_arr[i - ATR_LOOKBACK + 1 : i + 1]
        valid  = window[~np.isnan(window)]
        if len(valid) < 2:
            continue
        pct_arr[i] = float(np.sum(valid <= atr_arr[i]) / len(valid) * 100)
    df["atr_pct"] = pct_arr

    # レジームラベル
    def _regime(pct: float) -> str:
        if np.isnan(pct):
            return "normal"
        if pct < ATR_REGIME_LO:
            return "low"
        if pct >= ATR_REGIME_HI:
            return "high"
        return "normal"

    df["regime"] = df["atr_pct"].apply(_regime)
    return df


# ── 今日のシグナル判定 ────────────────────────────────────────
def _has_signal_today(df: pd.DataFrame) -> dict | None:
    """
    最新バーのシグナルを判定する。
    シグナルあり → dict（symbol情報以外の指標値・エントリー条件を含む）を返す。
    シグナルなし → None を返す。
    """
    if len(df) < 2:
        return None
    last = df.iloc[-1]

    close  = float(last["close"])
    rsi2   = last.get("rsi2",  float("nan"))
    ibs    = last.get("ibs",   float("nan"))
    ma200  = last.get("ma200", float("nan"))
    atr    = last.get("atr",   float("nan"))
    regime = last.get("regime", "normal")

    if pd.isna(rsi2) or pd.isna(ma200) or pd.isna(atr):
        return None
    if close <= ma200:
        return None

    sig_ibs  = (not pd.isna(ibs)) and (ibs < IBS_ENTRY)
    sig_rsi2 = (not pd.isna(rsi2)) and (rsi2 < RSI2_ENTRY)
    if not (sig_ibs or sig_rsi2):
        return None

    mult        = REGIME_MULT.get(str(regime), 1.0)
    limit_price = close - atr * mult
    stop_mult   = STOP_ATR_HIGH if regime == "high" else STOP_ATR_NORM
    stop_loss   = limit_price - atr * stop_mult

    return {
        "close":       close,
        "rsi2":        float(rsi2),
        "ibs":         float(ibs) if not pd.isna(ibs) else float("nan"),
        "ma200":       float(ma200),
        "atr":         float(atr),
        "atr_pct":     float(last.get("atr_pct", float("nan"))),
        "regime":      str(regime),
        "limit_price": limit_price,
        "stop_loss":   stop_loss,
        "expire_days": ENTRY_EXPIRE,
        "sig_ibs":     sig_ibs,
        "sig_rsi2":    sig_rsi2,
    }


# ── アダプティブMRバックテスト ────────────────────────────────
def backtest_amr(df: pd.DataFrame, backtest_days: int) -> list[dict]:
    """
    アダプティブ平均回帰バックテスト。

    エントリー条件（OR）:
      - IBS < 0.15 かつ終値 > MA200
      - RSI(2) < 5 かつ終値 > MA200
    エントリー: 指値 = 終値 - ATR * regime_mult（2日以内に未約定なら失効）
    決済条件:
      - 翌日IBS > 0.75（反転確認）
      - 翌日RSI(2) > 75（平均回帰完了）
      - 終値が5日SMAを上抜け
      - ハードストップ: エントリー価格 - 2.0 * ATR（高ボラ時 2.5 ATR）
      - 最大保有7日（終値で強制決済）
    """
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()
    if len(df) < 3:
        return []

    trades: list[dict] = []

    # ポジション状態
    in_pos      = False
    entry_p     = 0.0
    entry_dt    = None
    entry_atr   = 0.0
    entry_regime = "normal"
    qty         = 0
    hold_days   = 0

    # 未約定注文状態
    pending_order = None   # dict or None: {limit_price, qty, placed_dt, expire_after}

    for i in range(1, len(df)):
        row    = df.iloc[i]
        prev   = df.iloc[i - 1]
        dt     = df.index[i]

        op     = float(row["open"])
        hi     = float(row["high"])
        lo     = float(row["low"])
        cl     = float(row["close"])

        prev_rsi2   = prev.get("rsi2",   float("nan"))
        prev_ibs    = prev.get("ibs",    float("nan"))
        prev_ma200  = prev.get("ma200",  float("nan"))
        prev_atr    = prev.get("atr",    float("nan"))
        prev_sma5   = prev.get("sma5",   float("nan"))
        prev_regime = prev.get("regime", "normal")
        prev_close  = float(prev["close"])
        cur_ibs     = row.get("ibs",  float("nan"))
        cur_rsi2    = row.get("rsi2", float("nan"))
        cur_sma5    = row.get("sma5", float("nan"))

        if pd.isna(prev_ma200) or pd.isna(prev_atr):
            continue

        # ── ポジションあり: 決済判定 ──────────────────────────
        if in_pos:
            hold_days = (dt - entry_dt).days
            exit_p    = None
            reason    = None

            stop_mult = STOP_ATR_HIGH if entry_regime == "high" else STOP_ATR_NORM
            stop_lvl  = entry_p - entry_atr * stop_mult

            # 利確指値（当日ハイが目標到達） → exit_p = target で一致
            if hi >= entry_target:
                exit_p = entry_target
                reason = "利確"

            # 損切り逆指値（当日ローがストップ以下）
            elif lo <= stop_lvl:
                exit_p = max(stop_lvl, lo)   # ギャップダウン考慮
                reason = "損切り"

            # 最大保有日数（強制決済）
            elif hold_days >= MAX_HOLD:
                exit_p = cl
                reason = f"強制決済({MAX_HOLD}日)"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt     = entry_dt,
                    exit_dt      = dt,
                    entry_p      = entry_p,
                    exit_p       = exit_p,
                    qty          = qty,
                    pnl          = pnl,
                    pct          = pct,
                    hold         = hold_days,
                    reason       = reason,
                    limit_price  = entry_p,
                    atr          = entry_atr,
                    regime       = entry_regime,
                    stop_p       = stop_lvl,
                    target_p     = entry_target,
                    signal_dt    = signal_dt,
                    signal_close = signal_close,
                ))
                in_pos = False
                pending_order = None
                continue

        # ── 未約定注文: フィル判定 ────────────────────────────
        if pending_order is not None and not in_pos:
            age = (dt - pending_order["placed_dt"]).days
            if age > pending_order["expire_after"]:
                # 期限切れ
                pending_order = None
            else:
                # 当日ローが指値以下 → 約定
                if lo <= pending_order["limit_price"]:
                    entry_p       = pending_order["limit_price"]
                    entry_atr     = pending_order["atr"]
                    entry_regime  = pending_order["regime"]
                    qty           = pending_order["qty"]
                    entry_dt      = dt
                    hold_days     = 0
                    signal_dt     = pending_order["signal_dt"]
                    signal_close  = pending_order["signal_close"]
                    entry_target  = entry_p + entry_atr * TARGET_ATR_MULT
                    in_pos        = True
                    pending_order = None

        # ── ポジションなし・注文なし: エントリーシグナル判定 ─
        if not in_pos and pending_order is None:
            if pd.isna(prev_rsi2) or pd.isna(prev_ibs):
                continue
            if prev_close <= prev_ma200:
                continue

            sig_ibs  = (not pd.isna(prev_ibs))  and (prev_ibs  < IBS_ENTRY)
            sig_rsi2 = (not pd.isna(prev_rsi2)) and (prev_rsi2 < RSI2_ENTRY)
            if not (sig_ibs or sig_rsi2):
                continue

            regime_str = str(prev_regime) if not pd.isna(prev_regime) else "normal"
            mult       = REGIME_MULT.get(regime_str, 1.0)
            lim_price  = prev_close - prev_atr * mult

            # ポジションサイズ: ATR >= 70パーセンタイル → ハーフサイズ
            prev_atr_pct = prev.get("atr_pct", float("nan"))
            size_mult    = 0.5 if (not pd.isna(prev_atr_pct) and prev_atr_pct >= ATR_HIGH_PCT) else 1.0
            q            = LOT_SIZE

            pending_order = {
                "limit_price":  lim_price,
                "qty":          q,
                "placed_dt":    dt,
                "expire_after": ENTRY_EXPIRE,
                "atr":          prev_atr,
                "regime":       regime_str,
                "signal_dt":    prev.name,
                "signal_close": float(prev["close"]),
            }

    # 保有中ポジションを評価額で記録
    if in_pos:
        last_cl = float(df.iloc[-1]["close"])
        pnl     = (last_cl - entry_p) * qty
        pct     = (last_cl - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt     = entry_dt,
            exit_dt      = df.index[-1],
            entry_p      = entry_p,
            exit_p       = last_cl,
            qty          = qty,
            pnl          = pnl,
            pct          = pct,
            hold         = (df.index[-1] - entry_dt).days,
            reason       = "保有中★",
            limit_price  = entry_p,
            atr          = entry_atr,
            regime       = entry_regime,
            stop_p       = entry_p - entry_atr * (STOP_ATR_HIGH if entry_regime == "high" else STOP_ATR_NORM),
            target_p     = entry_target,
            signal_dt    = signal_dt,
            signal_close = signal_close,
        ))

    return trades


# ── HTML レポート生成 ─────────────────────────────────────────
def build_html(
    signals: list[dict],
    scan_results: list[dict],
    backtest_days: int,
    universe: str,
) -> Path:
    """
    HTMLレポートを生成する。

    引数:
      signals       : _has_signal_today() が返したシグナルのリスト（symbol, name を追加済み）
      scan_results  : スキャン結果（symbol, name, trades, win_rate, pf, total, total_pct を含む）
      backtest_days : バックテスト日数
      universe      : "watch" または "all"
    """
    today_str = _TODAY.strftime("%Y-%m-%d")
    since_str = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    gen_time  = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # ── 今日のシグナルテーブル ───────────────────────────────
    signal_rows = ""
    for s in signals:
        sym    = s.get("symbol", "")
        name   = s.get("name", "")
        close  = s.get("close",       float("nan"))
        rsi2   = s.get("rsi2",        float("nan"))
        ibs    = s.get("ibs",         float("nan"))
        regime = s.get("regime",      "normal")
        lim    = s.get("limit_price", float("nan"))
        stop   = s.get("stop_loss",   float("nan"))
        exp    = s.get("expire_days", ENTRY_EXPIRE)
        wr     = s.get("win_rate",    float("nan"))
        pf     = s.get("pf",         float("nan"))

        rg_cls  = {"low": "pos", "normal": "neu", "high": "neg"}.get(regime, "neu")
        wr_s    = f"{wr:.1f}%" if not pd.isna(wr) else "N/A"
        pf_s    = ("∞" if pf == float("inf") else f"{pf:.2f}") if not pd.isna(pf) else "N/A"
        ibs_s   = f"{ibs:.3f}" if not pd.isna(ibs) else "—"
        rsi2_s  = f"{rsi2:.1f}" if not pd.isna(rsi2) else "—"

        signal_rows += (
            f'<tr>'
            f'<td><b>{sym}</b></td>'
            f'<td>{name}</td>'
            f'<td class="neu">{close:,.0f}</td>'
            f'<td class="{"pos" if not pd.isna(rsi2) and rsi2 < RSI2_ENTRY else "neu"}">{rsi2_s}</td>'
            f'<td class="{"pos" if not pd.isna(ibs) and ibs < IBS_ENTRY else "neu"}">{ibs_s}</td>'
            f'<td class="{rg_cls}">{regime}</td>'
            f'<td class="neu">{lim:,.0f}</td>'
            f'<td class="neg">{stop:,.0f}</td>'
            f'<td>{exp}日</td>'
            f'<td class="{"pos" if not pd.isna(wr) and wr >= 60 else "neu"}">{wr_s}</td>'
            f'<td class="{"pos" if not pd.isna(pf) and pf >= 1.5 else "neu"}">{pf_s}</td>'
            f'</tr>\n'
        )

    if not signal_rows:
        signal_rows = '<tr><td colspan="11" style="text-align:center;color:#555;padding:20px">本日シグナルなし</td></tr>\n'

    # ── バックテスト詳細テーブル（シグナル銘柄のみ）──────────
    detail_sections = ""
    signal_symbols  = {s["symbol"] for s in signals}
    for r in scan_results:
        if r["symbol"] not in signal_symbols:
            continue
        trades  = r.get("trades_list", [])
        wins    = [t for t in trades if t["pnl"] > 0]
        losses  = [t for t in trades if t["pnl"] <= 0]
        total   = sum(t["pnl"] for t in trades)
        wr_val  = len(wins) / len(trades) * 100 if trades else 0
        pf_val  = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                   if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
        pf_str  = "∞" if pf_val == float("inf") else f"{pf_val:.2f}"
        tc_cls  = "pos" if total >= 0 else "neg"

        trade_rows = ""
        for i, t in enumerate(trades, 1):
            cls = "hold" if "保有中" in t.get("reason", "") else ("win" if t["pnl"] > 0 else "lose")
            trade_rows += (
                f'<tr class="{cls}">'
                f'<td>{i}</td>'
                f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t.get("limit_price", t["entry_p"]):,.0f}</td>'
                f'<td>{t["entry_p"]:,.0f}</td>'
                f'<td>{t["exit_p"]:,.0f}</td>'
                f'<td class="{"pos" if t["pct"]>=0 else "neg"}">{t["pct"]:+.2f}%</td>'
                f'<td>{t["hold"]}日</td>'
                f'<td>{t.get("reason","")}</td>'
                f'<td>{t.get("regime","")}</td>'
                f'</tr>\n'
            )

        detail_sections += f"""
<div class="detail-section">
  <h2>{r["symbol"]} &nbsp;<span class="name-label">{r["name"]}</span>
    &nbsp;<span class="badge-regime">{r.get("last_regime","")}</span>
  </h2>
  <div class="detail-stats">
    <span>トレード: <b>{len(trades)}</b></span>
    <span>勝率: <b class="{"pos" if wr_val>=60 else "neu"}">{wr_val:.1f}%</b></span>
    <span>PF: <b class="{"pos" if pf_val>=1.5 else "neu"}">{pf_str}</b></span>
    <span>損益: <b class="{tc_cls}">{total:+,.0f}円</b></span>
    <span>勝/負: <b>{len(wins)}/{len(losses)}</b></span>
  </div>
  <table>
  <thead><tr>
    <th>#</th><th>エントリー</th><th>エグジット</th>
    <th>指値</th><th>約定価格</th><th>決済価格</th>
    <th>変化率</th><th>保有日数</th><th>決済理由</th><th>レジーム</th>
  </tr></thead>
  <tbody>{trade_rows}</tbody>
  </table>
</div>
"""

    # ── スキャン結果テーブル（全銘柄）───────────────────────
    ranked = sorted(scan_results, key=lambda x: (-x.get("total_pct", 0), x["symbol"]))
    scan_rows = ""
    for rank, r in enumerate(ranked, 1):
        wr_v  = r.get("win_rate", float("nan"))
        pf_v  = r.get("pf",      float("nan"))
        tot   = r.get("total",   0)
        tp    = r.get("total_pct", 0.0)
        tr_n  = r.get("trade_count", 0)
        pcls  = "pos" if tot >= 0 else "neg"
        wr_s  = f"{wr_v:.1f}%" if not pd.isna(wr_v) else "—"
        pf_s2 = ("∞" if pf_v == float("inf") else f"{pf_v:.2f}") if not pd.isna(pf_v) else "—"
        scan_rows += (
            f'<tr class="{"win" if tot>=0 else "lose"}">'
            f'<td>{rank}</td>'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["name"]}</td>'
            f'<td class="{pcls}">{tot:+,.0f}円</td>'
            f'<td class="{pcls}">{tp:+.2f}%</td>'
            f'<td>{wr_s}</td>'
            f'<td>{pf_s2}</td>'
            f'<td>{tr_n}</td>'
            f'</tr>\n'
        )

    total_pnl   = sum(r.get("total", 0) for r in scan_results)
    total_trades = sum(r.get("trade_count", 0) for r in scan_results)
    plus_count  = sum(1 for r in scan_results if r.get("total", 0) > 0)
    tc_cls2     = "pos" if total_pnl >= 0 else "neg"
    nsigs       = len(signals)
    nuniv       = len(scan_results)

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>アダプティブMR バックテスト {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.4em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}}
h2{{font-size:1.1em;color:#e2e8f0;margin:24px 0 8px;border-left:3px solid #a78bfa;padding-left:10px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 8px 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.section{{background:#11141f;border:1px solid #1e2235;border-radius:10px;padding:16px;margin:20px 0}}
.section-title{{font-size:1.0em;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;
                margin-bottom:12px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:0.85em}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3){{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.detail-section{{background:#13162b;border:1px solid #1e2235;border-radius:10px;padding:20px;margin:24px 0}}
.name-label{{color:#94a3b8;font-size:0.85em;font-weight:400}}
.badge-regime{{display:inline-block;background:#1a1d30;border:1px solid #334155;
               color:#94a3b8;border-radius:4px;padding:1px 8px;font-size:0.75em}}
.detail-stats{{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 14px;font-size:0.88em;color:#888}}
.detail-stats b{{color:#dde1ec}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
</style>
</head>
<body>
<h1>アダプティブ平均回帰バックテスト</h1>
<div class="meta">
  期間: {since_str} 〜 {today_str} &nbsp;|&nbsp;
  ユニバース: {universe} ({nuniv}銘柄) &nbsp;|&nbsp;
  バックテスト: {backtest_days}日
</div>

<div class="cards">
  <div class="card"><div class="clabel">本日シグナル</div>
    <div class="cval {"pos" if nsigs > 0 else "neu"}">{nsigs}銘柄</div></div>
  <div class="card"><div class="clabel">全損益合計</div>
    <div class="cval {tc_cls2}">{total_pnl:+,.0f}円</div></div>
  <div class="card"><div class="clabel">トレード計</div>
    <div class="cval neu">{total_trades}回</div></div>
  <div class="card"><div class="clabel">プラス銘柄</div>
    <div class="cval neu">{plus_count}/{nuniv}</div></div>
</div>

<div class="section">
  <div class="section-title">本日のシグナル — IBS &lt; {IBS_ENTRY} OR RSI(2) &lt; {RSI2_ENTRY:.0f}</div>
  <table>
  <thead><tr>
    <th>銘柄</th><th>名称</th><th>終値</th><th>RSI2</th><th>IBS</th>
    <th>レジーム</th><th>指値</th><th>ストップ</th><th>有効期限</th>
    <th>勝率</th><th>PF</th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
  </table>
</div>

{detail_sections}

<div class="section">
  <div class="section-title">スキャン結果ランキング ({since_str} 〜 {today_str})</div>
  <table>
  <thead><tr>
    <th>#</th><th>銘柄</th><th>名称</th>
    <th>損益</th><th>累積%</th><th>勝率</th><th>PF</th><th>取引数</th>
  </tr></thead>
  <tbody>{scan_rows}</tbody>
  </table>
</div>

<div class="footer">生成: {gen_time} &nbsp;|&nbsp; アダプティブMR (IBS+RSI2+ボラレジーム)</div>
</body></html>"""

    fname = f"amr_{today_str}_{universe}.html"
    path  = Path(fname)
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン処理 ────────────────────────────────────────────────
def _process_symbol(
    symbol: str,
    name: str,
    backtest_days: int,
) -> dict | None:
    """
    1銘柄のデータ取得・指標計算・バックテストを実行する（スレッドプール用）。
    結果 dict または None を返す。
    """
    df = fetch(symbol, backtest_days)
    if df is None:
        return None
    df = calc(df)

    trades     = backtest_amr(df, backtest_days)
    today_sig  = _has_signal_today(df)
    wins       = [t for t in trades if t["pnl"] > 0]
    losses     = [t for t in trades if t["pnl"] <= 0]
    total_pnl  = sum(t["pnl"] for t in trades)
    total_pct  = sum(t["pct"] for t in trades)
    win_rate   = len(wins) / len(trades) * 100 if trades else float("nan")
    pf_val     = (
        sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        if losses and sum(t["pnl"] for t in losses) != 0
        else float("inf")
    )
    last_regime = str(df.iloc[-1].get("regime", "normal")) if len(df) > 0 else "normal"

    result = dict(
        symbol      = symbol,
        name        = name,
        trades_list = trades,
        trade_count = len(trades),
        win_rate    = win_rate,
        pf          = pf_val,
        total       = total_pnl,
        total_pct   = total_pct,
        last_regime = last_regime,
        today_sig   = today_sig,
    )
    return result


# ── 監視銘柄選定：マルチピリオドバックテスト ─────────────────────

# 5期間（ユーザー指定: 30/90/180/365/730日）
WATCHLIST_PERIODS = [30, 90, 180, 365, 730]

# 選定基準
WL_MIN_TRADES = 3      # 各期間の最低取引回数
WL_MIN_WR     = 60.0   # 各期間の最低勝率(%)
WL_MIN_PF     = 1.2    # 各期間の最低プロフィットファクター


def _calc_period_stats(trades: list[dict]) -> dict:
    """トレードリストから勝率・PF・合計損益を計算して返す。"""
    if not trades:
        return dict(n=0, wr=float("nan"), pf=float("nan"), total=0.0)
    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    wr    = len(wins) / len(trades) * 100
    ls    = abs(sum(t["pnl"] for t in loss))
    pf    = sum(t["pnl"] for t in wins) / ls if ls > 0 else float("inf")
    return dict(n=len(trades), wr=wr, pf=pf, total=sum(t["pnl"] for t in trades))


def _load_bt_cache() -> dict:
    try:
        if _BT_CACHE.exists():
            with open(_BT_CACHE, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass
    return {}

def _save_bt_cache(cache: dict) -> None:
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        with open(_BT_CACHE, "wb") as f:
            pickle.dump(cache, f)
    except Exception:
        pass

def _price_cache_mtime(symbol: str) -> float:
    p = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    return p.stat().st_mtime if p.exists() else 0.0


def _process_symbol_multiperiod(
    symbol: str,
    name: str,
    periods: list[int],
    bt_cache=None,
) -> dict | None:
    """
    複数期間のバックテストを1銘柄で実行。
    最長期間のデータを1回だけ取得して各期間に使い回す。
    """
    mtime = _price_cache_mtime(symbol)
    cache_key = (symbol, tuple(sorted(periods)))
    if bt_cache is not None and cache_key in bt_cache:
        cached_mtime, cached_result = bt_cache[cache_key]
        if cached_mtime == mtime and mtime > 0 and "period_trades" in cached_result:
            return cached_result

    max_days = max(periods)
    df_raw   = fetch(symbol, max_days)
    if df_raw is None:
        return None
    df = calc(df_raw)
    if len(df) < 50:
        return None

    period_results: dict[int, dict] = {}
    period_trades:  dict[int, list] = {}
    for days in periods:
        trades = backtest_amr(df, days)
        period_results[days] = _calc_period_stats(trades)
        period_trades[days]  = trades

    today_sig   = _has_signal_today(df)
    last_regime = str(df.iloc[-1].get("regime", "normal")) if len(df) > 0 else "normal"
    last_close  = float(df.iloc[-1]["close"]) if len(df) > 0 else 0.0

    result = dict(
        symbol        = symbol,
        name          = name,
        period_results= period_results,   # {days: {n, wr, pf, total}}
        period_trades = period_trades,    # {days: [trade dicts]}
        today_sig     = today_sig,
        last_regime   = last_regime,
        last_close    = last_close,
    )
    if bt_cache is not None:
        bt_cache[cache_key] = (mtime, result)
    return result


def _passes_watchlist_filter(period_results: dict[int, dict]) -> bool:
    """全期間でフィルター基準を満たすかチェック。"""
    for days, s in period_results.items():
        if s["n"] < WL_MIN_TRADES:
            return False
        if pd.isna(s["wr"]) or s["wr"] < WL_MIN_WR:
            return False
        pf = s["pf"]
        if pd.isna(pf) or (pf != float("inf") and pf < WL_MIN_PF):
            return False
    return True


def build_watchlist_html(
    candidates: list[dict],
    periods: list[int],
) -> Path:
    """監視銘柄選定結果をHTMLで出力する。"""
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    scan_dt   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    periods_s = sorted(periods)

    # ヘッダー列
    period_headers = "".join(
        f'<th colspan="4">{d}日</th>' for d in periods_s
    )
    period_subheaders = "".join(
        '<th>回数</th><th>勝率</th><th>PF</th><th>損益(円)</th>' for _ in periods_s
    )

    rows = ""
    for c in candidates:
        pr  = c["period_results"]
        sig = c["today_sig"]
        sig_mark = "★" if sig else ""
        sym_id = c["symbol"].replace(".", "_").replace("-", "_")

        period_cells = ""
        for d in periods_s:
            s    = pr.get(d, {})
            n    = s.get("n", 0)
            wr   = s.get("wr", float("nan"))
            pf   = s.get("pf", float("nan"))
            wr_s = f"{wr:.0f}%" if not pd.isna(wr) else "—"
            pf_s = ("∞" if pf == float("inf") else f"{pf:.2f}") if not pd.isna(pf) else "—"
            wr_cls = "pos" if (not pd.isna(wr) and wr >= WL_MIN_WR) else "neg"
            pf_cls = "pos" if (pf == float("inf") or (not pd.isna(pf) and pf >= WL_MIN_PF)) else "neg"
            total   = s.get("total", 0.0)
            tot_cls = "pos" if total >= 0 else "neg"
            tot_s   = f"{total:+,.0f}" if n > 0 else "—"
            period_cells += (
                f'<td>{n}</td>'
                f'<td class="{wr_cls}">{wr_s}</td>'
                f'<td class="{pf_cls}">{pf_s}</td>'
                f'<td class="{tot_cls}">{tot_s}</td>'
            )

        lp_s = f'{sig["limit_price"]:,.0f}' if sig else "—"
        sl_s = f'{sig["stop_loss"]:,.0f}'   if sig else "—"
        cl_s = f'{sig["close"]:,.0f}'        if sig else "—"

        rows += (
            f'<tr class="sym-row" onclick="toggleDetail(this)">'
            f'<td>▶\u00a0{sig_mark}{c["symbol"]}</td>'
            f'<td>{c["name"]}</td>'
            f'<td>{c["last_regime"]}</td>'
            f'<td>{cl_s}</td>'
            f'<td class="pos">{lp_s}</td>'
            f'<td class="neg">{sl_s}</td>'
            + period_cells +
            f'</tr>\n'
        )

        # Build inline detail row
        pt = c.get("period_trades", {})
        sections = ""
        for d in periods_s:
            trades = pt.get(d, [])
            if not trades:
                continue
            total_pnl = sum(t["pnl"] for t in trades)
            wins      = [t for t in trades if t["pnl"] > 0]
            tc_cls    = "pos" if total_pnl >= 0 else "neg"
            t_rows = ""
            for i, t in enumerate(trades, 1):
                cls   = "win" if t["pnl"] > 0 else ("hold" if "保有中" in t.get("reason","") else "lose")
                lp    = t.get("limit_price", t["entry_p"])
                sl    = t.get("stop_p", float("nan"))
                tgt   = t.get("target_p", float("nan"))
                sl_s  = f'{sl:,.0f}'  if not pd.isna(sl)  else "—"
                tgt_s = f'{tgt:,.0f}' if not pd.isna(tgt) else "—"
                rg    = t.get("regime", "")
                t_rows += (
                    f'<tr class="{cls}"><td>{i}</td>'
                    f'<td>{t["entry_dt"].strftime("%m/%d")}</td>'
                    f'<td>{t["exit_dt"].strftime("%m/%d")}</td>'
                    f'<td>{lp:,.0f}</td>'
                    f'<td>{t["entry_p"]:,.0f}</td>'
                    f'<td class="neg">{sl_s}</td>'
                    f'<td class="pos">{tgt_s}</td>'
                    f'<td>{t["exit_p"]:,.0f}</td>'
                    f'<td class="{"pos" if t["pct"]>=0 else "neg"}">{t["pct"]:+.1f}%</td>'
                    f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
                    f'<td>{t["hold"]}日</td>'
                    f'<td>{t.get("reason","")}</td>'
                    f'<td>{rg}</td></tr>\n'
                )
            sections += f"""
<h3 style="margin:14px 0 6px;font-size:0.95em;color:#94a3b8">{d}日間
  <span style="color:#666;font-size:0.85em">
    {len(trades)}回 / 勝:{len(wins)} / 損益:<span class="{tc_cls}">{total_pnl:+,.0f}円</span>
  </span></h3>
<table><thead><tr>
  <th>#</th><th>IN</th><th>OUT</th>
  <th>指値</th><th>約定値</th><th>逆指値</th><th>利確目標</th><th>決済値</th>
  <th>損益%</th><th>損益(円)</th><th>保有</th><th>理由</th><th>レジーム</th>
</tr></thead><tbody>{t_rows}</tbody></table>"""
        rows += (
            f'<tr class="detail-row" style="display:none">'
            f'<td colspan="99"><div class="detail-inner">{sections}</div></td>'
            f'</tr>\n'
        )

    signal_count = sum(1 for c in candidates if c["today_sig"])

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>監視銘柄リスト {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:13px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}}
.meta{{color:#555;font-size:0.8em;margin:2px 0 16px 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0 24px}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:12px 18px;min-width:120px}}
.clabel{{font-size:0.7em;color:#666;letter-spacing:.05em}}
.cval{{font-size:1.4em;font-weight:700;margin-top:2px}}
.criteria{{background:#131825;border:1px solid #1e3a5f;border-radius:8px;
           padding:12px 18px;margin:0 0 20px;font-size:0.85em;color:#7ab3d4}}
.criteria b{{color:#38bdf8}}
table{{width:100%;border-collapse:collapse;font-size:0.83em}}
th{{background:#16192a;color:#666;padding:7px 10px;text-align:right;
    border-bottom:2px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3){{text-align:left}}
th[colspan]{{text-align:center;border-left:1px solid #2a2f4a;color:#94a3b8}}
td{{padding:6px 10px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}
tr:hover>td{{background:#1b1f35}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.footer{{margin-top:28px;color:#333;font-size:0.75em;text-align:right}}
.sym-row{{cursor:pointer}}
.sym-row td:first-child{{user-select:none}}
.detail-row>td{{padding:0;background:#0d1020!important;border-bottom:2px solid #252840}}
.detail-inner{{padding:12px 20px}}
.detail-inner table{{width:auto;min-width:600px}}
.detail-inner th{{background:#0d1020}}
tr.win>td{{background:rgba(74,222,128,.05)}}
tr.lose>td{{background:rgba(248,113,113,.05)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
</style>
</head>
<body>
<h1>監視銘柄リスト — アダプティブMR戦略</h1>
<div class="meta">スキャン日時: {scan_dt} ／ 対象期間: {', '.join(str(d)+'日' for d in periods_s)}</div>

<div class="criteria">
  選定基準（全期間で満たすこと）：
  <b>取引回数 ≥ {WL_MIN_TRADES}回</b> ／
  <b>勝率 ≥ {WL_MIN_WR:.0f}%</b> ／
  <b>PF ≥ {WL_MIN_PF}</b>
</div>

<div class="cards">
  <div class="card"><div class="clabel">候補銘柄数</div><div class="cval pos">{len(candidates)}</div></div>
  <div class="card"><div class="clabel">本日シグナル</div><div class="cval pos">{signal_count}</div></div>
  <div class="card"><div class="clabel">評価期間数</div><div class="cval">{len(periods_s)}</div></div>
</div>

<table>
<thead>
<tr>
  <th rowspan="2">コード</th>
  <th rowspan="2">銘柄名</th>
  <th rowspan="2">レジーム</th>
  <th rowspan="2">終値</th>
  <th rowspan="2">買い指値</th>
  <th rowspan="2">損切り</th>
  {period_headers}
</tr>
<tr>{period_subheaders}</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>

<div class="footer">★ = 本日シグナルあり（買い指値注文を出す候補）</div>
<script>
function toggleDetail(row){{
  var next=row.nextElementSibling;
  if(!next||!next.classList.contains('detail-row'))return;
  var open=next.style.display==='table-row';
  next.style.display=open?'none':'table-row';
  var td=row.querySelector('td');
  if(td)td.textContent=td.textContent.replace(/^[▶▼]\u00a0/,''+(open?'▶\u00a0':'▼\u00a0'));
}}
</script>
</body></html>"""

    out = Path(f"watchlist_adaptive_mr_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="アダプティブ平均回帰バックテスト（IBS + RSI(2) + ボラティリティレジーム）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_adaptive_mr.py                     # 監視銘柄スキャン（1年）
  python backtest_adaptive_mr.py --universe all      # 日経225全銘柄スキャン
  python backtest_adaptive_mr.py --symbol 7011.T     # 1銘柄詳細
  python backtest_adaptive_mr.py --days 180          # 180日バックテスト
  python backtest_adaptive_mr.py --watchlist         # 5期間バックテストで監視銘柄を選定
  python backtest_adaptive_mr.py --no-browser        # ブラウザ自動起動なし
""")
    parser.add_argument("--symbol",     type=str,  default=None,
                        help="銘柄コード（省略時はユニバース全体スキャン）")
    parser.add_argument("--days",       type=int,  default=BACKTEST_DAYS,
                        help=f"バックテスト日数（デフォルト: {BACKTEST_DAYS}）")
    parser.add_argument("--universe",   type=str,  default="watch",
                        choices=["watch", "225", "all"],
                        help="銘柄ユニバース: watch（監視銘柄）/ 225（日経225）/ all（全上場銘柄）")
    parser.add_argument("--watchlist",  action="store_true",
                        help=f"5期間({'/'.join(str(p) for p in WATCHLIST_PERIODS)}日)バックテストで監視銘柄を選定")
    parser.add_argument("--no-browser", action="store_true",
                        help="HTMLレポートをブラウザで自動起動しない")
    args = parser.parse_args()

    backtest_days = args.days
    since_str     = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today_str     = _TODAY.strftime("%Y-%m-%d")

    # --universe 225/all の場合、--symbol 未指定なら自動で4期間バックテストモードへ
    if not args.watchlist and args.universe in ("225", "all") and not args.symbol:
        args.watchlist = True

    # ── 監視銘柄選定モード（--watchlist）────────────────────────
    if args.watchlist:
        symbols = _load_symbols(args.universe)
        periods      = WATCHLIST_PERIODS   # 常に全期間でバックテスト（キャッシュ安定化）
        show_periods = [p for p in WATCHLIST_PERIODS if p <= args.days] or [args.days]
        bt_cache = _load_bt_cache()
        cached_count = sum(1 for sym, _ in symbols if (sym, tuple(sorted(periods))) in bt_cache)
        print(f"\nバックテスト実行: {len(symbols)}銘柄 / 期間:{periods}日 / 表示:{show_periods}日")
        if cached_count:
            print(f"  キャッシュ: {cached_count}銘柄（株価更新なし → スキップ）")
        print(f"  基準: 全期間で 取引≥{WL_MIN_TRADES}回 / 勝率≥{WL_MIN_WR}% / PF≥{WL_MIN_PF}")
        print(f"  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")

        all_results: list[dict] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(_process_symbol_multiperiod, sym, name, periods, bt_cache): (sym, name)
                for sym, name in symbols
            }
            done_count = 0
            for fut in as_completed(futures):
                done_count += 1
                try:
                    r = fut.result()
                    if r:
                        all_results.append(r)
                except Exception:
                    pass
                if done_count % 20 == 0 or done_count == len(symbols):
                    print(f"  {done_count}/{len(symbols)} 完了...", end="\r", flush=True)
        _save_bt_cache(bt_cache)
        print()

        # 表示対象期間に取引があった銘柄のみ・表示期間の損益順でソート
        candidates = [r for r in all_results if any(r["period_results"].get(d, {}).get("n", 0) > 0 for d in show_periods)]
        candidates.sort(key=lambda r: (-sum(r["period_results"].get(d, {}).get("total", 0) for d in show_periods), r["symbol"]))

        print(f"\n  スキャン完了: {len(all_results)}銘柄処理")
        print(f"  スキャン結果（利益順）: {len(candidates)}銘柄\n")
        for c in candidates:
            sig_mark = "★" if c["today_sig"] else " "
            pr_summary = "  ".join(
                f"{d}日:[{c['period_results'].get(d,{}).get('n',0)}回 "
                f"WR{c['period_results'].get(d,{}).get('wr',0):.0f}% "
                f"PF{c['period_results'].get(d,{}).get('pf',0):.1f}]"
                for d in sorted(show_periods)
            )
            print(f"  {sig_mark}{c['symbol']:8s} {c['name'][:14]:<14}  {pr_summary}")

        path = build_watchlist_html(candidates, show_periods)
        print(f"\n  HTMLレポート保存: {path}")
        # CSV出力（run_ranking.py用）
        import csv as _csv
        _csv_path = Path(f"candidates_adaptive_mr.csv")
        with open(_csv_path, "w", newline="", encoding="utf-8") as _f:
            _w = _csv.writer(_f)
            _w.writerow(["symbol","name","last_close",
                         "30_n","30_wr","30_pf","30_total",
                         "90_n","90_wr","90_pf","90_total",
                         "180_n","180_wr","180_pf","180_total",
                         "365_n","365_wr","365_pf","365_total"])
            for _c in candidates:
                _pr = _c["period_results"]
                _row = [_c["symbol"], _c["name"], _c.get("last_close", 0)]
                for _d in [30, 90, 180, 365]:
                    _s = _pr.get(_d, {})
                    _pf = _s.get("pf", 0) or 0
                    _row += [_s.get("n", 0),
                             round(_s.get("wr", 0) or 0, 1),
                             round(0 if _pf == float("inf") else _pf, 4),
                             round(_s.get("total", 0) or 0, 0)]
                _w.writerow(_row)
        print(f"CSV: {_csv_path.resolve()}")
        if not args.no_browser:
            open_html(f"file://{path.resolve()}")
        return

    # ── 1銘柄モード ──────────────────────────────────────────
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        name = sym
        print(f"\n  データ取得中: {sym} ...")
        df = fetch(sym, backtest_days)
        if df is None:
            print(f"  エラー: {sym} のデータ取得に失敗しました\n")
            return
        df     = calc(df)
        trades = backtest_amr(df, backtest_days)
        sig    = _has_signal_today(df)

        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        wr    = len(wins) / len(trades) * 100 if trades else 0
        pf_v  = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                 if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        pf_s  = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"

        print(f"\n  [{sym}]  {since_str} 〜 {today_str}  ({backtest_days}日)")
        print(f"  トレード: {len(trades)}回  勝率: {wr:.1f}%  PF: {pf_s}  損益: {total:+,.0f}円")
        print()
        for i, t in enumerate(trades, 1):
            mark = "★" if "保有中" in t.get("reason", "") else " "
            print(f" {mark}{i:3d}  "
                  f"{t['entry_dt'].strftime('%Y-%m-%d')} → "
                  f"{t['exit_dt'].strftime('%Y-%m-%d')}  "
                  f"指値:{t.get('limit_price', t['entry_p']):,.0f}  "
                  f"約定:{t['entry_p']:,.0f}  "
                  f"決済:{t['exit_p']:,.0f}  "
                  f"{t['pct']:+.2f}%  "
                  f"{t['hold']}日  "
                  f"{t.get('reason','')}  "
                  f"[{t.get('regime','')}]")

        if sig:
            print(f"\n  ★ 本日シグナルあり!")
            print(f"     終値: {sig['close']:,.0f}  RSI2: {sig['rsi2']:.1f}  "
                  f"IBS: {sig['ibs']:.3f}  レジーム: {sig['regime']}")
            print(f"     指値: {sig['limit_price']:,.0f}  "
                  f"ストップ: {sig['stop_loss']:,.0f}  "
                  f"有効期限: {sig['expire_days']}日")
        else:
            print(f"\n  本日シグナルなし")

        # HTML出力（1銘柄用に scan_results を1件として渡す）
        result_entry = dict(
            symbol      = sym,
            name        = name,
            trades_list = trades,
            trade_count = len(trades),
            win_rate    = wr,
            pf          = pf_v,
            total       = total,
            total_pct   = sum(t["pct"] for t in trades),
            last_regime = str(df.iloc[-1].get("regime", "normal")) if len(df) > 0 else "normal",
            today_sig   = sig,
        )
        sigs_list = []
        if sig:
            s2 = dict(sig)
            s2["symbol"]   = sym
            s2["name"]     = name
            s2["win_rate"] = wr
            s2["pf"]       = pf_v
            sigs_list.append(s2)

        path = build_html(sigs_list, [result_entry], backtest_days, "single")
        print(f"\n  HTMLレポート保存: {path}")
        if not args.no_browser:
            open_html(f"file://{path.resolve()}")
        return

    # ── スキャンモード ───────────────────────────────────────
    symbols = _load_symbols(args.universe)
    print(f"\n  アダプティブMRスキャン  {len(symbols)}銘柄  {since_str} 〜 {today_str}")
    print(f"  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")

    scan_results: list[dict] = []
    signals:      list[dict] = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(_process_symbol, sym, name, backtest_days): (sym, name)
            for sym, name in symbols
        }
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            sym, name = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                print(f"  エラー [{sym}]: {e}")
                result = None
            if result is None:
                continue
            scan_results.append(result)
            if result["today_sig"] is not None:
                s = dict(result["today_sig"])
                s["symbol"]   = sym
                s["name"]     = name
                s["win_rate"] = result["win_rate"]
                s["pf"]       = result["pf"]
                signals.append(s)
            # 進捗表示（20件ごと）
            if done_count % 20 == 0 or done_count == len(symbols):
                print(f"  {done_count}/{len(symbols)} 完了...")

    # ── 結果表示 ─────────────────────────────────────────────
    total_pnl    = sum(r.get("total", 0)       for r in scan_results)
    total_trades = sum(r.get("trade_count", 0) for r in scan_results)
    plus_count   = sum(1 for r in scan_results if r.get("total", 0) > 0)
    tc_sign      = "+" if total_pnl >= 0 else ""

    print(f"\n  スキャン完了: {len(scan_results)}/{len(symbols)}銘柄")
    print(f"  トレード合計: {total_trades}回  プラス銘柄: {plus_count}/{len(scan_results)}")
    print(f"  全損益合計: {tc_sign}{total_pnl:,.0f}円\n")

    if signals:
        print(f"  ★ 本日のシグナル ({len(signals)}銘柄):")
        for s in sorted(signals, key=lambda x: x.get("rsi2", 99)):
            wr_s = f"{s.get('win_rate', float('nan')):.1f}%" if not pd.isna(s.get('win_rate', float('nan'))) else "N/A"
            print(f"    [{s['symbol']}] {s['name']:<20}  "
                  f"終値:{s['close']:,.0f}  "
                  f"RSI2:{s['rsi2']:.1f}  "
                  f"IBS:{s['ibs']:.3f}  "
                  f"レジーム:{s['regime']}  "
                  f"指値:{s['limit_price']:,.0f}  "
                  f"勝率:{wr_s}")
    else:
        print("  本日シグナルなし")

    # ── HTMLレポート生成 ─────────────────────────────────────
    path = build_html(signals, scan_results, backtest_days, args.universe)
    print(f"\n  HTMLレポート保存: {path}")
    if not args.no_browser:
        open_html(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
