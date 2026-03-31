"""
期間指定バックテスト（A7 V2: トレンドフィルター付きストキャスティクス + ATRトレイリングストップ）
────────────────────────────────────────────────────────────────────────
【V2 コンセプト】
  パラメータは V1 と同じ（シグナル品質を維持）
  デフォルト対象を日経225 全銘柄に拡大 → 高品質シグナルをより多く獲得
  ※ パラメータを緩めると利益率が低下するため、対象拡大で取引回数を増やす

対象銘柄: デフォルトはA7監視対象20銘柄（--all で日経225全銘柄）
バックテスト期間: 自由に指定可（デフォルト: 直近1ヶ月）

■ 実行方法:
  python backtest_stoch_atr_trail_v2.py               # 直近1ヶ月（デフォルト）
  python backtest_stoch_atr_trail_v2.py --months 3    # 直近3ヶ月
  python backtest_stoch_atr_trail_v2.py --months 6    # 直近6ヶ月
  python backtest_stoch_atr_trail_v2.py --years 1     # 直近1年
  python backtest_stoch_atr_trail_v2.py --years 5     # 直近5年（最大）
  python backtest_stoch_atr_trail_v2.py --days 45     # 直近45日（日数で直接指定）
  python backtest_stoch_atr_trail_v2.py 7203.T --years 2  # 特定銘柄のみ詳細表示
  python backtest_stoch_atr_trail_v2.py --years 3 --top 50  # 上位50件表示
  python backtest_stoch_atr_trail_v2.py --all --years 1     # 日経225全銘柄 1年

■ 出力:
  - 銘柄ごとの損益・勝率・平均保有日数
  - 個別トレード一覧（エントリー日・エグジット日・損益）
  - 損益率ランキング表示
"""

import argparse
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

try:
    from rsi2 import _CACHE_DIR as _RSI2_CACHE_DIR
except ImportError:
    _RSI2_CACHE_DIR = None

