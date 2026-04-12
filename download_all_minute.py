"""
download_all_minute.py  ―  全銘柄 5分足データ一括ダウンロード
==================================================================
J-Quants V2 の分足アドオンを使い、取得可能な全上場銘柄の5分足を
最大2年分ダウンロードして CSV + pickle で永久保存する。

【前提】
  - pip install jquants-api-client
  - .env に JQUANTS_API_KEY=... を設定
  - 分足・ティックアドオン契約済み

【保存先】
  data/minute_5m/
    72030.csv          ← CSV (Excel/pandas で読める)
    72030.pkl          ← pickle (高速ロード)
    ...
  data/download_progress.json  ← 進捗管理 (中断再開用)

【使い方】
  python download_all_minute.py                  # 全銘柄・2年
  python download_all_minute.py --days 365       # 1年分
  python download_all_minute.py --days 60        # 60日分 (テスト)
  python download_all_minute.py --limit 10       # 先頭10銘柄のみ (テスト)
  python download_all_minute.py --resume         # 中断再開 (デフォルトON)
  python download_all_minute.py --interval 1m    # 1分足
  python download_all_minute.py --market prime   # プライム市場のみ

【所要時間の目安】
  10銘柄 × 2年 ≈  5分
  100銘柄 × 2年 ≈ 50分
  全銘柄(~4000) × 2年 ≈ 数時間〜半日 (API レート制限次第)

【中断・再開】
  Ctrl+C で安全に中断可能。再実行すると未完了銘柄から再開。
  --no-resume で最初からやり直し。
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))

# ── 保存先 ──────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"
PROGRESS_FILE = Path(__file__).resolve().parent / "data" / "download_progress.json"


# ── .env ローダ ─────────────────────────────────────────────
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


# ── 公式クライアント ────────────────────────────────────────
def get_client():
    try:
        import jquantsapi
    except ImportError:
        print("[ERROR] pip install jquants-api-client", file=sys.stderr)
        sys.exit(1)
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[ERROR] JQUANTS_API_KEY が .env or 環境変数に必要です",
              file=sys.stderr)
        sys.exit(1)
    return jquantsapi.ClientV2(api_key=api_key)


# ── 進捗管理 ────────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed": [], "failed": [], "started_at": None}


def save_progress(prog: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps(prog, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── 銘柄リスト取得 ──────────────────────────────────────────
def get_all_symbols(cli, market_filter: str | None = None) -> list[dict]:
    """上場銘柄一覧を取得し、フィルタリングして返す。"""
    print("上場銘柄一覧を取得中...", flush=True)
    df = cli.get_eq_master()
    if df is None or df.empty:
        print("[ERROR] 上場銘柄一覧の取得に失敗", file=sys.stderr)
        sys.exit(1)

    print(f"  全銘柄: {len(df)}件", flush=True)
    print(f"  カラム: {list(df.columns)}", flush=True)

    # 市場フィルタ (V2 のカラム名バリアントに対応)
    if market_filter:
        market_map = {
            "prime":    "プライム",
            "standard": "スタンダード",
            "growth":   "グロース",
        }
        keyword = market_map.get(market_filter.lower(), market_filter)
        filtered = False
        for col in ["MarketCodeName", "MarketCode", "market_code_name",
                     "Section", "section", "Market", "market"]:
            if col in df.columns:
                before = len(df)
                df = df[df[col].astype(str).str.contains(keyword, na=False)]
                if len(df) < before:
                    print(f"  {col} で '{keyword}' フィルタ: "
                          f"{before} → {len(df)}件", flush=True)
                    filtered = True
                    break
        if not filtered:
            print(f"  [warn] 市場フィルタ '{keyword}' に一致するカラムなし。"
                  f"全銘柄を対象にします。", flush=True)

    # 必要な列を抽出
    symbols = []
    for _, row in df.iterrows():
        code = str(row.get("Code", ""))
        name = str(row.get("CompanyName", row.get("company_name", "")))
        market = str(row.get("MarketCodeName", row.get("market_code_name", "")))
        if code and len(code) >= 4:
            symbols.append({"code": code, "name": name, "market": market})

    print(f"  対象銘柄: {len(symbols)}件", flush=True)
    return symbols


# ── 1銘柄ダウンロード ───────────────────────────────────────
CHUNK_SLEEP    = 2.0    # チャンク間の待機 (秒)
SYMBOL_SLEEP   = 1.0    # 銘柄間の待機 (秒)
RATE_LIMIT_BASE = 30    # 429 エラー時の最小待機 (秒)
MAX_RETRY      = 5      # 最大リトライ回数


def download_one(cli, code: str, name: str, days: int,
                 interval: str) -> pd.DataFrame | None:
    """
    1銘柄の分足をチャンクで取得。

    高速化:
      - チャンクサイズ 90日 (30→90日で API呼出し 1/3)
      - 適応的スリープ (成功時0.3秒、429時のみ長く)
      - 連続空チャンク3回でスキップ (データなし銘柄を即判定)
    """
    method_map = {
        "1m":  "get_eq_bars_minute",
        "5m":  "get_eq_bars_5minute",
        "15m": "get_eq_bars_15minute",
    }
    method_name = method_map.get(interval, "get_eq_bars_5minute")
    fetch_method = getattr(cli, method_name, None)
    if fetch_method is None:
        return None

    now = datetime.now(JST)
    start = now - timedelta(days=days)
    chunk_size = 90  # 高速化①: 30→90日 (API呼出し 1/3)

    all_dfs: list[pd.DataFrame] = []
    current = start
    empty_streak = 0
    current_sleep = 0.3  # 高速化②: 成功時は短いスリープ

    while current < now:
        if _interrupted:
            break
        chunk_end = min(current + timedelta(days=chunk_size), now)
        from_d = current.strftime("%Y%m%d")
        to_d = chunk_end.strftime("%Y%m%d")

        for attempt in range(MAX_RETRY):
            try:
                raw = fetch_method(
                    code=code, from_yyyymmdd=from_d, to_yyyymmdd=to_d
                )
                if raw is not None and not raw.empty:
                    if "Code" in raw.columns:
                        raw = raw[raw["Code"].astype(str) == code].copy()
                    if not raw.empty:
                        all_dfs.append(raw)
                        empty_streak = 0
                        current_sleep = 0.3
                    else:
                        empty_streak += 1
                else:
                    empty_streak += 1
                break
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "too many" in err_str.lower():
                    wait = RATE_LIMIT_BASE * (2 ** attempt)
                    print(f"      [429] wait {wait}s", flush=True)
                    time.sleep(wait)
                    current_sleep = 2.0
                elif attempt < MAX_RETRY - 1:
                    time.sleep(2 ** (attempt + 1))
                else:
                    print(f"      [err] {code} {from_d}-{to_d}: {err_str[:60]}",
                          file=sys.stderr)
                    empty_streak += 1

        # 高速化③: 連続空チャンク → データなしと判断してスキップ
        if empty_streak >= 3:
            break

        time.sleep(current_sleep)
        current = chunk_end + timedelta(days=1)

    if not all_dfs:
        return None

    combined = pd.concat(all_dfs, ignore_index=True)
    dt_cols = [c for c in ["Date", "Time", "DateTime"] if c in combined.columns]
    if dt_cols:
        combined = combined.drop_duplicates(subset=dt_cols, keep="last")
    return combined


# ── 保存 ────────────────────────────────────────────────────
def save_data(code: str, df: pd.DataFrame) -> tuple[Path, Path]:
    """CSV + pickle で保存。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = DATA_DIR / f"{code}.csv"
    pkl_path = DATA_DIR / f"{code}.pkl"

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    pkl_path.write_bytes(pickle.dumps(df))

    return csv_path, pkl_path


