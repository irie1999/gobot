"""check_daily_universe.py — 09:00 に何銘柄を登録・照会することになるか

⛔ 照会のみ。ネットワークも kabu も叩かない。手元のCSVを数えるだけ。

■ なぜ要るか (2026-08-16)

K(09:00確認方式)は「その日の候補を全部登録して始値を読む」。ところが実測で

    **kabu の銘柄登録は 50件が上限。51件以上は 400 でリクエストごと失敗**
    (部分受理されない = 1件でも超えたら全滅)

と分かった。したがって **1日の候補数が50を超えるか** が実装の分かれ目になる。
超える日があるなら「前夜に流動性上位50件へ絞る」ルールが必要で、
**バックテスト側も同じ絞りを入れないとライブとズレる**(18.9 の鉄則)。

■ 何を数えるか

`lss_trades_H.csv`(.\\daily / .\\hvar が出力)は **約定せず の行も含む**ので、
これが「その日 注文を出した/出すはずだった銘柄」= 候補の母集団そのもの。
日ごとにユニーク銘柄数を数えれば、09:00 に登録・照会する件数が出る。

■ 使い方

    python check_daily_universe.py
    python check_daily_universe.py --csv lss_trades_H.csv --cap 50
    python check_daily_universe.py --csv lss_trades_K.csv     # K の母集団で見る
    python check_daily_universe.py --days 120                 # 直近N日だけ

⚠ H は「全シグナルに指値を置く」方式なので、H の明細が候補の母集団になる。
   K も判定対象は同じ集合(ギャップで絞るのは *読んだ後*)なので、
   **09:00 に読む件数は H の件数と同じ**。
"""
from __future__ import annotations

import argparse
import csv as _csv
import sys
from collections import defaultdict

ap = argparse.ArgumentParser(
    description="09:00 に登録・照会する銘柄数の分布(照会のみ・CSVを数えるだけ)")
ap.add_argument("--csv", type=str, default="lss_trades_H.csv",
                help="明細CSV。約定せず の行も含むもの(既定 lss_trades_H.csv)")
ap.add_argument("--cap", type=int, default=50,
                help="kabu の登録上限(実測50)。これを超える日を数える")
ap.add_argument("--days", type=int, default=0, help="直近N営業日だけ(0=全部)")
args = ap.parse_args()

_by_day: dict = defaultdict(set)
_n_rows = 0
try:
    for r in _csv.DictReader(open(args.csv, encoding="utf-8-sig")):
        d = str(r.get("entry_date") or "").strip()[:10]
        s = str(r.get("symbol") or "").strip().upper() \
            .removesuffix(".T").split(".")[0]
        if d and s:
            _by_day[d].add(s)
            _n_rows += 1
except FileNotFoundError:
    sys.exit(f"[error] {args.csv} がありません。\n"
             f"  H の明細は .\\daily / .\\hvar が出力します"
             f"(LSS_TRADES_CSV=lss_trades.csv → lss_trades_H.csv)。")

if not _by_day:
    sys.exit(f"[error] {args.csv} から (entry_date, symbol) を読めません")

_days = sorted(_by_day)
if args.days > 0:
    _days = _days[-args.days:]
_cnt = sorted(len(_by_day[d]) for d in _days)


def _q(p):
    return _cnt[min(len(_cnt) - 1, int(len(_cnt) * p))]


print("=" * 70)
print(f"■ 09:00 に登録・照会する銘柄数 — {args.csv}")
print("=" * 70)
print(f"  {len(_days):,}営業日 / 明細 {_n_rows:,}行 "
      f"({_days[0]} 〜 {_days[-1]})")
print(f"\n  中央 {_q(0.5)}件 / 75%点 {_q(0.75)}件 / 90%点 {_q(0.9)}件 / "
      f"95%点 {_q(0.95)}件 / **最大 {_cnt[-1]}件**")

_over = [d for d in _days if len(_by_day[d]) > args.cap]
print(f"\n  **{args.cap}件を超えた日: {len(_over)}日 / {len(_days)}日 "
      f"({len(_over) / len(_days) * 100:.0f}%)**")
