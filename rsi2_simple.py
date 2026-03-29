"""
RSI(2) 平均回帰戦略  軽量版（255銘柄スキャン対応）
─────────────────────────────────────────────────
使い方:
  python rsi2_simple.py                                  # 255銘柄スキャン（1年）→ ブラウザ自動表示
  python rsi2_simple.py --years 2                        # 2年
  python rsi2_simple.py --start 2023-01-01               # 開始日指定
  python rsi2_simple.py --start 2023-01-01 --end 2024-06-30
  python rsi2_simple.py 7011.T                           # 1銘柄詳細 → ブラウザ自動表示
  python rsi2_simple.py 7011.T --start 2022-01-01 --end 2023-12-31
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

# ── 対象255銘柄（日経225 + α） ──────────────────────────────────
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
    ("3086.T", "J.フロントリテイリング"),
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
    ("4004.T", "レゾナックHD"),
    ("4005.T", "住友化学"),
    ("4021.T", "日産化学"),
    ("4042.T", "東ソー"),
    ("4043.T", "トクヤマ"),
    ("4061.T", "デンカ"),
    ("4063.T", "信越化学工業"),
    ("4183.T", "三井化学"),
    ("4188.T", "三菱ケミカルG"),
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
    ("6532.T", "ベイカレントコンサルティング"),
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

# ── パラメーター ────────────────────────────────────────────────
RSI2_ENTRY      =  10.0   # RSI(2) がこの値以下 → 翌日買い
RSI2_EXIT       =  65.0   # RSI(2) がこの値以上 → 翌日売り
MA_TREND        = 200     # トレンドフィルター: 終値 > MA200 のみ対象
HARD_STOP_PCT   =   3.0   # 即損切り %
HALF_PROFIT_PCT =   5.0   # 半分利確 %
ATR_TRAIL_MULT  =   2.0   # ATR トレイリング係数
POSITION_SIZE   = 50_000  # 1回あたりの購入金額（円）
BACKTEST_YEARS  =   1     # デフォルトのバックテスト期間
WORKERS         =  16     # 並列ダウンロード数


# ── 期間解決 ────────────────────────────────────────────────────
def resolve_dates(args) -> tuple[pd.Timestamp, pd.Timestamp]:
    """--start / --end / --days / --years から (start, end) を確定する"""
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp(datetime.today().date())
    if args.start:
        start = pd.Timestamp(args.start)
    elif args.days is not None:
        start = end - timedelta(days=args.days)
    else:
        start = end - pd.DateOffset(years=args.years)
    if start >= end:
        raise ValueError(f"--start ({start.date()}) は --end ({end.date()}) より前の日付にしてください")
    return start, end


# キャッシュディレクトリ（当日分を保存し、翌日以降は自動的に再取得）
_CACHE_DIR = Path(".rsi2_cache")


# ── データ取得 ──────────────────────────────────────────────────
def fetch(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None:
    buf_days = 200 + 30
    dl_start = (start - timedelta(days=buf_days)).strftime("%Y-%m-%d")
    dl_end   = (end   + timedelta(days=1)).strftime("%Y-%m-%d")

    # キャッシュファイル: .rsi2_cache/7011T_20260329_start_end.pkl
    _CACHE_DIR.mkdir(exist_ok=True)
    cache_key  = f"{symbol.replace('.','_')}_{end.strftime('%Y%m%d')}_{dl_start}_{dl_end}"
    cache_file = _CACHE_DIR / f"{cache_key}.pkl"

    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                return pickle.load(f)
        except Exception:
            cache_file.unlink(missing_ok=True)

    try:
        raw = yf.download(symbol, start=dl_start, end=dl_end,
                          interval="1d", auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
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


# ── 指標計算 ────────────────────────────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    d     = c.diff()
    gain  = d.clip(lower=0).ewm(span=2, adjust=False).mean()
    loss  = (-d).clip(lower=0).ewm(span=2, adjust=False).mean()
    rsi2  = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    g14   = d.clip(lower=0).ewm(span=14, adjust=False).mean()
    l14   = (-d).clip(lower=0).ewm(span=14, adjust=False).mean()
    rsi14 = 100 - 100 / (1 + g14 / l14.replace(0, np.nan))

    ma200 = c.rolling(200).mean()
    ma50  = c.rolling(50).mean()
    prev  = c.shift(1)
    tr    = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr   = tr.ewm(span=14, adjust=False).mean()

    df = df.copy()
    df["rsi2"]  = rsi2
    df["rsi14"] = rsi14
    df["ma200"] = ma200
    df["ma50"]  = ma50
    df["atr"]   = atr
    return df


# ── バックテスト ────────────────────────────────────────────────
def backtest(df: pd.DataFrame,
             start: pd.Timestamp,
             end: pd.Timestamp) -> list[dict]:
    df = df[(df.index >= start) & (df.index <= end)].copy()

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
            reason = exit_p = None

            if lo <= entry_p * (1 - HARD_STOP_PCT / 100):
                exit_p = min(op, entry_p * (1 - HARD_STOP_PCT / 100))
                reason = f"損切り(-{HARD_STOP_PCT:.0f}%)"
            elif lo <= trail:
                exit_p = min(op, trail)
                reason = "トレイリング"
            elif float(prev["rsi2"]) >= RSI2_EXIT:
                exit_p = op
                reason = "RSI2回復"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p,
                    qty=qty, pnl=pnl,
                    hold=(dt - entry_dt).days, reason=reason,
                ))
                in_pos = half_done = False
                continue

            if not half_done:
                cl = float(row["close"])
                if (cl - entry_p) / entry_p * 100 >= HALF_PROFIT_PCT:
                    hq = qty // 2
                    if hq > 0:
                        trades.append(dict(
                            entry_dt=entry_dt, exit_dt=dt,
                            entry_p=entry_p, exit_p=cl,
                            qty=hq, pnl=(cl - entry_p) * hq,
                            hold=(dt - entry_dt).days,
                            reason="半分利確",
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
                qty = max(int(POSITION_SIZE / op), 0)
                if qty > 0:
                    entry_p   = op
                    trail     = op - float(row["atr"]) * ATR_TRAIL_MULT
                    entry_dt  = dt
                    half_done = False
                    in_pos    = True

    if in_pos:
        lp = float(df.iloc[-1]["close"])
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=lp,
            qty=qty, pnl=(lp - entry_p) * qty,
            hold=(df.index[-1] - entry_dt).days,
            reason="保有中★",
        ))

    return trades


# ── 1銘柄詳細表示 ───────────────────────────────────────────────
def show_detail(symbol: str, name: str, trades: list[dict],
                start: pd.Timestamp, end: pd.Timestamp) -> None:
    if not trades:
        print(f"\n  [{symbol}]  シグナルなし\n")
        return

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / len(trades) * 100
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    wh    = sum(t["hold"] for t in wins) / len(wins)   if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss)   if loss else 0

    print()
    print("═" * 62)
    print(f"  RSI(2) 平均回帰  [{symbol}] {name}")
    print(f"  期間: {start.strftime('%Y-%m-%d')} ～ {end.strftime('%Y-%m-%d')}")
    print("═" * 62)
    print(f"  トレード: {len(trades)}回  勝: {len(wins)}  負: {len(loss)}")
    print(f"  勝率: {wr:.1f}%   PF: {'∞' if pf == float('inf') else f'{pf:.2f}'}   損益: {total:+,.0f}円")
    print(f"  平均保有: 勝ち {wh:.1f}日 / 負け {lh:.1f}日")
    print()
    print(f"  {'#':<3} {'エントリー':>10} {'エグジット':>10} "
          f"{'買値':>8} {'売値':>8} {'損益':>9} 保有  決済理由")
    print("  " + "─" * 60)
    for i, t in enumerate(trades, 1):
        pct  = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        mark = "★" if "保有中" in t["reason"] else " "
        print(f" {mark}{i:<3} "
              f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} "
              f"{t['pnl']:>+9,.0f} {t['hold']:>3}日  "
              f"{t['reason']}({pct:+.1f}%)")
    print("  " + "─" * 60)
    print()


# ── 255銘柄ランキング表示 ───────────────────────────────────────
def show_ranking(results: list[dict],
                 start: pd.Timestamp, end: pd.Timestamp,
                 top: int | None = None) -> None:
    ranked    = sorted(results, key=lambda x: x["total"], reverse=True)
    total_tr  = sum(r["trades"] for r in results)
    total_pnl = sum(r["total"]  for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)
    display   = ranked[:top] if top else ranked
    top_label = f"  上位{top}銘柄表示" if top else ""

    print()
    print("═" * 72)
    print(f"  RSI(2) 平均回帰戦略  {len(SYMBOLS)}銘柄スキャン{top_label}")
    print(f"  期間: {start.strftime('%Y-%m-%d')} ～ {end.strftime('%Y-%m-%d')}")
    print(f"  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上  →  翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / "
          f"-{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確")
    print("═" * 72)
    print(f"  スキャン: {len(SYMBOLS)}銘柄  シグナルあり: {len(results)}銘柄  "
          f"トレード計: {total_tr}回  プラス銘柄: {plus_cnt}/{len(results)}"
          + (f"  表示: 上位{top}/" if top else ""))
    print()
    print(f"  {'順位':<4} {'銘柄':<22} {'損益':>10} {'勝率':>6} "
          f"{'PF':>5} {'取引':>4} {'平均保有':>7}")
    print("  " + "─" * 60)

    for rank, r in enumerate(display, 1):
        pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        sign  = "+" if r["total"] >= 0 else ""
        bar   = ("▲" if r["total"] >= 0 else "▽") * min(int(abs(r["total"]) / 5000), 8)
        label = f"{r['name']}({r['symbol']})"
        print(f"  {rank:<4} {label:<22} "
              f"{sign}{r['total']:>9,.0f}円  "
              f"{r['wr']:>5.1f}%  {pf_s:>5}  "
              f"{r['trades']:>3}回  {r['avg_hold']:>5.1f}日  {bar}")

    print("  " + "─" * 60)
    sign = "+" if total_pnl >= 0 else ""
    print(f"  合計損益（全銘柄・重複あり）: {sign}{total_pnl:,.0f}円")
    print()


# ── HTML共通CSS/JS ──────────────────────────────────────────────
_HTML_HEAD = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI(2) バックテスト結果</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
     background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}
h1{font-size:1.35em;color:#fff;border-left:4px solid #3a86ff;
   padding-left:12px;margin-bottom:6px}
.meta{color:#666;font-size:0.82em;margin:2px 0 0 16px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}
.card{background:#16192a;border:1px solid #252840;border-radius:10px;
      padding:14px 20px;min-width:130px}
.clabel{font-size:0.72em;color:#777;letter-spacing:.05em}
.cval{font-size:1.55em;font-weight:700;margin-top:3px}
.pos{color:#4ade80}.neg{color:#f87171}.neu{color:#c8cfe8}
.section{margin-top:28px}
.section h2{font-size:1em;color:#aaa;text-transform:uppercase;
            letter-spacing:.1em;margin-bottom:10px;border-bottom:1px solid #252840;
            padding-bottom:6px}
table{width:100%;border-collapse:collapse;font-size:0.86em}
th{background:#16192a;color:#888;padding:8px 12px;text-align:right;
   cursor:pointer;user-select:none;border-bottom:1px solid #252840;
   white-space:nowrap}
th:first-child,th.left{text-align:left}
th[data-dir="asc"]::after{content:" ▲";color:#3a86ff}
th[data-dir="desc"]::after{content:" ▼";color:#3a86ff}
td{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;
   white-space:nowrap}
td:first-child,td.left{text-align:left}
tr.profit>td{background:rgba(74,222,128,.04)}
tr.loss>td{background:rgba(248,113,113,.04)}
tr.hold>td{background:rgba(251,191,36,.06)}
tr:hover>td{background:#1b1f35!important}
.total-row>td{font-weight:700;background:#16192a;
              border-top:2px solid #252840;color:#fff}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:0.78em}
.tag-win{background:#1a3d2b;color:#4ade80}
.tag-loss{background:#3d1a1a;color:#f87171}
.tag-hold{background:#3d3010;color:#fbbf24}
.footer{margin-top:32px;color:#444;font-size:0.78em;text-align:right}
</style>
</head>
<body>
"""

