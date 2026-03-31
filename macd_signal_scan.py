"""
MACDブレイクアウト × 出来高急増 シグナルスキャナー（日経225）
────────────────────────────────────────────────────────────
現在の株価データからMACDシグナルを判定し、買い・売りを判断します。

■ アルゴリズム:
  Entry: MACDヒストグラムがゼロラインを下から上に突破
         かつ 出来高が20日平均の1.5倍以上（機関投資家の参入）
         かつ 終値 > 25日移動平均（短期上昇トレンド）
  Exit:  MACDヒストグラムがゼロラインを上から下に突破
         または ATRトレイリングストップ発動（ATR×2.5）

■ 実行方法:
  python macd_signal_scan.py              # 日経225全銘柄スキャン
  python macd_signal_scan.py --top 50     # 上位50件表示
  python macd_signal_scan.py --buy        # BUYシグナルのみ表示
"""

import io
import sys

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

# ── 日経225 全銘柄 ──────────────────────────────────────────
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
    ("8377.T", "ほくほくFG"),
    ("8411.T", "みずほフィナンシャルG"),
    ("8601.T", "大和証券グループ本社"),
    ("8604.T", "野村HD"),
    ("8628.T", "松井証券"),
    ("8630.T", "SOMPOホールディングス"),
    ("8697.T", "日本取引所グループ"),
    ("8725.T", "MS&ADインシュアランスG"),
    ("8750.T", "第一生命HD"),
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
    ("9735.T", "セコム"),
    ("9766.T", "コナミグループ"),
    ("9983.T", "ファーストリテイリング"),
    ("9984.T", "ソフトバンクグループ"),
]

# ── パラメータ ──────────────────────────────────────────────
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
VOL_MA       = 20       # 出来高移動平均の期間
VOL_SPIKE    = 1.5      # 出来高急増の閾値（平均の何倍以上）
MA_TREND     = 25       # 短期トレンドフィルター（25日MA）
ATR_PERIOD   = 14
ATR_STOP_MULT  = 1.5
ATR_TRAIL_MULT = 2.5    # 広めに設定（高ボラ相場対応）

INITIAL_CASH   = 500_000
RISK_PER_TRADE = 0.02
MAX_QTY        = 9999
WORKERS        = 16


