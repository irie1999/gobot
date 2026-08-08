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
  # ★ 推奨: TEST窓を3本回して、全窓で平坦域が一致するときだけ採用を検討する
  python sweep_lss_smtm.py --sm-list 0.1,0.2,0.3,0.5,0.7,1.0 --tm-list 1.0,1.5,2.0 \
                           --holdout-list 60,120,180 --bt-min 30 --workers 8

  # ルールを変えるだけなら再計算不要(sweep_smtm/ の保存済みCSVを読み直す)
  python sweep_lss_smtm.py --sm-list ... --tm-list ... --holdout-list ... --reparse

  python sweep_lss_smtm.py                       # 既定グリッド(4x3=12点)・窓1本
  python sweep_lss_smtm.py --holdout-days 120    # TEST に使う直近日数
  python sweep_lss_smtm.py --dry-run             # 実行せずコマンドだけ表示

⛔ **--bt-min は 30 以上**。0 にすると低品質シグナル込みの母集団になり、
   delay 系が構造的に崩壊する(18.9.1: フィルター前 delay1 = -18M)。
   compare_lss_rules の BT は『スキャン全宇宙に対する自前のスコア』で、
   sim_oos_budget のプール床(選定済み提案)とは別物なので混同しないこと。

⛔ **--rule は実機と合わせる**(既定 delay1 = watch.bat --stop-delay-bars 1)。
   base 行は delay0 なので実機と条件が違う。

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
ap.add_argument("--bt-min", type=float, default=30.0,
                help="compare_lss_rules に渡すBT下限。**既定30**。"
                     "⛔ 0 にしてはいけない。compare_lss_rules の BT は"
                     "『スキャン全宇宙に対する自前のスコア』で、sim_oos_budget の"
                     "プール床(選定済み提案)とは別物。0 = 低品質シグナル込みの母集団で、"
                     "そこでは delay 系が構造的に崩壊する(CLAUDE.md 18.9.1: "
                     "フィルター前 delay1 = -18M / 「必ず BT30以上で判断すること」)")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--rule", type=str, default="delay1",
                help="compare_lss_rules のどのルール行を読むか(前方一致)。"
                     "**既定 delay1 = 実機(watch.bat --stop-delay-bars 1)**。"
                     "base は delay0 なので実機と条件が違う(2026-08-08 まで base を読んでいた)")
ap.add_argument("--reparse", action="store_true",
                help="再計算せず sweep_smtm/ の保存済みCSVから読み直す。"
                     "--rule を変えたときはこれで済む(1点あたり50秒の再実行が不要)")
ap.add_argument("--out", type=str, default="")
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
_RULE = args.rule.strip()
# ルールごとに別ファイル。混ざると「base の数字を delay1 だと思って読む」事故になる。
OUT = Path(args.out.strip() or f"sweep_lss_smtm_results_{_RULE}.csv")

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


def _pick_rule(csv_p) -> dict | None:
    """compare_lss_rules の出力CSVから --rule に前方一致する行を返す。"""
    try:
        for r in csv.DictReader(open(csv_p, encoding="utf-8-sig")):
            if str(r.get("rule", "")).startswith(_RULE):
                return r
    except Exception as e:
        print(f"  [error] {csv_p} を読めません: {e}")
    return None


def _row_from(rec: dict, sm: float, tm: float, phase: str) -> dict:
    def _f(k):
        try:
            return float(str(rec.get(k, 0)).replace(",", "") or 0)
        except ValueError:
            return 0.0
    return {"sm": sm, "tm": tm, "phase": phase,
            "trades": int(_f("trades")), "win_rate": _f("win_rate"), "pf": _f("pf"),
            "target": int(_f("target")), "stop": int(_f("stop")), "close": int(_f("close")),
            "net_real": _f("net_realistic_gap"), "net_cons": _f("net_conservative_slip")}


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

    # 上書きされないよう (sm, tm, phase) 付きで退避。--reparse はこれを読む。
    keep = Path(f"sweep_smtm/{csv_p.stem}_sm{sm}_tm{tm}_{phase}.csv")
    keep.parent.mkdir(exist_ok=True)
    try:
        keep.write_bytes(csv_p.read_bytes())
    except Exception:
        pass

    base = _pick_rule(csv_p)
    if base is None:
        print(f"  [error] {csv_p} に『{_RULE}』で始まる行がありません")
        return None

    row = _row_from(base, sm, tm, phase)
    print(f"  → {int(row['trades']):,}件 勝率{row['win_rate']:.0f}% PF{row['pf']:.2f} "
          f"net現実 {row['net_real']:+,.0f} / net保守 {row['net_cons']:+,.0f}  "
          f"({time.time() - t0:.0f}秒)", flush=True)
    return row