# ── Ctrl+C ハンドラ ─────────────────────────────────────────
_interrupted = False


def _signal_handler(sig, frame):
    global _interrupted
    _interrupted = True
    print("\n[中断] 現在の銘柄完了後に安全に停止します...", file=sys.stderr)


# ── メイン ──────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="全銘柄 5分足データ一括ダウンロード")
    parser.add_argument("--days", type=int, default=730,
                        help="取得日数 (デフォルト: 730日 ≈ 2年)")
    parser.add_argument("--interval", choices=["1m", "5m", "15m"],
                        default="5m")
    parser.add_argument("--limit", type=int, default=0,
                        help="取得銘柄数の上限 (テスト用、0=全件)")
    parser.add_argument("--market", choices=["prime", "standard", "growth"],
                        default=None, help="市場フィルタ")
    parser.add_argument("--no-resume", action="store_true",
                        help="進捗をリセットして最初からやり直し")
    parser.add_argument("--no-csv", action="store_true",
                        help="CSV保存をスキップ (pickle のみ)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)

    print("=" * 70)
    print(f"  全銘柄 {args.interval} データ一括ダウンロード")
    print(f"  期間: {args.days}日 ({args.days/365:.1f}年)")
    print(f"  保存先: {DATA_DIR}")
    print("=" * 70)

    cli = get_client()

    # 銘柄リスト
    symbols = get_all_symbols(cli, market_filter=args.market)
    if args.limit > 0:
        symbols = symbols[:args.limit]
        print(f"  [limit] 先頭 {args.limit} 銘柄に絞り込み")

    # 進捗
    if args.no_resume:
        prog = {"completed": [], "failed": [], "started_at": None}
    else:
        prog = load_progress()
    completed_set = set(prog["completed"])
    if prog["started_at"] is None:
        prog["started_at"] = datetime.now(JST).isoformat()

    # 未完了銘柄 (進捗JSON + ファイル存在の両方でスキップ判定)
    existing_files = set(f.stem for f in DATA_DIR.glob("*.pkl")) if DATA_DIR.exists() else set()
    skip_set = completed_set | existing_files
    todo = [s for s in symbols if s["code"] not in skip_set]
    skipped_by_file = len(existing_files - completed_set)
    print(f"\n対象: {len(symbols)}銘柄  完了済み: {len(completed_set)}  "
          f"ファイル存在スキップ: {skipped_by_file}  残り: {len(todo)}")
    print()

    if not todo:
        print("全銘柄ダウンロード完了済みです。--no-resume で再取得できます。")
        return

    total = len(todo)
    total_bars = 0
    total_bytes = 0
    start_time = time.time()
    errors = 0

    for i, sym in enumerate(todo, 1):
        if _interrupted:
            print(f"\n[中断] {i-1}/{total} 完了で停止。再実行で再開可能。")
            break

        code = sym["code"]
        name = sym["name"]
        elapsed = time.time() - start_time
        eta = (elapsed / max(i - 1, 1)) * (total - i + 1) if i > 1 else 0

        print(f"[{i:>4}/{total}] {code} {name:<20} ", end="", flush=True)

        try:
            df = download_one(cli, code, name, args.days, args.interval)
        except Exception as e:
            print(f"ERROR: {e}")
            prog["failed"].append(code)
            errors += 1
            save_progress(prog)
            continue

        if df is None or df.empty:
            print("データなし")
            prog["completed"].append(code)
            save_progress(prog)
            continue

        # 保存
        csv_path, pkl_path = save_data(code, df)
        size_kb = pkl_path.stat().st_size / 1024
        total_bars += len(df)
        total_bytes += pkl_path.stat().st_size

        # 営業日数
        if "Date" in df.columns:
            n_days = df["Date"].nunique()
        else:
            n_days = 0

        eta_str = f"{int(eta//60)}:{int(eta%60):02d}"
        print(f"{len(df):>6}本 / {n_days:>3}日  "
              f"{size_kb:>6.0f}KB  "
              f"ETA {eta_str}")

        prog["completed"].append(code)
        save_progress(prog)

        # 銘柄間の小休止 (0.2秒 — チャンク内で適応スリープ済み)
        time.sleep(0.2)

    # ── サマリー ────────────────────────────────────────────
    elapsed = time.time() - start_time
    print()
    print("=" * 70)
    print(f"  ダウンロード完了")
    print("=" * 70)
    print(f"  完了銘柄: {len(prog['completed'])} / {len(symbols)}")
    print(f"  エラー  : {errors}")
    print(f"  総バー数: {total_bars:,}")
    print(f"  総サイズ: {total_bytes / 1024 / 1024:.1f} MB")
    print(f"  所要時間: {int(elapsed//60)}分{int(elapsed%60)}秒")
    print(f"  保存先  : {DATA_DIR}")
    print()

    if prog["failed"]:
        print(f"失敗銘柄 ({len(prog['failed'])}件):")
        for code in prog["failed"]:
            print(f"  {code}")
        print("→ 再実行すると未完了銘柄のみリトライします")

    # ファイル一覧
    csv_files = list(DATA_DIR.glob("*.csv"))
    pkl_files = list(DATA_DIR.glob("*.pkl"))
    print(f"\n保存ファイル: CSV {len(csv_files)}件 / pickle {len(pkl_files)}件")

    # ロード方法の案内
    print("""
【保存データの使い方】

  # CSV から読み込み (Excel / pandas)
  import pandas as pd
  df = pd.read_csv("data/minute_5m/72030.csv")

  # pickle から高速読み込み
  import pickle
  df = pickle.loads(Path("data/minute_5m/72030.pkl").read_bytes())

  # 全銘柄ロード
  from pathlib import Path
  all_data = {}
  for f in Path("data/minute_5m").glob("*.pkl"):
      code = f.stem
      all_data[code] = pickle.loads(f.read_bytes())
""")


if __name__ == "__main__":
    main()
