"""
scan_pairs.py  ―  ペアトレード 共和分ペアスキャン
==================================================
WATCHLIST の全銘柄ペアに Engle-Granger 共和分検定を実施し、
p値 < 閾値 のペアを pairs_scan_<date>.csv に保存する。

【使い方】
  python scan_pairs.py               # デフォルト (365日, p<0.05)
  python scan_pairs.py --days 730    # 2年
  python scan_pairs.py --p 0.01      # p値閾値を厳しく
  python scan_pairs.py --min-r2 0.7  # 最低R²
  python scan_pairs.py --no-browser
"""

from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import itertools
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from statsmodels.tsa.stattools import coint
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant
    _HAS_STATSMODELS = True
except ImportError:
    _HAS_STATSMODELS = False

from backtest_limit_entry import fetch, WORKERS as _DEFAULT_WORKERS

JST = timezone(timedelta(hours=9))

# ── WATCHLIST ─────────────────────────────────────────────────────────────────
# check_signals_stop.py の WATCHLIST からシンボルを抽出
STOP_WATCHLIST_SYMS: list[tuple[str, str]] = [
    ("5821.T", "平河ヒューテック"),
    ("4368.T", "扶桑化学工業"),
    ("7762.T", "シチズン時計"),
    ("4973.T", "日本高純度化学"),
    ("6101.T", "ツガミ"),
    ("4023.T", "クレハ"),
    ("8341.T", "七十七銀行"),
    ("4044.T", "セントラル硝子"),
    ("7747.T", "朝日インテック"),
    ("6136.T", "オーエスジー"),
    ("3395.T", "サンマルクHD"),
    ("4116.T", "大日精化工業"),
    ("8386.T", "百十四銀行"),
    ("6752.T", "パナソニックHD"),
    ("7384.T", "プロクレアHD"),
    ("6702.T", "富士通"),
    ("1861.T", "熊谷組"),
    ("4028.T", "石原産業"),
    ("7201.T", "日産自動車"),
    ("9984.T", "ソフトバンクグループ"),
    ("6417.T", "SANKYO"),
    ("3665.T", "エニグモ"),
    ("7381.T", "CCIグループ"),
    ("8381.T", "山陰合同銀行"),
    ("9507.T", "四国電力"),
    ("9508.T", "九州電力"),
    ("3132.T", "マクニカHD"),
    ("7167.T", "めぶきフィナンシャルG"),
    ("1605.T", "INPEX"),
    ("4189.T", "KHネオケム"),
    ("4631.T", "DIC"),
]

# check_signals_breakout.py の WATCHLIST からも追加
BRK_WATCHLIST_SYMS: list[tuple[str, str]] = [
    ("6745.T", "ホーチキ"),
    ("7380.T", "十六フィナンシャルグループ"),
    ("5830.T", "いよぎんホールディングス"),
    ("6498.T", "キッツ"),
    ("1332.T", "ニッスイ"),
    ("1979.T", "大氣社"),
    ("1982.T", "日比谷総合設備"),
    ("3482.T", "ロードスターキャピタル"),
    ("4401.T", "ADEKA"),
    ("7182.T", "ゆうちょ銀行"),
    ("4114.T", "日本触媒"),
    ("6058.T", "ベクトル"),
    ("4310.T", "ドリームインキュベータ"),
    ("6113.T", "アマダ"),
    ("4221.T", "大倉工業"),
    ("6310.T", "井関農機"),
    ("4343.T", "イオンファンタジー"),
]

# 重複を除いてまとめる
ALL_SYMS: list[tuple[str, str]] = list(dict.fromkeys(STOP_WATCHLIST_SYMS + BRK_WATCHLIST_SYMS))

# シグナル閾値 (デフォルト)
Z_ENTRY_DEFAULT = 2.0


def _fetch_log_prices(symbol: str, days: int) -> pd.Series | None:
    """終値の自然対数系列を取得。失敗時は None を返す。"""
    df = fetch(symbol, days)
    if df is None or len(df) < 60:
        return None
    # 指定日数分だけ使う
    cutoff = pd.Timestamp(datetime.now(JST).date()) - pd.Timedelta(days=days)
    df = df[df.index >= cutoff]
    if len(df) < 60:
        return None
    close = df["close"].astype(float)
    if (close <= 0).any():
        return None
    return np.log(close).rename(symbol)


