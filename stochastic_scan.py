"""
ストキャスティクス 銘柄スキャナー（日経225）
────────────────────────────────────────
A7: トレンドフィルター付きストキャスティクス + ATRトレイリングストップ

■ ストキャスティクス戦略:
  エントリー: スロー%K が %D を下から上にクロス（ゴールデンクロス）
              かつ %K が過売り圏（< STOCH_OVERSOLD=30）から脱出
              かつ 終値 > 75日移動平均線（上昇トレンド確認）
  エグジット:  スロー%K が %D を上から下にクロス（デッドクロス）
              または ATRトレイリングストップ発動（ATR×2.0・含み益に応じて切り上げ）

■ 実行方法:
  pip install yfinance pandas numpy
  python stochastic_scan.py
  python stochastic_scan.py --demo   # ネット不要のデモモード
"""

import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
BACKTEST_DAYS    = 365          # 直近1年
STOCH_K_PERIOD   = 14           # %K の計算期間
STOCH_D_PERIOD   = 3            # %D (シグナル線) の平滑化期間
STOCH_SMOOTH     = 3            # %K の平滑化（スロー版）
STOCH_OVERSOLD   = 30           # 過売りライン
STOCH_OVERBOUGHT = 70           # 過買いライン
ATR_PERIOD       = 14
ATR_STOP_MULT    = 1.5          # 初期ストップロス倍率
ATR_TRAIL_MULT   = 2.0          # トレイリングストップ倍率
MA_TREND_PERIOD  = 75           # トレンドフィルター移動平均期間
RISK_PER_TRADE   = 0.03         # 3%
INITIAL_CASH     = 500_000
LOT_SIZE         = 100
MAX_QTY          = 500

# ── 日経225 主要銘柄 ────────────────────────────────────────
SYMBOLS = [
    ("7203.T",  "トヨタ自動車"),
    ("9984.T",  "ソフトバンクG"),
    ("6758.T",  "ソニーG"),
    ("7974.T",  "任天堂"),
    ("6861.T",  "キーエンス"),
    ("8306.T",  "三菱UFJ"),
    ("9432.T",  "NTT"),
    ("9433.T",  "KDDI"),
    ("4502.T",  "武田薬品"),
    ("8035.T",  "東京エレクトロン"),
    ("5401.T",  "日本製鉄"),
    ("8316.T",  "三井住友FG"),
    ("2914.T",  "JT"),
    ("9020.T",  "JR東日本"),
    ("4661.T",  "オリエンタルランド"),
    ("6367.T",  "ダイキン工業"),
    ("4063.T",  "信越化学工業"),
    ("6954.T",  "ファナック"),
    ("6098.T",  "リクルートHD"),
    ("3382.T",  "セブン&アイHD"),
    ("8058.T",  "三菱商事"),
    ("8031.T",  "三井物産"),
    ("8001.T",  "伊藤忠商事"),
    ("8002.T",  "丸紅"),
    ("9022.T",  "JR東海"),
    ("6902.T",  "デンソー"),
    ("7751.T",  "キヤノン"),
    ("4519.T",  "中外製薬"),
    ("4568.T",  "第一三共"),
    ("7267.T",  "ホンダ"),
    ("7269.T",  "スズキ"),
    ("7201.T",  "日産自動車"),
    ("6301.T",  "小松製作所"),
    ("6326.T",  "クボタ"),
    ("5108.T",  "ブリヂストン"),
    ("4543.T",  "テルモ"),
    ("2802.T",  "味の素"),
    ("2269.T",  "明治HD"),
    ("4911.T",  "資生堂"),
    ("8267.T",  "イオン"),
    ("9983.T",  "ファーストリテイリング"),
    ("6752.T",  "パナソニックHD"),
    ("6702.T",  "富士通"),
    ("6701.T",  "NEC"),
    ("9613.T",  "NTTデータG"),
    ("8604.T",  "野村HD"),
    ("8725.T",  "MS&ADインシュアランス"),
    ("8750.T",  "第一生命HD"),
    ("1925.T",  "大和ハウス工業"),
    ("9021.T",  "JR西日本"),
    ("3407.T",  "旭化成"),
    ("4901.T",  "富士フイルムHD"),
    ("6471.T",  "NSK"),
    ("6503.T",  "三菱電機"),
    ("6506.T",  "安川電機"),
    ("7733.T",  "オリンパス"),
    ("4452.T",  "花王"),
    ("2503.T",  "キリンHD"),
    ("2502.T",  "アサヒGHD"),
    ("4005.T",  "住友化学"),
]


