"""
2か月バックテスト（A7: トレンドフィルター付きストキャスティクス + ATRトレイリングストップ）
────────────────────────────────────────────────────────────────────────
対象銘柄: 日経225 全銘柄（219銘柄）
バックテスト期間: 直近60日間（指標計算には6ヶ月分データを使用）

■ 実行方法:
  python backtest_1month.py               # 日経225 全スキャン（ランキング表示）
  python backtest_1month.py 7203.T        # 特定銘柄のみ詳細表示
  python backtest_1month.py --days 30     # 直近30日に変更
  python backtest_1month.py --top 50      # 上位50件表示（デフォルト30）

■ 出力:
  - 銘柄ごとの損益・勝率・平均保有日数
  - 個別トレード一覧（エントリー日・エグジット日・損益）
  - 損益率ランキング表示
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

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

# ── パラメータ ──────────────────────────────────────────────
BACKTEST_DAYS    = 60           # バックテスト期間（日）← ここを変更
WORKERS          = 16           # 並列バックテスト数（データ取得は順次）
STOCH_K_PERIOD   = 14
STOCH_D_PERIOD   = 3
STOCH_SMOOTH     = 3
STOCH_OVERSOLD   = 30
STOCH_OVERBOUGHT = 70
ATR_PERIOD       = 14
ATR_STOP_MULT    = 1.5
ATR_TRAIL_MULT   = 2.0
MA_TREND_PERIOD  = 75

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


# ── データ取得（メインスレッドから順次呼び出し） ─────────────
def fetch_df(symbol: str) -> pd.DataFrame | None:
    """6ヶ月分のデータを取得（75MA計算に十分な履歴が必要）"""
    try:
        raw = yf.download(symbol, period="6mo", interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        min_needed = MA_TREND_PERIOD + STOCH_K_PERIOD + STOCH_SMOOTH + STOCH_D_PERIOD
        if len(raw) < min_needed:
            return None
        # numpy 再構築（スレッド競合対策）
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
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


# ── メイン ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="A7 直近N日バックテスト（日経225）")
    parser.add_argument("symbol", nargs="?",  default=None,
                        help="特定銘柄コード（省略時は全225銘柄スキャン）")
    parser.add_argument("--days", type=int,   default=BACKTEST_DAYS,
                        help=f"バックテスト日数（デフォルト: {BACKTEST_DAYS}日）")
    parser.add_argument("--top",  type=int,   default=30,
                        help="ランキング表示件数（デフォルト: 30）")
    args = parser.parse_args()

    days = args.days
    print(f"\n  A7 直近{days}日バックテスト  (開始: "
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
    print(f"  [Phase 1] データ取得中 ({total}銘柄)...")
    for i, (sym, name) in enumerate(target, 1):
        print(f"  [{i:3d}/{total}] {name}({sym})", end=" ", flush=True)
        df = fetch_df(sym)
        if df is None:
            print("× スキップ")
        else:
            print("✓")
            stock_data[sym] = (name, df)

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


if __name__ == "__main__":
    main()
