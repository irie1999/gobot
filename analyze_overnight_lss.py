r"""analyze_overnight_lss.py — 「終値で入って翌日売る」を lss のシグナル集団で測る。

問い
----
現行 lss = シグナル日Dの引け後に逆指値売りを出し、翌日D+1に
『前日終値-1ティックを割ったら』約定 → **同日決済**。

案 = D の引けでそのまま空売りし、D+1 に買い戻す(持ち越し)。
トリガー(下ブレイク待ち)を捨て、保有を夜またぎにする。

既にわかっていること
--------------------
§18.19 で **無条件の overnight ドリフトはゼロ** と実測済み
(604,626銘柄日 / 511営業日 / ショート -17円/件 / t=-0.14 / CI -277〜+242)。
夜間に取れるドリフトは存在しない。ただしこれは全銘柄・全日の測定で、
**lss のシグナルが出た銘柄に限った持ち越し**は測っていない。ここを埋める。

§18.19 のもう一つの結論: エッジは「下ブレイクへの反応」。終値で入るとその条件を
捨てるので、不利が予想される。予想が外れるかどうかを見る。

測るもの (すべて同じシグナル集団・100株・摩擦なし・ショート)
--------------------------------------------------------
  現行     : oos_raw の pnl (D+1 にトリガー約定 → 同日決済)
  A 引け→翌寄り  : D終値で空売り → D+1始値で買い戻し (純オーバーナイト)
  B 引け→翌引け  : D終値で空売り → D+1終値で買い戻し (夜+日中まるごと)
  C 翌寄り→翌引け: D+1始値で空売り → D+1終値で買い戻し (トリガー無しの日中)

C を並べるのは、A と B の差が『夜』なのか『日中』なのかを分けるため。

使い方
------
  python analyze_overnight_lss.py --raw "oos_raw_fold*.csv"
  python analyze_overnight_lss.py --raw "oos_raw_fold*.csv" --workers 8 --by-month

⚠ 持ち越しには測定に出ないコストがある(逆日歩・一般信用デイトレが使えない・
   夜間のギャップで損切りが効かない・証拠金が2日拘束され資本回転が半減)。
   数字が並んでも、それだけで採用理由にはならない。
"""
from __future__ import annotations

import argparse
import glob as _glob
import statistics as _st
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

ap = argparse.ArgumentParser(description="lss シグナルでの持ち越しを測る")
ap.add_argument("--raw", required=True, help="生トレードCSV(グロブ可)")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--bt-min", type=float, default=0.0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--by-month", action="store_true")
a = ap.parse_args()

files = sorted(_glob.glob(a.raw)) if any(c in a.raw for c in "*?[") else [a.raw]
if not files:
    sys.exit(f"[error] {a.raw} に一致するファイルがありません")
d = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in files], ignore_index=True)
if "strategy" in d.columns:
    d = d[d["strategy"].astype(str) != "転換"]        # lss ではない(18.5.3)
if a.bt_min > 0 and "bt_score" in d.columns:
    d = d[pd.to_numeric(d["bt_score"], errors="coerce").fillna(0) >= a.bt_min]
d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
d = d[d["entry_date"].notna()].copy()
# 同じ(銘柄,日)は戦略が違っても同じ1トレードになる(トリガー/決済が同一)。
# 持ち越し案も同じなので、重複を排して1銘柄1日1件にする。
u = d.drop_duplicates(subset=["symbol", "entry_date"])[
    ["symbol", "entry_date", "oos_month"]].copy()
print(f"[入力] {len(files)}ファイル / シグナル {len(d):,}件 "
      f"→ 銘柄×日 {len(u):,}件 / {u['entry_date'].min().date()}〜"
      f"{u['entry_date'].max().date()}")

try:
    from backtest_limit_entry import fetch as _fetch
except Exception as e:
    sys.exit(f"[error] backtest_limit_entry を import できません: {e}")

_syms = sorted(u["symbol"].astype(str).unique())
print(f"[日足] {len(_syms):,}銘柄を取得中(キャッシュ利用)...")
_bars: dict = {}


def _load(sym: str):
    try:
        df = _fetch(sym, 900)
        if df is None or df.empty:
            return sym, None
        df = df.copy()
        df.index = pd.to_datetime(df.index).normalize()
        cols = {str(c).lower(): c for c in df.columns}
        need = ("open", "close")
        if any(cols.get(c) is None for c in need):
            return sym, None
        return sym, df.rename(columns={cols["open"]: "o", cols["close"]: "c"})[["o", "c"]]
    except Exception:
        return sym, None


with ThreadPoolExecutor(max_workers=a.workers) as ex:
    for i, fut in enumerate(as_completed([ex.submit(_load, s) for s in _syms]), 1):
        s, df = fut.result()
        _bars[s] = df
        if i % 200 == 0:
            print(f"  ...{i}/{len(_syms)}", flush=True)

