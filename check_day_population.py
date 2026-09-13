"""check_day_population.py — ある1日が J の母集団から丸ごと消えた理由を特定する

⛔ 何のためのツールか (2026-08-25)
────────────────────────────────────────────────────────────────────────
2026-08-21(J の初運用日 / 実発注7銘柄 +16,870円)が、レポートの J タブに
**1件も出ていない**。前後の日は出ている:

    lss_trades_K.csv   08-19: 9件 / 08-20: 10件 / **08-21: 0件** / 08-24: 6件
    lss_trades.csv     08-21: 113件   ← 生の lss バックテストには**ある**

つまり「lss は取引を作ったが、E/H/J 変換で全部落ちた」。eh_trades.py は
銘柄日ごとに次のどれかで捨てる (`_sk` カウンタ):

    日足なし / 日足に該当日なし / 価格・ATR異常 / 5分足なし / 分割ガード
    / 先頭バーが09:00でない(既定OFF)

このツールは **その判定を1日ぶん再現**して、どれで落ちたかを数える。
レポートを回し直さなくても分かる(既存の日足キャッシュと5分足を読むだけ)。

使い方:
  python check_day_population.py --date 2026-08-21
  python check_day_population.py --date 2026-08-21 --compare 2026-08-20
  python check_day_population.py --date 2026-08-21 --limit 40   # 銘柄を絞って速く

⚠ 照会のみ。発注も再計算もしない。
"""
from __future__ import annotations

import argparse
import csv as _csv
import re
from collections import Counter
from pathlib import Path

ap = argparse.ArgumentParser(description="ある1日が J の母集団から消えた理由を特定する")
ap.add_argument("--date", type=str, required=True, help="調べる日 yyyy-MM-dd")
ap.add_argument("--compare", type=str, default="",
                help="正常な日と並べる (yyyy-MM-dd)。差が出た項目が原因")
ap.add_argument("--trades", type=str, default="lss_trades.csv",
                help="銘柄リストの供給元 (既定 lss_trades.csv = 生の全取引)")
ap.add_argument("--limit", type=int, default=0, help="銘柄数の上限 (0=全部)")
ap.add_argument("--gap-bp", type=float, default=75.0, help="J の合格ギャップ (既定75bp)")
args = ap.parse_args()


def _n(s) -> str:
    return re.sub(r"\D", "", str(s or "").split(".")[0]) or str(s or "")


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


try:
    from intraday_integrity import day_scale_ok as _ig_ok
except Exception:                                    # ガードが無い版でも動かす
    def _ig_ok(_d5, _c):                             # noqa: ANN001
        return True

import backtest_limit_entry as _b                     # noqa: E402
import pandas as _pd                                  # noqa: E402


def _symbols_on(day: str) -> list[str]:
    p = Path(args.trades)
    if not p.exists():
        raise SystemExit(f"[error] {p} がありません")
    out = []
    for r in _csv.DictReader(open(p, encoding="utf-8-sig")):
        if str(r.get("entry_date") or "")[:10] == day:
            s = _n(r.get("symbol"))
            if s and s not in out:
                out.append(s)
    return out


