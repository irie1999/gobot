r"""preopen_market.py — 寄り前(09:00より前)に確定している **市場全体** の変数を作る。

なぜ要るか (§18.34 / 2026-08-13 ユーザー依頼)
---------------------------------------------
H は同日決済で全銘柄が同方向(ショート)。18.30 で「日次損益の94%は銘柄固有・
市場要因は6%(R²=0.063)」と出ているので**日中のヘッジは無意味**だが、
「その日に**参加するかどうか**」は別の問い。σ の6%しか説明できなくても、
裾(大きく負ける日)だけを避けられるなら価値がある。実際 18.24 で
「全体の最悪5日を除くと木曜の劣位が消える」= **少数の日が損益を支配している**。

⛔ 18.13(15軸) も 18.24(7属性) も **銘柄属性** しか掃いていない。
   **市場全体の寄り前変数は一度も測っていない**ので、「候補ゼロが続いている」
   という前例はここには当てはまらない。

リークの扱い(ここを外すと全部無意味)
------------------------------------
日本の営業日 D について、**09:00 時点で確定している**のは:
  ・米国市場の終値 = D-1 の米国バー(05:00 JST に確定) → `日付 < D` の最新バー
  ・前日の日本市場 = D-1 の ^N225 バー(15:00 JST 確定) → `日付 < D` の最新バー
  ・CME 日経先物 = D-1 のバー(≈05:00 JST 確定) → 同上
**どの系列も「日付 < D の最新バー」だけを使う。** D 当日のバーは使わない
(^N225 の D は当日の引け = 未来 / ^GSPC の D は D+1 早朝の確定 = 未来)。

使い方
------
  from preopen_market import preopen_features
  feat = preopen_features(["2026-08-13", "2026-08-12", ...])
  # -> {"2026-08-13": {"sp500_ret": -0.4, "vix_chg": +3.2, "fut_gap": +0.8, ...}, ...}

yfinance を1回だけ叩いてプロセス内にキャッシュする。取得失敗した系列は
その列ごと落ちる(呼び出し側は「列が無い」ことを許容する)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 系列名 -> (yfinance ティッカー, 説明)。取れなかったものは黙って落とす。
_SERIES = {
    "sp500":  ("^GSPC",  "S&P500"),
    "nasdaq": ("^IXIC",  "NASDAQ"),
    "vix":    ("^VIX",   "VIX"),
    "usdjpy": ("JPY=X",  "USDJPY"),
    "n225":   ("^N225",  "日経平均"),
    "nkfut":  ("NKD=F",  "日経225先物(CME)"),
}

_CACHE: dict = {}


def _load(tkr: str, start: str, end: str):
    """日足の終値を {date(str): close} で返す。失敗したら空。"""
    key = (tkr, start, end)
    if key in _CACHE:
        return _CACHE[key]
    out: dict = {}
    try:
        import yfinance as yf
        raw = yf.Ticker(tkr).history(start=start, end=end, interval="1d",
                                     auto_adjust=False, actions=False)
        for ts, row in raw.iterrows():
            c = float(row.get("Close") or 0)
            if c > 0:
                out[str(ts.date())] = c
    except Exception:
        out = {}
    _CACHE[key] = out
    return out


def _prev_bars(series: dict, day: str, n: int = 2) -> list[float]:
    """`日付 < day` の最新 n 本の終値を新しい順で返す。足りなければ空。

    ⛔ ここが唯一のリーク対策。当日のバーは **絶対に** 使わない。
    """
    ks = sorted(k for k in series if k < day)
    if len(ks) < n:
        return []
    return [series[k] for k in reversed(ks[-n:])]


def preopen_features(days: list[str]) -> dict:
    """営業日リスト(YYYY-MM-DD)に対し、寄り前に確定している変数を返す。"""
    if not days:
        return {}
    d0 = min(days)
    d1 = max(days)
    start = (datetime.fromisoformat(d0) - timedelta(days=40)).strftime("%Y-%m-%d")
    end = (datetime.fromisoformat(d1) + timedelta(days=2)).strftime("%Y-%m-%d")
    ser = {k: _load(t, start, end) for k, (t, _) in _SERIES.items()}

    out: dict = {}
    for d in days:
        f: dict = {}
        for k in ("sp500", "nasdaq", "usdjpy", "n225"):
            b = _prev_bars(ser.get(k) or {}, d, 2)
            if b:
                f[f"{k}_ret"] = (b[0] / b[1] - 1.0) * 100.0
        b = _prev_bars(ser.get("vix") or {}, d, 2)
        if b:
            f["vix"] = b[0]
            f["vix_chg"] = (b[0] / b[1] - 1.0) * 100.0
        # 5日リターン(日経)。寄り前に確定している直近5本。
        _n = sorted(k for k in (ser.get("n225") or {}) if k < d)
        if len(_n) >= 6:
            s = ser["n225"]
            f["n225_5d"] = (s[_n[-1]] / s[_n[-6]] - 1.0) * 100.0
        # ★ 先物と日経現物の乖離 = **寄りギャップの事前推定**。
        #   どちらも「日付 < D の最新」なので、先物のほうが後の時刻まで動いている。
        fb = _prev_bars(ser.get("nkfut") or {}, d, 1)
        nb = _prev_bars(ser.get("n225") or {}, d, 1)
        if fb and nb:
            f["fut_gap"] = (fb[0] / nb[0] - 1.0) * 100.0
        try:
            f["dow"] = float(datetime.fromisoformat(d).weekday())   # 0=月
        except Exception:
            pass
        out[d] = f
    return out


# 表示用のラベル(呼び出し側で使う)
LABELS = {
    "sp500_ret":  "S&P500 前日%",
    "nasdaq_ret": "NASDAQ 前日%",
    "vix":        "VIX 水準",
    "vix_chg":    "VIX 変化%",
    "usdjpy_ret": "USDJPY 前日%",
    "n225_ret":   "日経 前日%",
    "n225_5d":    "日経 5日%",
    "fut_gap":    "先物-現物% (寄りギャップ予想)",
}
