"""
バックテスト: swing_notify_top10.py 戦略
過去5年の日足データで10銘柄一括検証（S株 1株単位）

使い方:
  python backtest_top10.py
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── 戦略パラメータ（swing_notify_top10.py と完全一致） ──────────
WATCH_LIST = [
    ("8604.T", "野村HD"),
    ("2802.T", "味の素"),
    ("2914.T", "JT"),
    ("7203.T", "トヨタ自動車"),
    ("8001.T", "伊藤忠商事"),
    ("8306.T", "三菱UFJ"),
    ("6752.T", "パナソニックHD"),
    ("8002.T", "丸紅"),
    ("9020.T", "JR東日本"),
    ("6902.T", "デンソー"),
]

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
RISK_PER_TRADE = 0.03
INITIAL_CASH   = 500_000
MAX_COST_RATIO = 0.10
MAX_QTY        = 3000


# ── データ取得 ────────────────────────────────────────────────
def fetch_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ── インジケーター（swing_notify_top10.py と同一） ────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

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
    tr = pd.concat([h - l,
                    (h - prev_c).abs(),
                    (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    return df


# ── シグナル（swing_notify_top10.py と同一） ─────────────────
def add_signals(df: pd.DataFrame) -> pd.DataFrame:
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


# ── 株数計算（swing_notify_top10.py と同一） ─────────────────
def calc_qty(cash: float, atr: float, close: float) -> int:
    stop_dist = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    qty_by_risk = int(cash * RISK_PER_TRADE / stop_dist)
    qty_by_cost = int(cash * MAX_COST_RATIO / close) if close > 0 else qty_by_risk
    qty = min(qty_by_risk, qty_by_cost, MAX_QTY)
    return max(qty, 1)


# ── 1銘柄バックテスト ─────────────────────────────────────────
def backtest_symbol(df: pd.DataFrame, symbol: str, name: str) -> dict:
    """
    シグナルは前日終値で確認し、翌日始値で執行。
    ストップは当日の安値がストップ価格以下になった場合に執行
    （始値がギャップダウンしていれば始値で決済）。
    """
    cash = INITIAL_CASH
    in_pos = False
    entry_price = stop_price = 0.0
    qty = 0
    entry_dt = None
    trades = []

    for i in range(1, len(df)):
        today = df.iloc[i]
        prev  = df.iloc[i - 1]

        # ── 保有中: 決済判定 ─────────────────────────────────
        if in_pos:
            exit_price  = None
            exit_reason = None

            if today["low"] <= stop_price:
                # ストップ到達: ギャップダウン考慮
                exit_price  = min(today["open"], stop_price)
                exit_reason = "stop"
            elif prev["exit_sig"]:
                exit_price  = today["open"]
                exit_reason = "signal"

            if exit_price is not None:
                pnl    = (exit_price - entry_price) * qty
                cash  += exit_price * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     df.index[i],
                    "symbol":      symbol,
                    "name":        name,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl,
                    "reason":      exit_reason,
                })
                in_pos = False

        # ── 待機中: エントリー判定 ───────────────────────────
        if not in_pos and prev["entry_sig"]:
            q    = calc_qty(cash, prev["atr"], prev["close"])
            cost = today["open"] * q
            if q > 0 and cost <= cash:
                entry_price = today["open"]
                stop_price  = entry_price - prev["atr"] * ATR_STOP_MULT
                qty         = q
                cash       -= cost
                entry_dt    = df.index[i]
                in_pos      = True

    # 未決済: 最終日終値で強制クローズ
    if in_pos:
        exit_price = df.iloc[-1]["close"]
        pnl        = (exit_price - entry_price) * qty
        cash      += exit_price * qty
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     df.index[-1],
            "symbol":      symbol,
            "name":        name,
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "qty":         qty,
            "pnl":         pnl,
            "reason":      "force_close",
        })

    total_pnl = cash - INITIAL_CASH
    n_trades  = len(trades)

    wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf     = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")

    return {
        "symbol":        symbol,
        "name":          name,
        "trades":        n_trades,
        "win_rate":      len(wins) / n_trades * 100 if n_trades else 0.0,
        "total_pnl":     total_pnl,
        "return_pct":    total_pnl / INITIAL_CASH * 100,
        "avg_pnl":       np.mean([t["pnl"] for t in trades]) if trades else 0.0,
        "max_win":       max(wins)    if wins   else 0.0,
        "max_loss":      min(losses)  if losses else 0.0,
        "profit_factor": pf,
        "trades_detail": trades,
    }


# ── ポートフォリオ指標 ────────────────────────────────────────
def portfolio_metrics(all_trades: list) -> dict:
    if not all_trades:
        return {}

    td = pd.DataFrame(all_trades).sort_values("exit_dt").reset_index(drop=True)

    # 累積損益 → 最大ドローダウン
    td["cum_pnl"] = td["pnl"].cumsum()
    initial_total = INITIAL_CASH * len(WATCH_LIST)
    equity        = initial_total + td["cum_pnl"]
    peak          = equity.cummax()
    drawdown      = (peak - equity) / peak * 100
    max_dd        = drawdown.max()

    wins   = td[td["pnl"] > 0]
    losses = td[td["pnl"] <= 0]
    pf     = wins["pnl"].sum() / abs(losses["pnl"].sum()) \
             if len(losses) > 0 and losses["pnl"].sum() != 0 else float("inf")

    td["hold_days"] = (td["exit_dt"] - td["entry_dt"]).dt.days

    return {
        "n_trades":      len(td),
        "win_rate":      len(wins) / len(td) * 100,
        "profit_factor": pf,
        "max_drawdown":  max_dd,
        "avg_hold_days": td["hold_days"].mean(),
        "avg_pnl":       td["pnl"].mean(),
        "best_trade":    td["pnl"].max(),
        "worst_trade":   td["pnl"].min(),
        "stop_count":    len(td[td["reason"] == "stop"]),
        "signal_count":  len(td[td["reason"] == "signal"]),
        "force_count":   len(td[td["reason"] == "force_close"]),
    }


# ── メイン ───────────────────────────────────────────────────
def main():
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * 5 + 60)).strftime("%Y-%m-%d")

    print(f"\n{'='*65}")
    print(f"  バックテスト  swing_notify_top10.py 戦略")
    print(f"  期間 : {start} 〜 {end}（約5年）")
    print(f"  資金 : {INITIAL_CASH:,}円/銘柄 × {len(WATCH_LIST)}銘柄"
          f" = {INITIAL_CASH * len(WATCH_LIST):,}円")
    print(f"  設定 : コスト上限{MAX_COST_RATIO*100:.0f}%  "
          f"リスク{RISK_PER_TRADE*100:.0f}%  "
          f"ストップATR×{ATR_STOP_MULT}")
    print(f"{'='*65}")

    results    = []
    all_trades = []

    for symbol, name in WATCH_LIST:
        print(f"  取得中: {name}({symbol}) ...", end="\r", flush=True)
        try:
            df = fetch_data(symbol, start, end)
            df = add_indicators(df)
            df = add_signals(df)
            res = backtest_symbol(df, symbol, name)
            results.append(res)
            all_trades.extend(res["trades_detail"])
        except Exception as e:
            print(f"\n  ⚠ {name}({symbol}) スキップ: {e}")

    print(" " * 60, end="\r")

    # ── 銘柄別サマリー ────────────────────────────────────────
    print(f"\n{'─'*65}")
    print(f"  {'銘柄名':<16} {'取引':>4} {'勝率':>7} {'損益(円)':>10} {'収益率':>7} {'PF':>6}")
    print(f"{'─'*65}")

    for r in results:
        pf_s = "  ∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:6.2f}"
        sign = "+" if r["total_pnl"] >= 0 else ""
        print(
            f"  {r['name']:<16} {r['trades']:>4} {r['win_rate']:>6.1f}%"
            f" {sign}{r['total_pnl']:>9,.0f} {sign}{r['return_pct']:>5.1f}%"
            f" {pf_s}"
        )

    print(f"{'─'*65}")

    # ── ポートフォリオ合計 ────────────────────────────────────
    m             = portfolio_metrics(all_trades)
    total_pnl     = sum(r["total_pnl"] for r in results)
    total_init    = INITIAL_CASH * len(WATCH_LIST)
    total_ret_pct = total_pnl / total_init * 100
    years         = (datetime.strptime(end, "%Y-%m-%d") -
                     datetime.strptime(start, "%Y-%m-%d")).days / 365.25
    cagr          = ((total_init + total_pnl) / total_init) ** (1 / years) - 1

    pf_s = "∞（損失ゼロ）" if m["profit_factor"] == float("inf") \
           else f"{m['profit_factor']:.2f}"
    sign = "+" if total_pnl >= 0 else ""

    print(f"\n{'='*65}")
    print(f"  【ポートフォリオ合計】")
    print(f"{'='*65}")
    print(f"  総損益            : {sign}{total_pnl:,.0f} 円")
    print(f"  総収益率          : {sign}{total_ret_pct:.2f} %")
    print(f"  年率換算(CAGR)    : {cagr*100:+.2f} %")
    print(f"  総取引数          : {m['n_trades']} 回")
    print(f"  勝率              : {m['win_rate']:.1f} %")
    print(f"  プロフィットファクター: {pf_s}")
    print(f"  最大ドローダウン  : -{m['max_drawdown']:.2f} %")
    print(f"  平均保有日数      : {m['avg_hold_days']:.1f} 日")
    print(f"  平均損益/トレード : {m['avg_pnl']:+,.0f} 円")
    print(f"  最大利益トレード  : +{m['best_trade']:,.0f} 円")
    print(f"  最大損失トレード  : {m['worst_trade']:,.0f} 円")
    print(f"  決済内訳          : シグナル {m['signal_count']} / "
          f"ストップ {m['stop_count']} / "
          f"強制 {m['force_count']}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
