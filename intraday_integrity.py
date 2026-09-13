r"""intraday_integrity.py — 5分足と日足の価格基準ズレ(株式分割の未調整)を検出する。

何が起きていたか (CLAUDE.md 18.28)
----------------------------------
5分足(stock_5min / J-Quants)は **ダウンロード時のまま**保存され、あとから分割が
起きても再調整されない。一方 日足(yfinance)は **分割を遡及調整**する。
その結果、分割した銘柄の分割前の日は

    5分足の価格 = 日足の価格 × 分割比

となる。lss の注文値(トリガー/損切り/利確)は日足から作るので、
**日足ベースの損切りライン**と**5分足の生価格**を混ぜて計算すると破綻する。

とくに `max(line, bar_open)` 形の窓埋め約定は **片側にしか効かない**:
5分足が高い側にズレていれば常にそちらが選ばれ、巨大な偽の損失になる。
実測(2026-08-08): 1件で -1,424,500円、MAE(最大逆行) 601% = 1日で株価7倍。
東証の値幅制限があるので**物理的に不可能**な値。

判定のしかた
------------
日足の同じ日の OHLC は必ず手元にあるので、それと突き合わせる。
分割は 2倍/3倍/5倍… と離散的に効くので、**わずかなズレではなく桁で外れる**。
閾値はゆるく取れば十分(既定 30%)。値幅制限のある日本株なら、同じ日の
5分足と日足が30%以上ずれることは正常にはありえない。

使い方
------
  # ライブラリとして
  from intraday_integrity import day_scale_ok
  if not day_scale_ok(day_bars, daily_close):
      continue      # この日は使わない

  # 単体スキャン(どの銘柄・どの日が壊れているかを一覧)
  python intraday_integrity.py --symbols-file holdout_selected_symbols.py --days 400
  python intraday_integrity.py --limit 100 --out corrupt_days.csv
"""
from __future__ import annotations

# 同じ日の5分足と日足がこれ以上ずれていたら価格基準が違うとみなす。
# 日本株は値幅制限があるので、正常なら数%以内に収まる。
_MAX_DEV = 0.30


def day_scale_ok(day_bars, daily_close: float, max_dev: float = _MAX_DEV) -> bool:
    """その日の5分足が日足と同じ価格基準か。True=使ってよい。

    day_bars: その日の5分足 DataFrame (close 列があること)
    daily_close: 同じ日の日足終値(分割調整済み)
    """
    if day_bars is None or len(day_bars) == 0:
        return False
    if not (daily_close and daily_close > 0):
        return True                      # 日足が無ければ判定できない = 素通し
    try:
        # 終値の中央値で見る。1本だけ飛んでいる場合に引きずられないため。
        med = float(day_bars["close"].median())
    except Exception:
        return True
    if not (med > 0):
        return False
    return abs(med / daily_close - 1.0) <= max_dev


def scale_ratio(day_bars, daily_close: float) -> float:
    """5分足 / 日足 の価格比。分割未調整なら 2.0 / 3.0 / 5.0 等に近い値になる。"""
    if day_bars is None or len(day_bars) == 0 or not (daily_close and daily_close > 0):
        return 1.0
    try:
        med = float(day_bars["close"].median())
    except Exception:
        return 1.0
    return med / daily_close if daily_close > 0 else 1.0


# ─────────────────────────────────────────────────────────────────────
# 単体スキャン
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import csv as _csv
    import sys
    from pathlib import Path

    import pandas as pd

    ap = argparse.ArgumentParser(description="5分足と日足の価格基準ズレを洗い出す")
    ap.add_argument("--symbols-file", default="holdout_selected_symbols.py")
    ap.add_argument("--days", type=int, default=400, help="遡及日数")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(試し打ち)")
    ap.add_argument("--max-dev", type=float, default=_MAX_DEV, help="許容ズレ(既定0.30)")
    ap.add_argument("--out", default="intraday_integrity.csv")
    a = ap.parse_args()

    from backtest_limit_entry import fetch as _fetch
    from daytrade_data import load_intraday, split_by_day

    p = Path(a.symbols_file)
    if not p.exists():
        sys.exit(f"[error] {p} が見つかりません")
    ns: dict = {}
    exec(p.read_text(encoding="utf-8"), ns)
    syms = []
    for k, v in ns.items():
        if k.startswith("_") or not isinstance(v, (list, tuple, dict, set)):
            continue
        for item in (v.keys() if isinstance(v, dict) else v):
            s = item[0] if isinstance(item, (list, tuple)) and item else item
            if isinstance(s, str) and s.endswith(".T"):
                syms.append(s)
    syms = sorted(set(syms))
    if a.limit:
        syms = syms[:a.limit]
    print(f"[スキャン] {len(syms)}銘柄 / 遡及{a.days}日 / 許容ズレ {a.max_dev:.0%}")

    bad: list[dict] = []
    n_days = 0
    for i, sym in enumerate(syms, 1):
        try:
            df5 = load_intraday(sym, days=a.days, source="local")
            if df5 is None or df5.empty:
                continue
            dfd = _fetch(sym, a.days + 30)
            if dfd is None or getattr(dfd, "empty", True):
                continue
            dclose = {d.date(): float(c) for d, c in
                      zip(dfd.index, dfd["close"] if "close" in dfd.columns else dfd["Close"])}
            for d, g in split_by_day(df5).items():
                dc = dclose.get(d)
                if dc is None:
                    continue
                n_days += 1
                r = scale_ratio(g, dc)
                if abs(r - 1.0) > a.max_dev:
                    bad.append({"symbol": sym, "date": str(d), "ratio": round(r, 3),
                                "intraday_med": round(float(g["close"].median()), 1),
                                "daily_close": round(dc, 1)})
        except Exception as e:
            print(f"  [warn] {sym}: {e}")
        if i % 100 == 0:
            print(f"  ...{i}/{len(syms)}銘柄  検出 {len(bad):,}日", flush=True)

    print(f"\n[結果] 検査 {n_days:,}銘柄日 / **ズレ検出 {len(bad):,}銘柄日** "
          f"({len(bad) / n_days * 100 if n_days else 0:.2f}%)")
    if bad:
        b = pd.DataFrame(bad)
        print("\n  銘柄別(件数上位):")
        for s, c in b["symbol"].value_counts().head(15).items():
            rs = b[b["symbol"] == s]
            print(f"    {s:<10}{c:>5}日  比率 {rs['ratio'].min():.2f}〜{rs['ratio'].max():.2f}  "
                  f"期間 {rs['date'].min()}〜{rs['date'].max()}")
        print("\n  比率の分布(分割比に一致するはず):")
        for lo, hi, lab in ((1.8, 2.2, "2倍(1:2分割)"), (2.8, 3.2, "3倍(1:3)"),
                            (3.8, 4.2, "4倍(1:4)"), (4.8, 5.2, "5倍(1:5)"),
                            (9.5, 10.5, "10倍(1:10)")):
            n = int(((b["ratio"] >= lo) & (b["ratio"] <= hi)).sum())
            if n:
                print(f"    {lab:<14}{n:>6,}日")
        b.to_csv(a.out, index=False, encoding="utf-8-sig")
        print(f"\n[出力] {a.out}")
        print("  → この銘柄日を使った検証結果はすべて汚染されています。")
        print("    5分足を分割調整して再保存するか、該当日をスキップしてください。")
    else:
        print("  ズレは検出されませんでした。")
