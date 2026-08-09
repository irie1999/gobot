"""fetch_jquants_extra.py — lss対策に効く J-Quants 追加データを一括取得して CSV 保存。

取得対象(プランで可否):
  short_positions : 空売り残高報告(銘柄別)   … 踏み上げ/squeeze 回避の主役
  daily_margin    : 信用残高 日次(銘柄別)     … 売残過多=踏み上げ/逆日歩リスク
  weekly_margin   : 信用残高 週次(銘柄別)
  short_selling   : 業種別 空売り比率         … ショート過熱=レジーム検知
  announcement    : 決算発表予定日            … 決算またぎ空売り回避(イベントリスク)
  listed_info     : 銘柄マスタ(貸借区分/業種) … 空売り可否・業種分散
  calendar        : 営業日カレンダー          … 祝日考慮
  trades_spec     : 投資部門別売買            … 外国人/個人フロー
  breakdown       : 売買内訳

使い方(あなたの機械・.env 設定済み):
  python fetch_jquants_extra.py                       # 全部・約2年
  python fetch_jquants_extra.py --days 760
  python fetch_jquants_extra.py --only short_positions,daily_margin,announcement
  python fetch_jquants_extra.py --out-dir jquants_extra

各データは <out-dir>/<name>.csv に保存。エラー(プラン未契約/引数違い)は個別にスキップして続行。
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dateutil import tz as _dtz

# ClientV2 を流用(V1は廃止=410 Gone。5分足取得と同じ認証)。
from jquants_fetch import get_client

# 取得名 -> jquantsapi クライアントのメソッド名
DATASETS = {
    "short_sale_report": "get_mkt_short_sale_report_range",  # 空売り集計/残高 → 踏み上げ
    "short_ratio":       "get_mkt_short_ratio_range",        # 業種別空売り比率 → レジーム
    "margin_interest":   "get_mkt_margin_interest_range",    # 信用残高(週次 買残/売残)
    "margin_alert":      "get_mkt_margin_alert_range",       # 信用規制/日々公表
    "earnings_cal":      "get_eq_earnings_cal",              # 決算発表予定 → イベント回避
    "master":            "get_eq_master",                    # 銘柄マスタ(貸借区分/業種)
    "calendar":          "get_mkt_calendar",                 # 営業日カレンダー
    "investor_types":    "get_eq_investor_types",            # 投資部門別売買 → フロー
    "breakdown":         "get_mkt_breakdown_range",          # 売買内訳
}

ap = argparse.ArgumentParser(description="lss対策用 J-Quants 追加データ一括取得")
ap.add_argument("--days", type=int, default=760, help="遡及日数(既定760≒2年)")
ap.add_argument("--only", type=str, default="", help="取得対象をカンマ区切りで限定(既定=全部)")
ap.add_argument("--out-dir", type=str, default="jquants_extra", help="CSV出力フォルダ")
args = ap.parse_args()


def _try_call(method, start, now):
    """メソッドの引数形が不明なので複数パターンを試す(TypeErrorは次へ、他は上位で処理)。"""
    fr = start.strftime("%Y%m%d")
    to = now.strftime("%Y%m%d")
    for kwargs in (
        dict(start_dt=start, end_dt=now),
        dict(from_yyyymmdd=fr, to_yyyymmdd=to),
        dict(from_date=fr, to_date=to),
        dict(),
    ):
        try:
            return method(**kwargs)
        except TypeError:
            continue
    # どの形も TypeError の場合、最後にもう一度素で呼んで実エラーを出す
    return method()


def main():
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=_dtz.gettz("Asia/Tokyo"))
    start = now - timedelta(days=args.days)
    targets = ([x.strip() for x in args.only.split(",") if x.strip()]
               if args.only else list(DATASETS))
    cli = get_client()
    print(f"[info] 取得 {len(targets)}件 / 遡及{args.days}日 → {out.resolve()}", flush=True)
    ok = ng = 0
    for name in targets:
        mname = DATASETS.get(name)
        if not mname:
            print(f"  [skip] {name}: 未知の対象", flush=True)
            continue
        method = getattr(cli, mname, None)
        if method is None:
            print(f"  [skip] {name}: {mname} がクライアントに無い(プラン外?)", flush=True)
            ng += 1
            continue
        try:
            df = _try_call(method, start, now)
            if df is None or (hasattr(df, "empty") and df.empty):
                print(f"  [warn] {name}({mname}): データ空", flush=True)
                ng += 1
                continue
            fp = out / f"{name}.csv"
            df.to_csv(fp, index=False, encoding="utf-8-sig")
            cols = list(df.columns)[:8]
            print(f"  [ok] {name}: {len(df)}行 → {fp.name}  cols={cols}", flush=True)
            ok += 1
        except Exception as e:
            print(f"  [err] {name}({mname}): {e}", file=sys.stderr, flush=True)
            ng += 1
    print(f"\n[完了] 成功{ok} / 失敗・空{ng} / 合計{len(targets)}", flush=True)
    print(f"保存先: {out.resolve()}")


if __name__ == "__main__":
    main()