def _calc_hedge_ratio_r2(log_a: pd.Series, log_b: pd.Series) -> tuple[float, float]:
    """OLS で log_A ~ β * log_B + α を推定。(β, R²) を返す。"""
    common = log_a.index.intersection(log_b.index)
    if len(common) < 60:
        return 0.0, 0.0
    ya = log_a.loc[common].values
    xb = log_b.loc[common].values
    xb_const = add_constant(xb)
    res = OLS(ya, xb_const).fit()
    beta = float(res.params[1]) if len(res.params) > 1 else float(res.params[0])
    r2   = float(res.rsquared)
    return beta, r2


def _scan_pair(
    sym_a: str, name_a: str,
    sym_b: str, name_b: str,
    log_a: pd.Series, log_b: pd.Series,
    p_threshold: float,
    min_r2: float,
    z_entry: float,
) -> dict | None:
    """1ペアの共和分検定を実行。採用基準を満たせば結果 dict を返す。"""
    common = log_a.index.intersection(log_b.index)
    if len(common) < 60:
        return None

    la = log_a.loc[common]
    lb = log_b.loc[common]

    try:
        _, pval, _ = coint(la.values, lb.values)
    except Exception:
        return None

    if pval >= p_threshold:
        return None

    beta, r2 = _calc_hedge_ratio_r2(la, lb)
    if r2 < min_r2:
        return None

    # スプレッド = log_A - β * log_B
    spread = la - beta * lb

    # 252日ローリング統計（全期間が252日未満なら全体平均を使う）
    if len(spread) >= 252:
        roll_mean = spread.rolling(252).mean()
        roll_std  = spread.rolling(252).std()
        s_mean    = float(roll_mean.iloc[-1])
        s_std     = float(roll_std.iloc[-1])
    else:
        s_mean = float(spread.mean())
        s_std  = float(spread.std())

    if s_std <= 0 or np.isnan(s_std):
        return None

    z_current = (float(spread.iloc[-1]) - s_mean) / s_std

    # シグナル判定
    if z_current > z_entry:
        signal = "sell_a_buy_b"   # Aが割高、Bが割安
    elif z_current < -z_entry:
        signal = "buy_a_sell_b"   # Aが割安、Bが割高
    else:
        signal = "neutral"

    return dict(
        sym_a=sym_a,
        name_a=name_a,
        sym_b=sym_b,
        name_b=name_b,
        coint_pvalue=round(pval, 6),
        hedge_ratio=round(beta, 6),
        r_squared=round(r2, 6),
        spread_mean=round(s_mean, 6),
        spread_std=round(s_std, 6),
        z_score_current=round(z_current, 4),
        signal=signal,
    )


