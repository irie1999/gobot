"""
MACDブレイクアウト × 出来高急増 × ATRトレイリング  銘柄スキャナー（60銘柄）
────────────────────────────────────────────────────────────────────────
【アルゴリズム】
  Entry（3条件すべて）:
    1. MACDヒストグラムがゼロラインを下から上抜け（前日<=0 → 当日>0）
    2. 出来高 > 20日平均 × 1.5倍  ← 機関投資家の本物の参入
    3. 終値 > 25日移動平均線       ← 短期上昇トレンド確認

  Exit（いずれか）:
    A. MACDヒストグラムがゼロラインを上から下抜け（前日>=0 → 当日<0）
    B. ATRトレイリングストップ発動（ATR × 2.5）

■ 実行方法:
  python backtest_macd_scan.py                    # 直近1ヶ月（デフォルト）
  python backtest_macd_scan.py --months 3         # 直近3ヶ月
  python backtest_macd_scan.py --years 1          # 直近1年
  python backtest_macd_scan.py --years 5          # 直近5年
  python backtest_macd_scan.py --days 45          # 直近45日
  python backtest_macd_scan.py 7203.T --years 2   # 特定銘柄詳細
  python backtest_macd_scan.py --years 3 --top 30 # 上位30件表示
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── 対象銘柄（60銘柄：各セクター代表） ──────────────────────
SYMBOLS = [
    # 金融・保険
    ("8766.T", "東京海上HD"),
    ("8628.T", "松井証券"),
    ("8795.T", "T&Dホールディングス"),
    ("8601.T", "大和証券グループ本社"),
    ("8604.T", "野村HD"),
    ("8306.T", "三菱UFJフィナンシャルG"),
    ("8316.T", "三井住友フィナンシャルG"),
    ("8411.T", "みずほフィナンシャルG"),
    ("8630.T", "SOMPOホールディングス"),
    ("8725.T", "MS&ADインシュアランスG"),
    ("8750.T", "第一生命HD"),
    ("8697.T", "日本取引所グループ"),
    # 商社・流通
    ("8031.T", "三井物産"),
    ("8001.T", "伊藤忠商事"),
    ("8002.T", "丸紅"),
    ("8058.T", "三菱商事"),
    ("8053.T", "住友商事"),
    ("8267.T", "イオン"),
    ("8233.T", "高島屋"),
    # 自動車・輸送機
    ("7203.T", "トヨタ自動車"),
    ("7267.T", "本田技研工業"),
    ("7202.T", "いすゞ自動車"),
    ("7270.T", "SUBARU"),
    ("7272.T", "ヤマハ発動機"),
    # 機械・重工
    ("6301.T", "小松製作所"),
    ("6305.T", "日立建機"),
    ("6326.T", "クボタ"),
    ("7011.T", "三菱重工業"),
    ("7012.T", "川崎重工業"),
    ("7013.T", "IHI"),
    ("6367.T", "ダイキン工業"),
    ("6273.T", "SMC"),
    # 電機・精密
    ("6758.T", "ソニーグループ"),
    ("6861.T", "キーエンス"),
    ("6857.T", "アドバンテスト"),
    ("6954.T", "ファナック"),
    ("6702.T", "富士通"),
    ("6701.T", "NEC"),
    ("6503.T", "三菱電機"),
    ("6501.T", "日立製作所"),
    ("6752.T", "パナソニックHD"),
    ("6762.T", "TDK"),
    ("6981.T", "村田製作所"),
    # 化学・素材
    ("4063.T", "信越化学工業"),
    ("4568.T", "第一三共"),
    ("4502.T", "武田薬品工業"),
    ("4503.T", "アステラス製薬"),
    ("4578.T", "大塚HD"),
    ("5401.T", "日本製鉄"),
    ("5411.T", "JFEホールディングス"),
    ("5713.T", "住友金属鉱山"),
    # 不動産・建設
    ("8830.T", "住友不動産"),
    ("8802.T", "三菱地所"),
    ("8801.T", "三井不動産"),
    ("1812.T", "鹿島建設"),
    ("1801.T", "大成建設"),
    # 通信・IT
    ("9432.T", "日本電信電話"),
    ("9433.T", "KDDI"),
    ("9434.T", "ソフトバンク"),
    ("9984.T", "ソフトバンクグループ"),
    ("4307.T", "野村総合研究所"),
]

# ── パラメータ ──────────────────────────────────────────────
BACKTEST_DAYS   = 30            # デフォルト（1ヶ月）
WORKERS         = 16            # 並列バックテスト数

MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
VOL_MA_PERIOD   = 20
VOL_SPIKE_MULT  = 1.5
MA_TREND_PERIOD = 25

ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.5
ATR_TRAIL_MULT  = 2.5

INITIAL_CASH    = 500_000
RISK_PER_TRADE  = 0.02
MAX_COST_RATIO  = 0.10
MAX_QTY         = 9999


# ── インジケーター計算 ──────────────────────────────────────
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # ATR
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # MACD
    ema_fast    = c.ewm(span=MACD_FAST,   adjust=False).mean()
    ema_slow    = c.ewm(span=MACD_SLOW,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram   = macd_line - signal_line

    # 出来高・トレンドフィルター
    vol_ma = v.rolling(VOL_MA_PERIOD).mean()
    ma25   = c.rolling(MA_TREND_PERIOD).mean()

    prev_hist = histogram.shift(1)

    df = df.copy()
    df["atr"]       = atr
    df["macd"]      = macd_line
    df["macd_sig"]  = signal_line
    df["macd_hist"] = histogram
    df["vol_ma"]    = vol_ma
    df["ma25"]      = ma25

    # Entry: ヒストグラムがゼロライン上抜け AND 出来高急増 AND 上昇トレンド
    df["entry_sig"] = ((histogram > 0) & (prev_hist <= 0)
                       & (v > vol_ma * VOL_SPIKE_MULT)
                       & (c > ma25))

    # Exit: ヒストグラムがゼロライン下抜け
    df["exit_sig"]  = (histogram < 0) & (prev_hist >= 0)

    return df


# ── データ取得（メインスレッドから順次） ──────────────────
def fetch_df(symbol: str, backtest_days: int = BACKTEST_DAYS) -> pd.DataFrame | None:
    """バックテスト期間 + 指標計算バッファ分を取得。
    MACD(26日) + Signal(9日) + VolMA(20日) + MA25 + バックテスト + 余裕30日。"""
    buf_days  = MACD_SLOW + MACD_SIGNAL + VOL_MA_PERIOD + MA_TREND_PERIOD + 30
    total_cal = int((backtest_days + buf_days) * 1.5)

    if   total_cal <= 180:  period = "6mo"
    elif total_cal <= 365:  period = "1y"
    elif total_cal <= 730:  period = "2y"
    elif total_cal <= 1095: period = "3y"
    elif total_cal <= 1825: period = "5y"
    else:                   period = "max"

    try:
        raw = yf.download(symbol, period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        min_needed = MACD_SLOW + MACD_SIGNAL + VOL_MA_PERIOD + MA_TREND_PERIOD
        if len(raw) < min_needed:
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


# ── 1銘柄バックテスト ────────────────────────────────────────
def run_backtest(symbol: str, name: str, df: pd.DataFrame,
                 backtest_days: int) -> dict | None:
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

    for i in range(len(df_target)):
        row  = df_target.iloc[i]
        prev = df_target.iloc[i - 1] if i > 0 else row
        dt   = df_target.index[i]

        if pd.isna(row["macd_hist"]) or pd.isna(row["atr"]):
            continue

        # ── エグジット ──
        if in_pos:
            exit_p = exit_r = None
            if row["low"] <= trail_stop:
                exit_p = min(float(row["open"]), trail_stop)
                exit_r = "トレイリング"
            elif bool(prev["exit_sig"]):
                exit_p = float(row["open"])
                exit_r = "MACDクロス"
            if exit_p is not None:
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
                    "reason":      exit_r,
                })
                in_pos = False
            else:
                candidate = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
                if candidate > trail_stop:
                    trail_stop = candidate

        # ── エントリー ──
        if not in_pos and bool(prev["entry_sig"]):
            atr_v     = float(prev["atr"])
            stop_dist = atr_v * ATR_STOP_MULT
            if stop_dist > 0:
                q_risk = int(cash * RISK_PER_TRADE / stop_dist)
                q_cost = int(cash * MAX_COST_RATIO / float(row["open"])) if float(row["open"]) > 0 else q_risk
                q      = max(min(q_risk, q_cost, MAX_QTY), 0)
                cost   = float(row["open"]) * q
                if q > 0 and cost <= cash:
                    cash        -= cost
                    entry_price  = float(row["open"])
                    trail_stop   = entry_price - float(row["atr"]) * ATR_TRAIL_MULT
                    entry_dt     = dt
                    qty          = q
                    in_pos       = True

    # 未決済を最終日終値で仮決済
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
    losses   = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_hold = sum(t["hold_days"] for t in trades) / len(trades)
    pf       = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))

    last      = df_target.iloc[-1]
    last_close = float(last["close"])
    last_hist  = float(last["macd_hist"]) if not pd.isna(last["macd_hist"]) else 0.0
    last_ma25  = float(last["ma25"])      if not pd.isna(last["ma25"])      else 0.0

    return {
        "symbol":    symbol,
        "name":      name,
        "trades":    len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  win_rate,
        "pf":        pf,
        "total":     total,
        "ret_pct":   ret_pct,
        "avg_hold":  avg_hold,
        "trade_log": trades,
        "open_pos":  open_pos is not None,
        "last_close": last_close,
        "last_hist":  last_hist,
        "last_ma25":  last_ma25,
    }


# ── 単銘柄詳細表示 ──────────────────────────────────────────
def print_detail(r: dict, backtest_days: int) -> None:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")
    sign  = "+" if r["total"] >= 0 else ""
    pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"

    print()
    print("=" * 68)
    print(f"  {r['name']}({r['symbol']})  [{since} ～ {today}]")
    print("=" * 68)
    print(f"  トレード数       : {r['trades']}回  （勝: {r['wins']}  負: {r['losses']}）")
    print(f"  勝率             : {r['win_rate']:.1f}%    プロフィットファクター: {pf_s}")
    print(f"  損益合計         : {sign}{r['total']:,.0f}円  （{sign}{r['ret_pct']:.2f}%）")
    print(f"  平均保有日数     : {r['avg_hold']:.1f}日")
    print(f"  現在値           : {r['last_close']:,.1f}円  "
          f"MACDヒスト: {r['last_hist']:+.2f}  25MA: {r['last_ma25']:,.1f}  "
          f"トレンド: {'↑' if r['last_close'] > r['last_ma25'] else '↓'}")
    print()

    if r["trade_log"]:
        print(f"  {'#':<3} {'エントリー':>12} {'エグジット':>12} {'買値':>9} {'売値':>9} "
              f"{'株数':>5} {'損益':>10} {'保有日':>5}  決済理由")
        print("  " + "─" * 82)
        for i, t in enumerate(r["trade_log"], 1):
            mark = " ★" if t["reason"] == "保有中（最終日終値）" else ""
            print(f"  {i:<3} {t['entry_dt'].strftime('%Y-%m-%d'):>12} "
                  f"{t['exit_dt'].strftime('%Y-%m-%d'):>12} "
                  f"{t['entry_price']:>9,.1f} {t['exit_price']:>9,.1f} "
                  f"{t['qty']:>5} {t['pnl']:>+10,.0f} {t['hold_days']:>4}日  "
                  f"{t['reason']}{mark}")
        print("  " + "─" * 82)
        if r["open_pos"]:
            print("  ★ = 現在保有中（最終日終値で仮決済）")
    print()


# ── ランキング表示 ──────────────────────────────────────────
def print_ranking(results: list[dict], backtest_days: int, top_n: int) -> None:
    since    = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today    = datetime.today().strftime("%Y-%m-%d")
    sorted_r = sorted(results, key=lambda x: x["ret_pct"], reverse=True)
    display  = sorted_r[:top_n]

    print()
    print("═" * 80)
    print(f"  MACDブレイクアウト × 出来高 × ATRトレイリング  "
          f"ランキング TOP{top_n}  [{since} ～ {today}]")
    print("═" * 80)
    print(f"  {'順位':<4} {'銘柄':<24} {'損益':>10} {'損益率':>8} {'勝率':>6} "
          f"{'PF':>5} {'取引':>4} {'平均保有':>7}  現在値")
    print("  " + "─" * 76)

    for rank, r in enumerate(display, 1):
        sign  = "+" if r["ret_pct"] >= 0 else ""
        trend = "↑" if r["last_close"] > r["last_ma25"] else "↓"
        hist_s = f"{r['last_hist']:+.2f}"
        pf_s   = "∞" if r["pf"] == float("inf") else f"{r['pf']:.1f}"
        bar    = ("▲" if r["ret_pct"] >= 0 else "▽") * min(int(abs(r["ret_pct"]) / 2), 12)

        label = f"{r['name']}({r['symbol']})"
        print(f"  {rank:<4} {label:<24} "
              f"{sign}{r['total']:>9,.0f}円 {sign}{r['ret_pct']:>6.1f}% "
              f"{r['win_rate']:>5.0f}% {pf_s:>5} {r['trades']:>3}回 "
              f"{r['avg_hold']:>5.1f}日  {r['last_close']:>7,.0f}{trend} "
              f"ヒスト:{hist_s}  {bar}")

    print("  " + "─" * 76)
    total_tr   = sum(r["trades"] for r in results)
    profitable = sum(1 for r in results if r["ret_pct"] > 0)
    avg_ret    = sum(r["ret_pct"] for r in results) / len(results)
    no_sig_cnt = len(SYMBOLS) - len(results)
    print(f"  スキャン: {len(SYMBOLS)}銘柄  取引あり: {len(results)}件  "
          f"取引なし: {no_sig_cnt}件  トレード計: {total_tr}回")
    print(f"  プラス銘柄: {profitable}/{len(results)}  平均損益率: {avg_ret:+.2f}%")
    print()
    print(f"  【アルゴリズム】 MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) ヒストグラム ゼロクロス上抜け"
          f"  ＋  出来高 >{VOL_SPIKE_MULT}×20日平均  ＋  終値 >25MA")
    print(f"  【決済】 ヒストグラム ゼロクロス下抜け  or  ATRトレイリング×{ATR_TRAIL_MULT}")
    print(f"  【資金】 {INITIAL_CASH:,}円/銘柄  リスク{RISK_PER_TRADE*100:.0f}%/トレード")
    print()


# ── メイン ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="MACDブレイクアウト × 出来高 バックテスト（60銘柄）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_macd_scan.py                    # 直近1ヶ月
  python backtest_macd_scan.py --months 3         # 直近3ヶ月
  python backtest_macd_scan.py --years 1          # 直近1年
  python backtest_macd_scan.py --years 5          # 直近5年
  python backtest_macd_scan.py --days 45          # 直近45日
  python backtest_macd_scan.py 7203.T --years 2   # トヨタ 2年 詳細
  python backtest_macd_scan.py --years 3 --top 20 # 3年 上位20件
""")
    parser.add_argument("symbol",   nargs="?", default=None,
                        help="特定銘柄コード（省略時は60銘柄スキャン）")
    parser.add_argument("--days",   type=int,  default=None,
                        help="バックテスト日数")
    parser.add_argument("--months", type=int,  default=None,
                        help="バックテスト月数（1〜60）")
    parser.add_argument("--years",  type=int,  default=None,
                        help="バックテスト年数（1〜5）")
    parser.add_argument("--top",    type=int,  default=30,
                        help="ランキング表示件数（デフォルト: 30）")
    args = parser.parse_args()

    # 期間を日数に変換
    if args.days is not None:
        days = args.days
        label = f"{days}日"
    elif args.months is not None:
        days  = max(1, min(args.months, 60)) * 30
        label = f"{args.months}ヶ月"
    elif args.years is not None:
        days  = max(1, min(args.years, 5)) * 365
        label = f"{args.years}年"
    else:
        days  = BACKTEST_DAYS
        label = "1ヶ月"

    since = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    print(f"\n  MACDブレイクアウト × 出来高 × ATRトレイリング  直近{label}バックテスト")
    print(f"  期間: {since} ～ {datetime.today().strftime('%Y-%m-%d')}\n")

    # 対象銘柄を絞り込み
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        target = [(s, n) for s, n in SYMBOLS if s == sym]
        if not target:
            target = [(sym, sym.replace(".T", ""))]
    else:
        target = SYMBOLS

    total      = len(target)
    stock_data = {}

    # Phase 1: 順次データ取得
    print(f"  [Phase 1] データ取得中 ({total}銘柄)...")
    for i, (sym, name) in enumerate(target, 1):
        print(f"  [{i:2d}/{total}] {name}({sym})", end=" ", flush=True)
        df = fetch_df(sym, backtest_days=days)
        if df is None:
            print("× スキップ")
        else:
            print("✓")
            stock_data[sym] = (name, df)

    # Phase 2: 並列バックテスト計算
    print(f"\n  [Phase 2] バックテスト計算中 ({len(stock_data)}銘柄, {WORKERS}並列)...")
    tasks   = [(s, n, d) for s, (n, d) in stock_data.items()]
    results = []

    def _bt(task):
        return run_backtest(task[0], task[1], task[2], days)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_bt, t): t[0] for t in tasks}
        done = 0
        for fut in as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            print(f"  計算中 {done}/{len(tasks)}", end="\r", flush=True)

    print()

    if not results:
        print("\n  シグナルが発生した銘柄がありませんでした。\n")
        return

    if args.symbol:
        for r in results:
            print_detail(r, days)
    else:
        print_ranking(results, days, args.top)


if __name__ == "__main__":
    main()