_HTML_SORT_JS = """\
<script>
(function(){
  document.querySelectorAll('table.sortable').forEach(function(tbl){
    var ths=tbl.querySelectorAll('thead th[data-col]');
    ths.forEach(function(th){
      th.addEventListener('click',function(){
        var col=+th.dataset.col, numeric=th.dataset.numeric==='1';
        var dir=th.dataset.dir==='asc'?'desc':'asc';
        ths.forEach(function(h){delete h.dataset.dir});
        th.dataset.dir=dir;
        var rows=Array.from(tbl.tBodies[0].rows);
        rows.sort(function(a,b){
          var av=a.cells[col].dataset.val||a.cells[col].innerText.replace(/[,円%]/g,'').trim();
          var bv=b.cells[col].dataset.val||b.cells[col].innerText.replace(/[,円%]/g,'').trim();
          if(numeric){av=parseFloat(av)||0;bv=parseFloat(bv)||0}
          if(av<bv)return dir==='asc'?-1:1;
          if(av>bv)return dir==='asc'?1:-1;
          return 0;
        });
        rows.forEach(function(r){tbl.tBodies[0].appendChild(r)});
      });
    });
  });
})();
</script>
"""


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _sign_cls(val: float) -> str:
    return "pos" if val >= 0 else "neg"


