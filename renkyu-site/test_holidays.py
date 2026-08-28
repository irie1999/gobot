#!/usr/bin/env python3
"""祝日計算の回帰テスト。

実際に官報で告示された過去の祝日と照合する。
法改正への追従ミスやハッピーマンデーの計算ずれをここで検出する。

    python test_holidays.py
"""

import datetime as dt
import sys

from holidays import calc_equinox, holidays_for_year
from renkyu import find_renkyu

# 実際の祝日 (内閣府公表値)。ハッピーマンデー・振替休日・国民の休日を含む。
KNOWN = {
    2015: ["01-01", "01-12", "02-11", "03-21", "04-29", "05-03", "05-04",
           "05-05", "05-06", "07-20", "09-21", "09-22", "09-23", "10-12",
           "11-03", "11-23", "12-23"],
    2019: ["01-01", "01-14", "02-11", "03-21", "04-29", "04-30", "05-01",
           "05-02", "05-03", "05-04", "05-05", "05-06", "07-15", "08-11",
           "08-12", "09-16", "09-23", "10-14", "10-22", "11-03", "11-04",
           "11-23"],
    2020: ["01-01", "01-13", "02-11", "02-23", "02-24", "03-20", "04-29",
           "05-03", "05-04", "05-05", "05-06", "07-23", "07-24", "08-10",
           "09-21", "09-22", "11-03", "11-23"],
    2021: ["01-01", "01-11", "02-11", "02-23", "03-20", "04-29", "05-03",
           "05-04", "05-05", "07-22", "07-23", "08-08", "08-09", "09-20",
           "09-23", "11-03", "11-23"],
    2024: ["01-01", "01-08", "02-11", "02-12", "02-23", "03-20", "04-29",
           "05-03", "05-04", "05-05", "05-06", "07-15", "08-11", "08-12",
           "09-16", "09-22", "09-23", "10-14", "11-03", "11-04", "11-23"],
    2025: ["01-01", "01-13", "02-11", "02-23", "02-24", "03-20", "04-29",
           "05-03", "05-04", "05-05", "05-06", "07-21", "08-11", "09-15",
           "09-23", "10-13", "11-03", "11-23", "11-24"],
    2026: ["01-01", "01-12", "02-11", "02-23", "03-20", "04-29", "05-03",
           "05-04", "05-05", "05-06", "07-20", "08-11", "09-21", "09-22",
           "09-23", "10-12", "11-03", "11-23"],
}

# 官報で告示済みの春分・秋分 (近似式の検算用)
EQUINOX = {
    2020: (20, 22), 2021: (20, 23), 2022: (21, 23), 2023: (21, 23),
    2024: (20, 22), 2025: (20, 23), 2026: (20, 23),
}

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        failures.append(msg)


print("実際の祝日との照合")
for year, expected in sorted(KNOWN.items()):
    got = [h.date.strftime("%m-%d") for h in holidays_for_year(year)]
    if got == expected:
        check(True, f"{year}年 ({len(got)}日)")
    else:
        check(False, f"{year}年  余分={sorted(set(got) - set(expected))} "
                     f"不足={sorted(set(expected) - set(got))}")

print("\n春分・秋分の近似式")
for year, (sp, au) in sorted(EQUINOX.items()):
    check(calc_equinox(year, True) == sp, f"{year}年 春分 = 3/{sp}")
    check(calc_equinox(year, False) == au, f"{year}年 秋分 = 9/{au}")

print("\n祝日制度の個別ルール")
h2026 = {h.date: h for h in holidays_for_year(2026)}
check(h2026[dt.date(2026, 9, 22)].kind == "citizens",
      "2026/9/22 は国民の休日 (敬老の日と秋分の日に挟まれた平日)")
check(h2026[dt.date(2026, 5, 6)].kind == "substitute",
      "2026/5/6 は振替休日 (5/3 憲法記念日が日曜)")
check(all(h.date.weekday() == 0
          for y in (2024, 2025, 2026)
          for h in holidays_for_year(y) if h.name == "海の日"),
      "海の日は必ず月曜 (ハッピーマンデー)")
check(not any(h.name == "天皇誕生日" for h in holidays_for_year(2019)),
      "2019年に天皇誕生日は存在しない (代替わりの年)")
check(any(h.name == "山の日" for h in holidays_for_year(2016))
      and not any(h.name == "山の日" for h in holidays_for_year(2015)),
      "山の日は2016年から")
check(len(holidays_for_year(1947)) == 0, "法施行前 (1947年) は祝日なし")

print("\n連休の検出")
r2019 = [r for r in find_renkyu(2019, 2019) if r.start.month == 4]
check(bool(r2019) and r2019[0].length == 10,
      "2019年のゴールデンウィークは10連休")
r2026 = {r.slug: r for r in find_renkyu(2026, 2026)}
check(r2026["2026-09-19"].length == 5
      and r2026["2026-09-19"].season == "シルバーウィーク",
      "2026年のシルバーウィークは5連休")
check(all(r.length >= 3 for r in find_renkyu(2026, 2030)),
      "検出される連休はすべて3日以上")
check(all(r.end >= r.start for r in find_renkyu(2026, 2035)),
      "連休の開始日 <= 終了日")

print()
if failures:
    print(f"NG: {len(failures)} 件失敗")
    sys.exit(1)
print("すべて成功")
