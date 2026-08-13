r"""map_intraday_structure.py — 日中リターンの構造を「地図」にする。

何のためのものか
----------------
1日に複数回売買したい。だが思いついた戦略(オープニングレンジ / VWAP乖離 /
急騰の戻り…)を1つずつ試すのは効率が悪く、試した数だけ多重検定になる。

先に **データの構造そのもの** を測る:

    直近 M 分の動きは、その後 N 分の動きとどう関係するか

  * 上げた後に上がる / 下げた後に下がる → **継続(順張り)** が成立しうる
  * 上げた後に下がる / 下げた後に上がる → **反転(逆張り)** が成立しうる
  * ゼロ                                → その (M, N) では何もできない

これを時間帯別に格子で回すと、「何分の動きが何分後に継続/反転するか」が
1枚の表になる。**データが「ここに何かある」と示した升だけを戦略化する。**

判定は最初から決まっている
--------------------------
各升の値を **往復コスト** と直接比べる。東証の呼値は制度上の最小刻みなので
推測が入らない(建値2,800円なら1円 = 3.57bp):

  * 板を1回ずつ叩く   = 2ティック = **約 7.2bp**
  * 常に不利側で約定  = 4ティック = **約 14.3bp**

  ⛔ どの升も超えない → コストを超える構造が無い。**そこで終了**
  ✅ 超える升がある   → その升だけを戦略化する

⚠ ここで出る値は **上限の見積もり**。終値ベースで、スプレッドも指値の約定可否も
   入っていない。「上限がコストを超えなければ確実にダメ、超えても取れるとは限らない」
   という **非対称な使い方** をすること。

⚠ 格子は 6×6×時間帯4×方向2 = 288 升ある。**多重検定なので、|t|>=2 の升が数個
   出るのは偶然でも起きる**(288×5% ≈ 14個)。--split で TRAIN/TEST に割り、
   **両方で同じ升が出るか**を必ず確認すること。

使い方
------
  python map_intraday_structure.py                      # 300銘柄 × 180日
  python map_intraday_structure.py --symbols 600 --days 365
  python map_intraday_structure.py --split 2026-04-01   # TRAIN/TEST に割る
  python map_intraday_structure.py --quantile 0.05      # より極端な動きだけ見る
  python map_intraday_structure.py --csv map.csv

前提: 1分足が ~/.jquants_cache/minute/<code>_1m.pkl にあること(fetch_1m_all.py)。
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

ap = argparse.ArgumentParser(description="日中リターンの構造マップ(戦略を作る前の地図)")
ap.add_argument("--symbols", type=int, default=300, help="ランダムに何銘柄使うか(0=全部)")
ap.add_argument("--days", type=int, default=180, help="遡及日数")
ap.add_argument("--split", default=None, help="TRAIN/TEST の境界日 YYYY-MM-DD")
ap.add_argument("--min-price", type=float, default=1000.0, help="0=フィルタなし")
ap.add_argument("--max-price", type=float, default=6000.0, help="0=フィルタなし")
ap.add_argument("--quantile", type=float, default=0.20,
                help="上位/下位 何割の動きを『大きな動き』とみなすか")
ap.add_argument("--cost-bp", type=float, default=14.3,
                help="この値を超えた升だけ候補にする(呼値4ティック相当)")
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--csv", default=None, help="全升を保存")
args = ap.parse_args()

try:
    from tenkan_sim import _bars_one, drop_symbol, find_minute_dirs
except Exception as e:                                            # pragma: no cover
    sys.exit(f"[error] tenkan_sim を import できません: {e}")

_d5, _d1 = find_minute_dirs()
if _d1 is None:
    sys.exit("[error] 1分足ディレクトリが見つかりません。fetch_1m_all.py を実行するか "
             "MINUTE_1M_DIR を設定してください "
             "(既定 ~/.jquants_cache/minute/<コード>_1m.pkl)")

# ── 格子 ───────────────────────────────────────────────────────────────
MS = [1, 3, 5, 10, 15, 30]          # 直近M分の動き
NS = [1, 3, 5, 10, 15, 30]          # その後N分の動き
# 時間帯(分)。東証は2024/11以降 15:30 引け。
ZONES = [("寄り30分", 540, 570),     # 09:00-09:30
         ("前場", 570, 690),         # 09:30-11:30
         ("後場前半", 750, 870),     # 12:30-14:30
         ("引け60分", 870, 930)]     # 14:30-15:30
NZ = len(ZONES)
# r_past のビン(bp)。分位を後から求めるために細かく刻む。
EDGES = np.arange(-300, 301, 10, dtype=np.float64)     # 61本 → 62ビン
NB = len(EDGES) + 1

# 集計器: (M, N, zone, bin, day) の r_fut 合計 / 件数。
# 日次を持つのは ①日クラスタ t を出すため ②TRAIN/TEST を後から割るため。
_DAYIDX: dict = {}
_DAYLIST: list = []
_MAXDAY = args.days + 40
SUM = np.zeros((len(MS), len(NS), NZ, NB, _MAXDAY), dtype=np.float64)
CNT = np.zeros((len(MS), len(NS), NZ, NB, _MAXDAY), dtype=np.float64)
_OVERFLOW = [0]


def zone_of(mins: np.ndarray) -> np.ndarray:
    """分(0-1439)の配列 → 時間帯index。どこにも属さなければ -1。"""
    z = np.full(mins.shape, -1, dtype=np.int64)
    for i, (_, a, b) in enumerate(ZONES):
        z[(mins >= a) & (mins < b)] = i
    return z


def _day_index(day: str) -> int:
    di = _DAYIDX.get(day)
    if di is None:
        if len(_DAYIDX) >= _MAXDAY:
            _OVERFLOW[0] += 1
            return -1
        di = len(_DAYIDX)
        _DAYIDX[day] = di
        _DAYLIST.append(day)
    return di


def _jq_to_yf(code: str) -> str:
    """J-Quants の5桁(末尾0)を yfinance 表記へ。'72030' -> '7203.T'

    ⛔ available_local_symbols() は stock_5min の **ファイル名(=J-Quants 5桁)** を
       返す。そのまま '.T' を足すと '72030.T' になり、_bars_one が内部でさらに
       '0' を足して '720300_1m.pkl' を探しに行く(=全件ファイルなし)。
       fetch_1m_all.py と同じ変換をここでも通すこと。
    """
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def universe() -> list[str]:
    try:
        from daytrade_data import available_local_symbols
        syms = [_jq_to_yf(s) for s in available_local_symbols()]
    except Exception as e:
        sys.exit(f"[error] ユニバースが取れません: {e}")
    if not syms:
        sys.exit("[error] ユニバースが空です(daytrade_data.available_local_symbols)")
    rng = random.Random(args.seed)
    rng.shuffle(syms)
    return syms if args.symbols <= 0 else syms[:args.symbols]


def accumulate(c: np.ndarray, mins: np.ndarray, di: int) -> int:
    """1銘柄日ぶんを集計器に足す。戻り値=足した観測数。"""
    n = len(c)
    if di < 0 or n < max(MS) + 2:
        return 0
    zall = zone_of(mins)
    rf_of = {}
    for N in set(NS):
        if n > N:
            rf_of[N] = c[N:] / c[:-N] - 1.0           # rf_of[N][k] は t=k
    used = 0
    for mi, M in enumerate(MS):
        if n <= M + 1:
            continue
        rp_all = c[M:] / c[:-M] - 1.0                 # rp_all[k] は t=k+M
        b_all = np.digitize(rp_all * 1e4, EDGES)      # 0..NB-1
        for ni, N in enumerate(NS):
            hi = n - 1 - N                            # t の上限
            if hi < M or N not in rf_of:
                continue
            rp = rp_all[0:hi - M + 1]
            b = b_all[0:hi - M + 1]
            rf = rf_of[N][M:hi + 1]
            z = zall[M:hi + 1]
            ok = (z >= 0) & np.isfinite(rp) & np.isfinite(rf)
            if not ok.any():
                continue
            idx = z[ok] * NB + b[ok]
            s = np.bincount(idx, weights=rf[ok], minlength=NZ * NB)
            k = np.bincount(idx, minlength=NZ * NB).astype(np.float64)
            SUM[mi, ni, :, :, di] += s.reshape(NZ, NB)
            CNT[mi, ni, :, :, di] += k.reshape(NZ, NB)
            used += int(ok.sum())
    return used


def _tail_stats(mi: int, ni: int, zi: int, upper: bool, dmask: np.ndarray):
    """上位(または下位) quantile のビン群における r_fut 平均(bp)と日クラスタ t。

    分位は **そのセルの実分布** から求める(固定閾値だと M によって意味が変わる)。
    """
    cnt = CNT[mi, ni, zi][:, dmask].sum(axis=1)
    tot = cnt.sum()
    if tot < 500:
        return None
    target = tot * args.quantile
    order = range(NB - 1, -1, -1) if upper else range(NB)
    sel, acc = [], 0.0
    for b in order:
        if cnt[b] <= 0:
            continue
        sel.append(b)
        acc += cnt[b]
        if acc >= target:
            break
    if not sel or acc < 200:
        return None
    ds = SUM[mi, ni, zi][sel][:, dmask].sum(axis=0)
    dk = CNT[mi, ni, zi][sel][:, dmask].sum(axis=0)
    k = dk.sum()
    if k <= 0:
        return None
    mean_bp = ds.sum() / k * 1e4
    live = dk > 0
    t, nd = 0.0, int(live.sum())
    if nd > 2:
        dv = (ds[live] / dk[live]) * 1e4              # 日ごとの平均(bp)
        sd = dv.std(ddof=1)
        if sd > 0:
            t = float(dv.mean() / (sd / math.sqrt(nd)))
    return {"bp": float(mean_bp), "n": float(k), "t": t, "days": nd}


def collect(dmask: np.ndarray) -> list[dict]:
    rows = []
    for zi, (zn, _, _) in enumerate(ZONES):
        for updown, upper in (("上げた後", True), ("下げた後", False)):
            for mi, M in enumerate(MS):
                for ni, N in enumerate(NS):
                    r = _tail_stats(mi, ni, zi, upper, dmask)
                    if r is None:
                        continue
                    same = (upper and r["bp"] > 0) or ((not upper) and r["bp"] < 0)
                    rows.append({"zone": zn, "dir": updown, "M": M, "N": N,
                                 "bp": round(r["bp"], 2), "t": round(r["t"], 2),
                                 "n": int(r["n"]), "days": r["days"],
                                 "how": "継続" if same else "反転"})
    return rows


def print_tables(dmask: np.ndarray, title: str) -> list[dict]:
    print(f"  ═══ {title} ═══")
    for zi, (zn, _, _) in enumerate(ZONES):
        for updown, upper in (("上げた後", True), ("下げた後", False)):
            print(f"  【{zn} / 直近M分で{updown}(上位{args.quantile:.0%})】"
                  f"  数値 = その後N分のリターン平均(bp)  ※負=下がる")
            print("        " + "".join(f"{'N=' + str(n):>10}" for n in NS))
            for mi, M in enumerate(MS):
                line = f"  M={M:<4}"
                for ni in range(len(NS)):
                    r = _tail_stats(mi, ni, zi, upper, dmask)
                    if r is None:
                        line += f"{'—':>10}"
                        continue
                    mark = "*" if (abs(r["bp"]) >= args.cost_bp
                                   and abs(r["t"]) >= 2.0) else " "
                    line += f"{r['bp']:>+9.1f}{mark}"
                print(line)
            print()
    return collect(dmask)


def main() -> int:
    syms = universe()
    cut = (datetime.now(JST).date() - timedelta(days=args.days))
    print("■ 日中リターンの構造マップ")
    print("=" * 96)
    print(f"  M分の動き → その後N分の動き / 時間帯別 / 上位・下位 "
          f"{args.quantile:.0%} の動きの後を見る")
    print(f"  対象: {len(syms)}銘柄 × 直近{args.days}日 / 価格 "
          f"{args.min_price:,.0f}〜{args.max_price:,.0f}円")
    print(f"  判定: 往復コスト {args.cost_bp}bp(呼値4ティック相当)を超える升があるか")
    print(f"  ⚠ 格子は {len(MS) * len(NS) * NZ * 2} 升。多重検定なので "
          f"|t|>=2 が {len(MS) * len(NS) * NZ * 2 * 0.05:.0f} 個前後は偶然でも出る。")
    print()

    # ⛔ 例外を握りつぶすと「0採用」の原因が分からなくなる。理由を数える。
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
                    if not ((args.min_price <= 0 or px >= args.min_price) and
                            (args.max_price <= 0 or px <= args.max_price)):
                        why["価格帯外"] += 1
                    else:
                        dates = np.array([ts.date() for ts in d.index])
                        cl = d["close"].to_numpy(dtype=float)
                        mn = np.array([ts.hour * 60 + ts.minute
                                       for ts in d.index], dtype=np.int64)
                        for u in np.unique(dates):
                            m = dates == u
                            if int(m.sum()) < 40:
                                continue
                            got = accumulate(cl[m], mn[m], _day_index(str(u)))
                            if got:
                                nobs += got
                                nday += 1
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
        print()
        print("  【0件の内訳】 " + " / ".join(f"{k} {v}" for k, v in why.items()))
        for e in errs:
            print(f"    {e}")
        print(f"    1分足DIR: {_d1}")
        print(f"    例: {syms[0]} → {str(syms[0]).replace('.T', '')}0_1m.pkl を探します")
        sys.exit("[error] 観測が0件。上の内訳と、python check_minute_data.py を確認してください。")
    if sum(why.values()):
        print(f"    [info] 除外: " + " / ".join(f"{k} {v}" for k, v in why.items()
                                                if v))
    if _OVERFLOW[0]:
        print(f"    [warn] 日数上限({_MAXDAY})を超えた銘柄日 {_OVERFLOW[0]} 件を捨てました。"
              f"--days を減らすか _MAXDAY を上げてください。")
    print()
    print(f"  採用 {nsym}銘柄 / {nday:,}銘柄日 / {nobs:,}観測 / {len(_DAYIDX)}営業日")
    print()

    allmask = np.zeros(_MAXDAY, dtype=bool)
    allmask[:len(_DAYIDX)] = True
    rows = print_tables(allmask, "全期間")

    tr_rows = te_rows = []
    if args.split:
        trm = np.zeros(_MAXDAY, dtype=bool)
        tem = np.zeros(_MAXDAY, dtype=bool)
        for d, di in _DAYIDX.items():
            (trm if d < args.split else tem)[di] = True
        print(f"  ─── TRAIN({int(trm.sum())}日) / TEST({int(tem.sum())}日) "
              f"境界 {args.split} ───")
        tr_rows = collect(trm)
        te_rows = collect(tem)

    # ── 判定 ───────────────────────────────────────────────────────────
    cand = [r for r in rows if abs(r["bp"]) >= args.cost_bp and abs(r["t"]) >= 2.0]
    cand.sort(key=lambda r: -abs(r["bp"]))
    exp_fp = len(rows) * 0.05
    print("  【判定】")
    print(f"  全 {len(rows)} 升 / コスト {args.cost_bp}bp 超 かつ |日t|>=2 = "
          f"**{len(cand)} 升**  (偶然でも {exp_fp:.0f} 個前後は出る)")

    if not cand:
        print()
        print("  ⛔ **候補ゼロ。コストを超える構造が無い。**")
        print("     終値ベースの上限見積もりですら往復コストに届かないので、")
        print("     実際の執行(スプレッド・指値の約定可否)を入れれば確実にマイナス。")
        print("     この時間軸・この刻みで1日複数回の売買は成立しない。")
        print()
        print("     残る手は刻みを変えること:")
        print("       --quantile 0.05   より極端な動きだけ見る(急騰・急落に限定)")
        print("       --min-price 0 --max-price 0   価格帯を外す")
        print("       MS / NS の格子を変える(秒〜数分 or 1時間超)")
        print("     それでも出なければ、この方向は畳む。")
        return 0

    if len(cand) <= exp_fp:
        print(f"  ⚠ 候補数が偶然の期待値({exp_fp:.0f})以下。**構造がある証拠にならない。**")

    key = {(r["zone"], r["dir"], r["M"], r["N"]) for r in cand}
    tr = {(r["zone"], r["dir"], r["M"], r["N"]): r for r in tr_rows}
    te = {(r["zone"], r["dir"], r["M"], r["N"]): r for r in te_rows}
    print()
    hdr = (f"  {'時間帯':<10}{'方向':<10}{'M':>4}{'N':>4}{'bp':>9}{'日t':>7}"
           f"{'観測':>12}  {'解釈':<6}")
    if args.split:
        hdr += f"{'TRAIN bp':>10}{'TEST bp':>10}  一致"
    print(hdr)
    for r in cand[:25]:
        k = (r["zone"], r["dir"], r["M"], r["N"])
        line = (f"  {r['zone']:<10}{r['dir']:<10}{r['M']:>4}{r['N']:>4}"
                f"{r['bp']:>+9.1f}{r['t']:>+7.2f}{r['n']:>12,}  "
                f"{r['how'] + ('(順張り)' if r['how'] == '継続' else '(逆張り)'):<6}")
        if args.split:
            a, b = tr.get(k), te.get(k)
            av = a["bp"] if a else float("nan")
            bv = b["bp"] if b else float("nan")
            ok = (a and b and (av > 0) == (bv > 0)
                  and abs(bv) >= args.cost_bp)
            line += f"{av:>+10.1f}{bv:>+10.1f}  {'✓' if ok else '✗'}"
        print(line)

    if args.split:
        surv = [r for r in cand
                if (lambda a, b: a and b and (a["bp"] > 0) == (b["bp"] > 0)
                    and abs(b["bp"]) >= args.cost_bp)(
                    tr.get((r["zone"], r["dir"], r["M"], r["N"])),
                    te.get((r["zone"], r["dir"], r["M"], r["N"])))]
        print()
        print(f"  **TRAIN/TEST で符号が一致し、TEST でもコスト超 = {len(surv)} 升**")
        if not surv:
            print("  ⛔ 全期間で見えた升は TEST で再現しない。**期間ノイズ。**")
            return 0

    print()
    print("  ⚠ これは **上限の見積もり**(終値ベース / スプレッドも指値の約定可否も無し)。")
    print("     次: 生き残った升だけを戦略化し、約定判定を保守側に固定して測り直す。")
    print("     ・指値なら『高値が指値を超えたときだけ約定』")
    print("     ・同一1分足内でエントリーと決済を同時に起こさない")
    print("     ・ランダム帯を作ってから判定する")

    if args.csv:
        out = []
        for r in rows:
            k = (r["zone"], r["dir"], r["M"], r["N"])
            out.append({**r,
                        "train_bp": (tr.get(k) or {}).get("bp", ""),
                        "test_bp": (te.get(k) or {}).get("bp", "")})
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows(out)
        print(f"\n  全升を {args.csv} に保存({len(out)}行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
