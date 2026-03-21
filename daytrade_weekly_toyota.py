"""
週次デイトレ バックテスト — 2026/03/16 週（月〜金）
銘柄  : トヨタ自動車 (7203.T)
データ: yfinance 1 分足（ネット遮断時は合成データで自動フォールバック）
戦略  : 移動平均クロス（短期 MA / 長期 MA）

ルール:
  - 毎日独立してポジションを持つ（持越しなし）
  - 引けまでに未決済なら強制決済
  - 初期資金は毎日リセットせず、前日の最終資産を翌日に引き継ぐ
"""

import random
from collections import deque
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL       = "7203.T"
WEEK_START   = "2026-03-16"   # 月曜日
INTERVAL     = "1m"

SHORT_PERIOD = 5
LONG_PERIOD  = 25
QTY          = 100            # 1 回の取引株数
INITIAL_CASH = 1_000_000      # 週初めの資金（円）

# 東証セッション（前場・後場）
SESSIONS = [
    ("09:00", "11:30"),
    ("12:30", "15:30"),
]

# 分析対象週（月〜金）
def trading_days(week_start: str) -> list[str]:
    base = datetime.strptime(week_start, "%Y-%m-%d")
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]


# ── 1. yfinance でデータ取得 ─────────────────────────────────

def fetch_week_yfinance(dates: list[str]) -> dict[str, list[dict]]:
    """
    週全体を 1 度のダウンロードで取得し、日付ごとに分割して返す。
    失敗時は RuntimeError を送出。
    """
    import yfinance as yf
    import pandas as pd

    start = dates[0]
    end   = (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"[yfinance] {SYMBOL}  {start} 〜 {end}  interval={INTERVAL} を取得中...")
    df = yf.download(SYMBOL, start=start, end=end, interval=INTERVAL,
                     auto_adjust=True, progress=False)

    if df.empty:
        raise RuntimeError("yfinance: データが空でした")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)

    result: dict[str, list[dict]] = {}
    for date_str in dates:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        sub = df[df.index.date == day]
        if sub.empty:
            continue
        result[date_str] = [
            {
                "dt":     row.Index.to_pydatetime(),
                "open":   float(row.Open),
                "high":   float(row.High),
                "low":    float(row.Low),
                "close":  float(row.Close),
                "volume": int(row.Volume),
            }
            for row in sub.itertuples()
        ]

    if not result:
        raise RuntimeError("yfinance: 対象週にデータがありませんでした")

    total = sum(len(v) for v in result.values())
    print(f"[yfinance] 取得成功: {len(result)} 日 / {total} 本\n")
    return result


# ── 2. 合成データ（フォールバック） ──────────────────────────

def generate_day_bars(date_str: str, open_price: float, seed: int) -> list[dict]:
    """指定日の 1 分足合成データを生成する。"""
    rng = random.Random(seed)
    year, month, day = map(int, date_str.split("-"))
    sessions = [
        (datetime(year, month, day,  9,  0), datetime(year, month, day, 11, 30)),
        (datetime(year, month, day, 12, 30), datetime(year, month, day, 15, 30)),
    ]
    price = open_price
    bars  = []
    for (start, end) in sessions:
        dt = start
        while dt < end:
            change = rng.gauss(0.02, 3.0)
            if dt.hour == 9  and dt.minute < 15: change *= 2.0
            if dt.hour == 15 and dt.minute >= 15: change *= 1.5
            price = max(3_300.0, min(3_900.0, price + change))
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


def generate_week_synthetic(dates: list[str]) -> dict[str, list[dict]]:
    """週全体の合成データを生成する。前日終値を翌日の始値に引き継ぐ。"""
    result = {}
    price  = 3_560.0   # 週初め始値
    for i, d in enumerate(dates):
        seed = int(d.replace("-", "")) + i
        bars = generate_day_bars(d, price, seed)
        if bars:
            price = bars[-1]["close"]   # 翌日の始値 = 当日終値
            result[d] = bars
    return result


def load_week_bars(dates: list[str]) -> tuple[dict[str, list[dict]], str]:
    try:
        data   = fetch_week_yfinance(dates)
        source = f"yfinance ({SYMBOL} {INTERVAL})"
    except Exception as e:
        print(f"[警告] yfinance 取得失敗: {e}")
        print("[フォールバック] 合成データを使用します\n")
        data   = generate_week_synthetic(dates)
        source = "合成データ (seed 固定, 1 分足)"
    return data, source


# ── 3. MA クロス戦略 ─────────────────────────────────────────

class MACross:
    def __init__(self, short: int, long_: int):
        self.buf  = deque(maxlen=long_)
        self.short = short
        self.long_ = long_
        self.prev: str | None = None

    def reset(self):
        """1 日の終わりにリセット（翌日は独立した判断）"""
        self.buf.clear()
        self.prev = None

    def feed(self, price: float) -> str | None:
        self.buf.append(price)
        if len(self.buf) < self.long_:
            return None
        s   = sum(list(self.buf)[-self.short:]) / self.short
        l   = sum(self.buf) / self.long_
        sig = "buy" if s > l else "sell"
        if sig != self.prev:
            self.prev = sig
            return sig
        return None


# ── 4. 1 日分バックテスト ────────────────────────────────────

def backtest_day(bars: list[dict], cash: float) -> tuple[list[dict], float]:
    strat   = MACross(SHORT_PERIOD, LONG_PERIOD)
    entry_p = None
    entry_dt = None
    trades  = []

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