# ── 日経225 全銘柄（--all 時に使用）────────────────────────
_ALL_SYMBOLS = [
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
    # ("8355.T", "静岡銀行"),  # データ不備
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

# V2: デフォルトは日経225全銘柄（--watch で監視20銘柄に切替）
from symbols_watch_a7 import SYMBOLS as _WATCH_SYMBOLS_A7
SYMBOLS = _ALL_SYMBOLS

# ── パラメータ ──────────────────────────────────────────────
BACKTEST_DAYS    = 30           # デフォルトのバックテスト期間（日）
WORKERS          = 16           # 並列バックテスト数（データ取得は順次）
STOCH_K_PERIOD   = 14           # V1 と同じ（品質維持）
STOCH_D_PERIOD   = 3
STOCH_SMOOTH     = 3
STOCH_OVERSOLD   = 30
STOCH_OVERBOUGHT = 70           # V1 と同じ（品質維持）
ATR_PERIOD       = 14
ATR_STOP_MULT    = 1.5
ATR_TRAIL_MULT   = 2.0
MA_TREND_PERIOD  = 75           # V1 と同じ（品質維持）

INITIAL_CASH     = 500_000      # 運用資金（円）
RISK_PER_TRADE   = 0.02         # 1トレードあたり許容損失率 2%
MAX_QTY          = 9999         # 最大購入株数（S株：1株単位）


# ── インジケーター計算 ──────────────────────────────────────
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    h = df["high"]
    l = df["low"]
    c = df["close"]

    lowest_low   = l.rolling(STOCH_K_PERIOD).min()
    highest_high = h.rolling(STOCH_K_PERIOD).max()
    denom        = highest_high - lowest_low
    fast_k = (c - lowest_low) / denom.replace(0, np.nan) * 100
    slow_k = fast_k.rolling(STOCH_SMOOTH).mean()
    slow_d = slow_k.rolling(STOCH_D_PERIOD).mean()

    ma75 = c.rolling(MA_TREND_PERIOD).mean()

    prev_k = slow_k.shift(1)
    prev_d = slow_d.shift(1)

    df = df.copy()
    df["stoch_k"]      = slow_k
    df["stoch_d"]      = slow_d
    df["ma75"]         = ma75
    df["golden_cross"] = (slow_k > slow_d) & (prev_k <= prev_d)
    df["dead_cross"]   = (slow_k < slow_d) & (prev_k >= prev_d)
    df["entry_sig"]    = df["golden_cross"] & (slow_k < STOCH_OVERBOUGHT) & (c > ma75)
    df["exit_sig"]     = df["dead_cross"]

    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── データ取得（永続キャッシュ付き） ────────────────────────
def fetch_df(symbol: str, backtest_days: int = BACKTEST_DAYS) -> pd.DataFrame | None:
    """バックテスト期間 + 指標計算バッファ分のデータを取得する。
    backtest_macd_scan.py と同じキャッシュ（.rsi2_cache/*.pkl）を共有。
    キャッシュが新鮮（10日以内・210行以上）なら再取得しない。"""
    _today = pd.Timestamp(datetime.today().date())

    # ── 永続キャッシュを確認 ──────────────────────────────────
    if _RSI2_CACHE_DIR is not None:
        persistent = _RSI2_CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
        if persistent.exists():
            try:
                with open(persistent, "rb") as f:
                    cached = pickle.load(f)
                last_date = cached.index[-1]
                stale = last_date < (_today - timedelta(days=3))
                # キャッシュ検証: 価格変動があるか確認（汚染されたキャッシュを除外）
                price_range = float(cached["close"].max() - cached["close"].min())
                valid = price_range > 0.01 * float(cached["close"].mean())
                if len(cached) >= 210 and not stale and valid:
                    return cached
            except Exception:
                pass

    # ── キャッシュなし／古い → yfinance から取得 ─────────────
    buf_days = MA_TREND_PERIOD + STOCH_K_PERIOD + STOCH_SMOOTH + STOCH_D_PERIOD + 30
    dl_start = (datetime.today() - timedelta(days=int((backtest_days + buf_days) * 1.5))).strftime("%Y-%m-%d")
    dl_end   = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        # Ticker.history() を使用（単一銘柄に適しており、並列呼び出しでも安全）
        ticker = yf.Ticker(symbol)
        raw = ticker.history(period="2y", interval="1d", auto_adjust=False)
        if raw.empty:
            return None
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        available = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        if len(available) < 5:
            return None
        raw = raw[available].dropna()
        min_needed = MA_TREND_PERIOD + STOCH_K_PERIOD + STOCH_SMOOTH + STOCH_D_PERIOD
        if len(raw) < min_needed:
            return None
        df = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
        # キャッシュに保存
        if _RSI2_CACHE_DIR is not None:
            try:
                _RSI2_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                persistent = _RSI2_CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
                with open(persistent, "wb") as f:
                    pickle.dump(df, f)
            except Exception:
                pass
        return df
    except Exception:
        return None


# ── 1銘柄バックテスト ────────────────────────────────────────
def run_backtest(symbol: str, name: str, df: pd.DataFrame,
                 backtest_days: int) -> dict | None:
    """直近 backtest_days 日間だけを対象にバックテスト実行"""
    df = calc_indicators(df)

    cutoff    = pd.Timestamp(datetime.today() - timedelta(days=backtest_days))
    df_target = df[df.index >= cutoff].copy()

    if len(df_target) < 5:
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

        if in_pos:
            reason = exit_p = None
            if row["low"] <= trail_stop:
                exit_p = min(float(row["open"]), trail_stop)
                reason = "トレイリング"
            elif row["exit_sig"]:
                exit_p = float(row["close"])
                reason = "デッドクロス"
            if reason:
                pnl   = (exit_p - entry_price) * qty
                cash += exit_p * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     dt,
                    "entry_price": entry_price,
                    "exit_price":  exit_p,
                    "qty":         qty,
                    "pnl":         pnl,
                    "hold_days":   (dt - entry_dt).days,
                    "reason":      reason,
                })
                in_pos = False
            else:
                candidate = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
                if candidate > trail_stop:
                    trail_stop = candidate

        if not in_pos and row["entry_sig"]:
            risk_amt  = cash * RISK_PER_TRADE
            stop_dist = float(row["atr"]) * ATR_STOP_MULT
            if stop_dist > 0:
                q = min(int(risk_amt / stop_dist), MAX_QTY)
                q = max(q, 1)
                if float(row["close"]) * q <= cash:
                    cash        -= float(row["close"]) * q
                    entry_price  = float(row["close"])
                    trail_stop   = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
                    entry_dt     = dt
                    qty          = q
                    in_pos       = True

    # 未決済ポジションを最終日終値で決済
    open_pos = None
    if in_pos:
        lp  = float(df_target.iloc[-1]["close"])
        pnl = (lp - entry_price) * qty
        cash += lp * qty
        open_pos = {
            "entry_dt":    entry_dt,
            "exit_dt":     df_target.index[-1],
            "entry_price": entry_price,
            "exit_price":  lp,
            "qty":         qty,
            "pnl":         pnl,
            "hold_days":   (df_target.index[-1] - entry_dt).days,
            "reason":      "保有中（最終日終値）",
        }
        trades.append(open_pos)

    if not trades:
        return None

    total    = cash - INITIAL_CASH
    ret_pct  = total / INITIAL_CASH * 100
    wins     = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) * 100
    avg_hold = sum(t["hold_days"] for t in trades) / len(trades)

    # 現在値（最終日終値）
    last_close = float(df_target.iloc[-1]["close"])
    last_k     = float(df_target.iloc[-1]["stoch_k"]) if not pd.isna(df_target.iloc[-1]["stoch_k"]) else 0.0
    last_d     = float(df_target.iloc[-1]["stoch_d"]) if not pd.isna(df_target.iloc[-1]["stoch_d"]) else 0.0
    last_ma75  = float(df_target.iloc[-1]["ma75"])    if not pd.isna(df_target.iloc[-1]["ma75"])    else 0.0

    return {
        "symbol":     symbol,
        "name":       name,
        "trades":     len(trades),
        "wins":       len(wins),
        "losses":     len(trades) - len(wins),
        "win_rate":   win_rate,
        "total":      total,
        "ret_pct":    ret_pct,
        "avg_hold":   avg_hold,
        "trade_log":  trades,
        "open_pos":   open_pos is not None,
        "last_close": last_close,
        "last_k":     last_k,
        "last_d":     last_d,
        "last_ma75":  last_ma75,
    }


