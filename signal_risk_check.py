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
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# ── 日次キャッシュファイル ────────────────────────────────────────────────────
_CACHE_DIR = Path(".")
def _daily_cache_path(target_date: date | None = None) -> Path:
    d = target_date or datetime.now(JST).date()
    return _CACHE_DIR / f".risk_check_cache_{d}.json"

def _load_daily_cache(target_date: date | None = None) -> dict:
    p = _daily_cache_path(target_date)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_daily_cache(data: dict, target_date: date | None = None) -> None:
    p = _daily_cache_path(target_date)
    try:
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
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
            # 今日から3か月以内の未来日だけ候補に
            if today <= dt <= today + timedelta(days=120):
                candidates.append(dt)
        except Exception:
            pass

    if candidates:
        return str(min(candidates))
    return ""


def _fetch_next_earnings_date(symbol: str, target_date: date | None = None) -> str:
    """
    株探の財務ページから次回決算発表予定日を取得して "YYYY-MM-DD" を返す。
    取得できなければ空文字を返す。
    """
    code  = symbol.replace(".T", "").replace(".t", "")
    today = target_date or datetime.now(JST).date()

    # 試すURL: finance ページ → stock トップページ の順
    urls = [
        f"https://kabutan.jp/stock/finance?code={code}",
        f"https://kabutan.jp/stock/?code={code}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=_KABUTAN_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                html = r.read().decode("utf-8", errors="replace")
            result = _parse_date_from_html(html, today)
            if result:
                return result
        except Exception:
            pass
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

    # 取得できなかった場合
    if not dt_str:
        return (
            '<br><span style="color:#475569;font-size:10px">'
            '📅 決算日: 取得不可</span>'
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
