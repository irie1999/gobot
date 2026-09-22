#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""先物1分足の**寄り前情報**で「その日 N を建てるか」を決められるかを測る（調査用）。

⛔ **日々の手順には入れない。** 発注もしない。CSV/ZIP を読むだけ。
⛔ 判定の基準は `FUTURES_FREEZE.md` に**先に**書いてある。ここで作らない。

★ なぜ「日経を予測する」ではないのか（2026-09-07 に分解が壊れた）

  §18.60 は「先物 → 日経の日中% → N損益」という経路を仮定して、
  ①R²=0.235 × ②R²=0.045 = 天井 0.0105 と結論した。
  ところが 2026年8月(n=20)で測ると:

      夜間リターン → 日経の日中%    r = +0.106
      日経の日中%   → N損益         r = -0.389
      → 分解どおりなら 夜間 → N は -0.041 のはず
      実測                          r = **-0.383**（9倍）

  **夜間リターンは日経の日中%を経由せずに N へ効いていた。** 経路はこちら:

      夜間リターン → **当日の合格件数**  r = +0.846  [+0.64, +0.94]
      当日の合格件数 → N損益            r = -0.446  [-0.74, -0.00]

  夜間に上がる → ギャップアップ銘柄が増える → 合格が増える → 負ける。
  §18.60 の天井(σ削減0.5%)は **この経路には掛からない**。

⚠ ただし上は n=20。§18.58① で「前夜の候補数」は TRAIN/TEST で反転して
  不合格になっている。**2年で測り直すのがこのツールの目的。**

★ 使い方（この順を守る）

    # 0) N の日次損益を作る（既存ツール。1回だけ）
    python analyze_gap_edge.py --days 4200 --min-ret1 1.753 --min-gap-bp 100 \
        --min-price 1000 --max-price 6000 --split 2023-01-01 \
        --dump-picks n_picks_all.csv

    # 1) TRAIN(2023)だけで掃く。**TEST は画面にも出ない**
    python analyze_preopen_futures.py --futures-dir "C:/Users/to732/Downloads" \
        --picks n_picks_all.csv --explore

    # 2) 出た候補を **1つだけ** TEST(2024)で確かめる
    python analyze_preopen_futures.py --futures-dir "..." --picks n_picks_all.csv \
        --confirm night_ret:Q5

⛔ --confirm は **1候補につき1回きり**。落ちたら基準を緩めない。
⛔ 2025年以降(ホールドアウト)は読まない。--train/--test の外は捨てる。
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as _dt
import io
import os
import re
import sys
import zipfile
from collections import defaultdict

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--futures-dir", required=True,
                help="future_ohlc_minute_19_*.zip / .csv のあるフォルダ")
ap.add_argument("--picks", required=True,
                help="analyze_gap_edge --dump-picks が書いた明細CSV")
ap.add_argument("--train", default="2023", help="TRAIN の年（既定 2023）")
ap.add_argument("--test", default="2024", help="TEST の年（既定 2024）")
ap.add_argument("--explore", action="store_true",
                help="TRAIN だけで掃く。⛔ TEST は画面にも出さない")
ap.add_argument("--confirm", default="",
                help="TEST を1回だけ使う。例 night_ret:Q5")
ap.add_argument("--seeds", type=int, default=200, help="帰無較正の本数（既定200）")
ap.add_argument("--band", type=int, default=200, help="ランダム帯の本数（既定200）")
ap.add_argument("--budget-man", type=float, default=400.0, help="予算（万円）")
ap.add_argument("--exclude", default="20240806",
                help="除外する日 yyyyMMdd をカンマ区切り"
                     "（既定 20240806 = 寄り前が 2/15分。FUTURES_FREEZE §1.3）")
ap.add_argument("--save", default="preopen_panel.csv", help="日次パネルの保存先")
a = ap.parse_args()

if a.explore and a.confirm:
    sys.exit("[error] --explore と --confirm は同時に使えません")
if not a.explore and not a.confirm:
    sys.exit("[error] --explore か --confirm のどちらかを指定してください")

