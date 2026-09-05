"""probe_5m_history.py — J-Quants の5分足が「どこまで過去に遡れるか」を実測する。

download_all_minute.py で全銘柄を長期間ダウンロードする前に、まずこれを実行して
「そもそも自分のプランで 2024-07 より前の5分足が返ってくるのか」を確認する。
返ってこないなら、--days をいくら増やしても過去は取れない(プランの制約)。

使い方(swingtradeフォルダで):
  python probe_5m_history.py                 # 代表銘柄で 2022-01〜今日 を年ごとに探る
  python probe_5m_history.py 7203 9984 6758  # 銘柄を明示
  python probe_5m_history.py --interval 1m   # 1分足で確認
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))


def _load_dotenv() -> None:
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
            return
        except Exception:
            pass


_load_dotenv()


def get_client():
    try:
        import jquantsapi
    except ImportError:
        print("[ERROR] pip install jquants-api-client", file=sys.stderr)
        sys.exit(1)
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[ERROR] JQUANTS_API_KEY が必要です (.env か環境変数)", file=sys.stderr)
        sys.exit(1)
    return jquantsapi.ClientV2(api_key=api_key)


def _fetch(cli, method_name, code, frm, to):
    fn = getattr(cli, method_name, None)
    if fn is None:
        return None
    try:
        return fn(code=code, from_yyyymmdd=frm, to_yyyymmdd=to)
    except Exception as e:
        return f"ERR: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", default=["7203", "9984", "6758"])
    ap.add_argument("--interval", choices=["1m", "5m", "15m"], default="5m")
    args = ap.parse_args()

    method = {
        "1m": "get_eq_bars_minute",
        "5m": "get_eq_bars_5minute",
        "15m": "get_eq_bars_15minute",
    }[args.interval]

    cli = get_client()
    now = datetime.now(JST)
    this_year = now.year

    # 半年刻みの窓で「その窓に1本でもデータが返るか」を確認する。
    # 返れば ○、その窓の最古バー日付を出す。返らなければ ×。
    windows = []
    for y in range(this_year - 5, this_year + 1):
        windows.append((f"{y}0101", f"{y}0630", f"{y} H1"))
        windows.append((f"{y}0701", f"{y}1231", f"{y} H2"))

    print("=" * 64)
    print(f"J-Quants {args.interval} 遡及可能範囲プローブ")
    print(f"  method = {method}")
    print(f"  codes  = {', '.join(args.codes)}")
    print("=" * 64)

    global_oldest = None
    for code in args.codes:
        print(f"\n--- {code} ---")
        oldest_for_code = None
        for frm, to, label in windows:
            if frm > now.strftime("%Y%m%d"):
                continue
            raw = _fetch(cli, method, code, frm, to)
            if isinstance(raw, str):  # error
                print(f"  {label:<8} {frm}-{to}  {raw}")
                continue
            if raw is None or getattr(raw, "empty", True):
                print(f"  {label:<8} {frm}-{to}  ×(データなし)")
                continue
            # 日付カラムを探す
            import pandas as pd
            dcol = next((c for c in ["Date", "date", "Time"] if c in raw.columns), None)
            n = len(raw)
            if dcol:
                try:
                    dmin = str(pd.to_datetime(raw[dcol]).min())[:10]
                    dmax = str(pd.to_datetime(raw[dcol]).max())[:10]
                except Exception:
                    dmin = dmax = "?"
            else:
                dmin = dmax = "?"
            print(f"  {label:<8} {frm}-{to}  ○ {n:>6}本  {dmin}〜{dmax}")
            if oldest_for_code is None:
                oldest_for_code = dmin
        if oldest_for_code:
            print(f"  → {code} の最古取得日: {oldest_for_code}")
            if global_oldest is None or oldest_for_code < global_oldest:
                global_oldest = oldest_for_code

    print("\n" + "=" * 64)
    if global_oldest:
        print(f"結論: このプランで遡れる最古の {args.interval} データは概ね {global_oldest} 以降")
        print("  → download_all_minute.py の --days はこの日付をカバーする値にすれば十分。")
        print("  → これより前は × なので、--days をいくら増やしても取得不可。")
    else:
        print("結論: どの窓でもデータが返りませんでした。")
        print("  → プラン/銘柄/認証を確認してください。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
