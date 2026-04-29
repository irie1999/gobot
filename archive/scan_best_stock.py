"""
直近2ヶ月 スイングトレード 銘柄スキャナー
────────────────────────────────────────
日経225 全225銘柄を一括バックテストして利益率ランキングを表示します。

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

# ── 日経225 全225銘柄 ─────────────────────────────────────────
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
    ("4911.T", "資生堂"),
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
    ("6773.T", "パイオニア"),
    ("6794.T", "フォスター電機"),
    ("6796.T", "クラリオン"),
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
    ("7974.T", "任天堂"),
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
    ("8752.T", "三井住友海上グループHD"),
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
    ("9613.T", "NTTデータグループ"),
    ("9735.T", "セコム"),
    ("9766.T", "コナミグループ"),
    ("9983.T", "ファーストリテイリング"),
    ("9984.T", "ソフトバンクグループ"),
]


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
