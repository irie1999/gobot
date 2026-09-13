"""
download_all_minute.py  ―  全銘柄 5分足データ一括ダウンロード (v2 高速版)
==================================================================
【高速化】
  - 1銘柄を一括取得 (チャンク分割なし → API呼出し 1回/銘柄)
  - 並列3スレッド (ThreadPoolExecutor)
  - pickle のみ保存 (CSV省略で I/O 半減)
  - 既存ファイル自動スキップ
  - データなし銘柄を即スキップ
  - 429 エラー時のみスリープ (成功時は待機なし)

【使い方】
  python download_all_minute.py                    # 全銘柄・2年
  python download_all_minute.py --days 365         # 1年分
  python download_all_minute.py --limit 10         # テスト
  python download_all_minute.py --workers 5        # 5並列
  python download_all_minute.py --with-csv         # CSV も保存
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import pandas as pd

JST = timezone(timedelta(hours=9))


def _resolve_out_dir() -> Path:
    """保存先を決める。レポートが読む場所と同じにしないと、部分DLが
    完璧な stock_5min を隠してしまう(daytrade_data の優先順位より)。
      1. 環境変数 MINUTE_5M_DIR (最優先)
      2. daytrade_data.DATA_DIR (= 実際に stock_5min を解決する)
      3. 従来の data/minute_5m (フォールバック)
    """
    env = os.environ.get("MINUTE_5M_DIR")
    if env:
        return Path(env)
    try:
        import daytrade_data as _dd
        return Path(_dd.DATA_DIR)
    except Exception:
        return Path(__file__).resolve().parent / "data" / "minute_5m"


DATA_DIR = _resolve_out_dir()
PROGRESS_FILE = Path(__file__).resolve().parent / "data" / "download_progress.json"
MAX_RETRY = 4
RATE_LIMIT_WAIT = 30  # 429 時の待機秒

# 429 スリープ用のグローバルロック (全スレッド共有)
_rate_lock = Lock()
_last_429_time = 0.0


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
        print("[ERROR] JQUANTS_API_KEY が必要です", file=sys.stderr)
        sys.exit(1)
    return jquantsapi.ClientV2(api_key=api_key)


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed": [], "failed": []}


_progress_lock = Lock()


def save_progress(prog: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _progress_lock:
        PROGRESS_FILE.write_text(
            json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")


def get_all_symbols(cli, market_filter=None) -> list[dict]:
    print("上場銘柄一覧を取得中...", flush=True)
    df = cli.get_eq_master()
    if df is None or df.empty:
        print("[ERROR] 銘柄一覧取得失敗", file=sys.stderr)
        sys.exit(1)
    print(f"  全銘柄: {len(df)}件", flush=True)

    if market_filter:
        kw = {"prime": "プライム", "standard": "スタンダード",
              "growth": "グロース"}.get(market_filter.lower(), market_filter)
        for col in ["MarketCodeName", "MarketCode", "market_code_name",
                     "Section", "Market"]:
            if col in df.columns:
                before = len(df)
                df = df[df[col].astype(str).str.contains(kw, na=False)]
                if len(df) < before:
                    print(f"  {col} '{kw}' フィルタ: {before}→{len(df)}件")
                    break

    symbols = []
    for _, row in df.iterrows():
        code = str(row.get("Code", ""))
        name = str(row.get("CompanyName", row.get("company_name", "")))
        if code and len(code) >= 4:
            symbols.append({"code": code, "name": name})
    return symbols


# ─────────────────────────────────────────────────────────────
# 1銘柄ダウンロード (高速版: チャンクなし一括取得)
# ─────────────────────────────────────────────────────────────

def download_one(cli, code: str, days: int, interval: str) -> pd.DataFrame | None:
    """1銘柄を一括取得。チャンク分割なし (API側でページネーション処理)。"""
    global _last_429_time

    method_map = {
        "1m": "get_eq_bars_minute",
        "5m": "get_eq_bars_5minute",
        "15m": "get_eq_bars_15minute",
    }
    method_name = method_map.get(interval, "get_eq_bars_5minute")
    fetch_method = getattr(cli, method_name, None)
    if fetch_method is None:
        return None

    now = datetime.now(JST)
    from_d = (now - timedelta(days=days)).strftime("%Y%m%d")
    to_d = now.strftime("%Y%m%d")

    for attempt in range(MAX_RETRY):
        # 429 直後は全スレッドで待機
        with _rate_lock:
            since_429 = time.time() - _last_429_time
            if since_429 < RATE_LIMIT_WAIT:
                time.sleep(RATE_LIMIT_WAIT - since_429)

        try:
            raw = fetch_method(
                code=code, from_yyyymmdd=from_d, to_yyyymmdd=to_d
            )
            if raw is not None and not raw.empty:
                if "Code" in raw.columns:
                    raw = raw[raw["Code"].astype(str) == code].copy()
                if not raw.empty:
                    dt_cols = [c for c in ["Date", "Time"] if c in raw.columns]
                    if dt_cols:
                        raw = raw.drop_duplicates(subset=dt_cols, keep="last")
                    return raw
            return None
        except Exception as e:
            err = str(e)
            if "429" in err or "too many" in err.lower():
                with _rate_lock:
                    _last_429_time = time.time()
                wait = RATE_LIMIT_WAIT * (attempt + 1)
                time.sleep(wait)
            elif attempt < MAX_RETRY - 1:
                time.sleep(2 ** attempt)
            else:
                return None
    return None


def _merge_existing(code: str, df: pd.DataFrame) -> pd.DataFrame:
    """既存pklと新規取得を結合して重複排除(union)。
    過去へ延長するとき、既存の完璧なデータを絶対に失わないための安全策。
    新規fetchが既存より短くても、和集合を取るので縮まない。
    """
    pkl_path = DATA_DIR / f"{code}.pkl"
    if not pkl_path.exists():
        return df
    try:
        old = pickle.loads(pkl_path.read_bytes())
    except Exception:
        return df
    if old is None or getattr(old, "empty", True):
        return df
    try:
        combined = pd.concat([old, df], ignore_index=True)
        key = [c for c in ["Date", "Time"] if c in combined.columns]
        if key:
            combined = combined.drop_duplicates(subset=key, keep="last")
            combined = combined.sort_values(key).reset_index(drop=True)
        return combined
    except Exception:
        return df  # 結合に失敗したら新規を返す(既存は上書きされるが例外時のみ)


def save_data(code: str, df: pd.DataFrame, with_csv: bool = False,
              merge: bool = False) -> int:
    """pickle 保存 (高速)。オプションで CSV も / 既存とマージ。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if merge:
        df = _merge_existing(code, df)
    pkl_path = DATA_DIR / f"{code}.pkl"
    pkl_path.write_bytes(pickle.dumps(df))
    if with_csv:
        csv_path = DATA_DIR / f"{code}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return pkl_path.stat().st_size


