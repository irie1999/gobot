"""
デイトレ バックテスト — 2026-03-19 トヨタ自動車 (7203.T)
データ取得: yfinance (1分足)
戦略      : 移動平均クロス（短期 MA / 長期 MA）

【実行方法】
  pip install yfinance pandas
  python daytrade_yf_toyota_20260319.py

【yfinance の制限】
  - 1分足は直近 7 日分のみ取得可能（2026-03-19 は 2 日前なので対象内）
  - ネットワーク環境によっては接続できない場合がある
    → その場合は合成データ（seed 固定）で自動フォールバック
"""

import random
import sys
from collections import deque
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL       = "7203.T"
TARGET_DATE  = "2026-03-19"
INTERVAL     = "1m"           # 1分足（yfinance: "1m"/"5m"/"15m"）

SHORT_PERIOD = 5              # 短期 MA（足数）
LONG_PERIOD  = 25             # 長期 MA（足数）
QTY          = 100            # 1 回の取引株数
INITIAL_CASH = 1_000_000      # 初期資金（円）


# ── 1. yfinance でデータ取得 ─────────────────────────────────

def fetch_yfinance() -> list[dict]:
    """
    yfinance で 2026-03-19 の 1 分足を取得する。
    取得できた場合は bars のリストを返す。
    失敗した場合は RuntimeError を送出する。
    """
    import yfinance as yf
    import pandas as pd

    # 翌日まで指定しないと当日分が抜ける場合がある
    start = TARGET_DATE
    end   = (datetime.strptime(TARGET_DATE, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"[yfinance] {SYMBOL}  {start} 〜 {end}  interval={INTERVAL} を取得中...")
    df = yf.download(
        SYMBOL,
        start=start,
        end=end,
        interval=INTERVAL,
        auto_adjust=True,
        progress=False,
    )

    if df.empty:
        raise RuntimeError("yfinance: データが空でした（銘柄・期間・ネットワークを確認してください）")

    # MultiIndex 対応（yfinance >=0.2 は (Metric, Ticker) の MultiIndex になることがある）
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 東京時間 2026-03-19 の行だけ絞り込む
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df = df[df.index.date == datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()]

    if df.empty:
        raise RuntimeError(f"yfinance: {TARGET_DATE} のデータが見つかりませんでした")

    bars = [
        {
            "dt":    row.Index.to_pydatetime(),
            "open":  float(row.Open),
            "high":  float(row.High),
            "low":   float(row.Low),
            "close": float(row.Close),
            "volume": int(row.Volume),
        }
        for row in df.itertuples()
    ]
    print(f"[yfinance] 取得成功: {len(bars)} 本")
    return bars


# ── 2. 合成データ（フォールバック） ──────────────────────────

def generate_synthetic_bars() -> list[dict]:
    """
    ネット接続不可時のフォールバック。
    2026-03-19 のトヨタ 1 分足を乱数でシミュレートする（シード固定）。

    前場: 09:00〜11:30 (150 本)
    後場: 12:30〜15:30 (180 本)
    """
    rng = random.Random(20260319)
    sessions = [
        (datetime(2026, 3, 19,  9,  0), datetime(2026, 3, 19, 11, 30)),
        (datetime(2026, 3, 19, 12, 30), datetime(2026, 3, 19, 15, 30)),
    ]
    price = 3_560.0
    bars  = []
    for (start, end) in sessions:
        dt = start
        while dt < end:
            change = rng.gauss(0.02, 3.0)
            if dt.hour == 9  and dt.minute < 15:
                change *= 2.0
            if dt.hour == 15 and dt.minute >= 15:
                change *= 1.5
            price = max(3_400.0, min(3_750.0, price + change))
            o = price + rng.gauss(0, 1.5)
            h = max(o, price) + abs(rng.gauss(0, 2))
            l = min(o, price) - abs(rng.gauss(0, 2))
            bars.append({
                "dt": dt, "open": round(o, 1), "high": round(h, 1),
                "low": round(l, 1), "close": round(price, 1),
                "volume": max(0, int(rng.gauss(60_000, 15_000))),
            })
            dt += timedelta(minutes=1)
    return bars


def load_bars() -> tuple[list[dict], str]:
    try:
        bars   = fetch_yfinance()
        source = f"yfinance  ({SYMBOL}  {TARGET_DATE}  {INTERVAL})"
    except Exception as e:
        print(f"[警告] yfinance 取得失敗: {e}")
        print("[フォールバック] 合成データを使用します\n")
        bars   = generate_synthetic_bars()
        source = "合成データ (seed=20260319, 1 分足)"
    return bars, source


# ── 3. MA クロス戦略 ─────────────────────────────────────────

class MACross:
    def __init__(self, short: int, long_: int):
        self.buf  = deque(maxlen=long_)
        self.short = short
        self.long_ = long_
        self.prev: str | None = None

    def feed(self, price: float) -> str | None:
        self.buf.append(price)
        if len(self.buf) < self.long_:
            return None
        s = sum(list(self.buf)[-self.short:]) / self.short
        l = sum(self.buf) / self.long_
        sig = "buy" if s > l else "sell"
        if sig != self.prev:
            self.prev = sig
            return sig
        return None


# ── 4. バックテスト ──────────────────────────────────────────

def run_backtest(bars: list[dict]) -> tuple[list[dict], float]:
    strat       = MACross(SHORT_PERIOD, LONG_PERIOD)
    cash        = float(INITIAL_CASH)
    entry_p     = None
    entry_dt    = None
    trades      = []

    for bar in bars:
        price  = bar["close"]
        dt     = bar["dt"]
        signal = strat.feed(price)

        if signal == "buy" and entry_p is None:
            cost = price * QTY
            if cash >= cost:
                cash    -= cost
                entry_p  = price
                entry_dt = dt

        elif signal == "sell" and entry_p is not None:
            cash += price * QTY
            trades.append({
                "entry_dt":    entry_dt,
                "exit_dt":     dt,
                "entry_price": entry_p,
                "exit_price":  price,
                "pnl":         (price - entry_p) * QTY,
                "cash":        cash,
            })
            entry_p = None

    # 引け強制決済
    if entry_p is not None:
        b = bars[-1]
        cash += b["close"] * QTY
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     b["dt"],
            "entry_price": entry_p,
            "exit_price":  b["close"],
            "pnl":         (b["close"] - entry_p) * QTY,
            "cash":        cash,
            "note":        "引け強制決済",
        })

    return trades, cash


