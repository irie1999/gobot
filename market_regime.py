"""
market_regime.py — 日経平均から「今の相場レジーム(上げ / 横ばい / 下げ)」を大局判定する。

現行の日次分類(終値>MA5>MA10)は短期すぎるので、より大きな括りで：
  - ADX(トレンド強度)  : 高い=トレンドあり / 低い=横ばい(レンジ)
  - MA200 と傾き        : 方向(上げ / 下げ)
で分類する。過去N ヶ月の推移も出してチャートと照合できる。

使い方:
  python market_regime.py
  python market_regime.py --months 36        # 過去3年の推移
  python market_regime.py --symbol ^N225
"""
import argparse

import numpy as np
import pandas as pd
import yfinance as yf

ap = argparse.ArgumentParser()
ap.add_argument("--symbol", default="^N225", help="指数 (既定: 日経平均 ^N225)")
ap.add_argument("--months", type=int, default=18, help="推移を表示する月数 (既定18)")
ap.add_argument("--adx-trend", type=float, default=25.0, help="ADXがこれ以上=トレンドあり")
ap.add_argument("--adx-range", type=float, default=20.0, help="ADXがこれ未満=横ばい")
args = ap.parse_args()


def compute_adx(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), plus_di, minus_di


def classify(close, ma200, slope200, adxv, adx_trend, adx_range):
    """大局レジーム: 上げ / 横ばい / 下げ / 移行"""
    if pd.isna(ma200) or pd.isna(adxv):
        return "?"
    if adxv < adx_range:
        return "横ばい"          # トレンド弱い=レンジ
    if adxv >= adx_trend:
        if close > ma200 and slope200 > 0:
            return "上げ"
        if close < ma200 and slope200 < 0:
            return "下げ"
        return "移行"
    return "移行"                # ADX 20-25 は中間


# ── データ取得 ──
raw = yf.Ticker(args.symbol).history(period="6y", interval="1d", auto_adjust=False)
if raw.empty or len(raw) < 220:
    print("[ERROR] データ取得失敗 or データ不足")
    raise SystemExit(1)
if raw.index.tz is not None:
    raw.index = raw.index.tz_localize(None)

df = raw[["Open", "High", "Low", "Close"]].copy()
df["MA25"] = df["Close"].rolling(25).mean()
df["MA75"] = df["Close"].rolling(75).mean()
df["MA200"] = df["Close"].rolling(200).mean()
df["slope200"] = df["MA200"].pct_change(20) * 100          # MA200の20日変化率(%)
df["ret6m"] = df["Close"].pct_change(120) * 100            # 約6ヶ月リターン(%)
df["ADX"], df["+DI"], df["-DI"] = compute_adx(df)
df["regime"] = [
    classify(c, m, s, a, args.adx_trend, args.adx_range)
    for c, m, s, a in zip(df["Close"], df["MA200"], df["slope200"], df["ADX"])
]

cur = df.iloc[-1]
_lbl = {"上げ": "🟢 上げ相場(トレンドあり・上向き)",
        "横ばい": "🟡 横ばい相場(レンジ・トレンド弱い) ← 戦略が苦手",
        "下げ": "🔴 下げ相場(トレンドあり・下向き)",
        "移行": "⚪ 移行期(判定中間)",
        "?": "? 判定不能"}

print("=" * 60)
print(f"  日経平均 現在レジーム判定  ({cur.name.date()})")
print("=" * 60)
print(f"  → 【 {_lbl.get(cur['regime'], cur['regime'])} 】\n")
print(f"  終値        : {cur['Close']:>10,.0f}")
print(f"  MA200       : {cur['MA200']:>10,.0f}  (終値は {(cur['Close']/cur['MA200']-1)*100:+.1f}%)")
print(f"  MA200 傾き  : {cur['slope200']:+.1f}%  (20日変化 / +=上向き)")
print(f"  ADX(14)     : {cur['ADX']:>6.1f}   "
      f"({'トレンドあり' if cur['ADX'] >= args.adx_trend else '横ばい寄り' if cur['ADX'] < args.adx_range else '中間'})")
print(f"  +DI / -DI   : {cur['+DI']:.1f} / {cur['-DI']:.1f}  "
      f"({'買い優勢' if cur['+DI'] > cur['-DI'] else '売り優勢'})")
print(f"  6ヶ月リターン: {cur['ret6m']:+.1f}%")

# ── 月末レジームの推移 ──
print("\n" + "=" * 60)
print(f"  月末レジーム推移 (直近{args.months}ヶ月・チャートと照合用)")
print("=" * 60)
print(f"{'月':<9}{'終値':>10}{'ADX':>7}{'vs MA200':>10}   レジーム")
print("-" * 55)
df["ym"] = df.index.strftime("%Y-%m")
last = df.groupby("ym").tail(1).set_index("ym")
for ym in list(last.index)[-args.months:]:
    r = last.loc[ym]
    vm = (r["Close"] / r["MA200"] - 1) * 100 if not pd.isna(r["MA200"]) else float("nan")
    print(f"{ym:<9}{r['Close']:>10,.0f}{r['ADX']:>7.1f}{vm:>+9.1f}%   {r['regime']}")

print("\n※ 上げ=順張り(現行)が得意 / 横ばい=逆張り(RSI2)向き / 下げ=現金・ショート")
