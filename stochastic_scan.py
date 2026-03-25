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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# ── 日経225 全銘柄 ──────────────────────────────────────────
WORKERS = 20   # 並列ダウンロード数

SYMBOLS = [
    ("1332.T", "ニッスイ"),
    ("1333.T", "マルハニチロ"),
    ("1605.T", "INPEX"),
    ("1721.T", "コムシスHD"),
    ("1801.T", "大成建設"),
    ("1802.T", "大林組"),
    ("1803.T", "清水建設"),
    ("1808.T", "長谷工コーポレーション"),
    ("1812.T", "鹿島建設"),
    ("1925.T", "大和ハウス工業"),
    ("1928.T", "積水ハウス"),
    ("1963.T", "日揮HD"),
    ("2002.T", "日清製粉グループ本社"),
    ("2269.T", "明治HD"),
    ("2282.T", "日本ハム"),
    ("2413.T", "エムスリー"),
    ("2432.T", "ディー・エヌ・エー"),
    ("2501.T", "サッポロHD"),
    ("2502.T", "アサヒグループHD"),
    ("2503.T", "キリンHD"),
    ("2531.T", "宝HD"),
    ("2768.T", "双日"),
    ("2801.T", "キッコーマン"),
    ("2802.T", "味の素"),
    ("2871.T", "ニチレイ"),
    ("2914.T", "日本たばこ産業"),
    ("3086.T", "J.フロント リテイリング"),
    ("3099.T", "三越伊勢丹HD"),
    ("3105.T", "日清紡HD"),
    ("3289.T", "東急不動産HD"),
    ("3382.T", "セブン&アイHD"),
    ("3401.T", "帝人"),
    ("3402.T", "東レ"),
    ("3405.T", "クラレ"),
    ("3407.T", "旭化成"),
    ("3436.T", "SUMCO"),
    ("3861.T", "王子HD"),
    ("3863.T", "日本製紙"),
    ("4004.T", "レゾナック・HD"),
    ("4005.T", "住友化学"),
    ("4021.T", "日産化学"),
    ("4042.T", "東ソー"),
    ("4043.T", "トクヤマ"),
    ("4061.T", "デンカ"),
    ("4063.T", "信越化学工業"),
    ("4183.T", "三井化学"),
    ("4188.T", "三菱ケミカルグループ"),
    ("4208.T", "UBE"),
    ("4272.T", "日本化薬"),
    ("4307.T", "野村総合研究所"),
    ("4324.T", "電通グループ"),
    ("4502.T", "武田薬品工業"),
    ("4503.T", "アステラス製薬"),
    ("4506.T", "住友ファーマ"),
    ("4507.T", "塩野義製薬"),
    ("4519.T", "中外製薬"),
    ("4523.T", "エーザイ"),
    ("4543.T", "テルモ"),
    ("4568.T", "第一三共"),
    ("4578.T", "大塚HD"),
    ("4631.T", "DIC"),
    ("4689.T", "LINEヤフー"),
    ("4704.T", "トレンドマイクロ"),
    ("4751.T", "サイバーエージェント"),
    ("4755.T", "楽天グループ"),
    ("4901.T", "富士フイルムHD"),
    ("4902.T", "コニカミノルタ"),
    ("5019.T", "出光興産"),
    ("5020.T", "ENEOSホールディングス"),
    ("5101.T", "横浜ゴム"),
    ("5108.T", "ブリヂストン"),
    ("5201.T", "AGC"),
    ("5214.T", "日本電気硝子"),
    ("5232.T", "住友大阪セメント"),
    ("5233.T", "太平洋セメント"),
    ("5301.T", "東海カーボン"),
    ("5332.T", "TOTO"),
    ("5333.T", "日本碍子"),
    ("5401.T", "日本製鉄"),
    ("5406.T", "神戸製鋼所"),
    ("5411.T", "JFEホールディングス"),
    ("5631.T", "日本製鋼所"),
    ("5703.T", "日本軽金属HD"),
    ("5706.T", "三井金属鉱業"),
    ("5707.T", "東邦亜鉛"),
    ("5711.T", "三菱マテリアル"),
    ("5713.T", "住友金属鉱山"),
    ("5714.T", "DOWAホールディングス"),
    ("5741.T", "UACJ"),
    ("5801.T", "古河電気工業"),
    ("5802.T", "住友電気工業"),
    ("5803.T", "フジクラ"),
    ("6098.T", "リクルートHD"),
    ("6103.T", "オークマ"),
    ("6113.T", "アマダ"),
    ("6178.T", "日本郵政"),
    ("6273.T", "SMC"),
    ("6301.T", "小松製作所"),
    ("6302.T", "住友重機械工業"),
    ("6305.T", "日立建機"),
    ("6326.T", "クボタ"),
    ("6361.T", "荏原製作所"),
    ("6367.T", "ダイキン工業"),
    ("6370.T", "栗田工業"),
    ("6383.T", "ダイフク"),
    ("6406.T", "フジテック"),
    ("6412.T", "平和"),
    ("6417.T", "SANKYO"),
    ("6471.T", "日本精工"),
    ("6472.T", "NTN"),
    ("6473.T", "ジェイテクト"),
    ("6479.T", "ミネベアミツミ"),
    ("6501.T", "日立製作所"),
    ("6503.T", "三菱電機"),
    ("6504.T", "富士電機"),
    ("6506.T", "安川電機"),
    ("6526.T", "ソシオネクスト"),
    ("6532.T", "ベイカレント・コンサルティング"),
    ("6586.T", "マキタ"),
    ("6594.T", "ニデック"),
    ("6645.T", "オムロン"),
    ("6674.T", "GSユアサ"),
    ("6701.T", "NEC"),
    ("6702.T", "富士通"),
    ("6723.T", "ルネサスエレクトロニクス"),
    ("6724.T", "セイコーエプソン"),
    ("6752.T", "パナソニックHD"),
    ("6753.T", "シャープ"),
    ("6754.T", "アンリツ"),
    ("6758.T", "ソニーグループ"),
    ("6762.T", "TDK"),
    ("6770.T", "アルプスアルパイン"),
    # ("6773.T", "パイオニア"),     # 上場廃止
    # ("6794.T", "フォスター電機"), # 上場廃止
    # ("6796.T", "クラリオン"),     # 上場廃止
    ("6806.T", "ヒロセ電機"),
    ("6841.T", "横河電機"),
    ("6857.T", "アドバンテスト"),
    ("6861.T", "キーエンス"),
    ("6902.T", "デンソー"),
    ("6952.T", "カシオ計算機"),
    ("6954.T", "ファナック"),
    ("6971.T", "京セラ"),
    ("6981.T", "村田製作所"),
    ("6988.T", "日東電工"),
    ("7003.T", "三井E&S"),
    ("7011.T", "三菱重工業"),
    ("7012.T", "川崎重工業"),
    ("7013.T", "IHI"),
    ("7201.T", "日産自動車"),
    ("7202.T", "いすゞ自動車"),
    ("7203.T", "トヨタ自動車"),
    ("7205.T", "日野自動車"),
    ("7211.T", "三菱自動車工業"),
    ("7261.T", "マツダ"),
    ("7267.T", "本田技研工業"),
    ("7270.T", "SUBARU"),
    ("7272.T", "ヤマハ発動機"),
    ("7731.T", "ニコン"),
    ("7733.T", "オリンパス"),
    ("7735.T", "SCREENホールディングス"),
    ("7741.T", "HOYA"),
    ("7751.T", "キヤノン"),
    ("7752.T", "リコー"),
    ("7762.T", "シチズン時計"),
    ("7832.T", "バンダイナムコHD"),
    ("7951.T", "ヤマハ"),
    ("8001.T", "伊藤忠商事"),
    ("8002.T", "丸紅"),
    ("8015.T", "豊田通商"),
    ("8031.T", "三井物産"),
    ("8035.T", "東京エレクトロン"),
    ("8053.T", "住友商事"),
    ("8058.T", "三菱商事"),
    ("8233.T", "高島屋"),
    ("8252.T", "丸井グループ"),
    ("8253.T", "クレディセゾン"),
    ("8267.T", "イオン"),
    ("8303.T", "新生銀行"),
    ("8304.T", "あおぞら銀行"),
    ("8306.T", "三菱UFJフィナンシャルG"),
    ("8308.T", "りそなHD"),
    ("8309.T", "三井住友トラストHD"),
    ("8316.T", "三井住友フィナンシャルG"),
    ("8331.T", "千葉銀行"),
    ("8354.T", "ふくおかFG"),
    ("8355.T", "静岡銀行"),
    ("8377.T", "ほくほくFG"),
    ("8411.T", "みずほフィナンシャルG"),
    ("8601.T", "大和証券グループ本社"),
    ("8604.T", "野村HD"),
    ("8628.T", "松井証券"),
    ("8630.T", "SOMPOホールディングス"),
    ("8697.T", "日本取引所グループ"),
    ("8725.T", "MS&ADインシュアランスG"),
    ("8750.T", "第一生命HD"),
    # ("8752.T", "三井住友海上グループHD"), # データ不備（合併等）
    ("8766.T", "東京海上HD"),
    ("8795.T", "T&Dホールディングス"),
    ("8801.T", "三井不動産"),
    ("8802.T", "三菱地所"),
    ("8804.T", "東京建物"),
    ("8830.T", "住友不動産"),
    ("9001.T", "東武鉄道"),
    ("9005.T", "東急"),
    ("9007.T", "小田急電鉄"),
    ("9008.T", "京王電鉄"),
    ("9009.T", "京成電鉄"),
    ("9020.T", "東日本旅客鉄道"),
    ("9021.T", "西日本旅客鉄道"),
    ("9022.T", "東海旅客鉄道"),
    ("9064.T", "ヤマトHD"),
    ("9101.T", "日本郵船"),
    ("9104.T", "商船三井"),
    ("9107.T", "川崎汽船"),
    ("9202.T", "ANAホールディングス"),
    ("9301.T", "三菱倉庫"),
    ("9432.T", "日本電信電話"),
    ("9433.T", "KDDI"),
    ("9434.T", "ソフトバンク"),
    ("9501.T", "東京電力HD"),
    ("9502.T", "中部電力"),
    ("9503.T", "関西電力"),
    ("9531.T", "東京ガス"),
    ("9532.T", "大阪ガス"),
    ("9602.T", "東宝"),
    # ("9613.T", "NTTデータグループ"), # データ不備（再編等）
    ("9735.T", "セコム"),
    ("9766.T", "コナミグループ"),
    ("9983.T", "ファーストリテイリング"),
    ("9984.T", "ソフトバンクグループ"),
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
    # MultiIndex残存対策：明示的にSeriesへ変換
    h = df["high"].squeeze()
    l = df["low"].squeeze()
    c = df["close"].squeeze()

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
            # スレッドセーフのため内部ブロックを再構築（Gaps in blk ref_locs 対策）
            df = df.copy()
            if df.empty or len(df) < MA_TREND_PERIOD + 30:
                return None
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
                # 75MA計算に十分なデータを取得するため6ヶ月分取得
                df = yf.download(symbol, period="6mo", interval="1d",
                                 auto_adjust=True, progress=False)
                if df.empty:
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
                df = df[["open", "high", "low", "close", "volume"]].dropna().copy()
                if len(df) < MA_TREND_PERIOD + 10:
                    continue
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
    done_count = 0

    if demo_mode:
        # デモモードは順次実行
        for i, (symbol, name) in enumerate(SYMBOLS, 1):
            print(f"  [{i:>3}/{len(SYMBOLS)}] {symbol} {name:24} ...", end=" ", flush=True)
            r = run_backtest(symbol, name, demo=True, demo_seed=i * 7 + 3)
            if r:
                results.append(r)
                sign = "+" if r["ret_pct"] >= 0 else ""
                print(f"{r['trades']:3}回  {sign}{r['ret_pct']:6.1f}%  勝率{r['win_rate']:.0f}%")
            else:
                print("スキップ（データなし or トレードなし）")
    else:
        # 本番モードは並列ダウンロード（WORKERS スレッド）
        print(f"  並列処理中（{WORKERS}スレッド）... しばらくお待ちください\n")
        seed_map = {sym: i * 7 + 3 for i, (sym, _) in enumerate(SYMBOLS, 1)}

        def _bt(args):
            sym, nm, seed = args
            return run_backtest(sym, nm, demo=False, demo_seed=seed)

        tasks = [(sym, nm, seed_map[sym]) for sym, nm in SYMBOLS]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            future_to_sym = {ex.submit(_bt, t): t for t in tasks}
            for fut in as_completed(future_to_sym):
                sym, nm, _ = future_to_sym[fut]
                done_count += 1
                r = fut.result()
                if r:
                    results.append(r)
                    sign = "+" if r["ret_pct"] >= 0 else ""
                    print(f"  [{done_count:>3}/{len(SYMBOLS)}] {sym} {nm:24}  "
                          f"{r['trades']:3}回  {sign}{r['ret_pct']:6.1f}%  勝率{r['win_rate']:.0f}%")
                else:
                    print(f"  [{done_count:>3}/{len(SYMBOLS)}] {sym} {nm:24}  スキップ")

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