# ── インジケーター計算 ──────────────────────────────────────
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # MACD
    ema_fast   = c.ewm(span=MACD_FAST,   adjust=False).mean()
    ema_slow   = c.ewm(span=MACD_SLOW,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line= macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram  = macd_line - signal_line

    # 25日移動平均（トレンドフィルター）
    ma25 = c.rolling(MA_TREND).mean()

    # 出来高20日平均
    vol_ma = df["volume"].rolling(VOL_MA).mean()

    # ATR
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    df = df.copy()
    df["macd"]      = macd_line
    df["macd_sig"]  = signal_line
    df["macd_hist"] = histogram
    df["ma25"]      = ma25
    df["vol_ma"]    = vol_ma
    df["atr"]       = atr

    # エントリー: ヒストグラムがゼロ上抜け + 出来高急増 + 終値>25MA
    hist_cross_up   = (histogram > 0) & (histogram.shift(1) <= 0)
    vol_spike       = df["volume"] >= vol_ma * VOL_SPIKE
    trend_up        = c > ma25
    df["entry_sig"] = hist_cross_up & vol_spike & trend_up

    # エグジット: ヒストグラムがゼロ下抜け
    df["exit_sig"]  = (histogram < 0) & (histogram.shift(1) >= 0)

    return df


# ── データ取得 ──────────────────────────────────────────────
def fetch_df(symbol: str) -> pd.DataFrame | None:
    """6ヶ月分取得（MACD26日 + シグナル9日 + ボリューム20日 + バッファ）"""
    try:
        raw = yf.download(symbol, period="6mo", interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        if len(raw) < MACD_SLOW + MACD_SIGNAL + VOL_MA + 5:
            return None
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


# ── 1銘柄シグナル判定 ──────────────────────────────────────
def calc_signal(symbol: str, name: str, df: pd.DataFrame) -> dict | None:
    df = calc_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    if pd.isna(last["macd_hist"]) or pd.isna(last["ma25"]):
        return None

    close     = float(last["close"])
    macd_hist = float(last["macd_hist"])
    macd_line = float(last["macd"])
    macd_sig  = float(last["macd_sig"])
    ma25      = float(last["ma25"])
    atr       = float(last["atr"]) if not pd.isna(last["atr"]) else 0.0
    vol       = float(last["volume"])
    vol_ma_v  = float(last["vol_ma"]) if not pd.isna(last["vol_ma"]) else 1.0
    vol_ratio = vol / vol_ma_v if vol_ma_v > 0 else 1.0
    trend_up  = close > ma25
    date      = df.index[-1].strftime("%Y-%m-%d")

    # ── シグナル判定 ──
    entry = bool(last["entry_sig"])
    exit_ = bool(last["exit_sig"])

    # ヒストグラムの向き（加速・減速）
    hist_rising = macd_hist > float(prev["macd_hist"]) if not pd.isna(prev["macd_hist"]) else False

    if entry:
        signal = "BUY"          # ゼロ上抜け + 出来高急増 + トレンド上
    elif exit_:
        signal = "SELL"         # ゼロ下抜け
    elif macd_hist > 0 and hist_rising and trend_up:
        signal = "WATCH_BUY"    # ヒストグラム正 & 加速中 → BUY待ち
    elif macd_hist > 0 and not hist_rising:
        signal = "WATCH_SELL"   # ヒストグラム正 & 減速 → SELL警戒
    elif macd_hist < 0 and not hist_rising and not trend_up:
        signal = "WEAK"         # ヒストグラム負 & 下落中 → 弱い
    else:
        signal = "HOLD"

    # トレイリングストップ参考値 / 推奨株数
    trail_stop = close - atr * ATR_TRAIL_MULT
    stop_dist  = atr * ATR_STOP_MULT
    risk_amt   = INITIAL_CASH * RISK_PER_TRADE
    qty = max(min(int(risk_amt / stop_dist), MAX_QTY), 1) if stop_dist > 0 else 1

    return {
        "symbol":     symbol,
        "name":       name,
        "date":       date,
        "close":      close,
        "macd_hist":  macd_hist,
        "macd_line":  macd_line,
        "macd_sig":   macd_sig,
        "ma25":       ma25,
        "trend_up":   trend_up,
        "vol_ratio":  vol_ratio,
        "atr":        atr,
        "stop":       trail_stop,
        "signal":     signal,
        "qty":        qty,
    }


# ── シグナル表示 ──────────────────────────────────────────────
SIGNAL_ORDER = {"BUY": 0, "SELL": 1, "WATCH_BUY": 2, "WATCH_SELL": 3, "HOLD": 4, "WEAK": 5}
SIGNAL_LABEL = {
    "BUY":        "★ BUY       ← 買いシグナル！（ヒストグラム上抜け + 出来高急増）",
    "SELL":       "▼ SELL      ← 売りシグナル！（ヒストグラム下抜け）",
    "WATCH_BUY":  "◎ WATCH↑   ヒストグラム加速中（BUY候補）",
    "WATCH_SELL": "△ WATCH↓   ヒストグラム減速（SELL警戒）",
    "HOLD":       "  HOLD",
    "WEAK":       "  WEAK      トレンド下向き・ヒストグラム負",
}


def show_signals(signals: list[dict], only_buy: bool = False, top_n: int = 0) -> None:
    items = sorted(signals, key=lambda x: SIGNAL_ORDER.get(x["signal"], 9))

    if only_buy:
        items = [s for s in items if s["signal"] in ("BUY", "WATCH_BUY")]
    if top_n > 0:
        items = items[:top_n]

    date_str = items[0]["date"] if items else datetime.now().strftime("%Y-%m-%d")
    buy_count  = sum(1 for s in signals if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals if s["signal"] == "SELL")
    watch_up   = sum(1 for s in signals if s["signal"] == "WATCH_BUY")

    print()
    print(f"  ══════════════════════════════════════════════════════════════════════════════")
    print(f"  MACD シグナル一覧  [{date_str}]  日経225 ({len(signals)}銘柄スキャン)")
    print(f"  ══════════════════════════════════════════════════════════════════════════════")
    print(f"  {'#':<4} {'銘柄':<24} {'終値':>8} {'25MA':>8} {'TRD':>4} "
          f"{'HIST':>7} {'出来高比':>7} {'トレイル':>9}  シグナル")
    print(f"  {'─'*4} {'─'*24} {'─'*8} {'─'*8} {'─'*4} "
          f"{'─'*7} {'─'*7} {'─'*9}  {'─'*48}")

    for i, s in enumerate(items, 1):
        sym      = s["symbol"]
        label    = f"{s['name']}({sym})"
        trd      = "↑" if s["trend_up"] else "↓"
        hist_str = f"{s['macd_hist']:>+7.2f}"
        vol_str  = f"{s['vol_ratio']:>6.1f}x"
        sig_lbl  = SIGNAL_LABEL.get(s["signal"], s["signal"])
        ma25_str = f"{s['ma25']:>8,.1f}"

        print(f"  {i:<4} {label:<24} {s['close']:>8,.1f} {ma25_str} {trd:>4} "
              f"{hist_str} {vol_str:>7} {s['stop']:>9,.1f}  {sig_lbl}")

    print()
    print(f"  ★ BUY: {buy_count}件  ▼ SELL: {sell_count}件  ◎ WATCH↑: {watch_up}件  "
          f"スキャン合計: {len(signals)}銘柄")
    print(f"  ※ トレイルStop = 終値 − ATR×{ATR_TRAIL_MULT}  "
          f"出来高急増閾値: 20日平均×{VOL_SPIKE}倍以上  25MA トレンドフィルター")
    print()


# ── メイン ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="MACDブレイクアウト × 出来高急増 シグナルスキャナー（日経225）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python macd_signal_scan.py              # 全225銘柄スキャン
  python macd_signal_scan.py --top 30     # 上位30件のみ表示
  python macd_signal_scan.py --buy        # BUY/WATCH_BUYシグナルのみ表示
""")
    parser.add_argument("--top",  type=int,  default=0,
                        help="表示件数（0=全件）")
    parser.add_argument("--buy",  action="store_true",
                        help="BUY / WATCH_BUYシグナルのみ表示")
    args = parser.parse_args()

    print(f"\n  MACD シグナルスキャナー  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    print(f"  MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) × 出来高{VOL_SPIKE}倍急増 × 25MA × ATRトレイル×{ATR_TRAIL_MULT}")
    print(f"  対象: 日経225 ({len(SYMBOLS)}銘柄)\n")

    stock_data: dict = {}

    # Phase 1: 順次データ取得
    print(f"  [Phase 1] データ取得中 ({len(SYMBOLS)}銘柄)...")
    for i, (sym, name) in enumerate(SYMBOLS, 1):
        print(f"  [{i:3d}/{len(SYMBOLS)}] {name}({sym})", end=" ", flush=True)
        df = fetch_df(sym)
        if df is None:
            print("×")
        else:
            print("✓")
            stock_data[sym] = (name, df)

    # Phase 2: 並列シグナル計算
    print(f"\n  [Phase 2] シグナル計算中 ({len(stock_data)}銘柄, {WORKERS}並列)...")
    tasks   = [(sym, nm, df) for sym, (nm, df) in stock_data.items()]
    signals = []

    def _calc(task):
        sym, nm, df = task
        return calc_signal(sym, nm, df)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        future_to_sym = {ex.submit(_calc, t): t[0] for t in tasks}
        done = 0
        for fut in as_completed(future_to_sym):
            done += 1
            r = fut.result()
            if r:
                signals.append(r)
            print(f"  計算完了 {done}/{len(tasks)}", end="\r", flush=True)

    print()

    if not signals:
        print("\n  シグナルデータが取得できませんでした。\n")
        return

    show_signals(signals, only_buy=args.buy, top_n=args.top)


if __name__ == "__main__":
    main()
