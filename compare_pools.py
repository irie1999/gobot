r"""compare_pools.py — 2つの母集団(選定あり/なし)を **同一期間** で比べる

⛔ 照会のみ。手元のCSVを読むだけ。

■ なぜ要るか (2026-08-16)

選定あり(3,054ペア)と 選定なし(8,106ペア)を測ったら結果が割れた:

    選定あり  1,655件 / 月平均 +292,440 / σ 120,723 / 月平均/σ +2.42 / 資本効率 11.09%
    選定なし  1,901件 / 月平均 +372,277 / σ 181,481 / 月平均/σ +2.05 / 資本効率 13.72%

総額と資本効率は選定なし、安定度(主指標)は選定あり。**ところが期間が違う**:

    選定あり  212営業日 (2025-10-01 〜)   ← START_DATES で各ペアの集計開始が後ろにずれる
    選定なし  242営業日 (2025-08-18 〜)   ← 選定していないので制限が無い

**σ の差が『選定なしにだけ入っている2025-08〜09の2ヶ月』由来なら、
主指標の逆転は消えます。** それを確かめないと比較になっていない。

■ 使い方

    python compare_pools.py --a lss_trades_K.csv --b lss_trades_full_K.csv
    python compare_pools.py --a ... --b ... --label-a 選定あり --label-b 選定なし

■ ⚠ 読むCSVについて

`lss_trades_*_K.csv` は **予算制約をかける前**の K の明細です
(ログの『K: 予算内 1,901件 / 母集団 2,195件』の 2,195 のほう)。
月別の形は近いですが、ボードの数字とは一致しません。

**厳密にやるなら予算月別CSV**を使ってください:

    $env:LSS_BUDGET_MONTHLY_CSV = "bud_a.csv"   # 選定ありで .\hvar
    $env:LSS_BUDGET_MONTHLY_CSV = "bud_b.csv"   # 選定なしで .\hvar
    python compare_pools.py --a bud_a.csv --b bud_b.csv --budget-csv

■ 作法 (18.24)

月数が10前後しかないので、**差は必ず対応検定(同じ月どうしの差)で見る**こと。
別々の t を比べても、両方の信頼区間が重なっていれば区別できていない。
"""
from __future__ import annotations

import argparse
import csv as _csv
import math
import sys
from collections import defaultdict

ap = argparse.ArgumentParser(
    description="2つの母集団を同一期間で比べる(照会のみ)")
ap.add_argument("--a", type=str, default="lss_trades_K.csv")
ap.add_argument("--b", type=str, default="lss_trades_full_K.csv")
ap.add_argument("--label-a", type=str, default="A")
ap.add_argument("--label-b", type=str, default="B")
ap.add_argument("--budget-csv", action="store_true",
                help="LSS_BUDGET_MONTHLY_CSV 形式(month,trades,win_rate,pnl,...)を読む")
args = ap.parse_args()