# ── ストキャスティクス計算 ────────────────────────────────────
def calc_stochastic(df: pd.DataFrame) -> pd.DataFrame:
    """
    スロー・ストキャスティクスを計算する（A7: トレンドフィルター付き）
      FastK = (Close - LowestLow[K]) / (HighestHigh[K] - LowestLow[K]) * 100
      SlowK = SMA(FastK, smooth)   ← スロー化
      SlowD = SMA(SlowK, D_period) ← シグナル線
      MA75  = SMA(Close, 75)       ← トレンドフィルター
    エントリー: ゴールデンクロス (SlowK が SlowD を上抜け)
               かつ SlowK < STOCH_OVERBOUGHT（過買い追いかけ防止）
               かつ 終値 > 75MA（上昇トレンド確認）
    エグジット:  デッドクロス (SlowK が SlowD を下抜け)
               または ATRトレイリングストップ発動
    """
    h = df["high"]
    l = df["low"]
    c = df["close"]

    lowest_low   = l.rolling(STOCH_K_PERIOD).min()
    highest_high = h.rolling(STOCH_K_PERIOD).max()
    denom        = highest_high - lowest_low

    fast_k = (c - lowest_low) / denom.replace(0, np.nan) * 100
    slow_k = fast_k.rolling(STOCH_SMOOTH).mean()
    slow_d = slow_k.rolling(STOCH_D_PERIOD).mean()

    # 75日移動平均線（トレンドフィルター）
    ma75 = c.rolling(MA_TREND_PERIOD).mean()

    df["stoch_k"] = slow_k
    df["stoch_d"] = slow_d
    df["ma75"]    = ma75

    prev_k = slow_k.shift(1)
    prev_d = slow_d.shift(1)

    df["golden_cross"] = (slow_k > slow_d) & (prev_k <= prev_d)
    df["dead_cross"]   = (slow_k < slow_d) & (prev_k >= prev_d)

    # エントリー: ゴールデンクロス かつ 過買い圏でない かつ 75MA上（トレンドフィルター）
    df["entry_sig"] = df["golden_cross"] & (slow_k < STOCH_OVERBOUGHT) & (c > ma75)
    # エグジット: デッドクロス
    df["exit_sig"]  = df["dead_cross"]

    # ATR（ストップロス計算用）
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── デモ用疑似データ生成 ────────────────────────────────────
def make_demo_data(symbol: str, seed: int, base_price: float = 2000.0,
                   drift: float = 0.0002) -> pd.DataFrame:
    """再現性のある疑似株価データを生成する。"""
    rng  = np.random.default_rng(seed)
    days = BACKTEST_DAYS + 60
    dates = pd.date_range(end=datetime.today(), periods=days, freq="B")

    returns = rng.normal(drift, 0.015, size=days)
    close   = base_price * np.cumprod(1 + returns)
    noise   = rng.uniform(0.995, 1.005, size=days)
    high    = close * rng.uniform(1.000, 1.025, size=days)
    low     = close * rng.uniform(0.975, 1.000, size=days)
    open_   = close * noise

    df = pd.DataFrame({
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": rng.integers(500_000, 5_000_000, size=days).astype(float),
    }, index=dates)
    return df