# ── HTML: 1銘柄詳細 ─────────────────────────────────────────────
def build_html_detail(symbol: str, name: str, trades: list[dict],
                      start: pd.Timestamp, end: pd.Timestamp) -> str:
    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / len(trades) * 100 if trades else 0
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0

    rows = []
    for i, t in enumerate(trades, 1):
        pct  = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        cls  = "hold" if "保有中" in t["reason"] else ("profit" if t["pnl"] > 0 else "loss")
        tag_cls = "tag-hold" if cls == "hold" else ("tag-win" if t["pnl"] > 0 else "tag-loss")
        rows.append(
            f'<tr class="{cls}">'
            f'<td class="left">{i}</td>'
            f'<td class="left">{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td class="left">{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["entry_p"]:,.0f}</td>'
            f'<td>{t["exit_p"]:,.0f}</td>'
            f'<td class="{_sign_cls(t["pnl"])}" data-val="{t["pnl"]:.0f}">'
            f'{t["pnl"]:+,.0f}円</td>'
            f'<td class="{_sign_cls(pct)}">{pct:+.1f}%</td>'
            f'<td>{t["hold"]}日</td>'
            f'<td class="left"><span class="tag {tag_cls}">{t["reason"]}</span></td>'
            f'</tr>'
        )

    html  = _HTML_HEAD
    html += f'<h1>RSI(2) 平均回帰  [{symbol}] {name}</h1>\n'
    html += f'<div class="meta">期間: {start.strftime("%Y-%m-%d")} ～ {end.strftime("%Y-%m-%d")}</div>\n'
    html += f'<div class="meta">【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値 / '
    html += f'【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / '
    html += f'-{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確</div>\n'

    html += '<div class="cards">\n'
    for label, val, fmt in [
        ("損益合計",    total,         f'<span class="{_sign_cls(total)} cval">{total:+,.0f}円</span>'),
        ("勝率",        wr,            f'<span class="neu cval">{wr:.1f}%</span>'),
        ("プロフィットF", pf,          f'<span class="neu cval">{_pf_str(pf)}</span>'),
        ("トレード数",   len(trades),  f'<span class="neu cval">{len(trades)}回</span>'),
        ("勝/負",       0,             f'<span class="neu cval">{len(wins)}勝{len(loss)}負</span>'),
        ("平均保有(勝)", wh,           f'<span class="pos cval">{wh:.1f}日</span>'),
        ("平均保有(負)", lh,           f'<span class="neg cval">{lh:.1f}日</span>'),
    ]:
        html += f'<div class="card"><div class="clabel">{label}</div>{fmt}</div>\n'
    html += '</div>\n'

    html += '<div class="section"><h2>トレード一覧</h2>\n'
    html += '<table class="sortable"><thead><tr>'
    for i, (h, num) in enumerate([
        ("#", 0), ("エントリー", 0), ("エグジット", 0),
        ("買値", 1), ("売値", 1), ("損益", 1), ("変化率", 1),
        ("保有日数", 1), ("決済理由", 0),
    ]):
        left = ' class="left"' if i < 3 or i == 8 else ''
        html += f'<th{left} data-col="{i}" data-numeric="{num}">{h}</th>'
    html += '</tr></thead><tbody>\n'
    html += "\n".join(rows)
    html += '\n</tbody></table></div>\n'
    html += f'<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>\n'
    html += _HTML_SORT_JS
    html += '</body></html>\n'
    return html