# ── 単銘柄詳細表示 ──────────────────────────────────────────
def print_detail(r: dict, backtest_days: int) -> None:
    sym   = r["symbol"]
    name  = r["name"]
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")
    sign  = "+" if r["total"] >= 0 else ""

    print()
    print("=" * 65)
    print(f"  {name}({sym})  直近{backtest_days}日バックテスト  [{since} ～ {today}]")
    print("=" * 65)
    print(f"  トレード数  : {r['trades']}回  （勝: {r['wins']}  負: {r['losses']}）")
    print(f"  勝率        : {r['win_rate']:.1f}%")
    print(f"  損益合計    : {sign}{r['total']:,.0f}円  （{sign}{r['ret_pct']:.2f}%）")
    print(f"  平均保有    : {r['avg_hold']:.1f}日")
    print(f"  現在値      : {r['last_close']:,.1f}円  %K={r['last_k']:.1f}  %D={r['last_d']:.1f}  75MA={r['last_ma75']:,.1f}")
    print()

    if r["trade_log"]:
        print(f"  {'#':<3} {'エントリー':>12} {'エグジット':>12} {'取得価格':>9} {'売却価格':>9} "
              f"{'株数':>6} {'損益':>10} {'保有日':>6}  出口理由")
        print("  " + "─" * 85)
        for i, t in enumerate(r["trade_log"], 1):
            pnl_s = f"{t['pnl']:>+10,.0f}"
            entry_s = t["entry_dt"].strftime("%Y-%m-%d")
            exit_s  = t["exit_dt"].strftime("%Y-%m-%d")
            open_mark = " ★" if t["reason"] == "保有中（最終日終値）" else ""
            print(f"  {i:<3} {entry_s:>12} {exit_s:>12} {t['entry_price']:>9,.1f} {t['exit_price']:>9,.1f} "
                  f"{t['qty']:>6} {pnl_s} {t['hold_days']:>5}日  {t['reason']}{open_mark}")
        print("  " + "─" * 85)
        if r["open_pos"]:
            print("  ★ = 現在保有中（最終日終値で仮決済）")
    print()


# ── ランキング表示 ──────────────────────────────────────────
def print_ranking(results: list[dict], backtest_days: int, top_n: int = 30) -> None:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    sorted_r = sorted(results, key=lambda x: x["ret_pct"], reverse=True)
    display_r = sorted_r[:top_n]

    print()
    print("═" * 78)
    print(f"  A7 直近{backtest_days}日バックテスト ランキング TOP{top_n}  [{since} ～ {today}]")
    print("═" * 78)
    print(f"  {'順位':<4} {'銘柄':<22} {'損益':>10} {'損益率':>8} {'勝率':>7} "
          f"{'取引数':>6} {'平均保有':>8}  現在値")
    print("  " + "─" * 74)

    for rank, r in enumerate(display_r, 1):
        sign    = "+" if r["ret_pct"] >= 0 else ""
        bar_len = int(abs(r["ret_pct"]) / 2)
        bar     = ("▲" if r["ret_pct"] >= 0 else "▽") * min(bar_len, 15)
        trend   = "↑" if r["last_close"] > r["last_ma75"] else "↓"

        print(f"  {rank:<4} {r['name']}({r['symbol']}){'':<{max(0,20-len(r['name'])-len(r['symbol']))}}"
              f"  {sign}{r['total']:>9,.0f}円 {sign}{r['ret_pct']:>6.1f}% "
              f"{r['win_rate']:>6.0f}%  {r['trades']:>4}回  {r['avg_hold']:>5.1f}日  "
              f"{r['last_close']:>7,.0f}{trend}  {bar}")

    print("  " + "─" * 74)
    total_trades = sum(r["trades"] for r in results)
    profitable   = sum(1 for r in results if r["ret_pct"] > 0)
    avg_ret      = sum(r["ret_pct"] for r in results) / len(results) if results else 0
    no_trade_cnt = len(SYMBOLS) - len(results)
    print(f"  スキャン: {len(SYMBOLS)}銘柄  取引あり: {len(results)}件  "
          f"取引なし: {no_trade_cnt}件  トレード計: {total_trades}回")
    print(f"  プラス銘柄: {profitable}/{len(results)}  平均損益率: {avg_ret:+.2f}%")
    print()
    print(f"  ※ 運用資金 {INITIAL_CASH:,}円/銘柄  ATRストップ×{ATR_STOP_MULT}  "
          f"トレイル×{ATR_TRAIL_MULT}  75MA トレンドフィルター")
    print()


