"""HTML テンプレート。文字列連結だけで完結させ、外部依存を作らない。"""

from __future__ import annotations

import html

SITE_NAME = "連休カレンダー"
SITE_DESC = "日本の祝日と連休を、法律にもとづいて自動計算。次の連休までの日数がひと目で分かります。"
BASE_URL = "https://example.com"  # 独自ドメイン取得後にここを書き換える

WD = "月火水木金土日"


def e(s: str) -> str:
    return html.escape(str(s), quote=True)


def page(
    *,
    title: str,
    description: str,
    body: str,
    path: str,
    depth: int,
    jsonld: str = "",
    extra_head: str = "",
) -> str:
    """1ページ分の完全な HTML を返す。

    depth はルートからの階層数。相対パスで static を参照するために使う
    (サブディレクトリ配信でも壊れないようにするため)。
    """
    root = "../" * depth if depth else "./"
    canonical = f"{BASE_URL}{path}"
    full_title = title if title == SITE_NAME else f"{title} | {SITE_NAME}"

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(full_title)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{e(canonical)}">
<meta property="og:title" content="{e(full_title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{e(canonical)}">
<meta property="og:site_name" content="{e(SITE_NAME)}">
<meta name="twitter:card" content="summary">
<link rel="stylesheet" href="{root}static/style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📅</text></svg>">
{jsonld}
{extra_head}
</head>
<body>
<header class="site-header">
  <a class="brand" href="{root}">📅 {e(SITE_NAME)}</a>
  <nav>
    <a href="{root}">ホーム</a>
    <a href="{root}holiday/">祝日一覧</a>
    <a href="{root}about/">このサイトについて</a>
  </nav>
</header>
<main>
{body}
</main>
<footer class="site-footer">
  <p>祝日は「国民の祝日に関する法律」にもとづき自動計算しています。
     春分の日・秋分の日は前年2月の官報で正式に確定するため、
     それ以降の年は予測値です。</p>
  <p class="muted">© {e(SITE_NAME)}</p>
</footer>
</body>
</html>
"""


def breadcrumb(items: list[tuple[str, str]]) -> str:
    """[(ラベル, href or '')] からパンくずを作る。href が空なら現在地。"""
    parts = []
    for label, href in items:
        if href:
            parts.append(f'<a href="{e(href)}">{e(label)}</a>')
        else:
            parts.append(f"<span>{e(label)}</span>")
    return f'<nav class="crumb">{" › ".join(parts)}</nav>'


def affiliate_block(context: str) -> str:
    """広告・アフィリエイトの差し込み位置。

    実際のリンクはASP審査通過後に差し替える。今は場所と文脈だけ確保しておく。
    """
    return f"""<aside class="ad-slot">
  <p class="ad-label">PR</p>
  <p>{e(context)}</p>
  <!-- ここにアフィリエイトリンクを差し込む (楽天トラベル / じゃらん / 高速バス など) -->
</aside>"""


def renkyu_card(r, root: str) -> str:
    tag = f'<span class="season">{e(r.season)}</span>' if r.season else ""
    names = "・".join(r.holiday_names)
    return f"""<a class="card" href="{root}renkyu/{r.slug}/">
  <div class="card-head">
    <span class="len">{r.length}<small>連休</small></span>
    {tag}
  </div>
  <div class="card-body">
    <p class="range">{e(r.range_label)}</p>
    <p class="names">{e(names)}</p>
  </div>
</a>"""


def holiday_row(h, root: str, info) -> str:
    slug = info["slug"]
    name_cell = (
        f'<a href="{root}holiday/{slug}/">{e(h.name)}</a>' if slug else e(h.name)
    )
    cls = "sat" if h.date.weekday() == 5 else ("sun" if h.date.weekday() == 6 else "")
    return f"""<tr>
  <td class="date">{h.date.month}月{h.date.day}日</td>
  <td class="wd {cls}">{WD[h.date.weekday()]}</td>
  <td>{name_cell}</td>
</tr>"""