# t分布の両側5%臨界値(自由度1..30)。月数が少ないので正規近似1.96は狭すぎる。
_TT = [12.71, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
       2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
       2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


def _tcrit(df: int) -> float:
    if df <= 0:
        return float("nan")
    return _TT[df - 1] if df <= 30 else 1.96


def _load(path: str) -> dict:
    """月 -> (件数, 損益) を返す。"""
    _m: dict = defaultdict(lambda: [0, 0.0])
    try:
        for r in _csv.DictReader(open(path, encoding="utf-8-sig")):
            if args.budget_csv:
                _ym = str(r.get("month") or "").strip()[:7]
                try:
                    _n = int(float(r.get("trades") or 0))
                    _p = float(r.get("pnl") or 0)
                except Exception:
                    continue
                if _ym:
                    _m[_ym][0] += _n
                    _m[_ym][1] += _p
            else:
                if str(r.get("reason") or "").strip() in ("約定せず", ""):
                    continue
                _ym = str(r.get("entry_date") or "").strip()[:7]
                try:
                    _p = float(r.get("pnl") or 0)
                except Exception:
                    continue
                if _ym:
                    _m[_ym][0] += 1
                    _m[_ym][1] += _p
    except FileNotFoundError:
        sys.exit(f"[error] {path} がありません")
    if not _m:
        sys.exit(f"[error] {path} から月別の損益を読めません")
    return dict(_m)


A = _load(args.a)
B = _load(args.b)
_common = sorted(set(A) & set(B))
_onlyA = sorted(set(A) - set(B))
_onlyB = sorted(set(B) - set(A))

if len(_common) < 3:
    sys.exit(f"[error] 共通の月が {len(_common)}ヶ月しかありません。比較できません")


def _stats(m: dict, months: list):
    _v = [m[k][1] for k in months]
    _n = len(_v)
    _mu = sum(_v) / _n
    _sd = math.sqrt(sum((x - _mu) ** 2 for x in _v) / (_n - 1)) if _n > 1 else 0.0
    return _mu, _sd, sum(_v), sum(m[k][0] for k in months)


print("=" * 78)
print(f"■ 母集団の比較 — {args.label_a}({args.a}) vs {args.label_b}({args.b})")
print("=" * 78)
print(f"  共通 {len(_common)}ヶ月 ({_common[0]} 〜 {_common[-1]})")
if _onlyA:
    print(f"  ⚠ {args.label_a} のみ: {', '.join(_onlyA)} ← 比較から除外")
if _onlyB:
    print(f"  ⚠ {args.label_b} のみ: {', '.join(_onlyB)} ← **比較から除外**")

# ── 月別 ────────────────────────────────────────────────────────────
print(f"\n{'月':>8} {args.label_a:>14} {args.label_b:>14} {'差(B-A)':>14}")
_d = []
for _k in _common:
    _pa, _pb = A[_k][1], B[_k][1]
    _d.append(_pb - _pa)
    print(f"{_k:>8} {_pa:>+14,.0f} {_pb:>+14,.0f} {_pb - _pa:>+14,.0f}")

_ma, _sa, _ta, _na = _stats(A, _common)
_mb, _sb, _tb, _nb = _stats(B, _common)
print("-" * 78)
print(f"{'合計':>8} {_ta:>+14,.0f} {_tb:>+14,.0f} {_tb - _ta:>+14,.0f}")

# ── 同一期間での主指標 ──────────────────────────────────────────────
print(f"\n{'':>16}{args.label_a:>16}{args.label_b:>16}")
print(f"{'件数':>16}{_na:>16,}{_nb:>16,}")
print(f"{'月平均':>16}{_ma:>+16,.0f}{_mb:>+16,.0f}")
print(f"{'月次σ':>16}{_sa:>16,.0f}{_sb:>16,.0f}")
print(f"{'月平均/σ':>16}{_ma / max(_sa, 1e-9):>16.2f}"
      f"{_mb / max(_sb, 1e-9):>16.2f}   ← 主指標")

# ── 対応検定 (同じ月どうしの差) ─────────────────────────────────────
_n = len(_d)
_md = sum(_d) / _n
_sd = math.sqrt(sum((x - _md) ** 2 for x in _d) / (_n - 1)) if _n > 1 else 0.0
_se = _sd / math.sqrt(_n) if _n > 1 else 0.0
_t = _md / _se if _se > 0 else 0.0
_tc = _tcrit(_n - 1)
_lo, _hi = _md - _tc * _se, _md + _tc * _se
_win = sum(1 for x in _d if x > 0)
print(f"\n■ 対応検定 ({args.label_b} − {args.label_a})")
print(f"  平均差 {_md:>+12,.0f}円/月 / t {_t:>+6.2f} / "
      f"95%CI {_lo:>+,.0f} 〜 {_hi:>+,.0f} / {args.label_b}勝ち {_win}/{_n}ヶ月")
if _lo > 0:
    print(f"  ✅ **CI が全域プラス = {args.label_b} が有意に上**")
elif _hi < 0:
    print(f"  ✅ **CI が全域マイナス = {args.label_a} が有意に上**")
else:
    print(f"  ⚠ **CI がゼロをまたぐ = 区別できていません**"
          f"(月数 {_n} では判定不能)")

# ── 除外した月の影響 ────────────────────────────────────────────────
if _onlyB:
    _ex = sum(B[k][1] for k in _onlyB)
    _allB = sorted(B)
    _mb2, _sb2, _, _ = _stats(B, _allB)
    print(f"\n■ {args.label_b} だけにある月の影響 "
          f"({', '.join(_onlyB)} = {_ex:+,.0f}円)")
    print(f"  全期間({len(_allB)}ヶ月): 月平均 {_mb2:+,.0f} / σ {_sb2:,.0f} / "
          f"月平均/σ {_mb2 / max(_sb2, 1e-9):.2f}")
    print(f"  共通期間({_n}ヶ月): 月平均 {_mb:+,.0f} / σ {_sb:,.0f} / "
          f"月平均/σ {_mb / max(_sb, 1e-9):.2f}")
    _dd = _mb / max(_sb, 1e-9) - _mb2 / max(_sb2, 1e-9)
    print(f"  → 除外した月が主指標を {_dd:+.2f} 動かしていました"
          + ("（**σ の差の主因はこの月です**）" if abs(_dd) >= 0.15
             else "（影響は小さい = σ の差は本物）"))

print("""
■ 読み方
  ・**主指標(月平均/σ)は「共通期間」の行で比べること。** 全期間の数字は
    母集団ごとに窓が違うので比較になっていない。
  ・対応検定の CI がゼロをまたぐなら **区別できていない**。総額が大きいほうを
    選ぶ理由にはならない(18.24)。
  ・区別できないなら、**実装が単純なほう**を選ぶのが合理的。
    ⚠ ただし今回はどちらも登録上限50件なので **実装コストは同じ**
      (前夜に登録する50件が『候補49件の全部』か『候補154件の上位50』かの違い)。
""")
