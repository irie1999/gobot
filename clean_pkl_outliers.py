"""
clean_pkl_outliers.py - pkl ファイルの外れ値バーを除去
========================================================

【2モード】
- 日中央値モード (デフォルト): 各日の中央値から ±deviation_pct を超えるバーを除外
- yfinance 日足モード (--use-yfinance-daily): yfinance 日足の Low〜High 範囲外を除外
  → 真の取引範囲を権威ソースで判定するため、より正確

【発見されたバグ】
2910 ロック・フィールド の pkl で、同じ日の 5 分バー内に
二系列 (1,260 と 1,485) が混在していた。バックテストで
誤シグナル発生の原因。

【使い方】
  # yfinance 日足モード (推奨)
  python clean_pkl_outliers.py --from-watchlist daytrade_winners_2026-06-16.py --use-yfinance-daily

  # yfinance 日足モード + 実書込み
  python clean_pkl_outliers.py --from-watchlist daytrade_winners_2026-06-16.py --use-yfinance-daily --apply

  # 日中央値モード (フォールバック)
  python clean_pkl_outliers.py --from-watchlist daytrade_winners_2026-06-16.py
"""

from __future__ import annotations

import argparse
import importlib.util
import pickle
import shutil
from pathlib import Path

import pandas as pd


def fetch_yfinance_daily(ticker: str, period: str = "2y"):
    """yfinance 日足取得 (auto_adjust=False で生値)."""
    import yfinance as yf
    df = yf.download(
        ticker, period=period, interval="1d",
        auto_adjust=False, progress=False, threads=False,
    )
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.index = pd.to_datetime(df.index).date
    df.columns = [c.lower() for c in df.columns]
    return df