def run_scan(
    days: int = 365,
    p_threshold: float = 0.05,
    min_r2: float = 0.5,
    workers: int = _DEFAULT_WORKERS,
    z_entry: float = Z_ENTRY_DEFAULT,
) -> pd.DataFrame:
    """全ペアスキャンを実行して DataFrame を返す。"""
    if not _HAS_STATSMODELS:
        print("[ERROR] statsmodels がインストールされていません。", file=sys.stderr)
        print("  pip install statsmodels", file=sys.stderr)
        return pd.DataFrame()

    n = len(ALL_SYMS)
    print(f"データ取得中 ({n}銘柄, {days}日)...", flush=True)

    # 全銘柄のログ価格を並列取得
    log_prices: dict[str, pd.Series] = {}
    name_map: dict[str, str] = {sym: name for sym, name in ALL_SYMS}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_log_prices, sym, days): (sym, name)
                for sym, name in ALL_SYMS}
        done = 0
        for fut in as_completed(futs):
            sym, name = futs[fut]
            done += 1
            try:
                result = fut.result()
                if result is not None:
                    log_prices[sym] = result
            except Exception as e:
                print(f"  [WARN] {sym} 取得失敗: {e}", file=sys.stderr)
            if done % 10 == 0 or done == n:
                print(f"  データ取得 {done}/{n}", flush=True)

    valid_syms = [(sym, name_map[sym]) for sym, _ in ALL_SYMS if sym in log_prices]
    n_pairs = len(valid_syms) * (len(valid_syms) - 1) // 2
    print(f"有効銘柄: {len(valid_syms)}/{n}  ペア数: {n_pairs}", flush=True)

    # 全ペアの共和分検定 (並列)
    pairs = list(itertools.combinations(valid_syms, 2))
    print(f"共和分検定開始 ({len(pairs)}ペア, p<{p_threshold}, R²≥{min_r2})...", flush=True)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs2 = {
            ex.submit(
                _scan_pair,
                sym_a, name_a, sym_b, name_b,
                log_prices[sym_a], log_prices[sym_b],
                p_threshold, min_r2, z_entry,
            ): (sym_a, sym_b)
            for (sym_a, name_a), (sym_b, name_b) in pairs
        }
        done2 = 0
        for fut in as_completed(futs2):
            done2 += 1
            try:
                r = fut.result()
                if r is not None:
                    results.append(r)
            except Exception:
                pass
            if done2 % 50 == 0 or done2 == len(pairs):
                print(f"  検定 {done2}/{len(pairs)}  採用: {len(results)}", flush=True)

    if not results:
        print("採用ペアなし。p値閾値またはR²条件を緩めてください。", flush=True)
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.sort_values("coint_pvalue").reset_index(drop=True)
    print(f"\n採用ペア: {len(df)}件", flush=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="ペアトレード 共和分ペアスキャン")
    parser.add_argument("--days",       type=int,   default=365,            help="取得日数 (デフォルト365)")
    parser.add_argument("--p",          type=float, default=0.05,           help="p値閾値 (デフォルト0.05)")
    parser.add_argument("--min-r2",     type=float, default=0.5,            help="最低R² (デフォルト0.5)")
    parser.add_argument("--z-entry",    type=float, default=Z_ENTRY_DEFAULT, help=f"Z-scoreシグナル閾値 (デフォルト{Z_ENTRY_DEFAULT})")
    parser.add_argument("--workers",    type=int,   default=_DEFAULT_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    df = run_scan(
        days=args.days,
        p_threshold=args.p,
        min_r2=args.min_r2,
        workers=args.workers,
        z_entry=args.z_entry,
    )

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    out_path  = Path(f"pairs_scan_{today_str}.csv")

    if df.empty:
        print(f"出力なし。{out_path} は生成されません。")
        return

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\nCSV出力: {out_path.resolve()}")

    # 結果サマリーをコンソール表示
    print(f"\n{'sym_a':<10} {'name_a':<12} {'sym_b':<10} {'name_b':<12} {'p値':>8} {'β':>8} {'R²':>6} {'Z':>8} signal")
    print("-" * 90)
    for _, row in df.iterrows():
        sig_mark = "★" if row["signal"] != "neutral" else " "
        print(f"{row['sym_a']:<10} {row['name_a'][:10]:<12} {row['sym_b']:<10} {row['name_b'][:10]:<12}"
              f" {row['coint_pvalue']:>8.4f} {row['hedge_ratio']:>8.4f}"
              f" {row['r_squared']:>6.3f} {row['z_score_current']:>8.3f}"
              f" {sig_mark}{row['signal']}")

    active = df[df["signal"] != "neutral"]
    print(f"\nアクティブシグナル: {len(active)}件")
    if not active.empty:
        for _, row in active.iterrows():
            direction = "A売・B買" if row["signal"] == "sell_a_buy_b" else "A買・B売"
            print(f"  [{direction}] {row['sym_a']}({row['name_a']}) / {row['sym_b']}({row['name_b']})"
                  f"  Z={row['z_score_current']:+.3f}  p={row['coint_pvalue']:.4f}")

    print(f"\n次のステップ: python run_pairs.py --csv {out_path}")


if __name__ == "__main__":
    main()
