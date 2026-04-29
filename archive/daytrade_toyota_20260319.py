"""
デイトレ バックテスト — 2026-03-19 トヨタ自動車 (7203.T)
戦略: 移動平均クロス（短期MA / 長期MA）

【データソース優先順位】
  1. toyota_20260319.csv  が存在すれば実データとして読み込む
  2. 存在しなければ合成データを使用（シード固定で再現可能）

【CSV フォーマット（ヘッダー必須）】
  datetime,open,high,low,close,volume
  2026-03-19 09:00:00,3520,3528,3515,3522,85000
  ...

【なぜ yfinance が使えないか】
  この実行環境には egress プロキシが設定されており、
  接続可能なホストが JWT ホワイトリストで制限されています。
  finance.yahoo.com はそのリストに含まれないため 403 で遮断されます。
"""

import csv
import os
import random
from collections import deque
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL        = "7203.T"       # トヨタ自動車
TARGET_DATE   = "2026-03-19"
CSV_PATH      = "toyota_20260319.csv"

SHORT_PERIOD  = 5              # 短期 MA（足数）
LONG_PERIOD   = 25             # 長期 MA（足数）
BAR_SECONDS   = 60             # 足の時間幅（秒）: 1分足
QTY           = 100            # 1 回の取引株数
INITIAL_CASH  = 1_000_000      # 初期資金（円）


# ── 1. データ準備 ────────────────────────────────────────────

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
                "volume": int(float(row.get("volume", 0))),
            })
    return sorted(rows, key=lambda r: r["dt"])


def generate_synthetic_bars() -> list[dict]:
    """
    2026-03-19 トヨタ 1 分足を合成する。

    価格帯の根拠:
      - 2025 年末〜2026 年初めにかけてトヨタは 3,400〜3,700 円前後で推移。
      - 2026/03/19 はその中間あたり、始値 3,560 円と仮定。
      - 日中ボラティリティは 0.5〜0.8% 程度（大型株平均的水準）。

    東証セッション:
      前場: 09:00〜11:30 (150 分 → 150 本)
      後場: 12:30〜15:30 (180 分 → 180 本)
    """
    rng = random.Random(20260319)   # 再現性のためシード固定

    session_ranges = [
        (datetime(2026, 3, 19, 9,  0), datetime(2026, 3, 19, 11, 30)),
        (datetime(2026, 3, 19, 12, 30), datetime(2026, 3, 19, 15, 30)),
    ]

    price = 3_560.0   # 寄り付き想定価格
    bars  = []

    for (start, end) in session_ranges:
        dt = start
        while dt < end:
            # ランダムウォーク（σ ≈ 3 円/分）
            drift  = rng.gauss(0.02, 1.5)    # 微小上昇バイアス
            change = rng.gauss(drift, 3.0)

            # 寄り付き直後・大引け前はボラ高め
            if dt.hour == 9  and dt.minute < 15:
                change *= 2.0
            if dt.hour == 15 and dt.minute >= 15:
                change *= 1.5

            price = max(3_400.0, min(3_750.0, price + change))

            o = price + rng.gauss(0, 1.5)
            h = max(o, price) + abs(rng.gauss(0, 2))
            l = min(o, price) - abs(rng.gauss(0, 2))
            vol = int(rng.gauss(60_000, 15_000))

            bars.append({
                "dt":     dt,
                "open":   round(o, 1),
                "high":   round(h, 1),
                "low":    round(l, 1),
                "close":  round(price, 1),
                "volume": max(0, vol),
            })
            dt += timedelta(seconds=BAR_SECONDS)

    return bars


def load_bars() -> tuple[list[dict], str]:
    if os.path.exists(CSV_PATH):
        bars   = load_csv(CSV_PATH)
        source = f"実データ CSV ({CSV_PATH})"
    else:
        bars   = generate_synthetic_bars()
        source = "合成データ (seed=20260319, 1分足, 前後場 330 本)"
    return bars, source


# ── 2. MA クロス戦略 ─────────────────────────────────────────

class MACross:
    def __init__(self, short: int, long_: int):
        self.buf  = deque(maxlen=long_)
        self.short = short
        self.long_ = long_
        self.prev_signal: str | None = None

    def feed(self, price: float) -> str | None:
        """価格を1本追加し、クロス発生時に "buy"/"sell" を返す。"""
        self.buf.append(price)
        if len(self.buf) < self.long_:
            return None

        s_ma = sum(list(self.buf)[-self.short:]) / self.short
        l_ma = sum(self.buf) / self.long_
        sig  = "buy" if s_ma > l_ma else "sell"

        if sig != self.prev_signal:
            self.prev_signal = sig
            return sig
        return None


