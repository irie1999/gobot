"""
RSI(2) 平均回帰バックテスト  軽量版（255銘柄スキャン / 1銘柄詳細）
──────────────────────────────────────────────────────────────────
使い方:
  python rsi2.py                       # 255銘柄スキャン（1年）
  python rsi2.py --years 2             # 2年
  python rsi2.py --months 6            # 6ヶ月
  python rsi2.py --days 90             # 90日
  python rsi2.py --top 30              # 上位30銘柄のみ表示
  python rsi2.py 7011.T                # 1銘柄詳細
  python rsi2.py 7011.T --years 3      # 1銘柄 3年
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

# ── パラメーター ────────────────────────────────────────────
RSI2_ENTRY      =  10.0   # RSI(2) ≤ この値 → 翌日買い
RSI2_EXIT       =  65.0   # RSI(2) ≥ この値 → 翌日売り
MA_TREND        = 200     # トレンドフィルター（終値 > MA200 のみ）
HARD_STOP_PCT   =   3.0   # 即損切り %
HALF_PROFIT_PCT =   5.0   # 半分利確 %
ATR_TRAIL_MULT  =   2.0   # ATR トレイリング係数
POSITION_SIZE   = 100_000 # 1回あたりの投資金額（円）
BACKTEST_DAYS   =   365   # デフォルトのバックテスト日数
WORKERS         =    16   # 並列ダウンロード数

# 実行開始時刻を一度だけ固定（毎回 _TODAY を呼ぶと結果がずれる）
_TODAY = pd.Timestamp(datetime.now().date())

# ── 対象255銘柄（日経225） ───────────────────────────────────
SYMBOLS = [
    ("1332.T", "ニッスイ"), ("1333.T", "マルハニチロ"), ("1605.T", "INPEX"),
    ("1721.T", "コムシスHD"), ("1801.T", "大成建設"), ("1802.T", "大林組"),
    ("1803.T", "清水建設"), ("1808.T", "長谷工コーポレーション"), ("1812.T", "鹿島建設"),
    ("1925.T", "大和ハウス工業"), ("1928.T", "積水ハウス"), ("1963.T", "日揮HD"),
    ("2002.T", "日清製粉グループ本社"), ("2269.T", "明治HD"), ("2282.T", "日本ハム"),
    ("2413.T", "エムスリー"), ("2432.T", "ディー・エヌ・エー"), ("2501.T", "サッポロHD"),
    ("2502.T", "アサヒグループHD"), ("2503.T", "キリンHD"), ("2531.T", "宝HD"),
    ("2768.T", "双日"), ("2801.T", "キッコーマン"), ("2802.T", "味の素"),
    ("2871.T", "ニチレイ"), ("2914.T", "日本たばこ産業"),
    ("3086.T", "J.フロントリテイリング"), ("3099.T", "三越伊勢丹HD"),
    ("3105.T", "日清紡HD"), ("3289.T", "東急不動産HD"), ("3382.T", "セブン&アイHD"),
    ("3401.T", "帝人"), ("3402.T", "東レ"), ("3405.T", "クラレ"), ("3407.T", "旭化成"),
    ("3436.T", "SUMCO"), ("3861.T", "王子HD"), ("3863.T", "日本製紙"),
    ("4004.T", "レゾナックHD"), ("4005.T", "住友化学"), ("4021.T", "日産化学"),
    ("4042.T", "東ソー"), ("4043.T", "トクヤマ"), ("4061.T", "デンカ"),
    ("4063.T", "信越化学工業"), ("4183.T", "三井化学"), ("4188.T", "三菱ケミカルG"),
    ("4208.T", "UBE"), ("4272.T", "日本化薬"), ("4307.T", "野村総合研究所"),
    ("4324.T", "電通グループ"), ("4502.T", "武田薬品工業"), ("4503.T", "アステラス製薬"),
    ("4506.T", "住友ファーマ"), ("4507.T", "塩野義製薬"), ("4519.T", "中外製薬"),
    ("4523.T", "エーザイ"), ("4543.T", "テルモ"), ("4568.T", "第一三共"),
    ("4578.T", "大塚HD"), ("4631.T", "DIC"), ("4689.T", "LINEヤフー"),
    ("4704.T", "トレンドマイクロ"), ("4751.T", "サイバーエージェント"),
    ("4755.T", "楽天グループ"), ("4901.T", "富士フイルムHD"), ("4902.T", "コニカミノルタ"),
    ("5019.T", "出光興産"), ("5020.T", "ENEOSホールディングス"),
    ("5101.T", "横浜ゴム"), ("5108.T", "ブリヂストン"), ("5201.T", "AGC"),
    ("5214.T", "日本電気硝子"), ("5232.T", "住友大阪セメント"), ("5233.T", "太平洋セメント"),
    ("5301.T", "東海カーボン"), ("5332.T", "TOTO"), ("5333.T", "日本碍子"),
    ("5401.T", "日本製鉄"), ("5406.T", "神戸製鋼所"), ("5411.T", "JFEホールディングス"),
    ("5631.T", "日本製鋼所"), ("5703.T", "日本軽金属HD"), ("5706.T", "三井金属鉱業"),
    ("5707.T", "東邦亜鉛"), ("5711.T", "三菱マテリアル"), ("5713.T", "住友金属鉱山"),
    ("5714.T", "DOWAホールディングス"), ("5741.T", "UACJ"), ("5801.T", "古河電気工業"),
    ("5802.T", "住友電気工業"), ("5803.T", "フジクラ"), ("6098.T", "リクルートHD"),
    ("6103.T", "オークマ"), ("6113.T", "アマダ"), ("6178.T", "日本郵政"),
    ("6273.T", "SMC"), ("6301.T", "小松製作所"), ("6302.T", "住友重機械工業"),
    ("6305.T", "日立建機"), ("6326.T", "クボタ"), ("6361.T", "荏原製作所"),
    ("6367.T", "ダイキン工業"), ("6370.T", "栗田工業"), ("6383.T", "ダイフク"),
    ("6406.T", "フジテック"), ("6412.T", "平和"), ("6417.T", "SANKYO"),
    ("6471.T", "日本精工"), ("6472.T", "NTN"), ("6473.T", "ジェイテクト"),
    ("6479.T", "ミネベアミツミ"), ("6501.T", "日立製作所"), ("6503.T", "三菱電機"),
    ("6504.T", "富士電機"), ("6506.T", "安川電機"), ("6526.T", "ソシオネクスト"),
    ("6532.T", "ベイカレントコンサルティング"), ("6586.T", "マキタ"), ("6594.T", "ニデック"),
    ("6645.T", "オムロン"), ("6674.T", "GSユアサ"), ("6701.T", "NEC"),
    ("6702.T", "富士通"), ("6723.T", "ルネサスエレクトロニクス"), ("6724.T", "セイコーエプソン"),
    ("6752.T", "パナソニックHD"), ("6753.T", "シャープ"), ("6754.T", "アンリツ"),
    ("6758.T", "ソニーグループ"), ("6762.T", "TDK"), ("6770.T", "アルプスアルパイン"),
    ("6806.T", "ヒロセ電機"), ("6841.T", "横河電機"), ("6857.T", "アドバンテスト"),
    ("6861.T", "キーエンス"), ("6902.T", "デンソー"), ("6952.T", "カシオ計算機"),
    ("6954.T", "ファナック"), ("6971.T", "京セラ"), ("6981.T", "村田製作所"),
    ("6988.T", "日東電工"), ("7003.T", "三井E&S"), ("7011.T", "三菱重工業"),
    ("7012.T", "川崎重工業"), ("7013.T", "IHI"), ("7201.T", "日産自動車"),
    ("7202.T", "いすゞ自動車"), ("7203.T", "トヨタ自動車"), ("7205.T", "日野自動車"),
    ("7211.T", "三菱自動車工業"), ("7261.T", "マツダ"), ("7267.T", "本田技研工業"),
    ("7270.T", "SUBARU"), ("7272.T", "ヤマハ発動機"), ("7731.T", "ニコン"),
    ("7733.T", "オリンパス"), ("7735.T", "SCREENホールディングス"), ("7741.T", "HOYA"),
    ("7751.T", "キヤノン"), ("7752.T", "リコー"), ("7762.T", "シチズン時計"),
    ("7832.T", "バンダイナムコHD"), ("7951.T", "ヤマハ"), ("8001.T", "伊藤忠商事"),
    ("8002.T", "丸紅"), ("8015.T", "豊田通商"), ("8031.T", "三井物産"),
    ("8035.T", "東京エレクトロン"), ("8053.T", "住友商事"), ("8058.T", "三菱商事"),
    ("8233.T", "高島屋"), ("8252.T", "丸井グループ"), ("8253.T", "クレディセゾン"),
    ("8267.T", "イオン"), ("8303.T", "新生銀行"), ("8304.T", "あおぞら銀行"),
    ("8306.T", "三菱UFJフィナンシャルG"), ("8308.T", "りそなHD"),
    ("8309.T", "三井住友トラストHD"), ("8316.T", "三井住友フィナンシャルG"),
    ("8331.T", "千葉銀行"), ("8354.T", "ふくおかFG"), ("8377.T", "ほくほくFG"),
    ("8411.T", "みずほフィナンシャルG"), ("8601.T", "大和証券グループ本社"),
    ("8604.T", "野村HD"), ("8628.T", "松井証券"), ("8630.T", "SOMPOホールディングス"),
    ("8697.T", "日本取引所グループ"), ("8725.T", "MS&ADインシュアランスG"),
    ("8750.T", "第一生命HD"), ("8766.T", "東京海上HD"), ("8795.T", "T&Dホールディングス"),
    ("8801.T", "三井不動産"), ("8802.T", "三菱地所"), ("8804.T", "東京建物"),
    ("8830.T", "住友不動産"), ("9001.T", "東武鉄道"), ("9005.T", "東急"),
    ("9007.T", "小田急電鉄"), ("9008.T", "京王電鉄"), ("9009.T", "京成電鉄"),
    ("9020.T", "東日本旅客鉄道"), ("9021.T", "西日本旅客鉄道"), ("9022.T", "東海旅客鉄道"),
    ("9064.T", "ヤマトHD"), ("9101.T", "日本郵船"), ("9104.T", "商船三井"),
    ("9107.T", "川崎汽船"), ("9202.T", "ANAホールディングス"), ("9301.T", "三菱倉庫"),
    ("9432.T", "日本電信電話"), ("9433.T", "KDDI"), ("9434.T", "ソフトバンク"),
    ("9501.T", "東京電力HD"), ("9502.T", "中部電力"), ("9503.T", "関西電力"),
    ("9531.T", "東京ガス"), ("9532.T", "大阪ガス"), ("9602.T", "東宝"),
    ("9735.T", "セコム"), ("9766.T", "コナミグループ"),
    ("9983.T", "ファーストリテイリング"), ("9984.T", "ソフトバンクグループ"),
]


# ── データ取得 ──────────────────────────────────────────────

# キャッシュディレクトリ
_CACHE_DIR = Path(".rsi2_cache")


def fetch_nikkei(backtest_days: int) -> pd.DataFrame | None:
    """日経平均(^N225)を取得し MA25・MA200 を付与する。"""
    buf = 200 + 30
    dl_start = (_TODAY - timedelta(days=backtest_days + buf)).strftime("%Y-%m-%d")
    dl_end   = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.download("^N225", start=dl_start, end=dl_end,
                          interval="1d", auto_adjust=False, progress=False,
                          multi_level_index=False)
        if raw.empty:
            return None
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        s = raw["close"].dropna()
        df = pd.DataFrame({"close": s.to_numpy(dtype=float)}, index=s.index)
        df["ma25"]  = df["close"].rolling(25).mean()
        df["ma200"] = df["close"].rolling(200).mean()
        return df
    except Exception:
        return None


def _market_info(nikkei_df: pd.DataFrame | None) -> dict:
    """直近の日経平均・MA25・MA200 から地合い情報を返す。"""
    if nikkei_df is None or nikkei_df.empty:
        return {"ok": False}
    filtered = nikkei_df.dropna(subset=["ma25", "ma200"])
    if filtered.empty:
        return {"ok": False}
    row = filtered.iloc[-1]
    cl, ma25, ma200 = float(row["close"]), float(row["ma25"]), float(row["ma200"])
    above25  = cl > ma25
    above200 = cl > ma200
    if above25 and above200:
        phase, phase_cls = "リスクオン  ▲上昇トレンド", "on"
    elif above200:
        phase, phase_cls = "要注意  MA200上・MA25割れ",  "caution"
    else:
        phase, phase_cls = "リスクオフ  ▼下降トレンド", "off"
    return dict(ok=True, close=cl, ma25=ma25, ma200=ma200,
                above25=above25, above200=above200,
                phase=phase, phase_cls=phase_cls)


def fetch(symbol: str, backtest_days: int) -> pd.DataFrame | None:
    # ── 優先: fetch_all.py が保存した永続キャッシュ ──────────────
    # ファイル名: .rsi2_cache/{symbol}.pkl（日付なし・長期保存）
    # 鮮度チェック: 最終行が7営業日以上古い場合はキャッシュを無視して再取得
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            with open(persistent, "rb") as f:
                df = pickle.load(f)
            last_date = df.index[-1]
            stale = last_date < (_TODAY - timedelta(days=10))  # 土日祝考慮で10日
            if len(df) >= 210 and not stale:
                return df
            # 古い場合はキャッシュを削除して再取得
            persistent.unlink(missing_ok=True)
        except Exception:
            persistent.unlink(missing_ok=True)

    # ── フォールバック: 当日限りの一時キャッシュ ──────────────────
    buf_days = 200 + 30
    dl_start = (_TODAY - timedelta(days=backtest_days + buf_days)).strftime("%Y-%m-%d")
    dl_end   = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")

    _CACHE_DIR.mkdir(exist_ok=True)
    cache_key  = f"{symbol.replace('.','_')}_{_TODAY.strftime('%Y%m%d')}_{dl_start}_{dl_end}"
    cache_file = _CACHE_DIR / f"{cache_key}.pkl"

    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                return pickle.load(f)
        except Exception:
            cache_file.unlink(missing_ok=True)

    try:
        raw = yf.download(symbol, start=dl_start, end=dl_end,
                          interval="1d", auto_adjust=False, progress=False,
                          multi_level_index=False)
        if raw.empty:
            return None
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        if len(raw) < 210:
            return None
        df = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
        with open(cache_file, "wb") as f:
            pickle.dump(df, f)
        return df
    except Exception:
        return None


# ── 指標計算 ────────────────────────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    c    = df["close"]
    h    = df["high"]
    l    = df["low"]
    prev = c.shift(1)

    d    = c.diff()
    gain = d.clip(lower=0).ewm(com=1,  adjust=False).mean()   # Wilder α=1/2
    loss = (-d).clip(lower=0).ewm(com=1, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    g14   = d.clip(lower=0).ewm(com=13, adjust=False).mean()   # Wilder α=1/14
    l14   = (-d).clip(lower=0).ewm(com=13, adjust=False).mean()
    rsi14 = 100 - 100 / (1 + g14 / l14.replace(0, np.nan))

    ma200 = c.rolling(200).mean()
    ma50  = c.rolling(50).mean()
    tr    = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr   = tr.ewm(com=13, adjust=False).mean()                # Wilder α=1/14

    df = df.copy()
    df["rsi2"]  = rsi2
    df["rsi14"] = rsi14
    df["ma200"] = ma200
    df["ma50"]  = ma50
    df["atr"]   = atr
    return df


# ── バックテスト ────────────────────────────────────────────
def backtest(df: pd.DataFrame, backtest_days: int) -> list[dict]:
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()

    trades    = []
    in_pos    = False
    entry_p   = trail = 0.0
    entry_dt  = None
    half_done = False
    qty       = 0

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]

        if pd.isna(prev["rsi2"]) or pd.isna(prev["ma200"]):
            continue

        op = float(row["open"])
        lo = float(row["low"])

        if in_pos:
            exit_p = reason = None

            if lo <= entry_p * (1 - HARD_STOP_PCT / 100):
                exit_p = op   # 当日始値（実際の市場データ）で決済
                reason = "損切り"
            elif lo <= trail:
                exit_p = op   # 当日始値（実際の市場データ）で決済
                reason = "トレイリング"
            elif float(prev["rsi2"]) >= RSI2_EXIT:
                exit_p = op
                reason = "RSI2回復"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p, qty=qty,
                    pnl=pnl, pct=pct, hold=(dt - entry_dt).days, reason=reason,
                ))
                in_pos = half_done = False
                continue

            if not half_done:
                cl = float(row["close"])
                if (cl - entry_p) / entry_p * 100 >= HALF_PROFIT_PCT:
                    hq = qty // 2
                    if hq > 0:
                        pct_h = (cl - entry_p) / entry_p * 100
                        trades.append(dict(
                            entry_dt=entry_dt, exit_dt=dt,
                            entry_p=entry_p, exit_p=cl, qty=hq,
                            pnl=(cl - entry_p) * hq, pct=pct_h,
                            hold=(dt - entry_dt).days, reason="半分利確",
                        ))
                        qty -= hq
                        half_done = True

            cand = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
            if cand > trail:
                trail = cand

        if not in_pos:
            if (float(prev["rsi2"]) <= RSI2_ENTRY
                    and float(prev["close"]) > float(prev["ma200"])
                    and op > 0):
                qty = max(int(POSITION_SIZE / op), 1)
                entry_p  = op
                trail    = op - float(row["atr"]) * ATR_TRAIL_MULT
                entry_dt = dt
                half_done = False
                in_pos   = True

    if in_pos:
        lp = float(df.iloc[-1]["close"])
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=lp, qty=qty,
            pnl=(lp - entry_p) * qty,
            pct=(lp - entry_p) / entry_p * 100,
            hold=(df.index[-1] - entry_dt).days, reason="保有中★",
        ))

    return trades


# ── ターミナル表示 ──────────────────────────────────────────
def print_result(symbol: str, trades: list[dict],
                 backtest_days: int, label: str) -> None:
    since = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = _TODAY.strftime("%Y-%m-%d")

    if not trades:
        print(f"\n  [{symbol}]  シグナルなし（{since} ～ {today}）\n")
        return

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / len(trades) * 100
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0
    pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"

    print()
    print("═" * 66)
    print(f"  RSI(2) 平均回帰  [{symbol}]  直近{label}")
    print(f"  期間: {since} ～ {today}")
    print(f"  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / "
          f"-{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確")
    print("═" * 66)
    print(f"  トレード: {len(trades)}回  勝: {len(wins)}  負: {len(loss)}")
    print(f"  勝率: {wr:.1f}%   PF: {pf_s}   損益: {total:+,.0f}円")
    print(f"  平均保有: 勝ち {wh:.1f}日 / 負け {lh:.1f}日")
    print()
    print(f"  {'#':<3} {'エントリー':>10} {'エグジット':>10} "
          f"{'買値':>8} {'売値':>8} {'株数':>4} {'損益':>10} 保有  決済理由")
    print("  " + "─" * 66)
    for i, t in enumerate(trades, 1):
        pct  = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        mark = "★" if "保有中" in t["reason"] else " "
        print(f" {mark}{i:<3} "
              f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} "
              f"{t['qty']:>4} {t['pnl']:>+10,.0f} "
              f"{t['hold']:>3}日  {t['reason']}({pct:+.1f}%)")
    print("  " + "─" * 66)
    print(f"  合計: {total:+,.0f}円")
    print()


# ── HTML レポート生成 ───────────────────────────────────────
def generate_html(symbol: str, trades: list[dict],
                  backtest_days: int, label: str) -> Path:
    since = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = _TODAY.strftime("%Y-%m-%d")

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades) if trades else 0
    wr    = len(wins) / len(trades) * 100 if trades else 0
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0

    # チャート用データ（累積損益）
    cum = 0.0
    chart_dates, chart_cum = [], []
    for t in trades:
        cum += t["pnl"]
        chart_dates.append(t["exit_dt"].strftime("%Y-%m-%d"))
        chart_cum.append(round(cum, 0))

    # トレード行
    rows = ""
    for i, t in enumerate(trades, 1):
        pct = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        cls = "hold" if "保有中" in t["reason"] else ("win" if t["pnl"] > 0 else "lose")
        rows += (
            f'<tr class="{cls}">'
            f'<td>{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["entry_p"]:,.0f}</td>'
            f'<td>{t["exit_p"]:,.0f}</td>'
            f'<td>{t["qty"]}</td>'
            f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
            f'<td class="{"pos" if pct>=0 else "neg"}">{pct:+.1f}%</td>'
            f'<td>{t["hold"]}日</td>'
            f'<td>{t["reason"]}</td>'
            f'</tr>\n'
        )

    total_cls = "pos" if total >= 0 else "neg"

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI(2) [{symbol}] バックテスト結果</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #3a86ff;
    padding-left:12px;margin-bottom:6px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 0 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;
       padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.chart-wrap{{background:#16192a;border:1px solid #252840;border-radius:10px;
             padding:16px;margin:20px 0;max-width:900px}}
table{{width:100%;border-collapse:collapse;font-size:0.86em;margin-top:16px}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3),th:last-child{{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3),td:last-child{{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
</style>
</head>
<body>
<h1>RSI(2) 平均回帰バックテスト  [{symbol}]  直近{label}</h1>
<div class="meta">期間: {since} ～ {today}</div>
<div class="meta">
  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値エントリー
  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル /
  -{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確
</div>

<div class="cards">
  <div class="card"><div class="clabel">損益合計</div>
    <div class="cval {total_cls}">{total:+,.0f}円</div></div>
  <div class="card"><div class="clabel">勝率</div>
    <div class="cval neu">{wr:.1f}%</div></div>
  <div class="card"><div class="clabel">プロフィットF</div>
    <div class="cval neu">{pf_s}</div></div>
  <div class="card"><div class="clabel">トレード数</div>
    <div class="cval neu">{len(trades)}回</div></div>
  <div class="card"><div class="clabel">勝/負</div>
    <div class="cval neu">{len(wins)}勝{len(loss)}負</div></div>
  <div class="card"><div class="clabel">平均保有(勝)</div>
    <div class="cval pos">{wh:.1f}日</div></div>
  <div class="card"><div class="clabel">平均保有(負)</div>
    <div class="cval neg">{lh:.1f}日</div></div>
</div>

<div class="chart-wrap">
  <canvas id="cumChart" height="80"></canvas>
</div>

<table>
<thead><tr>
  <th>#</th><th>エントリー</th><th>エグジット</th>
  <th>買値</th><th>売値</th><th>株数</th>
  <th>損益</th><th>変化率</th><th>保有日数</th><th>決済理由</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>

<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

<script>
new Chart(document.getElementById('cumChart'), {{
  type: 'line',
  data: {{
    labels: {chart_dates},
    datasets: [{{
      label: '累積損益（円）',
      data: {chart_cum},
      borderColor: '#3a86ff',
      backgroundColor: 'rgba(58,134,255,0.08)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: '#3a86ff',
      fill: true,
      tension: 0.3,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ labels: {{ color: '#aaa' }} }},
      tooltip: {{ callbacks: {{ label: c => c.parsed.y.toLocaleString() + '円' }} }}
    }},
    scales: {{
      x: {{ ticks: {{ color: '#666', maxTicksLimit: 12 }}, grid: {{ color: '#1e2030' }} }},
      y: {{ ticks: {{ color: '#666', callback: v => v.toLocaleString() + '円' }},
             grid: {{ color: '#1e2030' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""

    path = Path(f"rsi2_{symbol.replace('.','_')}_{_TODAY.strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── ランキング表示（スキャンモード） ───────────────────────
def print_ranking(results: list[dict], days: int, label: str,
                  top: int | None = None, mkt: dict | None = None) -> None:
    since  = (_TODAY - timedelta(days=days)).strftime("%Y-%m-%d")
    today  = _TODAY.strftime("%Y-%m-%d")
    ranked = sorted(results, key=lambda x: (-x["total_pct"], x["symbol"]))
    disp   = ranked[:top] if top else ranked

    total_pnl = sum(r["total"] for r in results)
    total_tr  = sum(r["trades"] for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)

    print()
    print("═" * 72)
    print(f"  RSI(2) 平均回帰  {len(SYMBOLS)}銘柄スキャン  直近{label}"
          + (f"  上位{top}銘柄" if top else ""))
    print(f"  期間: {since} ～ {today}")
    if mkt and mkt.get("ok"):
        a25  = "▲" if mkt["above25"]  else "▼"
        a200 = "▲" if mkt["above200"] else "▼"
        print(f"  【地合い】日経平均 {mkt['close']:,.0f}円  "
              f"MA25 {mkt['ma25']:,.0f}{a25}  MA200 {mkt['ma200']:,.0f}{a200}  "
              f"→ {mkt['phase']}")
    print(f"  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / "
          f"-{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確")
    print("═" * 72)
    print(f"  スキャン: {len(SYMBOLS)}銘柄  シグナルあり: {len(results)}銘柄  "
          f"トレード計: {total_tr}回  プラス銘柄: {plus_cnt}/{len(results)}")
    print()
    print(f"  {'順位':<4} {'銘柄':<22} {'損益':>10} {'累積%':>7} {'勝率':>6} "
          f"{'PF':>5} {'取引':>4} {'平均保有':>7}")
    print("  " + "─" * 68)
    for rank, r in enumerate(disp, 1):
        pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        sign  = "+" if r["total"] >= 0 else ""
        psign = "+" if r["total_pct"] >= 0 else ""
        bar   = ("▲" if r["total_pct"] >= 0 else "▽") * min(int(abs(r["total_pct"]) / 3), 8)
        label2 = f"{r['name']}({r['symbol']})"
        print(f"  {rank:<4} {label2:<22} "
              f"{sign}{r['total']:>9,.0f}円  "
              f"{psign}{r['total_pct']:>5.1f}%  "
              f"{r['wr']:>5.1f}%  {pf_s:>5}  "
              f"{r['trades']:>3}回  {r['avg_hold']:>5.1f}日  {bar}")
    print("  " + "─" * 60)
    sign = "+" if total_pnl >= 0 else ""
    print(f"  合計損益（全銘柄・重複あり）: {sign}{total_pnl:,.0f}円")
    print()


# ── スキャン用 HTML ─────────────────────────────────────────
def _mkt_banner_html(mkt: dict | None) -> str:
    """地合いバナーの HTML を返す。mkt が None または ok=False なら空文字。"""
    if not mkt or not mkt.get("ok"):
        return '<div class="mkt-banner mkt-unknown"><span>地合い情報を取得できませんでした</span></div>'
    cls  = {"on": "mkt-on", "caution": "mkt-caution", "off": "mkt-off"}.get(mkt["phase_cls"], "mkt-unknown")
    a25  = "▲" if mkt["above25"]  else "▼"
    a200 = "▲" if mkt["above200"] else "▼"
    c25  = "#4ade80" if mkt["above25"]  else "#f87171"
    c200 = "#4ade80" if mkt["above200"] else "#f87171"
    return f'''\
<div class="mkt-banner {cls}">
  <div class="mkt-item"><div class="mkt-lbl">地合い</div>
    <div class="mkt-val">{mkt["phase"]}</div></div>
  <div class="mkt-item"><div class="mkt-lbl">日経平均</div>
    <div class="mkt-val">{mkt["close"]:,.0f}円</div></div>
  <div class="mkt-item"><div class="mkt-lbl">MA25</div>
    <div class="mkt-val" style="color:{c25}">{mkt["ma25"]:,.0f} {a25}</div></div>
  <div class="mkt-item"><div class="mkt-lbl">MA200</div>
    <div class="mkt-val" style="color:{c200}">{mkt["ma200"]:,.0f} {a200}</div></div>
</div>'''


def generate_html_scan(results: list[dict], days: int, label: str,
                       top: int | None = None, mkt: dict | None = None) -> Path:
    since  = (_TODAY - timedelta(days=days)).strftime("%Y-%m-%d")
    today  = _TODAY.strftime("%Y-%m-%d")
    ranked = sorted(results, key=lambda x: (-x["total_pct"], x["symbol"]))
    disp   = ranked[:top] if top else ranked

    total_pnl = sum(r["total"] for r in results)
    total_tr  = sum(r["trades"] for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)
    total_cls = "pos" if total_pnl >= 0 else "neg"
    top_label = f"  上位{top}銘柄" if top else ""

    # ランキング行
    rank_rows = ""
    for rank, r in enumerate(disp, 1):
        pf_s = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        cls  = "win" if r["total"] >= 0 else "lose"
        pcls = "pos" if r["total"] >= 0 else "neg"
        rank_rows += (
            f'<tr class="{cls}" onclick="toggleDetail(\'{r["symbol"]}\')" style="cursor:pointer">'
            f'<td>{rank}</td>'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["name"]}</td>'
            f'<td class="{pcls}">{r["total"]:+,.0f}円</td>'
            f'<td>{r["wr"]:.1f}%</td>'
            f'<td>{pf_s}</td>'
            f'<td>{r["trades"]}</td>'
            f'<td>{r["avg_hold"]:.1f}日</td>'
            f'</tr>\n'
            f'<tr id="detail-{r["symbol"]}" style="display:none">'
            f'<td colspan="8" style="padding:0">{_trade_table(r["trade_log"])}</td>'
            f'</tr>\n'
        )

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI(2) {len(SYMBOLS)}銘柄スキャン {label}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #3a86ff;
    padding-left:12px;margin-bottom:6px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 0 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;
       padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.chart-wrap{{background:#16192a;border:1px solid #252840;border-radius:10px;
             padding:16px;margin:20px 0}}
table{{width:100%;border-collapse:collapse;font-size:0.86em}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3){{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.inner-table{{width:100%;font-size:0.84em;background:#0d0f1a}}
.inner-table th{{background:#0d0f1a;font-size:0.8em}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
.mkt-banner{{border-radius:8px;padding:10px 16px;margin:14px 0;
             font-size:0.9em;display:flex;align-items:center;gap:18px;flex-wrap:wrap}}
.mkt-on     {{background:#0f2a1a;border:1px solid #166534;color:#4ade80}}
.mkt-caution{{background:#2a1f0a;border:1px solid #92400e;color:#fbbf24}}
.mkt-off    {{background:#2a0f0f;border:1px solid #7f1d1d;color:#f87171}}
.mkt-unknown{{background:#16192a;border:1px solid #252840;color:#666}}
.mkt-item{{display:flex;flex-direction:column;align-items:center;gap:2px}}
.mkt-lbl{{font-size:0.75em;opacity:.7}}
.mkt-val{{font-size:1.1em;font-weight:700}}
</style>
</head>
<body>
<h1>RSI(2) 平均回帰  {len(SYMBOLS)}銘柄スキャン  直近{label}{top_label}</h1>
<div class="meta">期間: {since} ～ {today}</div>
<div class="meta">【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値エントリー
  / 【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル /
  -{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確</div>
{_mkt_banner_html(mkt)}
<div class="cards">
  <div class="card"><div class="clabel">合計損益</div>
    <div class="cval {total_cls}">{total_pnl:+,.0f}円</div></div>
  <div class="card"><div class="clabel">スキャン銘柄</div>
    <div class="cval neu">{len(SYMBOLS)}銘柄</div></div>
  <div class="card"><div class="clabel">シグナルあり</div>
    <div class="cval neu">{len(results)}銘柄</div></div>
  <div class="card"><div class="clabel">トレード計</div>
    <div class="cval neu">{total_tr}回</div></div>
  <div class="card"><div class="clabel">プラス銘柄</div>
    <div class="cval pos">{plus_cnt}/{len(results)}</div></div>
</div>

<div class="chart-wrap">
  <canvas id="barChart" height="60"></canvas>
</div>

<p style="color:#666;font-size:0.82em;margin-bottom:8px">
  ▼ 行をクリックするとトレード明細を展開
</p>
<table>
<thead><tr>
  <th>#</th><th>コード</th><th>銘柄名</th>
  <th>損益</th><th>勝率</th><th>PF</th><th>取引数</th><th>平均保有</th>
</tr></thead>
<tbody>
{rank_rows}
</tbody>
</table>

<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

<script>
const labels = {[r['name'] for r in disp]};
const vals   = {[round(r['total']) for r in disp]};
const colors = vals.map(v => v >= 0 ? 'rgba(74,222,128,0.7)' : 'rgba(248,113,113,0.7)');
new Chart(document.getElementById('barChart'), {{
  type: 'bar',
  data: {{ labels, datasets: [{{ label:'損益(円)', data:vals, backgroundColor:colors, borderRadius:3 }}] }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display:false }},
      tooltip: {{ callbacks: {{ label: c => c.parsed.y.toLocaleString()+'円' }} }} }},
    scales: {{
      x: {{ ticks: {{ color:'#555', font:{{size:10}}, maxRotation:45 }}, grid:{{ color:'#1e2030' }} }},
      y: {{ ticks: {{ color:'#555', callback: v => v.toLocaleString()+'円' }}, grid:{{ color:'#1e2030' }} }}
    }}
  }}
}});
function toggleDetail(sym) {{
  const el = document.getElementById('detail-' + sym);
  el.style.display = el.style.display === 'none' ? '' : 'none';
}}
</script>
</body>
</html>"""

    path = Path(f"rsi2_scan_{_TODAY.strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


def _trade_table(trades: list[dict]) -> str:
    """トレード明細の内部テーブルHTML"""
    rows = ""
    for i, t in enumerate(trades, 1):
        pct = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        cls = "hold" if "保有中" in t["reason"] else ("win" if t["pnl"] > 0 else "lose")
        rows += (
            f'<tr class="{cls}">'
            f'<td>{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["entry_p"]:,.0f}</td><td>{t["exit_p"]:,.0f}</td>'
            f'<td>{t["qty"]}</td>'
            f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
            f'<td class="{"pos" if pct>=0 else "neg"}">{pct:+.1f}%</td>'
            f'<td>{t["hold"]}日</td><td>{t["reason"]}</td>'
            f'</tr>\n'
        )
    return (
        '<table class="inner-table"><thead><tr>'
        '<th>#</th><th>エントリー</th><th>エグジット</th>'
        '<th>買値</th><th>売値</th><th>株数</th>'
        '<th>損益</th><th>変化率</th><th>保有日数</th><th>決済理由</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )


# ── メイン ──────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="RSI(2) 平均回帰バックテスト（255銘柄スキャン / 1銘柄詳細）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python rsi2.py                       # 255銘柄スキャン（1年）
  python rsi2.py --years 2             # 2年スキャン
  python rsi2.py --months 6            # 6ヶ月スキャン
  python rsi2.py --days 90             # 90日スキャン
  python rsi2.py --top 30              # 上位30銘柄のみ表示
  python rsi2.py 7011.T                # 三菱重工 詳細（1年）
  python rsi2.py 7203.T --years 3      # トヨタ 3年
""")
    parser.add_argument("symbol",  nargs="?", default=None,
                        help="銘柄コード（省略時は255銘柄スキャン）")
    parser.add_argument("--days",   type=int, default=None, help="バックテスト日数")
    parser.add_argument("--months", type=int, default=None, help="バックテスト月数")
    parser.add_argument("--years",  type=int, default=None, help="バックテスト年数")
    parser.add_argument("--top",    type=int, default=None,
                        help="上位N銘柄のみ表示（スキャンモード）")
    args = parser.parse_args()

    if args.days is not None:
        days, label = args.days, f"{args.days}日"
    elif args.months is not None:
        days, label = args.months * 30, f"{args.months}ヶ月"
    elif args.years is not None:
        days, label = args.years * 365, f"{args.years}年"
    else:
        days, label = BACKTEST_DAYS, "1年"

    # ── 1銘柄詳細モード ─────────────────────────────────────
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        print(f"\n  データ取得中: {sym} ...")
        df = fetch(sym, days)
        if df is None:
            print(f"  エラー: {sym} のデータ取得に失敗しました\n")
            return
        df     = calc(df)
        trades = backtest(df, days)
        print_result(sym, trades, days, label)
        path = generate_html(sym, trades, days, label)
        print(f"  HTMLレポート保存: {path}")
        webbrowser.open(f"file://{path.resolve()}")
        return

    # ── 255銘柄スキャンモード ────────────────────────────────
    print(f"\n  RSI(2) 平均回帰  {len(SYMBOLS)}銘柄データ取得中 ...")

    stock_data: dict = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        nk_fut  = ex.submit(fetch_nikkei, days)           # 日経平均を並行取得
        futs = {ex.submit(fetch, sym, days): (sym, name) for sym, name in SYMBOLS}
        done = 0
        for fut in as_completed(futs):
            sym, name = futs[fut]
            done += 1
            print(f"\r  取得中 {done}/{len(SYMBOLS)} ...", end="", flush=True)
            df = fut.result()
            if df is not None:
                stock_data[sym] = (name, df)
        nk_df = nk_fut.result()
    print(f"\r  取得完了: {len(stock_data)}/{len(SYMBOLS)} 銘柄              ")

    mkt = _market_info(nk_df)

    results = []
    for sym, (name, df) in stock_data.items():
        trades = backtest(calc(df), days)
        if not trades:
            continue
        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        wr    = len(wins) / len(trades) * 100
        pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                 if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        total_pct = sum(t["pct"] for t in trades)
        results.append(dict(
            symbol=sym, name=name,
            trades=len(trades), total=total, total_pct=total_pct,
            wr=wr, pf=pf,
            avg_hold=sum(t["hold"] for t in trades) / len(trades),
            trade_log=trades,
        ))

    if not results:
        print("  シグナルが発生した銘柄がありませんでした。")
        return

    print_ranking(results, days, label, top=args.top, mkt=mkt)
    path = generate_html_scan(results, days, label, top=args.top, mkt=mkt)
    print(f"  HTMLレポート保存: {path}")
    webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