def _reparse() -> list[dict]:
    """再計算せず sweep_smtm/ の保存済みCSVから --rule の行を読み直す。

    compare_lss_rules は1回の実行で全ルールを出力しているので、
    ルールを変えるだけなら再実行(1点50秒)は不要。
    """
    import re as _re
    pat = _re.compile(r"_sm([0-9.]+)_tm([0-9.]+)_((?:TRAIN|TEST)\d*)\.csv$")
    out, miss = [], 0
    for f in sorted(Path("sweep_smtm").glob("*.csv")):
        m = pat.search(f.name)
        if not m:
            continue
        rec = _pick_rule(f)
        if rec is None:
            miss += 1
            continue
        out.append(_row_from(rec, float(m.group(1)), float(m.group(2)), m.group(3)))
    print(f"[再解析] sweep_smtm/ から {len(out)}点を『{_RULE}』で読み直しました"
          + (f" (該当行なし {miss}件)" if miss else ""))
    return out


if args.reparse:
    rows = _reparse()
    if not rows:
        sys.exit("[error] sweep_smtm/ に読める結果がありません。まず通常実行してください")
    _save(rows)
    done = {(str(r["sm"]), str(r["tm"]), r["phase"]): r for r in rows}
else:
    done = _load_done()
    rows = list(done.values())
print(f"[ルール] {_RULE} → {OUT}")
print(f"[スイープ] sm {SMS} × tm {TMS} = {len(SMS) * len(TMS)}点 × TEST窓 {HOLDOUTS} "
      f"× TRAIN/TEST = {len(SMS) * len(TMS) * len(HOLDOUTS) * 2}回")
for _H in HOLDOUTS:
    print(f"  窓{_H}日 … TRAIN: 直近{_H}日を除いた{args.train_days}日 / TEST: 直近{_H}日")
print(f"  BT下限: {args.bt_min:.0f}")
if args.bt_min < 30:
    print("  " + "!" * 86)
    print("  ⛔ --bt-min が 30 未満です。低品質シグナルを含む母集団になります。")
    print("     CLAUDE.md 18.9.1: 『フィルター前(低品質)では delay1 は崩壊(-18M)。")
    print("     BT30以上では delay1 が勝つ。**必ず BT30以上で判断すること**』")
    print("     この設定の結果で決済パラメータを決めてはいけません。")
    print("  " + "!" * 86)
if done:
    print(f"  済み {len(done)}件は飛ばします ({OUT})")

for sm in (SMS if not args.reparse else []):
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
    print(f"■ sm/tm スイープ結果 ({_RULE} ルールの net現実) — TEST窓 {H}日")
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


# ── サニティチェック: TRAIN が窓をまたいで同一なら分割が壊れている ────
# 2026-08-08 に実際に起きた。compare_lss_rules の --holdout-days が下限しか
# 動かしておらず、TRAIN に直近期間が丸ごと残っていた(TRAIN ⊇ TEST)。
# その状態では「全窓で一致」は当たり前で、判定に意味が無い。
if not _SINGLE:
    _bad = []
    for sm in SMS:
        for tm in TMS:
            vs = [_get(sm, tm, "TRAIN", H) for H in HOLDOUTS]
            vs = [v for v in vs if v is not None]
            if len(vs) > 1 and len(set(round(v, 2) for v in vs)) == 1:
                _bad.append((sm, tm))
    if _bad:
        print(f"\n{'!' * 92}")
        print("⛔ TRAIN の値が TEST窓をまたいで**完全に同一**です "
              f"({len(_bad)}/{len(SMS) * len(TMS)}点)。")
        print("   TRAIN/TEST の分割が成立していません(TEST が TRAIN の部分集合 = in-sample)。")
        print("   compare_lss_rules の --holdout-days が効いているか確認してください。")
        print("   この状態の『全窓で一致』は当たり前なので、判定は無効です。")
        print(f"{'!' * 92}")

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

# ── サニティチェック: TRAIN と TEST の1件あたりが桁違いなら母集団を疑う ──
# 2026-08-08 に実際に起きた。--bt-min 0(低品質込み)で delay1 を測ったら
# TRAIN -9,181円/件 / TEST +727円/件 と符号すら合わなかった。
# 隣接する期間で1件あたり1万円ずれるのはレジームでは説明できない。
_sane = []
for H in HOLDOUTS:
    for sm in SMS:
        for tm in TMS:
            a, b = _get(sm, tm, "TRAIN", H), _get(sm, tm, "TEST", H)
            na, nb = _get(sm, tm, "TRAIN", H, "trades"), _get(sm, tm, "TEST", H, "trades")
            if None in (a, b, na, nb) or not (na and nb):
                continue
            pa, pb = a / na, b / nb
            if pa * pb < 0 and abs(pa - pb) > 2000:
                _sane.append((H, sm, tm, pa, pb))
