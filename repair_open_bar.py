r"""repair_open_bar.py — 5分足の欠けた **09:00 バー** を J-Quants で埋め直す。

⛔⛔ **J-Quants の契約が要ります**。2026-08-18 時点で未契約なので **使えません**。
   契約なしの環境では、代わりに nikkei_analysis 側で『その日の最頻の先頭バー
   より後に始まった銘柄だけを遅寄りとする』ようにしてあります(データの欠落を
   遅寄りと数えない)。このファイルは将来 再契約したときのために置いてあります。

■ なぜ必要か (2026-08-18 に判明)

  レポートの5分足自動更新が **yfinance** を使っていた。yfinance は
  09:00-09:05 のバーを持たない(§18.38)ので、その日は全銘柄が『遅寄り』
  扱いになり **J(09:00確認)が1件も建たない**。

  実測 (check_open_bar.py / 198銘柄):
      08-10(月) 189/198 (95%)  ← J-Quants で取れていた
      08-17(月) 198/198 (100%) ← 同上
      08-18       4/198  (2%)  ⛔ yfinance で埋めた日
      08-04/05/06/07/12/13/14 も 1〜3%

  レポートの日別カードは **09:00バーがある日しか出ない**。だから7月が8日、
  8月が4日しか無かった。08-13 が「1件」だったのは 09:00 を持つ銘柄が
  ちょうど1つだったから(完全に一致した)。

  ⛔ daily_fetch_minute.run_update は「各pklの **最終時刻より後** のバーだけ
    追記」する設計なので、J-Quants で回し直しても **既に埋まった日の 09:00 は
    埋まらない**。だからこの専用ツールが要る。

■ 何をするか

  指定した日について J-Quants の5分足を取り直し、**既存に無いバーだけ**を
  マージして時刻順に並べ直す。既存バーは書き換えない(値は上書きしない)。

■ 使い方

    python repair_open_bar.py --check                    # 欠けている日を探すだけ
    python repair_open_bar.py --dates 2026-08-18 --dry-run
    python repair_open_bar.py --dates 2026-08-18
    python repair_open_bar.py --days 20                  # 直近20営業日を自動で修復
    python repair_open_bar.py --dates 2026-08-04,2026-08-05 --limit 50

  .env に JQUANTS_API_KEY が要ります(daily_fetch_minute と同じ)。

■ 終わったら

    python check_open_bar.py --days 10     # 09:00 が 90%超になったか
    .\dailyfast --days 365 --no-serve      # 日別カードにその日が出るか
"""
from __future__ import annotations

import argparse
import datetime as _dt
from collections import Counter
from pathlib import Path

ap = argparse.ArgumentParser(description="欠けた09:00バーをJ-Quantsで埋め直す")
ap.add_argument("--dates", type=str, default="", help="対象日 カンマ区切り")
ap.add_argument("--days", type=int, default=0,
                help="直近N営業日を調べ、09:00が半分未満の日を自動で修復")
ap.add_argument("--limit", type=int, default=0, help="銘柄数の上限(0=全部)")
ap.add_argument("--check", action="store_true", help="欠けている日を探すだけ")
ap.add_argument("--dry-run", action="store_true", help="書き込まず件数だけ")
ap.add_argument("--thresh", type=float, default=0.5,
                help="09:00バーの割合がこれ未満の日を『欠損』とする")
args = ap.parse_args()

try:
    import pandas as pd
    import daytrade_data as _dd
    from daily_fetch_minute import get_client
    from daytrade_data import normalize_minute_df
except Exception as e:
    raise SystemExit(f"[error] 依存モジュールを読めません: {e}")

_DIR = Path(_dd.DATA_DIR)
if not _DIR.exists():
    raise SystemExit(f"[error] {_DIR} がありません")
_pkls = sorted(_DIR.glob("*.pkl"))
if args.limit:
    _pkls = _pkls[:args.limit]
print(f"[5分足] {_DIR}\n[5分足] {len(_pkls):,}銘柄")


def _first_hm_by_day(df) -> dict:
    """日付 -> その日の先頭バー時刻(HH:MM)。"""
    _out = {}
    try:
        for _d, _sub in _dd.split_by_day(df).items():
            if _sub is not None and len(_sub):
                _out[str(_d)[:10]] = pd.Timestamp(_sub.index[0]).strftime("%H:%M")
    except Exception:
        pass
    return _out


