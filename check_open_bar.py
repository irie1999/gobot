r"""check_open_bar.py — 5分足に **09:00 のバー** があるかを日別に数える。

■ なぜ必要か (2026-08-18 ユーザー指摘)

  「今日はシグナルから合格もして、実際に約定もした。それでレポートに
    今日の結果が無いのはおかしい」

  そのとおり。J(09:00確認)は 5分足の先頭バーが 09:00 でない銘柄日を
  『遅寄り』として **建てない**(既定 LSS_SKIP_LATE_OPEN=1)。だからその日の
  5分足に 09:00 が無いと、合格していても損益タブに1件も出ない。

  ⛔ ところが「yfinance で埋めた日だから」という説明は **矛盾する**。
     2026-08-17 も同じ経路で取っているのに9件出ている。
     → 推測をやめて、**ファイルを直接読んで数える**のがこれ。

■ 使い方

    python check_open_bar.py                 # 直近10営業日 × 200銘柄
    python check_open_bar.py --days 20 --limit 500
    python check_open_bar.py --date 2026-08-18 --show 20   # その日の内訳を細かく
    python check_open_bar.py --symbols 9107,2734,6742      # 今日の実発注銘柄だけ

■ 読み方

    09:00あり が 0% の日 → J はその日 **1件も建てない**
      ・全銘柄が 09:05 始まり → データ側の問題(取り直しが要る)
      ・銘柄によってバラバラ → 個別の事情(その銘柄が実際に遅寄り)
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

ap = argparse.ArgumentParser(description="5分足に09:00バーがあるかを日別に数える")
ap.add_argument("--days", type=int, default=10, help="直近何営業日を見るか")
ap.add_argument("--limit", type=int, default=200, help="読む銘柄数(先頭から)")
ap.add_argument("--symbols", type=str, default="", help="カンマ区切りで銘柄を明示")
ap.add_argument("--date", type=str, default="", help="この日の内訳を細かく出す")
ap.add_argument("--show", type=int, default=10, help="--date のとき何銘柄まで出すか")
args = ap.parse_args()

try:
    import daytrade_data as _dd
    import pandas as pd
except Exception as e:
    raise SystemExit(f"[error] daytrade_data を読めません: {e}")

print(f"[5分足] DATA_DIR = {_dd.DATA_DIR}")
if not Path(_dd.DATA_DIR).exists():
    raise SystemExit(f"[error] {_dd.DATA_DIR} がありません")

if args.symbols:
    _syms = [s.strip().upper().removesuffix(".T").split(".")[0]
             for s in args.symbols.split(",") if s.strip()]
else:
    try:
        _syms = _dd.available_local_symbols()[:args.limit]
    except Exception:
        _syms = [p.stem for p in sorted(Path(_dd.DATA_DIR).glob("*.pkl"))][:args.limit]
print(f"[5分足] {len(_syms):,}銘柄 を読みます\n")

# 日付 -> 先頭バー時刻 -> 件数
_by_day: dict = defaultdict(Counter)
# (日付, 銘柄) -> 先頭バー時刻   ← --date の内訳用
_detail: dict = {}
_n_ok = 0
for _s in _syms:
    try:
        # ⛔ **ローカル(stock_5min)だけを見る**。load_intraday は
        #   無ければ yfinance に落ちるので、それだと『ローカルに
        #   09:00 があるか』という肝心の問いに答えられない。
        _df = _dd._load_local(_s, days=max(args.days * 2, 30))
    except Exception:
        continue
    if _df is None or len(_df) == 0:
        continue
    _n_ok += 1
    try:
        _days = _dd.split_by_day(_df)
    except Exception:
        continue
    for _d, _sub in _days.items():
        if _sub is None or len(_sub) == 0:
            continue
        _hm = pd.Timestamp(_sub.index[0]).strftime("%H:%M")
        _ds = str(_d)[:10]
        _by_day[_ds][_hm] += 1
        if args.date and _ds == args.date:
            _detail[(_ds, _s)] = _hm

print(f"[5分足] 読めた {_n_ok:,}/{len(_syms):,}銘柄\n")
if not _by_day:
    raise SystemExit("[error] 1件も読めませんでした")

_dd_list = sorted(_by_day)[-args.days:]
print("=" * 78)
print("■ 日別: 先頭バーが 09:00 の銘柄数  (J はこれが無いと1件も建たない)")
print("=" * 78)
print(f"  {'日付':<12}{'09:00あり':>12}{'全体':>8}{'割合':>7}   先頭バーの内訳")
_bad = []
for _d in _dd_list:
    _c = _by_day[_d]
    _tot = sum(_c.values())
    _ok = _c.get("09:00", 0)
    _pct = _ok / max(1, _tot) * 100
    if _pct < 50:
        _bad.append(_d)
    print(f"  {_d:<12}{_ok:>12,}{_tot:>8,}{_pct:>6.0f}%   "
          + " / ".join(f"{h} {n:,}" for h, n in _c.most_common(4))
          + ("   ⛔ **この日は J が建たない**" if _pct < 50 else ""))

print()
if _bad:
    print(f"⛔ 09:00バーが半分未満の日: {', '.join(_bad)}")
    print("   J(09:00確認)はこの日の取引を **1件も作りません**。")
    print("   合格していても、実際に約定していても、損益タブに出ません。")
    print()
    print("   考えられる原因:")
    print("     ① その日の5分足を yfinance で埋めた(09:00-09:05 を持たない / §18.38)")
    print("        → J-Quants の5分足を取り直す(stock_5min)")
    print("     ② 板寄せのバーを別扱いにするデータ源が混ざっている")
    print("   ★ 他の日が 09:00 を持っているなら ① が濃厚です。")
    print("     `--date <その日> --show 30` で銘柄ごとの内訳を見てください。")
else:
    print("✅ 直近の日はすべて 09:00 バーを持っています。")
    print("   → レポートに日が抜けるのは **別の原因**です(合格ゼロ / 予算 / 母集団)。")

if args.date:
    print()
    print("=" * 78)
    print(f"■ {args.date} の銘柄別 先頭バー時刻")
    print("=" * 78)
    _rows = sorted(_detail.items())
    if not _rows:
        print(f"  ⛔ {args.date} のデータが1銘柄もありません(休場日 or 未取得)")
    for (_d, _s), _hm in _rows[:args.show]:
        print(f"  {_s:<8} {_hm}" + ("" if _hm == "09:00" else "   ← 09:00 が無い"))
    if len(_rows) > args.show:
        print(f"  … 他 {len(_rows) - args.show:,}銘柄")
