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
    # 米国(05:00 JST 確定)
    "sp500":  ("^GSPC",     "S&P500"),
    "nasdaq": ("^IXIC",     "NASDAQ"),
    "sox":    ("^SOX",      "SOX半導体"),      # 日本株は半導体比重が高い
    "vix":    ("^VIX",      "VIX"),
    "us10y":  ("^TNX",      "米10年債利回り"),
    # 欧州(00:30〜01:30 JST 確定)
    "dax":    ("^GDAXI",    "DAX"),
    "sx5e":   ("^STOXX50E", "ユーロSTOXX50"),
    # アジア(前日の引け)。⚠ 東京が最も早く開くので、当日の情報にはならない
    "kospi":  ("^KS11",     "KOSPI(前日)"),
    # 為替・商品・先物(ほぼ24時間)
    "usdjpy": ("JPY=X",     "USDJPY"),
    "dxy":    ("DX-Y.NYB",  "ドル指数"),
    "oil":    ("CL=F",      "WTI原油"),
    "gold":   ("GC=F",      "金"),
    "esfut":  ("ES=F",      "S&P500先物"),
    # 日本
    "n225":   ("^N225",     "日経平均"),
    "nkfut":  ("NKD=F",     "日経225先物(CME)"),
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
        # ⛔ **nkfut がこのリストから漏れていた**(2026-08-28 に発覚)。
        #   日経先物そのもののリターン = 『直前の上昇の勢い』が
        #   一度も変数に入っていなかった。fut_gap(先物と現物の**乖離**)は
        #   水準であって勢いではない。
        for k in ("sp500", "nasdaq", "sox", "usdjpy", "n225", "nkfut",
                  "dax", "sx5e", "kospi", "dxy", "oil", "gold", "esfut"):
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
        # ★ 『勢い』。水準ではなく **変化の向きと持続**。
        #   ・nkfut_2d   … 先物の2日リターン(勢いが続いているか)
        #   ・fut_gap_chg… 先物-現物ギャップの前日差
        #                  (ギャップが**拡大している**のか縮んでいるのか)
        _fb = sorted(k for k in (ser.get("nkfut") or {}) if k < d)
        if len(_fb) >= 3:
            _sf = ser["nkfut"]
            f["nkfut_2d"] = (_sf[_fb[-1]] / _sf[_fb[-3]] - 1.0) * 100.0
        _nb = sorted(k for k in (ser.get("n225") or {}) if k < d)
        if len(_fb) >= 2 and len(_nb) >= 2:
            _sf, _sn = ser["nkfut"], ser["n225"]
            _g0 = (_sf[_fb[-1]] / _sn[_nb[-1]] - 1.0) * 100.0
            _g1 = (_sf[_fb[-2]] / _sn[_nb[-2]] - 1.0) * 100.0
            f["fut_gap_chg"] = _g0 - _g1
        # 米10年債は水準(%)そのものと前日差(bp)
        b = _prev_bars(ser.get("us10y") or {}, d, 2)
        if b:
            f["us10y_chg"] = (b[0] - b[1]) * 100.0      # bp
        # ★ 文献由来: ノイズトレーダーの**過剰反応 → 日中の反転**なので、
        #   効くとしたら符号ではなく **ショックの大きさ** かもしれない。
        #   (Chen et al. 2026: 前日S&P500が高いほど寄り30分のリターンは低い /
        #    反転は金融危機時には弱まる = 大きさに非線形)
        if "sp500_ret" in f:
            f["sp500_abs"] = abs(f["sp500_ret"])
        if "fut_gap" in f:
            f["futgap_abs"] = abs(f["fut_gap"])
        try:
            f["dow"] = float(datetime.fromisoformat(d).weekday())   # 0=月
        except Exception:
            pass
        out[d] = f
    return out