def clean_one(path: Path, deviation_pct: float = 5.0,
              dry_run: bool = True, backup: bool = True,
              use_yfinance_daily: bool = False,
              ticker: str | None = None,
              yf_tolerance_pct: float = 0.5,
              range_ratio: float = 2.0) -> dict:
    """1個の pkl をクリーンアップ。

    use_yfinance_daily=True 時: yfinance 日足の Low〜High を真の範囲として
    その外側のバーのみ除外。5%以下の正常な日内変動は保持される。

    use_yfinance_daily=False 時: 日中央値 ±deviation_pct を超えるバーを除外。
    """
    try:
        df = pickle.loads(path.read_bytes())
    except Exception as e:
        return {"path": str(path), "error": f"read fail: {e}"}

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {"path": str(path), "error": "empty"}

    if "Date" not in df.columns or "C" not in df.columns:
        return {"path": str(path), "error": "missing Date C"}

    df = df.copy()
    df["_DateStr"] = df["Date"].astype(str).str[:10]
    before = len(df)
    affected_dates = []

    if use_yfinance_daily:
        if ticker is None:
            return {"path": str(path), "error": "yfinance モードに ticker 必要"}
        try:
            yf_daily = fetch_yfinance_daily(ticker)
        except Exception as e:
            return {"path": str(path), "error": f"yfinance fail: {e}"}
        if yf_daily is None or yf_daily.empty:
            return {"path": str(path), "error": "yfinance 日足取得失敗"}

        # 各日について判定:
        # - intra-day 不一致 (Type A): pkl_range > yf_range * range_ratio
        #   → yfinance Low-High 範囲外のバーを除外
        # - 一貫オフセット (Type B): pkl_range ≈ yf_range だが値域が違う
        #   → corporate action と判定して触らない
        keep_mask = pd.Series(True, index=df.index)
        for date_str, group in df.groupby("_DateStr"):
            try:
                date_obj = pd.to_datetime(date_str).date()
            except Exception:
                continue
            if date_obj not in yf_daily.index:
                continue
            row = yf_daily.loc[date_obj]
            y_low = float(row["low"])
            y_high = float(row["high"])
            y_range = y_high - y_low

            pkl_high = float(group["C"].max())
            pkl_low = float(group["C"].min())
            pkl_range = pkl_high - pkl_low

            # Type A 判定: pkl 日内レンジが yf の range_ratio 倍超
            # かつ pkl レンジが絶対的にも大きい (yf range の絶対値が極小だと誤判定するため)
            is_type_a = (
                y_range > 0
                and pkl_range > y_range * range_ratio
                and pkl_range > float(row["close"]) * 0.02  # 2% 以上の幅
            )

            if not is_type_a:
                # Type B (consistent offset) or 正常日 → 触らない
                continue

            tol = float(row["close"]) * yf_tolerance_pct / 100.0
            valid_min = y_low - tol
            valid_max = y_high + tol

            bad_mask = ~((group["C"] >= valid_min) & (group["C"] <= valid_max))
            n_bad = int(bad_mask.sum())
            if n_bad > 0:
                bad_indices = group.index[bad_mask]
                keep_mask.loc[bad_indices] = False
                affected_dates.append((
                    date_str, n_bad,
                    pkl_low, pkl_high,
                    y_low, y_high,
                ))

        cleaned = df[keep_mask].drop(columns=["_DateStr"])
    else:
        # 日中央値 ±deviation_pct モード
        def keep_bars(group):
            med = group["C"].median()
            if pd.isna(med) or med == 0:
                return group
            deviation = (group["C"] - med).abs() / med
            return group[deviation <= deviation_pct / 100.0]

        cleaned = df.groupby("_DateStr", group_keys=False).apply(keep_bars)
        cleaned = cleaned.drop(columns=["_DateStr"])

        for d, g in df.groupby("_DateStr"):
            med = g["C"].median()
            if pd.isna(med) or med == 0:
                continue
            n_bad = int(((g["C"] - med).abs() / med
                         > deviation_pct / 100.0).sum())
            if n_bad > 0:
                affected_dates.append((
                    d, n_bad,
                    float(g["C"].min()), float(g["C"].max()), float(med),
                ))

    after = len(cleaned)
    removed = before - after

    result = {
        "path": str(path),
        "before": before,
        "after": after,
        "removed": removed,
        "removed_pct": removed / before * 100 if before > 0 else 0,
        "affected_dates": affected_dates,
        "mode": "yfinance_daily" if use_yfinance_daily else "median",
    }

    if not dry_run and removed > 0:
        if backup:
            backup_path = path.with_suffix(path.suffix + ".bak")
            if not backup_path.exists():
                shutil.copy2(path, backup_path)
                result["backup"] = str(backup_path)
        path.write_bytes(pickle.dumps(cleaned))
        result["saved"] = True

    return result


def load_watchlist_symbols(wl_path: Path) -> list[str]:
    """daytrade_winners_*.py から SYMBOLS を読み込み、銘柄コードのみ返す。"""
    spec = importlib.util.spec_from_file_location("wl", wl_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "SYMBOLS", [])
    syms = set()
    for e in raw:
        if isinstance(e, tuple) and len(e) >= 1:
            syms.add(e[0])
    return sorted(syms)


