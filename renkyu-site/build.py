#!/usr/bin/env python3
"""静的サイトを生成する。

    python build.py                 # dist/ に出力
    python build.py --out public    # 出力先を変える
    python build.py --years 10      # 何年先まで生成するか

外部ライブラリ不要。GitHub Actions から毎日実行すれば、
「次の連休」の表示が自動で最新になる。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path

import holiday_info
import theme as T
from holidays import holidays_for_year
from renkyu import find_renkyu, next_renkyu

WD = T.WD


def write(out: Path, rel: str, content: str) -> None:
    p = out / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- index

def build_index(out: Path, today: dt.date, years: list[int]) -> None:
    nxt = next_renkyu(today)
    body = []

    if nxt:
        days_left = (nxt.start - today).days
        if days_left > 0:
            eyebrow, num, unit = "次の連休まで", str(days_left), "日"
        elif nxt.end >= today:
            eyebrow, num, unit = "連休", "開催中", ""
        else:
            eyebrow, num, unit = "次の連休", "", ""
        body.append(f"""<section class="hero">
  <p class="eyebrow">{T.e(eyebrow)}</p>
  <p class="countdown">{T.e(num)}<small>{T.e(unit)}</small></p>
  <h1>{T.e(nxt.title)}</h1>
  <p class="range">{T.e(nxt.range_label)}</p>
  <a class="btn" href="renkyu/{nxt.slug}/">この連休の詳細</a>
</section>""")

    body.append(T.affiliate_block(
        "連休の宿は3か月前から埋まりはじめます。日程が決まったら早めの確保がおすすめです。"
    ))

    # 直近2年分の連休を並べる
    for y in years[:2]:
        rs = [r for r in find_renkyu(y, y) if r.end >= today] if y == today.year \
            else find_renkyu(y, y)
        if not rs:
            continue
        body.append(f'<h2>{y}年の連休'
                    f'{"（これから）" if y == today.year else ""}</h2>')
        body.append('<div class="cards">')
        body.extend(T.renkyu_card(r, "") for r in rs)
        body.append("</div>")

    body.append("<h2>年から探す</h2>")
    body.append('<ul class="yearnav">')
    body.extend(f'<li><a href="y/{y}/">{y}年</a></li>' for y in years)
    body.append("</ul>")

    body.append("""<h2>このサイトについて</h2>
