"""
直近2ヶ月 スイングトレード 銘柄スキャナー
────────────────────────────────────────
日経225 主要50銘柄を一括バックテストして利益率ランキングを表示します。

■ Google Colab での実行方法:
  1. https://colab.research.google.com/ を開く
  2. 「新しいノートブック」を作成
  3. 最初のセルに以下を貼り付けて実行:
       !pip install yfinance pandas numpy -q
  4. 次のセルにこのファイルの内容をすべて貼り付けて実行

■ ローカルPCでの実行方法:
  pip install yfinance pandas numpy
  python scan_best_stock.py
"""

import math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ── パラメータ（swing_backtest_4month.py と同設定） ──────────
BACKTEST_DAYS  = 365                # 直近1年
EMA_FAST       = 5
EMA_MID        = 20
EMA_SLOW       = 50
RSI_PERIOD     = 14
RSI_ENTRY      = 55
RSI_EXIT       = 60
BB_PERIOD      = 20
BB_K           = 2.0
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PER_TRADE = 0.03               # 3%（50万円対応に引き上げ）
INITIAL_CASH   = 500_000
LOT_SIZE       = 100
MAX_QTY        = 500

# ── 日経225 主要50銘柄 ────────────────────────────────────────
SYMBOLS = [
    ("7203.T", "トヨタ自動車"),
    ("9984.T", "ソフトバンクG"),
    ("6758.T", "ソニーG"),
    ("7974.T", "任天堂"),
    ("6861.T", "キーエンス"),
    ("8306.T", "三菱UFJ"),
    ("9432.T", "NTT"),
    ("9433.T", "KDDI"),
    ("4502.T", "武田薬品"),
    ("8035.T", "東京エレクトロン"),
    ("5401.T", "日本製鉄"),
    ("8316.T", "三井住友FG"),
    ("2914.T", "JT"),
    ("9020.T", "JR東日本"),
    ("4661.T", "オリエンタルランド"),
    ("6367.T", "ダイキン工業"),
    ("4063.T", "信越化学工業"),
    ("6954.T", "ファナック"),
    ("6098.T", "リクルートHD"),
    ("3382.T", "セブン&アイHD"),
    ("8058.T", "三菱商事"),
    ("8031.T", "三井物産"),
    ("8001.T", "伊藤忠商事"),
    ("8002.T", "丸紅"),
    ("9022.T", "JR東海"),
    ("6902.T", "デンソー"),
    ("7751.T", "キヤノン"),
    ("4519.T", "中外製薬"),
    ("4568.T", "第一三共"),
    ("7267.T", "ホンダ"),
    ("7269.T", "スズキ"),
    ("7201.T", "日産自動車"),
    ("6301.T", "小松製作所"),
    ("6326.T", "クボタ"),
    ("5108.T", "ブリヂストン"),
    ("4543.T", "テルモ"),
    ("2802.T", "味の素"),
    ("2269.T", "明治HD"),
    ("4911.T", "資生堂"),
    ("8267.T", "イオン"),
    ("3産.T", "ファーストリテイリング"),
    ("9983.T", "ファーストリテイリング"),
    ("6752.T", "パナソニックHD"),
    ("6702.T", "富士通"),
    ("6701.T", "NEC"),
    ("9613.T", "NTTデータG"),
    ("8604.T", "野村HD"),
    ("8725.T", "MS&ADインシュアランス"),
    ("8750.T", "第一生命HD"),
    ("1925.T", "大和ハウス工業"),
]
# 重複・誤コード除去
SYMBOLS = [(c, n) for c, n in SYMBOLS if "産.T" not in c]


# ── インジケーター + シグナル計算 ────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]; h = df["high"]; l = df["low"]

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

    sma = c.rolling(BB_PERIOD).mean()
    std = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()],
                   axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    trend_up = df["close"] > df["ema_slow"]
    df["entry_sig"] = trend_up & (
        (df["rsi"] < RSI_ENTRY) |
        df["ema_cross_up"] |
        (df["close"] <= df["bb_lower"] + df["bb_band"])
    )
    df["exit_sig"] = (
        (df["rsi"]      > RSI_EXIT) |
        (df["close"]    >= df["bb_upper"]) |
        (df["ema_fast"] < df["ema_mid"])
    )
    return df


