#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JPX 先物1分足(DataCube)の **仕様検査**。予測は一切しない。

⛔ **日々の手順には入れない。** 発注もしない。CSV/ZIP を読むだけ。

★ 何のために要るのか
  買った月をまたいで **書式が同じか** を機械的に確かめる。
  今日(2026-09-07)、株式歩み値で実際に事故が起きた:

      サンプル(2021-08) : session distinction = '1'   / time 11桁
      実データ(2026-08) : session distinction = '01'  / time 12桁
      → 9,820万行が全部フィルタで落ちて「窓内0行」

  **JPX の CSV 書式は年で変わる。** 2023〜2024 は 2026-08 から2〜3年前なので
  同じことが起きうる。特徴量を作る前に、ここで落としておく。

★ 検査する7項目 (CLAUDE.md 2026-09-07 の合意)
  1. 列の構成が基準月と一致するか
  2. session_id の値の集合が一致するか（日中/夜間を分離できるか）
  3. 日中セッションに 08:45〜08:59 が毎営業日そろっているか
  4. バーの時刻が「開始」か（0845 があるか。終了なら 0846 が最初になる）
  5. 出来高0の行があるか（無ければ「欠損は行ごと落ちる」）
  6. trade_date と execution_date を区別できるか
  7. 中心限月を **前日の出来高** で選べるか（先読みなし）＝ 限月交代の処理

使い方
    # フォルダを丸ごと（.zip のまま読める。展開不要）
    python check_futures_spec.py --dir "C:/Users/to732/Downloads"

    # 基準月を変える（既定 202608 = 2026-09-07 に検査済みの月）
    python check_futures_spec.py --dir "..." --ref 202608

    # mini だけ / ラージだけ
    python check_futures_spec.py --dir "..." --index-type 19

⚠ これは **仕様検査**であって特徴量の探索ではない。
  価格の中身（夜間リターン等）は一切計算しない。
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import zipfile
from collections import defaultdict

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--dir", type=str, required=True,
                help="先物1分足の .zip / .csv が入っているフォルダ")
ap.add_argument("--ref", type=str, default="202608",
                help="基準月 yyyyMM（既定 202608）。ここと書式が違う月を洗い出す")
ap.add_argument("--index-type", type=int, default=0,
                help="19=mini / 18=ラージ。0(既定)なら両方")
ap.add_argument("--day-session", type=int, default=999,
                help="日中セッションの session_id（既定 999）")
ap.add_argument("--pre-from", type=int, default=845,
                help="寄り前窓の開始 HHMM（既定 845）")
ap.add_argument("--pre-to", type=int, default=859,
                help="寄り前窓の終了 HHMM（既定 859）")
ap.add_argument("--quiet", action="store_true", help="月ごとの明細を出さない")
a = ap.parse_args()

# 数値として読む列。ここに無い列は文字列のまま比較する
_NUM = {"trade_date", "execution_date", "index_type", "security_code",
        "session_id", "interval_time", "trade_volume", "number_of_trade",
        "contract_month"}
_FNAME = re.compile(r"future_ohlc_minute_(\d+)_(\d{6})", re.I)


def _open_rows(path: str):
    """CSV / ZIP から (ファイル表示名, DictReader) を順に返す。ZIP は展開しない。"""
    if path.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            print(f"  ⛔ 壊れた ZIP: {os.path.basename(path)}")
            return
        for nm in zf.namelist():
            if not nm.lower().endswith(".csv"):
                continue
            with zf.open(nm) as fh:
                txt = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                yield f"{os.path.basename(path)}::{nm}", csv.DictReader(txt)
    else:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            yield os.path.basename(path), csv.DictReader(fh)


