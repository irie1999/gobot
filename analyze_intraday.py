"""
analyze_intraday.py  ―  デイトレード戦略設計のための分日中相場分析
=================================================================
yfinance の 5 分足（直近 60 日まで）と 1 分足（直近 7 日まで）を
取得し、デイトレード戦略の設計に役立つ統計を出力する。

【出力する指標】
  1. 日中値幅 (HL range / ATR / 値幅%)
  2. オープニングレンジ (9:00-9:15 / 9:00-9:30 / 9:00-10:00) の幅と
     その後のブレイク方向別の継続率
  3. 寄付 → 引け 方向性 (ギャップアップ/ダウン後の傾向)
  4. 時間帯別ボラティリティ (寄付 / 前場後半 / 後場前半 / 大引け)
  5. 出来高分布 (寄付出来高比率、前場/後場比率)
  6. VWAP からの平均乖離・最大乖離
  7. 連続上昇/下降バー数 (モメンタム継続性)

【使い方】
  python analyze_intraday.py                       # 標準銘柄リスト・5分足
  python analyze_intraday.py --interval 1m         # 1分足
  python analyze_intraday.py --days 30             # 30日間
  python analyze_intraday.py 7203.T 9984.T         # 個別指定
  python analyze_intraday.py --csv out.csv         # CSV保存
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

JST = timezone(timedelta(hours=9))

# ── デフォルト分析対象（流動性高め・デイトレ向き）──
DEFAULT_SYMBOLS = [
    ("7203.T", "トヨタ自動車"),
    ("9984.T", "ソフトバンクG"),
    ("8306.T", "三菱UFJ"),
    ("6758.T", "ソニーG"),
    ("9432.T", "NTT"),
    ("6098.T", "リクルートHD"),
    ("8035.T", "東京エレクトロン"),
    ("6861.T", "キーエンス"),
]

# 立会時間 (JST) — 前場 9:00-11:30 / 後場 12:30-15:00
AM_START = dtime(9, 0)
AM_END   = dtime(11, 30)
PM_START = dtime(12, 30)
PM_END   = dtime(15, 0)


# ─────────────────────────────────────────────────────────────
# データ取得
# ─────────────────────────────────────────────────────────────

def fetch_intraday(symbol: str, days: int, interval: str) -> pd.DataFrame | None:
    """yfinance で 5m / 1m の OHLCV を取得し、JST に揃えた DataFrame を返す。"""
    try:
        df = yf.download(
            symbol,
            period=f"{days}d",
            interval=interval,
            auto_adjust=False,
            progress=False,
        )
    except Exception as e:
        print(f"  [warn] {symbol}: download error: {e}", file=sys.stderr)
        return None

    if df is None or df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]

    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)

    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[cols].dropna(subset=["close"])
    if df.empty:
        return None
    return df


def split_by_day(df: pd.DataFrame) -> dict[pd.Timestamp, pd.DataFrame]:
    """日付ごとに DataFrame を分割。"""
    result: dict[pd.Timestamp, pd.DataFrame] = {}
    for date in sorted(set(df.index.date)):
        sub = df[df.index.date == date]
        if not sub.empty:
            result[pd.Timestamp(date)] = sub
    return result


# ─────────────────────────────────────────────────────────────
# 各種統計
# ─────────────────────────────────────────────────────────────

def calc_day_range_stats(daily_groups: dict) -> dict:
    """日中値幅 (HL range) と %幅 の統計。"""
    ranges_pct = []
    for _, day_df in daily_groups.items():
        hi = float(day_df["high"].max())
        lo = float(day_df["low"].min())
        op = float(day_df.iloc[0]["open"])
        if op > 0 and hi > lo:
            ranges_pct.append((hi - lo) / op * 100)
    if not ranges_pct:
        return {}
    arr = np.array(ranges_pct)
    return {
        "n_days":      len(arr),
        "avg_range_%": float(np.mean(arr)),
        "med_range_%": float(np.median(arr)),
        "p25_range_%": float(np.percentile(arr, 25)),
        "p75_range_%": float(np.percentile(arr, 75)),
        "max_range_%": float(np.max(arr)),
    }


def calc_or_breakout_stats(daily_groups: dict, or_minutes: int = 30) -> dict:
    """
    オープニングレンジ (9:00 〜 9:00+or_minutes) の幅と
    その後の高値ブレイク → 引けまでの継続率を計算。
    """
    or_widths = []
    breakout_up_continued = 0
    breakout_dn_continued = 0
    breakout_up_total = 0
    breakout_dn_total = 0

    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=or_minutes)).time()

    for _, day_df in daily_groups.items():
        or_bars = day_df[day_df.index.time < or_end]
        rest    = day_df[day_df.index.time >= or_end]
        if or_bars.empty or rest.empty:
            continue
        or_hi = float(or_bars["high"].max())
        or_lo = float(or_bars["low"].min())
        op    = float(or_bars.iloc[0]["open"])
        if op <= 0 or or_hi <= or_lo:
            continue
        or_widths.append((or_hi - or_lo) / op * 100)

        rest_hi = float(rest["high"].max())
        rest_lo = float(rest["low"].min())
        cl      = float(rest.iloc[-1]["close"])

        # 上抜けブレイク → 引けが OR上限以上で「継続」とみなす
        if rest_hi > or_hi:
            breakout_up_total += 1
            if cl > or_hi:
                breakout_up_continued += 1
        # 下抜けブレイク
        if rest_lo < or_lo:
            breakout_dn_total += 1
            if cl < or_lo:
                breakout_dn_continued += 1

    if not or_widths:
        return {}
    arr = np.array(or_widths)
    return {
        "or_window":          f"9:00-{or_end.strftime('%H:%M')}",
        "n_days":             len(arr),
        "avg_or_width_%":     float(np.mean(arr)),
        "med_or_width_%":     float(np.median(arr)),
        "up_breakout_count":  breakout_up_total,
        "up_continue_rate":   (breakout_up_continued / breakout_up_total * 100
                                if breakout_up_total else 0),
        "dn_breakout_count":  breakout_dn_total,
        "dn_continue_rate":   (breakout_dn_continued / breakout_dn_total * 100
                                if breakout_dn_total else 0),
    }


def calc_open_to_close_stats(daily_groups: dict) -> dict:
    """寄付 → 引け の方向性。陽線/陰線/平均%。"""
    rets = []
    bull = 0
    for _, day_df in daily_groups.items():
        op = float(day_df.iloc[0]["open"])
        cl = float(day_df.iloc[-1]["close"])
        if op > 0:
            r = (cl - op) / op * 100
            rets.append(r)
            if r > 0:
                bull += 1
    if not rets:
        return {}
    return {
        "n_days":      len(rets),
        "avg_oc_%":    float(np.mean(rets)),
        "std_oc_%":    float(np.std(rets)),
        "bull_rate_%": bull / len(rets) * 100,
    }


def calc_session_volatility(daily_groups: dict) -> dict:
    """時間帯別ボラ（バーごとの (high-low)/close% 平均）。"""
    buckets = {
        "open_first15":   [],   # 9:00-9:15
        "am_late":        [],   # 9:15-11:30
        "pm_first":       [],   # 12:30-14:00
        "close_last30":   [],   # 14:30-15:00
    }
    for _, day_df in daily_groups.items():
        for ts, row in day_df.iterrows():
            t  = ts.time()
            cl = float(row["close"])
            if cl <= 0:
                continue
            v = (float(row["high"]) - float(row["low"])) / cl * 100
            if t < dtime(9, 15):
                buckets["open_first15"].append(v)
            elif t < AM_END:
                buckets["am_late"].append(v)
            elif PM_START <= t < dtime(14, 0):
                buckets["pm_first"].append(v)
            elif dtime(14, 30) <= t < PM_END:
                buckets["close_last30"].append(v)
    return {k: float(np.mean(v)) if v else 0.0 for k, v in buckets.items()}


def calc_volume_distribution(daily_groups: dict) -> dict:
    """寄付バー / 前場 / 後場 の出来高シェア。"""
    open_share = []
    am_share   = []
    for _, day_df in daily_groups.items():
        total = float(day_df["volume"].sum())
        if total <= 0:
            continue
        first = float(day_df.iloc[0]["volume"])
        am    = float(day_df[day_df.index.time < AM_END]["volume"].sum())
        open_share.append(first / total * 100)
        am_share.append(am / total * 100)
    if not open_share:
        return {}
    return {
        "open_bar_share_%":  float(np.mean(open_share)),
        "am_session_share_%": float(np.mean(am_share)),
        "pm_session_share_%": 100 - float(np.mean(am_share)),
    }


def calc_vwap_deviation(daily_groups: dict) -> dict:
    """VWAP からの平均乖離・最大乖離。"""
    devs = []
    max_devs = []
    for _, day_df in daily_groups.items():
        tp  = (day_df["high"] + day_df["low"] + day_df["close"]) / 3
        vol = day_df["volume"]
        if vol.sum() <= 0:
            continue
        vwap = (tp * vol).cumsum() / vol.cumsum()
        dev_pct = (day_df["close"] - vwap) / vwap * 100
        devs.append(float(dev_pct.abs().mean()))
        max_devs.append(float(dev_pct.abs().max()))
    if not devs:
        return {}
    return {
        "avg_abs_dev_%": float(np.mean(devs)),
        "max_abs_dev_%": float(np.mean(max_devs)),
    }


def calc_run_lengths(daily_groups: dict) -> dict:
    """連続陽線/陰線の最大ラン長平均（モメンタム継続性）。"""
    bull_runs = []
    bear_runs = []
    for _, day_df in daily_groups.items():
        sign = np.sign(day_df["close"].diff().fillna(0))
        cur = 0
        prev = 0
        max_b = 0
        max_s = 0
        for s in sign:
            if s == prev and s != 0:
                cur += 1
            else:
                cur = 1
                prev = s
            if s > 0:
                max_b = max(max_b, cur)
            elif s < 0:
                max_s = max(max_s, cur)
        bull_runs.append(max_b)
        bear_runs.append(max_s)
    return {
        "max_bull_run_avg": float(np.mean(bull_runs)) if bull_runs else 0,
        "max_bear_run_avg": float(np.mean(bear_runs)) if bear_runs else 0,
    }


# ─────────────────────────────────────────────────────────────
# 銘柄ごとの分析
# ─────────────────────────────────────────────────────────────

def analyze_symbol(symbol: str, name: str, days: int, interval: str) -> dict | None:
    df = fetch_intraday(symbol, days, interval)
    if df is None or len(df) < 50:
        print(f"  [skip] {symbol} {name}: データなし", file=sys.stderr)
        return None
    daily = split_by_day(df)
    if len(daily) < 5:
        print(f"  [skip] {symbol}: 日数不足 ({len(daily)}日)", file=sys.stderr)
        return None

    return {
        "symbol":      symbol,
        "name":        name,
        "n_days":      len(daily),
        "n_bars":      len(df),
        "interval":    interval,
        "range":       calc_day_range_stats(daily),
        "or15":        calc_or_breakout_stats(daily, 15),
        "or30":        calc_or_breakout_stats(daily, 30),
        "or60":        calc_or_breakout_stats(daily, 60),
        "open_close":  calc_open_to_close_stats(daily),
        "session_vol": calc_session_volatility(daily),
        "volume_dist": calc_volume_distribution(daily),
        "vwap":        calc_vwap_deviation(daily),
        "runs":        calc_run_lengths(daily),
    }


# ─────────────────────────────────────────────────────────────
# レポート出力
# ─────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    if not results:
        print("分析対象なし")
        return

    W = 80
    print("=" * W)
    print(f"  デイトレード相場分析レポート  ({datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')})")
    print("=" * W)

    for r in results:
        print(f"\n■ {r['symbol']}  {r['name']}  "
              f"({r['interval']}, {r['n_days']}日, {r['n_bars']:,}本)")
        print("-" * W)

        rg = r["range"]
        if rg:
            print(f"  日中値幅%: 平均 {rg['avg_range_%']:.2f}%  中央 {rg['med_range_%']:.2f}%  "
                  f"P25 {rg['p25_range_%']:.2f}%  P75 {rg['p75_range_%']:.2f}%  "
                  f"最大 {rg['max_range_%']:.2f}%")

        oc = r["open_close"]
        if oc:
            print(f"  寄→引方向: 平均 {oc['avg_oc_%']:+.2f}%  σ {oc['std_oc_%']:.2f}%  "
                  f"陽線率 {oc['bull_rate_%']:.0f}%")

        for key, label in [("or15", "OR15分"), ("or30", "OR30分"), ("or60", "OR60分")]:
            o = r[key]
            if o:
                print(f"  {label}: 幅 {o['avg_or_width_%']:.2f}%  "
                      f"上抜け {o['up_breakout_count']}回 継続率 {o['up_continue_rate']:.0f}%  "
                      f"下抜け {o['dn_breakout_count']}回 継続率 {o['dn_continue_rate']:.0f}%")

        sv = r["session_vol"]
        if sv:
            print(f"  時間帯ボラ: 寄付15分 {sv['open_first15']:.3f}%  前場後半 {sv['am_late']:.3f}%  "
                  f"後場前 {sv['pm_first']:.3f}%  引け前30分 {sv['close_last30']:.3f}%")

        vd = r["volume_dist"]
        if vd:
            print(f"  出来高: 寄付バー {vd['open_bar_share_%']:.1f}%  "
                  f"前場 {vd['am_session_share_%']:.1f}%  後場 {vd['pm_session_share_%']:.1f}%")

        vw = r["vwap"]
        if vw:
            print(f"  VWAP乖離: 平均 |{vw['avg_abs_dev_%']:.2f}%|  最大 |{vw['max_abs_dev_%']:.2f}%|")

        rn = r["runs"]
        if rn:
            print(f"  連続バー: 陽 {rn['max_bull_run_avg']:.1f}本  陰 {rn['max_bear_run_avg']:.1f}本")

    # 集計
    print("\n" + "=" * W)
    print("  全銘柄サマリー")
    print("=" * W)
    avg_range = np.mean([r["range"]["avg_range_%"] for r in results if r["range"]])
    avg_or30  = np.mean([r["or30"]["avg_or_width_%"] for r in results if r["or30"]])
    avg_up_cr = np.mean([r["or30"]["up_continue_rate"] for r in results if r["or30"]])
    avg_dn_cr = np.mean([r["or30"]["dn_continue_rate"] for r in results if r["or30"]])
    print(f"  平均日中値幅 : {avg_range:.2f}%")
    print(f"  平均OR30幅   : {avg_or30:.2f}%")
    print(f"  OR30 上抜け継続率: {avg_up_cr:.0f}%   下抜け継続率: {avg_dn_cr:.0f}%")

    print("\n【戦略設計のヒント】")
    print(f"  - 損切り目安: 約 {avg_or30 * 0.5:.2f}% (OR幅×0.5)")
    print(f"  - 目標目安  : 約 {avg_range * 0.5:.2f}% (日中値幅×0.5)")
    print(f"  - ORブレイク継続率が {max(avg_up_cr, avg_dn_cr):.0f}% → "
          f"{'戦略1(ORB)有効' if max(avg_up_cr, avg_dn_cr) >= 50 else '戦略1(ORB)弱含み'}")


def save_csv(results: list[dict], path: Path) -> None:
    rows = []
    for r in results:
        flat = {"symbol": r["symbol"], "name": r["name"], "n_days": r["n_days"]}
        for sect in ["range", "or15", "or30", "or60", "open_close",
                     "session_vol", "volume_dist", "vwap", "runs"]:
            for k, v in (r.get(sect) or {}).items():
                flat[f"{sect}.{k}"] = v
        rows.append(flat)
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\nCSV出力: {path.resolve()}")


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="デイトレ相場分析")
    parser.add_argument("symbols", nargs="*",
                        help="銘柄コード (例: 7203.T)。省略時はデフォルトリスト")
    parser.add_argument("--interval", choices=["1m", "5m", "15m"], default="5m")
    parser.add_argument("--days", type=int, default=30,
                        help="取得日数 (1m=最大7, 5m=最大60)")
    parser.add_argument("--csv", type=str, default=None, help="CSV出力パス")
    args = parser.parse_args()

    # interval の上限調整
    max_days = {"1m": 7, "5m": 60, "15m": 60}[args.interval]
    if args.days > max_days:
        print(f"[info] {args.interval} は最大 {max_days} 日 → {max_days} に調整", file=sys.stderr)
        args.days = max_days

    if args.symbols:
        targets = [(s, s) for s in args.symbols]
    else:
        targets = DEFAULT_SYMBOLS

    print(f"分析開始: {len(targets)}銘柄  interval={args.interval}  days={args.days}",
          flush=True)

    results: list[dict] = []
    for sym, name in targets:
        print(f"  [fetch] {sym} {name}...", flush=True)
        r = analyze_symbol(sym, name, args.days, args.interval)
        if r:
            results.append(r)

    print_report(results)

    if args.csv:
        save_csv(results, Path(args.csv))


if __name__ == "__main__":
    main()
