"""
market_regime.py — 相場環境タブと同じ判定(get_regime)で、日経の大局レジーム
(上げ / 横ばい / 下げ)を現在＆過去について月ごとに表示する。

各月末について「その時点までの日経終値だけ」で判定するので(先読みなし)、
チャートと照合して『2021-2023は横ばい』『2020/3は下げ』等が正しく出るか検証できる。

使い方:
  python market_regime.py                 # 過去36ヶ月
  python market_regime.py --months 120    # 過去10年
  python market_regime.py --years 20      # 日経を20年ぶん取得
"""
import argparse

import nikkei_analysis as na

ap = argparse.ArgumentParser()
ap.add_argument("--months", type=int, default=36, help="表示する月数 (既定36)")
ap.add_argument("--years", type=int, default=15, help="取得する日経の年数 (既定15)")
args = ap.parse_args()

close = na.fetch_n225(args.years)
if close is None or len(close) < 220:
    print("[ERROR] 日経データ取得失敗 or データ不足")
    raise SystemExit(1)
close = close.sort_index()

# ── 各月末について「その時点まで」で大局レジームを判定(先読みなし) ──
_ym = close.index.strftime("%Y-%m")
_month_ends = close.groupby(_ym).tail(1)

_lbl = {"up": "🟢 上げ", "sideways": "🟡 横ばい", "down": "🔴 下げ", "?": "? 不明"}
rows = []
for d in _month_ends.index:
    sub = close[close.index <= d]
    if len(sub) < 220:
        continue
    r = na.get_regime(sub)
    vm = (r["cur"] / r["ma200"] - 1) * 100 if r.get("ma200") else float("nan")
    rows.append((d, r["cur"], r["macro"], r.get("er", 0.0), r.get("slope200", 0.0), vm))

if not rows:
    print("[ERROR] 判定に十分なデータがありません")
    raise SystemExit(1)

# ── 現在 ──
d0, c0, m0, er0, sl0, vm0 = rows[-1]
print("=" * 62)
print(f"  現在の大局レジーム ({d0.date()})  ※相場環境タブと同一ロジック")
print("=" * 62)
print(f"  → 【 {_lbl.get(m0, m0)} 】   終値 {c0:,.0f}円")
print(f"     ER(トレンド強度) {er0:.2f}   MA200傾き {sl0:+.1f}%   終値はMA200から {vm0:+.1f}%")
print(f"     (ER<0.20=横ばい / ER高+MA200上向き&上=上げ / ER高+MA200下向き&下=下げ)")

# ── 月末推移(チャート照合用) ──
print("\n" + "=" * 62)
print(f"  月末レジーム推移 (直近{args.months}ヶ月・チャートと見比べる)")
print("=" * 62)
print(f"{'月':<9}{'終値':>10}{'ER':>6}{'MA200傾':>9}{'vsMA200':>9}  レジーム")
print("-" * 58)
for d, c, m, er, sl, vm in rows[-args.months:]:
    print(f"{d.strftime('%Y-%m'):<9}{c:>10,.0f}{er:>6.2f}{sl:>+8.1f}%{vm:>+8.1f}%  {_lbl.get(m, m)}")

# ── 集計 ──
from collections import Counter
_c = Counter(r[2] for r in rows[-args.months:])
print("\n" + "-" * 58)
print(f"  直近{args.months}ヶ月の内訳: "
      f"上げ {_c.get('up',0)}ヶ月 / 横ばい {_c.get('sideways',0)}ヶ月 / 下げ {_c.get('down',0)}ヶ月")
print("※ 横ばいの月が多い時期＝現行(順張り)が苦手だった時期のはず")
