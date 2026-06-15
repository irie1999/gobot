"""
yfinance_update.py  ―  yfinance から最新5分足を取得 + 既存pklに追記
==================================================================
J-Quants の有料プランがなくても、yfinance で最大60日分の5分足が取れる。
既存の data/minute_5m/*.pkl と同じ形式で保存し、daytrade_data.py 互換。

【動作】
  1. data/minute_5m/*.pkl の各銘柄について最終日確認
  2. yfinance で最新N日分を取得 (デフォルト30日)
  3. J-Quants形式に変換して既存に追記
  4. 重複バー自動除去

【使い方】
  python yfinance_update.py                  # 全銘柄、過去30日
  python yfinance_update.py --days 60        # 過去60日 (yfinance上限)
  python yfinance_update.py --workers 5      # 並列5
  python yfinance_update.py --limit 100      # テスト
  python yfinance_update.py --daytrade-only  # winners銘柄のみ
  python yfinance_update.py --check-only     # 取得せず最終日確認

【yfinance制約】
  - 5分足は最大60日分
  - 銘柄コード形式: 7203.T (4桁+.T)
  - レート制限: 並列5以下推奨

【yfinanceとJ-Quantsの形式変換】
  yfinance: DatetimeIndex + Open/High/Low/Close/Volume
  J-Quants: DateTime/Date/Time列 + Open/High/Low/Close/Volume
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"


def jquants_code_to_yf(code5: str) -> str:
    """J-Quantsの5桁コード '72030' → yfinance形式 '7203.T'"""
    code = code5.strip()
    if len(code) == 5:
        code = code[:4]
    return f"{code}.T"


def yf_df_to_jquants(df: pd.DataFrame, code5: str) -> pd.DataFrame:
    """yfinance形式 → J-Quants V2形式 (DateTime/Date/Time列)"""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # MultiIndexカラム対応 (yfinance 0.2.x で単一ティッカーでも MultiIndex)
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [c[0] if isinstance(c, tuple) else c for c in out.columns]

    # 重複カラム除去 (concat後やMultiIndex flatten で発生しうる)
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()]

    # tz統一 (Asia/Tokyo に変換、tz-naive 化)
    if out.index.tz is not None:
        out.index = out.index.tz_convert("Asia/Tokyo").tz_localize(None)

    # DateTime/Date/Time列を追加
    out["DateTime"] = out.index
    out["Date"] = out.index.strftime("%Y-%m-%d")
    out["Time"] = out.index.strftime("%H:%M")
    out["Code"] = code5

    # OHLCV カラム名を J-Quants V2 短縮形式 (O/H/L/C/Vo) に統一
    # 既存pkl が J-Quants 由来の場合に concat 時のカラム不一致を防ぐ
    rename = {}
    for src, dst in [("Open", "O"), ("High", "H"), ("Low", "L"),
                     ("Close", "C"), ("Volume", "Vo")]:
        if src in out.columns:
            rename[src] = dst
        elif src.lower() in out.columns:
            rename[src.lower()] = dst
    out = out.rename(columns=rename)
    # Va (売買代金) を作成: J-Quants 形式互換
    if "O" in out.columns and "Vo" in out.columns and "Va" not in out.columns:
        out["Va"] = pd.to_numeric(out["O"], errors="coerce") * \
                    pd.to_numeric(out["Vo"], errors="coerce")

    # 必要列を絞る
    cols = ["DateTime", "Date", "Time", "Code", "O", "H", "L", "C", "Vo", "Va"]
    cols = [c for c in cols if c in out.columns]
    out = out[cols].reset_index(drop=True)

    # 数値変換
    for c in ["O", "H", "L", "C", "Vo", "Va"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "C" in out.columns:
        out = out.dropna(subset=["C"])

    return out


def get_last_date(pkl_path: Path) -> str | None:
    """既存pklの最終日を YYYY-MM-DD で返す。"""
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
        pass
    return None


def fetch_yfinance(code5: str, days: int) -> pd.DataFrame:
    """yfinanceから5分足取得。"""
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance未インストール: pip install yfinance")

    ticker = jquants_code_to_yf(code5)
    df = yf.download(
        ticker, period=f"{days}d", interval="5m",
        auto_adjust=True, progress=False, threads=False,  # 株式分割等を自動調整
    )
    if df is None or df.empty:
        return pd.DataFrame()

    return yf_df_to_jquants(df, code5)


def update_one(pkl_path: Path, days: int) -> tuple:
    """1銘柄を yfinanceで取得して既存pklに追記。"""
    code5 = pkl_path.stem
    try:
        df_new = fetch_yfinance(code5, days)
        if df_new.empty:
            return ("err", 0, code5, "no data")

        # 既存読込
        try:
            df_old = pickle.loads(pkl_path.read_bytes())
            if not isinstance(df_old, pd.DataFrame):
                df_old = pd.DataFrame()
            # 既存pklに重複カラムがあれば除去
            if not df_old.empty and df_old.columns.duplicated().any():
                df_old = df_old.loc[:, ~df_old.columns.duplicated()]
        except Exception:
            df_old = pd.DataFrame()

        # 結合 + 重複除去
        if not df_old.empty:
            # カラムを揃える (df_old のカラムに合わせる)
            common_cols = [c for c in df_new.columns if c in df_old.columns]
            if common_cols:
                df_new = df_new[common_cols]
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
            # concat 後の重複カラムも除去
            if df_combined.columns.duplicated().any():
                df_combined = df_combined.loc[:, ~df_combined.columns.duplicated()]
        else:
            df_combined = df_new

        # 重複除去 (DateTime か Date+Time で)
        if "DateTime" in df_combined.columns:
            df_combined = df_combined.drop_duplicates(
                subset=["DateTime"], keep="last"
            ).sort_values("DateTime").reset_index(drop=True)
        elif "Date" in df_combined.columns and "Time" in df_combined.columns:
            df_combined = df_combined.drop_duplicates(
                subset=["Date", "Time"], keep="last"
            ).sort_values(["Date", "Time"]).reset_index(drop=True)

        # 保存
        pkl_path.write_bytes(pickle.dumps(df_combined))
        return ("ok", len(df_new), code5, "")
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()
        # 最後の3行 (実際にエラー出した場所) を返す
        loc = " | ".join(tb[-3:]) if len(tb) >= 3 else str(e)
        return ("err", 0, code5, f"{type(e).__name__}: {str(e)[:60]} @ {loc[:200]}")


def main():
    parser = argparse.ArgumentParser(
        description="yfinance から最新5分足取得 + 既存pklに追記")
    parser.add_argument("--days", type=int, default=30,
                        help="取得日数 (1-60、デフォルト30、最大60)")
    parser.add_argument("--workers", type=int, default=3,
                        help="並列スレッド数 (デフォルト3、推奨3-5)")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理銘柄数の上限 (テスト用)")
    parser.add_argument("--daytrade-only", action="store_true",
                        help="winners銘柄のみ更新")
    parser.add_argument("--prime-only", action="store_true",
                        help="東証プライム銘柄 (約1800) のみ更新")
    parser.add_argument("--watchlist-file", default=None,
                        help="指定モジュールの SYMBOLS リストのみ更新 "
                             "(例: daytrade_combined_watchlist.py)。"
                             "--daytrade-only / --prime-only より優先")
    parser.add_argument("--stale-days", type=int, default=0,
                        help="最終日が N 日以上前の銘柄のみ更新 (0=全て)")
    parser.add_argument("--check-only", action="store_true",
                        help="最終日確認のみ (取得しない)")
    args = parser.parse_args()

    if args.days > 60:
        print("[warn] yfinance 5分足は最大60日 → 60に調整")
        args.days = 60

    if not DATA_DIR.exists():
        print(f"[error] {DATA_DIR} が存在しません")
        sys.exit(1)

    # 対象pkl決定
    if args.watchlist_file:
        try:
            import importlib.util
            p = Path(args.watchlist_file)
            if not p.exists():
                print(f"[error] {args.watchlist_file} がありません")
                sys.exit(1)
            spec = importlib.util.spec_from_file_location("wl", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            SYMBOLS = getattr(mod, "SYMBOLS", [])
            target_codes = [e[0].replace(".T", "") + "0" for e in SYMBOLS]
            pkl_files = [DATA_DIR / f"{c}.pkl" for c in target_codes
                         if (DATA_DIR / f"{c}.pkl").exists()]
            print(f"WATCHLIST ({p.name}): {len(pkl_files)}/{len(target_codes)}銘柄")
        except Exception as e:
            print(f"[error] WATCHLIST 読み込み失敗: {e}")
            sys.exit(1)
    elif args.daytrade_only:
        try:
            from daytrade_donchian_winners import SYMBOLS
            target_codes = [s.replace(".T", "") + "0" for s, _ in SYMBOLS]
            pkl_files = [DATA_DIR / f"{c}.pkl" for c in target_codes
                         if (DATA_DIR / f"{c}.pkl").exists()]
            print(f"winners対象: {len(pkl_files)}銘柄")
        except ImportError:
            print("[error] daytrade_donchian_winners.py がありません")
            sys.exit(1)
    elif args.prime_only:
        try:
            from symbols_listed_all import SYMBOLS
            target_codes = [s.replace(".T", "") + "0" for s, _ in SYMBOLS]
            pkl_files = [DATA_DIR / f"{c}.pkl" for c in target_codes
                         if (DATA_DIR / f"{c}.pkl").exists()]
            missing = len(target_codes) - len(pkl_files)
            print(f"prime対象: {len(pkl_files)}/{len(target_codes)}銘柄"
                  f" (pkl未存在: {missing})")
        except ImportError:
            print("[error] symbols_listed_all.py がありません。"
                  "先に download_tse_symbols.py --market prime を実行")
            sys.exit(1)
    else:
        pkl_files = sorted(DATA_DIR.glob("*.pkl"))
        print(f"全銘柄対象: {len(pkl_files)}銘柄")

    # stale-days フィルタ: 最終日が N 日以上前の銘柄のみ
    if args.stale_days > 0:
        today_dt = datetime.now(JST).date()
        cutoff = today_dt - timedelta(days=args.stale_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        print(f"stale-days {args.stale_days}: 最終日 < {cutoff_str} のみ対象")
        filtered = []
        for pkl in pkl_files:
            last = get_last_date(pkl)
            if last is None or last < cutoff_str:
                filtered.append(pkl)
        print(f"  → {len(filtered)}/{len(pkl_files)}銘柄が対象")
        pkl_files = filtered

    if args.limit > 0:
        pkl_files = pkl_files[:args.limit]
        print(f"テスト制限: {args.limit}銘柄")

    if args.check_only:
        # check-onlyモード
        print()
        today = datetime.now(JST).strftime("%Y-%m-%d")
        for pkl in pkl_files[:50]:
            last = get_last_date(pkl)
            if last and last < today:
                gap = (datetime.strptime(today, "%Y-%m-%d") -
                       datetime.strptime(last, "%Y-%m-%d")).days
                if gap > 1:
                    print(f"  {pkl.stem}: {last}  ({gap}日欠損)")
        return

    print(f"yfinance更新開始 (days={args.days}, workers={args.workers})")
    print(f"目安時間: {len(pkl_files) // args.workers // 30}〜"
          f"{len(pkl_files) // args.workers // 15}分")
    print()

    updated = 0
    errors = 0
    total_bars = 0
    completed = 0
    err_samples = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(update_one, p, args.days): p for p in pkl_files}
        for future in as_completed(futures):
            completed += 1
            try:
                status, bars, code, msg = future.result()
                if status == "ok":
                    updated += 1
                    total_bars += bars
                else:
                    errors += 1
                    if len(err_samples) < 5:
                        err_samples.append(f"{code}: {msg}")
            except Exception as e:
                errors += 1
            if completed % 50 == 0 or completed == len(pkl_files):
                print(f"  {completed}/{len(pkl_files)} 完了 "
                      f"(更新:{updated} エラー:{errors} +バー:{total_bars:,})",
                      flush=True)

    print()
    print("=" * 60)
    print(f"  yfinance更新完了")
    print(f"  処理: {len(pkl_files)}銘柄")
    print(f"  更新: {updated}  エラー: {errors}")
    print(f"  追加バー: {total_bars:,}")
    if err_samples:
        print(f"\n  エラー例 (最大5件):")
        for s in err_samples:
            print(f"    {s}")
    print("=" * 60)


if __name__ == "__main__":
    main()
