"""
デイトレ バックテスト — 2025-03-19 KDDI (9433)
戦略: 移動平均クロス（kabu_trade_bot.py と同一ロジック）

データソース優先順位:
  1. CSV ファイル（kabu_data_20250319.csv）が存在すれば読み込む
  2. なければ合成データ（実際の相場感に基づいた乱数シミュレーション）を使用

CSV フォーマット（1行目ヘッダー）:
  datetime,open,high,low,close,volume
  2025-03-19 09:00:00,4920,4925,4915,4918,12000
  ...
"""

import csv
import math
import os
import random
from datetime import datetime, timedelta

# ---- 設定（kabu_trade_bot.py と同じパラメータ） ----
SYMBOL        = "9433"       # KDDI
SHORT_PERIOD  = 5            # 短期 MA（足数）
LONG_PERIOD   = 25           # 長期 MA（足数）
QTY           = 100          # 1 回の取引株数
INITIAL_CASH  = 1_000_000   # 初期資金（円）
POLL_SECONDS  = 10           # ボット取得間隔（秒）→ 10 秒足として再現

TARGET_DATE   = "2025-03-19"
CSV_PATH      = "kabu_data_20250319.csv"


# ────────────────────────────────────────────────
# 1. データ準備
# ────────────────────────────────────────────────

def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "dt":    datetime.fromisoformat(row["datetime"]),
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            })
    return rows


def generate_synthetic_data() -> list[dict]:
    """
    2025-03-19 KDDI の合成 10 秒足データを生成する。
    実際の東証セッション: 09:00〜11:30, 12:30〜15:30
    KDDI はこの時期 4,900〜5,050 円 付近で推移していた想定。
    """
    rng = random.Random(20250319)  # 再現性のためシードを固定

    base  = datetime(2025, 3, 19, 9, 0, 0)
    price = 4_930.0   # 始値
    tick  = 10        # 秒

    # 午前セッション: 09:00-11:30 = 150 分 = 900 足
    # 午後セッション: 12:30-15:30 = 180 分 = 1080 足
    am_start = base
    am_end   = datetime(2025, 3, 19, 11, 30, 0)
    pm_start = datetime(2025, 3, 19, 12, 30, 0)
    pm_end   = datetime(2025, 3, 19, 15, 30, 0)

    bars = []
    dt = am_start
    while dt < am_end or (pm_start <= dt < pm_end):
        # ランダムウォーク（ドリフト微小、σ ≈ 5 円/足）
        change = rng.gauss(0, 5)
        # 9:00 前後の寄り付きラッシュでボラ高め
        if dt.hour == 9 and dt.minute < 10:
            change *= 2.5
        # 引け前 10 分も少し動く
        if dt.hour == 15 and dt.minute >= 20:
            change *= 1.5

        price = max(4_800, min(5_100, price + change))

        high  = price + abs(rng.gauss(0, 3))
        low   = price - abs(rng.gauss(0, 3))

        bars.append({
            "dt":    dt,
            "open":  round(price - rng.gauss(0, 2), 1),
            "high":  round(high, 1),
            "low":   round(low, 1),
            "close": round(price, 1),
        })

        dt += timedelta(seconds=tick)
        # 昼休み跨ぎ
        if am_start <= dt <= am_end and dt >= am_end:
            dt = pm_start

    return bars


def load_bars() -> tuple[list[dict], str]:
    if os.path.exists(CSV_PATH):
        bars = load_csv(CSV_PATH)
        source = f"CSV ({CSV_PATH})"
    else:
        bars = generate_synthetic_data()
        source = "合成データ（シード: 20250319）"
    return bars, source


# ────────────────────────────────────────────────
# 2. バックテスト本体
# ────────────────────────────────────────────────

class MACrossSimulator:
    """kabu_trade_bot.py の MACrossStrategy をオフライン再現"""

    def __init__(self, short: int, long_: int):
        self.short = short
        self.long_ = long_
        self.prices: list[float] = []
        self.prev_signal: str | None = None

    def feed(self, price: float) -> str | None:
        self.prices.append(price)
        if len(self.prices) < self.long_:
            return None

        short_ma = sum(self.prices[-self.short:]) / self.short
        long_ma  = sum(self.prices[-self.long_:]) / self.long_

        signal = "buy" if short_ma > long_ma else "sell"
        if signal != self.prev_signal:
            self.prev_signal = signal
            return signal
        return None