# 表示用のラベル(呼び出し側で使う)
LABELS = {
    "fut_gap":    "先物-現物% (寄りギャップ予想)",
    "futgap_abs": "|先物-現物%| (ギャップの大きさ)",
    "vix":        "VIX 水準",
    "vix_chg":    "VIX 変化%",
    "sp500_ret":  "S&P500 前日%",
    "sp500_abs":  "|S&P500 前日%| (ショックの大きさ)",
    "nasdaq_ret": "NASDAQ 前日%",
    "sox_ret":    "SOX半導体 前日%",
    "us10y_chg":  "米10年債 前日差(bp)",
    "dax_ret":    "DAX 前日%",
    "sx5e_ret":   "ユーロSTOXX50 前日%",
    "kospi_ret":  "KOSPI 前日%",
    "usdjpy_ret": "USDJPY 前日%",
    "dxy_ret":    "ドル指数 前日%",
    "oil_ret":    "WTI原油 前日%",
    "gold_ret":   "金 前日%",
    "esfut_ret":  "S&P500先物 前日%",
    "n225_ret":   "日経 前日%",
    "nkfut_ret":  "日経先物 前日% (勢い)",
    "nkfut_2d":   "日経先物 2日% (勢いの持続)",
    "fut_gap_chg": "先物ギャップの前日差 (拡大/縮小)",
    "n225_5d":    "日経 5日%",
}


# ══════════════════════════════════════════════════════════════════════
# ⛔⛔ ここから下は **事後(hindsight)** の値です。発注判断に使ってはいけません。
#     「大負け日は そもそも相場で説明できるのか」= 予測可能性の**上限**を
#     測るためだけに使います(§18.59 / --tail-diag)。
#     上限が小さければ、寄り前の変数がどれだけ優秀でも原理的に届きません。
# ══════════════════════════════════════════════════════════════════════

def _load_ohlc(tkr: str, start: str, end: str):
    """日足の Open/Close を {date(str): (open, close)} で返す。失敗したら空。"""
    key = ("OHLC", tkr, start, end)
    if key in _CACHE:
        return _CACHE[key]
    out: dict = {}
    try:
        import yfinance as yf
        raw = yf.Ticker(tkr).history(start=start, end=end, interval="1d",
                                     auto_adjust=False, actions=False)
        for ts, row in raw.iterrows():
            o, c = float(row.get("Open") or 0), float(row.get("Close") or 0)
            if o > 0 and c > 0:
                out[str(ts.date())] = (o, c)
    except Exception:
        out = {}
    _CACHE[key] = out
    return out


def sameday_features(days: list[str]) -> dict:
    """⛔ **事後**。その日の日経が実際にどう動いたか。

    返すもの(すべて %):
      n225_same_ret … 前日終値 → 当日終値      (N の建玉が晒された区間ほぼ全部)
      n225_same_gap … 前日終値 → 当日**始値**  (N が建てる瞬間)
      n225_same_day … 当日始値 → 当日終値      (**N が保有している区間そのもの**)

    ★ N は寄りで売って引けで買い戻すので、**n225_same_day が本命**。
      これで説明できない損失は、相場全体では説明できない = 銘柄固有。
    """
    if not days:
        return {}
    d0, d1 = min(days), max(days)
    start = (datetime.fromisoformat(d0) - timedelta(days=40)).strftime("%Y-%m-%d")
    end = (datetime.fromisoformat(d1) + timedelta(days=2)).strftime("%Y-%m-%d")
    ser = _load_ohlc("^N225", start, end)
    ks = sorted(ser)
    _pos = {k: i for i, k in enumerate(ks)}
    out: dict = {}
    for d in days:
        i = _pos.get(d)
        if i is None or i == 0:
            continue
        o, c = ser[d]
        pc = ser[ks[i - 1]][1]
        out[d] = {
            "n225_same_ret": (c / pc - 1.0) * 100.0,
            "n225_same_gap": (o / pc - 1.0) * 100.0,
            "n225_same_day": (c / o - 1.0) * 100.0,
        }
    return out


SAMEDAY_LABELS = {
    "n225_same_ret": "日経 当日% (前日終値→終値)",
    "n225_same_gap": "日経 当日の寄りギャップ%",
    "n225_same_day": "日経 当日の**日中**% (始値→終値) ★N の保有区間",
}