def yf_to_jquants(yf_code: str) -> str:
    """7203.T → 72030"""
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("paths", nargs="*", help="pkl ファイル or ディレクトリ")
    p.add_argument("--from-watchlist", default=None,
                   help="watchlist .py を読み、SYMBOLS の pkl を対象にする")
    p.add_argument("--pkl-dir", default="data/minute_5m",
                   help="pkl 格納ディレクトリ (--from-watchlist 用)")
    p.add_argument("--deviation-pct", type=float, default=5.0,
                   help="日中央値からの許容偏差 (デフォルト 5.0)")
    p.add_argument("--use-yfinance-daily", action="store_true",
                   help="yfinance 日足の Low-High 範囲を真の取引範囲とし、"
                        "その外側のバーのみ除外 (5%%以内の正常変動は保持)")
    p.add_argument("--yf-tolerance-pct", type=float, default=0.5,
                   help="yfinance モード時の許容差 (パーセント、デフォルト 0.5)")
    p.add_argument("--range-ratio", type=float, default=2.0,
                   help="Type A 判定: pkl 日内レンジが yfinance 日内レンジの "
                        "何倍を超えたら corruption とみなすか (デフォルト 2.0)。"
                        "Type B (corporate action による一貫オフセット) を保護")
    p.add_argument("--apply", action="store_true",
                   help="実際に書き込み (デフォルト dry-run)")
    p.add_argument("--no-backup", action="store_true",
                   help=".bak ファイルを作らない")
    p.add_argument("--show-affected", action="store_true",
                   help="影響を受けた日付の詳細を表示")
    args = p.parse_args()

    paths: list[tuple[Path, str | None]] = []  # (pkl_path, ticker)
    if args.from_watchlist:
        wl_path = Path(args.from_watchlist)
        if not wl_path.exists():
            print(f"[error] watchlist 不在: {wl_path}")
            return
        syms = load_watchlist_symbols(wl_path)
        print(f"[INFO] watchlist {wl_path.name}: {len(syms)}銘柄")
        pkl_dir = Path(args.pkl_dir)
        for sym in syms:
            code5 = yf_to_jquants(sym)
            pkl = pkl_dir / f"{code5}.pkl"
            if pkl.exists():
                paths.append((pkl, sym))
            else:
                print(f"  [skip] {sym}: pkl 不在 ({pkl})")

    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            for f in sorted(path.glob("*.pkl")):
                # ファイル名 (J-Quants コード) から ticker 推測
                code = f.stem
                ticker = code[:4] + ".T" if len(code) == 5 else None
                paths.append((f, ticker))
        elif path.exists():
            code = path.stem
            ticker = code[:4] + ".T" if len(code) == 5 else None
            paths.append((path, ticker))
        else:
            print(f"  [warn] 不在: {raw}")

    if not paths:
        print("[error] 対象 pkl が見つかりません")
        return

    mode_str = "実書込み (--apply)" if args.apply else "DRY-RUN (--apply で実書込み)"
    if args.use_yfinance_daily:
        clean_mode = f"yfinance 日足参照 (±{args.yf_tolerance_pct}%)"
    else:
        clean_mode = f"日中央値 ±{args.deviation_pct}%"
    print(f"対象: {len(paths)}ファイル / モード: {mode_str} / "
          f"判定: {clean_mode}")
    print()

    total_before = 0
    total_after = 0
    n_affected = 0
    for path, ticker in paths:
        result = clean_one(
            path, args.deviation_pct,
            dry_run=not args.apply,
            backup=not args.no_backup,
            use_yfinance_daily=args.use_yfinance_daily,
            ticker=ticker,
            yf_tolerance_pct=args.yf_tolerance_pct,
            range_ratio=args.range_ratio,
        )
        if "error" in result:
            print(f"  [skip] {path.name}: {result['error']}")
            continue
        total_before += result["before"]
        total_after += result["after"]
        marker = ""
        if result["removed"] > 0:
            marker = " ⚠️"
            n_affected += 1
        print(f"  {path.name}: {result['before']:>6,} → "
              f"{result['after']:>6,} "
              f"(-{result['removed']:>5,} / {result['removed_pct']:5.1f}%)"
              f"{marker}")
        if args.show_affected and result["affected_dates"]:
            for item in result["affected_dates"]:
                if args.use_yfinance_daily:
                    d, n, c_min, c_max, y_low, y_high = item
                    print(f"      {d}: {n}件除外, "
                          f"pkl範囲 {c_min:.0f}〜{c_max:.0f} / "
                          f"yf範囲 {y_low:.0f}〜{y_high:.0f}")
                else:
                    d, n, c_min, c_max, med = item
                    print(f"      {d}: {n}件除外, "
                          f"範囲 {c_min:.0f}〜{c_max:.0f} "
                          f"(中央値 {med:.0f})")

    print()
    print(f"合計: {total_before:,} → {total_after:,} "
          f"(-{total_before - total_after:,}) / "
          f"破損あり: {n_affected}/{len(paths)} ファイル")
    if not args.apply and total_before > total_after:
        print()
        print("【次のステップ】 実際に書き込むには --apply を付けて再実行してください")
        print("                 (元ファイルは自動で .bak バックアップが作られます)")


if __name__ == "__main__":
    main()
