"""
fetch_signal_news.py — シグナル銘柄のニュース・SNS情報取得

使用ライブラリ:
  - yfinance (pip install yfinance) → 英語ニュース
  - urllib.request + xml.etree.ElementTree (stdlib) → Google News RSS (日本語)
"""
from __future__ import annotations

import html as _html_mod
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

JST = timezone(timedelta(hours=9))

# ── センチメントキーワード ────────────────────────────────────────────────────
POSITIVE_JP = [
    "増益", "上昇", "買い推奨", "好調", "最高値", "増配", "TOB", "黒字化",
    "好業績", "急伸", "上方修正", "過去最高", "強気", "買い場", "大幅高",
    "増収", "目標株価引き上げ", "レーティング引き上げ", "好決算", "自社株買い",
]
NEGATIVE_JP = [
    "減益", "下落", "売り推奨", "赤字", "不正", "急落", "損失", "リストラ",
    "下方修正", "経営危機", "倒産", "警告", "減収", "目標株価引き下げ",
    "レーティング引き下げ", "業績悪化", "大幅安", "不祥事", "行政処分",
]
POSITIVE_EN = [
    "beat", "upgrade", "buy", "outperform", "record", "strong", "growth",
    "raised", "positive", "surge", "rally", "profit",
]
NEGATIVE_EN = [
    "miss", "downgrade", "sell", "underperform", "loss", "weak", "decline",
    "cut", "warning", "crash", "plunge", "deficit",
]