<p class="lead">日本の祝日は「国民の祝日に関する法律」で決まっており、
ハッピーマンデー・振替休日・国民の休日まで含めて計算で求められます。
このサイトはその計算を毎日実行して、次の連休と年間の休日を自動で表示しています。</p>
<p>春分の日と秋分の日だけは天文学的に決まるため、正式には前年2月1日の官報で告示されます。
それより先の年は計算による予測値です。</p>""")

    write(out, "index.html", T.page(
        title=T.SITE_NAME,
        description=T.SITE_DESC,
        body="\n".join(body),
        path="/",
        depth=0,
    ))


# ---------------------------------------------------------------- 年ページ

def build_year(out: Path, y: int, years: list[int], today: dt.date) -> None:
    hs = holidays_for_year(y)
    rs = find_renkyu(y, y)
    longest = max(rs, key=lambda r: r.length) if rs else None

    body = [T.breadcrumb([("ホーム", "../../"), (f"{y}年", "")])]
    body.append(f"<h1>{y}年の祝日と連休</h1>")

    summary = f"{y}年の国民の祝日は{len(hs)}日、3日以上の連休は{len(rs)}回あります。"
    if longest:
        summary += f"最も長いのは{longest.range_label}の{longest.length}連休です。"
    body.append(f'<p class="lead">{T.e(summary)}</p>')

    if rs:
        body.append("<h2>連休一覧</h2>")
        body.append('<div class="cards">')
        body.extend(T.renkyu_card(r, "../../") for r in rs)
        body.append("</div>")

    body.append(T.affiliate_block(
        f"{y}年の連休に旅行を計画するなら、早割プランのある時期に押さえておくと有利です。"
    ))

    body.append("<h2>祝日一覧</h2>")
    body.append('<div class="table-wrap"><table>')
    body.append("<thead><tr><th>日付</th><th></th><th>祝日</th></tr></thead><tbody>")
    body.extend(T.holiday_row(h, "../../", holiday_info.get(h.name)) for h in hs)
    body.append("</tbody></table></div>")

    body.append("<h2>年から探す</h2>")
    body.append('<ul class="yearnav">')
    body.extend(
        f'<li><a class="{"on" if yy == y else ""}" href="../{yy}/">{yy}年</a></li>'
        for yy in years
    )
    body.append("</ul>")

    if y > today.year + 1:
        body.append('<p class="note">春分の日・秋分の日は前年2月1日の官報で確定します。'
                    f'{y}年の日付は計算による予測値です。</p>')

    write(out, f"y/{y}/index.html", T.page(
        title=f"{y}年の祝日カレンダーと連休一覧",
        description=summary,
        body="\n".join(body),
        path=f"/y/{y}/",
        depth=2,
    ))


# ---------------------------------------------------------------- 連休ページ

def build_renkyu(out: Path, r, today: dt.date, siblings: list) -> None:
    hset = {h.date: h for h in r.holidays}
    body = [T.breadcrumb([
        ("ホーム", "../../"),
        (f"{r.start.year}年", f"../../y/{r.start.year}/"),
        (r.title, ""),
    ])]

    body.append(f"<h1>{T.e(r.start.year)}年 {T.e(r.title)}</h1>")

    left = (r.start - today).days
    when = (f"あと{left}日で始まります。" if left > 0
            else "現在この連休の期間中です。" if r.end >= today
            else "この連休は終了しています。")
    lead = (f"{r.start.year}年の{r.title}は{r.range_label}の{r.length}日間です。{when}")
    body.append(f'<p class="lead">{T.e(lead)}</p>')

    # 日並び
    body.append('<ul class="strip">')
    for d in r.days:
        h = hset.get(d)
        cls = "hol" if h else ""
        label = h.name if h else ("土曜" if d.weekday() == 5 else "日曜")
        body.append(f'<li class="{cls}"><span class="d">{d.month}/{d.day}</span>'
                    f'<span class="n">{WD[d.weekday()]}・{T.e(label)}</span></li>')
    body.append("</ul>")

    body.append(T.affiliate_block(
        f"{r.length}連休は宿・航空券ともに競争が激しくなります。"
        "日程が固まった段階で仮予約しておくと選択肢が残ります。"
    ))

    if r.holiday_names:
        body.append("<h2>この連休に含まれる祝日</h2>")
        for name in r.holiday_names:
            info = holiday_info.get(name)
            link = (f'<a href="../../holiday/{info["slug"]}/">{T.e(name)}</a>'
                    if info["slug"] else T.e(name))
            body.append(f"<h3>{link}</h3>")
            if info["summary"]:
                body.append(f"<p>{T.e(info['summary'])}</p>")

    # 前後の連休へのリンク (回遊させる)。生成済みのページだけを対象にする。
    idx = next((i for i, x in enumerate(siblings) if x.slug == r.slug), None)
    if idx is not None:
        sib = [x for x in (siblings[idx - 1] if idx > 0 else None,
                           siblings[idx + 1] if idx + 1 < len(siblings) else None) if x]
        if sib:
            body.append("<h2>前後の連休</h2>")
            body.append('<div class="cards">')
            body.extend(T.renkyu_card(x, "../../") for x in sib)
            body.append("</div>")

    jsonld = _event_jsonld(r)
    write(out, f"renkyu/{r.slug}/index.html", T.page(
        title=f"{r.start.year}年 {r.title}（{r.range_label}）",
        description=lead,
        body="\n".join(body),
        path=f"/renkyu/{r.slug}/",
        depth=2,
        jsonld=jsonld,
    ))


def _event_jsonld(r) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": f"{r.start.year}年 {r.title}",
        "startDate": r.start.isoformat(),
        "endDate": r.end.isoformat(),
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {"@type": "Country", "name": "日本"},
    }
    return ('<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False) + "</script>")


# ---------------------------------------------------------------- 祝日ページ

def build_holiday_pages(out: Path, years: list[int]) -> None:
    # 生成対象の年に実際に登場する祝日だけをページ化する
    occur: dict[str, list[dt.date]] = {}
    for y in years:
        for h in holidays_for_year(y):
            occur.setdefault(h.name, []).append(h.date)

    index_rows = []
    for name, dates in occur.items():
        info = holiday_info.get(name)
        if not info["slug"]:
            continue
        _build_one_holiday(out, name, info, dates)
        index_rows.append(
            f'<tr><td><a href="{info["slug"]}/">{T.e(name)}</a></td>'
            f'<td>{T.e(info["summary"])}</td></tr>'
        )

    body = [T.breadcrumb([("ホーム", "../"), ("祝日一覧", "")])]
    body.append("<h1>国民の祝日の一覧と意味</h1>")
    body.append('<p class="lead">日本の国民の祝日は現在16日あります。'
                'それぞれの祝日には「国民の祝日に関する法律」第2条に趣旨が定められています。</p>')
    body.append('<div class="table-wrap"><table>')
    body.append("<thead><tr><th>祝日</th><th>法律上の趣旨</th></tr></thead><tbody>")
    body.extend(index_rows)
    body.append("</tbody></table></div>")

    write(out, "holiday/index.html", T.page(
        title="国民の祝日の一覧と意味",
        description="日本の国民の祝日をすべて一覧にし、法律上の趣旨と制定経緯をまとめています。",
        body="\n".join(body),
        path="/holiday/",
        depth=1,
    ))


def _build_one_holiday(out: Path, name: str, info: dict, dates: list[dt.date]) -> None:
    body = [T.breadcrumb([
        ("ホーム", "../../"), ("祝日一覧", "../"), (name, ""),
    ])]
    body.append(f"<h1>{T.e(name)}とは</h1>")
    if info["summary"]:
        body.append(f'<p class="lead">{T.e(info["summary"])}</p>')
    if info["note"]:
        body.append(f"<p>{T.e(info['note'])}</p>")
    if info["since"]:
        body.append(f'<p class="muted">{info["since"]}年から実施。</p>')

    body.append(f"<h2>{T.e(name)}はいつ？</h2>")
    body.append('<div class="table-wrap"><table>')
    body.append("<thead><tr><th>年</th><th>日付</th><th></th></tr></thead><tbody>")
    for d in dates:
        body.append(
            f'<tr><td class="date"><a href="../../y/{d.year}/">{d.year}年</a></td>'
            f"<td class=\"date\">{d.month}月{d.day}日</td>"
            f'<td class="wd">{WD[d.weekday()]}</td></tr>'
        )
    body.append("</tbody></table></div>")

    write(out, f"holiday/{info['slug']}/index.html", T.page(
        title=f"{name}とは｜意味・由来と何年何月何日か",
        description=f"{name}の法律上の趣旨、制定の経緯、各年の日付を一覧でまとめています。",
        body="\n".join(body),
        path=f"/holiday/{info['slug']}/",
        depth=2,
    ))


# ---------------------------------------------------------------- その他

def build_about(out: Path) -> None:
    body = [T.breadcrumb([("ホーム", "../"), ("このサイトについて", "")])]
    body.append("""<h1>このサイトについて</h1>
