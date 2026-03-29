"""
RSI(2) 平均回帰戦略  軽量版
─────────────────────────────
使い方:
  python rsi2_simple.py              # デフォルト: 8316.T (SMFG) 1年
  python rsi2_simple.py 7011.T       # 銘柄指定
  python rsi2_simple.py 7011.T --years 2
"""

import argparse
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── パラメーター ────────────────────────────────────────────────
RSI2_ENTRY      =  10.0   # RSI(2) がこの値以下 → 翌日買い
RSI2_EXIT       =  65.0   # RSI(2) がこの値以上 → 翌日売り
MA_TREND        = 200     # トレンドフィルター: 終値 > MA200 のみ対象
HARD_STOP_PCT   =   3.0   # 即損切り %
HALF_PROFIT_PCT =   5.0   # 半分利確 %
ATR_TRAIL_MULT  =   2.0   # ATR トレイリング係数
POSITION_SIZE   = 50_000  # 1回あたりの購入金額（円）
BACKTEST_YEARS  =   1     # デフォルトのバックテスト期間


# ── データ取得 ──────────────────────────────────────────────────
def fetch(symbol: str, years: int) -> pd.DataFrame | None:
    period = f"{years + 1}y"   # MA200 計算バッファ込み
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

    # RSI(2)
    d     = c.diff()
    gain  = d.clip(lower=0).ewm(span=2, adjust=False).mean()
    loss  = (-d).clip(lower=0).ewm(span=2, adjust=False).mean()
    rsi2  = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # RSI(14) — 参考表示用
    g14   = d.clip(lower=0).ewm(span=14, adjust=False).mean()
    l14   = (-d).clip(lower=0).ewm(span=14, adjust=False).mean()
    rsi14 = 100 - 100 / (1 + g14 / l14.replace(0, np.nan))

    # MA / ATR
    ma200 = c.rolling(200).mean()
    ma50  = c.rolling(50).mean()
    prev  = c.shift(1)
    tr    = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr   = tr.ewm(span=14, adjust=False).mean()

    df = df.copy()
    df["rsi2"]    = rsi2
    df["rsi14"]   = rsi14
    df["ma200"]   = ma200
    df["ma50"]    = ma50
    df["atr"]     = atr
    return df


# ── バックテスト ────────────────────────────────────────────────
def backtest(df: pd.DataFrame, years: int) -> list[dict]:
    cutoff = pd.Timestamp(datetime.today() - timedelta(days=years * 365))
    df = df[df.index >= cutoff].copy()

    trades    = []
    in_pos    = False
    entry_p   = trail  = 0.0
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

        # ── エグジット ──────────────────────────────────────────
        if in_pos:
            reason = exit_p = None

            if lo <= entry_p * (1 - HARD_STOP_PCT / 100):   # 即損切り
                exit_p = min(op, entry_p * (1 - HARD_STOP_PCT / 100))
                reason = f"損切り(-{HARD_STOP_PCT:.0f}%)"

            elif lo <= trail:                                 # ATR トレイル
                exit_p = min(op, trail)
                reason = "トレイリング"

            elif float(prev["rsi2"]) >= RSI2_EXIT:           # RSI(2) 回復
                exit_p = op
                reason = f"RSI2回復(≥{RSI2_EXIT:.0f})"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p,
                    qty=qty, pnl=pnl,
                    hold=(dt - entry_dt).days, reason=reason,
                    rsi2_at_entry=float(df.iloc[trades.__len__() if trades else 0]["rsi2"]
                                        if False else prev["rsi2"]),
                ))
                in_pos = half_done = False
                continue

            # 半分利確
            if not half_done:
                cl = float(row["close"])
                if (cl - entry_p) / entry_p * 100 >= HALF_PROFIT_PCT:
                    hq = qty // 2
                    if hq > 0:
                        pnl_h = (cl - entry_p) * hq
                        trades.append(dict(
                            entry_dt=entry_dt, exit_dt=dt,
                            entry_p=entry_p, exit_p=cl,
                            qty=hq, pnl=pnl_h,
                            hold=(dt - entry_dt).days,
                            reason=f"半分利確(+{HALF_PROFIT_PCT:.0f}%)",
                            rsi2_at_entry=0.0,
                        ))
                        qty -= hq
                        half_done = True

            # ATR トレイル更新
            cand = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
            if cand > trail:
                trail = cand

        # ── エントリー ──────────────────────────────────────────
        if not in_pos:
            rsi2_prev  = float(prev["rsi2"])
            ma200_prev = float(prev["ma200"])
            close_prev = float(prev["close"])

            if (rsi2_prev <= RSI2_ENTRY              # RSI(2) 売られすぎ
                    and close_prev > ma200_prev       # MA200 上（長期上昇トレンド）
                    and op > 0):
                qty = max(int(POSITION_SIZE / op), 0)
                if qty > 0:
                    entry_p   = op
                    trail     = op - float(row["atr"]) * ATR_TRAIL_MULT
                    entry_dt  = dt
                    half_done = False
                    in_pos    = True

    # 未決済 → 最終日終値で仮決済
    if in_pos:
        last = df.iloc[-1]
        lp   = float(last["close"])
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=lp,
            qty=qty, pnl=(lp - entry_p) * qty,
            hold=(df.index[-1] - entry_dt).days,
            reason="保有中★", rsi2_at_entry=0.0,
        ))

    return trades


