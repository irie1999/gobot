r"""probe_tick_size.py — 実際の呼値(tick size)を分足の価格から実測する。

なぜ要るか (2026-08-14)
-----------------------
kabu の板で実測したら、コードの呼値テーブルが実際より **5〜10倍粗かった**:

  8331 千葉銀行 2,858円  実際 0.5円  /  _TICK_TABLE 5円  → **10倍**
  8306 三菱UFJ  3,718円  実際 1円    /  _TICK_TABLE 5円  → **5倍**

東証は呼値の細分化を広い範囲に適用しているのに、`backtest_limit_entry.
_TICK_TABLE` は古い(粗い)表のままだった。

⛔ 壊れてはいない。バックテストも実発注も同じテーブルを使うので内部は一貫して
   おり、発注は通っている。問題は **探索範囲** で、H の「前日終値 -5ティック」は
   板の上では -50ティック(千葉銀行なら -25円)に相当する。
   つまり **『板で数ティックだけ下』という浅い指値を一度も試していない**。

何をするか
----------
分足の価格を 0.1円単位の整数にして、**隣接差の最大公約数**を取る。
飛んだ価格があっても GCD なら約数として残るので頑健。
価格帯をまたぐと呼値が変わるので **銘柄日ごとに** 推定し、その日の平均価格で
価格帯に振り分ける。

使い方
------
  python probe_tick_size.py                       # lss_trades*.csv の銘柄
  python probe_tick_size.py --symbols 8331,8306,5838
  python probe_tick_size.py --limit 200 --days 60
  python probe_tick_size.py --propose             # 実測から呼値テーブル案を出す
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import sys
from math import gcd
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_BASE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser(description="実際の呼値を分足の価格から実測")
ap.add_argument("--symbols", default=None, help="カンマ区切り(省略時は CSV から)")
ap.add_argument("--csv-glob", default="lss_trades*.csv")
ap.add_argument("--days", type=int, default=90, help="直近何日ぶんの分足を見るか")
ap.add_argument("--limit", type=int, default=300, help="銘柄数の上限(0=全部)")
ap.add_argument("--min-days", type=int, default=5, help="推定に要る最低銘柄日数")
ap.add_argument("--propose", action="store_true", help="呼値テーブル案を出す")
args = ap.parse_args()

try:
    from tenkan_sim import _bars_one, drop_symbol, find_minute_dirs
except Exception as e:                                            # pragma: no cover
    sys.exit(f"[error] tenkan_sim を import できません: {e}")
try:
    from backtest_limit_entry import _TICK_TABLE, tick_size
except Exception:
    _TICK_TABLE = [(3_000, 5), (5_000, 5), (30_000, 10), (50_000, 50),
                   (300_000, 100), (float("inf"), 10_000)]

    def tick_size(p):
        for th, tk in _TICK_TABLE:
            if p < th:
                return tk
        return 10_000


def _norm(sym: str) -> str:
    s = str(sym).strip().upper()
    if s.endswith(".T"):
        return s
    if len(s) == 5 and s.isdigit() and s[-1] == "0":
        return s[:4] + ".T"
    return s + ".T"


def _targets() -> list[str]:
    if args.symbols:
        return [_norm(x) for x in str(args.symbols).split(",") if x.strip()]
    seen = collections.Counter()
    for fp in sorted(glob.glob(str(_BASE / args.csv_glob))):
        try:
            with open(fp, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    s = str(r.get("symbol") or "").strip()
                    if s:
                        seen[_norm(s)] += 1
        except Exception:
            continue
    out = [s for s, _ in seen.most_common()]      # 取引が多い銘柄から
    return out[:args.limit] if args.limit > 0 else out


def _day_tick(prices) -> float:
    """その銘柄日の呼値を推定する。0 なら推定不能。

    0.1円単位の整数にして隣接差の GCD を取る。飛んだ価格があっても
    GCD なら約数として残るので頑健(最小差より安定)。
    """
    vals = sorted({int(round(float(p) * 10)) for p in prices
                   if p and float(p) > 0})
    if len(vals) < 8:
        return 0.0
    g = 0
    for a, b in zip(vals, vals[1:]):
        d = b - a
        if d > 0:
            g = gcd(g, d)
        if g == 1:
            break
    return g / 10.0 if g > 0 else 0.0


def _band(px: float) -> str:
    for lo, hi in [(0, 1000), (1000, 3000), (3000, 5000), (5000, 10000),
                   (10000, 30000), (30000, 50000)]:
        if lo <= px < hi:
            return f"{lo:,}〜{hi:,}円"
    return "50,000円〜"


_BAND_MID = {"0〜1,000円": 800, "1,000〜3,000円": 2000, "3,000〜5,000円": 4000,
             "5,000〜10,000円": 7000, "10,000〜30,000円": 20000,
             "30,000〜50,000円": 40000, "50,000円〜": 60000}


def main() -> int:
    syms = _targets()
    if not syms:
        print("[error] 対象銘柄が見つかりません。")
        return 1
    d5, d1 = find_minute_dirs()
    print(f"■ 呼値の実測  {len(syms)}銘柄 / 直近{args.days}日の分足から推定")
    print(f"  1分足DIR: {d1} / 5分足DIR: {d5}")
    print()

    import numpy as np
    from datetime import datetime, timedelta, timezone
    JST = timezone(timedelta(hours=9))
    cut = datetime.now(JST).date() - timedelta(days=args.days)

    per_sym: dict = {}
    band_obs: dict = collections.defaultdict(collections.Counter)
    nodata = 0
    for i, sym in enumerate(syms, 1):
        try:
            df = _bars_one(sym, 1)
            if df is None or getattr(df, "empty", True):
                df = _bars_one(sym, 5)
            if df is None or getattr(df, "empty", True):
                nodata += 1
                continue
            d = df[df.index.date >= cut]
            if len(d) == 0:
                nodata += 1
                continue
            cols = [c for c in ("open", "high", "low", "close") if c in d.columns]
            dates = np.array([ts.date() for ts in d.index])
            ticks = []
            for u in np.unique(dates):
                m = dates == u
                px = []
                for c in cols:
                    px.extend(d[c].to_numpy(dtype=float)[m].tolist())
                t = _day_tick(px)
                if t > 0:
                    ticks.append((t, float(np.nanmean(px))))
            if len(ticks) < args.min_days:
                nodata += 1
                continue
            # その銘柄の代表値 = 最頻の呼値(日ごとに推定した値の最頻)
            cnt = collections.Counter(round(t, 2) for t, _ in ticks)
            tk = cnt.most_common(1)[0][0]
            apx = sum(p for _, p in ticks) / len(ticks)
            per_sym[sym] = (tk, apx, len(ticks))
            band_obs[_band(apx)][tk] += 1
        except Exception:
            nodata += 1
        finally:
            drop_symbol(sym)
        if i % 50 == 0:
            print(f"    {i}/{len(syms)} … 推定{len(per_sym)} / 不能{nodata}",
                  flush=True)

    if not per_sym:
        print("[error] 1銘柄も推定できませんでした。分足の在庫を確認してください。")
        return 1

    print()
    print(f"  推定できた {len(per_sym)}銘柄 / 推定不能 {nodata}銘柄")
    print()
    print("【価格帯ごとの 実測呼値 vs コード(_TICK_TABLE)】")
    print(f"  {'価格帯':<16}{'銘柄':>6}  {'実測の呼値(銘柄数)':<34}"
          f"{'コード':>8}{'倍率':>8}")
    rows = []
    for b in ["0〜1,000円", "1,000〜3,000円", "3,000〜5,000円", "5,000〜10,000円",
              "10,000〜30,000円", "30,000〜50,000円", "50,000円〜"]:
        c = band_obs.get(b)
        if not c:
            continue
        n = sum(c.values())
        top = c.most_common(1)[0][0]
        code = tick_size(_BAND_MID[b])
        det = " / ".join(f"{k}円×{v}" for k, v in c.most_common(4))
        mult = code / top if top > 0 else 0
        rows.append((b, n, top, code, mult))
        mk = "  ⛔" if mult >= 2 else ("  ⚠" if mult > 1 else "")
        print(f"  {b:<16}{n:>6}  {det:<34}{code:>7}円{mult:>7.1f}x{mk}")

    print()
    print("【個別の確認(kabu の板と突き合わせた銘柄)】")
    for s in ("8331.T", "8306.T", "5838.T", "5844.T", "6323.T"):
        if s in per_sym:
            tk, apx, nd = per_sym[s]
            code = tick_size(apx)
            print(f"  {s:<8} 平均{apx:>8,.0f}円  実測 {tk:>4}円  "
                  f"コード {code:>3}円  {code / tk if tk else 0:>4.0f}倍  ({nd}日)")

    bad = [r for r in rows if r[4] >= 2]
    print()
    if bad:
        print(f"  ⛔ **{len(bad)}帯でコードが2倍以上粗い**。")
        print("     H の『前日終値 -5ティック』は、板の上では"
              " -5×(倍率)ティックに相当します。")
        print("     つまり『板で数ティックだけ下』という浅い指値を"
              "一度も試していません。")
    else:
        print("  ✅ コードの呼値は実測とおおむね一致しています。")

    if args.propose:
        print()
        print("【実測から作った呼値テーブル案】")
        print("  ⚠ 銘柄によって呼値が違う(細分化の対象かどうか)ので、"
              "価格帯だけでは決まりません。")
        print("     下は **最頻値** です。採用するなら、銘柄ごとの実測値を"
              "使うほうが正確です。")
        print("  _TICK_TABLE = [")
        for b, n, top, code, mult in rows:
            hi = b.split("〜")[1].replace("円", "").replace(",", "")
            hi = hi if hi else "inf"
            print(f"      ({hi:>9}, {top:>5}),   # 実測 {n}銘柄 "
                  f"(現行 {code}円)")
        print("  ]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
