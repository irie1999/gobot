"""日本の国民の祝日を計算する。

「国民の祝日に関する法律」(昭和23年法律第178号) と、その改正内容を
そのまま実装している。外部 API やスクレイピングに依存しないので、
ネットワークが無くても、また将来 API が消えても動き続ける。

対応範囲: 1948年(法施行) 〜 2099年
春分・秋分は近似式のため、遠い未来は誤差が出る (詳細は calc_equinox 参照)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# 法律の施行日
LAW_START_YEAR = 1948
# 春分・秋分の近似式が使える上限
MAX_YEAR = 2099


@dataclass(frozen=True)
class Holiday:
    date: dt.date
    name: str
    # "statutory" = 法律で定められた祝日そのもの
    # "substitute" = 振替休日
    # "citizens"   = 国民の休日 (祝日に挟まれた平日)
    kind: str = "statutory"


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> dt.date:
    """その月の第 nth 週の weekday (月曜=0) の日付を返す。"""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (nth - 1))


def calc_equinox(year: int, spring: bool) -> int:
    """春分日 / 秋分日を近似式で求める。

    国立天文台が官報で公表する値の近似。1900〜2099 年で実用上一致する
    とされる式を使っている。ただし公式には前年2月の官報で確定するため、
    数十年先の値は「予測」であることに注意 (サイト上でも明記している)。
    """
    if spring:
        base = 20.8431 if year >= 1980 else 20.8357
    else:
        base = 23.2488 if year >= 1980 else 23.2588
    return int(base + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _statutory_holidays(year: int) -> list[Holiday]:
    """振替休日・国民の休日を除いた、法定の祝日を列挙する。"""
    h: list[Holiday] = []

    def add(month: int, day: int, name: str) -> None:
        h.append(Holiday(dt.date(year, month, day), name))

    add(1, 1, "元日")

    # 成人の日: 2000年から1月第2月曜 (ハッピーマンデー制度)
    if year >= 2000:
        h.append(Holiday(_nth_weekday(year, 1, 0, 2), "成人の日"))
    else:
        add(1, 15, "成人の日")

    # 建国記念の日: 1967年から
    if year >= 1967:
        add(2, 11, "建国記念の日")

    # 天皇誕生日: 在位する天皇によって日付が変わる
    if year >= 2020:
        add(2, 23, "天皇誕生日")      # 今上天皇
    elif 1989 <= year <= 2018:
        add(12, 23, "天皇誕生日")     # 上皇 (2019年は在位の関係で祝日なし)
    elif year <= 1988:
        add(4, 29, "天皇誕生日")      # 昭和天皇

    h.append(Holiday(dt.date(year, 3, calc_equinox(year, True)), "春分の日"))

    # 4/29 の呼び名の変遷
    if year >= 2007:
        add(4, 29, "昭和の日")
    elif year >= 1989:
        add(4, 29, "みどりの日")

    add(5, 3, "憲法記念日")

    # 5/4: 2007年から「みどりの日」として正式な祝日に
    # (1988〜2006年は「国民の休日」として休みだったが祝日ではない)
    if year >= 2007:
        add(5, 4, "みどりの日")

    add(5, 5, "こどもの日")

    # 海の日: 1996年新設 → 2003年からハッピーマンデー
    if year >= 2003:
        h.append(Holiday(_nth_weekday(year, 7, 0, 3), "海の日"))
    elif year >= 1996:
        add(7, 20, "海の日")

    # 山の日: 2016年新設
    if year >= 2016:
        add(8, 11, "山の日")

    # 敬老の日: 1966年新設 → 2003年からハッピーマンデー
    if year >= 2003:
        h.append(Holiday(_nth_weekday(year, 9, 0, 3), "敬老の日"))
    elif year >= 1966:
        add(9, 15, "敬老の日")

    h.append(Holiday(dt.date(year, 9, calc_equinox(year, False)), "秋分の日"))

    # 体育の日 → スポーツの日 (2020年改称)
    if year >= 2020:
        h.append(Holiday(_nth_weekday(year, 10, 0, 2), "スポーツの日"))
    elif year >= 2000:
        h.append(Holiday(_nth_weekday(year, 10, 0, 2), "体育の日"))
    elif year >= 1966:
        add(10, 10, "体育の日")

    add(11, 3, "文化の日")
    add(11, 23, "勤労感謝の日")

    h = _apply_special_years(year, h)
    return sorted(h, key=lambda x: x.date)


def _apply_special_years(year: int, h: list[Holiday]) -> list[Holiday]:
    """特例法で一度だけ動いた年を補正する。

    2019年: 天皇の即位に伴う祝日 (特例法)
    2020/2021年: 東京オリンピック・パラリンピックのための祝日移動 (特例法)
    """
    if year == 2019:
        h.append(Holiday(dt.date(2019, 5, 1), "天皇の即位の日"))
        h.append(Holiday(dt.date(2019, 10, 22), "即位礼正殿の儀の行われる日"))

    elif year == 2020:
        # 海の日 → 7/23, スポーツの日 → 7/24, 山の日 → 8/10
        h = [x for x in h if x.name not in ("海の日", "スポーツの日", "山の日")]
        h.append(Holiday(dt.date(2020, 7, 23), "海の日"))
        h.append(Holiday(dt.date(2020, 7, 24), "スポーツの日"))
        h.append(Holiday(dt.date(2020, 8, 10), "山の日"))

    elif year == 2021:
        # 海の日 → 7/22, スポーツの日 → 7/23, 山の日 → 8/8
        h = [x for x in h if x.name not in ("海の日", "スポーツの日", "山の日")]
        h.append(Holiday(dt.date(2021, 7, 22), "海の日"))
        h.append(Holiday(dt.date(2021, 7, 23), "スポーツの日"))
        h.append(Holiday(dt.date(2021, 8, 8), "山の日"))

    return h


def holidays_for_year(year: int) -> list[Holiday]:
    """振替休日・国民の休日を含めた、その年の全休日を返す。"""
    if year < LAW_START_YEAR:
        return []

    # 年をまたぐ振替休日 (12/31 が日曜など) を正しく扱うため前後年も計算する
    base: list[Holiday] = []
    for y in (year - 1, year, year + 1):
        if LAW_START_YEAR <= y <= MAX_YEAR + 1:
            base.extend(_statutory_holidays(y))

    by_date = {x.date: x for x in base}

    # --- 振替休日 (1973年施行) ---
    # 祝日が日曜のとき、その後の最も近い平日を休日にする。
    # 2007年改正で「翌日」から「翌日以降の最初の非祝日」に変わった。
    if year >= 1973:
        for h in sorted(base, key=lambda x: x.date):
            if h.date.weekday() != 6:  # 日曜以外は対象外
                continue
            d = h.date + dt.timedelta(days=1)
            while d in by_date:
                d += dt.timedelta(days=1)
            by_date[d] = Holiday(d, "振替休日", kind="substitute")

    # --- 国民の休日 (1988年施行) ---
    # 祝日と祝日に挟まれた平日を休日にする。
    # 典型例: 2026年9月22日 (敬老の日と秋分の日の間)
    if year >= 1988:
        for h in sorted(base, key=lambda x: x.date):
            mid = h.date + dt.timedelta(days=1)
            nxt = h.date + dt.timedelta(days=2)
            if mid in by_date or nxt not in by_date:
                continue
            if by_date[nxt].kind == "substitute":
                continue
            if mid.weekday() == 6:  # 日曜は元から休みなので対象外
                continue
            by_date[mid] = Holiday(mid, "国民の休日", kind="citizens")

    return sorted(
        (x for x in by_date.values() if x.date.year == year),
        key=lambda x: x.date,
    )


def holiday_map(start_year: int, end_year: int) -> dict[dt.date, Holiday]:
    """年範囲をまとめた date -> Holiday の辞書を返す。"""
    out: dict[dt.date, Holiday] = {}
    for y in range(start_year, end_year + 1):
        for h in holidays_for_year(y):
            out[h.date] = h
    return out


def is_day_off(d: dt.date, hmap: dict[dt.date, Holiday]) -> bool:
    """土日または祝日なら True。"""
    return d.weekday() >= 5 or d in hmap
