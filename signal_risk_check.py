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

import gzip as _gz
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ── 日次キャッシュファイル ────────────────────────────────────────────────────
# チェック項目を追加・変更したらこの番号を上げると古いキャッシュを自動破棄する
_CACHE_VERSION = 4

_CACHE_DIR = Path(".")

def _daily_cache_path(target_date: date | None = None) -> Path:
    d = target_date or datetime.now(JST).date()
    return _CACHE_DIR / f".risk_check_cache_{d}.json"

def _load_daily_cache(target_date: date | None = None) -> dict:
    p = _daily_cache_path(target_date)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # バージョン不一致 → キャッシュ無効
            if data.get("_version") != _CACHE_VERSION:
                p.unlink(missing_ok=True)
                return {}
            return data
        except Exception:
            pass
    return {}

def _save_daily_cache(data: dict, target_date: date | None = None) -> None:
    p = _daily_cache_path(target_date)
    try:
        data["_version"] = _CACHE_VERSION
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# ── モジュールレベルキャッシュ (check_signals_*.py から参照) ──────────────────
RISK_FLAGS:     dict[str, list[dict]] = {}
EARNINGS_DATES: dict[str, str]        = {}  # symbol → "YYYY-MM-DD" or ""

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


_KABUTAN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Cache-Control": "max-age=0",
}
_IRBANK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.co.jp/",
}

# 株探 HTML から日付を抜く正規表現（複数パターン）
_DATE_PATTERNS = [
    # 「次回決算発表予定日」直後のYYYY年MM月DD日 or YYYY/MM/DD
    r'次回決算[発表]*予定[日]?[^<]{0,60}?(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?',
    # 「決算発表日」テキストのそば
    r'決算発表[日]?[^<]{0,40}?(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?',
    # td の中に単独で YYYY/MM/DD
    r'<td[^>]*>\s*(\d{4})/(\d{2})/(\d{2})\s*</td>',
    # YYYY年MM月DD日 の汎用パターン（ページ内で最初に現れる近未来日）
    r'(\d{4})年(\d{1,2})月(\d{1,2})日',
]


