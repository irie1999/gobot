"""
移動平均クロス バックテスト
銘柄: トヨタ自動車 (7203.T)
期間: 2020-01-01 〜 2024-12-31
"""

import sys
import pandas as pd
import yfinance as yf

# ---- 設定 ----
SYMBOL      = "7203.T"
START       = "2020-01-01"
END         = "2024-12-31"
SHORT_PERIOD = 5
LONG_PERIOD  = 25
INITIAL_CASH = 1_000_000   # 初期資金（円）
QTY          = 100          # 1回の取引株数


# ---- データ取得 ----

def fetch_data(symbol: str, start: str, end: str) -> pd.Series:
    print(f"データ取得中: {symbol} ({start} 〜 {end})")
    df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if df.empty:
        print("ERROR: データが取得できませんでした。銘柄コード・期間を確認してください。")
        sys.exit(1)
    close = df["Close"].squeeze()
    print(f"取得完了: {len(close)} 営業日\n")
    return close


# ---- バックテスト ----

def run_backtest(
    close: pd.Series,
    short_period: int,
    long_period: int,
    initial_cash: float,
    qty: int,
) -> pd.DataFrame:
    short_ma = close.rolling(short_period).mean()
    long_ma  = close.rolling(long_period).mean()

    # シグナル: 1=買い保有, 0=ポジションなし
    signal = (short_ma > long_ma).astype(int)
    # クロス検出: +1=ゴールデンクロス(買い), -1=デッドクロス(売り)
    cross = signal.diff()

    trades = []
    cash = initial_cash
    entry_price = None
    entry_date  = None

    for date, price in close.items():
        c = cross.get(date, 0)

        if c == 1 and entry_price is None:
            # 買いエントリー
            cost = price * qty
            if cash >= cost:
                cash -= cost
                entry_price = price
                entry_date  = date

        elif c == -1 and entry_price is not None:
            # 決済
            proceeds = price * qty
            cash += proceeds
            pnl = (price - entry_price) * qty
            trades.append({
                "entry_date":  entry_date,
                "exit_date":   date,
                "entry_price": entry_price,
                "exit_price":  price,
                "pnl":         pnl,
                "cash_after":  cash,
            })
            entry_price = None
            entry_date  = None

    # 未決済ポジションを最終値で強制決済
    if entry_price is not None:
        last_date  = close.index[-1]
        last_price = close.iloc[-1]
        proceeds   = last_price * qty
        cash += proceeds
        pnl  = (last_price - entry_price) * qty
        trades.append({
            "entry_date":  entry_date,
            "exit_date":   last_date,
            "entry_price": entry_price,
            "exit_price":  last_price,
            "pnl":         pnl,
            "cash_after":  cash,
            "note":        "未決済→強制決済",
        })

    return pd.DataFrame(trades)


# ---- 評価指標 ----

def evaluate(trades: pd.DataFrame, initial_cash: float) -> None:
    if trades.empty:
        print("トレードが発生しませんでした。")
        return

    total_trades = len(trades)
    wins         = (trades["pnl"] > 0).sum()
    losses       = (trades["pnl"] <= 0).sum()
    win_rate     = wins / total_trades * 100
    total_pnl    = trades["pnl"].sum()
    final_cash   = trades["cash_after"].iloc[-1]
    return_pct   = (final_cash - initial_cash) / initial_cash * 100

    # 最大ドローダウン
    cumulative = trades["cash_after"]
    peak       = cumulative.cummax()
    drawdown   = (cumulative - peak) / peak * 100
    max_dd     = drawdown.min()

    avg_win  = trades.loc[trades["pnl"] > 0,  "pnl"].mean() if wins   > 0 else 0
    avg_loss = trades.loc[trades["pnl"] <= 0, "pnl"].mean() if losses > 0 else 0
    payoff   = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    print("=" * 50)
    print(f"  バックテスト結果 ({SYMBOL}  {START}〜{END})")
    print("=" * 50)
    print(f"  初期資金         : {initial_cash:>12,.0f} 円")
    print(f"  最終資産         : {final_cash:>12,.0f} 円")
    print(f"  総損益           : {total_pnl:>+12,.0f} 円")
    print(f"  運用リターン     : {return_pct:>+11.2f} %")
    print("-" * 50)
    print(f"  総トレード数     : {total_trades:>12} 回")
    print(f"  勝ちトレード     : {wins:>12} 回")
    print(f"  負けトレード     : {losses:>12} 回")
    print(f"  勝率             : {win_rate:>11.1f} %")
    print(f"  平均利益         : {avg_win:>+12,.0f} 円")
    print(f"  平均損失         : {avg_loss:>+12,.0f} 円")
    print(f"  ペイオフレシオ   : {payoff:>12.2f}")
    print(f"  最大ドローダウン : {max_dd:>+11.2f} %")
    print("=" * 50)

    print("\n--- トレード一覧 ---")
    display_cols = ["entry_date", "exit_date", "entry_price", "exit_price", "pnl"]
    print(trades[display_cols].to_string(index=False, float_format="{:,.0f}".format))


# ---- エントリーポイント ----

if __name__ == "__main__":
    close  = fetch_data(SYMBOL, START, END)
    trades = run_backtest(close, SHORT_PERIOD, LONG_PERIOD, INITIAL_CASH, QTY)
    evaluate(trades, INITIAL_CASH)