def sentiment_score(titles: list[str]) -> float:
    """
    タイトルリストからセンチメントスコアを計算。
    -1 (完全ネガティブ) から +1 (完全ポジティブ)。
    キーワードマッチ数で正規化する。
    """
    if not titles:
        return 0.0
    pos = 0
    neg = 0
    for title in titles:
        tl = title.lower()
        for kw in POSITIVE_JP:
            if kw in title:
                pos += 1
        for kw in NEGATIVE_JP:
            if kw in title:
                neg += 1
        for kw in POSITIVE_EN:
            if kw in tl:
                pos += 1
        for kw in NEGATIVE_EN:
            if kw in tl:
                neg += 1
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def load_and_apply_model(
    symbol: str,
    name: str,
    entry_date,
    bt_score: int,
    skip_news: bool = False,
) -> dict:
    """
    news_model.json を読み込み、指定シグナルの予測スコアを返す。

    Args:
        symbol:     例 "7203.T"
        name:       例 "トヨタ自動車"
        entry_date: date オブジェクト または "YYYY-MM-DD" 文字列
        bt_score:   BTスコア (0-100)
        skip_news:  True なら Google News 取得をスキップ

    Returns:
        {
          "news_score":          float,   # ニューススコア = 感情 × 記事数/10
          "predicted_win_prob":  float,   # モデル予測勝率 (0-1)
          "news_count":          int,     # ニュース記事数
          "news_sentiment":      float,   # センチメントスコア (-1 to +1)
        }
    """
    try:
        from news_sentiment_model import load_and_apply_model as _lam
        return _lam(symbol, name, entry_date, bt_score, skip_news=skip_news)
    except Exception:
        pass
    # フォールバック: モデルなし
    return dict(news_score=0.0, predicted_win_prob=0.5, news_count=0, news_sentiment=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# ニュース取得
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yfinance_news(symbol: str, max_items: int = 5) -> list[dict]:
    """yfinance から最新ニュースを取得 (英語)"""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        raw = ticker.news or []
        result: list[dict] = []
        for n in raw[:max_items * 2]:
            # yfinance の新旧フォーマット両対応
            content = n.get("content", n)
            title   = (content.get("title") or n.get("title", "")).strip()
            url_val = (
                (content.get("canonicalUrl") or {}).get("url")
                or content.get("url")
                or n.get("link")
                or n.get("url", "")
            )
            publisher = (
                (content.get("provider") or {}).get("displayName")
                or n.get("publisher", "")
            )
            pub_raw = content.get("pubDate") or n.get("providerPublishTime", "")
            if isinstance(pub_raw, (int, float)):
                dt      = datetime.fromtimestamp(pub_raw, tz=JST)
                pub_str = dt.strftime("%m/%d %H:%M")
            else:
                pub_str = str(pub_raw)[:16] if pub_raw else ""

            if title:
                result.append({
                    "title":  title,
                    "url":    url_val,
                    "source": publisher,
                    "date":   pub_str,
                    "lang":   "en",
                })
            if len(result) >= max_items:
                break
        return result
    except Exception:
        return []


def fetch_google_news_rss(query: str, max_items: int = 5) -> list[dict]:
    """Google News RSS から日本語ニュースを取得"""
    try:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://news.google.com/rss/search"
            f"?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; gobot-news/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        root  = ET.fromstring(data)
        items = root.findall(".//item")
        result: list[dict] = []
        for item in items[:max_items]:
            title    = (item.findtext("title") or "").strip()
            link     = (item.findtext("link")  or "").strip()
            pub_date = (item.findtext("pubDate") or "")[:16]
            source_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
            src_name = ""
            if source_el is None:
                src_name = item.findtext("source") or "Google News"
            if title:
                # タイトル末尾の " - メディア名" を分離
                if " - " in title:
                    parts    = title.rsplit(" - ", 1)
                    title    = parts[0].strip()
                    src_name = parts[1].strip()
                result.append({
                    "title":  title,
                    "url":    link,
                    "source": src_name or "Google News",
                    "date":   pub_date,
                    "lang":   "ja",
                })
        return result
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SNS リンク生成
# ─────────────────────────────────────────────────────────────────────────────

def social_links(symbol: str, name: str) -> dict[str, str]:
    """各 SNS / 掲示板への検索リンクを返す"""
    q_name   = urllib.parse.quote(f"{name} 株")
    q_sym    = urllib.parse.quote(f"{symbol} 株")
    sym_bare = symbol.replace(".T", "")

    return {
        "twitter":    f"https://twitter.com/search?q={q_name}&f=live",
        "yf_bbs":     f"https://finance.yahoo.co.jp/quote/{symbol}/bbs",
        "yf_chart":   f"https://finance.yahoo.co.jp/quote/{symbol}/chart",
        "stocktwits":f"https://stocktwits.com/symbol/{sym_bare}.T",
        "kabutan":    f"https://kabutan.jp/stock/news?code={sym_bare}",
        "nikkei":     f"https://www.nikkei.com/search?query={urllib.parse.quote(name)}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1銘柄分のデータ収集
# ─────────────────────────────────────────────────────────────────────────────

def fetch_one(symbol: str, name: str) -> dict:
    """yfinance + Google News RSS でニュース収集"""
    yf_news  = fetch_yfinance_news(symbol, max_items=4)
    gn_news  = fetch_google_news_rss(f"{name} 株", max_items=5)
    links    = social_links(symbol, name)
    return {
        "symbol":   symbol,
        "name":     name,
        "yf_news":  yf_news,
        "gn_news":  gn_news,
        "links":    links,
    }


def fetch_all(signal_stocks: list[tuple], workers: int = 4) -> list[dict]:
    """並列でニュースを取得する。signal_stocks = [(symbol, name, bt_score), ...]"""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(signal_stocks) or 1)) as ex:
        futs = {
            ex.submit(fetch_one, sym, nm): (sym, nm, bt)
            for sym, nm, bt in signal_stocks
        }
        for fut in as_completed(futs):
            sym, nm, bt = futs[fut]
            try:
                data = fut.result()
                data["bt_score"] = bt
            except Exception:
                data = {
                    "symbol": sym, "name": nm, "bt_score": bt,
                    "yf_news": [], "gn_news": [], "links": social_links(sym, nm),
                }
            results.append(data)
    # BT スコア降順でソート
    results.sort(key=lambda x: -(x.get("bt_score") or 0))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# HTML 生成
# ─────────────────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return _html_mod.escape(str(s))


def _news_list_html(items: list[dict], lang_icon: str) -> str:
    if not items:
        return '<p style="color:#64748b;font-size:0.78rem;margin:4px 0">取得できませんでした</p>'
    rows = ""
    for it in items:
        title  = _esc(it.get("title", ""))
        url    = _esc(it.get("url", "#"))
        source = _esc(it.get("source", ""))
        date   = _esc(it.get("date", ""))
        rows += (
            f'<li style="margin:5px 0;line-height:1.45">'
            f'<a href="{url}" target="_blank" rel="noopener" '
            f'style="color:#93c5fd;text-decoration:none;font-size:0.8rem">'
            f'{lang_icon} {title}</a>'
            f'<span style="color:#64748b;font-size:0.72rem;margin-left:6px">'
            f'{source} {date}</span></li>\n'
        )
    return f'<ul style="padding-left:16px;margin:4px 0">{rows}</ul>'