def _parse_date_from_html(html: str, today: date) -> str:
    """
    HTML テキストから次回決算日らしい日付を探して YYYY-MM-DD で返す。
    HTMLタグを除去してから検索するのでタグ境界に強い。
    """
    # タグを除去してプレーンテキスト化
    plain = re.sub(r'<[^>]+>', ' ', html)
    plain = re.sub(r'\s+', ' ', plain)

    # ── Step1: 「次回決算」キーワード付近の日付を優先 ──
    # 前後200文字以内にある日付を拾う
    for kw in ["次回決算発表予定", "次回決算予定", "次回決算", "決算発表予定"]:
        pos = plain.find(kw)
        if pos == -1:
            continue
        context = plain[pos: pos + 200]
        m = re.search(r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?', context)
        if m:
            try:
                dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if dt >= today:
                    return str(dt)
            except Exception:
                pass

    # ── Step2: ページ全体から近未来日付を収集 ──
    candidates: list[date] = []
    for m in re.finditer(r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?', plain):
        try:
            dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            # 今日から6か月以内の未来日だけ候補に
            if today <= dt <= today + timedelta(days=180):
                candidates.append(dt)
        except Exception:
            pass

    if candidates:
        return str(min(candidates))
    return ""


def _kabutan_get(url: str) -> str:
    """
    Cookie セッションを使って kabutan へアクセスする。
    トップページで Cookie を取得してから目的ページへ GET する。
    """
    cj  = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # Step1: トップページを取得して Cookie をセット (Bot 判定回避)
    try:
        req0 = urllib.request.Request("https://kabutan.jp/", headers=_KABUTAN_HEADERS)
        with opener.open(req0, timeout=8):
            pass
    except Exception:
        pass

    # Step2: 目的ページを Cookie 付きで取得
    req = urllib.request.Request(url, headers=_KABUTAN_HEADERS)
    with opener.open(req, timeout=10) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding", "") == "gzip":
            raw = _gz.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def _fetch_next_earnings_date(symbol: str, target_date: date | None = None) -> str:
    """
    複数ソースから次回決算発表予定日を取得して "YYYY-MM-DD" を返す。
    取得できなければ空文字を返す。

    試す順序:
      1. 株探 株価トップページ (/stock/?code=) — 「次回決算発表日」が最も目立つ
      2. 株探 財務ページ (/stock/finance?code=) — 決算スケジュール欄
      3. irbank.net 決算ページ
    """
    code  = symbol.replace(".T", "").replace(".t", "")
    today = target_date or datetime.now(JST).date()

    # --- 株探 (Cookie セッション付き) ---
    for path in [f"/stock/?code={code}", f"/stock/finance?code={code}"]:
        url = f"https://kabutan.jp{path}"
        try:
            html   = _kabutan_get(url)
            result = _parse_date_from_html(html, today)
            if result:
                return result
        except urllib.error.HTTPError as _e:
            print(f"  [決算日] {symbol} kabutan{path[:16]} → HTTP {_e.code}", flush=True)
        except Exception as _e:
            print(f"  [決算日] {symbol} kabutan{path[:16]} → {type(_e).__name__}", flush=True)

    # --- irbank.net (Referer: google 付き) ---
    for url in [f"https://irbank.net/{code}/kessan",
                f"https://irbank.net/{code}"]:
        try:
            req = urllib.request.Request(url, headers=_IRBANK_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding", "") == "gzip":
                    raw = _gz.decompress(raw)
                html = raw.decode("utf-8", errors="replace")
            result = _parse_date_from_html(html, today)
            if result:
                return result
        except urllib.error.HTTPError as _e:
            print(f"  [決算日] {symbol} irbank → HTTP {_e.code}", flush=True)
        except Exception as _e:
            print(f"  [決算日] {symbol} irbank → {type(_e).__name__}", flush=True)

    return ""


def _check_earnings_proximity(symbol: str, target_date: date | None = None) -> dict | None:
    """
    株探から決算日を取得し、±7日以内なら警告を返す。
    EARNINGS_DATES がすでに埋まっている場合はそれを再利用する。
    """
    try:
        today   = target_date or datetime.now(JST).date()
        dt_str  = EARNINGS_DATES.get(symbol) or _fetch_next_earnings_date(symbol, today)
        if not dt_str:
            return None
        earn_dt = date.fromisoformat(dt_str)
        diff    = (earn_dt - today).days
        if abs(diff) <= 7:
            direction = "前" if diff >= 0 else "後"
            return {
                "level": "warning",
                "code":  "EARNINGS",
                "msg":   f"決算{abs(diff)}日{direction} ({earn_dt})",
            }
    except Exception:
        pass
    return None


def _check_volume_price_divergence(hist) -> dict | None:
    """出来高が増加しているのに価格が下落（機関売り圧力の兆候）"""
    try:
        if hist is None or len(hist) < 10:
            return None
        recent    = hist.tail(5)
        baseline  = hist.iloc[:-5]
        vol_ratio = recent["Volume"].mean() / (baseline["Volume"].mean() + 1)
        price_chg = (recent["Close"].iloc[-1] - recent["Close"].iloc[0]) / (recent["Close"].iloc[0] + 1e-8)
        if vol_ratio >= 1.5 and price_chg <= -0.03:
            return {
                "level": "danger",
                "code":  "VOL_SELL",
                "msg":   f"機関売り圧力 (出来高{vol_ratio:.1f}倍 / 価格{price_chg*100:+.1f}%)",
            }
    except Exception:
        pass
    return None


def _check_atr_spike(hist) -> dict | None:
    """ATRが直近3日平均 ÷ 前20日平均 ≥ 2倍 → 異常ボラ"""
    try:
        if hist is None or len(hist) < 25:
            return None
        tr           = (hist["High"] - hist["Low"]).abs()
        atr_recent   = tr.iloc[-3:].mean()
        atr_baseline = tr.iloc[-23:-3].mean()
        if atr_baseline > 0 and atr_recent / atr_baseline >= 2.0:
            return {
                "level": "warning",
                "code":  "ATR_SPIKE",
                "msg":   f"ボラ急騰 ATR {atr_recent/atr_baseline:.1f}倍 (直近3日/前20日)",
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
# 決算シーズン判定（3月末決算・日本上場企業の約70%が対象）
# ─────────────────────────────────────────────────────────────────────────────

# (月, 開始日, 終了日, 発表内容ラベル)
_EARNINGS_SEASONS = [
    (5,  1, 20, "本決算"),          # 通期 (3月期)
    (7, 25, 31, "1Q決算"),          # 第1四半期前半
    (8,  1, 15, "1Q決算"),          # 第1四半期後半
    (10, 25, 31, "2Q・中間決算"),   # 中間期前半
    (11,  1, 15, "2Q・中間決算"),   # 中間期後半
    (1,  25, 31, "3Q決算"),         # 第3四半期前半
    (2,   1, 15, "3Q決算"),         # 第3四半期後半
]


def _check_earnings_season(target_date: date | None = None) -> dict | None:
    """
    3月末決算企業の決算発表集中期間（年4回）に該当する場合に警告を返す。
    個別銘柄の決算日を調べなくてもシーズン到来を事前に通知できる。
    """
    today = target_date or datetime.now(JST).date()
    for month, d_start, d_end, label in _EARNINGS_SEASONS:
        if today.month == month and d_start <= today.day <= d_end:
            return {
                "level": "warning",
                "code":  "EARNINGS_SEASON",
                "msg":   f"決算シーズン中 ({label} / 3月決算企業)",
            }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 追加チェック（yfinance データを1回取得してまとめて判定）
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_ohlcv(symbol: str, period: str = "6mo"):
    """yfinance から OHLCV を取得。失敗時は None。"""
    try:
        import yfinance as yf
        return yf.Ticker(symbol).history(period=period, auto_adjust=True)
    except Exception:
        return None


def _check_consecutive_decline(hist) -> dict | None:
    """連続陰線: 終値が3日以上連続で前日終値を下回る"""
    if hist is None or len(hist) < 4:
        return None
    closes = hist["Close"].values
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            n += 1
        else:
            break
    if n >= 5:
        return {"level": "danger",  "code": "CONSEC_DECLINE",
                "msg": f"連続陰線 {n}日継続 — 売り圧力持続"}
    if n >= 3:
        return {"level": "warning", "code": "CONSEC_DECLINE",
                "msg": f"連続陰線 {n}日"}
    return None


def _check_ma_death_cross(hist) -> dict | None:
    """MA25 < MA75 かつ株価が MA25 を下回る（デッドクロス後の下落継続）"""
    if hist is None or len(hist) < 80:
        return None
    closes = hist["Close"]
    ma25 = float(closes.rolling(25).mean().iloc[-1])
    ma75 = float(closes.rolling(75).mean().iloc[-1])
    price = float(closes.iloc[-1])
    if ma25 < ma75 and price < ma25:
        pct = (price - ma75) / ma75 * 100
        return {"level": "warning", "code": "MA_DEATH",
                "msg": f"デッドクロス(MA25<MA75) / MA75比{pct:+.1f}%"}
    return None


def _check_gap_down(hist) -> dict | None:
    """直近1営業日の窓開け下落（当日始値 < 前日終値）"""
    if hist is None or len(hist) < 2:
        return None
    prev_close = float(hist["Close"].iloc[-2])
    today_open = float(hist["Open"].iloc[-1])
    if prev_close <= 0:
        return None
    gap_pct = (today_open - prev_close) / prev_close * 100
    if gap_pct <= -3.0:
        return {"level": "danger",  "code": "GAP_DOWN",
                "msg": f"ギャップダウン {gap_pct:.1f}% (本日始値)"}
    if gap_pct <= -1.5:
        return {"level": "warning", "code": "GAP_DOWN",
                "msg": f"窓開け下落 {gap_pct:.1f}% (本日始値)"}
    return None


def _check_volume_spike(hist) -> dict | None:
    """
    直近5日の出来高が過去15日平均の2倍以上 → 警告。
    分析結果: 3倍超=勝率64%/損切21%, 2-3倍=PF2.18 と有意に悪化。
    価格方向は問わない（上昇中の群衆参加も危険）。
    """
    try:
        if hist is None or len(hist) < 20:
            return None
        recent_vol   = float(hist["Volume"].iloc[-5:].mean())
        baseline_vol = float(hist["Volume"].iloc[-20:-5].mean())
        if baseline_vol <= 0:
            return None
        ratio = recent_vol / baseline_vol
        if ratio >= 3.0:
            return {"level": "danger",  "code": "VOL_SPIKE",
                    "msg": f"出来高急増 {ratio:.1f}倍 — 群衆参加/損切率↑"}
        if ratio >= 2.0:
            return {"level": "warning", "code": "VOL_SPIKE",
                    "msg": f"出来高増加 {ratio:.1f}倍 — PF低下傾向"}
    except Exception:
        pass
    return None


def _check_margin_ratio(symbol: str) -> dict | None:
    """
    信用倍率（信用買い残 ÷ 信用売り残）を株探から取得。
    高倍率 + 下落中 → 追証（マージンコール）による強制売りリスク。
    """
    code = symbol.replace(".T", "").replace(".t", "")
    try:
        html  = _kabutan_get(f"https://kabutan.jp/stock/?code={code}")
        plain = re.sub(r'<[^>]+>', ' ', html)
        plain = re.sub(r'\s+', ' ', plain)
        # 「信用倍率」の直後にある数値を取得
        m = re.search(r'信用倍率\s*(\d+\.?\d*)', plain)
        if not m:
            return None
        ratio = float(m.group(1))
        if ratio >= 10.0:
            return {"level": "danger",  "code": "MARGIN_HIGH",
                    "msg": f"信用倍率 {ratio:.1f}倍 — 追証リスク高"}
        if ratio >= 5.0:
            return {"level": "warning", "code": "MARGIN_HIGH",
                    "msg": f"信用倍率 {ratio:.1f}倍 — 追証リスク注意"}
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 一括事前計算
# ─────────────────────────────────────────────────────────────────────────────

def _compute_one(symbol: str, name: str, target_date: date | None) -> list[dict]:
    # yfinance データを1回だけ取得（複数チェックで共有）
    hist = _fetch_ohlcv(symbol, period="6mo")

    flags: list[dict] = []
    for fn in [
        lambda: _check_negative_news(symbol, name),
        lambda: _check_earnings_proximity(symbol, target_date),
        lambda: _check_earnings_season(target_date),
        lambda: _check_volume_price_divergence(hist),
        lambda: _check_atr_spike(hist),
        lambda: _check_consecutive_decline(hist),
        lambda: _check_ma_death_cross(hist),
        lambda: _check_gap_down(hist),
        lambda: _check_volume_spike(hist),
        lambda: _check_margin_ratio(symbol),
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
    シグナル銘柄のリスクフラグを並列で計算し RISK_FLAGS に格納する。

    当日キャッシュ (.risk_check_cache_YYYY-MM-DD.json) が存在する場合、
    既にキャッシュ済みの銘柄はスキップし、未キャッシュ分のみ計算する。

    Args:
        symbols_names: [(symbol, name), ...]  例 [("7203.T", "トヨタ"), ...]
        workers:       並列数
        target_date:   判定基準日 (None = 今日)
    """
    if not symbols_names:
        return

    # 日次キャッシュをロード
    cache = _load_daily_cache(target_date)
    cached_flags   = cache.get("RISK_FLAGS",     {})
    cached_earnings = cache.get("EARNINGS_DATES", {})

    # キャッシュ済み銘柄はそのまま復元
    for sym, nm in symbols_names:
        if sym in cached_earnings:
            if cached_flags.get(sym):
                RISK_FLAGS[sym] = cached_flags[sym]
            EARNINGS_DATES[sym] = cached_earnings[sym]

    # 未キャッシュ分のみ計算
    todo = [(sym, nm) for sym, nm in symbols_names if sym not in cached_earnings]
    if todo:
        n = len(todo)
        total = len(symbols_names)
        skipped = total - n
        skip_msg = f" (キャッシュ{skipped}銘柄スキップ)" if skipped else ""
        print(f"リスクチェック + 決算日取得中 ({n}銘柄){skip_msg}...", flush=True)

        def _compute_full(sym: str, nm: str) -> tuple[list[dict], str]:
            flags   = _compute_one(sym, nm, target_date)
            earn_dt = _fetch_next_earnings_date(sym, target_date)
            return flags, earn_dt

        with ThreadPoolExecutor(max_workers=min(workers, n)) as ex:
            futs = {
                ex.submit(_compute_full, sym, nm): sym
                for sym, nm in todo
            }
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    flags, earn_dt = fut.result()
                    if flags:
                        RISK_FLAGS[sym] = flags
                    EARNINGS_DATES[sym] = earn_dt
                except Exception:
                    EARNINGS_DATES[sym] = ""

        # キャッシュに追記して保存
        merged = {
            "RISK_FLAGS":     dict(cached_flags,     **{k: v for k, v in RISK_FLAGS.items()     if k in {s for s, _ in todo}}),
            "EARNINGS_DATES": dict(cached_earnings,  **{k: v for k, v in EARNINGS_DATES.items() if k in {s for s, _ in todo}}),
        }
        _save_daily_cache(merged, target_date)
    else:
        print(f"リスクチェック: 全{len(symbols_names)}銘柄キャッシュ済みスキップ", flush=True)

    warn_count = sum(len(v) for v in RISK_FLAGS.values())
    earn_count = sum(1 for v in EARNINGS_DATES.values() if v)
    print(f"リスクチェック完了: {warn_count}件の警告 / 決算日取得: {earn_count}銘柄", flush=True)


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


def render_earnings_date(symbol: str, target_date: date | None = None) -> str:
    """
    次回決算日を小さなテキストで返す。
    - 取得できた場合: 日付 + 残り日数（7日以内→赤、14日以内→黄、それ以外→グレー）
    - 取得できなかった場合: 「📅 決算日: 取得不可」をグレーで表示
    - precompute_all() 未実行の場合: 何も表示しない
    """
    # symbol が dict にない = precompute_all() 未実行 → 表示しない
    if symbol not in EARNINGS_DATES:
        return ""

    dt_str = EARNINGS_DATES[symbol]

    # 取得できなかった場合 → 株探リンクを表示
    if not dt_str:
        code = symbol.replace(".T", "").replace(".t", "")
        return (
            f'<br><span style="color:#475569;font-size:10px">'
            f'📅 決算日: <a href="https://kabutan.jp/stock/finance?code={code}" '
            f'target="_blank" style="color:#475569;text-decoration:underline">'
            f'株探で確認</a></span>'
        )

    try:
        earn_dt = date.fromisoformat(dt_str)
        today   = target_date or datetime.now(JST).date()
        diff    = (earn_dt - today).days
        if diff < 0:
            diff_label = f"{abs(diff)}日前"
            color = "#64748b"
        elif diff == 0:
            diff_label = "本日！"
            color = "#ef4444"
        elif diff <= 7:
            diff_label = f"あと{diff}日"
            color = "#ef4444"
        elif diff <= 14:
            diff_label = f"あと{diff}日"
            color = "#fbbf24"
        else:
            diff_label = f"あと{diff}日"
            color = "#64748b"
        return (
            f'<br><span style="color:{color};font-size:10px">'
            f'📅 決算: {dt_str} ({diff_label})</span>'
        )
    except Exception:
        return (
            f'<br><span style="color:#64748b;font-size:10px">'
            f'📅 決算: {dt_str}</span>'
        )


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
