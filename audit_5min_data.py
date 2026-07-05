"""
audit_5min_data.py ― 共有 5分足データ(../stock_5min)の信頼性を一括監査する。
================================================================================
fetch_jquants_minute.py が書き込む共有フォルダ ../stock_5min/*.pkl を全件走査し、
「本当に信頼に足るデータか」を機械的にチェックする。取得後に必ず1回走らせる想定。

チェック項目 (1つでも×なら赤フラグ):
  A. 00:00:00バグ  … 全バーが 00:00:00 (時刻欠落)。壊れデータ。要再取得
  B. 場中時刻      … 9:00-15:00(場中)のバーが存在するか。場外だけなら異常
  C. データ鮮度    … 最新バー日付が古すぎないか (--stale-days 超で警告)
  D. カバレッジ    … 期間(日数)とバー数が少なすぎないか
  E. 重複/逆順     … index に重複・時刻逆行が無いか
  F. 欠損列        … open/high/low/close/volume が揃っているか

使い方:
  python audit_5min_data.py                    # ../stock_5min を全件監査
  python audit_5min_data.py --dir ../stock_5min --stale-days 5
  python audit_5min_data.py --bad-only         # 問題のある銘柄だけ表示
  python audit_5min_data.py --csv audit_5min.csv   # 結果をCSV出力
"""
from __future__ import annotations

import argparse
import csv as csvmod
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


def _audit_one(df) -> dict:
    """1銘柄のDataFrameを検査して所見dictを返す。"""
    r = {"bars": 0, "d0": "", "d1": "", "span_days": 0,
         "intraday": False, "in_session": False, "dup": 0,
         "unsorted": False, "missing_cols": "", "midnight_ratio": 1.0}
    try:
        r["bars"] = len(df)
        if r["bars"] == 0:
            r["missing_cols"] = "EMPTY"
            return r
        idx = df.index
        # 列チェック
        need = {"open", "high", "low", "close", "volume"}
        cols = {str(c).lower() for c in df.columns}
        miss = need - cols
        r["missing_cols"] = ",".join(sorted(miss))
        # 時刻チェック (00:00:00バグ)
        secs = idx.hour * 3600 + idx.minute * 60 + getattr(idx, "second", 0)
        nonzero = int((secs != 0).sum())
        r["midnight_ratio"] = round(1 - nonzero / len(idx), 3)
        r["intraday"] = nonzero > 0
        # 場中(9:00-15:00)バスが存在するか
        h = idx.hour
        r["in_session"] = bool(((h >= 9) & (h < 15)).any())
        # 期間
        r["d0"] = str(idx[0].date())
        r["d1"] = str(idx[-1].date())
        r["span_days"] = (idx[-1] - idx[0]).days
        # 重複・逆順
        r["dup"] = int(idx.duplicated().sum())
        r["unsorted"] = bool(not idx.is_monotonic_increasing)
    except Exception as e:
        r["missing_cols"] = f"ERR:{type(e).__name__}"
    return r


def _verdict(r, today, stale_days, min_bars, min_span) -> tuple[str, list[str]]:
    """所見から総合判定 (OK / WARN / BAD) と理由リストを返す。"""
    bad, warn = [], []
    if r["bars"] == 0 or r["missing_cols"] in ("EMPTY",) or r["missing_cols"].startswith("ERR"):
        bad.append(f"読込不可({r['missing_cols']})")
        return "BAD", bad
    if not r["intraday"] or r["midnight_ratio"] >= 1.0:
        bad.append("全バー00:00:00(時刻欠落)")
    elif r["midnight_ratio"] > 0.0:
        warn.append(f"00:00バー混在{r['midnight_ratio']:.0%}")
    if not r["in_session"]:
        bad.append("場中(9-15時)バー無し")
    if r["missing_cols"] and not r["missing_cols"].startswith(("ERR", "EMPTY")):
        bad.append(f"列欠損({r['missing_cols']})")
    if r["dup"] > 0:
        warn.append(f"重複{r['dup']}本")
    if r["unsorted"]:
        warn.append("時刻逆行")
    if r["bars"] < min_bars:
        warn.append(f"バー少{r['bars']}<{min_bars}")
    if r["span_days"] < min_span:
        warn.append(f"期間短{r['span_days']}d<{min_span}")
    # 鮮度
    try:
        d1 = datetime.strptime(r["d1"], "%Y-%m-%d").date()
        age = (today - d1).days
        if age > stale_days:
            warn.append(f"古い(最新{age}日前)")
    except Exception:
        pass
    if bad:
        return "BAD", bad
    if warn:
        return "WARN", warn
    return "OK", []