# ── 欠けている日を探す ───────────────────────────────────────────────
_scan_n = min(len(_pkls), 200)          # 判定は先頭200銘柄で足りる
_cnt: dict = {}
for _p in _pkls[:_scan_n]:
    try:
        _df = pd.read_pickle(_p)
    except Exception:
        continue
    for _d, _hm in _first_hm_by_day(_df).items():
        _cnt.setdefault(_d, Counter())[_hm] += 1

_all_days = sorted(_cnt)
_look = _all_days[-args.days:] if args.days else _all_days
_bad = [_d for _d in _look
        if _cnt[_d].get("09:00", 0) < sum(_cnt[_d].values()) * args.thresh]

print(f"[判定] 先頭{_scan_n}銘柄で {len(_look)}日を確認 → "
      f"09:00が{args.thresh * 100:.0f}%未満の日 **{len(_bad)}日**")
for _d in _bad[-20:]:
    _c = _cnt[_d]
    print(f"    {_d}  09:00 {_c.get('09:00', 0):>4}/{sum(_c.values()):>4}"
          f"   {' / '.join(f'{h} {n}' for h, n in _c.most_common(3))}")

if args.dates:
    _targets = [d.strip() for d in args.dates.split(",") if d.strip()]
    print(f"[対象] --dates 指定: {', '.join(_targets)}")
else:
    _targets = _bad
    if not args.days:
        print("[対象] --dates も --days も無いので **調査のみ**で終わります。"
              "\n       修復するなら --dates か --days を付けてください")
        raise SystemExit(0)

if args.check or not _targets:
    raise SystemExit(0)

# ── J-Quants で取り直してマージ ─────────────────────────────────────
try:
    _cli = get_client()
except SystemExit:
    raise
except Exception as e:
    raise SystemExit(f"[error] J-Quants に接続できません: {e}\n"
                     f"  .env の JQUANTS_API_KEY を確認してください")

_from = min(_targets).replace("-", "")
_to = max(_targets).replace("-", "")
_tset = set(_targets)
print(f"\n[取得] J-Quants {_from}〜{_to} / {len(_pkls):,}銘柄"
      + ("  (dry-run)" if args.dry_run else ""))

_n_sym = _n_bar = _n_fail = 0
for _i, _p in enumerate(_pkls, 1):
    if _i % 200 == 0:
        print(f"  … {_i:,}/{len(_pkls):,} (修復 {_n_sym:,}銘柄 / "
              f"+{_n_bar:,}バー)", flush=True)
    _code = _p.stem
    try:
        _cur = pd.read_pickle(_p)
    except Exception:
        continue
    try:
        _raw = _cli.get_eq_bars_5minute(code=_code, from_yyyymmdd=_from,
                                        to_yyyymmdd=_to)
    except Exception:
        _n_fail += 1
        continue
    if _raw is None or getattr(_raw, "empty", True):
        _n_fail += 1
        continue
    try:
        _new = normalize_minute_df(_raw)
    except Exception:
        _n_fail += 1
        continue
    # 対象日だけに絞る
    _new = _new[[str(_t)[:10] in _tset for _t in _new.index]]
    if _new.empty:
        continue
    # ⛔ **既存バーは書き換えない**。無い時刻だけ足す(値の上書きは事故のもと)。
    _add = _new[~_new.index.isin(_cur.index)]
    if _add.empty:
        continue
    _n_sym += 1
    _n_bar += len(_add)
    if args.dry_run:
        continue
    try:
        _mg = pd.concat([_cur, _add]).sort_index()
        _mg = _mg[~_mg.index.duplicated(keep="first")]
        _mg.to_pickle(_p)
    except Exception as e:
        print(f"  ⚠ {_code}: 書き込み失敗 {e}")
        _n_fail += 1

print(f"\n{'=' * 70}")
print(f"■ {'(dry-run) ' if args.dry_run else ''}修復 {_n_sym:,}銘柄 / "
      f"+{_n_bar:,}バー"
      + (f" / 取得失敗 {_n_fail:,}銘柄" if _n_fail else ""))
print(f"{'=' * 70}")
print("  次:  python check_open_bar.py --days 10     ← 09:00 が 90%超になったか")
print("       .\\dailyfast --days 365 --no-serve      ← 日別カードにその日が出るか")
