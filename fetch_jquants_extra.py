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

# 取得名 -> クライアントのメソッド名 **の候補**(先頭から順に試す)。
# ⛔ ライブラリの版で名前が変わる(V1 は get_listed_info / V2 は get_eq_master 系)。
#   1つ決め打ちにすると「ライブラリに無い」で黙って落ちる(2026-09-05 に master が
#   出なかった)。候補を並べ、**どれも無ければ実在するメソッド名を列挙**して
#   次の実行で原因が分かるようにする。
DATASETS = {
    "short_sale_report": ["get_mkt_short_sale_report_range", "get_short_selling",
                          "get_markets_short_selling"],
    "short_ratio":       ["get_mkt_short_ratio_range", "get_short_selling_ratio"],
    "margin_interest":   ["get_mkt_margin_interest_range", "get_weekly_margin_interest",
                          "get_markets_weekly_margin_interest"],
    "margin_alert":      ["get_mkt_margin_alert_range", "get_daily_margin_interest"],
    # 決算 **発表予定**。⚠ 直近の予定しか返らないことが多い(過去には遡れない)
    "earnings_cal":      ["get_eq_earnings_cal", "get_fins_announcement",
                          "get_announcement"],
    # ★ 決算 **実績**(DisclosedDate を持つ)。過去に遡れるのはこちら。
    #   11.5年の「決算翌日を除外」を測るならこれが要る
    "statements":        ["get_fins_statements", "get_statements",
                          "get_fin_statements"],
    "master":            ["get_eq_master", "get_listed_info", "get_list"],
    "calendar":          ["get_mkt_calendar", "get_markets_trading_calendar",
                          "get_trading_calendar"],
    "investor_types":    ["get_eq_investor_types", "get_markets_trades_spec",
                          "get_trades_spec"],
    "breakdown":         ["get_mkt_breakdown_range", "get_markets_breakdown"],
}
# 候補が全滅したとき、実在メソッドを探すためのキーワード
_HINT = {"short_sale_report": "short", "short_ratio": "short",
         "margin_interest": "margin", "margin_alert": "margin",
         "earnings_cal": "announce", "statements": "statement",
         "master": "list", "calendar": "calendar",
         "investor_types": "trades", "breakdown": "breakdown"}

ap = argparse.ArgumentParser(description="lss対策用 J-Quants 追加データ一括取得")
ap.add_argument("--days", type=int, default=760, help="遡及日数(既定760≒2年)")
ap.add_argument("--only", type=str, default="", help="取得対象をカンマ区切りで限定(既定=全部)")
ap.add_argument("--out-dir", type=str, default="jquants_extra", help="CSV出力フォルダ")
ap.add_argument("--list-methods", action="store_true",
                help="★ クライアントの **実在メソッドを全部** 出して終了。\n                     版によって名前が変わるので、詰まったらまずこれ")
# ── どの項目がどのプランか (2026-08-28 時点) ────────────────────────
#   全プラン : calendar / master / earnings_cal
#   Light 〜 : investor_types / TOPIX四本値
#   Standard〜: short_sale_report(空売り残高) / short_ratio(業種別空売り比率) /
#              margin_interest(信用週末残高) / margin_alert(日々公表) / 指数四本値
#   Premium  : breakdown(売買内訳) / **先物四本値** / 前場四本値 / 配当金
#   アドオン  : 分足・ティック(2年) 5,500円/月
# ⚠ この表は **参考**。2026-09-06 の実測では master / earnings_cal が
#   403 "No active subscription found" だった。契約内容で変わるので
#   ここの値を信じて「取れるはず」と判断しないこと。
_PLAN = {"calendar": "全?", "master": "要契約確認", "earnings_cal": "要契約確認",
         "investor_types": "Light", "short_sale_report": "Standard",
         "statements": "全", "short_ratio": "Standard", "margin_interest": "Standard",
         "margin_alert": "Standard", "breakdown": "Premium"}
args = ap.parse_args()


def _try_call(method, start, now):
    """メソッドの引数形が不明なので複数パターンを試す(TypeErrorは次へ、他は上位で処理)。

    ⛔ 最後の `dict()` は **日付指定なし**。ここに落ちると「760日ぶん取った」
       つもりが直近しか返っていない、ということが起こる。どの形で通ったかを
       必ず返し、呼び出し側で実際の最古日・最新日を表示すること
       (2026-09-04 指摘)。
    """
    fr = start.strftime("%Y%m%d")
    to = now.strftime("%Y%m%d")
    for label, kwargs in (
        ("start_dt/end_dt",         dict(start_dt=start, end_dt=now)),
        ("from_yyyymmdd/to_yyyymmdd", dict(from_yyyymmdd=fr, to_yyyymmdd=to)),
        ("from_date/to_date",       dict(from_date=fr, to_date=to)),
        ("**引数なし(日付指定が効いていない)**", dict()),
    ):
        try:
            return method(**kwargs), label
        except TypeError:
            continue
    # どの形も TypeError の場合、最後にもう一度素で呼んで実エラーを出す
    return method(), "**引数なし(日付指定が効いていない)**"


def _date_span(df) -> str:
    """データの実際の最古日・最新日を返す。日付らしい列を総当たりで探す。"""
    for c in df.columns:
        if not any(k in str(c).lower() for k in ("date", "day", "日")):
            continue
        try:
            s = pd.to_datetime(df[c], errors="coerce").dropna()
        except Exception:
            continue
        if len(s) == 0:
            continue
        lo, hi = s.min(), s.max()
        return (f"{c}: {lo:%Y-%m-%d} 〜 {hi:%Y-%m-%d} "
                f"({(hi - lo).days}日ぶん)")
    return "**日付列が見つからない** — 期間を確認できません"