# ── 1銘柄バックテスト ────────────────────────────────────────
def run_backtest(symbol: str, name: str, demo: bool = False,
                 demo_seed: int = 42) -> dict | None:
    if demo:
        # 銘柄ごとに異なるシードとドリフトで疑似データを生成
        seed       = demo_seed
        base_price = float(hash(symbol) % 5000 + 1000)
        drift      = (demo_seed % 7 - 3) * 0.0001  # -0.0003 ～ +0.0003
        df         = make_demo_data(symbol, seed, base_price, drift)
    else:
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

    df = calc_stochastic(df)

    cutoff    = pd.Timestamp(datetime.today() - timedelta(days=BACKTEST_DAYS))
    df_target = df[df.index >= cutoff].copy()
    min_rows  = STOCH_K_PERIOD + STOCH_SMOOTH + STOCH_D_PERIOD
    if len(df_target) < min_rows:
        return None

    in_pos      = False
    cash        = float(INITIAL_CASH)
    trades      = []
    entry_price = trail_stop = 0.0
    entry_dt    = None
    qty         = 0

    for dt, row in df_target.iterrows():
        if pd.isna(row["stoch_k"]) or pd.isna(row["stoch_d"]) or pd.isna(row["atr"]):
            continue

        # ── ポジション保有中: エグジット判定 ──
        if in_pos:
            reason = exit_p = None
            if row["low"] <= trail_stop:
                exit_p = trail_stop
                reason = "トレイリング"
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
            else:
                # トレイリングストップを切り上げ（下がらない）
                candidate = row["close"] - row["atr"] * ATR_TRAIL_MULT
                if candidate > trail_stop:
                    trail_stop = candidate

        # ── ポジションなし: エントリー判定 ──
        if not in_pos and row["entry_sig"]:
            risk_amt  = cash * RISK_PER_TRADE
            stop_dist = row["atr"] * ATR_STOP_MULT
            if stop_dist > 0:
                raw = int(risk_amt / stop_dist)
                q   = min(raw // LOT_SIZE, MAX_QTY // LOT_SIZE) * LOT_SIZE
                if q > 0 and row["close"] * q <= cash:
                    cash        -= row["close"] * q
                    entry_price  = row["close"]
                    # 初期トレイリングストップ = 終値 − ATR × TRAIL_MULT
                    trail_stop   = row["close"] - row["atr"] * ATR_TRAIL_MULT
                    entry_dt     = dt
                    qty          = q
                    in_pos       = True

    # 未決済ポジションを最終日終値で決済
    if in_pos:
        lp   = df_target.iloc[-1]["close"]
        pnl  = (lp - entry_price) * qty
        cash += lp * qty
        trades.append({
            "pnl":       pnl,
            "hold_days": (df_target.index[-1] - entry_dt).days,
            "note":      "最終日",
        })

    if not trades:
        return None

    final    = cash
    total    = final - INITIAL_CASH
    ret_pct  = total / INITIAL_CASH * 100
    wins     = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) * 100
    avg_hold = sum(t["hold_days"] for t in trades) / len(trades)

    return {
        "symbol":   symbol,
        "name":     name,
        "trades":   len(trades),
        "wins":     len(wins),
        "losses":   len(trades) - len(wins),
        "win_rate": win_rate,
        "total":    total,
        "ret_pct":  ret_pct,
        "final":    final,
        "avg_hold": avg_hold,
    }


# ── 現在シグナルチェック ─────────────────────────────────────
def check_today_signals(results: list, demo: bool) -> None:
    """ランキング上位銘柄について本日のシグナルを確認する。"""
    import yfinance as yf

    print("  ─" * 37)
    print("  【本日のストキャスティクス シグナル】")
    print("  ─" * 37)

    buy_signals  = []
    sell_signals = []

    for i, r in enumerate(results):
        symbol = r["symbol"]
        name   = r["name"]
        if demo:
            seed       = i + len(symbol)
            base_price = float(hash(symbol) % 5000 + 1000)
            drift      = (i % 7 - 3) * 0.0001
            df = make_demo_data(symbol, seed, base_price, drift)
        else:
            try:
                df = yf.download(symbol, period="3mo", interval="1d",
                                 auto_adjust=True, progress=False)
                if df.empty:
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
                df = df[["open", "high", "low", "close", "volume"]].dropna()
            except Exception:
                continue

        df   = calc_stochastic(df)
        last = df.iloc[-1]
        k    = last["stoch_k"]
        d    = last["stoch_d"]
        if pd.isna(k) or pd.isna(d):
            continue

        price = last["close"]
        if last["entry_sig"]:
            buy_signals.append((name, symbol, k, d, price))
        elif last["exit_sig"]:
            sell_signals.append((name, symbol, k, d, price))

    if buy_signals:
        print(f"\n  ▲ 買いシグナル ({len(buy_signals)}銘柄):")
        for name, sym, k, d, price in buy_signals:
            print(f"     {name:20} ({sym})  %K={k:5.1f}  %D={d:5.1f}  "
                  f"現値={price:>10,.0f}円")
    else:
        print("\n  買いシグナル: なし")

    if sell_signals:
        print(f"\n  ▼ 売りシグナル ({len(sell_signals)}銘柄):")
        for name, sym, k, d, price in sell_signals:
            print(f"     {name:20} ({sym})  %K={k:5.1f}  %D={d:5.1f}  "
                  f"現値={price:>10,.0f}円")
    else:
        print("\n  売りシグナル: なし")
    print()


