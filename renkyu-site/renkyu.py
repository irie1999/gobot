"""連休 (連続した休日) を検出し、説明用のメタ情報を付ける。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from holidays import Holiday, holiday_map, is_day_off

WD = "月火水木金土日"

# 名前が付いている連休シーズン。(開始月日, 終了月日, 名称) で判定する。
SEASONS = [
    ((12, 27), (1, 5), "年末年始"),
    ((4, 25), (5, 10), "ゴールデンウィーク"),
    ((9, 12), (9, 26), "シルバーウィーク"),
]


@dataclass
class Renkyu:
    """連続した休日の1区間。"""

    start: dt.date
    end: dt.date
    days: list[dt.date] = field(default_factory=list)
    holidays: list[Holiday] = field(default_factory=list)

    @property
    def length(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def slug(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def season(self) -> str | None:
        """年末年始 / GW / SW のいずれかに該当すればその名前。"""
        for (sm, sd), (em, ed), name in SEASONS:
            for d in (self.start, self.end):
                md = (d.month, d.day)
                if sm > em:  # 年をまたぐ (年末年始)
                    if md >= (sm, sd) or md <= (em, ed):
                        return name
                elif (sm, sd) <= md <= (em, ed):
                    return name
        return None

    @property
    def title(self) -> str:
        s = self.season
        base = f"{self.length}連休"
        return f"{s} {base}" if s else base

    @property
    def range_label(self) -> str:
        if self.start.year != self.end.year:
            return (
                f"{self.start.year}年{self.start.month}月{self.start.day}日"
                f"({WD[self.start.weekday()]})〜"
                f"{self.end.year}年{self.end.month}月{self.end.day}日"
                f"({WD[self.end.weekday()]})"
            )
        return (
            f"{self.start.month}月{self.start.day}日({WD[self.start.weekday()]})〜"
            f"{self.end.month}月{self.end.day}日({WD[self.end.weekday()]})"
        )

    @property
    def holiday_names(self) -> list[str]:
        seen: list[str] = []
        for h in self.holidays:
            if h.name not in seen:
                seen.append(h.name)
        return seen


def find_renkyu(
    start_year: int, end_year: int, min_length: int = 3
) -> list[Renkyu]:
    """指定年範囲の連休を、min_length 日以上の区間だけ返す。

    年末年始をまたぐ連休を落とさないよう、前後1年を余分に走査してから
    範囲でフィルタしている。
    """
    hmap = holiday_map(start_year - 1, end_year + 1)
    cur = dt.date(start_year - 1, 12, 1)
    last = dt.date(end_year + 1, 1, 31)

    out: list[Renkyu] = []
    run: list[dt.date] = []

    while cur <= last:
        if is_day_off(cur, hmap):
            run.append(cur)
        else:
            if len(run) >= min_length:
                out.append(_make(run, hmap))
            run = []
        cur += dt.timedelta(days=1)
    if len(run) >= min_length:
        out.append(_make(run, hmap))

    # 開始日が対象年範囲に入っているものだけ採用する
    return [r for r in out if start_year <= r.start.year <= end_year]


def _make(run: list[dt.date], hmap: dict[dt.date, Holiday]) -> Renkyu:
    return Renkyu(
        start=run[0],
        end=run[-1],
        days=list(run),
        holidays=[hmap[d] for d in run if d in hmap],
    )


def next_renkyu(today: dt.date, horizon_years: int = 3) -> Renkyu | None:
    """today 以降に来る最初の連休 (開催中のものを含む)。"""
    for r in find_renkyu(today.year, today.year + horizon_years):
        if r.end >= today:
            return r
    return None