# ── 結果表示 ────────────────────────────────────────────────────
def show(symbol: str, trades: list[dict], years: int) -> None:
    if not trades:
        print(f"\n  [{symbol}]  シグナルなし\n")
        return

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    wr     = len(wins) / len(trades) * 100
    pf     = (sum(t["pnl"] for t in wins) /
              abs(sum(t["pnl"] for t in losses))
              if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
    avg_win  = sum(t["hold"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(t["hold"] for t in losses) / len(losses) if losses else 0

    since = (datetime.today() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    print()
    print("═" * 60)
    print(f"  RSI(2) 平均回帰戦略  [{symbol}]  直近{years}年")
    print(f"  期間: {since} ～ {today}")
    print("═" * 60)
    print(f"  トレード: {len(trades)}回  勝: {len(wins)}  負: {len(losses)}")
    print(f"  勝率   : {wr:.1f}%    PF: {'∞' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"  損益   : {total:+,.0f}円")
    print(f"  平均保有: 勝ち {avg_win:.1f}日 / 負け {avg_loss:.1f}日")
    print()
    print(f"  【条件】RSI(2) ≤{RSI2_ENTRY:.0f} + 終値>MA{MA_TREND}  →  翌日始値エントリー")
    print(f"  【決済】RSI(2) ≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / 損切り-{HARD_STOP_PCT:.0f}% / +{HALF_PROFIT_PCT:.0f}%半分利確")
    print()
    print(f"  {'#':<3} {'エントリー':>10} {'エグジット':>10} "
          f"{'買値':>8} {'売値':>8} {'損益':>9} {'保有':>4}  決済理由")
    print("  " + "─" * 56)
    for i, t in enumerate(trades, 1):
        pct  = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        mark = "★" if "保有中" in t["reason"] else " "
        print(f" {mark}{i:<3} "
              f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} "
              f"{t['pnl']:>+9,.0f} {t['hold']:>3}日  "
              f"{t['reason']}({pct:+.1f}%)")
    print("  " + "─" * 56)
    print()


# ── メイン ──────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="RSI(2) 平均回帰バックテスト（軽量版）")
    parser.add_argument("symbol", nargs="?", default="8316.T",
                        help="銘柄コード（例: 7011.T）")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS,
                        help="バックテスト期間（年）")
    args = parser.parse_args()

    sym = args.symbol.upper()
    if not sym.endswith(".T"):
        sym += ".T"

    print(f"\n  データ取得中: {sym} ...")
    df = fetch(sym, args.years)
    if df is None or len(df) < 210:
        print(f"  エラー: {sym} のデータ取得に失敗しました")
        return

    df     = calc(df)
    trades = backtest(df, args.years)
    show(sym, trades, args.years)


if __name__ == "__main__":
    main()