<p class="lead">日本の祝日と連休を、法律の条文にもとづいて計算し、自動で公開しています。</p>

<h2>祝日の計算方法</h2>
<p>「国民の祝日に関する法律」(昭和23年法律第178号) と、その後の改正内容を
そのままプログラムとして実装しています。具体的には次のものを扱っています。</p>
<ul>
  <li>法律で日付が固定されている祝日 (元日、憲法記念日など)</li>
  <li>ハッピーマンデー制度で第◯月曜日に移動した祝日 (成人の日、海の日、敬老の日、スポーツの日)</li>
  <li>天文学的に決まる春分の日・秋分の日</li>
  <li>祝日が日曜にあたるときの振替休日 (2007年改正後のルール)</li>
  <li>祝日に挟まれた平日を休日とする国民の休日</li>
  <li>2019年の即位関連の休日、2020・2021年のオリンピックに伴う祝日移動などの特例</li>
</ul>

<h2>正確性について</h2>
<p>過去の実際の祝日と照合して検証していますが、次の点にご注意ください。</p>
<ul>
  <li><strong>春分の日・秋分の日は予測値を含みます。</strong>
      これらは国立天文台の計算にもとづき、前年2月1日の官報で正式に告示されます。
      それより先の年の日付は近似式による計算結果です。</li>
  <li><strong>将来の法改正は反映できません。</strong>
      新しい祝日の追加や日付の変更が行われた場合、このサイトの計算も更新が必要です。</li>
  <li>年末年始やお盆は法律上の祝日ではありませんが、多くの企業が休業します。
      当サイトの連休判定は法律上の休日 (祝日・土日) のみを対象としています。</li>
