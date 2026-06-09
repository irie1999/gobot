"""
signal_risk_check.py — シグナル銘柄のリスク警告チェック

スキップではなく、シグナル表に警告バッジとして表示するための情報を提供。

警告レベル:
  danger  (🔴) : ネガティブニュース検出 / 機関売り圧力
  warning (🟡) : 決算前後 / ATRスパイク / 日経急落

使い方 (run_signals_holdout_all.py から呼び出す):
  from signal_risk_check import precompute_all, RISK_FLAGS, get_nikkei_status
  precompute_all([(sym, name), ...], workers=4)
  # → RISK_FLAGS[symbol] = [{"level":"warning","code":"EARNINGS","msg":"決算3日前"}, ...]
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ── モジュールレベルキャッシュ (check_signals_*.py から参照) ──────────────────
RISK_FLAGS: dict[str, list[dict]] = {}

# ── ネガティブキーワード（回避判断用のみ） ───────────────────────────────────
_NEG_JP = [
    "不正", "行政処分", "経営危機", "倒産", "破産", "粉飾", "横領",
    "減益", "赤字", "下方修正", "業績悪化", "大幅減", "損失拡大",
    "リストラ", "希望退職", "売り推奨", "格下げ", "目標株価引き下げ",
    "レーティング引き下げ", "急落", "ストップ安",
]
_NEG_EN = [
    "fraud", "scandal", "bankruptcy", "downgrade", "sell",
    "miss", "loss", "decline", "warning", "investigation",
    "crash", "plunge", "suspended",
]


# ─────────────────────────────────────────────────────────────────────────────
# 個別チェック関数
# ─────────────────────────────────────────────────────────────────────────────

def _check_negative_news(symbol: str, name: str) -> dict | None:
    """直近7日のニュースタイトルにネガティブキーワードがあれば返す"""
    try:
        query   = urllib.parse.quote(f"{name} 株")
        url     = (f"https://news.google.com/rss/search"
                   f"?q={query}&hl=ja&gl=JP&ceid=JP:ja")
        req     = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; gobot-risk/1.0)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            root = ET.fromstring(r.read())

        found: list[str] = []
        for item in root.findall(".//item")[:10]:
            title = (item.findtext("title") or "").strip()
            tl    = title.lower()
            for kw in _NEG_JP:
                if kw in title and kw not in found:
                    found.append(kw)
            for kw in _NEG_EN:
                if kw in tl and kw not in found:
                    found.append(kw)
        if found:
            return {
                "level": "danger",
                "code":  "NEG_NEWS",
                "msg":   f"ネガティブニュース: {', '.join(found[:3])}",
            }
    except Exception:
        pass
    return None


def _check_earnings_proximity(symbol: str, target_date: date | None = None) -> dict | None:
    """決算発表日が±3営業日以内なら警告"""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        cal = t.calendar
        if not cal:
            return None

        # 'Earnings Date' は list or single date
        ed = cal.get("Earnings Date")
        if ed is None:
            return None
        if isinstance(ed, (list, tuple)):
            dates = [d if isinstance(d, date) else d.date() for d in ed if d is not None]
        else:
            dates = [ed if isinstance(ed, date) else ed.date()]

        today = target_date or datetime.now(JST).date()
        for d in dates:
            diff = abs((d - today).days)
            if diff <= 5:
                direction = "前" if d >= today else "後"
                return {
                    "level": "warning",
                    "code":  "EARNINGS",
                    "msg":   f"決算{diff}日{direction} ({d})",
                }
    except Exception:
        pass
    return None


def _check_volume_price_divergence(symbol: str) -> dict | None:
    """出来高が増加しているのに価格が下落（機関売り圧力の兆候）"""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="1mo", auto_adjust=True)
        if len(hist) < 10:
            return None

        recent   = hist.tail(5)
        baseline = hist.iloc[:-5]

        vol_ratio  = recent["Volume"].mean() / (baseline["Volume"].mean() + 1)
        price_chg  = (recent["Close"].iloc[-1] - recent["Close"].iloc[0]) / (recent["Close"].iloc[0] + 1e-8)

        if vol_ratio >= 1.5 and price_chg <= -0.03:
            return {
                "level": "danger",
                "code":  "VOL_SELL",
                "msg":   f"機関売り圧力 (出来高{vol_ratio:.1f}倍 / 価格{price_chg*100:+.1f}%)",
            }
    except Exception:
        pass
    return None


def _check_atr_spike(symbol: str) -> dict | None:
    """ATRが直近20日平均の2倍超 → 異常ボラ"""
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="3mo", auto_adjust=True)
        if len(hist) < 25:
            return None

        tr = (hist["High"] - hist["Low"]).abs()
        atr_recent  = tr.iloc[-3:].mean()
        atr_baseline = tr.iloc[-23:-3].mean()
        if atr_baseline > 0 and atr_recent / atr_baseline >= 2.0:
            ratio = atr_recent / atr_baseline
            return {
                "level": "warning",
                "code":  "ATR_SPIKE",
                "msg":   f"ボラ急騰 ATR {ratio:.1f}倍 (直近3日/前20日)",
            }
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 日経平均チェック（銘柄ごとではなく全体チェック）
# ─────────────────────────────────────────────────────────────────────────────

_nikkei_status_cache: dict | None = None


def get_nikkei_status(target_date: date | None = None) -> dict | None:
    """
    日経平均の直近変化率を返す。
    -1.5% 以下なら warning, -3.0% 以下なら danger。
    None なら問題なし / 取得失敗。
    """
    global _nikkei_status_cache
    if _nikkei_status_cache is not None:
        return _nikkei_status_cache

    try:
        import yfinance as yf
        hist = yf.Ticker("^N225").history(period="5d", auto_adjust=True)
        if len(hist) < 2:
            return None
        prev  = hist["Close"].iloc[-2]
        last  = hist["Close"].iloc[-1]
        chg   = (last - prev) / prev * 100
        last_date = hist.index[-1].date()

        if chg <= -3.0:
            result = {"level": "danger",  "chg": chg, "date": str(last_date),
                      "msg": f"日経急落 {chg:+.2f}% — 全シグナルに注意"}
        elif chg <= -1.5:
            result = {"level": "warning", "chg": chg, "date": str(last_date),
                      "msg": f"日経下落 {chg:+.2f}% — シグナル精度に影響の可能性"}
        else:
            result = None

        _nikkei_status_cache = result
        return result
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 一括事前計算
# ─────────────────────────────────────────────────────────────────────────────

def _compute_one(symbol: str, name: str, target_date: date | None) -> list[dict]:
    flags: list[dict] = []
    for fn in [
        lambda: _check_negative_news(symbol, name),
        lambda: _check_earnings_proximity(symbol, target_date),
        lambda: _check_volume_price_divergence(symbol),
        lambda: _check_atr_spike(symbol),
    ]:
        try:
            r = fn()
            if r:
                flags.append(r)
        except Exception:
            pass
    return flags


def precompute_all(
    symbols_names: list[tuple[str, str]],
    workers: int = 4,
    target_date: date | None = None,
) -> None:
    """
    全シグナル銘柄のリスクフラグを並列で計算し RISK_FLAGS に格納する。

    Args:
        symbols_names: [(symbol, name), ...]  例 [("7203.T", "トヨタ"), ...]
        workers:       並列数
        target_date:   判定基準日 (None = 今日)
    """
    RISK_FLAGS.clear()
    if not symbols_names:
        return

    n = len(symbols_names)
    print(f"リスクチェック中 ({n}銘柄)...", flush=True)

    with ThreadPoolExecutor(max_workers=min(workers, n)) as ex:
        futs = {
            ex.submit(_compute_one, sym, nm, target_date): sym
            for sym, nm in symbols_names
        }
        done = 0
        for fut in as_completed(futs):
            sym  = futs[fut]
            done += 1
            try:
                flags = fut.result()
                if flags:
                    RISK_FLAGS[sym] = flags
            except Exception:
                pass
        print(f"リスクチェック完了: {sum(len(v) for v in RISK_FLAGS.values())}件の警告", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# バッジ HTML 生成ヘルパー (check_signals_*.py から呼び出す)
# ─────────────────────────────────────────────────────────────────────────────

def render_risk_badges(symbol: str) -> str:
    """RISK_FLAGS から symbol の警告バッジ HTML を返す"""
    flags = RISK_FLAGS.get(symbol, [])
    if not flags:
        return ""
    html = ""
    for f in flags:
        msg  = f["msg"].replace('"', "&quot;")
        if f["level"] == "danger":
            html += (f'<br><span title="{msg}" style="cursor:help;'
                     f'color:#ef4444;font-size:10px;font-weight:700">'
                     f'🔴 {f["msg"]}</span>')
        else:
            html += (f'<br><span title="{msg}" style="cursor:help;'
                     f'color:#fbbf24;font-size:10px;font-weight:700">'
                     f'🟡 {f["msg"]}</span>')
    return html


def render_nikkei_banner() -> str:
    """日経急落バナー HTML を返す (問題なければ空文字)"""
    status = get_nikkei_status()
    if not status:
        return ""
    color = "#ef4444" if status["level"] == "danger" else "#f59e0b"
    bg    = "#450a0a" if status["level"] == "danger" else "#451a03"
    icon  = "🔴" if status["level"] == "danger" else "🟡"
    return (
        f'<div style="background:{bg};border:1px solid {color};'
        f'border-radius:6px;padding:10px 16px;margin:8px 0 12px;'
        f'color:{color};font-weight:700;font-size:0.88rem">'
        f'{icon} {status["msg"]}</div>'
    )
