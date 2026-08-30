"""kabu の /ranking を記録するだけのスクリプト。

⛔ 発注機能はありません。売買系のモジュールを import していません。
   GET は /kabusapi/ranking だけです。

Day 1 は ranking だけを回します。/board は混ぜません:
銘柄登録は50件上限で、登録直後の初回読み取りに 40〜140 秒かかり、
過去に同じ朝で 429 が積み上がって発注が危うくなった実例があります。
⛔ **トークンの衝突は「2プロセス」ではなく「2回の /token 呼び出し」が原因です。**
kabu は /token を POST するたびに前のトークンを無効にします。逆に言えば
**同じトークンなら2プロセスが同時に使えます**。同じ朝に `.\norder` と
併走させるなら `--token` か `.kabu_token` を使ってください
(このスクリプトはトークンを渡されれば /token を叩きません)。

⚠ ただし **429 (レート制限) は共有されます。** 呼び出しは4本だけ、既定2秒間隔。
429 を受けたら **リトライせず即停止**します (積むと `.\norder` のポーリングを
巻き添えにするため)。

記録は "1"x"TP" / "2"x"TP" / "1"x"ALL" / "2"x"ALL" の4本。
ALL は2回分の追加コストしかなく、**後から絶対に取れません**。

    python kabu_ranking_log.py                 # 4本を1回記録して終了
    python kabu_ranking_log.py --token XXXX    # 既存トークンを使う (併走可)
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

# ⛔ 返却件数は **50件** (2026-08-30 実測)。「通常 最大30件」という仕様説明は
#   誤りでした。Count / Limit / Size / Page / Offset の5種すべてで変わらず、
#   50 が固定の上限です。→ 監視枠の判定は N=30 ではなく **N=50** で読むこと。
RANKING_CAP_OBSERVED = 50

# ETF の初約定時刻を測るための銘柄。
# ⛔ 「ETF は 09:00 に必ず確定する」は成立しません。**ETF も板寄せ**なので
#   注文不均衡があれば特別気配で寄りません。ETF を指数の代わりに使える
#   必要条件は:
#       ETF の初約定時刻 ＋ 計算・発注時間 ＜ 対象銘柄の板寄せ成立時刻
#   これは履歴では測れないので、朝に OpeningPriceTime を記録して測ります。
# ⚠ 1306 は 1321 の代替ではなく **頑健性の確認** として扱うこと。
#   成績の良い方を選ぶと、指数の選択そのものが探索になります。
# ⚠ 指数として使うなら **分配落ちと分割の補正が必須**です
#   (1321 は毎年7月8日、1306 は7月10日が分配基準日、1306 は 2026年4月に分割)。
#   β も調整済みリターンで推定し直す必要があります。
ETF_SYMBOLS = [("1321", 1), ("1306", 1)]     # (銘柄コード, 市場コード 1=東証)


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


TOKEN_FILE = Path(".kabu_token")


def resolve_token(base_url: str, token: str | None,
                  token_file: str | None) -> str:
    """トークンを決める。⛔ **可能な限り /token を叩かない。**

    衝突の正体は「2つのプロセス」ではなく「**2回の /token 呼び出し**」です。
    kabu は /token を POST するたびに前のトークンを無効にするので、同じ朝に
    `.\\norder` と ranking を走らせると片方が死にます。
    **同じトークンなら2プロセスが同時に使えます。**

    優先順:
      1. --token
      2. --token-file (既定 .kabu_token)
      3. 環境変数 KABU_API_TOKEN
      4. ⛔ ここで初めて /token を POST する (他プロセスのトークンを無効にする)
    """
    if token:
        print("  トークン: --token で受け取りました (/token は叩きません)")
        return token
    f = Path(token_file) if token_file else TOKEN_FILE
    if f.is_file():
        t = f.read_text(encoding="utf-8").strip()
        if t:
            print(f"  トークン: {f} から読みました (/token は叩きません)")
            return t
    t = os.environ.get("KABU_API_TOKEN")
    if t:
        print("  トークン: 環境変数 KABU_API_TOKEN から (/token は叩きません)")
        return t
    print("  ⛔ トークンが渡されなかったので /token を POST します。")
    print("     **他プロセスのトークンが無効になります。** 同じ朝に併走させるなら")
    print("     --token か .kabu_token を使ってください。")
    return get_token(base_url)


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
        if r.status_code == 429:
            # ⛔ リトライしない。429 を積むと .\\norder のポーリングを巻き添えに
            #   します (§18.48 ⑩ で気配ログの429が09:00の発注を危うくした)。
            rec["fatal"] = "rate_limit"
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


def register(base_url: str, token: str, symbols: list) -> dict:
    """板を読むための銘柄登録。⛔ 登録は50件が上限です。

    ここで登録するのは ETF 2件だけ。発火候補の板は登録しません
    (登録直後の初回読み取りに 40〜140秒かかり、同じ朝に 429 が積み上がって
     発注が危うくなった実例があるため)。
    """
    body = {"Symbols": [{"Symbol": s, "Exchange": e} for s, e in symbols]}
    rec = {"req_ts": _now(), "kind": "register", "body_sent": body}
    try:
        r = requests.put(f"{base_url}/kabusapi/register",
                         headers={"X-API-KEY": token,
                                  "Content-Type": "application/json"},
                         json=body, timeout=15)
        rec["resp_ts"] = _now()
        rec["status"] = r.status_code
        rec["ok"] = r.status_code == 200
        try:
            rec["body"] = r.json()
        except ValueError:
            rec["body"] = {"_text": r.text[:2000]}
    except Exception as e:
        rec["resp_ts"] = _now()
        rec["ok"] = False
        rec["status"] = None
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


def fetch_board(base_url: str, token: str, sym: str, exch: int) -> dict:
    """1銘柄の板。**見るのは OpeningPriceTime (初約定時刻) です。**"""
    rec = {"req_ts": _now(), "kind": "board", "symbol": sym, "exchange": exch}
    try:
        r = requests.get(f"{base_url}/kabusapi/board/{sym}@{exch}",
                         headers={"X-API-KEY": token}, timeout=15)
        rec["resp_ts"] = _now()
        rec["status"] = r.status_code
        rec["ok"] = r.status_code == 200
        try:
            rec["body"] = r.json()      # 生 JSON。列は作らない
        except ValueError:
            rec["body"] = {"_text": r.text[:2000]}
    except Exception as e:
        rec["resp_ts"] = _now()
        rec["ok"] = False
        rec["status"] = None
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


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
        if rec.get("fatal") == "rate_limit":
            print("  ⛔ 429。ここで停止します。")
            return
        time.sleep(2.0)
    print(f"  ★ どれも件数が変わらなければ、上位{n0}件が固定の上限です。")
    print("     ⛔ 2026-08-30 実測: **50件**。引数5種すべてで変わらず。")
    print("        『通常30件』という仕様説明は誤りでした。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="デモ 18081 に繋ぐ")
    ap.add_argument("--base-url", dest="base_url")
    ap.add_argument("--repeat", type=int, default=1, help="記録の回数")
    ap.add_argument("--interval", type=float, default=60.0, help="間隔 (秒)")
    ap.add_argument("--probe", action="store_true",
                    help="件数指定/ページングの引数があるかを試す")
    ap.add_argument("--token", help="既存のトークンを使う (/token を叩かない)。"
                                    "同じ朝に .\\norder と併走するときに必須")
    ap.add_argument("--token-file", dest="token_file",
                    help=f"トークンを読むファイル (既定 {TOKEN_FILE})")
    ap.add_argument("--spacing", type=float, default=2.0,
                    help="呼び出しの間隔(秒)。429 を避けるため既定2秒")
    ap.add_argument("--etf", action="store_true",
                    help="ranking の合間に ETF (1321/1306) の板を読み、"
                         "OpeningPriceTime (初約定時刻) を記録する")
    args = ap.parse_args()

    base = args.base_url or (DEMO_URL if args.demo else PROD_URL)
    path = OUT_DIR / f"ranking_{dt.date.today():%Y-%m-%d}.jsonl"
    print(f"接続先 {base}  → {path}")
    print("⛔ このスクリプトは記録だけです。発注は一切しません。")

    try:
        token = resolve_token(base, args.token, args.token_file)
    except requests.exceptions.ConnectionError:
        print("接続エラー: kabuステーションが起動しているか確認してください。")
        return 1

    if args.probe:
        probe(base, token, path)
        return 0

    if args.etf:
        rec = register(base, token, ETF_SYMBOLS)
        append(path, rec)
        print(f"  ETF 登録 {len(ETF_SYMBOLS)} 件  ok={rec.get('ok')} "
              f"(登録上限は50件。ここでは2件だけ使います)")
        print("  ⛔ ETF も板寄せです。特別気配で寄らないことがあります。")
        print("     必要条件: ETFの初約定時刻 + 計算・発注時間 < 対象銘柄の板寄せ成立時刻")

    for i in range(args.repeat):
        if i:
            time.sleep(args.interval)
        if args.etf:
            for sym, exch in ETF_SYMBOLS:
                rec = fetch_board(base, token, sym, exch)
                append(path, rec)
                b = rec.get("body") or {}
                # ⛔ 判定に使うのは公式の OpeningPriceTime 同士の差ではなく
                #   **こちらが始値を初めて受信した時刻 (resp_ts)** です。
                #   API の配信遅延とこちらの処理時間を落とさないため。
                #       resp_ts + 計算・発注時間 + 安全余裕
                #         < 対象銘柄の板寄せ成立時刻
                print(f"  board {sym}  ok={rec.get('ok')}  "
                      f"受信 {rec.get('resp_ts')}  "
                      f"初約定(公式) {b.get('OpeningPriceTime')}  "
                      f"始値 {b.get('OpeningPrice')}  現値 {b.get('CurrentPrice')}")
                time.sleep(args.spacing)
        for typ, div in PLAN:
            rec = fetch_ranking(base, token, typ, div)
            append(path, rec)
            if rec.get("fatal") == "rate_limit":
                print("  ⛔ 429 (レート制限)。**ここで停止します。**")
                print("     リトライすると .\\norder のポーリングを巻き添えにします。")
                return 1
            body = rec.get("body") or {}
            rows = body.get("Ranking") or []
            head = ""
            if rows:
                r0 = rows[0]
                head = (f"1位 {r0.get('Symbol')} {r0.get('SymbolName')} "
                        f"{r0.get('ChangeRatio')}")
            print(f"  {rec['req_ts']}  Type={typ} {div:<3} "
                  f"ok={rec.get('ok')} {len(rows):>3}件  {head}")
            time.sleep(args.spacing)
    print(f"\n記録しました: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
