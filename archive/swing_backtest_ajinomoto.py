"""
スイングトレード バックテスト【直近4ヶ月・現物取引版】
銘柄  : 味の素 (2802.T)
データ: yfinance 日足・取得可能な最大期間（ウォームアップ用）

■ 戦略（海外クオンツサイト参考: QuantConnect / Quantopian スタイル）
  エントリー条件（AND）:
    1. 終値 > EMA(200)          ─ トレンドフィルター（上昇トレンドのみロング）
    2. RSI(14) < 40             ─ 押し目・売られすぎ
    3. 終値 ≤ BB下限 + 1σ幅    ─ ボリンジャー下限付近
  エグジット条件（OR）:
    A. RSI(14) > 65             ─ 買われすぎ
    B. 終値 ≥ BB上限            ─ 上限タッチ
    C. EMA(5) < EMA(20)        ─ 短期下方クロス
    D. 当日安値 ≤ ストップ価格 ─ ATR × 2.5 ハードストップ

■ ポジションサイジング（ATRベース）:
    リスク額   = 残余資金 × RISK_PER_TRADE（1.5%）
    ストップ幅 = ATR(14) × ATR_STOP_MULT（2.5）
    取引株数   = リスク額 ÷ ストップ幅 → LOT_SIZE単位に切り捨て

■ 現物取引ルール:
  - 初期資金     : 50 万円
  - 売買単位     : 100 株（1 単元）
  - ロングのみ（現物はショート不可）
  - 手数料       : 0 円（ネット証券の無料プラン想定）
  - 信用・レバレッジなし

■ 実行方法:
  pip install yfinance pandas numpy
  python swing_backtest_4month.py

■ 参考:
  - QuantConnect: https://www.quantconnect.com/
  - ATR Position Sizing: https://quantstrategy.io/blog/using-atr-to-adjust-position-size-volatility-based-risk/
  - Backtesting Metrics: https://quantstrategy.io/blog/essential-backtesting-metrics-understanding-drawdown-sharpe/

⚠ 注意: バックテストは過去データに基づくものであり、将来の利益を保証しません。
"""

import math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL          = "2802.T"             # 味の素
END_DATE        = datetime.today().strftime("%Y-%m-%d")
BACKTEST_DAYS   = 120               # バックテスト対象: 直近4ヶ月

# EMA
EMA_FAST        = 5
EMA_MID         = 20
EMA_SLOW        = 50                # トレンドフィルター（200→50 で緩和）

# RSI
RSI_PERIOD      = 14
RSI_ENTRY       = 55                # これ以下でエントリー候補（40→55 で緩和）
RSI_EXIT        = 60                # これ以上でエグジット（65→60 で早め利確）

# Bollinger Bands
BB_PERIOD       = 20
BB_K            = 2.0

# ATR
ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.5               # ストップロス = ATR × 1.5（2.5→1.5 で資金回転向上）

# リスク管理
RISK_PER_TRADE  = 0.03              # 1トレードあたりのリスク許容 3%（50万円対応）
INITIAL_CASH    = 500_000           # 50 万円
LOT_SIZE        = 100               # 1 単元 = 100 株
MAX_QTY         = 500               # 最大取引株数

RISK_FREE_RATE  = 0.001             # 無リスク金利（年率0.1% ≈ 日本国債）