# ── HTML: 255銘柄ランキング ──────────────────────────────────────
def build_html_ranking(results: list[dict],
                       start: pd.Timestamp, end: pd.Timestamp,
                       top: int | None = None) -> str:
    ranked    = sorted(results, key=lambda x: x["total"], reverse=True)
    total_tr  = sum(r["trades"] for r in results)
    total_pnl = sum(r["total"]  for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)
    display   = ranked[:top] if top else ranked

    rank_rows = []
    for rank, r in enumerate(display, 1):
        cls = "profit" if r["total"] >= 0 else "loss"
        rank_rows.append(
            f'<tr class="{cls}">'
            f'<td>{rank}</td>'
            f'<td class="left">{r["symbol"]}</td>'
            f'<td class="left">{r["name"]}</td>'
            f'<td class="{_sign_cls(r["total"])}" data-val="{r["total"]:.0f}">'
            f'{r["total"]:+,.0f}円</td>'
            f'<td>{r["wr"]:.1f}%</td>'
            f'<td data-val="{r["pf"] if r["pf"]!=float("inf") else 9999}">'
            f'{_pf_str(r["pf"])}</td>'
            f'<td>{r["trades"]}</td>'
            f'<td>{r["avg_hold"]:.1f}</td>'
            f'</tr>'
        )

    # トレード明細（アコーディオン展開）
    detail_html = ""
    for r in display:
        sym, nm = r["symbol"], r["name"]
        detail_rows = []
        for i, t in enumerate(r["trade_log"], 1):
            pct = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
            cls = "hold" if "保有中" in t["reason"] else ("profit" if t["pnl"] > 0 else "loss")
            tag_cls = "tag-hold" if cls == "hold" else ("tag-win" if t["pnl"] > 0 else "tag-loss")
            detail_rows.append(
                f'<tr class="{cls}">'
                f'<td>{i}</td>'
                f'<td class="left">{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td class="left">{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t["entry_p"]:,.0f}</td>'
                f'<td>{t["exit_p"]:,.0f}</td>'
                f'<td class="{_sign_cls(t["pnl"])}">{t["pnl"]:+,.0f}円</td>'
                f'<td class="{_sign_cls(pct)}">{pct:+.1f}%</td>'
                f'<td>{t["hold"]}日</td>'
                f'<td class="left"><span class="tag {tag_cls}">{t["reason"]}</span></td>'
                f'</tr>'
            )
        detail_html += (
            f'<details style="margin:4px 0;border:1px solid #252840;border-radius:6px">'
            f'<summary style="padding:8px 12px;cursor:pointer;background:#16192a;'
            f'border-radius:6px;list-style:none;color:#c8cfe8">'
            f'▶ {sym} {nm}'
            f'  <span class="{_sign_cls(r["total"])}">{r["total"]:+,.0f}円</span>'
            f'  勝率{r["wr"]:.1f}%  {r["trades"]}回</summary>'
            f'<div style="padding:8px">'
            f'<table><thead><tr>'
            f'<th data-col="0">#</th>'
            f'<th class="left" data-col="1">エントリー</th>'
            f'<th class="left" data-col="2">エグジット</th>'
            f'<th data-col="3" data-numeric="1">買値</th>'
            f'<th data-col="4" data-numeric="1">売値</th>'
            f'<th data-col="5" data-numeric="1">損益</th>'
            f'<th data-col="6" data-numeric="1">変化率</th>'
            f'<th data-col="7" data-numeric="1">保有日数</th>'
            f'<th class="left" data-col="8">決済理由</th>'
            f'</tr></thead><tbody>'
            + "\n".join(detail_rows) +
            f'</tbody></table></div></details>\n'
        )

    top_label = f"  上位{top}銘柄" if top else ""
    html  = _HTML_HEAD
    html += f'<h1>RSI(2) 平均回帰戦略  {len(SYMBOLS)}銘柄スキャン{top_label}</h1>\n'
    html += f'<div class="meta">期間: {start.strftime("%Y-%m-%d")} ～ {end.strftime("%Y-%m-%d")}</div>\n'
    html += f'<div class="meta">【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値 / '
    html += f'【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / '
    html += f'-{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確</div>\n'

    html += '<div class="cards">\n'
    for label, fmt in [
        ("合計損益",    f'<span class="{_sign_cls(total_pnl)} cval">{total_pnl:+,.0f}円</span>'),
        ("スキャン銘柄", f'<span class="neu cval">{len(SYMBOLS)}銘柄</span>'),
        ("シグナルあり", f'<span class="neu cval">{len(results)}銘柄</span>'),
        ("トレード計",  f'<span class="neu cval">{total_tr}回</span>'),
        ("プラス銘柄",  f'<span class="pos cval">{plus_cnt}/{len(results)}</span>'),
    ]:
        html += f'<div class="card"><div class="clabel">{label}</div>{fmt}</div>\n'
    html += '</div>\n'

    html += '<div class="section"><h2>銘柄ランキング</h2>\n'
    html += '<table class="sortable"><thead><tr>'
    for i, (h, num) in enumerate([
        ("順位", 1), ("コード", 0), ("銘柄名", 0),
        ("損益(円)", 1), ("勝率(%)", 1), ("PF", 1),
        ("取引数", 1), ("平均保有(日)", 1),
    ]):
        left = ' class="left"' if i in (1, 2) else ""
        html += f'<th{left} data-col="{i}" data-numeric="{num}">{h}</th>'
    html += '</tr></thead><tbody>\n'
    html += "\n".join(rank_rows)
    html += f'\n<tr class="total-row"><td colspan="3">合計（全銘柄・重複あり）</td>'
    html += f'<td class="{_sign_cls(total_pnl)}">{total_pnl:+,.0f}円</td>'
    html += f'<td colspan="4"></td></tr>'
    html += '\n</tbody></table></div>\n'

    html += '<div class="section"><h2>銘柄別トレード明細</h2>\n'
    html += detail_html
    html += '</div>\n'
    html += f'<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>\n'
    html += _HTML_SORT_JS
    html += '</body></html>\n'
    return html