_FNAME = re.compile(r"future_ohlc_minute_(\d+)_(\d{6})", re.I)
_EXC = {int(x) for x in a.exclude.split(",") if x.strip().isdigit()}


# ══════════════════════════════════════════════════════════════════
# 1. 先物1分足 → 夜間・寄り前の特徴（価格だけ。予測はしない）
# ══════════════════════════════════════════════════════════════════
def _iter_csv(path: str):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for nm in zf.namelist():
                if nm.lower().endswith(".csv"):
                    with zf.open(nm) as fh:
                        yield csv.DictReader(
                            io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
    else:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            yield csv.DictReader(fh)


def _second_friday(y: int, m: int) -> int:
    """四半期SQ(第2金曜) を yyyyMMdd で返す。"""
    c = calendar.Calendar()
    fri = [d for d in c.itermonthdates(y, m)
           if d.month == m and d.weekday() == 4]
    d = fri[1]
    return d.year * 10000 + d.month * 100 + d.day


def _front_by_calendar(days: list[int]) -> dict[int, int]:
    """取引日 → 中心限月。**四半期SQ の前営業日に乗り換える**。

    FUTURES_FREEZE.md §1.2。2023-01〜2024-12 の交代8回すべてがこの規則で、
    例外は無かった（2026-09-07 実測）。営業日は先物データの trade_date から取る
    （祝日カレンダーを別に持たない）。
    """
    import bisect
    ds = sorted(days)
    last = ds[-1]

    def _roll_day(y: int, m: int) -> int:
        """SQ(第2金曜) の **前営業日**。営業日は ds から取る。

        ⛔ SQ がデータの終端より後なら、その乗り換えは観測範囲に入らない。
          そこを ds の最終日で代用すると、最終日だけ次の限月へ飛ぶ
          (2026-09-07 の検算で 20241230 → 202612 が出た)。+∞ を返す。
        """
        sq = _second_friday(y, m)
        if sq > last:
            return 99999999
        i = bisect.bisect_left(ds, sq)
        return ds[i - 1] if i > 0 else sq

    out: dict[int, int] = {}
    for d in ds:
        y, m = d // 10000, (d // 100) % 100
        ym = (y, ((m - 1) // 3 + 1) * 3)
        if ym[1] > 12:
            ym = (y + 1, 3)
        for _ in range(12):
            if d < _roll_day(*ym):
                out[d] = ym[0] * 100 + ym[1]
                break
            ym = (ym[0] + 1, 3) if ym[1] == 12 else (ym[0], ym[1] + 3)
        else:                                    # 3年先まで見て決まらない = 異常
            sys.exit(f"[error] {d} の中心限月が決まりません（暦規則の不具合）")
    return out


def build_futures_panel(folder: str) -> pd.DataFrame:
    files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
             if _FNAME.search(f) and f.lower().endswith((".zip", ".csv"))
             and _FNAME.search(f).group(1) == "19"]
    if not files:
        sys.exit(f"[error] future_ohlc_minute_19_*.zip がありません: {folder}")
    print(f"[futures] mini {len(files)}ファイル")

    # (trade_date, cmonth) -> 夜間/日中の集計。1パスで集める
    ng: dict[tuple[int, int], dict] = {}
    dy: dict[tuple[int, int], dict] = {}
    vol: dict[tuple[int, int], int] = defaultdict(int)
    for p in files:
        for rd in _iter_csv(p):
            for row in rd:
                try:
                    td = int(row["trade_date"]); sid = int(row["session_id"])
                    it = int(row["interval_time"]); cm = int(row["contract_month"])
                    o = float(row["open_price"]); h = float(row["high_price"])
                    lo = float(row["low_price"]); c = float(row["close_price"])
                    v = int(float(row["trade_volume"] or 0))
                except (TypeError, ValueError, KeyError):
                    continue
                k = (td, cm)
                if sid == 3:                       # 夜間 17:00-23:59 / 00:00-06:00
                    # 並び順: 17時台からの通し。1700 未満は翌日ぶんなので後ろ
                    ordv = (0 if it >= 1700 else 1) * 10000 + it
                    d = ng.get(k)
                    if d is None:
                        ng[k] = {"first": (ordv, o), "last": (ordv, c),
                                 "hi": h, "lo": lo, "n": 1}
                    else:
                        if ordv < d["first"][0]:
                            d["first"] = (ordv, o)
                        if ordv > d["last"][0]:
                            d["last"] = (ordv, c)
                        d["hi"] = max(d["hi"], h); d["lo"] = min(d["lo"], lo)
                        d["n"] += 1
                else:                              # 日中
                    vol[k] += v
                    d = dy.get(k)
                    if d is None:
                        dy[k] = {"o845": None, "c859": None, "c1545": None, "n": 0}
                        d = dy[k]
                    d["n"] += 1
                    if it == 845:
                        d["o845"] = o
                    if it == 859:
                        d["c859"] = c
                    if it == 1545:
                        d["c1545"] = c

    days = sorted({td for td, _ in vol})
    front = _front_by_calendar(days)

    # 自己点検: 前日の出来高1位と何日ちがうか（8回前後なら正常）
    topv: dict[int, int] = {}
    for (td, cm), v in vol.items():
        if td not in topv or v > vol[(td, topv[td])]:
            topv[td] = cm
    dis = sum(1 for d in days if front.get(d) != topv.get(d))
    print(f"[futures] 営業日 {len(days)} / 暦の限月と出来高1位のちがい {dis}日 "
          + ("(乗り換え日ぶん = 正常)" if dis <= len(days) // 40 + 10
             else "⛔ 多すぎ。暦規則を疑うこと"))

    rows = []
    prev_c1545 = None
    for d in days:
        cm = front[d]
        n = ng.get((d, cm)); y = dy.get((d, cm))
        if n is None or y is None or n["n"] < 50:
            prev_c1545 = (y or {}).get("c1545") or prev_c1545
            continue
        o17 = n["first"][1]; c06 = n["last"][1]
        r = {
            "date": d,
            "night_ret": (c06 - o17) / o17 * 100 if o17 else np.nan,
            "night_range": (n["hi"] - n["lo"]) / o17 * 100 if o17 else np.nan,
            "night_pos": ((c06 - n["lo"]) / (n["hi"] - n["lo"])
                          if n["hi"] > n["lo"] else 0.5),
            "night_bars": n["n"],
        }
        # 前日15:45終値 → 06:00（夜間リターンのもう一つの定義）
        r["night_ret_c2c"] = ((c06 - prev_c1545) / prev_c1545 * 100
                              if prev_c1545 else np.nan)
        # 夜間終値 → 08:45始値（寄りのギャップ）
        r["open_gap"] = ((y["o845"] - c06) / c06 * 100
                         if y.get("o845") and c06 else np.nan)
        # 08:45 → 08:59（寄り前15分）
        r["pre15_ret"] = ((y["c859"] - y["o845"]) / y["o845"] * 100
                          if y.get("o845") and y.get("c859") else np.nan)
        rows.append(r)
        prev_c1545 = y.get("c1545") or prev_c1545
    df = pd.DataFrame(rows)
    print(f"[futures] 特徴を作れた日 {len(df)} / {len(days)}")
    return df


# ══════════════════════════════════════════════════════════════════
# 2. N の日次損益（analyze_gap_edge --dump-picks の明細から）
# ══════════════════════════════════════════════════════════════════
def build_n_daily(path: str) -> pd.DataFrame:
    p = pd.read_csv(path, encoding="utf-8-sig")
    need = {"date", "pnl"}
    if need - set(p.columns):
        sys.exit(f"[error] {path} に {need - set(p.columns)} がありません")
    p["d"] = pd.to_datetime(p["date"]).dt.strftime("%Y%m%d").astype(int)
    g = p.groupby("d").agg(N=("pnl", "sum"), 建玉=("pnl", "size"),
                           投入=("entry_p", lambda s: float(s.sum()) * 100)
                           if "entry_p" in p.columns else ("pnl", "size"))
    print(f"[picks] {len(p):,}件 / {len(g)}営業日 "
          f"({g.index.min()} 〜 {g.index.max()})")
    return g


# ══════════════════════════════════════════════════════════════════
# 3. 判定（FUTURES_FREEZE.md §4）
# ══════════════════════════════════════════════════════════════════
def _cvar(x: np.ndarray, q: float = 0.05) -> float:
    if len(x) == 0:
        return 0.0
    k = max(1, int(np.ceil(len(x) * q)))
    return float(np.sort(x)[:k].mean())


def _stats(x: np.ndarray) -> dict:
    return {"合計": float(x.sum()), "日平均": float(x.mean()),
            "日σ": float(x.std(ddof=1)) if len(x) > 1 else 0.0,
            "CVaR5%": _cvar(x), "最悪日": float(x.min()) if len(x) else 0.0}


def judge(pnl: np.ndarray, flag: np.ndarray, label: str, seeds: int) -> dict:
    """flag=True の日を休んだときの効果。§4.1 の3条件を全部出す。"""
    k = int(flag.sum())
    n = len(pnl)
    if k == 0 or k == n:
        return {"ok": False, "why": f"休む日が {k}/{n} 日で比較できません"}
    kept = pnl[~flag]
    base = _stats(pnl)
    rule = _stats(kept)
    # ① 等価な一律縮小（同じ日数を休むぶんだけサイズを落としただけの世界）
    scale = 1.0 - k / n
    flat = _stats(pnl * scale)
    # ② 同じ日数をランダムに休む帯（片側。CVaR は負なので z>0 が「軽い」）
    rng = np.random.default_rng(0)
    rc, rw, rt = [], [], []
    for _ in range(seeds):
        m = np.zeros(n, dtype=bool)
        m[rng.choice(n, k, replace=False)] = True
        s = _stats(pnl[~m])
        rc.append(s["CVaR5%"]); rw.append(s["最悪日"]); rt.append(s["合計"])
    rc = np.array(rc); rw = np.array(rw); rt = np.array(rt)
    z_c = ((rule["CVaR5%"] - rc.mean()) / rc.std(ddof=1)) if rc.std(ddof=1) else 0.0
    z_w = ((rule["最悪日"] - rw.mean()) / rw.std(ddof=1)) if rw.std(ddof=1) else 0.0
    z_t = ((rule["合計"] - rt.mean()) / rt.std(ddof=1)) if rt.std(ddof=1) else 0.0
    return {"ok": True, "k": k, "n": n, "base": base, "rule": rule, "flat": flat,
            "z_cvar": z_c, "z_worst": z_w, "z_total": z_t, "label": label,
            "rand_cvar": rc.mean()}


def show(j: dict) -> None:
    if not j.get("ok"):
        print(f"  ⛔ {j['why']}")
        return
    b, r, f = j["base"], j["rule"], j["flat"]
    print(f"  休む日 {j['k']}/{j['n']}日 ({j['k'] / j['n'] * 100:.0f}%)")
    print(f"    {'':16s}{'合計':>12}{'日平均':>10}{'日σ':>10}{'CVaR5%':>11}{'最悪日':>11}")
    for nm, s in (("① 現行(全部建てる)", b), ("② このルール", r),
                  ("③ 一律縮小(等価)", f)):
        print(f"    {nm:16s}{s['合計']:>12,.0f}{s['日平均']:>10,.0f}"
              f"{s['日σ']:>10,.0f}{s['CVaR5%']:>11,.0f}{s['最悪日']:>11,.0f}")
    print(f"    ②−③ CVaR {r['CVaR5%'] - f['CVaR5%']:+,.0f}  "
          f"最悪日 {r['最悪日'] - f['最悪日']:+,.0f}  合計 {r['合計'] - f['合計']:+,.0f}")
    print(f"    ランダム帯(同じ日数を休む) z: CVaR {j['z_cvar']:+.2f} / "
          f"最悪日 {j['z_worst']:+.2f} / 合計 {j['z_total']:+.2f}"
          f"   {'★帯の外' if j['z_cvar'] >= 2.0 else '帯の中'}")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fut = build_futures_panel(a.futures_dir)
    nd = build_n_daily(a.picks)
    df = fut.merge(nd, left_on="date", right_index=True, how="inner")
    if _EXC:
        before = len(df)
        df = df[~df["date"].isin(_EXC)]
        print(f"[除外] {sorted(_EXC)} → {before - len(df)}日 落としました "
              f"(FUTURES_FREEZE §1.3)")
    df["year"] = df["date"] // 10000
    tr = df[df["year"].astype(str) == a.train].reset_index(drop=True)
    te = df[df["year"].astype(str) == a.test].reset_index(drop=True)
    out = df[df["year"].astype(str).isin({a.train, a.test})]
    out.to_csv(a.save, index=False, encoding="utf-8-sig")
    print(f"[panel] TRAIN({a.train}) {len(tr)}日 / TEST({a.test}) {len(te)}日 "
          f"→ {a.save}")
    dropped = len(df) - len(out)
    if dropped:
        print(f"[panel] TRAIN/TEST の外 {dropped}日 は **読みません**"
              f"（ホールドアウト保護 / FUTURES_FREEZE §1.4）")
    if len(tr) < 60:
        print("⛔ TRAIN が 60日未満です。データが足りているか確認してください")
        return 1

    FEATS = ["night_ret", "night_ret_c2c", "night_range", "night_pos",
             "open_gap", "pre15_ret"]
    FEATS = [c for c in FEATS if c in tr.columns and tr[c].notna().sum() >= 40]

    if a.explore:
        print("\n" + "=" * 78)
        print(f"■ 探索 — TRAIN({a.train}) {len(tr)}日 だけ")
        print(f"  ⛔ TEST は読んでいません。ここで見つけたものは **候補** であって"
              f"結論ではありません")
        print("=" * 78)
        pnl = tr["N"].to_numpy(float)
        # 帰無較正: 損益を日順に巡回シフト（日ブロックを保つ / §18.13）
        rng = np.random.default_rng(1)
        null_best = []
        for _ in range(a.seeds):
            sh = np.roll(pnl, int(rng.integers(1, len(pnl))))
            bz = -9e9
            for c in FEATS:
                v = tr[c].to_numpy(float)
                m = ~np.isnan(v)
                if m.sum() < 40:
                    continue
                qs = np.quantile(v[m], [0.2, 0.4, 0.6, 0.8])
                for qi in range(5):
                    lo = -np.inf if qi == 0 else qs[qi - 1]
                    hi = np.inf if qi == 4 else qs[qi]
                    fl = m & (v > lo) & (v <= hi) if qi else m & (v <= qs[0])
                    if fl.sum() < 5:
                        continue
                    j = judge(sh, fl, "", 24)
                    if j.get("ok"):
                        bz = max(bz, j["z_cvar"])
            null_best.append(bz)
        null_best = np.array([x for x in null_best if x > -9e8])
        thr = float(np.quantile(null_best, 0.95)) if len(null_best) else 2.0
        print(f"\n  帰無較正 {len(null_best)}本（損益を巡回シフト。**最良分位を選ぶ操作**"
              f"込み）\n    帰無の最良 z: 中央 {np.median(null_best):+.2f} / "
              f"95%点 **{thr:+.2f}**  ← これを超えて初めて候補")
        print(f"\n  {'特徴':>14} {'分位':>4} {'日数':>5} {'CVaR z':>8} "
              f"{'最悪日 z':>9} {'②−③ CVaR':>11}  判定")
        print("  " + "-" * 74)
        cands = []
        for c in FEATS:
            v = tr[c].to_numpy(float)
            m = ~np.isnan(v)
            qs = np.quantile(v[m], [0.2, 0.4, 0.6, 0.8])
            for qi in range(5):
                lo = -np.inf if qi == 0 else qs[qi - 1]
                hi = np.inf if qi == 4 else qs[qi]
                fl = m & (v > lo) & (v <= hi) if qi else m & (v <= qs[0])
                if fl.sum() < 5:
                    continue
                j = judge(pnl, fl, f"{c}:Q{qi + 1}", a.band)
                if not j.get("ok"):
                    continue
                d = j["rule"]["CVaR5%"] - j["flat"]["CVaR5%"]
                hit = j["z_cvar"] >= thr
                if hit:
                    cands.append((c, qi + 1, j["z_cvar"], d))
                print(f"  {c:>14} {'Q' + str(qi + 1):>4} {j['k']:>5} "
                      f"{j['z_cvar']:>8.2f} {j['z_worst']:>9.2f} {d:>11,.0f}"
                      f"  {'★候補' if hit else ''}")
        print(f"\n  候補 {len(cands)}個（帰無の期待 {len(FEATS) * 5 * 0.05:.1f}個）")
        if cands:
            for c, q, z, d in sorted(cands, key=lambda x: -x[2]):
                print(f"    python analyze_preopen_futures.py --futures-dir ... "
                      f"--picks ... --confirm {c}:Q{q}")
            print("\n  ⛔ **1つだけ選んで --confirm を1回**。落ちたら基準を緩めない")
        else:
            print("    → TRAIN で候補ゼロ。**TEST を使わずにここで終了**"
                  "（in-sample で出ないものが OOS で出ることはない / §18.34b）")
        return 0

    # ---- confirm ----
    try:
        cf, cq = a.confirm.split(":")
        qi = int(cq.lstrip("Qq")) - 1
    except ValueError:
        return int(bool(sys.stderr.write("[error] --confirm は night_ret:Q5 の形式\n"))) or 1
    if cf not in tr.columns:
        print(f"[error] 特徴 {cf} がありません。使えるのは {FEATS}")
        return 1
    print("\n" + "=" * 78)
    print(f"■ 確認 — {a.confirm}   ⛔ TEST({a.test}) を **1回だけ** 使います")
    print("=" * 78)
    res = {}
    for nm, d in (("TRAIN " + a.train, tr), ("TEST " + a.test, te)):
        v = d[cf].to_numpy(float); m = ~np.isnan(v)
        qs = np.quantile(tr[cf].dropna().to_numpy(float), [0.2, 0.4, 0.6, 0.8])
        lo = -np.inf if qi == 0 else qs[qi - 1]
        hi = np.inf if qi == 4 else qs[qi]
        fl = m & (v > lo) & (v <= hi) if qi else m & (v <= qs[0])
        print(f"\n【{nm}】  ※ 分位の境界は **TRAIN で決めた値** を当てはめています")
        j = judge(d["N"].to_numpy(float), fl, nm, a.band)
        show(j)
        res[nm] = j
    print("\n" + "=" * 78)
    print("■ 合否（FUTURES_FREEZE.md §4.3）")
    tj, sj = res["TRAIN " + a.train], res["TEST " + a.test]
    checks = [
        ("TRAIN でも CVaR が一律縮小を上回る",
         tj.get("ok") and tj["rule"]["CVaR5%"] > tj["flat"]["CVaR5%"]),
        ("TEST でも CVaR が一律縮小を上回る",
         sj.get("ok") and sj["rule"]["CVaR5%"] > sj["flat"]["CVaR5%"]),
        ("TEST がランダム帯の外 (z_CVaR ≥ +2.0)",
         sj.get("ok") and sj["z_cvar"] >= 2.0),
        ("両年で符号が一致",
         tj.get("ok") and sj.get("ok")
         and (tj["rule"]["CVaR5%"] - tj["flat"]["CVaR5%"])
         * (sj["rule"]["CVaR5%"] - sj["flat"]["CVaR5%"]) > 0),
        ("TEST で日平均が悪化しない",
         sj.get("ok") and sj["rule"]["日平均"] >= sj["flat"]["日平均"]),
    ]
    for nm, ok in checks:
        print(f"  {'✅' if ok else '⛔'} {nm}")
    if all(ok for _, ok in checks):
        print("\n  ✅ 5つとも通りました。次は 2025年以降(ホールドアウト)を買って最終確認")
        print("  ⚠ 買う前に FUTURES_FREEZE.md §4.4 の閾値を確認すること")
        return 0
    print("\n  ⛔ 不合格。**この候補は終了**。基準を緩めない / 別の分位を試さない")
    print("     (試すなら試した回数を FUTURES_FREEZE.md に記録し、補正をやり直す)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