</ul>
<p>重要な予定を立てる際は、内閣府が公表している
「国民の祝日について」のページもあわせてご確認ください。</p>

<h2>更新について</h2>
<p>このサイトは毎日自動で再生成されており、「次の連休まであと何日」の表示は常に最新です。</p>""")

    write(out, "about/index.html", T.page(
        title="このサイトについて",
        description="祝日の計算方法、対応している制度、正確性の注意点を説明しています。",
        body="\n".join(body),
        path="/about/",
        depth=1,
    ))


def build_meta(out: Path, years: list[int], renkyus: list, today: dt.date) -> None:
    urls = ["/", "/about/", "/holiday/"]
    urls += [f"/y/{y}/" for y in years]
    urls += [f"/renkyu/{r.slug}/" for r in renkyus]
    urls += [
        f"/holiday/{holiday_info.get(n)['slug']}/"
        for n in {h.name for y in years for h in holidays_for_year(y)}
        if holiday_info.get(n)["slug"]
    ]

    lastmod = today.isoformat()
    items = "\n".join(
        f"  <url><loc>{T.BASE_URL}{u}</loc><lastmod>{lastmod}</lastmod></url>"
        for u in urls
    )
    write(out, "sitemap.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
          f"{items}\n</urlset>\n")

    write(out, "robots.txt",
          f"User-agent: *\nAllow: /\n\nSitemap: {T.BASE_URL}/sitemap.xml\n")

    # 他所から再利用できるよう JSON でも出しておく
    data = {
        "generated": today.isoformat(),
        "years": {
            str(y): [
                {"date": h.date.isoformat(), "name": h.name, "kind": h.kind}
                for h in holidays_for_year(y)
            ]
            for y in years
        },
    }
    write(out, "holidays.json", json.dumps(data, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist", help="出力ディレクトリ")
    ap.add_argument("--years", type=int, default=6, help="何年先まで生成するか")
    ap.add_argument("--today", help="基準日 (YYYY-MM-DD)。デバッグ用")
    args = ap.parse_args()

    today = (dt.date.fromisoformat(args.today) if args.today
             else dt.date.today())
    years = list(range(today.year, today.year + args.years + 1))

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    shutil.copytree("static", out / "static")

    renkyus = find_renkyu(years[0], years[-1])

    build_index(out, today, years)
    for y in years:
        build_year(out, y, years, today)
    for r in renkyus:
        build_renkyu(out, r, today, renkyus)
    build_holiday_pages(out, years)
    build_about(out)
    build_meta(out, years, renkyus, today)

    n = sum(1 for _ in out.rglob("*.html"))
    print(f"生成完了: {n} ページ -> {out}/  (基準日 {today})")


if __name__ == "__main__":
    main()