# ── データ取得 ────────────────────────────────────────────────
def fetch_data() -> pd.DataFrame:
    import yfinance as yf

    print(f"[yfinance] {SYMBOL} 最大期間の日足を取得中...")
    df = yf.download(SYMBOL, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("データが空でした（ネット接続を確認してください）")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    print(f"[yfinance] 取得成功: {len(df):,} 本  "
          f"({df.index[0].date()} 〜 {df.index[-1].date()})\n")
    return df


# ── インジケーター計算（pandas ベクトル化） ───────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # EMA
    df["ema_fast"] = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["ema_mid"]  = c.ewm(span=EMA_MID,   adjust=False).mean()
    df["ema_slow"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()

    # EMA ゴールデンクロス（fast が mid を上抜けた瞬間）
    df["ema_cross_up"] = (
        (df["ema_fast"] > df["ema_mid"]) &
        (df["ema_fast"].shift(1) <= df["ema_mid"].shift(1))
    )

    # RSI
    delta   = c.diff()
    gain    = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    loss    = (-delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    # Bollinger Bands
    sma         = c.rolling(BB_PERIOD).mean()
    std         = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std           # 1σ幅（エントリー判定用）

    # ATR（True Range の EMA）
    prev_c      = c.shift(1)
    tr          = pd.concat([h - l,
                             (h - prev_c).abs(),
                             (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"]   = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── エントリー / エグジットシグナル ──────────────────────────
def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    # トレンドフィルター: EMA50 上（上昇トレンド中のみロング）
    trend_up = df["close"] > df["ema_slow"]

    # シグナルA: RSI 押し目（売られすぎからの反発）
    sig_rsi = df["rsi"] < RSI_ENTRY

    # シグナルB: EMA ゴールデンクロス（短期上抜け）
    sig_cross = df["ema_cross_up"]

    # シグナルC: ボリンジャー下限タッチ（平均回帰）
    sig_bb = df["close"] <= df["bb_lower"] + df["bb_band"]

    # エントリー: トレンド方向 AND (A or B or C のどれか1つ)
    df["entry_sig"] = trend_up & (sig_rsi | sig_cross | sig_bb)

    # エグジット: RSI 高値 OR BB上限 OR 短期クロス下
    df["exit_sig"] = (
        (df["rsi"]      > RSI_EXIT) |
        (df["close"]    >= df["bb_upper"]) |
        (df["ema_fast"] < df["ema_mid"])
    )
    return df


# ── 取引株数計算（ATR ポジションサイジング） ─────────────────
def calc_qty(cash: float, atr: float) -> int:
    risk_amount  = cash * RISK_PER_TRADE
    stop_dist    = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    raw_qty = int(risk_amount / stop_dist)
    lots    = min(raw_qty // LOT_SIZE, MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── バックテスト ──────────────────────────────────────────────
def backtest(df: pd.DataFrame) -> tuple[list[dict], list[tuple]]:
    """
    df: バックテスト対象期間のみ（インジケーター計算済み）
    returns: (trades, equity_curve)

    ■ 執行モデル（翌営業日始値）:
      シグナル発生日の終値確定後 → 人間が翌朝に成行注文 → 翌日始値で約定
      エントリー / シグナルエグジット / ストップロス すべて翌日始値
      ※ ギャップダウンでストップを割り込んだ場合はその日の始値で執行（スリッページ再現）
    """
    trades      = []
    equity      = [(df.index[0], float(INITIAL_CASH))]
    cash        = float(INITIAL_CASH)

    in_pos      = False
    entry_price = 0.0
    stop_price  = 0.0
    entry_dt    = None
    signal_dt   = None          # エントリーシグナル発生日
    qty         = 0

    # ── 翌日執行用フラグ ─────────────────────────────────────
    pending_entry     = False
    pending_entry_atr = 0.0
    pending_entry_sig_dt = None  # シグナル発生日（記録用）

    pending_exit      = False
    pending_exit_reason = ""
    pending_exit_sig_dt = None   # シグナル発生日（記録用）

    for dt, row in df.iterrows():
        if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"],
                        row["bb_upper"], row["bb_lower"]])):
            continue

        # ── 翌日始値：ペンディングエントリー執行 ────────────
        if pending_entry and not in_pos:
            new_qty = calc_qty(cash, pending_entry_atr)
            if new_qty > 0:
                ep   = row["open"]                          # 翌日始値で約定
                sp   = ep - pending_entry_atr * ATR_STOP_MULT
                cost = ep * new_qty
                if cost <= cash:
                    cash       -= cost
                    entry_price = ep
                    stop_price  = sp
                    entry_dt    = dt                        # 執行日
                    signal_dt   = pending_entry_sig_dt      # シグナル発生日
                    qty         = new_qty
                    in_pos      = True
                    equity.append((dt, cash))
            pending_entry     = False
            pending_entry_atr = 0.0

        # ── 翌日始値：ペンディングエグジット執行 ────────────
        if pending_exit and in_pos:
            # ギャップリスク: 始値がストップより低ければ始値で執行
            exit_price  = row["open"]
            pnl         = (exit_price - entry_price) * qty
            cash       += exit_price * qty
            trades.append({
                "signal_dt":   pending_exit_sig_dt,
                "entry_dt":    entry_dt,
                "exit_dt":     dt,                         # 執行日
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

        # ── ポジション保有中：当日終値でシグナル判定 → 翌日執行 ──
        if in_pos and not pending_exit:
            # ハードストップ（当日安値でトリガー判定 → 翌日始値執行）
            if row["low"] <= stop_price:
                pending_exit          = True
                pending_exit_reason   = f"ストップロス(ATR×{ATR_STOP_MULT})"
                pending_exit_sig_dt   = dt
            # シグナルエグジット
            elif row["exit_sig"]:
                pending_exit          = True
                pending_exit_reason   = "シグナル"
                pending_exit_sig_dt   = dt

        # ── 新規エントリー：当日終値でシグナル判定 → 翌日始値執行 ──
        if not in_pos and not pending_entry and row["entry_sig"]:
            pending_entry         = True
            pending_entry_atr     = row["atr"]
            pending_entry_sig_dt  = dt

    # 最終バーで未決済を強制クローズ（終値）
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

    return trades, equity


# ── パフォーマンス指標 ────────────────────────────────────────
def calc_metrics(trades: list[dict], equity: list[tuple],
                 n_days: int) -> dict:
    if not trades:
        return {}

    final      = trades[-1]["cash"]
    total_pnl  = final - INITIAL_CASH
    years      = n_days / 252
    cagr       = (final / INITIAL_CASH) ** (1 / years) - 1 if years > 0 else 0

    # ドローダウン
    vals       = [v for _, v in equity]
    peak       = vals[0]
    max_dd     = 0.0
    for v in vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak)

    calmar = cagr / abs(max_dd) if max_dd != 0 else float("inf")

    # 日次リターン（エクイティカーブから）
    eq_df      = pd.Series(dict(equity)).sort_index()
    eq_daily   = eq_df.resample("B").last().ffill()
    daily_ret  = eq_daily.pct_change().dropna()
    rf_daily   = RISK_FREE_RATE / 252
    excess_ret = daily_ret - rf_daily
    sharpe     = (excess_ret.mean() / excess_ret.std() * math.sqrt(252)
                  if excess_ret.std() > 0 else 0)

    wins       = [t for t in trades if t["pnl"] > 0]
    losses     = [t for t in trades if t["pnl"] <= 0]
    avg_w      = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_l      = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    avg_hold   = sum(t["hold_days"] for t in trades) / len(trades)

    return {
        "final":    final,
        "total_pnl": total_pnl,
        "ret_pct":  total_pnl / INITIAL_CASH * 100,
        "cagr":     cagr * 100,
        "sharpe":   sharpe,
        "max_dd":   max_dd * 100,
        "calmar":   calmar,
        "n_trades": len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_w":    avg_w,
        "avg_l":    avg_l,
        "payoff":   abs(avg_w / avg_l) if avg_l else float("inf"),
        "avg_hold": avg_hold,
    }


# ── レポート ──────────────────────────────────────────────────
DAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

def report(df_target: pd.DataFrame, trades: list[dict],
           equity: list[tuple], metrics: dict):
    W = 82
    start = df_target.index[0].strftime("%Y-%m-%d")
    end   = df_target.index[-1].strftime("%Y-%m-%d")

    print("=" * W)
    print(f"  スイングトレード バックテスト【直近4ヶ月・現物取引版】  {SYMBOL} (味の素)")
    print("=" * W)
    print(f"  期間         : {start} 〜 {end}（{len(df_target)} 営業日）")
    print(f"  データ       : yfinance 日足（最大期間取得・ウォームアップ済み）")
    print(f"  戦略         : EMA({EMA_FAST}/{EMA_MID}/{EMA_SLOW}) + RSI({RSI_PERIOD}) "
          f"+ BB({BB_PERIOD},{BB_K}σ) + ATR({ATR_PERIOD})")
    print(f"  エントリー   : 終値>EMA{EMA_SLOW}（トレンド） かつ "
          f"[RSI<{RSI_ENTRY} or EMAクロス or BB下限タッチ]")
    print(f"  エグジット   : RSI>{RSI_EXIT} or BB上限 or EMA短期クロス or ATR×{ATR_STOP_MULT}ストップ")
    print(f"  ★執行モデル  : シグナル発生翌営業日の始値で成行執行（人間が翌朝注文）")
    print(f"  ポジション   : リスク{RISK_PER_TRADE*100:.1f}%÷ATR×{ATR_STOP_MULT}で株数算出")
    print(f"  初期資金     : {INITIAL_CASH:,.0f} 円")
    print("=" * W)

    # ── トレード一覧 ─────────────────────────────────────────
    if not trades:
        print("\n  ※ 期間内にトレードは発生しませんでした")
    else:
        print(f"\n  ※ エントリー/エグジット日は【執行日（翌営業日始値）】")
        print(f"\n{'#':>3}  {'シグナル':8} {'エントリー':10} {'エグジット':10} "
              f"{'保有':>4} {'株数':>4} {'取得値':>7} {'決済値':>7} "
              f"{'ストップ':>7} {'損益':>10} {'資金残高':>13}  メモ")
        print("-" * (W + 10))
        for i, t in enumerate(trades, 1):
            e_dow   = DAY_JP[t["entry_dt"].weekday()]
            x_dow   = DAY_JP[t["exit_dt"].weekday()]
            sign    = "+" if t["pnl"] >= 0 else ""
            sig_str = (t["signal_dt"].strftime("%m/%d")
                       if t.get("signal_dt") else "  ─  ")
            print(
                f"  {i:>2}  "
                f"{sig_str}→     "
                f"{t['entry_dt'].strftime('%m/%d')}({e_dow})    "
                f"{t['exit_dt'].strftime('%m/%d')}({x_dow})    "
                f"{t['hold_days']:>3}日  "
                f"{t['qty']:>4}株  "
                f"{t['entry_price']:>7,.0f}  "
                f"{t['exit_price']:>7,.0f}  "
                f"{t['stop_price']:>7,.0f}  "
                f"{sign}{t['pnl']:>9,.0f}円  "
                f"{t['cash']:>13,.0f}円  "
                f"{t['note']}"
            )

    if not metrics:
        return

    # ── サマリー ────────────────────────────────────────────
    m = metrics
    print("\n" + "=" * W)
    print(f"  【直近4ヶ月サマリー】（スイングトレード・現物）")
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
    print(f"  平均保有日数         : {m['avg_hold']:>13.1f} 日")
    print("=" * W)

    # ── 損益バーチャート ─────────────────────────────────────
    if trades:
        max_abs = max(abs(t["pnl"]) for t in trades) or 1
        print(f"\n--- トレード別損益チャート（シグナル日→執行日） ---")
        for i, t in enumerate(trades, 1):
            w       = max(1, int(abs(t["pnl"]) / max_abs * 38))
            bar     = ("▪" if t["pnl"] >= 0 else "▫") * w
            sign    = "+" if t["pnl"] >= 0 else ""
            sig_str = (t["signal_dt"].strftime("%m/%d")
                       if t.get("signal_dt") else "─────")
            print(f"  #{i:>2} sig:{sig_str}→exec:{t['entry_dt'].strftime('%m/%d')}→"
                  f"{t['exit_dt'].strftime('%m/%d')} "
                  f"({t['hold_days']:>2}日) "
                  f"{sign}{t['pnl']:>9,.0f}円  |{bar}")


# ── エントリーポイント ────────────────────────────────────────
if __name__ == "__main__":
    # 1. 最大期間データ取得
    df_all = fetch_data()

    # 2. インジケーター計算（全期間・ベクトル化）
    df_all = add_indicators(df_all)
    df_all = add_signals(df_all)

    # 3. バックテスト対象期間を切り出し
    cutoff     = pd.Timestamp(datetime.today() - timedelta(days=BACKTEST_DAYS))
    df_target  = df_all[df_all.index >= cutoff].copy()

    print(f"ウォームアップ: {len(df_all) - len(df_target):,} 本")
    print(f"バックテスト対象: {len(df_target)} 営業日 "
          f"({df_target.index[0].date()} 〜 {df_target.index[-1].date()})\n")
    print("バックテスト実行中...")

    # 4. バックテスト実行
    trades, equity = backtest(df_target)
    print(f"完了: {len(trades)} トレード\n")

    # 5. 指標計算 & レポート
    metrics = calc_metrics(trades, equity, len(df_target))
    report(df_target, trades, equity, metrics)