# ── Ctrl+C ────────────────────────────────────────────────
_interrupted = False


def _signal_handler(sig, frame):
    global _interrupted
    _interrupted = True
    print("\n[中断] 安全に停止中...", file=sys.stderr)


# ── メイン ──────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="全銘柄 5分足 一括ダウンロード (高速版)")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--interval", choices=["1m", "5m", "15m"], default="5m")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--market", choices=["prime", "standard", "growth"])
    parser.add_argument("--workers", type=int, default=3,
                        help="並列スレッド数 (デフォルト3)")
    parser.add_argument("--with-csv", action="store_true",
                        help="CSV も保存 (遅くなる)")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="既存銘柄もスキップせず再取得し、既存pklとマージ"
                             "(過去へ延長するとき用。既存データは失われない)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)

    print("=" * 60)
    print(f"  全銘柄 {args.interval} 一括ダウンロード (高速版)")
    print(f"  期間: {args.days}日 / 並列: {args.workers}スレッド")
    print(f"  保存先: {DATA_DIR}")
    print("=" * 60)

    cli = get_client()
    symbols = get_all_symbols(cli, market_filter=args.market)
    if args.limit > 0:
        symbols = symbols[:args.limit]

    # スキップ判定
    # --refresh: 既存pkl/進捗を無視して全銘柄を再取得(過去延長)。マージ保存で既存は守る。
    prog = {"completed": [], "failed": []} if (args.no_resume or args.refresh) else load_progress()
    if args.refresh:
        skip = set()
        print("  [refresh] 既存銘柄も再取得し、既存pklとマージします(過去延長モード)")
    else:
        existing = set(f.stem for f in DATA_DIR.glob("*.pkl")) if DATA_DIR.exists() else set()
        skip = set(prog["completed"]) | existing
    todo = [s for s in symbols if s["code"] not in skip]

    print(f"\n対象: {len(symbols)}  スキップ: {len(skip)}  残り: {len(todo)}")

    if not todo:
        print("全銘柄完了済み。--no-resume で再取得。")
        return

    total = len(todo)
    done = 0
    total_bars = 0
    total_bytes = 0
    errors = 0
    start_time = time.time()

    def _process(sym):
        """1銘柄の取得→保存を行うワーカー関数。"""
        code = sym["code"]
        df = download_one(cli, code, sym.get("days", args.days),
                          args.interval)
        if df is None or df.empty:
            return code, 0, 0, 0
        size = save_data(code, df, with_csv=args.with_csv, merge=args.refresh)
        n_days = df["Date"].nunique() if "Date" in df.columns else 0
        return code, len(df), n_days, size

    # 並列実行
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {}
        for sym in todo:
            if _interrupted:
                break
            sym["days"] = args.days
            fut = ex.submit(_process, sym)
            futures[fut] = sym

        for fut in as_completed(futures):
            if _interrupted:
                break
            done += 1
            sym = futures[fut]
            code = sym["code"]
            name = sym.get("name", "")

            try:
                _, bars, n_days, size = fut.result()
            except Exception as e:
                print(f"[{done:>4}/{total}] {code} {name:<16} ERROR: {e}")
                prog["failed"].append(code)
                errors += 1
                save_progress(prog)
                continue

            if bars == 0:
                print(f"[{done:>4}/{total}] {code} {name:<16} データなし")
            else:
                total_bars += bars
                total_bytes += size
                elapsed = time.time() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / speed if speed > 0 else 0
                print(f"[{done:>4}/{total}] {code} {name:<16} "
                      f"{bars:>6}本/{n_days:>3}日 "
                      f"{size/1024:>5.0f}KB "
                      f"ETA {int(eta//60)}:{int(eta%60):02d}")

            prog["completed"].append(code)
            if done % 10 == 0:
                save_progress(prog)

    save_progress(prog)

    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print(f"  完了: {done}/{total}  エラー: {errors}")
    print(f"  {total_bars:,}本  {total_bytes/1024/1024:.1f}MB  "
          f"{int(elapsed//60)}分{int(elapsed%60)}秒")
    print(f"  速度: {done/elapsed*60:.1f} 銘柄/分" if elapsed > 0 else "")
    print("=" * 60)


if __name__ == "__main__":
    main()