def run_backtest(bars: list[dict]) -> list[dict]:
    strat = MACrossSimulator(SHORT_PERIOD, LONG_PERIOD)

    cash         = float(INITIAL_CASH)
    entry_price  = None
    entry_dt     = None
    trades       = []

    for bar in bars:
        price  = bar["close"]
        dt     = bar["dt"]
        signal = strat.feed(price)

        if signal == "buy" and entry_price is None:
            cost = price * QTY
            if cash >= cost:
                cash        -= cost
                entry_price  = price
                entry_dt     = dt

        elif signal == "sell" and entry_price is not None:
            proceeds = price * QTY
            cash    += proceeds
            pnl      = (price - entry_price) * QTY
            trades.append({
                "entry_dt":    entry_dt,
                "exit_dt":     dt,
                "entry_price": entry_price,
                "exit_price":  price,
                "pnl":         pnl,
                "cash":        cash,
            })
            entry_price = None
            entry_dt    = None

    # 引け強制決済
    if entry_price is not None:
        last  = bars[-1]
        price = last["close"]
        dt    = last["dt"]
        proceeds = price * QTY
        cash    += proceeds
        pnl      = (price - entry_price) * QTY
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     dt,
            "entry_price": entry_price,
            "exit_price":  price,
            "pnl":         pnl,
            "cash":        cash,
            "note":        "引け強制決済",
        })

    return trades, cash


# ────────────────────────────────────────────────
# 3. 結果表示
# ────────────────────────────────────────────────

def report(trades: list[dict], final_cash: float, source: str, n_bars: int) -> None:
    W  = 54
    print("=" * W)
    print(f"  デイトレ バックテスト  {TARGET_DATE}  {SYMBOL} (KDDI)")
    print("=" * W)
    print(f"  データソース : {source}")
    print(f"  足数         : {n_bars:,} 本 ({POLL_SECONDS} 秒足)")
    print(f"  MA           : 短期 {SHORT_PERIOD} / 長期 {LONG_PERIOD}")
    print("-" * W)

    if not trades:
        print("  トレードが発生しませんでした。")
        return

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl   = sum(t["pnl"] for t in trades)
    win_rate    = len(wins) / len(trades) * 100
    avg_win     = sum(t["pnl"] for t in wins)  / len(wins)  if wins   else 0
    avg_loss    = sum(t["pnl"] for t in losses)/ len(losses) if losses else 0
    payoff      = abs(avg_win / avg_loss) if avg_loss else float("inf")
    ret_pct     = (final_cash - INITIAL_CASH) / INITIAL_CASH * 100

    # 最大ドローダウン
    cumulative = [INITIAL_CASH] + [t["cash"] for t in trades]
    peak = cumulative[0]
    max_dd_pct = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100
        if dd < max_dd_pct:
            max_dd_pct = dd

    print(f"  初期資金       : {INITIAL_CASH:>12,.0f} 円")
    print(f"  最終資産       : {final_cash:>12,.0f} 円")
    print(f"  総損益         : {total_pnl:>+12,.0f} 円")
    print(f"  リターン       : {ret_pct:>+11.2f} %")
    print("-" * W)
    print(f"  総トレード数   : {len(trades):>12} 回")
    print(f"  勝ち           : {len(wins):>12} 回")
    print(f"  負け           : {len(losses):>12} 回")
    print(f"  勝率           : {win_rate:>11.1f} %")
    print(f"  平均利益       : {avg_win:>+12,.0f} 円")
    print(f"  平均損失       : {avg_loss:>+12,.0f} 円")
    print(f"  ペイオフ比     : {payoff:>12.2f}")
    print(f"  最大ドローダウン: {max_dd_pct:>+10.2f} %")
    print("=" * W)

    print("\n--- トレード一覧 ---")
    header = f"{'エントリー時刻':20} {'決済時刻':20} {'買値':>8} {'売値':>8} {'損益':>10}"
    print(header)
    print("-" * len(header))
    for t in trades:
        note = t.get("note", "")
        row = (
            f"{str(t['entry_dt']):20} {str(t['exit_dt']):20} "
            f"{t['entry_price']:>8.1f} {t['exit_price']:>8.1f} "
            f"{t['pnl']:>+10,.0f} {note}"
        )
        print(row)


# ────────────────────────────────────────────────
# 4. エントリーポイント
# ────────────────────────────────────────────────

if __name__ == "__main__":
    bars, source = load_bars()
    trades, final_cash = run_backtest(bars)
    report(trades, final_cash, source, len(bars))