# ── HTML レポート生成 ────────────────────────────────────────
def generate_html(results: list[dict], backtest_days: int,
                  period_label: str, single_sym: str | None = None) -> Path:
    today = datetime.today().strftime("%Y-%m-%d")
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    sorted_r = sorted(results, key=lambda x: x["ret_pct"], reverse=True)

    total_trades = sum(r["trades"] for r in results)
    profitable   = sum(1 for r in results if r["ret_pct"] > 0)
    avg_ret      = sum(r["ret_pct"] for r in results) / len(results) if results else 0
    no_trade_cnt = len(SYMBOLS) - len(results)

    # ── ランキング行 ──────────────────────────────────────────
    ranking_rows = ""
    for rank, r in enumerate(sorted_r, 1):
        sign     = "+" if r["ret_pct"] >= 0 else ""
        cls      = "pos" if r["ret_pct"] >= 0 else "neg"
        trend    = "↑" if r["last_close"] > r["last_ma75"] else "↓"
        open_tag = " ★" if r["open_pos"] else ""
        pf_wins  = sum(t["pnl"] for t in r["trade_log"] if t["pnl"] > 0)
        pf_loss  = sum(-t["pnl"] for t in r["trade_log"] if t["pnl"] < 0)
        pf_val   = f"{pf_wins/pf_loss:.2f}" if pf_loss > 0 else "∞"

        # トレード明細行（折りたたみ）
        trade_rows = ""
        for t in r["trade_log"]:
            t_cls  = "pos" if t["pnl"] >= 0 else "neg"
            mark   = " ★" if t["reason"] == "保有中（最終日終値）" else ""
            pnl_pct = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            trade_rows += f"""
            <tr class="trade-row">
              <td>{t['entry_dt'].strftime('%Y-%m-%d')}</td>
              <td>{t['exit_dt'].strftime('%Y-%m-%d')}</td>
              <td class="num">{t['entry_price']:,.0f}</td>
              <td class="num">{t['exit_price']:,.0f}</td>
              <td class="num {t_cls}">{t['pnl']:+,.0f}円<br><small>{pnl_pct:+.1f}%</small></td>
              <td class="num">{t['hold_days']}日</td>
              <td>{t['reason']}{mark}</td>
            </tr>"""

        ranking_rows += f"""
        <tr class="stock-row {cls}" onclick="toggleTrades('{r['symbol']}')">
          <td class="rank">{rank}</td>
          <td class="name">{r['name']}<br><small>{r['symbol']}</small></td>
          <td class="num {cls}">{sign}{r['total']:,.0f}円</td>
          <td class="num {cls}">{sign}{r['ret_pct']:.1f}%</td>
          <td class="num">{r['win_rate']:.0f}%</td>
          <td class="num">{pf_val}</td>
          <td class="num">{r['trades']}</td>
          <td class="num">{r['avg_hold']:.1f}日</td>
          <td class="num">{r['last_close']:,.0f} {trend}<br>
            <small>%K={r['last_k']:.0f} %D={r['last_d']:.0f}</small></td>
          <td class="open-mark">{"●保有中" if r['open_pos'] else ""}</td>
        </tr>
        <tr class="trade-detail" id="detail-{r['symbol']}" style="display:none">
          <td colspan="10">
            <table class="inner-table">
              <thead>
                <tr><th>エントリー</th><th>エグジット</th><th>取得価格</th>
                    <th>売却価格</th><th>損益</th><th>保有日</th><th>出口理由</th></tr>
              </thead>
              <tbody>{trade_rows}</tbody>
            </table>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A7バックテスト {period_label} {today}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Hiragino Kaku Gothic ProN', Meiryo, sans-serif;
          background: #0f172a; color: #e2e8f0; font-size: 13px; padding: 20px; }}
  h1 {{ font-size: 1.4rem; color: #f1f5f9; margin-bottom: 6px; }}
  .subtitle {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 16px; }}
  .summary {{ display: flex; gap: 20px; flex-wrap: wrap;
              background: #1e293b; border-radius: 8px; padding: 14px 20px;
              margin-bottom: 20px; font-size: 0.9rem; }}
  .summary .item {{ display: flex; flex-direction: column; }}
  .summary .lbl {{ color: #64748b; font-size: 0.75rem; margin-bottom: 2px; }}
  .summary .val {{ font-weight: 600; font-size: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #1e293b; padding: 7px 10px; text-align: center;
        color: #94a3b8; font-weight: 600; white-space: nowrap;
        border-bottom: 1px solid #334155; position: sticky; top: 0; z-index: 1; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #1e293b; white-space: nowrap; }}
  .stock-row {{ cursor: pointer; }}
  .stock-row:hover {{ background: #1e293b; }}
  .name {{ font-weight: 600; }}
  .name small {{ color: #64748b; font-weight: 400; display: block; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .rank {{ text-align: center; color: #64748b; font-weight: 700; }}
  .pos {{ color: #4ade80; }}
  .neg {{ color: #f87171; }}
  .open-mark {{ color: #fbbf24; font-weight: 600; text-align: center; }}
  .trade-detail td {{ background: #0f172a; padding: 0; }}
  .inner-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .inner-table th {{ background: #0f172a; color: #475569; padding: 5px 10px; position: static; }}
  .inner-table td {{ padding: 5px 10px; border-bottom: 1px solid #1e293b; }}
  .note {{ color: #64748b; font-size: 0.8rem; margin-top: 16px; }}
</style>
</head>
<body>
<h1>A7 ストキャスティクス + ATRトレイリング　バックテスト結果</h1>
<p class="subtitle">期間: {since} ～ {today}（直近{period_label}）　クリックでトレード明細を展開</p>

<div class="summary">
  <div class="item"><span class="lbl">スキャン銘柄</span><span class="val">{len(SYMBOLS)}銘柄</span></div>
  <div class="item"><span class="lbl">取引あり</span><span class="val">{len(results)}件</span></div>
  <div class="item"><span class="lbl">取引なし</span><span class="val">{no_trade_cnt}件</span></div>
  <div class="item"><span class="lbl">トレード総数</span><span class="val">{total_trades}回</span></div>
  <div class="item"><span class="lbl">プラス銘柄</span><span class="val">{profitable}/{len(results)}</span></div>
  <div class="item"><span class="lbl">平均損益率</span>
    <span class="val {'pos' if avg_ret >= 0 else 'neg'}">{avg_ret:+.2f}%</span></div>
</div>

<table>
  <thead>
    <tr>
      <th>#</th><th>銘柄</th><th>損益</th><th>損益率</th><th>勝率</th>
      <th>PF</th><th>取引数</th><th>平均保有</th><th>現在値 / Stoch</th><th>保有</th>
    </tr>
  </thead>
  <tbody>{ranking_rows}</tbody>
</table>

<p class="note">
  ※ 75MA トレンドフィルター（終値 &gt; 75MA のみエントリー）<br>
  ※ エントリー: ストキャスティクス %K が %D をゴールデンクロス かつ %K &lt; 70<br>
  ※ エグジット: デッドクロス または ATRトレイリングストップ（×{ATR_TRAIL_MULT}）<br>
  ※ 運用資金 {INITIAL_CASH:,}円/銘柄　ATRストップ×{ATR_STOP_MULT}
</p>

<script>
function toggleTrades(sym) {{
  const el = document.getElementById('detail-' + sym);
  el.style.display = el.style.display === 'none' ? 'table-row' : 'none';
}}
</script>
</body>
</html>"""

    fname = f"backtest_stoch_v2_{period_label}_{today}.html"
    path  = Path(fname)
    path.write_text(html, encoding="utf-8")
    return path


