"""sweep_lss_smtm.py — 損切り(sm)/利確(tm)を TRAIN/TEST に分けてスイープする。

なぜ必要か
-----------
lss の決済は 損切ATR=0.1 / 利確ATR=1.0 = 名目 1:10。ところが実測(lss_exit_breakdown.py)では

    目標達成の平均勝ち  +5,246円  → 1 ATR x 100株 ≒ 5,246円
    損切りの平均       -3,091円  → 名目 0.1ATR = 約 -520円 のはずが **約6倍深い**

損切りだけで -10,387,629円 を出しており、目標達成 +7,777,625 と引け +2,196,498 を
食い潰している。**0.1 を狙っても 0.6 で約定するなら、0.1 という設定自体に意味が無い。**

実際、`--sm 0.3 --tm 1.5` で回すと、現行設定ではマイナスだった期間
(2025-04〜2026-04)が net現実 +1,623,458円 になった。ただしこれは1点の観測なので、
**グリッドで TRAIN/TEST を分けて確かめる必要がある**。

  ⛔ 単に一番良い (sm, tm) を選ぶのは in-sample フィット。CLAUDE.md 18.10 で
     同じ失敗をしている(TRAIN改善・OOS悪化)。必ず TRAIN で選んで TEST で確認する。

やること
--------
グリッドの各 (sm, tm) について compare_lss_rules.py を2回走らせる:

  TRAIN … --days <train-days> --holdout-days <holdout>   (直近を除いた期間)
  TEST  … --days <holdout>                                (その直近だけ)

`base` 行の net現実 / net保守 を拾って並べる。**TRAIN の最良点が TEST でも
生きているか**が唯一の判断材料。TEST がフラットならパラメータに意味は無い。

使い方
------
  # ★ 推奨: TEST窓を3本回して、全窓で同じ推奨が出るときだけ採用する
  python sweep_lss_smtm.py --sm-list 0.1,0.2,0.3,0.5 --tm-list 1.0,1.5,2.0 \
                           --holdout-list 60,120,180 --workers 8

  python sweep_lss_smtm.py                       # 既定グリッド(4x3=12点)・窓1本
  python sweep_lss_smtm.py --holdout-days 120    # TEST に使う直近日数
  python sweep_lss_smtm.py --dry-run             # 実行せずコマンドだけ表示

⛔ TEST窓1本の結果で採用を決めないこと。ローリングOOSの実測では月次σが月平均の
   2.7倍あり、10ヶ月でも t=+1.18 しか出ない(CLAUDE.md 18.24)。窓を1つ変えるだけで
   順位はひっくり返る。**全窓で同じ推奨が出ること**が最低条件。
   さらに採用前に sim_portfolio_lss.py(予算・上限キャンセル込み)で再検証する(18.10)。

途中で止めても、結果は sweep_lss_smtm_results.csv に都度追記されるので
**再実行すれば済んだ組み合わせは飛ばす**(重いので再開できるようにしてある)。
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser(description="lss の sm/tm を TRAIN/TEST に分けてスイープ")
ap.add_argument("--sm-list", type=str, default="0.1,0.2,0.3,0.5",
                help="損切りATR倍率のリスト(カンマ区切り)")
ap.add_argument("--tm-list", type=str, default="1.0,1.5,2.0",
                help="利確ATR倍率のリスト(カンマ区切り)")
ap.add_argument("--train-days", type=int, default=365, help="TRAIN の遡及日数")
ap.add_argument("--holdout-days", type=int, default=120,
                help="TEST に回す直近日数。TRAIN からはこの日数が除外される")
ap.add_argument("--holdout-list", type=str, default="",
                help="TEST 窓を複数指定(例 60,120,180)。指定すると窓ごとに "
                     "TRAIN/TEST を回し、**全窓で向きが一致するか**で判定する。"
                     "1窓だけの結果はノイズと区別できない(CLAUDE.md 18.24)")
ap.add_argument("--bt-min", type=float, default=0.0,
                help="compare_lss_rules に渡すBT下限。既定0=全件。"
                     "実機は BT30(=プールの床=実質フィルタなし)なので 0 と同義")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--out", type=str, default="sweep_lss_smtm_results.csv")
ap.add_argument("--dry-run", action="store_true", help="実行せずコマンドだけ表示")
ap.add_argument("--extra", type=str, default="", help="compare_lss_rules に渡す追加引数")
args = ap.parse_args()

SMS = [float(x) for x in args.sm_list.split(",") if x.strip()]
TMS = [float(x) for x in args.tm_list.split(",") if x.strip()]
# TEST 窓。--holdout-list があればそちら、無ければ --holdout-days 1本。
# ⛔ 1本だけの結果を信じてはいけない。ローリングOOSの実測で月次σが月平均の
#    2.7倍あり、10ヶ月でも t=+1.18 しか出ない(CLAUDE.md 18.24)。窓を1つ変えるだけで
#    順位は簡単にひっくり返る。**全窓で向きが一致するか**だけが判断材料。
HOLDOUTS = ([int(x) for x in args.holdout_list.split(",") if x.strip()]
            if args.holdout_list.strip() else [args.holdout_days])
OUT = Path(args.out)

_COLS = ["sm", "tm", "phase", "trades", "win_rate", "pf",
         "target", "stop", "close", "net_real", "net_cons"]


def _load_done() -> dict:
    done = {}
    if OUT.exists():
        try:
            for r in csv.DictReader(open(OUT, encoding="utf-8-sig")):
                done[(r["sm"], r["tm"], r["phase"])] = r
        except Exception:
            pass
    return done


def _save(rows: list[dict]) -> None:
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _run(sm: float, tm: float, phase: str, holdout: int) -> dict | None:
    """compare_lss_rules を1回走らせ、base 行の成績を返す。"""
    cmd = [sys.executable, "compare_lss_rules.py",
           "--min-price", str(args.min_price), "--max-price", str(args.max_price),
           "--bt-min", str(args.bt_min), "--workers", str(args.workers),
           "--sm", str(sm), "--tm", str(tm)]
    if phase.startswith("TRAIN"):
        cmd += ["--days", str(args.train_days), "--holdout-days", str(holdout)]
    else:
        cmd += ["--days", str(holdout)]
    if args.extra.strip():
        cmd += args.extra.split()

    print(f"\n{'=' * 70}\n▶ sm={sm} tm={tm} [{phase}]  {' '.join(cmd[1:])}", flush=True)
    if args.dry_run:
        return None

    _before = {p: p.stat().st_mtime for p in Path(".").glob("compare_lss_rules_*.csv")}
    t0 = time.time()
    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    except Exception as e:
        print(f"  [error] 実行に失敗: {e}")
        return None
    if pr.returncode != 0:
        print(f"  [error] compare_lss_rules が異常終了 (rc={pr.returncode})")
        print((pr.stderr or pr.stdout or "")[-1500:])
        return None

    # 新しく書かれた/更新された CSV を探す
    cands = [p for p in Path(".").glob("compare_lss_rules_*.csv")
             if p.stat().st_mtime > _before.get(p, 0)]
    if not cands:
        print("  [error] 出力CSVが見つかりません。compare_lss_rules の出力を確認してください")
        print((pr.stdout or "")[-800:])
        return None
    csv_p = max(cands, key=lambda p: p.stat().st_mtime)

    base = None
    try:
        for r in csv.DictReader(open(csv_p, encoding="utf-8-sig")):
            if str(r.get("rule", "")).startswith("base("):
                base = r
                break
    except Exception as e:
        print(f"  [error] {csv_p} を読めません: {e}")
        return None
    if base is None:
        print(f"  [error] {csv_p} に base 行がありません")
        return None

    # 上書きされないよう (sm, tm, phase) 付きで退避
    keep = Path(f"sweep_smtm/{csv_p.stem}_sm{sm}_tm{tm}_{phase}.csv")
    keep.parent.mkdir(exist_ok=True)
    try:
        keep.write_bytes(csv_p.read_bytes())
    except Exception:
        pass

    def _f(k):
        try:
            return float(str(base.get(k, 0)).replace(",", "") or 0)
        except ValueError:
            return 0.0

    row = {"sm": sm, "tm": tm, "phase": phase,
           "trades": int(_f("trades")), "win_rate": _f("win_rate"), "pf": _f("pf"),
           "target": int(_f("target")), "stop": int(_f("stop")), "close": int(_f("close")),
           "net_real": _f("net_realistic_gap"), "net_cons": _f("net_conservative_slip")}
    print(f"  → {int(row['trades']):,}件 勝率{row['win_rate']:.0f}% PF{row['pf']:.2f} "
          f"net現実 {row['net_real']:+,.0f} / net保守 {row['net_cons']:+,.0f}  "
          f"({time.time() - t0:.0f}秒)", flush=True)
    return row


done = _load_done()
rows = list(done.values())
print(f"[スイープ] sm {SMS} × tm {TMS} = {len(SMS) * len(TMS)}点 × TEST窓 {HOLDOUTS} "
      f"× TRAIN/TEST = {len(SMS) * len(TMS) * len(HOLDOUTS) * 2}回")
for _H in HOLDOUTS:
    print(f"  窓{_H}日 … TRAIN: 直近{_H}日を除いた{args.train_days}日 / TEST: 直近{_H}日")
print(f"  BT下限: {args.bt_min:.0f} (実機は BT30=プールの床=実質フィルタなし)")
if done:
    print(f"  済み {len(done)}件は飛ばします ({OUT})")

for sm in SMS:
    for tm in TMS:
        for H in HOLDOUTS:
            for _ph in ("TRAIN", "TEST"):
                # 窓が1本のときは従来の "TRAIN"/"TEST" のまま(既存CSVと互換)
                phase = _ph if len(HOLDOUTS) == 1 else f"{_ph}{H}"
                if (str(sm), str(tm), phase) in done:
                    continue
                r = _run(sm, tm, phase, H)
                if r:
                    rows.append(r)
                    _save(rows)

if args.dry_run:
    sys.exit(0)

# ── 結果表 ────────────────────────────────────────────────────────────
idx = {(float(r["sm"]), float(r["tm"]), r["phase"]): r for r in rows}
_SINGLE = len(HOLDOUTS) == 1


def _ph(kind: str, H: int) -> str:
    return kind if _SINGLE else f"{kind}{H}"


def _get(sm, tm, kind, H, col="net_real"):
    r = idx.get((sm, tm, _ph(kind, H)))
    if not r:
        return None
    try:
        return float(r[col])
    except (KeyError, TypeError, ValueError):
        return None


for H in HOLDOUTS:
    print(f"\n{'=' * 92}")
    print(f"■ sm/tm スイープ結果 (base ルールの net現実) — TEST窓 {H}日")
    print(f"{'=' * 92}")
    for kind in ("TRAIN", "TEST"):
        print(f"\n  [{kind}]")
        print(f"  {'sm＼tm':<10}" + "".join(f"{t:>16.1f}" for t in TMS))
        print("  " + "-" * (10 + 16 * len(TMS)))
        for sm in SMS:
            cells = ""
            for tm in TMS:
                v = _get(sm, tm, kind, H)
                cells += f"{v:>+16,.0f}" if v is not None else f"{'—':>16}"
            print(f"  {sm:<10.2f}{cells}")


# ── 軸ごとに TRAIN/TEST が一致しているか ────────────────────────────
# ⛔ ここが判断の本体。グリッドの最良点1つを選ぶのは in-sample フィット。
#    「sm は両方で単調、tm は逆向き」なら **sm だけ変えて tm は据え置く** のが正しい。
_PLATEAU = 0.10   # 最良値からこの割合以内は「同等」とみなす


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0] * len(xs)
    for pos, i in enumerate(order):
        r[i] = pos
    return r


def _spearman(a, b) -> float:
    ra, rb = _rank(a), _rank(b)
    n = len(a)
    if n < 3:
        return 0.0
    d2 = sum((ra[i] - rb[i]) ** 2 for i in range(n))
    return 1 - 6 * d2 / (n * (n * n - 1))


def _axis_means(axis_vals, other_vals, axis_is_sm, kind, H, col):
    """軸の各水準について、もう一方の軸で平均した値を返す。"""
    out = []
    for a in axis_vals:
        xs = []
        for o in other_vals:
            v = _get(a, o, kind, H, col) if axis_is_sm else _get(o, a, kind, H, col)
            if v is not None:
                xs.append(v)
        out.append(sum(xs) / len(xs) if xs else float("nan"))
    return out


# 窓ごとの推奨を貯めて、最後に一致を見る
_picks: dict = {}

for name, av, ov, is_sm, cur in (("sm(損切り)", SMS, TMS, True, 0.1),
                                 ("tm(利確)", TMS, SMS, False, 1.0)):
    if len(av) < 3:
        print(f"\n  {name}: 水準が{len(av)}個しかないので判定しません"
              f"(3個以上のグリッドで回してください)")
        continue
    print(f"\n{'=' * 92}")
    print(f"■ {name} — 軸ごとの TRAIN/TEST 一致 (ここが判断の本体)")
    print(f"{'=' * 92}")
    for H in HOLDOUTS:
        tr = _axis_means(av, ov, is_sm, "TRAIN", H, "net_real")
        te = _axis_means(av, ov, is_sm, "TEST", H, "net_real")
        tc = _axis_means(av, ov, is_sm, "TEST", H, "net_cons")
        if any(x != x for x in tr + te):
            print(f"\n  窓{H}日: 未完了の組み合わせがあるので判定を飛ばします")
            continue
        rho = _spearman(tr, te)
        print(f"\n  窓{H}日  (もう一方の軸で平均)")
        print(f"    {'値':<12}" + "".join(f"{v:>14.2f}" for v in av))
        print(f"    {'TRAIN 現実':<12}" + "".join(f"{x:>+14,.0f}" for x in tr))
        print(f"    {'TEST  現実':<12}" + "".join(f"{x:>+14,.0f}" for x in te))
        print(f"    {'TEST  保守':<12}" + "".join(f"{x:>+14,.0f}" for x in tc))
        print(f"    順位相関(Spearman) = {rho:+.2f}", end="")
        if rho >= 0.7:
            print("  → TRAIN と TEST が同じ向き")
        elif rho <= -0.7:
            print("  → 逆向き。**この窓では採用してはいけない**")
        else:
            print("  → 弱い。平坦域で判断")
        # TEST が平坦なとき、わずかな順位差を「不一致」と誤読しないための平坦域判定。
        te_max = max(te)
        plateau = [av[i] for i, x in enumerate(te)
                   if te_max > 0 and x >= te_max * (1 - _PLATEAU)]
        pick = None
        if plateau:
            cand = [(av[i], tr[i]) for i in range(len(av)) if av[i] in plateau]
            pick = max(cand, key=lambda kv: kv[1])[0] if cand else None
            print(f"    TEST の平坦域(最良から{_PLATEAU:.0%}以内): {plateau}")
        if pick is not None:
            print(f"    → この窓の推奨: {pick}"
                  f"{'  ⚠ グリッドの端。広げて確かめること' if pick in (av[0], av[-1]) else ''}")
            _picks.setdefault(name, []).append((H, pick))
        if cur in av:
            i = av.index(cur)
            print(f"    現行 {cur}: TRAIN {tr[i]:+,.0f} / TEST {te[i]:+,.0f} / "
                  f"TEST保守 {tc[i]:+,.0f}")

# ── 窓をまたいだ一致 (ここを通らないものは採用しない) ─────────────
print(f"\n{'=' * 92}")
print("■ 判定")
print(f"{'=' * 92}")
if _SINGLE:
    print("\n  ⚠ TEST窓が1本しかありません。**この結果だけで採用を決めないこと。**")
    print("     ローリングOOSの実測では月次σが月平均の2.7倍あり、10ヶ月でも t=+1.18"
          "(CLAUDE.md 18.24)。")
    print("     窓を1つ変えるだけで順位はひっくり返ります。")
    print("     → --holdout-list 60,120,180 で回し直して、全窓で同じ推奨が出るか"
          "確かめてください。")
else:
    for name, ps in _picks.items():
        vals = [v for _, v in ps]
        agree = len(set(vals)) == 1
        detail = " / ".join(f"窓{h}日→{v}" for h, v in ps)
        print(f"\n  {name}: {detail}")
        if agree and len(ps) == len(HOLDOUTS):
            print(f"    → **全{len(ps)}窓で一致 ({vals[0]})。この軸は本物の可能性がある**")
        else:
            print(f"    → 窓ごとにバラける。**ノイズ。この軸は変えない**")
    for name in ("sm(損切り)", "tm(利確)"):
        if name not in _picks:
            print(f"\n  {name}: 判定できるデータがありません")

print("\n  ⛔ 採用前に必ずやること:")
print("     1. sim_portfolio_lss.py(予算・上限キャンセル込み)で再検証する。")
print("        単体の期待値がプラスでも、予算が有限だと機会費用でマイナスになる"
      "(CLAUDE.md 18.10)。")
print("     2. 差がノイズ帯を超えているか確かめる。ローリングOOSの実測で"
      "発注順の入れ替えだけで σ=124,660円/10ヶ月 動く(18.24)。")
print("        それ未満の改善は『測れていない』のであって『改善した』ではない。")