def build_news_html(signal_stocks: list[tuple], workers: int = 4) -> str:
    """ニュース・情報タブ全体の HTML を生成"""
    if not signal_stocks:
        return '<p style="color:#94a3b8;padding:24px">シグナル銘柄なし</p>'

    print(f"ニュース取得中 ({len(signal_stocks)}銘柄)...", flush=True)
    all_data = fetch_all(signal_stocks, workers=workers)

    # ── カード CSS ──
    card_css = """
<style>
.news-grid {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(360px,1fr));
  gap:16px; padding:12px 0;
}
.news-card {
  background:#0f172a; border:1px solid #1e293b;
  border-radius:10px; padding:14px 16px;
}
.news-card-header {
  display:flex; align-items:baseline; gap:8px;
  border-bottom:1px solid #1e293b; padding-bottom:8px; margin-bottom:10px;
}
.news-sym  { font-size:1.0rem; font-weight:700; color:#e2e8f0; }
.news-name { font-size:0.8rem; color:#94a3b8; }
.news-bt   { font-size:0.75rem; color:#fbbf24; margin-left:auto; }
.news-section-label {
  font-size:0.72rem; font-weight:700; color:#64748b;
  text-transform:uppercase; letter-spacing:.06em;
  margin:8px 0 2px;
}
.social-links {
  display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; padding-top:8px;
  border-top:1px solid #1e293b;
}
.social-link {
  padding:3px 10px; border-radius:4px; font-size:0.72rem;
  text-decoration:none; font-weight:600;
}
.sl-twitter    { background:#1d9bf0; color:#fff; }
.sl-yfbbs      { background:#ff0032; color:#fff; }
.sl-chart      { background:#22c55e; color:#fff; }
.sl-stocktwits { background:#f26522; color:#fff; }
.sl-kabutan    { background:#3b82f6; color:#fff; }
.sl-nikkei     { background:#c0392b; color:#fff; }
</style>
"""

    # ── カードごとの HTML ──
    cards = ""
    for d in all_data:
        sym    = d["symbol"]
        name   = d["name"]
        bt     = d.get("bt_score", 0)
        lnks   = d.get("links", {})
        yn     = d.get("yf_news", [])
        gn     = d.get("gn_news", [])

        tw  = _esc(lnks.get("twitter", "#"))
        ybb = _esc(lnks.get("yf_bbs", "#"))
        yc  = _esc(lnks.get("yf_chart", "#"))
        st  = _esc(lnks.get("stocktwits", "#"))
        kb  = _esc(lnks.get("kabutan", "#"))
        nk  = _esc(lnks.get("nikkei", "#"))

        yf_html = _news_list_html(yn, "🇺🇸")
        gn_html = _news_list_html(gn, "🇯🇵")

        cards += f"""
<div class="news-card">
  <div class="news-card-header">
    <span class="news-sym">{_esc(sym)}</span>
    <span class="news-name">{_esc(name)}</span>
    <span class="news-bt">BT:{bt}</span>
  </div>

  <div class="news-section-label">📰 日本語ニュース (Google News)</div>
  {gn_html}

  <div class="news-section-label">🌐 英語ニュース (Yahoo Finance)</div>
  {yf_html}

  <div class="social-links">
    <a class="social-link sl-twitter"    href="{tw}"  target="_blank" rel="noopener">🐦 X/Twitter</a>
    <a class="social-link sl-yfbbs"      href="{ybb}" target="_blank" rel="noopener">💬 YF掲示板</a>
    <a class="social-link sl-chart"      href="{yc}"  target="_blank" rel="noopener">📈 YFチャート</a>
    <a class="social-link sl-kabutan"    href="{kb}"  target="_blank" rel="noopener">📊 株探</a>
    <a class="social-link sl-nikkei"     href="{nk}"  target="_blank" rel="noopener">📋 日経</a>
    <a class="social-link sl-stocktwits" href="{st}"  target="_blank" rel="noopener">💹 StockTwits</a>
  </div>
</div>
"""

    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    return f"""{card_css}
<p style="color:#64748b;font-size:0.78rem;margin:8px 0 4px">
  取得時刻: {ts} &nbsp;|&nbsp; {len(all_data)}銘柄
  <span style="font-size:0.72rem;color:#475569">
    ※ X/Twitter の投稿はリンク先で確認してください (API制限のため直接取得不可)
  </span>
</p>
<div class="news-grid">
{cards}
</div>
"""