if _sane:
    print(f"\n{'!' * 92}")
    print(f"⛔ TRAIN と TEST で1件あたりの符号が逆かつ差が大きい点が {len(_sane)}件あります。")
    for H, sm, tm, pa, pb in _sane[:5]:
        print(f"   窓{H}日 sm={sm} tm={tm}: TRAIN {pa:+,.0f}円/件 / TEST {pb:+,.0f}円/件")
    if len(_sane) > 5:
        print(f"   ... 他 {len(_sane) - 5}件")
    print("   隣接する期間でこれはレジームでは説明できません。母集団(--bt-min)を疑ってください。")
    print("   CLAUDE.md 18.9.1: delay 系は BT30未満の低品質シグナルを含めると構造的に崩壊する。")
    print(f"{'!' * 92}")

# ⛔ 判定は『窓ごとの argmax』では**やってはいけない**。
#    平坦域の中で TRAIN を tie-break に使うと、TRAIN の argmax が安定している限り
#    どの窓でも同じ値が選ばれ、「全窓で一致」が自動的に成立してしまう。
#    それは TEST の証拠ではなく TRAIN の意見をコピーしているだけ。
#    2026-08-08 に実際にこれで tm=1.5 を「本物」と誤判定した(TEST は2窓で 1.0 が最良)。
#
#    正しくは **平坦域(=TESTで同等な範囲)そのものを窓間で比べる**:
#      ・現行値が全窓の平坦域に入っている → 変える理由が無い
#      ・現行値がどの窓の平坦域にも入っていない → **動かすべき**。
#        移動先は全窓の平坦域の共通集合から選ぶ(その中のどれを選ぶかは決められない)
_plateaus: dict = {}
_curstat: dict = {}

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
        # 保守モデルの平坦域も出す。タイトな損切りは損切り発火が多く滑りを買うので、
        # 現実モデルより保守モデルのほうが差が出やすい(18.17 の実測と同じ向き)。
        tc_max = max(tc)
        plateau_c = [av[i] for i, x in enumerate(tc)
                     if tc_max > 0 and x >= tc_max * (1 - _PLATEAU)]
        print(f"    TEST現実 の平坦域(最良から{_PLATEAU:.0%}以内): {plateau}")
        print(f"    TEST保守 の平坦域: {plateau_c if plateau_c else '(全域マイナス)'}")
        _plateaus.setdefault(name, []).append((H, plateau))
        if cur in av:
            i = av.index(cur)
            _in = cur in plateau
            print(f"    現行 {cur}: TRAIN {tr[i]:+,.0f} / TEST {te[i]:+,.0f} / "
                  f"TEST保守 {tc[i]:+,.0f}   → 平坦域に{'入っている' if _in else '**入っていない**'}")
            if not _in and te_max > 0:
                print(f"       現行は平坦域の水準の {te[i] / te_max * 100:.0f}% "
                      f"(差 {te_max - te[i]:+,.0f}円)")
            _curstat.setdefault(name, []).append((H, _in))

# ── 窓をまたいだ一致 (ここを通らないものは採用しない) ─────────────
print(f"\n{'=' * 92}")
print("■ 判定")
print(f"{'=' * 92}")
if _SINGLE:
    print("\n  ⚠ TEST窓が1本しかありません。**この結果だけで採用を決めないこと。**")
    print("     --holdout-list 60,120,180 で回し直してください。")
else:
    for name, ps in _plateaus.items():
        sets = [set(pl) for _, pl in ps]
        inter = set.intersection(*sets) if sets else set()
        cur = 0.1 if name.startswith("sm") else 1.0
        print(f"\n  {name}")
        for H, pl in ps:
            print(f"    窓{H}日の平坦域: {sorted(pl)}")
        print(f"    共通集合: {sorted(inter) if inter else '(なし)'}")
        ins = [b for _, b in _curstat.get(name, [])]
        if not inter:
            print(f"    → 窓ごとに平坦域が食い違う。**ノイズ。この軸は変えない**")
        elif all(ins) and ins:
            print(f"    → 現行 {cur} は全窓の平坦域の中。**変える理由が無い**")
        elif not any(ins) and ins:
            print(f"    → ⚠ 現行 {cur} は **どの窓の平坦域にも入っていない**。")
            print(f"       全窓でTESTが同等に良いのは {sorted(inter)}。この範囲へ動かす根拠がある。")
            print(f"       ※ この中のどれを選ぶかは決められない(TESTでは同等)。"
                  f"TRAINで選ぶと TRAIN の意見をコピーするだけになる。")
        else:
            print(f"    → 現行 {cur} は窓によって平坦域に入ったり入らなかったり。**判断保留**")

print("\n  ⛔ 採用前に必ずやること:")
print("     1. sim_portfolio_lss.py(予算・上限キャンセル込み)で再検証する。")
print("        単体の期待値がプラスでも、予算が有限だと機会費用でマイナスになる"
      "(CLAUDE.md 18.10)。")
print("     2. 差がノイズ帯を超えているか確かめる。ローリングOOSの実測で"
      "発注順の入れ替えだけで σ=124,660円/10ヶ月 動く(18.24)。")
print("        それ未満の改善は『測れていない』のであって『改善した』ではない。")
print(f"     3. 読んだルールは **{_RULE}**。実機(watch.bat --stop-delay-bars)と"
      "一致しているか確認する。")