_recs = []
_miss = 0
for sym, ed, om in u[["symbol", "entry_date", "oos_month"]].itertuples(index=False):
    df = _bars.get(str(sym))
    if df is None:
        _miss += 1
        continue
    idx = df.index
    pos = idx.searchsorted(pd.Timestamp(ed))
    # entry_date(=D+1) の行と、その1本前(=D, シグナル日)が要る
    if pos <= 0 or pos >= len(idx) or idx[pos] != pd.Timestamp(ed):
        _miss += 1
        continue
    pc = float(df["c"].iloc[pos - 1])      # D の終値 = 入る値
    o1 = float(df["o"].iloc[pos])          # D+1 の始値
    c1 = float(df["c"].iloc[pos])          # D+1 の終値
    if not (pc > 0 and o1 > 0 and c1 > 0):
        _miss += 1
        continue
    q = a.qty
    _recs.append({
        "date": ed, "month": om, "symbol": sym, "entry_p": pc,
        "A_引け→翌寄り": (pc - o1) * q,     # ショート: 入った値 − 返した値
        "B_引け→翌引け": (pc - c1) * q,
        "C_翌寄り→翌引け": (o1 - c1) * q,
    })
if _miss:
    print(f"[warn] 日足が揃わず除外 {_miss:,}件")
r = pd.DataFrame(_recs)
if r.empty:
    sys.exit("[error] 計算できる行がありません")

_cur = d.drop_duplicates(subset=["symbol", "entry_date"])
_cur = _cur[_cur["filled"] == 1] if "filled" in _cur.columns else _cur
_cur_pnl = pd.to_numeric(_cur["pnl"], errors="coerce").fillna(0)

print(f"\n■ 同じシグナル集団での比較 ({len(r):,}銘柄日 / "
      f"{r['date'].nunique():,}営業日 / {a.qty}株 / 摩擦なし)")
print(f"  {'方式':<18}{'件数':>7}{'勝率':>7}{'合計':>14}{'円/件':>9}{'bp/件':>8}"
      f"{'日クラスタt':>11}")
print(f"  {'現行 lss (参考)':<18}{len(_cur_pnl):>7,}"
      f"{(_cur_pnl > 0).mean()*100:>6.1f}%{_cur_pnl.sum():>+14,.0f}"
      f"{_cur_pnl.mean():>+9,.0f}{'—':>8}{'—':>11}")
_res = {}
for col in ("A_引け→翌寄り", "B_引け→翌引け", "C_翌寄り→翌引け"):
    v = r[col]
    bp = (v / (r["entry_p"] * a.qty) * 10_000).mean()
    dm = r.groupby("date")[col].mean()
    t = (dm.mean() / (dm.std(ddof=1) / (len(dm) ** 0.5))) if len(dm) > 1 else 0.0
    _res[col] = (v.sum(), v.mean(), bp, t)
    print(f"  {col:<18}{len(v):>7,}{(v > 0).mean()*100:>6.1f}%{v.sum():>+14,.0f}"
          f"{v.mean():>+9,.0f}{bp:>+8.1f}{t:>+11.2f}")

_a, _b, _c = (_res["A_引け→翌寄り"][1], _res["B_引け→翌引け"][1],
              _res["C_翌寄り→翌引け"][1])
print(f"\n  分解: B(夜+日中) = A(夜) + C(日中)  →  "
      f"{_b:+,.0f} ≒ {_a:+,.0f} + {_c:+,.0f}")
print(f"    夜のぶん {_a:+,.0f}円/件 (t={_res['A_引け→翌寄り'][3]:+.2f})  /  "
      f"日中のぶん {_c:+,.0f}円/件 (t={_res['C_翌寄り→翌引け'][3]:+.2f})")

if a.by_month:
    print(f"\n■ 月別 (円/件)")
    print(f"  {'月':<10}{'件数':>7}{'現行':>10}" +
          "".join(f"{c.split('_')[0]:>10}" for c in
                  ("A_", "B_", "C_")))
    _cm = _cur.assign(_p=_cur_pnl).groupby("oos_month")["_p"].agg(["size", "mean"])
    for m, g in r.groupby("month"):
        cur = _cm["mean"].get(m, float("nan"))
        print(f"  {str(m):<10}{len(g):>7,}"
              f"{(f'{cur:+,.0f}' if cur == cur else '—'):>10}"
              + "".join(f"{g[c].mean():>+10,.0f}" for c in
                        ("A_引け→翌寄り", "B_引け→翌引け", "C_翌寄り→翌引け")))

print(f"\n{'─'*78}")
print("■ 読み方")
print(f"{'─'*78}")
print("  ・A(夜だけ) が §18.19 の overnight と同じ向き(ゼロ近傍)なら、")
print("    lss のシグナルで条件付けても夜に取れるものは無い、が確認できる。")
print("  ・B が現行 lss に届かないなら、**トリガー(下ブレイク待ち)がエッジ**という")
print("    §18.19/18.20 の結論どおり。終値で入ると条件を捨てることになる。")
print("  ・日クラスタ t で見ること。同日決済でない B/C も、日ごとに相関する。")
print("  ⚠ 持ち越しには測定に出ないコストがある:")
print("     逆日歩 / 一般信用デイトレ(MarginTradeType 3)が使えない /")
print("     夜間のギャップで損切りが効かない / 証拠金が2日拘束され資本回転が半減。")
print("     現行の同日決済は予算を毎日リサイクルできるので、同じ 円/件 でも")
print("     **持ち越しは実質半分の成績**として比べること。")
