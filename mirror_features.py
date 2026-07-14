"""mirror_features.py — 全ブレイク信号に特徴量を付与し、1日ミラー損益との関係を発見する。

BT/regime に縛られず、価格帯の全銘柄でブレイク信号(上=ロングミラー / 下=ショートミラー)
を集め、各信号の市場特徴量と「翌日フェード(ミラー)損益」を記録する。
どの特徴量がフェード成功を予測するかを5分位バケット分析で炙り出す。

思想: ミラー = 「ブレイクはダマシ=反転する」に賭ける。上ブレイクは空売り、下ブレイクは買い。
      どのブレイクが失敗するかを、BTでなく直接指標(出来高/過伸び/ギャップ/RSI/…)から発見する。

使い方:
  python mirror_features.py --limit 200                 # まず200銘柄で試す(推奨)
  python mirror_features.py --short                     # 下ブレイク(ショートミラー)も含める
  python mirror_features.py --days 1095 --workers 8     # 3年・並列8
  python mirror_features.py --min-price 1000 --max-price 6000 --out mf.csv

出力: データセットCSV(信号1行=特徴量+ミラー損益) + コンソールにバケット分析
※ 選定は"しない"。全信号を記録し、結果から「どの特徴量で絞ると勝てるか」を発見する用。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd

import backtest_limit_entry as ble

ap = argparse.ArgumentParser()
ap.add_argument("--symbols", default=None,
                help="ユニバースファイル(省略時 symbols_listed_prime.py → symbols_all.py)")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(デバッグ)")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--days", type=int, default=365 * 3, help="バックテスト期間(日)")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--short", action="store_true", help="ショートミラー(下ブレイク→買い)も含める")
ap.add_argument("--out", default="mirror_features.csv")
ap.add_argument("--split-date", default=None,
                help="この日付でTRAIN(前)/TEST=OOS(後)に分割。TRAINで見つけた帯がTESTで再現するか検証 例2025-07-01")
ap.add_argument("--slip", type=float, default=0.0,
                help="スリッページ(既定0=幻の利益を排除)。0以外だとミラーが水増しされるので特徴発見には0推奨")
ap.add_argument("--fee", type=float, default=0.0, help="片道手数料(既定0)")
ap.add_argument("--aggressive", action="store_true")
args = ap.parse_args()

# ミラー特徴発見はコストゼロ(素の価格方向)で見る。既定0でモジュール定数を上書き。
ble.SLIPPAGE_STOP_PCT = args.slip
ble.SLIPPAGE_LIMIT_PCT = args.slip
ble.FEE_PCT_ONE_WAY = args.fee

# ── 戦略モジュール(フラットに全戦略の信号を集める) ─────────────────────────────
import check_signals_stop as _stop           # noqa: E402
import check_signals_breakout as _brk         # noqa: E402
SIDE_MODS = [("long", _stop), ("long", _brk)]
if args.short:
    import check_signals_short as _s1          # noqa: E402
    import check_signals_short_breakout as _s2  # noqa: E402
    SIDE_MODS += [("short", _s1), ("short", _s2)]

FEAT_COLS = ["price", "ext_ma5", "ext_ma25", "gap", "rsi2", "rsi14", "up_days",
             "atr_ratio", "ma200_pos", "ma200_slope", "vol_ratio", "ret5", "ret20"]


def load_universe(path):
    import importlib.util
    cands = ([path] if path else []) + [
        "symbols_listed_prime.py", "symbols_listed_all.py",
        "symbols_listed_standard.py", "symbols_all.py"]
    for cand in cands:
        p = Path(cand)
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return [tuple(t) for t in mod.SYMBOLS], p.name
        except Exception:
            continue
    return [], "(なし)"


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """特徴量計算用のインジケータ列を付ける(元のOHLCV/atrは保持)。"""
    df = df.copy()
    c = df["close"]
    df["ma5"] = c.rolling(5).mean()
    df["ma25"] = c.rolling(25).mean()
    df["ma200"] = c.rolling(200).mean()
    df["ma200_slope"] = df["ma200"].pct_change(20) * 100
    for n in (2, 14):
        d = c.diff()
        up = d.clip(lower=0).rolling(n).mean()
        dn = (-d.clip(upper=0)).rolling(n).mean()
        rs = up / dn.replace(0, np.nan)
        df[f"rsi{n}"] = 100 - 100 / (1 + rs)
    upd = (c > c.shift(1)).astype(int)
    grp = (upd != upd.shift()).cumsum()
    df["up_days"] = upd.groupby(grp).cumsum() * upd
    if "volume" in df.columns:
        vm = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["volume"] / vm.replace(0, np.nan)
    df["ret5"] = c.pct_change(5) * 100
    df["ret20"] = c.pct_change(20) * 100
    return df


def _f(x):
    try:
        v = float(x)
        return v if v == v else np.nan   # NaN 判定
    except Exception:
        return np.nan


def features_at(df: pd.DataFrame, ts) -> dict | None:
    try:
        i = df.index.get_loc(ts)
    except Exception:
        return None
    if not isinstance(i, int) or i < 1:
        return None
    row = df.iloc[i]
    prev = df.iloc[i - 1]
    close = _f(row.get("close"))
    if not (close > 0):
        return None
    ma5 = _f(row.get("ma5")); ma25 = _f(row.get("ma25")); ma200 = _f(row.get("ma200"))
    atr = _f(row.get("atr")); opn = _f(row.get("open")); pc = _f(prev.get("close"))
    return {
        "price":      close,
        "ext_ma5":    (close - ma5) / ma5 * 100 if ma5 > 0 else np.nan,
        "ext_ma25":   (close - ma25) / ma25 * 100 if ma25 > 0 else np.nan,
        "gap":        (opn - pc) / pc * 100 if (opn > 0 and pc > 0) else np.nan,
        "rsi2":       _f(row.get("rsi2")),
        "rsi14":      _f(row.get("rsi14")),
        "up_days":    _f(row.get("up_days")),
        "atr_ratio":  atr / close * 100 if atr > 0 else np.nan,
        "ma200_pos":  (close - ma200) / ma200 * 100 if ma200 > 0 else np.nan,
        "ma200_slope": _f(row.get("ma200_slope")),
        "vol_ratio":  _f(row.get("vol_ratio")),
        "ret5":       _f(row.get("ret5")),
        "ret20":      _f(row.get("ret20")),
    }


def process(sym: str, name: str) -> list:
    rows = []
    try:
        df = ble.fetch(sym, args.days + 260)
    except Exception:
        return rows
    if df is None or len(df) < 260:
        return rows
    if not (args.min_price <= float(df["close"].iloc[-1]) <= args.max_price):
        return rows
    dfx = add_indicators(df)
    for side, mod in SIDE_MODS:
        params = getattr(mod, "STRATEGY_PARAMS", {})
        etype = getattr(mod, "ENTRY_TYPE", "stop")
        for strat, p in params.items():
            if not p or len(p) < 4:
                continue
            cf, em, sm, tm = p[0], p[1], p[2], p[3]
            try:
                res = ble.run_limit_backtest(sym, name, df, cf, em, sm, tm,
                                             args.days, strat, entry_type=etype, max_hold=1)
            except Exception:
                continue
            if not res:
                continue
            for t in res["trade_log"]:
                if t.get("reason") in ("発注中", "保有中") or t.get("signal_dt") is None:
                    continue
                feats = features_at(dfx, pd.Timestamp(t["signal_dt"]))
                if feats is None:
                    continue
                mpnl = -float(t.get("pnl", 0) or 0)   # ミラー = 符号反転
                row = {
                    "symbol": sym, "name": name, "strategy": strat, "side": side,
                    "signal_dt": str(pd.Timestamp(t["signal_dt"]).date()),
                    "mirror_pnl": round(mpnl), "mirror_win": 1 if mpnl > 0 else 0,
                }
                for k in FEAT_COLS:
                    v = feats.get(k)
                    row[k] = round(v, 3) if (v is not None and v == v) else ""
                rows.append(row)
    return rows


def _single_period_buckets(df: pd.DataFrame) -> None:
    print("\n特徴量ごとの5分位バケット(フェード成功=勝率/平均が高い帯を探す):")
    for col in FEAT_COLS:
        s = pd.to_numeric(df[col], errors="coerce")
        d = df.assign(_v=s).dropna(subset=["_v"])
        if len(d) < 50:
            continue
        try:
            d = d.assign(_q=pd.qcut(d["_v"], 5, labels=False, duplicates="drop"))
        except Exception:
            continue
        print(f"\n■ {col}")
        for q in sorted(d["_q"].dropna().unique()):
            sub = d[d["_q"] == q]
            lo, hi = sub["_v"].min(), sub["_v"].max()
            print(f"  Q{int(q)+1} [{lo:9.2f}〜{hi:9.2f}]  n={len(sub):5d}  "
                  f"勝率{sub['mirror_win'].mean()*100:5.1f}%  平均{sub['mirror_pnl'].mean():+8,.0f}")


def _train_test_buckets(df: pd.DataFrame, split_date: str) -> None:
    cut = pd.Timestamp(split_date)
    df = df.assign(_d=pd.to_datetime(df["signal_dt"], errors="coerce"))
    tr = df[df["_d"] < cut].copy()
    te = df[df["_d"] >= cut].copy()
    print(f"\nTRAIN(〜{split_date}) {len(tr):,}件 勝率{tr['mirror_win'].mean()*100:.1f}%"
          f"  |  TEST/OOS({split_date}〜) {len(te):,}件 勝率{te['mirror_win'].mean()*100:.1f}%")
    print("\n各特徴の5分位: TRAIN勝率 → TEST勝率 (両方55%超で一致する帯=再現する本物のフィルタ)")
    for col in FEAT_COLS:
        trv = pd.to_numeric(tr[col], errors="coerce")
        tev = pd.to_numeric(te[col], errors="coerce")
        dv = trv.dropna()
        if len(dv) < 100:
            continue
        try:
            _, edges = pd.qcut(dv, 5, retbins=True, duplicates="drop")
        except Exception:
            continue
        tr["_b"] = pd.cut(trv, bins=edges, labels=False, include_lowest=True)
        te["_b"] = pd.cut(tev, bins=edges, labels=False, include_lowest=True)
        print(f"\n■ {col}")
        for b in range(len(edges) - 1):
            ts = tr[tr["_b"] == b]
            es = te[te["_b"] == b]
            tw = ts["mirror_win"].mean() * 100 if len(ts) else float("nan")
            ew = es["mirror_win"].mean() * 100 if len(es) else float("nan")
            ep = es["mirror_pnl"].mean() if len(es) else float("nan")
            print(f"  Q{b+1} [{edges[b]:9.2f}〜{edges[b+1]:9.2f}]  "
                  f"TRAIN n={len(ts):5d} {tw:5.1f}%  →  TEST n={len(es):5d} {ew:5.1f}% 平均{ep:+7,.0f}")


def analyze(rows: list, split_date: str | None = None) -> None:
    df = pd.DataFrame(rows)
    print("\n" + "=" * 74)
    print("全体")
    print("=" * 74)
    print(f"信号数 {len(df):,} / 勝率 {df['mirror_win'].mean()*100:.1f}% / "
          f"平均ミラー損益 {df['mirror_pnl'].mean():+,.0f} / 合計 {df['mirror_pnl'].sum():+,.0f}")
    for sd in df["side"].unique():
        s = df[df["side"] == sd]
        print(f"  [{sd}] 信号{len(s):,} 勝率{s['mirror_win'].mean()*100:.1f}% "
              f"平均{s['mirror_pnl'].mean():+,.0f}")
    if split_date:
        _train_test_buckets(df, split_date)
    else:
        _single_period_buckets(df)


def main() -> int:
    syms, src = load_universe(args.symbols)
    if not syms:
        print("[ERROR] ユニバースが見つかりません(symbols_listed_prime.py 等)")
        return 1
    if args.limit > 0:
        syms = syms[: args.limit]
    print(f"ユニバース: {src} / {len(syms)}銘柄 / 価格{args.min_price:.0f}-{args.max_price:.0f} / "
          f"side={'long+short' if args.short else 'long'} / {len(SIDE_MODS)}モジュール")
    all_rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, s, n): s for s, n in syms}
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(syms)} (信号 {len(all_rows):,})", flush=True)
            try:
                all_rows.extend(fut.result())
            except Exception:
                pass
    print(f"\n収集信号数: {len(all_rows):,}")
    if not all_rows:
        print("信号なし。終了。")
        return 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    print(f"データセット出力: {args.out}")
    analyze(all_rows, args.split_date)
    print("\n※ ここから『勝率/平均が高い帯』を組み合わせてフィルタ法則を作り、"
          "別期間(train/test)で再現するか検証するのが次の一歩。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
