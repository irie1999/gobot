"""
RSI(2) 平均回帰戦略  軽量版（255銘柄スキャン対応）
─────────────────────────────────────────────────
使い方:
  python rsi2_simple.py              # 255銘柄スキャン（1年）
  python rsi2_simple.py --years 2    # 255銘柄スキャン（2年）
  python rsi2_simple.py 7011.T       # 1銘柄詳細
  python rsi2_simple.py 7011.T --years 2
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

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


# ── データ取得 ──────────────────────────────────────────────────
def fetch(symbol: str, years: int) -> pd.DataFrame | None:
    period = f"{years + 1}y"
    try:
        raw = yf.download(symbol, period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        return raw[["open", "high", "low", "close", "volume"]].dropna()
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
def backtest(df: pd.DataFrame, years: int) -> list[dict]:
    cutoff = pd.Timestamp(datetime.today() - timedelta(days=years * 365))
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
            reason = exit_p = None

            if lo <= entry_p * (1 - HARD_STOP_PCT / 100):
                exit_p = min(op, entry_p * (1 - HARD_STOP_PCT / 100))
                reason = f"損切り(-{HARD_STOP_PCT:.0f}%)"
            elif lo <= trail:
                exit_p = min(op, trail)
                reason = "トレイリング"
            elif float(prev["rsi2"]) >= RSI2_EXIT:
                exit_p = op
                reason = f"RSI2回復"

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
                            reason=f"半分利確",
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
def show_detail(symbol: str, name: str, trades: list[dict], years: int) -> None:
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

    since = (datetime.today() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    print()
    print("═" * 62)
    print(f"  RSI(2) 平均回帰  [{symbol}] {name}  直近{years}年")
    print(f"  期間: {since} ～ {today}")
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


# ── 50銘柄ランキング表示 ────────────────────────────────────────
def show_ranking(results: list[dict], years: int) -> None:
    since = (datetime.today() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    ranked = sorted(results, key=lambda x: x["total"], reverse=True)
    total_tr  = sum(r["trades"] for r in results)
    total_pnl = sum(r["total"]  for r in results)
    plus_cnt  = sum(1 for r in results if r["total"] > 0)

    print()
    print("═" * 72)
    print(f"  RSI(2) 平均回帰戦略  50銘柄スキャン  直近{years}年")
    print(f"  期間: {since} ～ {today}")
    print(f"  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上  →  翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / -{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確")
    print("═" * 72)
    print(f"  スキャン: {len(SYMBOLS)}銘柄  シグナルあり: {len(results)}銘柄  "
          f"トレード計: {total_tr}回  プラス銘柄: {plus_cnt}/{len(results)}")
    print()
    print(f"  {'順位':<4} {'銘柄':<22} {'損益':>10} {'勝率':>6} "
          f"{'PF':>5} {'取引':>4} {'平均保有':>7}")
    print("  " + "─" * 60)

    for rank, r in enumerate(ranked, 1):
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


# ── メイン ──────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="RSI(2) 平均回帰バックテスト")
    parser.add_argument("symbol", nargs="?", default=None,
                        help="銘柄コード指定で1銘柄詳細表示（省略で50銘柄スキャン）")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS,
                        help="バックテスト期間（年）")
    args = parser.parse_args()

    # ── 1銘柄モード ──────────────────────────────────────────────
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        name = next((n for s, n in SYMBOLS if s == sym), sym)
        print(f"\n  データ取得中: {sym} ...")
        df = fetch(sym, args.years)
        if df is None or len(df) < 210:
            print(f"  エラー: {sym} のデータ取得に失敗しました")
            return
        show_detail(sym, name, backtest(calc(df), args.years), args.years)
        return

    # ── 50銘柄スキャンモード ──────────────────────────────────────
    print(f"\n  RSI(2) 平均回帰  {len(SYMBOLS)}銘柄データ取得中 ...")

    # Phase1: 並列ダウンロード
    stock_data: dict[str, tuple[str, pd.DataFrame]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch, sym, args.years): (sym, name)
                for sym, name in SYMBOLS}
        done = 0
        for fut in as_completed(futs):
            sym, name = futs[fut]
            done += 1
            print(f"\r  取得中 {done}/{len(SYMBOLS)} ...", end="", flush=True)
            df = fut.result()
            if df is not None and len(df) >= 210:
                stock_data[sym] = (name, df)
    print(f"\r  取得完了: {len(stock_data)}/{len(SYMBOLS)} 銘柄              ")

    # Phase2: バックテスト
    results = []
    for sym, (name, df) in stock_data.items():
        trades = backtest(calc(df), args.years)
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

    show_ranking(results, args.years)


if __name__ == "__main__":
    main()