def save_and_open_html(html: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTMLレポート保存: {path}")
    webbrowser.open(f"file://{__import__('os').path.abspath(path)}")


# ── メイン ──────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="RSI(2) 平均回帰バックテスト")
    parser.add_argument("symbol", nargs="?", default=None,
                        help="銘柄コード指定で1銘柄詳細表示（省略で全銘柄スキャン）")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS,
                        help="バックテスト期間（年）。--start/--days 指定時は無視される")
    parser.add_argument("--days", type=int, default=None,
                        help="何日前からバックテスト（例: 90）。--start 指定時は無視される")
    parser.add_argument("--start", metavar="YYYY-MM-DD", default=None,
                        help="バックテスト開始日（例: 2023-01-01）")
    parser.add_argument("--end", metavar="YYYY-MM-DD", default=None,
                        help="バックテスト終了日（例: 2024-12-31）。省略時は今日")
    parser.add_argument("--top", type=int, default=None,
                        help="損益上位N銘柄のみ表示（例: --top 50）。省略時は全銘柄")
    args = parser.parse_args()

    try:
        start, end = resolve_dates(args)
    except ValueError as e:
        print(f"  エラー: {e}")
        return

    # ── 1銘柄モード ──────────────────────────────────────────────
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        name = next((n for s, n in SYMBOLS if s == sym), sym)
        print(f"\n  データ取得中: {sym}  {start.strftime('%Y-%m-%d')} ～ {end.strftime('%Y-%m-%d')} ...")
        df = fetch(sym, start, end)
        if df is None:
            print(f"  エラー: {sym} のデータ取得に失敗しました")
            return
        df = calc(df)
        trades = backtest(df, start, end)
        show_detail(sym, name, trades, start, end)
        fname = (f"rsi2_{sym.replace('.', '')}_{start.strftime('%Y%m%d')}"
                 f"_{end.strftime('%Y%m%d')}.html")
        save_and_open_html(build_html_detail(sym, name, trades, start, end), fname)
        return

    # ── 全銘柄スキャンモード ──────────────────────────────────────
    print(f"\n  RSI(2) 平均回帰  {len(SYMBOLS)}銘柄データ取得中 ...")
    print(f"  期間: {start.strftime('%Y-%m-%d')} ～ {end.strftime('%Y-%m-%d')}")

    # Phase1: 並列ダウンロード
    stock_data: dict[str, tuple[str, pd.DataFrame]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch, sym, start, end): (sym, name)
                for sym, name in SYMBOLS}
        done = 0
        for fut in as_completed(futs):
            sym, name = futs[fut]
            done += 1
            print(f"\r  取得中 {done}/{len(SYMBOLS)} ...", end="", flush=True)
            df = fut.result()
            if df is not None:
                stock_data[sym] = (name, df)
    print(f"\r  取得完了: {len(stock_data)}/{len(SYMBOLS)} 銘柄              ")

    # Phase2: バックテスト
    results = []
    for sym, (name, df) in stock_data.items():
        trades = backtest(calc(df), start, end)
        if not trades:
            continue
        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        wr    = len(wins) / len(trades) * 100
        pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                 if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        avg_hold = sum(t["hold"] for t in trades) / len(trades)
        results.append(dict(
            symbol=sym, name=name,
            trades=len(trades), total=total,
            wr=wr, pf=pf, avg_hold=avg_hold,
            trade_log=trades,
        ))

    if not results:
        print("  シグナルが発生した銘柄がありませんでした。")
        return

    show_ranking(results, start, end, top=args.top)
    fname = (f"rsi2_scan_{start.strftime('%Y%m%d')}"
             f"_{end.strftime('%Y%m%d')}.html")
    save_and_open_html(build_html_ranking(results, start, end, top=args.top), fname)


if __name__ == "__main__":
    main()
