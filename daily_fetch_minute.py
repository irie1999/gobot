"""
daily_fetch_minute.py  ―  毎日の5分足データ自動取得 + 欠損日補完
==================================================================
毎日15:30以降に実行し、当日の5分足を既存pklに追記保存する。
取得忘れの日があれば自動検出して補完取得する。

【動作】
  1. data/minute_5m/*.pkl の各銘柄について最終日を確認
  2. 最終日 ~ 今日 の間に欠損営業日があれば補完取得
  3. 当日分を取得して追記
  4. 重複バーは自動除去

【使い方】
  python daily_fetch_minute.py                  # 全銘柄更新 (月次スキャン用、1-2時間)
  python daily_fetch_minute.py --daytrade-only  # 監視20銘柄のみ (日次運用、数分)
  python daily_fetch_minute.py --limit 10       # テスト (10銘柄)
  python daily_fetch_minute.py --check-only     # 欠損確認のみ (取得しない)

【タスクスケジューラ登録】
  毎日 15:35 に自動実行:
    run_daily_fetch.bat

【設定】
  .env に JQUANTS_API_KEY=... を設定
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))
# 保存先は daytrade_data の自動解決を使う(環境変数 MINUTE_5M_DIR →
# data/minute_5m → 隣接 stock_5min の順)。これで swingtrade から実行しても
# 「完璧な」J-Quants データ(stock_5min)を直接 追記更新できる。
try:
    from daytrade_data import DATA_DIR  # noqa: E402
except Exception:
    DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"


# .env
def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        return


_load_dotenv()


def get_client():
    import jquantsapi
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[ERROR] JQUANTS_API_KEY が必要です", file=sys.stderr)
        sys.exit(1)
    return jquantsapi.ClientV2(api_key=api_key)


def get_last_date(pkl_path: Path) -> str | None:
    """pkl ファイルの最終日を取得。"""
    try:
        df = pickle.loads(pkl_path.read_bytes())
        if df is None or df.empty:
            return None
        if "Date" in df.columns:
            return str(df["Date"].max())[:10]
        return None
    except Exception:
        return None


def get_trading_days(cli, from_date: str, to_date: str) -> set[str]:
    """営業日カレンダーを取得。"""
    try:
        cal = cli.get_mkt_calendar(
            from_yyyymmdd=from_date.replace("-", ""),
            to_yyyymmdd=to_date.replace("-", ""),
        )
        if cal is not None and not cal.empty:
            if "Date" in cal.columns:
                return set(str(d)[:10] for d in cal["Date"].tolist())
            if "HolidayDivision" in cal.columns:
                # 0=営業日
                biz = cal[cal["HolidayDivision"] == "0"]
                if "Date" in biz.columns:
                    return set(str(d)[:10] for d in biz["Date"].tolist())
    except Exception:
        pass
    # フォールバック: 土日除外
    days = set()
    d = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while d <= end:
        if d.weekday() < 5:
            days.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


def fetch_and_append(cli, code: str, pkl_path: Path,
                     from_date: str, to_date: str) -> int:
    """指定期間の5分足を取得して既存pklに追記。"""
    from_d = from_date.replace("-", "")
    to_d = to_date.replace("-", "")

    try:
        df_new = cli.get_eq_bars_5minute(
            code=code, from_yyyymmdd=from_d, to_yyyymmdd=to_d
        )
    except Exception as e:
        return 0

    if df_new is None or df_new.empty:
        return 0

    # 銘柄フィルタ
    if "Code" in df_new.columns:
        df_new = df_new[df_new["Code"].astype(str) == code].copy()
    if df_new.empty:
        return 0

    # 既存データ読み込み
    try:
        df_old = pickle.loads(pkl_path.read_bytes())
    except Exception:
        df_old = pd.DataFrame()

    # 結合 + 重複除去
    if not df_old.empty:
        df_combined = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_combined = df_new

    dt_cols = [c for c in ["Date", "Time"] if c in df_combined.columns]
    if dt_cols:
        df_combined = df_combined.drop_duplicates(subset=dt_cols, keep="last")
        df_combined = df_combined.sort_values(dt_cols).reset_index(drop=True)

    # 保存
    pkl_path.write_bytes(pickle.dumps(df_combined))
    return len(df_new)


def main():
    parser = argparse.ArgumentParser(
        description="毎日の5分足データ自動取得 + 欠損補完")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理銘柄数の上限 (テスト用)")
    parser.add_argument("--check-only", action="store_true",
                        help="欠損確認のみ (取得しない)")
    parser.add_argument("--daytrade-only", action="store_true",
                        help="daytrade_symbols.py の20銘柄のみ更新 (日次運用用)")
    args = parser.parse_args()

    print(f"  保存先(更新対象): {DATA_DIR}")
    if not DATA_DIR.exists():
        print(f"[ERROR] 5分足フォルダが見つかりません: {DATA_DIR}\n"
              "  環境変数 MINUTE_5M_DIR で明示指定するか、stock_5min の場所を確認してください。",
              file=sys.stderr)
        sys.exit(1)

    cli = get_client()
    today = datetime.now(JST).strftime("%Y-%m-%d")

    # 対象 pkl ファイル決定
    if args.daytrade_only:
        try:
            from daytrade_symbols import DAYTRADE_SYMBOLS
        except ImportError:
            print("[ERROR] daytrade_symbols.py が見つかりません", file=sys.stderr)
            sys.exit(1)
        # "8032.T" → "80320" に変換
        target_codes = [s.replace(".T", "") + "0" for s, _ in DAYTRADE_SYMBOLS]
        pkl_files = []
        for code5 in target_codes:
            pkl = DATA_DIR / f"{code5}.pkl"
            if pkl.exists():
                pkl_files.append(pkl)
            else:
                print(f"  [warn] {code5}.pkl が存在しません (スキップ)")
        pkl_files.sort()
        print(f"daytrade対象: {len(pkl_files)}/{len(target_codes)}銘柄")
    else:
        pkl_files = sorted(DATA_DIR.glob("*.pkl"))
    if args.limit > 0:
        pkl_files = pkl_files[:args.limit]

    print(f"日次更新: {len(pkl_files)}銘柄 / 基準日: {today}", flush=True)

    # 欠損チェック + 取得
    updated = 0
    skipped = 0
    errors = 0
    total_bars = 0

    for i, pkl_path in enumerate(pkl_files, 1):
        code = pkl_path.stem  # "72030"

        # 最終日確認
        last_date = get_last_date(pkl_path)
        if last_date is None:
            skipped += 1
            continue

        # 最終日が今日ならスキップ
        if last_date >= today:
            skipped += 1
            continue

        # 欠損日数
        gap_days = (datetime.strptime(today, "%Y-%m-%d")
                    - datetime.strptime(last_date, "%Y-%m-%d")).days

        # 翌日から今日まで取得
        fetch_from = (datetime.strptime(last_date, "%Y-%m-%d")
                      + timedelta(days=1)).strftime("%Y-%m-%d")

        if args.check_only:
            if gap_days > 1:
                print(f"  {code}: 最終日 {last_date} → {gap_days}日分の欠損")
            continue

        # 取得
        bars = fetch_and_append(cli, code, pkl_path, fetch_from, today)
        if bars > 0:
            total_bars += bars
            updated += 1
        else:
            errors += 1

        if i % 100 == 0 or i == len(pkl_files):
            print(f"  {i}/{len(pkl_files)} 処理済み "
                  f"(更新:{updated} スキップ:{skipped} エラー:{errors})",
                  flush=True)

        # レート制限対策
        time.sleep(0.3)

    print()
    print("=" * 50)
    print(f"  日次更新完了")
    print(f"  処理: {len(pkl_files)}銘柄")
    print(f"  更新: {updated}  スキップ: {skipped}  エラー: {errors}")
    print(f"  追加バー: {total_bars:,}")
    print("=" * 50)


if __name__ == "__main__":
    main()