DAY_JP = ["月", "火", "水", "木", "金"]

def report(week_result: dict, source: str):
    W = 62
    print("=" * W)
    print(f"  週次デイトレ バックテスト  {WEEK_START} 週  {SYMBOL} (トヨタ)")
    print("=" * W)
    print(f"  データ  : {source}")
    print(f"  MA      : 短期 {SHORT_PERIOD} / 長期 {LONG_PERIOD}  /  取引株数 {QTY} 株")
    print(f"  初期資金: {INITIAL_CASH:,.0f} 円")
    print("=" * W)

    all_trades = []
    prev_cash  = INITIAL_CASH

    for i, (date_str, info) in enumerate(week_result.items()):
        dow    = DAY_JP[i]
        trades = info["trades"]
        cash   = info["final_cash"]
        n_bars = info["n_bars"]
        day_pnl = cash - prev_cash

        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_r  = len(wins) / len(trades) * 100 if trades else 0.0

        sign = "+" if day_pnl >= 0 else ""
        print(f"\n【{dow}】{date_str}  ({n_bars} 本)  "
              f"トレード {len(trades)} 回  勝率 {win_r:.0f}%")
        print(f"  資産: {prev_cash:>10,.0f} 円 → {cash:>10,.0f} 円  "
              f"({sign}{day_pnl:,.0f} 円)")

        if trades:
            print(f"  {'エントリー':5} {'決済':5}  {'買値':>7} {'売値':>7}  {'損益':>9}  結果")
            print(f"  " + "-" * 50)
            for t in trades:
                mark = "○" if t["pnl"] > 0 else "●"
                note = " " + t.get("note", "")
                print(
                    f"  {t['entry_dt'].strftime('%H:%M')}  "
                    f"{t['exit_dt'].strftime('%H:%M')}  "
                    f"{t['entry_price']:>7.1f} "
                    f"{t['exit_price']:>7.1f}  "
                    f"{t['pnl']:>+9,.0f}  {mark}{note}"
                )
        else:
            print("  （トレードなし）")

        all_trades.extend(trades)
        prev_cash = cash

    # ── 週間サマリー ─────────────────────────────────────────
    final   = prev_cash
    total_p = final - INITIAL_CASH
    ret_pct = total_p / INITIAL_CASH * 100

    all_wins   = [t for t in all_trades if t["pnl"] > 0]
    all_losses = [t for t in all_trades if t["pnl"] <= 0]
    n_all      = len(all_trades)
    win_r_all  = len(all_wins) / n_all * 100 if all_trades else 0.0
    avg_w      = sum(t["pnl"] for t in all_wins)   / len(all_wins)   if all_wins   else 0.0
    avg_l      = sum(t["pnl"] for t in all_losses) / len(all_losses) if all_losses else 0.0
    payoff     = abs(avg_w / avg_l) if avg_l else float("inf")

    # 最大ドローダウン（資産推移ベース）
    asset_vals = [INITIAL_CASH]
    for t in all_trades:
        asset_vals.append(t["cash"])
    peak = asset_vals[0]; max_dd = 0.0
    for v in asset_vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    print("\n" + "=" * W)
    print("  【週間サマリー】")
    print("=" * W)
    print(f"  初期資金         : {INITIAL_CASH:>12,.0f} 円")
    print(f"  最終資産         : {final:>12,.0f} 円")
    print(f"  週間損益         : {total_p:>+12,.0f} 円")
    print(f"  週間リターン     : {ret_pct:>+11.2f} %")
    print("-" * W)
    print(f"  総トレード数     : {n_all:>12} 回")
    print(f"  勝ち / 負け      : {len(all_wins):>5} 勝 / {len(all_losses)} 敗")
    print(f"  週間勝率         : {win_r_all:>11.1f} %")
    print(f"  平均利益         : {avg_w:>+12,.0f} 円")
    print(f"  平均損失         : {avg_l:>+12,.0f} 円")
    print(f"  ペイオフ比       : {payoff:>12.2f}")
    print(f"  最大ドローダウン : {max_dd:>+11.2f} %")
    print("=" * W)

    # 日別損益バーチャート
    print("\n--- 日別損益チャート ---")
    day_pnls = [info["final_cash"] - (INITIAL_CASH if i == 0 else
                list(week_result.values())[i-1]["final_cash"])
                for i, info in enumerate(week_result.values())]
    max_abs  = max(abs(p) for p in day_pnls) or 1
    for i, (date_str, pnl) in enumerate(zip(week_result.keys(), day_pnls)):
        w    = int(abs(pnl) / max_abs * 30)
        bar  = ("+" if pnl >= 0 else "-") * w
        sign = "+" if pnl >= 0 else ""
        print(f"  {DAY_JP[i]} {date_str}  {sign}{pnl:>7,.0f} 円  |{bar}")


# ── 6. エントリーポイント ────────────────────────────────────

if __name__ == "__main__":
    dates       = trading_days(WEEK_START)
    week_bars, source = load_week_bars(dates)

    week_result: dict[str, dict] = {}
    cash = float(INITIAL_CASH)

    for date_str in dates:
        if date_str not in week_bars:
            print(f"[スキップ] {date_str}: データなし（祝日等）")
            continue
        bars           = week_bars[date_str]
        trades, cash   = backtest_day(bars, cash)
        week_result[date_str] = {
            "trades":     trades,
            "final_cash": cash,
            "n_bars":     len(bars),
        }

    report(week_result, source)
