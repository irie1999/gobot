"""kabu の /ranking を記録するだけのスクリプト。

⛔ 発注機能はありません。売買系のモジュールを import していません。
   GET は /kabusapi/ranking だけです。

Day 1 は ranking だけを回します。/board は混ぜません:
銘柄登録は50件上限で、登録直後の初回読み取りに 40〜140 秒かかり、
過去に同じ朝で 429 が積み上がって発注が危うくなった実例があります。
kabu の有効トークンは1つなので、他プロセスと同時に走らせないこと。

記録は "1"x"TP" / "2"x"TP" / "1"x"ALL" / "2"x"ALL" の4本。
ALL は2回分の追加コストしかなく、**後から絶対に取れません**。

    python kabu_ranking_log.py                 # 4本を1回記録して終了
    python kabu_ranking_log.py --demo          # デモ (18081)
    python kabu_ranking_log.py --probe         # 件数指定/ページングの引数を探る
    python kabu_ranking_log.py --repeat 3 --interval 60

パスワードは環境変数から読みます。⛔ コードに埋め込まないこと。
    KABU_API_PASSWORD_PROD (無ければ KABU_API_PASSWORD)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

OUT_DIR = Path("forward_records")
PROD_URL = "http://localhost:18080"
DEMO_URL = "http://localhost:18081"

# Type: "1"=値上がり率 "2"=値下がり率 "3"=売買高 "4"=売買代金
#       "5"=TICK回数 "6"=急上昇 "7"=急降下   ← すべて文字列
# ExchangeDivision: "ALL"=全市場 "T"=東証全体 "TP"=プライム
#                   "TS"=スタンダード "TG"=グロース "M"=名証
PLAN = [("1", "TP"), ("2", "TP"), ("1", "ALL"), ("2", "ALL")]


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def get_token(base_url: str) -> str:
    pw = os.environ.get("KABU_API_PASSWORD_PROD") or os.environ.get(
        "KABU_API_PASSWORD")
    if not pw:
        raise SystemExit(
            "環境変数 KABU_API_PASSWORD_PROD (または KABU_API_PASSWORD) が未設定です")
    r = requests.post(f"{base_url}/kabusapi/token",
                      headers={"Content-Type": "application/json"},
                      json={"APIPassword": pw}, timeout=10)
    r.raise_for_status()
    tok = r.json().get("Token")
    if not tok:
        raise SystemExit(f"トークンが取得できません: {r.text[:200]}")
    return tok


def fetch_ranking(base_url: str, token: str, typ: str, div: str,
                  extra: dict | None = None) -> dict:
    """1本ぶん取得する。失敗・タイムアウト・429 も必ず1行残す。"""
    params = {"Type": typ, "ExchangeDivision": div}
    if extra:
        params.update(extra)
    rec = {"req_ts": _now(), "type": typ, "exchange_division": div,
           "params": params}
    try:
        r = requests.get(f"{base_url}/kabusapi/ranking",
                         headers={"X-API-KEY": token}, params=params,
                         timeout=15)
        rec["resp_ts"] = _now()
        rec["status"] = r.status_code
        rec["ok"] = r.status_code == 200
        try:
            rec["body"] = r.json()          # 生 JSON をそのまま。列は作らない
        except ValueError:
            rec["body"] = {"_text": r.text[:2000]}
    except Exception as e:                   # タイムアウト・接続断も記録する
        rec["resp_ts"] = _now()
        rec["ok"] = False
        rec["status"] = None
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


def append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def probe(base_url: str, token: str, path: Path) -> None:
    """件数指定 / ページングの引数があるかを試す。コストはゼロ。

    仕様書に無い引数は無視されるだけのはずなので、返ってきた件数が
    変わるかどうかで判別する。
    """
    print("\n件数指定 / ページングの引数を探ります (観測のついで)")
    base = fetch_ranking(base_url, token, "1", "TP")
    append(path, base | {"probe": "baseline"})
    n0 = len(base.get("body", {}).get("Ranking", []) or [])
    print(f"  引数なし: {n0} 件 (status {base.get('status')})")
    for extra in ({"Count": "50"}, {"Limit": "50"}, {"Size": "50"},
                  {"Page": "2"}, {"Offset": "30"}):
        rec = fetch_ranking(base_url, token, "1", "TP", extra)
        append(path, rec | {"probe": list(extra)[0]})
        n = len(rec.get("body", {}).get("Ranking", []) or [])
        mark = "★ 効いています" if n != n0 and rec.get("ok") else ""
        print(f"  {str(extra):<20} {n:>3} 件 (status {rec.get('status')}) {mark}")
        time.sleep(1.0)
    print("  ★ どれも件数が変わらなければ、上位30件が固定の上限です。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="デモ 18081 に繋ぐ")
    ap.add_argument("--base-url", dest="base_url")
    ap.add_argument("--repeat", type=int, default=1, help="記録の回数")
    ap.add_argument("--interval", type=float, default=60.0, help="間隔 (秒)")
    ap.add_argument("--probe", action="store_true",
                    help="件数指定/ページングの引数があるかを試す")
    args = ap.parse_args()

    base = args.base_url or (DEMO_URL if args.demo else PROD_URL)
    path = OUT_DIR / f"ranking_{dt.date.today():%Y-%m-%d}.jsonl"
    print(f"接続先 {base}  → {path}")
    print("⛔ このスクリプトは記録だけです。発注は一切しません。")

    try:
        token = get_token(base)
    except requests.exceptions.ConnectionError:
        print("接続エラー: kabuステーションが起動しているか確認してください。")
        return 1

    if args.probe:
        probe(base, token, path)
        return 0

    for i in range(args.repeat):
        if i:
            time.sleep(args.interval)
        for typ, div in PLAN:
            rec = fetch_ranking(base, token, typ, div)
            append(path, rec)
            body = rec.get("body") or {}
            rows = body.get("Ranking") or []
            head = ""
            if rows:
                r0 = rows[0]
                head = (f"1位 {r0.get('Symbol')} {r0.get('SymbolName')} "
                        f"{r0.get('ChangeRatio')}")
            print(f"  {rec['req_ts']}  Type={typ} {div:<3} "
                  f"ok={rec.get('ok')} {len(rows):>3}件  {head}")
            time.sleep(1.0)
    print(f"\n記録しました: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