# ── A7シグナルスキャン ────────────────────────────────────────
def scan_signals_a7(stock_data_map: dict, results_90d: list[dict]) -> dict:
    """本日のA7エントリー/エグジットシグナルをスキャンする。
    stock_data_map: {sym: (name, df)}
    results_90d   : run_backtest(90日) の結果リスト（保有中銘柄の検出に使用）
    戻り値: {"buy": [...], "sell": [...], "hold": [...], "today": str}
    """
    today = datetime.today().strftime("%Y-%m-%d")

    # 90日バックテストで保有中の銘柄マップ
    open_pos_map: dict[str, dict] = {}
    for r in results_90d:
        if r.get("open_pos") and r.get("trade_log"):
            last_trade = r["trade_log"][-1]
            if "保有中" in last_trade.get("reason", ""):
                open_pos_map[r["symbol"]] = r

    buy: list[dict]  = []
    sell: list[dict] = []
    hold: list[dict] = []

    for sym, (name, df) in stock_data_map.items():
        df_ind = calc_indicators(df)
        if len(df_ind) < 5:
            continue

        last = df_ind.iloc[-1]
        if pd.isna(last["stoch_k"]) or pd.isna(last["stoch_d"]) or pd.isna(last["atr"]):
            continue

        last_close = float(last["close"])
        last_k     = float(last["stoch_k"])
        last_d     = float(last["stoch_d"])
        last_ma75  = float(last["ma75"]) if not pd.isna(last["ma75"]) else 0.0
        last_atr   = float(last["atr"])
        atr_pct    = last_atr / last_close * 100 if last_close > 0 else 0.0
        trend_up   = last_close > last_ma75
        signal_dt  = str(df_ind.index[-1].date())

        # ── 買いシグナル: ゴールデンクロス + %K<70 + 75MA上 ──
        if bool(last["entry_sig"]):
            buy.append({
                "symbol":     sym,
                "name":       name,
                "open":       float(last["open"]),
                "close":      last_close,
                "stoch_k":    last_k,
                "stoch_d":    last_d,
                "ma75":       last_ma75,
                "atr_pct":    atr_pct,
                "trend_up":   trend_up,
                "signal_dt":  signal_dt,
            })

        # ── 売り/継続保有 ──────────────────────────────────────
        if sym in open_pos_map:
            bt          = open_pos_map[sym]
            last_trade  = bt["trade_log"][-1]
            entry_price = last_trade["entry_price"]
            entry_dt    = last_trade["entry_dt"]
            hold_days   = (datetime.today() - entry_dt.to_pydatetime()).days
            unrealized  = (last_close - entry_price) / entry_price * 100

            common = {
                "symbol":      sym,
                "name":        name,
                "open":        float(last["open"]),
                "close":       last_close,
                "entry_price": entry_price,
                "entry_dt":    str(entry_dt.date()),
                "hold_days":   hold_days,
                "unrealized":  unrealized,
                "stoch_k":     last_k,
                "stoch_d":     last_d,
                "trend_up":    trend_up,
            }
            if bool(last["exit_sig"]):
                sell.append(common)
            else:
                hold.append(common)

    buy.sort(key=lambda x: x["stoch_k"])  # %K が低い順（より深い押し目を優先）
    return {"buy": buy, "sell": sell, "hold": hold, "today": today}


