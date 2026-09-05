"""audit_score_cache.py — 凍結BTキャッシュに『後から書かれた(=先読み込みの)値』が
混ざっていないか監査する。必要なら汚染分だけ削除する。

なぜ必要か (2026-08-08 判明)
-----------------------------
`signal_score_cache_lss.json` は (銘柄::戦略::シグナル日) → BTスコア を **書き込み1回きり**で
凍結する。書き込みは `run_signals_holdout_all.py:1800-1803`:

    if _skey not in _score_cache:
        _real_bt = _sig.get("rec_score", 0)          # ← **その実行時点**のBTスコア
        _score_cache[_skey] = {"bt_score": _real_bt, "first_seen": str(TODAY)}

BTスコアは常に **実行日までの365日** から計算される
(`check_signals_stop.py:334` `today = datetime.now(JST).date()`)。したがって:

  * シグナル当日〜翌営業日に走らせた   → その取引はまだ決済していない → **正しい凍結**
  * 過去日を後から再生成して走らせた   → その取引の決済結果を織り込んだBTを
                                          **正しい値のフリをして永久凍結** → **先読み**

そして損益タブは **凍結 → as-of → 今日のスコア** の順に引く
(`nikkei_analysis.py:7722`)。凍結が見つかればそこで確定するので、
**汚染された凍結値は、正しい as-of 計算を上書きする。** 一番たちが悪い。

判定
----
lss はシグナル日の翌営業日に約定・同日決済する。よって

    first_seen <= signal_date の翌営業日   → OK (その取引はまだ決済していない)
    first_seen >= signal_date の翌々営業日 → **汚染** (決済結果を織り込んだBTが凍結された)

※ 祝日は考慮しない(土日のみ)。祝日を挟むと1営業日ぶん厳しめに出る = 安全側。

使い方
------
  python audit_score_cache.py                       # lss キャッシュを監査
  python audit_score_cache.py --json signal_score_cache.json
  python audit_score_cache.py --list                # 汚染エントリを一覧
  python audit_score_cache.py --purge               # 汚染分だけ削除(バックアップを取る)
  python audit_score_cache.py --purge --dry-run     # 削除せず件数だけ

削除すると、そのシグナルは損益タブで **as-of BT(正しい再計算)** に落ちる。
削除は損しない: as-of は当時決済済みのトレードだけで計算するので先読みしない。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

# 東証の大引け(2024/11以降 15:30)。翌営業日のこれ以降に凍結された = その日の
# 約定・同日決済がBTに入っている = 汚染。
CLOSE_TIME = time(15, 30)

ap = argparse.ArgumentParser(description="凍結BTキャッシュの先読み監査")
ap.add_argument("--json", type=str, default="signal_score_cache_lss.json")
ap.add_argument("--list", action="store_true", help="汚染エントリを一覧表示")
ap.add_argument("--limit", type=int, default=40, help="--list の表示件数")
ap.add_argument("--purge", action="store_true", help="汚染エントリを削除する")
ap.add_argument("--dry-run", action="store_true", help="--purge しても書き込まない")
args = ap.parse_args()

p = Path(args.json)
if not p.exists():
    sys.exit(f"[error] {p} がありません")

cache: dict = json.loads(p.read_text(encoding="utf-8"))
print(f"[入力] {p}  {len(cache):,}件")


def _d(s: str) -> date | None:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _next_bday(d: date) -> date:
    """翌営業日(土日のみ考慮。祝日は無視=安全側に厳しめ)。"""
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


rows, bad, no_meta = [], [], []
for k, v in cache.items():
    parts = k.split("::")
    if len(parts) != 3:
        continue
    sym, strat, sig_s = parts
    sig_d, seen_d = _d(sig_s), _d(v.get("first_seen", ""))
    bt = v.get("bt_score", 0)
    if sig_d is None or seen_d is None:
        no_meta.append((k, bt))
        continue
    lag_days = (seen_d - sig_d).days
    # 許容 = シグナル日 と その翌営業日 まで
    ok = seen_d <= _next_bday(sig_d)
    # 隙間ふさぎ: 翌営業日でも **引け後(15:30 JST以降)** に凍結されていれば、
    # その日に約定・同日決済した取引がBTに入っている = 汚染。日付だけでは判別できず、
    # first_seen_ts (2026-08-08 以降の書き込みにのみ存在) があるときだけ判定できる。
    late_close = False
    if ok and seen_d == _next_bday(sig_d):
        _ts = str(v.get("first_seen_ts", "") or "")
        if len(_ts) >= 16:
            try:
                if datetime.fromisoformat(_ts).time() >= CLOSE_TIME:
                    ok, late_close = False, True
            except Exception:
                pass
    rows.append({"key": k, "sym": sym, "strat": strat, "sig": sig_d,
                 "seen": seen_d, "lag": lag_days, "bt": bt, "ok": ok,
                 "late_close": late_close,
                 "ts": str(v.get("first_seen_ts", "") or "")})
    if not ok:
        bad.append(rows[-1])

if not rows and not no_meta:
    sys.exit("[error] 解析できるエントリがありません (キー形式 sym::strat::date を想定)")

n_ok = sum(1 for r in rows if r["ok"])
print(f"\n{'=' * 78}")
print("■ 凍結タイミングの内訳")
print(f"{'=' * 78}")
print(f"  {'区分':<34}{'件数':>9}{'割合':>8}")
print("  " + "-" * 51)
_tot = len(rows) + len(no_meta)
_n_late = sum(1 for r in bad if r.get("late_close"))
for label, n in [("✅ 正しい凍結 (翌営業日の引け前まで)", n_ok),
                 ("⛔ 汚染 (翌々営業日以降に凍結)", len(bad) - _n_late),
                 ("⛔ 汚染 (翌営業日だが引け後に凍結)", _n_late),
                 ("? first_seen/日付が読めない", len(no_meta))]:
    print(f"  {label:<34}{n:>9,}{(n / _tot * 100 if _tot else 0):>7.1f}%")
_n_ts = sum(1 for r in rows if r.get("ts"))
if _n_ts < len(rows):
    print(f"  ※ {len(rows) - _n_ts:,}件は first_seen_ts(時刻)が無い古い書き込み。"
          f"『翌営業日の引け後』の判定ができません(日付だけで正常扱い)。")
print("  " + "-" * 51)
print(f"  {'合計':<34}{_tot:>9,}")

if bad:
    lags = Counter(min(r["lag"], 999) for r in bad)
    print(f"\n  汚染の遅れ日数(暦日) 上位:")
    for lag, n in sorted(lags.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {lag:>4}日後に凍結: {n:>6,}件")
    _bts = [r["bt"] for r in bad]
    _oks = [r["bt"] for r in rows if r["ok"]]
    print(f"\n  平均BT — 汚染 {sum(_bts) / len(_bts):.1f} / "
          f"正しい凍結 {(sum(_oks) / len(_oks) if _oks else 0):.1f}")
    print(f"  ※ 汚染側が高ければ『勝った取引ほど高いBTで凍結された』"
          f"= 先読みが効いている兆候")

if args.list and bad:
    print(f"\n{'=' * 78}")
    print(f"■ 汚染エントリ (先頭{min(args.limit, len(bad))}件 / 遅れ降順)")
    print(f"{'=' * 78}")
    print(f"  {'銘柄':<10}{'戦略':<9}{'シグナル日':<13}{'凍結日':<13}{'遅れ':>6}{'BT':>6}")
    print("  " + "-" * 59)
    for r in sorted(bad, key=lambda x: -x["lag"])[:args.limit]:
        print(f"  {r['sym']:<10}{r['strat']:<9}{str(r['sig']):<13}"
              f"{str(r['seen']):<13}{r['lag']:>5}日{r['bt']:>6}")

# ── 削除 ────────────────────────────────────────────────────────────
if args.purge:
    if not bad and not no_meta:
        print("\n[purge] 削除対象なし。キャッシュはクリーンです")
    else:
        _drop = {r["key"] for r in bad} | {k for k, _ in no_meta}
        print(f"\n[purge] 削除対象 {len(_drop):,}件 "
              f"(汚染 {len(bad):,} + メタ欠損 {len(no_meta):,})")
        if args.dry_run:
            print("[purge] --dry-run のため書き込みません")
        else:
            _bak = p.with_suffix(p.suffix + ".bak")
            shutil.copy2(p, _bak)
            for k in _drop:
                cache.pop(k, None)
            p.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            print(f"[purge] バックアップ: {_bak}")
            print(f"[purge] 保存: {p}  残り {len(cache):,}件")
            print("[purge] 削除したシグナルは損益タブで as-of BT(正しい再計算)に落ちます。"
                  "次回の .\\daily で [BT出所] の as-of 件数が増えるはずです")

print(f"\n{'─' * 78}")
print("読み方: 汚染 = 『その取引が決済した後に計算されたBT』を発生時スコアとして")
print("        凍結してしまったもの。損益タブは凍結を as-of より優先して引くので")
print("        (nikkei_analysis.py:7722)、汚染分は正しい再計算を上書きしてしまう。")
print("        --purge で消せば as-of に落ちて先読みが消える(消して損はしない)。")
print("★ 過去日を --date で再生成すると新たに汚染が書かれる。再生成のあとは必ず")
print("  このツールを流すこと。")