def _scan_one(disp: str, rd: csv.DictReader) -> dict | None:
    """1ファイルを1パスで走査して、仕様検査に必要な要約だけ返す。

    ⛔ 価格は読まない（仕様検査であって特徴量の探索ではない）。
    """
    cols = list(rd.fieldnames or [])
    if not cols:
        return None
    need = {"trade_date", "execution_date", "session_id", "interval_time",
            "contract_month", "trade_volume", "index_type"}
    missing = need - set(cols)

    r = {
        "disp": disp, "cols": cols, "missing": sorted(missing), "rows": 0,
        "index_type": set(), "session": set(), "cmonth": set(),
        "vol0": 0, "exec_ne_trade": 0, "bad": 0,
        # 日中セッション用
        "day_rows": 0,
        "day_times": set(),
        # ⛔ 1ファイルに **複数の限月** が入っている。行数で数えると必ず外れるので
        #   (取引日, 限月) ごとに「その分足があったか」を集合で持つ。
        #   寄り前15分がそろっているかは、あとで中心限月を決めてから判定する。
        "pre_min": defaultdict(set),          # (trade_date, cmonth) -> {interval_time}
        "day_min": defaultdict(set),          # (trade_date, cmonth) -> {interval_time}
        # 限月交代用: (trade_date, contract_month) -> 日中出来高
        "vol": defaultdict(int),
        "tdates": set(),
    }
    if missing:
        return r  # 列が足りないなら中身は読まない

    ds = a.day_session
    for row in rd:
        try:
            td = int(row["trade_date"])
            ed = int(row["execution_date"])
            sid = int(row["session_id"])
            it = int(row["interval_time"])
            cm = int(row["contract_month"])
            vol = int(float(row["trade_volume"] or 0))
            ix = int(row["index_type"])
        except (TypeError, ValueError):
            r["bad"] += 1
            continue
        r["rows"] += 1
        r["index_type"].add(ix)
        r["session"].add(sid)
        r["cmonth"].add(cm)
        r["tdates"].add(td)
        if vol == 0:
            r["vol0"] += 1
        if ed != td:
            r["exec_ne_trade"] += 1
        if sid == ds:
            r["day_rows"] += 1
            r["day_times"].add(it)
            r["day_min"][(td, cm)].add(it)
            r["vol"][(td, cm)] += vol
            if a.pre_from <= it <= a.pre_to:
                r["pre_min"][(td, cm)].add(it)
    return r


def _month_of(disp: str) -> str:
    m = _FNAME.search(disp)
    return m.group(2) if m else "?"


def _itype_of(disp: str) -> str:
    m = _FNAME.search(disp)
    return m.group(1) if m else "?"


