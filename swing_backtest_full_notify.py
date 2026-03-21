"""
スイングトレード 全期間バックテスト + Telegram 通知テスト版
銘柄  : 味の素 (2802.T)
目的  : 指定期間の過去データを日次で再生し
         ① 全シグナル（買い/売り/ストップ）を Telegram 通知
         ② バックテスト損益・各種指標をレポート表示

■ 実行方法:
  # デフォルト（直近1年）
  python swing_backtest_full_notify.py

  # 期間指定
  python swing_backtest_full_notify.py --days 60
  python swing_backtest_full_notify.py --start 2025-01-01 --end 2025-12-31

  # Telegram 通知なし（結果確認のみ）
  python swing_backtest_full_notify.py --no-notify

■ 環境変数:
  export TELEGRAM_BOT_TOKEN="xxxx"
  export TELEGRAM_CHAT_ID="123456789"

⚠ 注意: バックテストは過去データに基づくものであり、将来の利益を保証しません。
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

# ── 設定 ─────────────────────────────────────────────────────────
SYMBOL      = "2802.T"
SYMBOL_NAME = "味の素"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── 戦略パラメータ（swing_notify.py / swing_backtest_ajinomoto.py と同一） ──
EMA_FAST      = 5
EMA_MID       = 20
EMA_SLOW      = 50
RSI_PERIOD    = 14
RSI_ENTRY     = 55
RSI_EXIT      = 60
BB_PERIOD     = 20
BB_K          = 2.0
ATR_PERIOD    = 14
ATR_STOP_MULT = 1.5
RISK_PER_TRADE = 0.03
INITIAL_CASH  = 500_000
LOT_SIZE      = 100
MAX_QTY       = 500
RISK_FREE_RATE = 0.001   # 無リスク金利（年率0.1%）

# Telegram 連続送信間隔（秒）— レート制限回避
NOTIFY_INTERVAL = 1.0


# ── Telegram 送信 ────────────────────────────────────────────────
def send_telegram(message: str, dry_run: bool = False) -> bool:
    """Telegram にメッセージを送信する。dry_run=True ならコンソール出力のみ。"""
    if dry_run or not TELEGRAM_BOT_TOKEN:
        tag = "[DRY RUN]" if dry_run else "[Telegram未設定]"
        print(f"\n{tag}")
        print("─" * 52)
        print(message)
        print("─" * 52)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"  Telegram 送信失敗: {e}")
        return False


# ── データ取得 ────────────────────────────────────────────────────
def fetch_data(start: str, end: str) -> pd.DataFrame:
    import yfinance as yf

    # インジケーター計算のウォームアップ用に200日多く取得
    warmup_start = (datetime.strptime(start, "%Y-%m-%d")
                    - timedelta(days=200)).strftime("%Y-%m-%d")
    fetch_end = (datetime.strptime(end, "%Y-%m-%d")
                 + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"[yfinance] {SYMBOL} データ取得中 (ウォームアップ含む)...")
    df = yf.download(SYMBOL, start=warmup_start, end=fetch_end,
                     interval="1d", auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("データが取得できませんでした（ネット接続を確認してください）")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    print(f"  取得成功: {len(df):,} 本 "
          f"({df.index[0].date()} 〜 {df.index[-1].date()})")
    return df


# ── インジケーター計算 ─────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    df["ema_fast"] = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["ema_mid"]  = c.ewm(span=EMA_MID,   adjust=False).mean()
    df["ema_slow"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()

    df["ema_cross_up"] = (
        (df["ema_fast"] > df["ema_mid"]) &
        (df["ema_fast"].shift(1) <= df["ema_mid"].shift(1))
    )

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    sma        = c.rolling(BB_PERIOD).mean()
    std        = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std

    prev_c = c.shift(1)
    tr     = pd.concat([h - l,
                        (h - prev_c).abs(),
                        (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── シグナル判定 ───────────────────────────────────────────────
def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    trend_up  = df["close"] > df["ema_slow"]
    sig_rsi   = df["rsi"] < RSI_ENTRY
    sig_cross = df["ema_cross_up"]
    sig_bb    = df["close"] <= df["bb_lower"] + df["bb_band"]

    df["entry_sig"] = trend_up & (sig_rsi | sig_cross | sig_bb)
    df["exit_sig"]  = (
        (df["rsi"]      > RSI_EXIT) |
        (df["close"]    >= df["bb_upper"]) |
        (df["ema_fast"] < df["ema_mid"])
    )
    return df


# ── 取引株数計算 ───────────────────────────────────────────────
def calc_qty(cash: float, atr: float) -> int:
    risk_amount = cash * RISK_PER_TRADE
    stop_dist   = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    raw_qty = int(risk_amount / stop_dist)
    lots    = min(raw_qty // LOT_SIZE, MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── シグナル理由テキスト ───────────────────────────────────────
def _entry_reasons(row: pd.Series) -> str:
    r = []
    if row["rsi"] < RSI_ENTRY:
        r.append(f"RSI低値({row['rsi']:.1f}<{RSI_ENTRY})")
    if row["ema_cross_up"]:
        r.append("EMAゴールデンクロス")
    if row["close"] <= row["bb_lower"] + row["bb_band"]:
        r.append("BB下限タッチ")
    return " / ".join(r) if r else "複合"


def _exit_reasons(row: pd.Series) -> str:
    r = []
    if row["rsi"] > RSI_EXIT:
        r.append(f"RSI高値({row['rsi']:.1f}>{RSI_EXIT})")
    if row["close"] >= row["bb_upper"]:
        r.append("BB上限タッチ")
    if row["ema_fast"] < row["ema_mid"]:
        r.append("EMAデッドクロス")
    return " / ".join(r) if r else "複合"


# ── Telegram メッセージ生成 ─────────────────────────────────────
def build_buy_msg(row: pd.Series, date_str: str, cash: float) -> str:
    close    = row["close"]
    atr      = row["atr"]
    stop     = close - atr * ATR_STOP_MULT
    qty      = calc_qty(cash, atr)
    cost_est = close * qty
    return (
        f"\n【買いシグナル】{SYMBOL_NAME}({SYMBOL})\n"
        f"[バックテスト再現]\n"
        f"─────────────────────\n"
        f"日付        : {date_str}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"理由        : {_entry_reasons(row)}\n"
        f"EMA{EMA_SLOW}上  : {'✓上昇トレンド' if close > row['ema_slow'] else '✗'}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行買い推奨\n"
        f"推奨株数    : {qty} 株\n"
        f"概算コスト  : {cost_est:,.0f} 円\n"
        f"ストップ目安: {stop:,.0f} 円 (ATR×{ATR_STOP_MULT})\n"
        f"ATR         : {atr:.1f} 円"
    )


def build_sell_msg(row: pd.Series, date_str: str,
                   entry_price: float, qty: int, entry_dt: datetime) -> str:
    close   = row["close"]
    pnl_est = (close - entry_price) * qty
    sign    = "+" if pnl_est >= 0 else ""
    return (
        f"\n【売りシグナル】{SYMBOL_NAME}({SYMBOL})\n"
        f"[バックテスト再現]\n"
        f"─────────────────────\n"
        f"日付        : {date_str}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"理由        : {_exit_reasons(row)}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行売り推奨\n"
        f"保有株数    : {qty} 株\n"
        f"取得値      : {entry_price:,.0f} 円\n"
        f"含み損益    : {sign}{pnl_est:,.0f} 円\n"
        f"取得日      : {entry_dt.strftime('%Y-%m-%d')}"
    )


def build_stop_msg(row: pd.Series, date_str: str,
                   entry_price: float, stop_price: float,
                   qty: int, entry_dt: datetime) -> str:
    close   = row["close"]
    pnl_est = (close - entry_price) * qty
    sign    = "+" if pnl_est >= 0 else ""
    return (
        f"\n【ストップ到達】{SYMBOL_NAME}({SYMBOL})\n"
        f"[バックテスト再現]\n"
        f"─────────────────────\n"
        f"日付          : {date_str}\n"
        f"本日安値      : {row['low']:,.0f} 円\n"
        f"ストップ価格  : {stop_price:,.0f} 円\n"
        f"─────────────────────\n"
        f"⚠ 翌朝 成行売り（損切り）\n"
        f"保有株数      : {qty} 株\n"
        f"取得値        : {entry_price:,.0f} 円\n"
        f"概算損益      : {sign}{pnl_est:,.0f} 円\n"
        f"取得日        : {entry_dt.strftime('%Y-%m-%d')}"
    )


# ── バックテスト（翌日始値執行モデル） ──────────────────────────
def backtest(df: pd.DataFrame, dry_run: bool) -> tuple[list[dict], list[tuple]]:
    """
    翌営業日の始値で成行執行するモデル。
    シグナル発生ごとに Telegram 通知を送信する。
    """
    trades = []
    equity = [(df.index[0], float(INITIAL_CASH))]
    cash   = float(INITIAL_CASH)

    in_pos      = False
    entry_price = 0.0
    stop_price  = 0.0
    entry_dt    = None
    signal_dt   = None
    qty         = 0

    pending_entry     = False
    pending_entry_atr = 0.0
    pending_entry_sig_dt = None

    pending_exit        = False
    pending_exit_reason = ""
    pending_exit_sig_dt = None
    pending_exit_row    = None   # シグナル発生行（メッセージ生成用）

    notify_count = 0

    for dt, row in df.iterrows():
        if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"],
                        row["bb_upper"], row["bb_lower"]])):
            continue

        date_str = dt.strftime("%Y-%m-%d(%a)")

        # ── 翌日始値：ペンディングエントリー執行 ──────────────
        if pending_entry and not in_pos:
            new_qty = calc_qty(cash, pending_entry_atr)
            if new_qty > 0:
                ep   = row["open"]
                sp   = ep - pending_entry_atr * ATR_STOP_MULT
                cost = ep * new_qty
                if cost <= cash:
                    cash        -= cost
                    entry_price  = ep
                    stop_price   = sp
                    entry_dt     = dt
                    signal_dt    = pending_entry_sig_dt
                    qty          = new_qty
                    in_pos       = True
                    equity.append((dt, cash))
            pending_entry     = False
            pending_entry_atr = 0.0

        # ── 翌日始値：ペンディングエグジット執行 ──────────────
        if pending_exit and in_pos:
            exit_price = row["open"]
            pnl        = (exit_price - entry_price) * qty
            cash      += exit_price * qty
            trades.append({
                "signal_dt":   pending_exit_sig_dt,
                "entry_dt":    entry_dt,
                "exit_dt":     dt,
                "entry_price": entry_price,
                "exit_price":  exit_price,
                "stop_price":  stop_price,
                "qty":         qty,
                "hold_days":   (dt - entry_dt).days,
                "pnl":         pnl,
                "cash":        cash,
                "note":        pending_exit_reason,
            })
            equity.append((dt, cash))
            in_pos              = False
            pending_exit        = False
            pending_exit_reason = ""
            pending_exit_row    = None

        # ── ポジション保有中：シグナル判定 ────────────────────
        if in_pos and not pending_exit:
            if row["low"] <= stop_price:
                # ストップロス通知
                msg = build_stop_msg(row, date_str, entry_price,
                                     stop_price, qty, entry_dt)
                print(f"  {date_str}  ⚠ ストップ到達")
                send_telegram(msg, dry_run=dry_run)
                notify_count += 1
                time.sleep(NOTIFY_INTERVAL)

                pending_exit        = True
                pending_exit_reason = f"ストップロス(ATR×{ATR_STOP_MULT})"
                pending_exit_sig_dt = dt
                pending_exit_row    = row

            elif row["exit_sig"]:
                # 売りシグナル通知
                msg = build_sell_msg(row, date_str, entry_price, qty, entry_dt)
                print(f"  {date_str}  売りシグナル")
                send_telegram(msg, dry_run=dry_run)
                notify_count += 1
                time.sleep(NOTIFY_INTERVAL)

                pending_exit        = True
                pending_exit_reason = "シグナル"
                pending_exit_sig_dt = dt
                pending_exit_row    = row

        # ── 新規エントリー：シグナル判定 ──────────────────────
        if not in_pos and not pending_entry and row["entry_sig"]:
            # 買いシグナル通知
            msg = build_buy_msg(row, date_str, cash)
            print(f"  {date_str}  買いシグナル  終値:{row['close']:,.0f}  RSI:{row['rsi']:.1f}")
            send_telegram(msg, dry_run=dry_run)
            notify_count += 1
            time.sleep(NOTIFY_INTERVAL)

            pending_entry         = True
            pending_entry_atr     = row["atr"]
            pending_entry_sig_dt  = dt

    # 最終日強制クローズ
    if in_pos:
        last = df.iloc[-1]
        pnl  = (last["close"] - entry_price) * qty
        cash += last["close"] * qty
        trades.append({
            "signal_dt":   None,
            "entry_dt":    entry_dt,
            "exit_dt":     df.index[-1],
            "entry_price": entry_price,
            "exit_price":  last["close"],
            "stop_price":  stop_price,
            "qty":         qty,
            "hold_days":   (df.index[-1] - entry_dt).days,
            "pnl":         pnl,
            "cash":        cash,
            "note":        "最終日強制決済",
        })
        equity.append((df.index[-1], cash))

    print(f"\n  Telegram 通知送信数: {notify_count} 件")
    return trades, equity


# ── パフォーマンス指標 ─────────────────────────────────────────
def calc_metrics(trades: list[dict], equity: list[tuple], n_days: int) -> dict:
    if not trades:
        return {}

    final     = trades[-1]["cash"]
    total_pnl = final - INITIAL_CASH
    years     = n_days / 252
    cagr      = (final / INITIAL_CASH) ** (1 / years) - 1 if years > 0 else 0

    vals   = [v for _, v in equity]
    peak   = vals[0]
    max_dd = 0.0
    for v in vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak)

    calmar = cagr / abs(max_dd) if max_dd != 0 else float("inf")

    eq_df    = pd.Series(dict(equity)).sort_index()
    eq_daily = eq_df.resample("B").last().ffill()
    daily_ret = eq_daily.pct_change().dropna()
    excess   = daily_ret - RISK_FREE_RATE / 252
    sharpe   = (excess.mean() / excess.std() * math.sqrt(252)
                if excess.std() > 0 else 0)

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    avg_w  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_l  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    avg_hld = sum(t["hold_days"] for t in trades) / len(trades)

    return {
        "final": final, "total_pnl": total_pnl,
        "ret_pct": total_pnl / INITIAL_CASH * 100,
        "cagr": cagr * 100, "sharpe": sharpe,
        "max_dd": max_dd * 100, "calmar": calmar,
        "n_trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_w": avg_w, "avg_l": avg_l,
        "payoff": abs(avg_w / avg_l) if avg_l else float("inf"),
        "avg_hld": avg_hld,
    }


# ── サマリー通知メッセージ ──────────────────────────────────────
def build_summary_msg(m: dict, start: str, end: str) -> str:
    sign = "+" if m["total_pnl"] >= 0 else ""
    return (
        f"\n【バックテスト結果】{SYMBOL_NAME}({SYMBOL})\n"
        f"─────────────────────\n"
        f"期間      : {start} 〜 {end}\n"
        f"初期資金  : {INITIAL_CASH:,.0f} 円\n"
        f"最終資金  : {m['final']:,.0f} 円\n"
        f"期間損益  : {sign}{m['total_pnl']:,.0f} 円 ({sign}{m['ret_pct']:.1f}%)\n"
        f"CAGR      : {m['cagr']:+.1f}%\n"
        f"─────────────────────\n"
        f"トレード数: {m['n_trades']} 回\n"
        f"勝率      : {m['win_rate']:.0f}%  ({m['wins']}勝/{m['losses']}敗)\n"
        f"ペイオフ比: {m['payoff']:.2f}\n"
        f"平均保有  : {m['avg_hld']:.1f} 日\n"
        f"─────────────────────\n"
        f"シャープレシオ   : {m['sharpe']:.2f}\n"
        f"最大ドローダウン : {m['max_dd']:+.1f}%\n"
        f"カルマーレシオ   : {m['calmar']:.2f}"
    )


# ── レポート表示 ───────────────────────────────────────────────
DAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

def report(df_target: pd.DataFrame, trades: list[dict],
           equity: list[tuple], metrics: dict):
    W = 86
    start = df_target.index[0].strftime("%Y-%m-%d")
    end   = df_target.index[-1].strftime("%Y-%m-%d")

    print("\n" + "=" * W)
    print(f"  スイングトレード バックテスト  {SYMBOL} ({SYMBOL_NAME})")
    print("=" * W)
    print(f"  期間     : {start} 〜 {end}（{len(df_target)} 営業日）")
    print(f"  戦略     : EMA({EMA_FAST}/{EMA_MID}/{EMA_SLOW}) + "
          f"RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_K}σ) + ATR({ATR_PERIOD})")
    print(f"  エントリー: 終値>EMA{EMA_SLOW} かつ "
          f"[RSI<{RSI_ENTRY} or EMAクロス or BB下限タッチ]")
    print(f"  エグジット: RSI>{RSI_EXIT} or BB上限 or EMAクロス or ATR×{ATR_STOP_MULT}ストップ")
    print(f"  執行モデル: シグナル翌営業日の始値で成行執行")
    print(f"  初期資金 : {INITIAL_CASH:,.0f} 円")
    print("=" * W)

    if not trades:
        print("\n  期間内にトレードは発生しませんでした")
        return

    print(f"\n{'#':>3}  {'シグナル':8} {'エントリー':10} {'エグジット':10} "
          f"{'保有':>4} {'株数':>4} {'取得値':>7} {'決済値':>7} "
          f"{'損益':>10} {'資金残高':>13}  メモ")
    print("-" * W)
    for i, t in enumerate(trades, 1):
        e_dow   = DAY_JP[t["entry_dt"].weekday()]
        x_dow   = DAY_JP[t["exit_dt"].weekday()]
        sign    = "+" if t["pnl"] >= 0 else ""
        sig_str = (t["signal_dt"].strftime("%m/%d")
                   if t.get("signal_dt") else "─────")
        print(
            f"  {i:>2}  {sig_str}→ "
            f"{t['entry_dt'].strftime('%m/%d')}({e_dow})  "
            f"{t['exit_dt'].strftime('%m/%d')}({x_dow})  "
            f"{t['hold_days']:>3}日  "
            f"{t['qty']:>4}株  "
            f"{t['entry_price']:>7,.0f}  "
            f"{t['exit_price']:>7,.0f}  "
            f"{sign}{t['pnl']:>9,.0f}円  "
            f"{t['cash']:>13,.0f}円  "
            f"{t['note']}"
        )

    if not metrics:
        return

    m = metrics
    print("\n" + "=" * W)
    print(f"  【サマリー】  {start} 〜 {end}")
    print("=" * W)
    print(f"  初期資金             : {INITIAL_CASH:>14,.0f} 円")
    print(f"  最終資金残高         : {m['final']:>14,.0f} 円")
    print(f"  期間損益             : {m['total_pnl']:>+14,.0f} 円")
    print(f"  資金リターン         : {m['ret_pct']:>+13.2f} %")
    print(f"  年換算リターン(CAGR) : {m['cagr']:>+13.2f} %")
    print("-" * W)
    print(f"  シャープレシオ       : {m['sharpe']:>14.2f}  (>1.0 良好 / >2.0 優秀)")
    print(f"  最大ドローダウン     : {m['max_dd']:>+13.2f} %  (<15% 優秀 / <25% 許容)")
    print(f"  カルマーレシオ       : {m['calmar']:>14.2f}  (>0.5 目標)")
    print("-" * W)
    print(f"  総トレード数         : {m['n_trades']:>14} 回")
    print(f"  勝ち / 負け          : {m['wins']:>6} 勝 / {m['losses']} 敗")
    print(f"  勝率                 : {m['win_rate']:>13.1f} %")
    print(f"  平均利益             : {m['avg_w']:>+14,.0f} 円")
    print(f"  平均損失             : {m['avg_l']:>+14,.0f} 円")
    print(f"  ペイオフ比           : {m['payoff']:>14.2f}  (>1.5 目標)")
    print(f"  平均保有日数         : {m['avg_hld']:>13.1f} 日")
    print("=" * W)

    # 損益バーチャート
    max_abs = max(abs(t["pnl"]) for t in trades) or 1
    print("\n--- トレード別損益チャート ---")
    for i, t in enumerate(trades, 1):
        w    = max(1, int(abs(t["pnl"]) / max_abs * 38))
        bar  = ("▪" if t["pnl"] >= 0 else "▫") * w
        sign = "+" if t["pnl"] >= 0 else ""
        sig  = t["signal_dt"].strftime("%m/%d") if t.get("signal_dt") else "─────"
        print(f"  #{i:>2} {sig}→{t['entry_dt'].strftime('%m/%d')}→"
              f"{t['exit_dt'].strftime('%m/%d')} "
              f"({t['hold_days']:>2}日) "
              f"{sign}{t['pnl']:>9,.0f}円  |{bar}")


# ── CLI ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="スイングトレード バックテスト + Telegram 通知テスト")
    parser.add_argument("--days",      type=int,   default=365,
                        help="バックテスト日数（デフォルト: 365）")
    parser.add_argument("--start",     type=str,   default=None,
                        help="開始日 YYYY-MM-DD（指定時は --days 無視）")
    parser.add_argument("--end",       type=str,   default=None,
                        help="終了日 YYYY-MM-DD（デフォルト: 今日）")
    parser.add_argument("--no-notify", action="store_true",
                        help="Telegram 通知を送らず画面表示のみ（動作確認用）")
    args = parser.parse_args()

    end_date = args.end or datetime.today().strftime("%Y-%m-%d")
    if args.start:
        start_date = args.start
    else:
        start_date = (datetime.strptime(end_date, "%Y-%m-%d")
                      - timedelta(days=args.days)).strftime("%Y-%m-%d")

    dry_run = args.no_notify or not TELEGRAM_BOT_TOKEN

    print("=" * 60)
    print(f"  スイングトレード バックテスト + Telegram 通知テスト")
    print(f"  銘柄   : {SYMBOL_NAME} ({SYMBOL})")
    print(f"  期間   : {start_date} 〜 {end_date}")
    print(f"  通知   : {'DRY RUN（コンソール出力のみ）' if dry_run else 'Telegram 送信あり'}")
    print("=" * 60 + "\n")

    # 1. データ取得 & インジケーター
    df_all = fetch_data(start_date, end_date)
    df_all = add_indicators(df_all)
    df_all = add_signals(df_all)

    # 2. バックテスト対象期間を切り出し
    df_target = df_all[
        (df_all.index >= start_date) & (df_all.index <= end_date)
    ].copy()

    print(f"\nウォームアップ: {len(df_all) - len(df_target):,} 本")
    print(f"バックテスト対象: {len(df_target)} 営業日 "
          f"({df_target.index[0].date()} 〜 {df_target.index[-1].date()})")
    print("\nシグナル検出中 (シグナル発生ごとに通知送信)...\n")

    # 3. バックテスト実行（通知含む）
    trades, equity = backtest(df_target, dry_run=dry_run)

    # 4. 指標計算 & レポート
    metrics = calc_metrics(trades, equity, len(df_target))
    report(df_target, trades, equity, metrics)

    # 5. バックテスト結果サマリーを Telegram 送信
    if metrics:
        summary_msg = build_summary_msg(metrics, start_date, end_date)
        print("\n最終サマリーを Telegram に送信中...")
        send_telegram(summary_msg, dry_run=dry_run)

    print("\n完了。スマホの Telegram を確認してください。")


if __name__ == "__main__":
    main()