def print_signals_a7(sig: dict) -> None:
    """scan_signals_a7 の結果をターミナルに表示。"""
    today = sig["today"]
    buy   = sig["buy"]
    sell  = sig["sell"]
    hold  = sig["hold"]

    print()
    print("═" * 68)
    print(f"  A7シグナル（ストキャスティクス + ATRトレイリング）  {today} 引け後")
    print("═" * 68)

    # ── 買いシグナル ───────────────────────────────────────
    print(f"\n  ◆ 買いシグナル  ({len(buy)} 銘柄)  ← 明日の始値で購入候補")
    if not buy:
        print("    なし")
    else:
        print(f"  {'#':<3} {'銘柄':<22} {'始値':>8} {'終値':>8} {'%K':>5} {'%D':>5} {'ATR%':>5}  状態")
        print("  " + "─" * 70)
        for i, c in enumerate(buy, 1):
            trend = "↑MA75上" if c["trend_up"] else "↓MA75下"
            label = f"{c['name']}({c['symbol']})"
            print(f"  {i:<3} {label:<22} {c['open']:>8,.0f} {c['close']:>8,.0f} "
                  f"{c['stoch_k']:>5.1f} {c['stoch_d']:>5.1f} {c['atr_pct']:>5.1f}%  {trend}")

    # ── 売りシグナル ───────────────────────────────────────
    print(f"\n  ◆ 売りシグナル  ({len(sell)} 銘柄)  ← 明日の始値で売却候補")
    if not sell:
        print("    なし")
    else:
        print(f"  {'銘柄':<22} {'始値':>8} {'終値':>8} {'買値':>8} {'含み損益':>9} {'保有日':>5} {'%K':>5} {'%D':>5}")
        print("  " + "─" * 75)
        for c in sell:
            label = f"{c['name']}({c['symbol']})"
            sign  = "+" if c["unrealized"] >= 0 else ""
            print(f"  {label:<22} {c['open']:>8,.0f} {c['close']:>8,.0f} {c['entry_price']:>8,.0f} "
                  f"{sign}{c['unrealized']:>+8.1f}% {c['hold_days']:>4}日 "
                  f"{c['stoch_k']:>5.1f} {c['stoch_d']:>5.1f}")

    # ── 継続保有 ──────────────────────────────────────────
    print(f"\n  ◆ 継続保有  ({len(hold)} 銘柄)  ← デッドクロスなし・保有継続")
    if not hold:
        print("    なし")
    else:
        print(f"  {'銘柄':<22} {'始値':>8} {'終値':>8} {'買値':>8} {'含み損益':>9} {'保有日':>5} {'%K':>5} {'%D':>5}")
        print("  " + "─" * 75)
        for c in hold:
            label = f"{c['name']}({c['symbol']})"
            sign  = "+" if c["unrealized"] >= 0 else ""
            print(f"  {label:<22} {c['open']:>8,.0f} {c['close']:>8,.0f} {c['entry_price']:>8,.0f} "
                  f"{sign}{c['unrealized']:>+8.1f}% {c['hold_days']:>4}日 "
                  f"{c['stoch_k']:>5.1f} {c['stoch_d']:>5.1f}")

    print()
    print(f"  ※ エントリー条件: ゴールデンクロス(%K>%D) + %K<{STOCH_OVERBOUGHT} + 終値>{MA_TREND_PERIOD}MA")
    print(f"  ※ エグジット条件: デッドクロス or ATRトレイリングストップ(×{ATR_TRAIL_MULT})")
    print(f"  ※ 翌営業日の始値で執行")
    print()


def generate_signal_html_a7(sig: dict) -> Path:
    """A7シグナルをHTMLレポートに出力する。"""
    today = sig["today"]
    buy   = sig["buy"]
    sell  = sig["sell"]
    hold  = sig["hold"]

    def _rows(items: list[dict], mode: str) -> str:
        if not items:
            return '<tr><td colspan="8" style="text-align:center;color:#888">なし</td></tr>'
        rows = ""
        for c in items:
            trend = "↑MA75上" if c["trend_up"] else "↓MA75下"
            if mode == "buy":
                rows += f"""<tr>
  <td>{c['name']}<br><small>{c['symbol']}</small></td>
  <td class="num">{c['open']:,.0f}</td>
  <td class="num">{c['close']:,.0f}</td>
  <td class="num">{c['stoch_k']:.1f}</td>
  <td class="num">{c['stoch_d']:.1f}</td>
  <td class="num">{c['atr_pct']:.1f}%</td>
  <td>{trend}</td>
  <td>{c['signal_dt']}</td>
</tr>"""
            else:
                sign = "+" if c["unrealized"] >= 0 else ""
                cls  = "pos" if c["unrealized"] >= 0 else "neg"
                rows += f"""<tr>
  <td>{c['name']}<br><small>{c['symbol']}</small></td>
  <td class="num">{c['open']:,.0f}</td>
  <td class="num">{c['close']:,.0f}</td>
  <td class="num">{c['entry_price']:,.0f}</td>
  <td class="num {cls}">{sign}{c['unrealized']:.1f}%</td>
  <td class="num">{c['hold_days']}日</td>
  <td class="num">{c['stoch_k']:.1f}</td>
  <td class="num">{c['stoch_d']:.1f}</td>
  <td>{trend}</td>
</tr>"""
        return rows

    buy_rows  = _rows(buy,  "buy")
    sell_rows = _rows(sell, "pos")
    hold_rows = _rows(hold, "pos")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>A7シグナル {today}</title>
