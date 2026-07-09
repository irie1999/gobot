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


# .env — swingtrade だけでなく近隣フォルダ(kabu station直下・daytrading・
# stock_5min 等)も探索して JQUANTS_API_KEY 等を読む。データ場所と同じ思想。
def _load_dotenv():
    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".env",
        here / ".env",
        here.parent / ".env",              # "kabu station" 直下
        here.parent / "daytrading" / ".env",
        here.parent / "stock_5min" / ".env",
    ]
    try:
        from daytrade_data import DATA_DIR as _DD  # stock_5min 等
        candidates += [Path(_DD) / ".env", Path(_DD).parent / ".env"]
    except Exception:
        pass
    seen = set()
    for p in candidates:
        p = p.resolve()
        if p in seen or not p.exists():
            continue
        seen.add(p)
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        if os.environ.get("JQUANTS_API_KEY"):
            return   # キーが取れたら十分


_load_dotenv()


def get_client():
    import jquantsapi
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[ERROR] JQUANTS_API_KEY が必要です", file=sys.stderr)
        sys.exit(1)
    return jquantsapi.ClientV2(api_key=api_key)


def get_last_date(pkl_path: Path) -> str | None:
    """pkl ファイルの最終日(YYYY-MM-DD)を取得。

    stock_5min は正規化済み(DatetimeIndex・小文字ohlcv)で保存されているため、
    まず index から読む。旧 raw 形式(Date列)にもフォールバック対応。
    """
    try:
        df = pickle.loads(pkl_path.read_bytes())
        if df is None or getattr(df, "empty", True):
            return None
        # 正規化済み(DatetimeIndex)
        if isinstance(df.index, pd.DatetimeIndex) and len(df.index):
            return str(df.index.max())[:10]
        # 旧 raw 形式(Date列)
        if hasattr(df, "columns") and "Date" in df.columns:
            return str(df["Date"].max())[:10]
        # それ以外は正規化して読む
        from daytrade_data import normalize_minute_df
        n = normalize_minute_df(df)
        if n is not None and not n.empty:
            return str(n.index.max())[:10]
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


_PREVIEW_SHOWN = False


def jq_to_yf(code: str) -> str:
    """J-Quants 5桁コード → yfinance ティッカー。 72030 → 7203.T / 130A0 → 130A.T"""
    c = str(code).strip().upper()
    if len(c) == 5 and c.endswith("0"):
        return c[:4] + ".T"
    if len(c) == 4:
        return c + ".T"
    return (c[:-1] if c.endswith("0") and len(c) > 4 else c) + ".T"


