"""check_5m_freshness.py — .\\daily が使うローカル5分足(stock_5min/data)が
「最新の営業日」まで揃っているかを判定し、古ければ警告する。

背景:
  .\\daily は run_signals_holdout_all の _auto_update_minute() が yfinance で
  ローカル5分足に「既存より後のバーだけ」を追記する(1日1回)。ネットワーク不調や
  レート制限で更新に失敗すると、直近営業日の5分足が欠けたまま lss の同日決済
  (取引明細・BT・予算タブ)が古いデータで計算され、最近の月の成績が欠落する。
  本チェックはローカル5分足の最新バー日付を、期待される最新営業日
  (backtest_limit_entry._expected_latest_bar_date: 平日15:40 JST以降=当日 /
  以前=前営業日 / 週末=金)と比較して、遅れていれば警告する。

使い方:
  python check_5m_freshness.py            # 判定して表示
  python check_5m_freshness.py --sample 120
  python check_5m_freshness.py --strict   # 古いとき exit 1(daily側で分岐したい場合)

daily.bat / run_signals_holdout_all からは check_freshness() を import して呼ぶ。
"""
from __future__ import annotations
import argparse
import pickle
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

_JST = timezone(timedelta(hours=9))


def _expected_latest():
    """期待される最新営業日(Timestamp, tzナイーブ)。backtest_limit_entry の定義を流用。"""
    try:
        from backtest_limit_entry import _expected_latest_bar_date
        return pd.Timestamp(_expected_latest_bar_date())
    except Exception:
        now = datetime.now(_JST)
        today = now.date()
        wd = today.weekday()
        if wd == 5:
            return pd.Timestamp(today - timedelta(days=1))
        if wd == 6:
            return pd.Timestamp(today - timedelta(days=2))
        if now.hour < 15 or (now.hour == 15 and now.minute < 40):
            return pd.Timestamp(today - timedelta(days=(3 if wd == 0 else 1)))
        return pd.Timestamp(today)


def _pkl_latest_date(path):
    """pkl の最新バー日付(tzナイーブTimestamp)。読めなければ None。"""
    try:
        raw = pickle.loads(path.read_bytes())
        idx = getattr(raw, "index", None)
        if idx is None or len(idx) == 0:
            return None
        mx = pd.to_datetime(pd.Series(idx)).max()
        mx = pd.Timestamp(mx)
        if mx.tzinfo is not None:
            mx = mx.tz_convert("Asia/Tokyo").tz_localize(None)
        return mx.normalize()
    except Exception:
        return None


def check_freshness(sample: int = 80, verbose: bool = True) -> dict:
    """ローカル5分足の鮮度を判定して結果 dict を返す。
    戻り: {fresh, data_dir, latest, expected, n_checked, n_behind, reason}
    fresh=True なら最新営業日まで揃っている。"""
    try:
        import daytrade_data as dd
        data_dir = dd.DATA_DIR
    except Exception as e:
        if verbose:
            print(f"[5分足鮮度] daytrade_data 読込失敗: {e}", flush=True)
        return {"fresh": None, "reason": "daytrade_data import 失敗"}

    exp = _expected_latest()
    res = {"fresh": None, "data_dir": str(data_dir), "expected": exp,
           "latest": None, "n_checked": 0, "n_behind": 0, "reason": ""}

    if not data_dir or not data_dir.exists():
        res.update(fresh=False, reason=f"5分足フォルダが無い: {data_dir}")
        if verbose:
            _warn(res)
        return res
    pkls = sorted(data_dir.glob("*.pkl"))
    if not pkls:
        res.update(fresh=False, reason=f"5分足pklが空: {data_dir}")
        if verbose:
            _warn(res)
        return res

    # 均等サンプリング(fleet は一括更新なので代表性あり)
    if sample > 0 and len(pkls) > sample:
        step = len(pkls) / sample
        pick = [pkls[int(i * step)] for i in range(sample)]
    else:
        pick = pkls

    dates = []
    for p in pick:
        d = _pkl_latest_date(p)
        if d is not None:
            dates.append(d)
    if not dates:
        res.update(fresh=False, reason="pkl から日付を読めなかった")
        if verbose:
            _warn(res)
        return res

    fleet_latest = max(dates)                       # サンプル中で最も新しいバー日付
    n_behind = sum(1 for d in dates if d < exp)     # 期待日より遅れている本数
    res.update(latest=fleet_latest, n_checked=len(dates), n_behind=n_behind)

    if fleet_latest.date() < exp.date():
        # 全体が古い = 直近の更新が丸ごと失敗している可能性が高い
        res["fresh"] = False
        res["reason"] = "全体が最新営業日より前(更新失敗の可能性)"
    elif n_behind > len(dates) * 0.2:
        # 一部銘柄だけ古い(部分更新・個別欠損)
        res["fresh"] = False
        res["reason"] = f"一部銘柄が古い({n_behind}/{len(dates)}本が期待日より前)"
    else:
        res["fresh"] = True
        res["reason"] = "最新営業日まで揃っている"

    if verbose:
        if res["fresh"]:
            print(f"[5分足鮮度] OK 最新バー {fleet_latest.date()} "
                  f"(期待 {exp.date()}) / 確認 {len(dates)}本・遅れ {n_behind}本 / {data_dir}",
                  flush=True)
        else:
            _warn(res)
    return res


def _warn(res: dict):
    exp = res.get("expected")
    latest = res.get("latest")
    print("=" * 68, flush=True)
    print("⚠⚠  5分足が最新ではありません — lssの同日成績が古いデータで計算されます  ⚠⚠", flush=True)
    print(f"  理由    : {res.get('reason')}", flush=True)
    if latest is not None and exp is not None:
        print(f"  最新バー: {pd.Timestamp(latest).date()}  /  期待(最新営業日): {pd.Timestamp(exp).date()}",
              flush=True)
    print(f"  フォルダ: {res.get('data_dir')}", flush=True)
    print("  対処    : 5分足の自動更新(yfinance)に失敗している可能性があります。", flush=True)
    print("            ネット接続を確認し、次を実行してから .\\daily を回し直してください:", flush=True)
    print("              python daily_fetch_minute.py         (5分足を最新化)", flush=True)
    print("            J-Quants分足がある場合はそちらでの更新も検討。", flush=True)
    print("=" * 68, flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ローカル5分足の鮮度チェック(最新営業日まであるか)")
    ap.add_argument("--sample", type=int, default=80, help="確認するpklのサンプル数(0=全部)")
    ap.add_argument("--strict", action="store_true", help="古いとき exit 1 で終了する")
    a = ap.parse_args()
    r = check_freshness(sample=a.sample, verbose=True)
    if a.strict and r.get("fresh") is False:
        sys.exit(1)