# ── 1銘柄バックテスト ────────────────────────────────────────
def run_backtest(symbol: str, name: str) -> dict | None:
    import yfinance as yf

    try:
        df = yf.download(symbol, period="max", interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception:
        return None

    df = calc(df)

    cutoff    = pd.Timestamp(datetime.today() - timedelta(days=BACKTEST_DAYS))
    df_target = df[df.index >= cutoff].copy()
    if len(df_target) < 5:
        return None

    in_pos = False
    cash   = float(INITIAL_CASH)
    trades = []
    entry_price = stop_price = 0.0
    entry_dt = None
    qty = 0

    for dt, row in df_target.iterrows():
        if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"]])):
            continue

        if in_pos:
            reason = exit_p = None
            if row["low"] <= stop_price:
                exit_p = stop_price
                reason = "ストップ"
            elif row["exit_sig"]:
                exit_p = row["close"]
                reason = "シグナル"
            if reason:
                pnl   = (exit_p - entry_price) * qty
                cash += exit_p * qty
                trades.append({
                    "pnl":       pnl,
                    "hold_days": (dt - entry_dt).days,
                    "note":      reason,
                })
                in_pos = False

        if not in_pos and row["entry_sig"]:
            risk_amt  = cash * RISK_PER_TRADE
            stop_dist = row["atr"] * ATR_STOP_MULT
            if stop_dist > 0:
                raw = int(risk_amt / stop_dist)
                q   = min(raw // LOT_SIZE, MAX_QTY // LOT_SIZE) * LOT_SIZE
                if q > 0 and row["close"] * q <= cash:
                    cash       -= row["close"] * q
                    entry_price = row["close"]
                    stop_price  = row["close"] - stop_dist
                    entry_dt    = dt
                    qty         = q
                    in_pos      = True

    if in_pos:
        lp   = df_target.iloc[-1]["close"]
        pnl  = (lp - entry_price) * qty
        cash += lp * qty
        trades.append({"pnl": pnl, "hold_days": (df_target.index[-1] - entry_dt).days,
                        "note": "最終日"})

    if not trades:
        return None

    final    = cash
    total    = final - INITIAL_CASH
    ret_pct  = total / INITIAL_CASH * 100
    wins     = [t for t in trades if t["pnl"] > 0]
    losses   = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100

    return {
        "symbol":   symbol,
        "name":     name,
        "trades":   len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "win_rate": win_rate,
        "total":    total,
        "ret_pct":  ret_pct,
        "final":    final,
    }


# ── メイン ───────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print(f"  直近1年 スイングトレード 銘柄スキャン")
    print(f"  初期資金: {INITIAL_CASH:,}円  リスク: {RISK_PER_TRADE*100:.0f}%/トレード")
    print(f"  対象: {len(SYMBOLS)}銘柄  期間: 直近{BACKTEST_DAYS}日")
    print("=" * 70)

    results = []
    for i, (symbol, name) in enumerate(SYMBOLS, 1):
        print(f"  [{i:>2}/{len(SYMBOLS)}] {symbol} {name} ...", end=" ", flush=True)
        r = run_backtest(symbol, name)
        if r:
            results.append(r)
            sign = "+" if r["ret_pct"] >= 0 else ""
            print(f"{r['trades']}回  {sign}{r['ret_pct']:.1f}%")
        else:
            print("スキップ（データなし or トレードなし）")

    # 利益率順にソート
    results.sort(key=lambda x: x["ret_pct"], reverse=True)

    W = 70
    print("\n" + "=" * W)
    print("  【利益率ランキング】直近1年")
    print("=" * W)
    print(f"  {'順位':>3}  {'銘柄':8}  {'名称':16}  {'回数':>4}  {'勝率':>5}  "
          f"{'損益':>10}  {'利益率':>7}")
    print("-" * W)

    for rank, r in enumerate(results, 1):
        sign = "+" if r["ret_pct"] >= 0 else ""
        pnl_sign = "+" if r["total"] >= 0 else ""
        print(f"  {rank:>3}位  {r['symbol']:8}  {r['name']:16}  "
              f"{r['trades']:>4}回  {r['win_rate']:>4.0f}%  "
              f"{pnl_sign}{r['total']:>9,.0f}円  {sign}{r['ret_pct']:>6.1f}%")

    print("=" * W)
    if results:
        best = results[0]
        print(f"\n  ★ 最高利益率: {best['name']} ({best['symbol']})")
        print(f"     利益率: {best['ret_pct']:+.2f}%  "
              f"損益: {best['total']:+,.0f}円  "
              f"トレード数: {best['trades']}回  "
              f"勝率: {best['win_rate']:.0f}%")