def main():
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=_dtz.gettz("Asia/Tokyo"))
    start = now - timedelta(days=args.days)
    targets = ([x.strip() for x in args.only.split(",") if x.strip()]
               if args.only else list(DATASETS))
    cli = get_client()
    if args.list_methods:
        # ★ 版で名前が変わるので、実在するものを丸ごと出す。
        #   これを貼れば DATASETS の候補をすぐ直せる(2026-09-06)。
        _all = sorted(m for m in dir(cli)
                      if not m.startswith("_")
                      and callable(getattr(cli, m, None)))
        print(f"[methods] {type(cli).__name__} … {len(_all)}個\n")
        for _m2 in _all:
            print(f"    {_m2}")
        return
    print(f"[info] 取得 {len(targets)}件 / 遡及{args.days}日 → {out.resolve()}", flush=True)
    print(f"[info] 必要プラン: "
          + " / ".join(f"{t}={_PLAN.get(t, '?')}" for t in targets), flush=True)
    ok = ng = 0
    for name in targets:
        cands = DATASETS.get(name)
        if not cands:
            print(f"  [skip] {name}: 未知の対象", flush=True)
            continue
        if isinstance(cands, str):
            cands = [cands]
        mname = next((c for c in cands if getattr(cli, c, None) is not None), None)
        method = getattr(cli, mname, None) if mname else None
        if method is None:
            # ⛔ 『クライアントに無い』は **ライブラリの版が違う**という意味で、
            #   プランとは別。プラン不足なら呼べるが 403/空になる。
            #   ★ 候補が全滅したら **実在するメソッド名を出す**。これが無いと
            #     何を書けばいいのか分からないまま終わる(2026-09-05 の master)。
            print(f"  [skip] {name}: 候補がどれもありません "
                  f"({', '.join(cands)})", flush=True)
            _kw = _HINT.get(name, "")
            _av = sorted(m for m in dir(cli)
                         if not m.startswith("_") and _kw in m.lower()
                         and callable(getattr(cli, m, None)))
            if _av:
                print(f"      ★ **'{_kw}' を含む実在メソッド**: "
                      f"{', '.join(_av[:12])}", flush=True)
                print(f"      → DATASETS['{name}'] の候補に足してください",
                      flush=True)
            else:
                print(f"      ⚠ '{_kw}' を含むメソッドが1つもありません。"
                      f"クライアントの版を確認 (pip install -U jquants-api-client)",
                      flush=True)
            ng += 1
            continue
        if mname != cands[0]:
            print(f"  [info] {name}: 第1候補 {cands[0]} が無いので "
                  f"**{mname}** を使います", flush=True)
        try:
            df, _form = _try_call(method, start, now)
            if df is None or (hasattr(df, "empty") and df.empty):
                print(f"  [warn] {name}({mname}): **データ空** "
                      f"— プラン不足でも空が返ることがあります"
                      f"(エラーにならない)", flush=True)
                ng += 1
                continue
            fp = out / f"{name}.csv"
            df.to_csv(fp, index=False, encoding="utf-8-sig")
            cols = list(df.columns)[:8]
            print(f"  [ok] {name}: {len(df)}行 → {fp.name}  cols={cols}", flush=True)
            # ★ 「760日ぶん」を信じないための2行。引数の形と実際の期間を出す。
            print(f"        引数の形: {_form}", flush=True)
            print(f"        実際の期間: {_date_span(df)}", flush=True)
            if "引数なし" in _form:
                print(f"        ⛔ **--days {args.days} は効いていません。**"
                      f"上の期間が実際に取れた範囲です", flush=True)
            ok += 1
        except Exception as e:
            _m = str(e).lower()
            # ⛔ 403 を全部「プラン不足」と書いてはいけない(2026-09-06 に誤記)。
            #   J-Quants の body は2種類あり、意味がまったく違う:
            #     "No active subscription found" … **契約そのものが無い/失効**
            #     それ以外の 403                  … 上位プランが要る項目
            #   前者を「Standard が要る」と書くと、契約状態の確認に行けなくなる。
            if "no active subscription" in _m:
                _why = ("**契約が見つかりません**(上位プランの話ではない)。"
                        "J-Quants のマイページで契約状態を確認してください。"
                        "\n        ⚠ 分足アドオンだけ有効で v2 の equities 系が"
                        "未契約、ということも起こります")
            elif any(k in _m for k in ("403", "forbidden", "not subscribe",
                                       "subscription", "plan", "unauthorized",
                                       "401")):
                _why = "**プラン不足**(上位プランが要る項目)"
            elif "404" in _m or "not found" in _m:
                _why = "エンドポイントが無い(ライブラリ/API の版違い)"
            elif "429" in _m or "rate" in _m:
                _why = "レート制限(--sleep を増やして再実行)"
            else:
                _why = "原因不明"
            print(f"  [err] {name}({mname}): {_why}\n"
                  f"        生のエラー: {e}", file=sys.stderr, flush=True)
            ng += 1
    print(f"\n[完了] 成功{ok} / 失敗・空{ng} / 合計{len(targets)}", flush=True)
    print(f"保存先: {out.resolve()}")


if __name__ == "__main__":
    main()