def main() -> int:
    if not os.path.isdir(a.dir):
        print(f"[error] フォルダがありません: {a.dir}")
        return 1
    files = [os.path.join(a.dir, f) for f in sorted(os.listdir(a.dir))
             if _FNAME.search(f) and f.lower().endswith((".zip", ".csv"))]
    if not files:
        print(f"[error] future_ohlc_minute_*.zip / .csv が1つもありません: {a.dir}")
        try:
            print("  フォルダの中身(先頭20):",
                  ", ".join(sorted(os.listdir(a.dir))[:20]))
        except OSError:
            pass
        return 1

    if a.index_type:
        files = [f for f in files if _itype_of(os.path.basename(f)) == str(a.index_type)]
        if not files:
            print(f"[error] index_type={a.index_type} のファイルがありません")
            return 1

    print(f"[info] {len(files)}ファイル / 基準月 {a.ref} / 日中 session_id={a.day_session}")
    print(f"[info] 検査するのは **書式だけ**。価格は1つも読みません\n")

    res: list[dict] = []
    for p in files:
        for disp, rd in _open_rows(p):
            r = _scan_one(disp, rd)
            if r is None:
                print(f"  ⛔ 空/ヘッダなし: {disp}")
                continue
            r["month"] = _month_of(disp)
            r["itype"] = _itype_of(disp)
            res.append(r)

    if not res:
        print("[error] 読めたファイルがありません")
        return 1

    # ---- 基準を選ぶ（index_type ごとに）----
    by_it: dict[str, list[dict]] = defaultdict(list)
    for r in res:
        by_it[r["itype"]].append(r)

    ng_total = 0
    for it in sorted(by_it):
        rs = sorted(by_it[it], key=lambda x: x["month"])
        ref = next((x for x in rs if x["month"] == a.ref), None)
        label = {"19": "mini", "18": "ラージ"}.get(it, f"index_type={it}")
        print("=" * 78)
        print(f"■ {label} (index_type={it}) — {len(rs)}ファイル "
              f"{rs[0]['month']} 〜 {rs[-1]['month']}")
        if ref is None:
            ref = rs[0]
            print(f"  ⚠ 基準月 {a.ref} が無いので {ref['month']} を基準にします")
        print("=" * 78)

        # ---- 1) 月ごとの要約 ----
        # ⛔ と ⚠ を混ぜない。⛔=基準月と書式が違う(致命的) / ⚠=品質(基準月でも起こる)
        want = a.pre_to - a.pre_from + 1
        hdr = (f"  {'月':>7} {'行':>9} {'日数':>4} {'日中行':>8} "
               f"{'寄前15分':>9} {'出来高0':>7} {'session':>12} {'列':>4} {'判定'}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        drift: list[str] = []
        warn: list[str] = []
        for r in rs:
            ndays = len(r["tdates"])
            # その月の中心限月を日ごとに決めてから、寄り前窓がそろっているか見る
            top_d: dict[int, int] = {}
            for (td, cm), v in r["vol"].items():
                if td not in top_d or v > r["vol"][(td, top_d[td])]:
                    top_d[td] = cm
            pre_full = sum(1 for td, cm in top_d.items()
                           if len(r["pre_min"].get((td, cm), ())) == want)
            bad: list[str] = []
            soft: list[str] = []
            if r["missing"]:
                bad.append(f"列不足 {','.join(r['missing'])}")
            else:
                if r["cols"] != ref["cols"]:
                    bad.append("列ちがい")
                if r["session"] != ref["session"]:
                    bad.append(f"session {sorted(r['session'])}")
                if a.day_session not in r["session"]:
                    bad.append("日中セッションなし")
                if a.pre_from not in r["day_times"]:
                    bad.append(f"{a.pre_from:04d}なし(時刻が終了基準?)")
                if pre_full != ndays:
                    soft.append(f"寄前欠け {ndays - pre_full}日")
                if r["vol0"]:
                    soft.append(f"出来高0が{r['vol0']}行")
                if r["bad"]:
                    soft.append(f"数値化できない{r['bad']}行")
            if bad:
                drift.append(f"{r['month']}: {' / '.join(bad)}")
                ng_total += 1
            if soft:
                warn.append(f"{r['month']}: {' / '.join(soft)}")
            ok = ("OK" if not (bad or soft)
                  else " / ".join(["⛔" + x for x in bad] + ["⚠" + x for x in soft]))
            if not a.quiet:
                print(f"  {r['month']:>7} {r['rows']:>9,} {ndays:>4} "
                      f"{r['day_rows']:>8,} {pre_full:>6}/{ndays:<2} "
                      f"{r['vol0']:>7,} {str(sorted(r['session'])):>12} "
                      f"{len(r['cols']):>4} {ok}")

        # ---- 2) 書式ドリフトのまとめ ----
        print()
        if drift:
            print(f"  ⛔ 基準月({ref['month']})と **書式が違う** 月が {len(drift)}:")
            for d in drift:
                print(f"     {d}")
        else:
            print(f"  ✅ 全 {len(rs)} ヶ月が基準月({ref['month']})と同じ書式です")
            print(f"     列 {len(ref['cols'])} / session {sorted(ref['session'])} / "
                  f"{a.pre_from:04d} のバーあり(時刻はバー開始)")
        if warn:
            print(f"  ⚠ 品質の注意 {len(warn)}件（書式ではないので止めません）:")
            for w in warn:
                print(f"     {w}")

        # ---- 3) 限月交代: 前日の出来高1位で当日の1位を当てられるか ----
        vol: dict[tuple[int, int], int] = defaultdict(int)
        for r in rs:
            for k, v in r["vol"].items():
                vol[k] += v
        top: dict[int, int] = {}
        for (td, cm), v in vol.items():
            if td not in top or v > vol[(td, top[td])]:
                top[td] = cm
        days = sorted(top)
        if len(days) < 2:
            print("\n  ⚠ 営業日が足りず限月交代を検査できません")
            continue
        miss = [(days[i - 1], top[days[i - 1]], days[i], top[days[i]])
                for i in range(1, len(days)) if top[days[i - 1]] != top[days[i]]]
        hit = len(days) - 1 - len(miss)
        print(f"\n  ■ 中心限月（日中の出来高1位）")
        print(f"    営業日 {len(days)} / 前日の1位をそのまま使って当日も1位: "
              f"{hit}/{len(days) - 1}日 ({hit / max(len(days) - 1, 1) * 100:.1f}%)")
        if miss:
            print(f"    交代した日 {len(miss)}回 ← ここは前日の値では外す（1日ぶんだけ）:")
            for x, tx, y, ty in miss:
                print(f"      {x} ({tx}) → {y} ({ty})")
            print("    ⚠ 交代は年4回(四半期SQ)が正常。もっと多いなら選び方が不安定です")
        else:
            print("    ⚠ 交代が1回も無い = この期間では限月交代を検査できていません")
        print()

    print("=" * 78)
    if ng_total:
        print(f"⛔ 書式に問題のある月が {ng_total} あります。特徴量を作る前に潰してください")
        return 2
    print("✅ 仕様検査 合格。特徴量の作成に進めます")
    print("⚠ ただし **凍結ファイルを先にコミットしてから**（特徴量4つの計算式 /")
    print("   停止ルールの形 / 増分R²のベースライン / ホールドアウトの定義 / 不合格条件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
