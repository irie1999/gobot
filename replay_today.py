"""
replay_today.py  ―  今日1日分のORBリプレイ
==================================================================
監視中の30銘柄について、今日の5分足データでORB戦略をリプレイし、
「もし市場時間中に動かしていたら」の結果を表示する。

【使い方】
  python replay_today.py                  # 今日
  python replay_today.py --date 2026-04-11   # 指定日
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))

# .env
for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        break

from kabu_daytrade_bot import WATCH_SYMBOLS
from daytrade_data import calc_position_size

OR_MINUTES = 30
TARGET_K = 1.5
GAP_MAX_PCT = 2.0
TRAILING_TRIGGER = 0.5
FORCE_CLOSE = dtime(14, 55)
ENTRY_CUTOFF = dtime(11, 0)
AM_START = dtime(9, 0)
BUDGET = 600_000
MAX_RISK = 6_000


def get_client():
    import jquantsapi
    return jquantsapi.ClientV2(api_key=os.environ["JQUANTS_API_KEY"])


def fetch_day(cli, code5: str, date: str):
    """指定日の5分足を取得。"""
    d = date.replace("-", "")
    try:
        df = cli.get_eq_bars_5minute(
            code=code5, from_yyyymmdd=d, to_yyyymmdd=d
        )
    except Exception as e:
        return None
    if df is None or df.empty:
        return None
    if "Code" in df.columns:
        df = df[df["Code"].astype(str) == code5]
    if df.empty:
        return None

    # Date+Time → DateTime
    df["DateTime"] = pd.to_datetime(
        df["Date"].astype(str) + " " + df["Time"].astype(str),
        format="%Y-%m-%d %H:%M"
    )
    df = df.sort_values("DateTime").reset_index(drop=True)
    df = df.rename(columns={
        "O": "open", "H": "high", "L": "low", "C": "close", "Vo": "volume"
    })
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("DateTime")
    return df


def fetch_prev_close(cli, code5: str, date: str) -> float | None:
    """前日終値を取得 (日足から)。"""
    try:
        from dateutil import tz as dtz
        dt = datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=dtz.gettz("Asia/Tokyo"))
        df = cli.get_eq_bars_daily_range(
            start_dt=dt - timedelta(days=10), end_dt=dt - timedelta(days=1)
        )
        if df is None or df.empty:
            return None
        if "Code" in df.columns:
            df = df[df["Code"].astype(str) == code5]
        if df.empty:
            return None
        cl_col = "AdjustmentClose" if "AdjustmentClose" in df.columns else "Close"
        return float(df.iloc[-1][cl_col])
    except Exception:
        return None


def replay_orb(df: pd.DataFrame, prev_close: float | None = None):
    """1日分のORBリプレイ。詳細なイベントログ付き。"""
    events = []
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=OR_MINUTES)).time()

    or_bars = df[df.index.time < or_end]
    rest = df[df.index.time >= or_end]
    if len(or_bars) < 2 or rest.empty:
        return None, ["データ不足"]

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w = or_hi - or_lo
    events.append(f"09:30 OR確定: 高値={or_hi:,.0f} 安値={or_lo:,.0f} 幅={or_w:,.0f}")

    if or_w <= 0:
        return None, events + ["OR幅=0"]

    open_p = float(or_bars.iloc[0]["open"])
    if prev_close and prev_close > 0:
        gap = abs(open_p - prev_close) / prev_close * 100
        events.append(f"前日終値={prev_close:,.0f} ギャップ={gap:+.2f}%")
        if gap > GAP_MAX_PCT:
            return None, events + [f"ギャップ{gap:.1f}%>{GAP_MAX_PCT}% → 見送り"]

    state = "idle"
    entry_p = target_p = 0.0
    stop_p = or_lo
    qty = 0
    trailing = False
    exit_p = None
    reason = None
    entry_dt = exit_dt = None

    bars = list(rest.itertuples())
    for i, bar in enumerate(bars):
        ts = bar.Index
        t = ts.time()
        hi, lo, cl = float(bar.high), float(bar.low), float(bar.close)

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p, exit_dt, reason = cl, ts, "引け強制"
                break
            if not trailing and target_p > entry_p:
                if (cl - entry_p) / (target_p - entry_p) >= TRAILING_TRIGGER:
                    stop_p = entry_p
                    trailing = True
                    events.append(f"{ts.strftime('%H:%M')} トレーリング発動 → 建値撤退")
            if lo <= stop_p and hi >= target_p:
                exit_p, exit_dt = stop_p, ts
                reason = "建値撤退" if trailing else "損切り"
                break
            if hi >= target_p:
                exit_p, exit_dt, reason = target_p, ts, "目標達成"
                break
            if lo <= stop_p:
                exit_p, exit_dt = stop_p, ts
                reason = "建値撤退" if trailing else "損切り"
                break
            continue

        if t >= ENTRY_CUTOFF:
            events.append(f"{ts.strftime('%H:%M')} エントリー締切 (シグナルなし)")
            break

        if cl > or_hi and i + 1 < len(bars):
            entry_p = float(bars[i + 1].open)
            entry_dt = bars[i + 1].Index
            target_p = entry_p + or_w * TARGET_K
            if entry_p <= stop_p:
                continue
            qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)
            state = "in_pos"
            trailing = False
            events.append(
                f"{entry_dt.strftime('%H:%M')} ★ ORBシグナル! "
                f"買値={entry_p:,.0f} 損切={stop_p:,.0f} "
                f"目標={target_p:,.0f} 株数={qty}"
            )

    if state == "in_pos" and exit_p is None:
        exit_p = float(bars[-1].close)
        exit_dt = bars[-1].Index
        reason = "引け強制"

    if exit_p is None or entry_dt is None:
        return None, events

    pnl = (exit_p - entry_p) * qty
    events.append(
        f"{exit_dt.strftime('%H:%M')} ★ 決済 [{reason}] "
        f"{entry_p:,.0f}→{exit_p:,.0f} {pnl:+,.0f}円"
    )
    return {
        "entry_p": entry_p, "exit_p": exit_p, "qty": qty, "pnl": pnl,
        "reason": reason, "entry_dt": entry_dt, "exit_dt": exit_dt,
        "or_hi": or_hi, "or_lo": or_lo,
    }, events


def main():
    parser = argparse.ArgumentParser(description="今日のORBリプレイ")
    parser.add_argument("--date", default=None, help="日付 (YYYY-MM-DD)")
    args = parser.parse_args()

    date = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    cli = get_client()

    print(f"ORBリプレイ: {date}  {len(WATCH_SYMBOLS)}銘柄")
    print("=" * 70)

    first_signal: tuple | None = None  # (priority, sym, name, events, trade)

    for i, (code4, name) in enumerate(WATCH_SYMBOLS, 1):
        code5 = code4 + "0"
        df = fetch_day(cli, code5, date)
        if df is None or df.empty:
            continue

        prev_close = fetch_prev_close(cli, code5, date)
        trade, events = replay_orb(df, prev_close)

        # シグナル時刻を取得
        sig_time = None
        if trade:
            sig_time = trade["entry_dt"]

        print(f"\n[{i:>2}/{len(WATCH_SYMBOLS)}] {name} ({code4})")
        for ev in events:
            print(f"    {ev}")

        if trade and sig_time and first_signal is None:
            first_signal = (sig_time, code4, name, trade, events)
        elif trade and sig_time and first_signal:
            if sig_time < first_signal[0]:
                first_signal = (sig_time, code4, name, trade, events)

    # 最初にシグナルが出た銘柄 (実運用ではこれを取引)
    print()
    print("=" * 70)
    if first_signal:
        _, code, name, trade, _ = first_signal
        mark = "○" if trade["pnl"] > 0 else "●"
        print(f"  【実運用想定】最初にシグナル発生した1銘柄で取引:")
        print(f"  {mark} {name} ({code})")
        print(f"    {trade['entry_dt'].strftime('%H:%M')} 買値 {trade['entry_p']:,.0f} "
              f"× {trade['qty']}株")
        print(f"    {trade['exit_dt'].strftime('%H:%M')} 決済 {trade['exit_p']:,.0f} "
              f"({trade['reason']})")
        print(f"    損益: {trade['pnl']:+,.0f}円")
    else:
        print("  【実運用想定】シグナル発生なし → トレードなし")
    print("=" * 70)


if __name__ == "__main__":
    main()
