"""
analyze_nikkei_trend.py  ―  日経平均トレンド期間分析

MA10 / MA25 のクロスを使ってトレンド転換を検出し、
上昇・下落それぞれの期間の長さ・騰落率を集計する。

select_signals.py と同じ判定基準:
  上昇 = 終値 > MA10 かつ MA10 > MA25
  下落 = 終値 < MA10 かつ MA10 < MA25
  横ばい = それ以外

Usage:
    python analyze_nikkei_trend.py          # 過去5年
    python analyze_nikkei_trend.py --years 10
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

JST = timezone(timedelta(hours=9))


def fetch_n225(years: int) -> pd.Series:
    df = yf.download("^N225", period=f"{years * 365 + 30}d", interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError("日経データ取得失敗")
    close = df["Close"].squeeze()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.dropna().sort_index()


def label_trend(close: pd.Series) -> pd.Series:
    """各日のトレンドラベルを返す: 'up' / 'down' / 'sideways'"""
    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()
    trend = pd.Series("sideways", index=close.index)
    trend[(close > ma10) & (ma10 > ma25)] = "up"
    trend[(close < ma10) & (ma10 < ma25)] = "down"
    return trend


def extract_periods(close: pd.Series, trend: pd.Series) -> list[dict]:
    """連続するトレンド区間を抽出"""
    periods = []
    cur_trend = None
    start_idx = None

    for i in range(len(trend)):
        t = trend.iloc[i]
        if t != cur_trend:
            if cur_trend is not None and cur_trend != "sideways":
                end_idx = i - 1
                start_price = float(close.iloc[start_idx])
                end_price   = float(close.iloc[end_idx])
                start_date  = close.index[start_idx].date()
                end_date    = close.index[end_idx].date()
                days        = (end_date - start_date).days
                pct         = (end_price / start_price - 1) * 100
                periods.append({
                    "trend":       cur_trend,
                    "start":       start_date,
                    "end":         end_date,
                    "days":        days,
                    "pct":         pct,
                    "start_price": start_price,
                    "end_price":   end_price,
                })
            cur_trend  = t
            start_idx  = i

    # 最後の区間
    if cur_trend is not None and cur_trend != "sideways" and start_idx is not None:
        end_idx     = len(trend) - 1
        start_price = float(close.iloc[start_idx])
        end_price   = float(close.iloc[end_idx])
        start_date  = close.index[start_idx].date()
        end_date    = close.index[end_idx].date()
        days        = (end_date - start_date).days
        pct         = (end_price / start_price - 1) * 100
        periods.append({
            "trend": cur_trend, "start": start_date, "end": end_date,
            "days": days, "pct": pct,
            "start_price": start_price, "end_price": end_price,
        })

    return periods


def print_stats(label: str, periods: list[dict]) -> None:
    if not periods:
        print(f"  {label}: データなし")
        return
    days_list = [p["days"] for p in periods]
    pct_list  = [p["pct"]  for p in periods]
    avg_days  = sum(days_list) / len(days_list)
    med_days  = sorted(days_list)[len(days_list) // 2]
    max_days  = max(days_list)
    min_days  = min(days_list)
    avg_pct   = sum(pct_list) / len(pct_list)
    print(f"  {label}: {len(periods)}回")
    print(f"    期間: 平均 {avg_days:.0f}日 / 中央値 {med_days}日 / 最短 {min_days}日 / 最長 {max_days}日")
    print(f"    騰落: 平均 {avg_pct:+.1f}%")

    # 分布
    buckets = [(0,10),(10,20),(20,30),(30,60),(60,90),(90,180),(180,9999)]
    dist = []
    for lo, hi in buckets:
        cnt = sum(1 for d in days_list if lo <= d < hi)
        if cnt:
            lbl = f"{lo}〜{hi-1}日" if hi < 9999 else f"{lo}日以上"
            dist.append(f"{lbl}:{cnt}回")
    print(f"    分布: {' / '.join(dist)}")


def main():
    parser = argparse.ArgumentParser(description="日経平均トレンド期間分析")
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args()

    print(f"日経平均トレンド期間分析 (過去{args.years}年)", flush=True)
    close  = fetch_n225(args.years)
    trend  = label_trend(close)
    periods = extract_periods(close, trend)

    up_periods   = [p for p in periods if p["trend"] == "up"]
    down_periods = [p for p in periods if p["trend"] == "down"]

    W = 72
    print("=" * W)
    print(f"  分析期間: {close.index[0].date()} 〜 {close.index[-1].date()}  ({len(close)}営業日)")
    print(f"  現在の日経: {float(close.iloc[-1]):,.0f}円")
    print(f"  現在のトレンド: {trend.iloc[-1]}")
    print("=" * W)

    print()
    print("【統計サマリー】")
    print_stats("上昇トレンド", up_periods)
    print()
    print_stats("下落トレンド", down_periods)

    # ── 全期間リスト ──────────────────────────────────────────────────────────
    print()
    print("=" * W)
    print("【全トレンド期間一覧】")
    print("=" * W)
    header = f"  {'種別':6} {'開始':12} {'終了':12} {'日数':>6} {'騰落':>8} {'開始値':>9} {'終了値':>9}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for p in periods:
        mark = "▲" if p["trend"] == "up" else "▼"
        color_open  = "+" if p["pct"] >= 0 else ""
        print(f"  {mark} {p['trend']:4} {str(p['start']):12} {str(p['end']):12} "
              f"{p['days']:>6}日 {color_open}{p['pct']:>6.1f}% "
              f"{p['start_price']:>9,.0f} {p['end_price']:>9,.0f}")

    # ── 現在のトレンド継続日数 ────────────────────────────────────────────────
    if periods:
        last = periods[-1]
        print()
        print("=" * W)
        trend_ja = "上昇" if last["trend"] == "up" else ("下落" if last["trend"] == "down" else "横ばい")
        print(f"【現在のトレンド状況】")
        print(f"  トレンド  : {trend_ja}")
        print(f"  開始日    : {last['start']} ({last['days']}日継続中)")
        print(f"  開始値    : {last['start_price']:,.0f}円 → 現在 {float(close.iloc[-1]):,.0f}円 "
              f"({last['pct']:+.1f}%)")

        if last["trend"] == "up":
            avg = sum(p["days"] for p in up_periods) / len(up_periods) if up_periods else 0
            med = sorted(p["days"] for p in up_periods)[len(up_periods)//2] if up_periods else 0
            print(f"  上昇の平均期間: {avg:.0f}日 / 中央値: {med}日")
            remaining = med - last["days"]
            if remaining > 0:
                print(f"  中央値まであと: {remaining}日（参考値・保証なし）")
            else:
                print(f"  中央値({med}日)をすでに超過 → 転換注意（参考値・保証なし）")
        elif last["trend"] == "down":
            avg = sum(p["days"] for p in down_periods) / len(down_periods) if down_periods else 0
            med = sorted(p["days"] for p in down_periods)[len(down_periods)//2] if down_periods else 0
            print(f"  下落の平均期間: {avg:.0f}日 / 中央値: {med}日")
            remaining = med - last["days"]
            if remaining > 0:
                print(f"  中央値まであと: {remaining}日（参考値・保証なし）")
            else:
                print(f"  中央値({med}日)をすでに超過 → 反転の可能性（参考値・保証なし）")

    print()
    print("※ トレンド判定: 終値>MA10>MA25=上昇 / 終値<MA10<MA25=下落 / それ以外=横ばい")
    print("※ 過去の統計は将来を保証しません")


if __name__ == "__main__":
    main()