# ── メイン ───────────────────────────────────────────────────
if __name__ == "__main__":
    demo_mode = "--demo" in sys.argv

    W = 80
    print("=" * W)
    print(f"  ストキャスティクス 銘柄スキャン（日経225）")
    if demo_mode:
        print(f"  ※ デモモード（疑似データ使用）")
    print(f"  アルゴリズム: A7 スロー・ストキャスティクス + トレンドフィルター + ATRトレイリングストップ")
    print(f"  %K={STOCH_K_PERIOD}  %D={STOCH_D_PERIOD}  Smooth={STOCH_SMOOTH}  トレンドフィルター: {MA_TREND_PERIOD}日MA")
    print(f"  エントリー: ゴールデンクロス（%K < {STOCH_OVERBOUGHT}）かつ 終値 > {MA_TREND_PERIOD}日MA")
    print(f"  エグジット:  デッドクロス  |  ATRトレイリングストップ: ATR×{ATR_TRAIL_MULT}（切り上げ式）")
    print(f"  初期資金: {INITIAL_CASH:,}円  リスク/トレード: {RISK_PER_TRADE*100:.0f}%")
    print(f"  対象: {len(SYMBOLS)}銘柄  バックテスト期間: 直近{BACKTEST_DAYS}日")
    print("=" * W)

    results = []
    for i, (symbol, name) in enumerate(SYMBOLS, 1):
        print(f"  [{i:>2}/{len(SYMBOLS)}] {symbol} {name:18} ...", end=" ", flush=True)
        r = run_backtest(symbol, name, demo=demo_mode, demo_seed=i * 7 + 3)
        if r:
            results.append(r)
            sign = "+" if r["ret_pct"] >= 0 else ""
            print(f"{r['trades']:3}回  {sign}{r['ret_pct']:6.1f}%  勝率{r['win_rate']:.0f}%")
        else:
            print("スキップ（データなし or トレードなし）")

    results.sort(key=lambda x: x["ret_pct"], reverse=True)

    print("\n" + "=" * W)
    print("  【ストキャスティクス 利益率ランキング】直近1年")
    print("=" * W)
    header = (f"  {'順位':>3}  {'銘柄':8}  {'名称':20}  "
              f"{'回数':>4}  {'勝率':>5}  {'損益':>11}  {'利益率':>7}  {'平均保有':>7}")
    print(header)
    print("-" * W)

    for rank, r in enumerate(results, 1):
        sign     = "+" if r["ret_pct"] >= 0 else ""
        pnl_sign = "+" if r["total"]   >= 0 else ""
        print(f"  {rank:>3}位  {r['symbol']:8}  {r['name']:20}  "
              f"{r['trades']:>4}回  {r['win_rate']:>4.0f}%  "
              f"{pnl_sign}{r['total']:>10,.0f}円  {sign}{r['ret_pct']:>6.1f}%  "
              f"{r['avg_hold']:>5.1f}日")

    print("=" * W)

    if results:
        top5 = results[:5]
        print(f"\n  ★ TOP5 銘柄（ストキャスティクス戦略）")
        print(f"  {'順位':>3}  {'名称':20}  {'銘柄':8}  {'利益率':>8}  "
              f"{'損益':>11}  {'勝率':>5}  {'回数':>5}")
        print("  " + "-" * 72)
        for rank, r in enumerate(top5, 1):
            print(f"  {rank:>3}位  {r['name']:20}  {r['symbol']:8}  "
                  f"{r['ret_pct']:>+8.2f}%  {r['total']:>+10,.0f}円  "
                  f"{r['win_rate']:>4.0f}%  {r['trades']:>4}回")
        print()

        # 本日のシグナル
        check_today_signals(results, demo=demo_mode)
