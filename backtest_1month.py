"""
1か月バックテスト（A7: トレンドフィルター付きストキャスティクス + ATRトレイリングストップ）
────────────────────────────────────────────────────────────────────────
対象銘柄: signal_monitor の WATCH_LIST（スキャン利益率ランキング上位30銘柄）
バックテスト期間: 直近30日間（指標計算には6ヶ月分データを使用）

■ 実行方法:
  python backtest_1month.py               # 上位30銘柄 全スキャン
  python backtest_1month.py 7203.T        # 特定銘柄のみ詳細表示
  python backtest_1month.py --days 20     # 直近20日に変更

■ 出力:
  - 銘柄ごとの損益・勝率・平均保有日数
  - 個別トレード一覧（エントリー日・エグジット日・損益）
  - ランキング表示
"""

import argparse
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── 対象銘柄（signal_monitor WATCH_LIST と同一） ──────────────
WATCH_LIST = [
    ("6326.T",  "クボタ"),
    ("5101.T",  "横浜ゴム"),
    ("6473.T",  "ジェイテクト"),
    ("8830.T",  "住友不動産"),
    ("3105.T",  "日清紡HD"),
    ("8267.T",  "イオン"),
    ("7202.T",  "いすゞ自動車"),
    ("8802.T",  "三菱地所"),
    ("6305.T",  "日立建機"),
    ("8304.T",  "あおぞら銀行"),
    ("4506.T",  "住友ファーマ"),
    ("6506.T",  "安川電機"),
    ("6103.T",  "オークマ"),
    ("3289.T",  "東急不動産HD"),
    ("1803.T",  "清水建設"),
    ("4004.T",  "レゾナック・HD"),
    ("9532.T",  "大阪ガス"),
    ("6471.T",  "日本精工"),
    ("4503.T",  "アステラス製薬"),
    ("6302.T",  "住友重機械工業"),
    ("1802.T",  "大林組"),
    ("8233.T",  "高島屋"),
    ("6752.T",  "パナソニックHD"),
    ("4902.T",  "コニカミノルタ"),
    ("9502.T",  "中部電力"),
    ("7013.T",  "IHI"),
    ("8002.T",  "丸紅"),
    ("7731.T",  "ニコン"),
    ("6971.T",  "京セラ"),
    ("8628.T",  "松井証券"),
]

# ── パラメータ ──────────────────────────────────────────────
BACKTEST_DAYS    = 30           # バックテスト期間（日）← ここを変更
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
def print_ranking(results: list[dict], backtest_days: int) -> None:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    sorted_r = sorted(results, key=lambda x: x["ret_pct"], reverse=True)

    print()
    print("═" * 78)
    print(f"  A7 直近{backtest_days}日バックテスト ランキング  [{since} ～ {today}]")
    print("═" * 78)
    print(f"  {'順位':<4} {'銘柄':<22} {'損益':>10} {'損益率':>8} {'勝率':>7} "
          f"{'取引数':>6} {'平均保有':>8}  現在値")
    print("  " + "─" * 74)

    for rank, r in enumerate(sorted_r, 1):
        sign    = "+" if r["ret_pct"] >= 0 else ""
        bar_len = int(abs(r["ret_pct"]) / 2)
        bar     = ("▲" if r["ret_pct"] >= 0 else "▽") * min(bar_len, 15)
        trend   = "↑" if r["last_close"] > r["last_ma75"] else "↓"

        print(f"  {rank:<4} {r['name']}({r['symbol']}){'':<{max(0,20-len(r['name'])-len(r['symbol']))}}"
              f"  {sign}{r['total']:>9,.0f}円 {sign}{r['ret_pct']:>6.1f}% "
              f"{r['win_rate']:>6.0f}%  {r['trades']:>4}回  {r['avg_hold']:>5.1f}日  "
              f"{r['last_close']:>7,.0f}{trend}  {bar}")

    print("  " + "─" * 74)
    total_trades   = sum(r["trades"] for r in results)
    profitable     = sum(1 for r in results if r["ret_pct"] > 0)
    avg_ret        = sum(r["ret_pct"] for r in results) / len(results) if results else 0
    print(f"  対象銘柄: {len(results)}件  トレード計: {total_trades}回  "
          f"プラス銘柄: {profitable}/{len(results)}  平均損益率: {avg_ret:+.2f}%")
    print()
    print(f"  ※ 運用資金 {INITIAL_CASH:,}円/銘柄  ATRストップ×{ATR_STOP_MULT}  "
          f"トレイル×{ATR_TRAIL_MULT}  75MA トレンドフィルター")
    print()

    # サマリー：取引ゼロ銘柄
    no_trade = [s for s, n in WATCH_LIST if not any(r["symbol"] == s for r in results)]
    if no_trade:
        print(f"  ※ 取引なし（シグナル不発）: {len(no_trade)}銘柄")
        for sym in no_trade:
            nm = next((n for s, n in WATCH_LIST if s == sym), sym)
            print(f"     {nm}({sym})")
        print()


# ── メイン ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="A7 直近Nか月バックテスト")
    parser.add_argument("symbol",    nargs="?",  default=None,
                        help="特定銘柄コード（省略時は全WATCH_LISTをスキャン）")
    parser.add_argument("--days",    type=int,   default=BACKTEST_DAYS,
                        help=f"バックテスト日数（デフォルト: {BACKTEST_DAYS}日）")
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
        target = [(s, n) for s, n in WATCH_LIST if s == sym_input]
        if not target:
            # WATCH_LIST外でも試みる
            nm = sym_input.replace(".T", "")
            target = [(sym_input, nm)]
    else:
        target = WATCH_LIST

    results = []
    total   = len(target)

    for i, (sym, name) in enumerate(target, 1):
        print(f"  [{i:2d}/{total}] {name}({sym}) データ取得中...", end=" ", flush=True)
        df = fetch_df(sym)
        if df is None:
            print("スキップ（データ取得失敗）")
            continue

        r = run_backtest(sym, name, df, days)
        if r is None:
            print("取引なし（シグナル不発）")
            continue

        sign = "+" if r["ret_pct"] >= 0 else ""
        print(f"{sign}{r['ret_pct']:.1f}%  {r['trades']}取引  勝率{r['win_rate']:.0f}%")
        results.append(r)

        # 単銘柄指定の場合は詳細表示
        if args.symbol:
            print_detail(r, days)

    if not results:
        print("\n  取引シグナルが発生した銘柄がありませんでした。\n")
        return

    # 全銘柄スキャンの場合はランキング表示
    if not args.symbol or len(target) > 1:
        print_ranking(results, days)

    # 単銘柄の場合はサマリーも出す
    if args.symbol and results:
        r = results[0]
        sign = "+" if r["ret_pct"] >= 0 else ""
        print(f"  結果: {sign}{r['total']:,.0f}円  ({sign}{r['ret_pct']:.2f}%)")


if __name__ == "__main__":
    main()