<style>
  body {{ background:#1a1a2e; color:#e0e0e0; font-family:'Meiryo',sans-serif; padding:20px; }}
  h1   {{ color:#00d4ff; font-size:1.3em; border-bottom:1px solid #444; padding-bottom:8px; }}
  h2   {{ color:#ffd700; font-size:1.1em; margin-top:28px; }}
  table {{ border-collapse:collapse; width:100%; margin-top:8px; }}
  th   {{ background:#2a2a4a; color:#aaa; padding:8px 12px; text-align:left; font-size:.85em; }}
  td   {{ padding:7px 12px; border-bottom:1px solid #2a2a3a; font-size:.9em; }}
  tr:hover td {{ background:#252540; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .pos {{ color:#4caf50; }}
  .neg {{ color:#f44336; }}
  .note {{ color:#888; font-size:.8em; margin-top:16px; }}
</style>
</head>
<body>
<h1>A7シグナル（ストキャスティクス + ATRトレイリング）― {today} 引け後</h1>
<p style="color:#aaa;font-size:.85em">
  エントリー: ゴールデンクロス(%K&gt;%D) + %K&lt;{STOCH_OVERBOUGHT} + 終値&gt;{MA_TREND_PERIOD}MA ／
  エグジット: デッドクロス or ATRトレイリング×{ATR_TRAIL_MULT}
</p>

<h2>◆ 買いシグナル（{len(buy)} 銘柄）― 明日の始値で購入候補</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>始値</th><th>終値</th><th>%K</th><th>%D</th><th>ATR%</th><th>トレンド</th><th>シグナル日</th>
  </tr></thead>
  <tbody>{buy_rows}</tbody>
</table>

<h2>◆ 売りシグナル（{len(sell)} 銘柄）― 明日の始値で売却候補</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>始値</th><th>終値</th><th>買値</th><th>含み損益</th><th>保有日</th><th>%K</th><th>%D</th><th>トレンド</th>
  </tr></thead>
  <tbody>{sell_rows}</tbody>
</table>

<h2>◆ 継続保有（{len(hold)} 銘柄）― デッドクロスなし・保有継続</h2>
<table>
  <thead><tr>
    <th>銘柄</th><th>始値</th><th>終値</th><th>買値</th><th>含み損益</th><th>保有日</th><th>%K</th><th>%D</th><th>トレンド</th>
  </tr></thead>
  <tbody>{hold_rows}</tbody>
</table>

<p class="note">※ 保有中銘柄は直近90日バックテストで検出（実際のポジションとは異なる場合があります）</p>
<p class="note">生成: {today}</p>
</body>
</html>"""

    fname = f"signal_a7_v2_{today}.html"
    path  = Path(fname)
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="A7 期間指定バックテスト（日経225）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_stoch_atr_trail.py --signal           # 明日の売買シグナル（監視20銘柄）
  python backtest_stoch_atr_trail.py --signal --all     # 明日の売買シグナル（日経225全銘柄）
  python backtest_stoch_atr_trail.py                    # 直近1ヶ月バックテスト（監視20銘柄）
  python backtest_stoch_atr_trail.py --months 3         # 直近3ヶ月
  python backtest_stoch_atr_trail.py --years 1          # 直近1年
  python backtest_stoch_atr_trail.py --years 5          # 直近5年（最大）
  python backtest_stoch_atr_trail.py --days 45          # 直近45日
  python backtest_stoch_atr_trail.py 7203.T --years 2   # トヨタ 2年詳細
  python backtest_stoch_atr_trail.py --years 3 --top 50 # 3年 上位50件表示
  python backtest_stoch_atr_trail.py --all --years 1    # 日経225全銘柄 1年
""")
    parser.add_argument("symbol",   nargs="?", default=None,
                        help="特定銘柄コード（省略時は監視対象銘柄スキャン）")
    parser.add_argument("--signal", action="store_true",
                        help="明日の売買シグナルをスキャンしてHTML出力")
    parser.add_argument("--watch",  action="store_true",
                        help="監視対象20銘柄に絞る（デフォルト: 日経225全銘柄）")
    parser.add_argument("--all",    action="store_true",
                        help="日経225全銘柄をスキャン（V2ではデフォルト）")
    parser.add_argument("--days",   type=int,  default=None,
                        help="バックテスト日数（直接指定）")
    parser.add_argument("--months", type=int,  default=None,
                        help="バックテスト月数（1〜60）")
    parser.add_argument("--years",  type=int,  default=None,
                        help="バックテスト年数（1〜5）")
    parser.add_argument("--top",    type=int,  default=30,
                        help="ランキング表示件数（デフォルト: 30）")
    args = parser.parse_args()

    # V2: デフォルトは225全銘柄。--watch で監視20銘柄に切替。
    # symbols_listed_*.py が存在すれば全上場銘柄を自動使用。
    global SYMBOLS
    if args.watch:
        SYMBOLS = _WATCH_SYMBOLS_A7
    else:
        # 全上場銘柄ファイルを自動検出（prime → standard → all の順）
        _listed_symbols = None
        for _candidate in ["symbols_listed_prime.py",
                           "symbols_listed_standard.py",
                           "symbols_listed_all.py"]:
            _p = Path(_candidate)
            if _p.exists():
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location("_listed_stoch", _p)
                _mod  = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                _listed_symbols = _mod.SYMBOLS
                print(f"  銘柄ユニバース: {_candidate} ({len(_listed_symbols)}銘柄)")
                break
        if _listed_symbols:
            SYMBOLS = _listed_symbols
        # else: SYMBOLS はすでに _ALL_SYMBOLS (モジュール初期値)

    if args.signal:
        # ── シグナルスキャンモード ───────────────────────────────
        mode_label = f"A7監視対象{len(SYMBOLS)}銘柄" if args.watch else f"日経225全{len(SYMBOLS)}銘柄"
        print(f"\n  A7シグナルスキャン  ({mode_label})")
        print(f"  シグナル日: {datetime.today().strftime('%Y-%m-%d')}\n")

        target = SYMBOLS
        total  = len(target)
        stock_data: dict = {}

        print(f"  [Phase 1] データ取得中 ({total}銘柄)  ※キャッシュ済みは高速スキップ")
        skipped = 0
        for i, (sym, name) in enumerate(target, 1):
            df = fetch_df(sym, backtest_days=90)
            if df is None:
                skipped += 1
            else:
                stock_data[sym] = (name, df)
            print(f"  {i}/{total} 取得済  (スキップ: {skipped})", end="\r", flush=True)
        print(f"  {total}/{total} 完了  成功: {len(stock_data)}銘柄  スキップ: {skipped}銘柄      ")

        print(f"\n  [Phase 2] 保有中銘柄の検出（直近90日バックテスト）...")
        tasks_90 = [(s, n, d) for s, (n, d) in stock_data.items()]
        results_90: list[dict] = []

        def _bt90(task):
            return run_backtest(task[0], task[1], task[2], 90)

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(_bt90, t): t[0] for t in tasks_90}
            done = 0
            for fut in as_completed(futures):
                done += 1
                r = fut.result()
                if r:
                    results_90.append(r)
                print(f"  計算中 {done}/{len(tasks_90)}", end="\r", flush=True)
        print()

        print(f"\n  [Phase 3] シグナル解析中...")
        sig = scan_signals_a7(stock_data, results_90)
        print_signals_a7(sig)

        try:
            from portfolio import print_positions_for_signal
            print_positions_for_signal("A7")
        except ImportError:
            pass

        html_path = generate_signal_html_a7(sig)
        print(f"  HTMLレポート: {html_path.resolve()}")
        webbrowser.open(html_path.resolve().as_uri())
        print()
        return

    # 期間を日数に変換（優先順位: --days > --months > --years > デフォルト1ヶ月）
    if args.days is not None:
        days = args.days
        period_label = f"{days}日"
    elif args.months is not None:
        months = max(1, min(args.months, 60))
        days   = months * 30
        period_label = f"{months}ヶ月"
    elif args.years is not None:
        years = max(1, min(args.years, 5))
        days  = years * 365
        period_label = f"{years}年"
    else:
        days = BACKTEST_DAYS  # 30日（1ヶ月）
        period_label = "1ヶ月"

    print(f"\n  A7 直近{period_label}バックテスト  (開始: "
          f"{(datetime.today()-timedelta(days=days)).strftime('%Y-%m-%d')} ～ 本日)")
    print(f"  75MA トレンドフィルター + ATRトレイリングストップ×{ATR_TRAIL_MULT}\n")

    # 対象銘柄を絞り込み
    if args.symbol:
        sym_input = args.symbol.upper()
        if not sym_input.endswith(".T"):
            sym_input += ".T"
        target = [(s, n) for s, n in SYMBOLS if s == sym_input]
        if not target:
            nm = sym_input.replace(".T", "")
            target = [(sym_input, nm)]
    else:
        target = SYMBOLS

    total      = len(target)
    stock_data = {}  # sym -> (name, df)

    # Phase 1: 順次データ取得（yfinance はスレッドセーフでないため）
    print(f"  [Phase 1] データ取得中 ({total}銘柄)  ※キャッシュ済みは高速スキップ")
    skipped = 0
    for i, (sym, name) in enumerate(target, 1):
        df = fetch_df(sym, backtest_days=days)
        if df is None:
            skipped += 1
        else:
            stock_data[sym] = (name, df)
        print(f"  {i}/{total} 取得済  (スキップ: {skipped})", end="\r", flush=True)
    print(f"  {total}/{total} 完了  成功: {len(stock_data)}銘柄  スキップ: {skipped}銘柄      ")

    # Phase 2: 並列バックテスト計算（IO なし・スレッドセーフ）
    print(f"\n  [Phase 2] バックテスト計算中 ({len(stock_data)}銘柄, {WORKERS}並列)...")
    tasks   = [(sym, nm, df) for sym, (nm, df) in stock_data.items()]
    results = []

    def _bt(task):
        sym, nm, df = task
        return run_backtest(sym, nm, df, days)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        future_to_sym = {ex.submit(_bt, t): t[0] for t in tasks}
        done = 0
        for fut in as_completed(future_to_sym):
            done += 1
            sym = future_to_sym[fut]
            r   = fut.result()
            if r:
                results.append(r)
            print(f"  計算完了 {done}/{len(tasks)}: {sym}", end="\r", flush=True)

    print()

    if not results:
        print("\n  取引シグナルが発生した銘柄がありませんでした。\n")
        return

    # 単銘柄指定の場合は詳細表示
    if args.symbol:
        for r in results:
            print_detail(r, days)
        r = results[0]
        sign = "+" if r["ret_pct"] >= 0 else ""
        print(f"  結果: {sign}{r['total']:,.0f}円  ({sign}{r['ret_pct']:.2f}%)")
    else:
        print_ranking(results, days, args.top)

    # HTML レポートを自動生成・ブラウザで開く
    html_path = generate_html(results, days, period_label,
                              single_sym=args.symbol if args.symbol else None)
    print(f"  HTMLレポート: {html_path.resolve()}")
    webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
