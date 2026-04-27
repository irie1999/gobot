"""
update_prime_minute.py  ―  東証プライム 1800銘柄の5分足を J-Quants で最新化
==================================================================
daily_fetch_minute.py の問題:
  - エラーがサイレント (silent catch)、原因不明
  - 全4382銘柄対象、不要なものも含む

このスクリプトは:
  - symbols_listed_all.py のプライム銘柄のみ更新
  - エラー詳細を表示 (種別ごとに集計)
  - 進捗保存 (中断・再開可能)
  - 適切なレート制限

【使い方】
  python update_prime_minute.py                  # 標準 (並列3)
  python update_prime_minute.py --workers 5      # 並列5
  python update_prime_minute.py --limit 50       # テスト
  python update_prime_minute.py --resume         # 中断再開
  python update_prime_minute.py --check-only     # 欠損確認のみ
  python update_prime_minute.py --verbose        # エラー全件表示
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import pandas as pd

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"
PROGRESS_FILE = Path(__file__).resolve().parent / "data" / "prime_update_progress.json"


def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def yf_to_jquants(yf_code: str) -> str:
    """7203.T → 72030"""
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


def get_last_date(pkl_path: Path) -> str | None:
    """pkl の最終日 (YYYY-MM-DD) を返す。"""
    if not pkl_path.exists():
        return None
    try:
        df = pickle.loads(pkl_path.read_bytes())
        if df is None or df.empty:
            return None
        if "DateTime" in df.columns:
            dt = pd.to_datetime(df["DateTime"], errors="coerce").dropna()
            if len(dt) > 0:
                return str(dt.iloc[-1])[:10]
        if "Date" in df.columns:
            return str(df["Date"].iloc[-1])[:10]
        if isinstance(df.index, pd.DatetimeIndex):
            return str(df.index[-1])[:10]
    except Exception:
        return None
    return None


def fetch_and_append(cli, code5: str, pkl_path: Path,
                     from_date: str, to_date: str) -> tuple:
    """1銘柄を J-Quants で取得して pkl に追記。
    Returns: (status, bars, error_type, error_msg)
    """
    from_d = from_date.replace("-", "")
    to_d = to_date.replace("-", "")

    try:
        df_new = cli.get_eq_bars_5minute(
            code=code5, from_yyyymmdd=from_d, to_yyyymmdd=to_d
        )
    except Exception as e:
        return ("err", 0, type(e).__name__, str(e)[:120])

    if df_new is None or df_new.empty:
        return ("empty", 0, "EMPTY", f"no data {from_date}~{to_date}")

    # 銘柄フィルタ
    if "Code" in df_new.columns:
        df_new = df_new[df_new["Code"].astype(str) == code5].copy()
    if df_new.empty:
        return ("empty", 0, "FILTERED", "code mismatch")

    # 既存読込
    if pkl_path.exists():
        try:
            df_old = pickle.loads(pkl_path.read_bytes())
            if not isinstance(df_old, pd.DataFrame):
                df_old = pd.DataFrame()
        except Exception:
            df_old = pd.DataFrame()
    else:
        df_old = pd.DataFrame()

    if not df_old.empty:
        df_combined = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_combined = df_new

    # 重複除去
    dt_cols = [c for c in ["Date", "Time"] if c in df_combined.columns]
    if dt_cols:
        df_combined = df_combined.drop_duplicates(subset=dt_cols, keep="last")
        df_combined = df_combined.sort_values(dt_cols).reset_index(drop=True)

    # 保存
    pkl_path.write_bytes(pickle.dumps(df_combined))
    return ("ok", len(df_new), "", "")


def main():
    parser = argparse.ArgumentParser(
        description="東証プライム1800銘柄の5分足をJ-Quantsで最新化")
    parser.add_argument("--workers", type=int, default=3,
                        help="並列スレッド数 (デフォルト3、推奨3-5、最大10)")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理銘柄数の上限 (テスト用)")
    parser.add_argument("--check-only", action="store_true",
                        help="最終日確認のみ (取得しない)")
    parser.add_argument("--resume", action="store_true",
                        help="進捗ファイルから再開 (前回中断時)")
    parser.add_argument("--verbose", action="store_true",
                        help="エラー全件を表示")
    parser.add_argument("--include-existing-only", action="store_true",
                        help="既存pklが存在する銘柄のみ更新 (新規ダウンロード回避)")
    parser.add_argument("--days-buffer", type=int, default=2,
                        help="最終日からの取得開始バッファ (デフォルト2日前から)")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # プライム銘柄リスト読込
    try:
        from symbols_listed_all import SYMBOLS as PRIME_SYMBOLS
        all_codes5 = [yf_to_jquants(s) for s, _ in PRIME_SYMBOLS]
        print(f"プライム銘柄: {len(all_codes5)}")
    except ImportError:
        print("[error] symbols_listed_all.py が存在しません")
        print("  先に実行: python download_tse_symbols.py --market prime")
        sys.exit(1)

    # 既存pklのみフィルタ (オプション)
    if args.include_existing_only:
        codes5 = [c for c in all_codes5 if (DATA_DIR / f"{c}.pkl").exists()]
        print(f"既存pkl対象: {len(codes5)}/{len(all_codes5)}")
    else:
        codes5 = all_codes5

    if args.limit > 0:
        codes5 = codes5[:args.limit]
        print(f"テスト制限: {args.limit}銘柄")

    # 進捗復元
    completed_codes = set()
    if args.resume and PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text())
            completed_codes = set(data.get("completed", []))
            print(f"進捗復元: {len(completed_codes)}銘柄処理済み")
            codes5 = [c for c in codes5 if c not in completed_codes]
        except Exception:
            pass

    if not codes5:
        print("処理対象なし")
        return

    # クライアント初期化
    print("\nJ-Quants クライアント初期化中...")
    try:
        from jquants_fetch import get_client
        cli = get_client()
        print("✓ 認証成功")
    except Exception as e:
        print(f"✗ 認証失敗: {type(e).__name__}: {e}")
        sys.exit(1)

    today = datetime.now(JST).strftime("%Y-%m-%d")

    # check-only モード
    if args.check_only:
        print(f"\n欠損確認モード ({len(codes5)}銘柄)")
        gap_dist = Counter()
        for code5 in codes5[:500]:
            pkl = DATA_DIR / f"{code5}.pkl"
            if not pkl.exists():
                gap_dist["missing"] += 1
                continue
            last = get_last_date(pkl)
            if not last:
                gap_dist["unknown"] += 1
                continue
            gap = (datetime.strptime(today, "%Y-%m-%d") -
                   datetime.strptime(last, "%Y-%m-%d")).days
            if gap == 0:
                gap_dist["up-to-date"] += 1
            elif gap <= 3:
                gap_dist[f"{gap}日前"] += 1
            else:
                gap_dist[f"{gap}日以上"] += 1
        print("\n  欠損状況 (最初の500銘柄):")
        for k, v in sorted(gap_dist.items(), key=lambda x: -x[1]):
            print(f"    {k:<15}: {v}")
        return

    print(f"\n更新開始: {len(codes5)}銘柄 / 並列{args.workers}")
    target_days = (len(codes5) // args.workers // 30 + 1)
    print(f"目安時間: 約{target_days}〜{target_days*2}分")
    print()

    updated = 0
    no_data = 0
    errors = 0
    skipped = 0
    total_bars = 0
    completed = 0
    progress_lock = Lock()
    error_types = Counter()
    error_samples = []
    verbose_log = []

    def _process(code5):
        pkl_path = DATA_DIR / f"{code5}.pkl"
        last_date = get_last_date(pkl_path)
        if last_date and last_date >= today:
            return ("skip", 0, "up-to-date", f"last={last_date} today={today}", code5)
        # 取得開始日: 最終日 + 1日 (バッファあり)
        if last_date:
            from_date = (datetime.strptime(last_date, "%Y-%m-%d")
                         + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            # 新規銘柄なら 30日前から
            from_date = (datetime.now(JST) - timedelta(days=30)).strftime("%Y-%m-%d")
        status, bars, err_type, err_msg = fetch_and_append(
            cli, code5, pkl_path, from_date, today
        )
        return (status, bars, err_type, err_msg, code5)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_process, c): c for c in codes5}
        for future in as_completed(futures):
            with progress_lock:
                completed += 1
                try:
                    status, bars, err_type, err_msg, code5 = future.result()
                    if args.verbose:
                        verbose_log.append(f"  {code5}: {status} bars={bars} {err_type} {err_msg}")
                    if status == "ok":
                        updated += 1
                        total_bars += bars
                        completed_codes.add(code5)
                    elif status == "empty":
                        no_data += 1
                        completed_codes.add(code5)
                    elif status == "skip":
                        skipped += 1
                        completed_codes.add(code5)
                    elif status == "err":
                        errors += 1
                        error_types[err_type] += 1
                        if len(error_samples) < 20:
                            error_samples.append(f"{code5}: [{err_type}] {err_msg}")
                except Exception as e:
                    errors += 1
                    error_types[type(e).__name__] += 1
                if completed % 50 == 0 or completed == len(codes5):
                    print(f"  {completed}/{len(codes5)} "
                          f"(更新:{updated} データなし:{no_data} スキップ:{skipped} エラー:{errors} +バー:{total_bars:,})",
                          flush=True)
                # 進捗保存 (100件ごと)
                if completed % 100 == 0:
                    try:
                        PROGRESS_FILE.write_text(json.dumps({
                            "completed": list(completed_codes),
                            "updated": updated,
                            "errors": errors,
                            "timestamp": datetime.now(JST).isoformat(),
                        }))
                    except Exception:
                        pass

    # 最終進捗保存
    try:
        PROGRESS_FILE.write_text(json.dumps({
            "completed": list(completed_codes),
            "updated": updated,
            "errors": errors,
            "timestamp": datetime.now(JST).isoformat(),
        }))
    except Exception:
        pass

    print()
    print("=" * 60)
    print(f"  プライム 更新完了")
    print(f"  処理: {len(codes5)}銘柄")
    print(f"  ✓ 更新: {updated}  (バー追加: {total_bars:,})")
    print(f"  ⚪ データなし: {no_data}")
    print(f"  ⏭ スキップ(既に最新): {skipped}")
    print(f"  ✗ エラー: {errors}")
    print()
    if args.verbose and verbose_log:
        print("  詳細ログ:")
        for line in verbose_log[:30]:
            print(line)
        print()
    if error_types:
        print("  エラー種別:")
        for err_type, count in error_types.most_common():
            print(f"    {err_type:<25}: {count}")
        print()
        print(f"  エラーサンプル (最大{len(error_samples)}件):")
        sample_count = len(error_samples) if args.verbose else min(10, len(error_samples))
        for s in error_samples[:sample_count]:
            print(f"    {s}")
    print("=" * 60)
    if errors > 0:
        print()
        print(f"  進捗ファイル: {PROGRESS_FILE}")
        print(f"  再開コマンド: python update_prime_minute.py --resume")


if __name__ == "__main__":
    main()