def main() -> None:
    ap = argparse.ArgumentParser(description="共有5分足データの信頼性監査")
    ap.add_argument("--dir", default="../stock_5min", help="監査対象フォルダ")
    ap.add_argument("--stale-days", type=int, default=7,
                    help="最新バーがこの日数より古いとWARN (既定7)")
    ap.add_argument("--min-bars", type=int, default=1000,
                    help="この本数未満はWARN (既定1000)")
    ap.add_argument("--min-span", type=int, default=300,
                    help="この日数未満はWARN (既定300)")
    ap.add_argument("--bad-only", action="store_true", help="OK以外だけ表示")
    ap.add_argument("--csv", default=None, help="所見をCSV出力")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.exists():
        print(f"[ERROR] {root} が存在しません。--dir で共有フォルダを指定してください。")
        return
    pkls = sorted(root.glob("*.pkl"))
    if args.limit:
        pkls = pkls[:args.limit]
    today = datetime.now(JST).date()

    print("=" * 88)
    print(f"5分足データ監査  {root.resolve()}  ({len(pkls)}銘柄)")
    print(f"  基準日{today} / 鮮度閾値{args.stale_days}日 / 最小{args.min_bars}本 / 最小{args.min_span}日")
    print("=" * 88)

    rows = []
    n_ok = n_warn = n_bad = 0
    for p in pkls:
        try:
            df = pickle.loads(p.read_bytes())
        except Exception as e:
            r = {"bars": 0, "d0": "", "d1": "", "span_days": 0,
                 "intraday": False, "in_session": False, "dup": 0,
                 "unsorted": False, "missing_cols": f"ERR:{type(e).__name__}",
                 "midnight_ratio": 1.0}
        else:
            r = _audit_one(df)
        verdict, reasons = _verdict(r, today, args.stale_days,
                                    args.min_bars, args.min_span)
        r["code"] = p.stem
        r["verdict"] = verdict
        r["reasons"] = "; ".join(reasons)
        rows.append(r)
        if verdict == "OK":
            n_ok += 1
        elif verdict == "WARN":
            n_warn += 1
        else:
            n_bad += 1
        if args.bad_only and verdict == "OK":
            continue
        mark = {"OK": "✓", "WARN": "△", "BAD": "✗"}[verdict]
        print(f"  {mark} {p.stem:<10} {verdict:<4} "
              f"{r['bars']:>6,}本 {r['d0']}〜{r['d1']} ({r['span_days']:>3}d)"
              + (f"  │ {r['reasons']}" if r['reasons'] else ""))

    print("=" * 88)
    print(f"合計 {len(rows)}銘柄 → ✓OK {n_ok} / △WARN {n_warn} / ✗BAD {n_bad}")
    if n_bad:
        bad_codes = [r["code"] for r in rows if r["verdict"] == "BAD"]
        print(f"  ✗BAD (要再取得): {' '.join(bad_codes[:40])}"
              + (" …" if len(bad_codes) > 40 else ""))
        print(f"  再取得例: python fetch_jquants_minute.py "
              + " ".join(bad_codes[:5]) + " --force --days 730")
    if n_bad == 0 and n_warn == 0:
        print("  → 全銘柄が信頼に足ります (時刻あり・場中バーあり・鮮度OK・列そろい)。")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            fields = ["code", "verdict", "bars", "d0", "d1", "span_days",
                      "intraday", "in_session", "midnight_ratio", "dup",
                      "unsorted", "missing_cols", "reasons"]
            w = csvmod.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})
        print(f"  CSV出力: {args.csv}")


if __name__ == "__main__":
    main()