def _audit(day: str) -> tuple[Counter, list[str]]:
    """1日ぶん、eh_trades と同じ順番で判定して落ちた理由を数える。"""
    syms = _symbols_on(day)
    if args.limit:
        syms = syms[:args.limit]
    print(f"\n{'=' * 74}\n■ {day} — {args.trades} に載っている {len(syms)} 銘柄を判定\n{'=' * 74}")
    if not syms:
        print("  ⛔ この日の取引が1件もありません。日付か --trades を確認してください")
        return Counter(), []

    cnt: Counter = Counter()
    ok_syms: list[str] = []
    _first_hm_cnt: Counter = Counter()
    ts = _pd.Timestamp(day)
    for i, s in enumerate(syms, 1):
        if i % 50 == 0:
            print(f"  … {i}/{len(syms)}", flush=True)
        try:
            df = _b.fetch(f"{s}.T")
        except Exception:
            df = None
        if df is None or len(df) == 0:
            cnt["日足なし"] += 1
            continue
        # ⛔ fetch は atr 列を持たない。eh_trades._load_d と**同じ式**で作る
        #    (TR の EWM span=14)。ここを合わせないと『価格/ATR異常』の判定がずれる。
        _idx = _pd.to_datetime(df.index).normalize()
        _d = df.copy()
        _d.index = _idx
        if ts not in _d.index:
            cnt["日足に該当日なし"] += 1
            continue
        pos = int(_idx.searchsorted(ts))
        if pos <= 0 or pos >= len(_idx) or _idx[pos] != ts:
            cnt["日足に該当日なし"] += 1
            continue
        _pcs = _d["close"].shift(1)
        _tr = _pd.concat([_d["high"] - _d["low"], (_d["high"] - _pcs).abs(),
                          (_d["low"] - _pcs).abs()], axis=1).max(axis=1)
        _atr_s = _tr.ewm(span=14, adjust=False).mean()
        pc = float(_d["close"].iloc[pos - 1])
        o1 = float(_d["open"].iloc[pos])
        c1 = float(_d["close"].iloc[pos])
        atr = float(_atr_s.iloc[pos - 1])
        if not (pc > 0 and o1 > 0 and c1 > 0 and atr == atr and atr > 0):
            cnt["価格/ATR異常"] += 1
            continue
        try:
            d5 = (_b._load_5m_by_day(f"{s}.T") or {}).get(ts.date())
        except Exception:
            d5 = None
        if d5 is None or len(d5) == 0:
            cnt["5分足なし"] += 1
            continue
        if not _ig_ok(d5, c1):
            cnt["分割ガード"] += 1
            continue
        try:
            _hm = _pd.Timestamp(d5.index[0]).strftime("%H:%M")
        except Exception:
            _hm = "?"
        _first_hm_cnt[_hm] += 1
        # ここまで来れば母集団に入る。あとは J の合格判定(参考)
        gap = (o1 - pc) / pc * 1e4
        if gap >= args.gap_bp:
            cnt["✅ 母集団に入り、J も合格"] += 1
            ok_syms.append(f"{s}({gap:+.0f}bp)")
        else:
            cnt["母集団に入るが J は不合格(ギャップ不足)"] += 1

    print(f"\n  {'判定':<34}{'件数':>6}")
    print("  " + "-" * 40)
    for k, v in sorted(cnt.items(), key=lambda x: -x[1]):
        _mark = "  " if k.startswith(("✅", "母集団")) else "⛔"
        print(f"  {_mark}{k:<32}{v:>6}")
    print("  " + "-" * 40)
    print(f"  {'合計':<34}{sum(cnt.values()):>6}")
    if _first_hm_cnt:
        _tot = sum(_first_hm_cnt.values())
        _top = _first_hm_cnt.most_common(4)
        print(f"\n  5分足の先頭バー時刻: "
              + " / ".join(f"{h} {n}件({n / _tot * 100:.0f}%)" for h, n in _top))
    if ok_syms:
        print(f"\n  ✅ J が建てるはずの銘柄 {len(ok_syms)}件: "
              + ", ".join(ok_syms[:15])
              + (f" …他{len(ok_syms) - 15}件" if len(ok_syms) > 15 else ""))
    return cnt, ok_syms


_c1, _ok1 = _audit(args.date)
if args.compare:
    _c2, _ok2 = _audit(args.compare)
    print(f"\n{'=' * 74}\n■ 差分 — {args.date} と {args.compare}\n{'=' * 74}")
    _keys = sorted(set(_c1) | set(_c2))
    print(f"  {'判定':<34}{args.date:>12}{args.compare:>12}{'差':>8}")
    print("  " + "-" * 66)
    for k in _keys:
        a, b = _c1.get(k, 0), _c2.get(k, 0)
        _mk = "⛔" if (a - b) and not k.startswith(("✅", "母集団")) else "  "
        print(f"  {_mk}{k:<32}{a:>12}{b:>12}{a - b:>+8}")
    print(f"\n  → 差が大きい行が原因です。"
          f"『5分足なし』なら J-Quants の取得漏れ、"
          f"『分割ガード』なら5分足と日足のスケール不整合(§18.27)。")

print(f"""
{'=' * 74}
■ 読み方
{'=' * 74}
  この判定は eh_trades.py:425-478 と同じ順番・同じ条件です。
  ✅ の件数が 0 なのに lss_trades.csv にはその日の取引がある場合、
  **上の ⛔ のどれかで全部落ちている** = J タブがその日を丸ごと採点していません。

  ⚠ 「母集団に入るが J は不合格」は正常です(その日ギャップが足りなかっただけ)。
  ⚠ このツールは母集団に入るかどうかだけを見ます。実際の J の取引になるには
     さらに予算(資金均等)を通ります。
""")