def fetch_and_append(cli, code: str, pkl_path: Path,
                     from_date: str, to_date: str, dry_run: bool = False,
                     source: str = "yfinance", fetch_days: int = 7) -> int:
    """5分足を取得し、既存(正規化済み)pklに「後ろだけ」追記する。

    source:
      "yfinance" (既定) : daytrade_data.load_intraday(source='yfinance') で取得。
                          index が datetime のまま正規化され、時刻を保持する。
                          J-Quants サブスク終了後の日次運用はこちら。
      "jquants"         : cli.get_eq_bars_5minute(raw) を normalize_minute_df で正規化。

    安全設計 (共通):
      - stock_5min と同一の正規化形式(DatetimeIndex・小文字ohlcv)に揃えてから結合。
        fetch_intraday が時刻を落とす問題は使わないので発生しない。
      - 既存 index の最大時刻より「後」のバーだけ追加。既存バーは一切書き換えない。
      - dry_run=True なら書き込まず、追加されるバー数だけ返す(プレビュー)。
    返り値: 追加される(された)新バー数。
    """
    global _PREVIEW_SHOWN
    from daytrade_data import normalize_minute_df, load_intraday

    if source == "yfinance":
        yf_sym = jq_to_yf(code)
        new = load_intraday(yf_sym, days=fetch_days, source="yfinance")
        if new is None or new.empty:
            return 0
        if not isinstance(new.index, pd.DatetimeIndex):
            new = normalize_minute_df(new)
    else:
        from_d = from_date.replace("-", "")
        to_d = to_date.replace("-", "")
        try:
            raw_new = cli.get_eq_bars_5minute(
                code=code, from_yyyymmdd=from_d, to_yyyymmdd=to_d
            )
        except Exception:
            return 0
        if raw_new is None or raw_new.empty:
            return 0
        if "Code" in raw_new.columns:
            raw_new = raw_new[raw_new["Code"].astype(str) == code].copy()
        if raw_new.empty:
            return 0
        new = normalize_minute_df(raw_new)
    if new is None or new.empty:
        return 0

    # 既存(正規化済み)を読み込む
    try:
        old = pickle.loads(pkl_path.read_bytes())
    except Exception:
        old = pd.DataFrame()
    if old is not None and not old.empty and not isinstance(old.index, pd.DatetimeIndex):
        old = normalize_minute_df(old)   # 万一 raw 形式でも揃える

    # 既存の最大時刻より「後」の新バーだけを採用(既存は絶対に触らない)
    if old is not None and not old.empty:
        new_only = new[new.index > old.index.max()]
    else:
        new_only = new
    if new_only.empty:
        return 0

    # プレビュー: 最初の1銘柄だけ、追加内容(時刻が保持されているか)を表示
    if not _PREVIEW_SHOWN:
        _PREVIEW_SHOWN = True
        print(f"  [プレビュー] {code}: 追加 {len(new_only)}本  "
              f"{new_only.index.min()} 〜 {new_only.index.max()}")
        print("    追加末尾:")
        for ts, row in new_only.tail(2).iterrows():
            print(f"      {ts}  o={row['open']} h={row['high']} "
                  f"l={row['low']} c={row['close']} v={row['volume']}")

    if dry_run:
        return len(new_only)

    combined = (pd.concat([old, new_only])
                if (old is not None and not old.empty) else new_only)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    combined.index.name = "DateTime"
    pkl_path.write_bytes(pickle.dumps(combined))
    return len(new_only)


def main():
    parser = argparse.ArgumentParser(
        description="毎日の5分足データ自動取得 + 欠損補完")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理銘柄数の上限 (テスト用)")
    parser.add_argument("--check-only", action="store_true",
                        help="欠損確認のみ (取得しない)")
    parser.add_argument("--dry-run", action="store_true",
                        help="取得して追加内容をプレビューするが書き込まない (安全確認用)")
    parser.add_argument("--source", choices=["yfinance", "jquants"], default="yfinance",
                        help="取得元 (既定 yfinance。J-Quantsサブスク終了後は yfinance)")
    parser.add_argument("--fetch-days", type=int, default=7,
                        help="yfinance で遡って取得する日数 (欠損補完の余裕。既定7)")
    parser.add_argument("--daytrade-only", action="store_true",
                        help="daytrade_symbols.py の20銘柄のみ更新 (日次運用用)")
    args = parser.parse_args()

    print(f"  保存先(更新対象): {DATA_DIR}")
    print(f"  取得元: {args.source}"
          + (f" (直近{args.fetch_days}日)" if args.source == "yfinance" else "")
          + ("  ★DRY-RUN(書き込みなし)" if args.dry_run else ""))
    if not DATA_DIR.exists():
        print(f"[ERROR] 5分足フォルダが見つかりません: {DATA_DIR}\n"
              "  環境変数 MINUTE_5M_DIR で明示指定するか、stock_5min の場所を確認してください。",
              file=sys.stderr)
        sys.exit(1)

    # J-Quants クライアントは source=jquants のときだけ必要
    cli = get_client() if args.source == "jquants" else None
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

        # 取得 (yfinance 既定 / 既存より後のバーだけ追記)
        bars = fetch_and_append(cli, code, pkl_path, fetch_from, today,
                                dry_run=args.dry_run, source=args.source,
                                fetch_days=max(args.fetch_days, gap_days + 2))
        if bars > 0:
            total_bars += bars
            updated += 1
        # bars==0 は「新規バー無し(最新済み)」も含むので error 扱いにしない
        # (旧実装は0をerrorにしていたが誤解を招くため updated/skip のみで集計)

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