# ── 3. バックテスト ──────────────────────────────────────────

def run_backtest(bars: list[dict]) -> tuple[list[dict], float]:
    strat        = MACross(SHORT_PERIOD, LONG_PERIOD)
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
                cash       -= cost
                entry_price = price
                entry_dt    = dt

        elif signal == "sell" and entry_price is not None:
            cash += price * QTY
            trades.append({
                "entry_dt":    entry_dt,
                "exit_dt":     dt,
                "entry_price": entry_price,
                "exit_price":  price,
                "pnl":         (price - entry_price) * QTY,
                "cash":        cash,
            })
            entry_price = None

    # 引け強制決済
    if entry_price is not None:
        last_bar = bars[-1]
        price    = last_bar["close"]
        cash    += price * QTY
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     last_bar["dt"],
            "entry_price": entry_price,
            "exit_price":  price,
            "pnl":         (price - entry_price) * QTY,
            "cash":        cash,
            "note":        "引け強制決済",
        })

    return trades, cash


# ── 4. 結果表示 ──────────────────────────────────────────────

def report(trades: list[dict], final_cash: float, source: str, n_bars: int):
    W = 58
    print("=" * W)
    print(f"  デイトレ バックテスト  {TARGET_DATE}  {SYMBOL} (トヨタ)")
    print("=" * W)
    print(f"  データ  : {source}")
    print(f"  足      : {n_bars} 本  ({BAR_SECONDS} 秒足)")
    print(f"  MA      : 短期 {SHORT_PERIOD} / 長期 {LONG_PERIOD}")
    print(f"  取引株数: {QTY} 株/回")
    print("-" * W)

    if not trades:
        print("  トレードが発生しませんでした。")
        return

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n      = len(trades)
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate  = len(wins) / n * 100
    avg_win   = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    payoff    = abs(avg_win / avg_loss) if avg_loss else float("inf")
    ret_pct   = (final_cash - INITIAL_CASH) / INITIAL_CASH * 100

    # 最大ドローダウン
    vals  = [INITIAL_CASH] + [t["cash"] for t in trades]
    peak  = vals[0]
    max_dd = 0.0
    for v in vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    print(f"  初期資金         : {INITIAL_CASH:>12,.0f} 円")
    print(f"  最終資産         : {final_cash:>12,.0f} 円")
    print(f"  総損益           : {total_pnl:>+12,.0f} 円")
    print(f"  リターン         : {ret_pct:>+11.2f} %")
    print("-" * W)
    print(f"  総トレード数     : {n:>12} 回")
    print(f"  勝ち / 負け      : {len(wins):>5} 勝 / {len(losses)} 敗")
    print(f"  勝率             : {win_rate:>11.1f} %")
    print(f"  平均利益         : {avg_win:>+12,.0f} 円")
    print(f"  平均損失         : {avg_loss:>+12,.0f} 円")
    print(f"  ペイオフ比       : {payoff:>12.2f}")
    print(f"  最大ドローダウン : {max_dd:>+11.2f} %")
    print("=" * W)

    # トレード一覧
    print(f"\n{'No':>3}  {'エントリー':19} {'決済':19} {'買値':>7} {'売値':>7} {'損益':>9}  備考")
    print("-" * W)
    for i, t in enumerate(trades, 1):
        note = t.get("note", "")
        win  = "○" if t["pnl"] > 0 else "●"
        print(
            f"{i:>3}  "
            f"{str(t['entry_dt']):19} "
            f"{str(t['exit_dt']):19} "
            f"{t['entry_price']:>7.1f} "
            f"{t['exit_price']:>7.1f} "
            f"{t['pnl']:>+9,.0f} {win} {note}"
        )

    # 累積損益グラフ（簡易テキスト）
    print("\n--- 累積損益推移 ---")
    cumulative = 0.0
    bar_width  = 30
    for t in trades:
        cumulative += t["pnl"]
        filled = int(abs(cumulative) / max(abs(total_pnl), 1) * bar_width)
        bar    = ("+" if cumulative >= 0 else "-") * filled
        print(f"  {str(t['exit_dt'].strftime('%H:%M')):5}  {cumulative:>+9,.0f} 円  |{bar}")


# ── 5. エントリーポイント ────────────────────────────────────

if __name__ == "__main__":
    bars, source = load_bars()
    trades, final_cash = run_backtest(bars)
    report(trades, final_cash, source, len(bars))