# ── 5. レポート ──────────────────────────────────────────────

def report(trades: list[dict], final_cash: float, source: str, n_bars: int):
    W = 60
    sep = "=" * W
    print(sep)
    print(f"  デイトレ バックテスト  {TARGET_DATE}  {SYMBOL}")
    print(sep)
    print(f"  データ  : {source}")
    print(f"  足数    : {n_bars} 本")
    print(f"  MA      : 短期 {SHORT_PERIOD} / 長期 {LONG_PERIOD}")
    print(f"  取引株数: {QTY} 株")
    print("-" * W)

    if not trades:
        print("  トレードが発生しませんでした。")
        return

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n      = len(trades)
    total  = sum(t["pnl"] for t in trades)
    win_r  = len(wins) / n * 100
    avg_w  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_l  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    payoff = abs(avg_w / avg_l) if avg_l else float("inf")
    ret    = (final_cash - INITIAL_CASH) / INITIAL_CASH * 100

    vals   = [INITIAL_CASH] + [t["cash"] for t in trades]
    peak   = vals[0]; max_dd = 0.0
    for v in vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    print(f"  初期資金         : {INITIAL_CASH:>12,.0f} 円")
    print(f"  最終資産         : {final_cash:>12,.0f} 円")
    print(f"  総損益           : {total:>+12,.0f} 円")
    print(f"  リターン         : {ret:>+11.2f} %")
    print("-" * W)
    print(f"  総トレード数     : {n:>12} 回")
    print(f"  勝ち / 負け      : {len(wins):>5} 勝 / {len(losses)} 敗")
    print(f"  勝率             : {win_r:>11.1f} %")
    print(f"  平均利益         : {avg_w:>+12,.0f} 円")
    print(f"  平均損失         : {avg_l:>+12,.0f} 円")
    print(f"  ペイオフ比       : {payoff:>12.2f}")
    print(f"  最大ドローダウン : {max_dd:>+11.2f} %")
    print(sep)

    # トレード一覧
    print(f"\n{'#':>2}  {'エントリー':16} {'決済':16} {'買値':>7} {'売値':>7} {'損益':>9}  結果")
    print("-" * W)
    cumulative = 0.0
    for i, t in enumerate(trades, 1):
        cumulative += t["pnl"]
        mark = "○" if t["pnl"] > 0 else "●"
        note = " " + t.get("note", "")
        print(
            f"{i:>2}  "
            f"{t['entry_dt'].strftime('%H:%M'):>5}        "
            f"{t['exit_dt'].strftime('%H:%M'):>5}        "
            f"{t['entry_price']:>7.1f} "
            f"{t['exit_price']:>7.1f} "
            f"{t['pnl']:>+9,.0f}  {mark}{note}"
        )
    print("-" * W)
    print(f"  累積損益: {cumulative:>+,.0f} 円")

    # 累積損益テキストグラフ
    print("\n--- 累積損益チャート ---")
    cum = 0.0
    max_abs = max(abs(t["pnl"]) for t in trades) * n or 1
    for t in trades:
        cum += t["pnl"]
        w   = int(abs(cum) / max_abs * 40)
        bar = ("+" if cum >= 0 else "-") * w
        print(f"  {t['exit_dt'].strftime('%H:%M')}  {cum:>+9,.0f} 円  |{bar}")


# ── 6. エントリーポイント ────────────────────────────────────

if __name__ == "__main__":
    bars, source = load_bars()
    trades, final_cash = run_backtest(bars)
    report(trades, final_cash, source, len(bars))
