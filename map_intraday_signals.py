r"""map_intraday_signals.py — 1日に何回転もできる日中シグナルを探す(第2版)。

第1版(map_intraday_structure.py)の何が足りなかったか
-----------------------------------------------------
第1版は **「直近M分の変化率」という1軸しか見ていなかった**。結果、寄り30分に
1升だけ見つかったが、エントリー窓(20分) < 保有時間(30分) で **1日1回転が上限**
だった。目的は「1日に何回転もする」なので、これでは答えになっていない。

日中トレードの定番シグナルを一度も測っていない:
  * VWAP乖離        — 平均回帰の基準として最も広く使われる
  * 当日レンジ位置  — 高値圏 / 安値圏
  * 出来高スパイク  — 同じ -1% でも、出来高を伴うかで意味がまったく違う
  * ボラ正規化      — 同じ -1% でも、その銘柄にとって大きいかどうか

第2版はこの5軸を同じ土俵で測る。

⛔ すべて「その時点までの情報」だけで作る(cummax / cumsum / 過去のσ)。
   未来を1本でも混ぜると必ず勝つ結果が出る。

判定
----
1回あたりのエッジが往復コストを超えることが **必須**(回数を増やせばコストも
増えるので、1回で負けるものは何回やっても負ける)。そのうえで:

    1日あたり期待値 = (bp - コスト) × 1日の回転数 × 建玉

を出す。回転数は **min(1日の機会数, 取引時間 ÷ 保有時間)** で頭打ちにする
(同じ銘柄は保有中に建て直せないため)。

多重検定
--------
升が数百あるので |t|>=2 は偶然でも出る。**日ブロックを保ったまま損益系列を
巡回シフト** した帰無を実際に回し、「偶然なら何升出るか」を実測して並べる
(完全シャッフルは日内相関を壊して帰無分布が狭くなり、偽陽性を過小評価する)。

使い方
------
  python map_intraday_signals.py --symbols 300 --days 365
  python map_intraday_signals.py --quantile 0.05 --min-price 3000 --cost-bp 8
  python map_intraday_signals.py --split 2026-02-15 --csv sig.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))

ap = argparse.ArgumentParser(description="1日に何回転もできる日中シグナルを探す")
ap.add_argument("--symbols", type=int, default=300, help="ランダムに何銘柄(0=全部)")
ap.add_argument("--only", default=None,
                help="銘柄を明示指定(カンマ区切り)。ETF・低コスト銘柄を直接測る用。\n                     指定時は --symbols と価格帯フィルタを無視する")
ap.add_argument("--days", type=int, default=365)
ap.add_argument("--split", default=None, help="TRAIN/TEST 境界 YYYY-MM-DD")
ap.add_argument("--min-price", type=float, default=3000.0, help="0=フィルタなし")
ap.add_argument("--max-price", type=float, default=0.0, help="0=フィルタなし")
ap.add_argument("--quantile", type=float, default=0.05, help="上位/下位 何割を見るか")
ap.add_argument("--cost-bp", type=float, default=8.0, help="往復コスト(呼値4ティック)")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--budget", type=float, default=4_000_000.0, help="1日あたり使える資金")
ap.add_argument("--null-shifts", type=int, default=16, help="帰無較正の本数")
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--csv", default=None)
args = ap.parse_args()

try:
    from tenkan_sim import _bars_one, drop_symbol, find_minute_dirs
except Exception as e:                                            # pragma: no cover
    sys.exit(f"[error] tenkan_sim を import できません: {e}")

_d5, _d1 = find_minute_dirs()
if _d1 is None:
    sys.exit("[error] 1分足ディレクトリが見つかりません。fetch_1m_all.py を実行するか "
             "MINUTE_1M_DIR を設定してください。")

# ── シグナル定義 ───────────────────────────────────────────────────────
# (キー, ラベル, M, ビン境界)  M=0 は「当日累積で決まるのでM不要」の意味。
_E_RET = np.arange(-200, 201, 10, dtype=np.float64)     # bp
_E_Z = np.arange(-400, 401, 20, dtype=np.float64)       # σ×100
_E_VW = np.arange(-300, 301, 15, dtype=np.float64)      # bp
_E_RG = np.arange(0, 101, 5, dtype=np.float64)          # %
_E_VL = np.arange(0, 501, 25, dtype=np.float64)         # %

SIGS = [
    ("ret1", "直近1分の変化率", 1, _E_RET),
    ("ret5", "直近5分の変化率", 5, _E_RET),
    ("ret15", "直近15分の変化率", 15, _E_RET),
    ("retz1", "直近1分/当日σ", 1, _E_Z),
    ("retz5", "直近5分/当日σ", 5, _E_Z),
    ("retz15", "直近15分/当日σ", 15, _E_Z),
    ("vwap", "VWAP乖離", 0, _E_VW),
    ("range", "当日レンジ位置", 0, _E_RG),
    ("volz5", "直近5分の出来高比", 5, _E_VL),
    ("volz15", "直近15分の出来高比", 15, _E_VL),
]
NS = [1, 2, 3, 5, 10, 15, 30]
# ゾーンは **排他** にする(昼休みを除けば取引時間をちょうど覆う)。
# 「全時間」はこの4つの和なので集計しない — 集計を1回の bincount に畳めて5倍速い。
ZONES = [("寄り30分", 540, 570), ("前場", 570, 690),
         ("後場前半", 750, 870), ("引け60分", 870, 930)]
NZ = len(ZONES)
ZALL = -1                     # _tail に渡すと4ゾーンの和 = 全時間
NSIG = len(SIGS)
NBMAX = max(len(e) + 1 for _, _, _, e in SIGS)
MAXM = max(m for _, _, m, _ in SIGS)

_DAYIDX: dict = {}
_MAXDAY = args.days + 40
SUM = np.zeros((NSIG, len(NS), NZ, NBMAX, _MAXDAY), dtype=np.float64)
CNT = np.zeros((NSIG, len(NS), NZ, NBMAX, _MAXDAY), dtype=np.float64)
# 1銘柄日あたりの機会数を数えるため、銘柄日数も持つ
NSYMDAY = [0]


def _day_index(day: str) -> int:
    di = _DAYIDX.get(day)
    if di is None:
        if len(_DAYIDX) >= _MAXDAY:
            return -1
        di = len(_DAYIDX)
        _DAYIDX[day] = di
    return di


def _jq_to_yf(code: str) -> str:
    """J-Quants の5桁(末尾0) -> yfinance 表記。'72030' -> '7203.T'

    ⛔ そのまま '.T' を足すと _bars_one が内部でさらに '0' を足して探しに行き、
       全件ファイルなしになる(第1版で実際に踏んだ)。
    """
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def universe() -> list[str]:
    if args.only:
        # ⛔ 明示指定のときは価格帯フィルタを掛けない(ETFは価格帯がばらばらで、
        #    3万円の日経レバも1万円台のTOPIXも同じ土俵で見たいため)。
        return [_jq_to_yf(x) for x in str(args.only).split(",") if x.strip()]
    try:
        from daytrade_data import available_local_symbols
        syms = [_jq_to_yf(s) for s in available_local_symbols()]
    except Exception as e:
        sys.exit(f"[error] ユニバースが取れません: {e}")
    if not syms:
        sys.exit("[error] ユニバースが空です")
    rng = random.Random(args.seed)
    rng.shuffle(syms)
    return syms if args.symbols <= 0 else syms[:args.symbols]


def build_signals(c: np.ndarray, v: np.ndarray) -> dict:
    """その時点までの情報だけで各シグナルを作る。値は表示用のスケールに揃える。"""
    n = len(c)
    out: dict = {}
    r1 = np.zeros(n)
    r1[1:] = c[1:] / c[:-1] - 1.0
    # 当日ここまでの1分リターンσ(過去のみ。ddofは気にしない)
    cs = np.cumsum(r1)
    cs2 = np.cumsum(r1 * r1)
    idx = np.arange(1, n + 1, dtype=float)
    var = np.maximum(cs2 / idx - (cs / idx) ** 2, 1e-12)
    sd = np.sqrt(var)
    # VWAP(当日累積)。出来高が無い/ゼロなら close の累積平均で代用
    if v is not None and np.nansum(v) > 0:
        pv = np.cumsum(c * v)
        vv = np.maximum(np.cumsum(v), 1e-9)
        vwap = pv / vv
    else:
        vwap = np.cumsum(c) / idx
    hi = np.maximum.accumulate(c)
    lo = np.minimum.accumulate(c)
    for key, _lab, M, _e in SIGS:
        if key.startswith("ret") and not key.startswith("retz"):
            x = np.full(n, np.nan)
            if n > M:
                x[M:] = (c[M:] / c[:-M] - 1.0) * 1e4                 # bp
        elif key.startswith("retz"):
            x = np.full(n, np.nan)
            if n > M:
                x[M:] = ((c[M:] / c[:-M] - 1.0) /
                         (sd[M:] * math.sqrt(M) + 1e-12)) * 100.0    # σ×100
        elif key == "vwap":
            x = (c / vwap - 1.0) * 1e4                               # bp
        elif key == "range":
            rng_ = np.maximum(hi - lo, 1e-9)
            x = (c - lo) / rng_ * 100.0                              # %
        elif key.startswith("volz"):
            x = np.full(n, np.nan)
            if v is not None and n > M and np.nansum(v) > 0:
                cv = np.cumsum(v)
                avg = np.maximum(cv / idx, 1e-9)
                win = np.full(n, np.nan)
                win[M:] = (cv[M:] - cv[:-M]) / M
                x = win / avg * 100.0                                # %
        else:
            x = np.full(n, np.nan)
        out[key] = x
    return out


def zone_index(mins: np.ndarray) -> np.ndarray:
    """分 -> ゾーンindex(0..NZ-1)。取引時間外・昼休みは -1。"""
    z = np.full(mins.shape, -1, dtype=np.int64)
    for i, (_, a, b) in enumerate(ZONES):
        z[(mins >= a) & (mins < b)] = i
    return z


def accumulate(c: np.ndarray, v: np.ndarray, mins: np.ndarray, di: int) -> int:
    n = len(c)
    if di < 0 or n < MAXM + max(NS) + 2:
        return 0
    sig = build_signals(c, v)
    zall = zone_index(mins)
    used = 0
    for si, (key, _lab, M, edges) in enumerate(SIGS):
        x = sig[key]
        for ni, N in enumerate(NS):
            hi = n - 1 - N
            if hi <= M:
                continue
            xs = x[M:hi + 1]
            rf = c[M + N:hi + N + 1] / c[M:hi + 1] - 1.0
            z = zall[M:hi + 1]
            ok = np.isfinite(xs) & np.isfinite(rf) & (z >= 0)
            if not ok.any():
                continue
            # (ゾーン, ビン) を複合indexに畳んで bincount 1回で済ませる
            idx = z[ok] * NBMAX + np.digitize(xs[ok], edges)
            s = np.bincount(idx, weights=rf[ok], minlength=NZ * NBMAX)
            k = np.bincount(idx, minlength=NZ * NBMAX).astype(np.float64)
            SUM[si, ni] [:, :, di] += s[:NZ * NBMAX].reshape(NZ, NBMAX)
            CNT[si, ni] [:, :, di] += k[:NZ * NBMAX].reshape(NZ, NBMAX)
            used += int(ok.sum())
    return used


def _slab(A: np.ndarray, si: int, ni: int, zi: int) -> np.ndarray:
    """(ビン, 日) の面を取り出す。zi<0 は全ゾーンの和 = 全時間。"""
    return A[si, ni].sum(axis=0) if zi < 0 else A[si, ni, zi]


def _tail(si: int, ni: int, zi: int, upper: bool, dmask: np.ndarray,
          shift: int = 0):
    """上位(下位) quantile のビン群の r_fut 平均(bp)と日クラスタ t。

    shift>0 のときは **日ラベルを巡回シフト** して t だけを帰無化する
    (bp は日順序に依らないので変わらない)。
    """
    _S, _C = _slab(SUM, si, ni, zi), _slab(CNT, si, ni, zi)
    cnt = _C[:, dmask].sum(axis=1)
    tot = cnt.sum()
    if tot < 1000:
        return None
    target = tot * args.quantile
    order = range(NBMAX - 1, -1, -1) if upper else range(NBMAX)
    sel, acc = [], 0.0
    for b in order:
        if cnt[b] <= 0:
            continue
        sel.append(b)
        acc += cnt[b]
        if acc >= target:
            break
    if not sel or acc < 300:
        return None
    ds = _S[sel][:, dmask].sum(axis=0)
    dk = _C[sel][:, dmask].sum(axis=0)
    k = dk.sum()
    if k <= 0:
        return None
    live = dk > 0
    dv = (ds[live] / dk[live]) * 1e4
    if shift:
        dv = np.roll(dv, shift)
    nd = int(live.sum())
    t = 0.0
    if nd > 2:
        sd = dv.std(ddof=1)
        if sd > 0:
            t = float(dv.mean() / (sd / math.sqrt(nd)))
    return {"bp": float(ds.sum() / k * 1e4), "n": float(k), "t": t, "days": nd}


_ZLIST = [(ZALL, "全時間", 540, 930)] + [(i, z[0], z[1], z[2])
                                        for i, z in enumerate(ZONES)]


def scan(dmask: np.ndarray, shift: int = 0) -> list[dict]:
    out = []
    for si, (key, lab, M, _e) in enumerate(SIGS):
        for ni, N in enumerate(NS):
            for zi, zn, _a, _b in _ZLIST:
                for updown, upper in (("上位", True), ("下位", False)):
                    r = _tail(si, ni, zi, upper, dmask, shift)
                    if r is None:
                        continue
                    out.append({"sig": key, "label": lab, "N": N,
                                "zone": zn, "side": updown,
                                "bp": r["bp"], "t": r["t"], "n": r["n"],
                                "days": r["days"]})
    return out


def main() -> int:
    syms = universe()
    cut = (datetime.now(JST).date() - timedelta(days=args.days))
    print("■ 日中シグナル探索(第2版) — 1日に何回転もできるものを探す")
    print("=" * 100)
    print(f"  シグナル {NSIG}種 × 保有N {len(NS)}通り × ゾーン {len(_ZLIST)} × 上下2 = "
          f"{NSIG * len(NS) * len(_ZLIST) * 2} 升")
    print(f"  対象: {len(syms)}銘柄 × 直近{args.days}日 / 価格 "
          f"{args.min_price:,.0f}〜{args.max_price or 999999:,.0f}円 / "
          f"上位下位 {args.quantile:.0%}")
    print(f"  判定: 1回あたり {args.cost_bp}bp 超が必須。そのうえで1日の回転数を掛ける")
    print()

    why = {"1分足なし": 0, "期間内バーなし": 0, "価格帯外": 0, "エラー": 0}
    errs: list[str] = []
    nsym = nday = nobs = 0
    for i, sym in enumerate(syms, 1):
        try:
            df = _bars_one(sym, 1)
            if df is None or getattr(df, "empty", True) or "close" not in df.columns:
                why["1分足なし"] += 1
            else:
                d = df[df.index.date >= cut]
                if len(d) == 0:
                    why["期間内バーなし"] += 1
                else:
                    px = float(d["close"].iloc[-1])
                    if not (args.only or
                            ((args.min_price <= 0 or px >= args.min_price) and
                             (args.max_price <= 0 or px <= args.max_price))):
                        why["価格帯外"] += 1
                    else:
                        dates = np.array([ts.date() for ts in d.index])
                        cl = d["close"].to_numpy(dtype=float)
                        vol = (d["volume"].to_numpy(dtype=float)
                               if "volume" in d.columns else None)
                        mn = np.array([ts.hour * 60 + ts.minute
                                       for ts in d.index], dtype=np.int64)
                        for u in np.unique(dates):
                            m = dates == u
                            if int(m.sum()) < 60:
                                continue
                            got = accumulate(cl[m], None if vol is None else vol[m],
                                             mn[m], _day_index(str(u)))
                            if got:
                                nobs += got
                                nday += 1
                                NSYMDAY[0] += 1
                        nsym += 1
        except Exception as e:
            why["エラー"] += 1
            if len(errs) < 3:
                errs.append(f"{sym}: {type(e).__name__}: {e}")
        drop_symbol(sym)
        if i % 25 == 0:
            print(f"    {i}/{len(syms)}銘柄 … {nsym}採用 / {nday:,}銘柄日 / "
                  f"{nobs:,}観測", flush=True)

    if nobs == 0:
        print("  【0件の内訳】 " + " / ".join(f"{k} {v}" for k, v in why.items()))
        for e in errs:
            print(f"    {e}")
        sys.exit("[error] 観測が0件。")
    print()
    print(f"  採用 {nsym}銘柄 / {nday:,}銘柄日 / {nobs:,}観測 / {len(_DAYIDX)}営業日")
    print()

    allmask = np.zeros(_MAXDAY, dtype=bool)
    allmask[:len(_DAYIDX)] = True
    rows = scan(allmask)

    # ── 全時間帯の一覧(回転前提なのでここが主役) ─────────────────────
    print("  ═══ 全時間帯(ゾーン=全時間) — 1回あたりのリターン(bp) ═══")
    print(f"  {'シグナル':<20}{'側':<5}" + "".join(f"{'N=' + str(n):>9}" for n in NS))
    for si, (key, lab, M, _e) in enumerate(SIGS):
        for updown, upper in (("上位", True), ("下位", False)):
            line = f"  {lab:<20}{updown:<5}"
            for ni in range(len(NS)):
                r = _tail(si, ni, ZALL, upper, allmask)
                if r is None:
                    line += f"{'—':>9}"
                    continue
                mk = "*" if (abs(r["bp"]) >= args.cost_bp
                             and abs(r["t"]) >= 2.0) else " "
                line += f"{r['bp']:>+8.1f}{mk}"
            print(line)
    print()

    # ── 帰無較正(日ブロック巡回シフト) ───────────────────────────────
    hit = [r for r in rows if abs(r["bp"]) >= args.cost_bp and abs(r["t"]) >= 2.0]
    nd_all = len(_DAYIDX)
    null_counts = []
    for s in range(1, args.null_shifts + 1):
        sh = int(s * nd_all / (args.null_shifts + 1)) or 1
        nr = scan(allmask, shift=sh)
        null_counts.append(sum(1 for r in nr
                               if abs(r["bp"]) >= args.cost_bp
                               and abs(r["t"]) >= 2.0))
    nc = np.array(null_counts, dtype=float)
    print("  【判定】")
    print(f"  全 {len(rows)} 升 / コスト {args.cost_bp}bp 超 かつ |日t|>=2 = "
          f"**{len(hit)} 升**")
    print(f"  帰無(日ブロック巡回シフト {args.null_shifts}本): "
          f"平均 {nc.mean():.1f} 升 / 最大 {nc.max():.0f} 升 / "
          f"実測はこの帯の{'外' if len(hit) > nc.max() else '中'}")
    if len(hit) <= nc.mean():
        print("  ⛔ **実測が帰無平均以下。構造がある証拠にならない。**")

    if not hit:
        print()
        print("  ⛔ **候補ゼロ。1回あたりコストを超えるシグナルが無い。**")
        print("     回数を増やせばコストも増えるので、1回で負けるものは何回やっても負ける。")
        print("     この時間軸(1分足)・この5シグナルでは1日複数回転は成立しない。")
        return 0

    # ── 回転数と1日あたり期待値 ─────────────────────────────────────
    print()
    print(f"  {'シグナル':<20}{'側':<5}{'ゾーン':<10}{'N':>4}{'bp':>8}{'日t':>7}"
          f"{'機会/日':>9}{'回転/日':>8}{'手残bp':>8}{'円/日':>11}")
    hit.sort(key=lambda r: -abs(r["bp"]))
    nsd = max(1, NSYMDAY[0])
    ndays = max(1, len(_DAYIDX))
    px = max(args.min_price, 1.0) if args.min_price > 0 else 3000.0
    lot = px * args.qty
    slots = max(1.0, args.budget / lot)
    out_rows = []
    for r in hit[:25]:
        opp = r["n"] / ndays                       # 1日あたりの機会(全銘柄合計)
        _zr = next(z for z in _ZLIST if z[1] == r["zone"])
        zmin = _zr[3] - _zr[2]
        turns = min(opp / slots, zmin / max(1, r["N"]))   # 枠あたり回転数
        net = abs(r["bp"]) - args.cost_bp
        yen = net / 1e4 * lot * slots * turns
        out_rows.append({**r, "opp": opp, "turns": turns, "net": net, "yen": yen})
        print(f"  {r['label']:<20}{r['side']:<5}{r['zone']:<10}{r['N']:>4}"
              f"{r['bp']:>+8.1f}{r['t']:>+7.2f}{opp:>9.0f}{turns:>8.1f}"
              f"{net:>+8.1f}{yen:>+11,.0f}")

    best = max(out_rows, key=lambda r: r["yen"]) if out_rows else None
    if best:
        print()
        print(f"  最良: {best['label']} {best['side']} / {best['zone']} / "
              f"保有{best['N']}分")
        print(f"        1回 {best['bp']:+.1f}bp - コスト {args.cost_bp} = "
              f"**{best['net']:+.1f}bp**")
        print(f"        枠 {slots:.0f}件(予算{args.budget:,.0f}円 ÷ 建玉{lot:,.0f}円) × "
              f"回転 {best['turns']:.1f} 回/日")
        print(f"        → **1日 {best['yen']:+,.0f}円 / 月 "
              f"{best['yen'] * 20:+,.0f}円**")

    if args.split:
        trm = np.zeros(_MAXDAY, dtype=bool)
        tem = np.zeros(_MAXDAY, dtype=bool)
        for d, di in _DAYIDX.items():
            (trm if d < args.split else tem)[di] = True
        tr = {(r["sig"], r["N"], r["zone"], r["side"]): r for r in scan(trm)}
        te = {(r["sig"], r["N"], r["zone"], r["side"]): r for r in scan(tem)}
        print()
        print(f"  ─── TRAIN({int(trm.sum())}日) / TEST({int(tem.sum())}日) "
              f"境界 {args.split} ───")
        surv = 0
        for r in hit[:25]:
            k = (r["sig"], r["N"], r["zone"], r["side"])
            a, b = tr.get(k), te.get(k)
            ok = (a and b and (a["bp"] > 0) == (b["bp"] > 0)
                  and abs(b["bp"]) >= args.cost_bp)
            surv += 1 if ok else 0
            print(f"  {r['label']:<20}{r['side']:<5}{r['zone']:<10}{r['N']:>4}"
                  f"  TRAIN{(a or {}).get('bp', float('nan')):>+8.1f}"
                  f"  TEST{(b or {}).get('bp', float('nan')):>+8.1f}"
                  f"  {'✓' if ok else '✗'}")
        print(f"  **符号一致かつ TEST でもコスト超 = {surv} 升**")
        if not surv:
            print("  ⛔ TEST で再現しない。**期間ノイズ。**")

    print()
    print("  ⚠ これは **上限の見積もり**(終値ベース / スプレッドも約定可否も無し)。")
    print("     生き残った升だけを、約定判定を保守側に固定して実装し直すこと。")

    if args.csv and rows:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n  全升を {args.csv} に保存({len(rows)}行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
