"""
RSI(2) 平均回帰戦略  軽量版（50銘柄スキャン対応）
─────────────────────────────────────────────────
使い方:
  python rsi2_simple.py              # 50銘柄スキャン（1年）
  python rsi2_simple.py --years 2    # 50銘柄スキャン（2年）
  python rsi2_simple.py 7011.T       # 1銘柄詳細
  python rsi2_simple.py 7011.T --years 2
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── 対象50銘柄 ──────────────────────────────────────────────────
SYMBOLS = [
    # 銀行・保険（金利上昇恩恵）
    ("8316.T", "三井住友FG"),
    ("8306.T", "三菱UFJFG"),
    ("8411.T", "みずほFG"),
    ("8308.T", "りそなHD"),
    ("8766.T", "東京海上HD"),
    ("8725.T", "MS&AD"),
    ("8630.T", "SOMPOhd"),
    ("8750.T", "第一生命HD"),
    # 重工・防衛
    ("7011.T", "三菱重工"),
    ("7013.T", "IHI"),
    ("7012.T", "川崎重工"),
    ("5631.T", "日本製鋼所"),
    # 半導体・電機
    ("8035.T", "東京エレクトロン"),
    ("6857.T", "アドバンテスト"),
    ("6723.T", "ルネサス"),
    ("6758.T", "ソニーG"),
    ("6861.T", "キーエンス"),
    ("6954.T", "ファナック"),
    ("6501.T", "日立"),
    ("6702.T", "富士通"),
    # 自動車
    ("7203.T", "トヨタ"),
    ("7267.T", "ホンダ"),
    ("7270.T", "SUBARU"),
    # 機械
    ("6301.T", "コマツ"),
    ("6273.T", "SMC"),
    ("6367.T", "ダイキン"),
    # 化学・素材
    ("4063.T", "信越化学"),
    ("5713.T", "住友金属鉱山"),
    ("4901.T", "富士フイルムHD"),
    ("6988.T", "日東電工"),
    # 医薬
    ("4502.T", "武田薬品"),
    ("4519.T", "中外製薬"),
    ("4568.T", "第一三共"),
    ("4543.T", "テルモ"),
    # 商社
    ("8031.T", "三井物産"),
    ("8058.T", "三菱商事"),
    ("8001.T", "伊藤忠"),
    # 不動産
    ("8801.T", "三井不動産"),
    ("8802.T", "三菱地所"),
    # 通信・IT
    ("9432.T", "NTT"),
    ("9433.T", "KDDI"),
    ("9984.T", "SBG"),
    ("9983.T", "ファストリ"),
    # 電力・ガス
    ("9503.T", "関西電力"),
    ("9531.T", "東京ガス"),
    # 鉄道
    ("9020.T", "JR東日本"),
    ("9022.T", "JR東海"),
    # 証券・精密
    ("8604.T", "野村HD"),
    ("7741.T", "HOYA"),
    ("7751.T", "キヤノン"),
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