if _over:
    print(f"    最悪の5日:")
    for d in sorted(_over, key=lambda x: -len(_by_day[x]))[:5]:
        print(f"      {d}  {len(_by_day[d])}件 "
              f"(**{len(_by_day[d]) - args.cap}件 溢れる**)")

# 分布のヒストグラム(20件刻み)
print(f"\n  分布:")
_bins = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50),
         (50, 70), (70, 100), (100, 10 ** 9)]
for lo, hi in _bins:
    n = sum(1 for c in _cnt if lo <= c < hi)
    if not n:
        continue
    _lbl = f"{lo}-{hi - 1}件" if hi < 10 ** 9 else f"{lo}件以上"
    _mark = " ⛔ 上限超" if lo >= args.cap else ""
    print(f"    {_lbl:>10}  {n:>4}日  "
          f"{'█' * max(1, int(n / max(1, len(_days)) * 50))}{_mark}")

# ── 必要バッチ数 (登録上限で割る) ───────────────────────────────────────
print(f"\n  必要バッチ数（登録上限{args.cap}件で割った回数）:")
_bt = {}
for d in _days:
    _nb = -(-len(_by_day[d]) // args.cap)      # 切り上げ
    _bt[_nb] = _bt.get(_nb, 0) + 1
_cum = 0
for _nb in sorted(_bt):
    _cum += _bt[_nb]
    print(f"    {_nb}バッチ  {_bt[_nb]:>4}日 "
          f"({_bt[_nb] / len(_days) * 100:>4.0f}%)  累積 "
          f"{_cum / len(_days) * 100:>3.0f}%")
_avg_b = sum(_nb * n for _nb, n in _bt.items()) / len(_days)
print(f"    平均 {_avg_b:.2f}バッチ/日")
print(f"\n  ★ バッチ回しが成立した場合の所要時間の目安"
      f"(1バッチ ≒ 読み6秒 + 待ち1秒 = 7秒):")
for _lbl, _nb in (("中央", -(-_q(0.5) // args.cap)),
                  ("75%点", -(-_q(0.75) // args.cap)),
                  ("95%点", -(-_q(0.95) // args.cap)),
                  ("最大", -(-_cnt[-1] // args.cap))):
    print(f"    {_lbl:>5}  {_nb}バッチ = 約 {_nb * 7:>3}秒")
print(f"    平均 約 {_avg_b * 7:.0f}秒")
print("    ⚠ ただし **再登録がウォームなら**の話。毎回コールドなら1バッチ125秒。"
      "\n       確かめるのは check_board_limits.py --rotate 100")

print("\n" + "=" * 70)
print("■ 判定")
print("=" * 70)
if not _over:
    print(f"  ✅ **{args.cap}件を超える日はありません**。"
          f"候補をそのまま全部登録できます。絞りのルールは不要。")
elif len(_over) / len(_days) < 0.05:
    print(f"  ⚠ 超える日が {len(_over) / len(_days) * 100:.0f}% あります。"
          f"稀ですが **1件でも超えると 400 で全滅**するので、"
          f"絞りは必ず実装すること(『たまにしか起きない』では済まない)。")
else:
    print(f"  ⛔ **{len(_over) / len(_days) * 100:.0f}% の日で超えます**。"
          f"絞りは必須。前夜に **流動性降順で上位{args.cap}件** に切り、"
          f"バックテスト側にも同じ絞りを入れること(18.9 の鉄則)。")
print(f"""
  ★ 絞るなら軸は **流動性(売買代金)降順** しかありません。
    ギャップは 09:00 まで分からないので前夜には使えず(先読み)、
    BT は識別力ゼロと確定しています(18.12/18.24)。
    18.21 で発注順を流動性降順にしたのと同じ理屈です。

  ⚠ 絞ると **その日の候補が減る** ので、資金均等の分母も変わります。
    バックテストに入れると K の数字が動くので、必ず同じ実行の中で比較すること。""")
