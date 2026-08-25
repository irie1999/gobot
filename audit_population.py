"""audit_population.py — ライブが建てた銘柄日が、バックテストの母集団にあるか数える

⛔ 何を測るツールか (2026-08-25)
────────────────────────────────────────────────────────────────────────
J タブの取引は **自前でシグナルを探していない**。lss(逆指値)バックテストが
作った (銘柄, 日) の一覧を借りて、「もし J のやり方で建てたら」を計算し直して
いるだけ (`eh_trades.py:275` = `for t in trades + nofills`)。

そして lss の記録の作られ方は3通りあり、**2つが不安定**:

  ① 逆指値に触れて約定した  → **触れた日**で記録            ✅ 固定
  ② まだ触れていない        → **実行時の最終バー**の日付で記録 ⛔ 回すたびに動く
  ③ 3営業日 触れず期限切れ  → **記録なし**(破棄)             ⛔ 永久に消える

② は `backtest_limit_entry.py` の `entry_dt=df.index[-1]` (発注中の一律付与)、
③ は `if i > po["expire_idx"]: continue  # 期限切れ → 破棄`。

実例 (3382 セブン＆アイ / 2026-08-24):
  前日終値 2,030.5 / 始値 2,049.5 (+93.6bp) / **安値 2,031.5** = 逆指値に1円届かず
  → 08-24 の夕方に回すと「発注中(日付=08-24)」で母集団に入り J の取引ができた
  → 08-25 の夕方に回すと日付が 08-25 に移り、**(3382, 08-24) が消えた**
  ライブは 09:00 の始値で判定するので **普通に約定している**(実損益 +60円)。

母集団に入る条件は `安値 ≤ 前日終値` なので、ギャップ g に対して
「始値から g だけ下げること」を要求する。つまり **抜けるのは
『その日ほとんど下げなかった銘柄日』** で、その日は利確(1.0ATR)に届きようが
ないのに損切り(0.5ATR)には当たりうる = 期待値はマイナス寄りのはず。

★ ただしこれは**理屈**。本当にマイナス寄りかを実データで確かめるのがこのツール。

────────────────────────────────────────────────────────────────────────
読むファイル (すべて既存。発注経路には一切触らない):

  k_paper_<yyyymmdd>.csv  ライブが 09:00 に読んだ全候補と判定
                          (pass_gap / guard_ng / ordered / gap_bp / lots_k …)
  lss_trades_K.csv        バックテスト(J 実装版)の取引 = 母集団
  fills_<yyyymmdd>.csv    実約定の損益 (あれば。`.\\fills --save` が出す)

使い方:
  python audit_population.py                    # k_paper_*.csv 全部
  python audit_population.py --date 20260824    # 1日だけ
  python audit_population.py --estimate         # 抜けた銘柄日の損益を日足で推定
  python audit_population.py --trades lss_trades_hvar_K.csv   # 365日窓と比較

⛔ 比較の分母は **ライブが実際に読んだ銘柄(= k_paper の中身)** に限る。
   ライブは流動性上位50件しか読まないので、それ以外を分母に入れると
   「バックテストにしか無い」が大量に出て意味がなくなる。
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob as _glob
import re
from pathlib import Path

ap = argparse.ArgumentParser(
    description="ライブが建てた銘柄日がバックテストの母集団にあるか数える")
ap.add_argument("--date", type=str, default="",
                help="対象日 yyyyMMdd。省略で k_paper_*.csv を全部")
ap.add_argument("--trades", type=str, default="lss_trades_K.csv",
                help="バックテストの母集団CSV (既定 lss_trades_K.csv)")
ap.add_argument("--paper-glob", type=str, default="k_paper_*.csv",
                help="ライブ判定CSVのグロブ")
ap.add_argument("--estimate", action="store_true",
                help="抜けた銘柄日の損益を日足(高値/安値/終値)で推定する。"
                     "yfinance キャッシュを読むので少し時間がかかる")
ap.add_argument("--all-ordered", action="store_true",
                help="合格(pass_gap)ではなく **実発注(ordered=1)** だけを分子にする")
ap.add_argument("--since", type=str, default="",
                help="この日以降だけを見る (yyyy-MM-dd)。⛔ 方式が変わった日をまたぐと"
                     "比較にならない。J の実運用は 2026-08-21 から "
                     "(それ以前は H / slip_daily_log.csv の『方式』列で確認できる)")
ap.add_argument("--budget-man", type=float, default=400.0,
                help="レポート側の予算(万円)。予算落ちかどうかの判定に使う (既定400)")
args = ap.parse_args()
_SINCE = str(args.since or "")[:10]


def _n(s) -> str:
    """銘柄コードを正規化 ('3382.T' / '3382' → '3382')。"""
    return re.sub(r"\D", "", str(s or "").split(".")[0]) or str(s or "")


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


def _i(x, d=0) -> int:
    try:
        return int(float(x))
    except Exception:
        return d


# ══════════════════════════════════════════════════════════════════════
#  読み込み
# ══════════════════════════════════════════════════════════════════════
_tp = Path(args.trades)
if not _tp.exists():
    raise SystemExit(f"[error] {_tp} がありません。先に .\\dailyfast --no-serve を流してください")

# 母集団: (日付, 銘柄) -> その日の取引(複数戦略ぶん)
_pop: dict[tuple[str, str], list[dict]] = {}
for r in _csv.DictReader(open(_tp, encoding="utf-8-sig")):
    _d = str(r.get("entry_date") or "")[:10]
    _s = _n(r.get("symbol"))
    if len(_d) == 10 and _s:
        _pop.setdefault((_d, _s), []).append(r)
print(f"[母集団] {_tp.name}: {len(_pop):,} 銘柄日 "
      f"({min(k[0] for k in _pop) if _pop else '-'} 〜 "
      f"{max(k[0] for k in _pop) if _pop else '-'})")

# ライブ判定
if args.date:
    _papers = [Path(f"k_paper_{re.sub(r'[^0-9]', '', args.date)}.csv")]
else:
    _papers = sorted(Path(p) for p in _glob.glob(args.paper_glob))
_papers = [p for p in _papers if p.exists()]
if not _papers:
    raise SystemExit(f"[error] {args.paper_glob} が見つかりません")

# 実約定 (あれば)
_fills: dict[tuple[str, str], dict] = {}
for p in sorted(Path(x) for x in _glob.glob("fills_*.csv")):
    _d8 = re.sub(r"\D", "", p.stem)
    if len(_d8) != 8:
        continue
    _d = f"{_d8[:4]}-{_d8[4:6]}-{_d8[6:]}"
    try:
        for r in _csv.DictReader(open(p, encoding="utf-8-sig")):
            _fills[(_d, _n(r.get("symbol")))] = r
    except Exception:
        pass
if _fills:
    print(f"[実約定] fills_*.csv: {len(_fills):,} 銘柄日")

_daily_cache: dict[str, object] = {}


def _daily_bar(sym: str, day: str):
    """日足の1本を返す (open/high/low/close)。取れなければ None。"""
    if sym not in _daily_cache:
        try:
            import backtest_limit_entry as _b
            _daily_cache[sym] = _b.fetch(f"{sym}.T")
        except Exception:
            _daily_cache[sym] = None
    df = _daily_cache.get(sym)
    if df is None:
        return None
    try:
        import pandas as _pd
        ts = _pd.Timestamp(day)
        if ts not in df.index:
            return None
        row = df.loc[ts]
        return (float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]))
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
#  突合
# ══════════════════════════════════════════════════════════════════════
_LBL = "実発注(ordered=1)" if args.all_ordered else "合格(pass_gap=1)"
_SHORT = "発注" if args.all_ordered else "合格"
_POP_DAYS = {k[0] for k in _pop}
print(f"\n{'=' * 78}\n"
      f"■ ライブが {_LBL} と判定した銘柄日が、バックテストの母集団にあるか\n"
      f"{'=' * 78}")
print("  ⛔ 分母は **ライブが実際に読んだ銘柄** だけ(k_paper の中身 = 流動性上位50件)。\n"
      "     ライブが読んでいない銘柄を混ぜると比較にならない。\n"
      "  ⚠ その日が母集団に1件も無い日は『窓外?』= レポートの表示窓の外か、\n"
      "     その日を含む実行をまだ流していない。集計から外す。\n")

if _SINCE:
    print(f"  ★ {_SINCE} 以降だけを見ます（--since）\n")
else:
    print("  ⛔ **方式が変わった日をまたいでいないか確認すること**。J の実運用は\n"
          "     2026-08-21 から。それ以前は H(前夜指値)で、母集団も株数も別物。\n"
          "     → `--since 2026-08-21` を付けて測り直すこと。\n")

_hdr = (f"  {'日付':<12}{'読んだ':>7}{_SHORT:>6}{'BT件数':>7}{'BT投入':>9}"
        f"{'母集団に有':>11}{'⛔抜け':>8}{'抜け率':>8}   {'逆':>5}")
print(_hdr)
print("  " + "-" * (len(_hdr) - 2))

_miss_all: list[dict] = []
_hit_all: list[dict] = []
_tot_read = _tot_pass = _tot_hit = _tot_rev = 0

for p in _papers:
    try:
        _rows = list(_csv.DictReader(open(p, encoding="utf-8-sig")))
    except Exception as e:
        print(f"  ⚠ {p.name} を読めません: {e}")
        continue
    if not _rows:
        continue
    _day = str(_rows[0].get("date") or "")[:10]
    if len(_day) != 10:
        _d8 = re.sub(r"\D", "", p.stem)
        _day = f"{_d8[:4]}-{_d8[4:6]}-{_d8[6:]}" if len(_d8) == 8 else ""
    if not _day:
        continue

    _read = {_n(r.get("symbol")) for r in _rows if _n(r.get("symbol"))}
    _pass = []
    for r in _rows:
        _s = _n(r.get("symbol"))
        if not _s:
            continue
        if args.all_ordered:
            ok = _i(r.get("ordered")) == 1
        else:
            ok = (_i(r.get("pass_gap")) == 1 and _i(r.get("guard_ng")) == 0
                  and _i(r.get("stale_open")) == 0)
        if ok:
            _pass.append(r)

    if _SINCE and _day < _SINCE:
        continue

    # その日の母集団の件数と投入額。予算に余裕があれば「予算落ち」ではないと言える。
    _bt_rows = [t for k, v in _pop.items() if k[0] == _day for t in v]
    _bt_yen = sum(_f(t.get("entry_p")) * _f(t.get("qty")) for t in _bt_rows)
    _room = args.budget_man * 1e4 - _bt_yen

    # ⚠ その日が母集団に1件も無い = レポートの表示窓の外(または未実行)。
    #   「全部抜けた」と数えると抜け率が意味を失うので、集計から外す。
    if _day not in _POP_DAYS:
        print(f"  {_day:<12}{len(_read):>7}{len(_pass):>6}{0:>7}{'—':>9}"
              f"{'—':>11}{'—':>8}{'⚠ 窓外?':>9}   {'—':>5}")
        continue

    _hit, _miss = [], []
    for r in _pass:
        _s = _n(r.get("symbol"))
        (_hit if (_day, _s) in _pop else _miss).append(r)

    # 逆向き: ライブが読んだ銘柄のうち、BT には取引があるが ライブは合格させなかった
    _passed_syms = {_n(r.get("symbol")) for r in _pass}
    _rev = [s for s in _read
            if (_day, s) in _pop and s not in _passed_syms]

    for r in _miss:
        r["_day"] = _day
        _miss_all.append(r)
    for r in _hit:
        r["_day"] = _day
        _hit_all.append(r)

    _tot_read += len(_read)
    _tot_pass += len(_pass)
    _tot_hit += len(_hit)
    _tot_rev += len(_rev)
    _rate = (len(_miss) / len(_pass) * 100) if _pass else 0.0
    # ★ 予算に1単元ぶんの余裕があれば、その日の抜けは「予算落ち」では説明できない
    #   = 母集団に本当に無い。余裕が無い日だけ ⚠ を付ける。
    _tight = "⚠" if _room < 60 * 1e4 else " "
    print(f"  {_day:<12}{len(_read):>7}{len(_pass):>6}{len(_bt_rows):>7}"
          f"{_bt_yen / 1e4:>8.0f}万{len(_hit):>11}"
          f"{len(_miss):>8}{_rate:>7.0f}%   {len(_rev):>4}{_tight}")

print("  " + "-" * (len(_hdr) - 2))
_miss_n = _tot_pass - _tot_hit
_rate = (_miss_n / _tot_pass * 100) if _tot_pass else 0.0
print(f"  {'合計':<12}{_tot_read:>7}{_tot_pass:>6}{'':>7}{'':>9}{_tot_hit:>11}"
      f"{_miss_n:>8}{_rate:>7.0f}%   {_tot_rev:>5}")
print(f"\n  ★ 「BT投入」が予算({args.budget_man:g}万)より 60万以上 少ない日は、"
      f"予算にまだ1単元ぶんの余裕がある\n"
      f"     = その日の抜けは **予算落ちでは説明できない**(母集団に本当に無い)。"
      f"⚠ が付いた日だけ予算落ちが混ざりうる。")

if not _tot_pass:
    raise SystemExit("\n[error] ライブの合格が0件です。k_paper の中身を確認してください")

# ══════════════════════════════════════════════════════════════════════
#  抜けた銘柄日の中身
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 78}\n"
      f"■ ⛔ 抜けた銘柄日 {len(_miss_all)}件 — ライブは建てたのにバックテストに無い\n"
      f"{'=' * 78}")

if not _miss_all:
    print("  ✅ 抜けはありません。母集団はライブと一致しています。")
else:
    print(f"  {'日付':<12}{'銘柄':>6}{'ギャップ':>9}{'発注':>5}{'株数':>6}"
          f"{'実損益':>10}{'推定損益':>10}  {'推定の内訳'}")
    _real_sum = _real_n = 0
    _est_sum = _est_n = _est_amb = _est_zero = 0
    _est_vals: list[float] = []
    for r in sorted(_miss_all, key=lambda x: (x["_day"], _n(x.get("symbol")))):
        _s = _n(r.get("symbol"))
        _day = r["_day"]
        _ordered = _i(r.get("ordered"))
        _qty = _i(r.get("lots_k")) * 100
        _fr = _fills.get((_day, _s))
        _real = _f(_fr.get("pnl")) if _fr else None
        if _real is not None:
            _real_sum += _real
            _real_n += 1
        _est_txt, _est = "", None
        if args.estimate and _qty <= 0:
            # ⛔ 株数0(その日の予算枠で1単元も建たなかった)を損益0として平均に
            #   混ぜると、抜けの期待値がゼロ方向に薄まる。件数だけ数えて除外する。
            _est_txt = "株数0(推定不能)"
            _est_zero += 1
        elif args.estimate:
            _bar = _daily_bar(_s, _day)
            if _bar:
                _o, _hi, _lo, _cl = _bar
                _stop = _f(r.get("stop_k"))
                _tgt = _f(r.get("target_k"))
                _op = _f(r.get("open_p")) or _o
                _hs = _stop > 0 and _hi >= _stop
                _ht = _tgt > 0 and _lo <= _tgt
                if _hs and _ht:
                    _est_txt = "⚠判定不能(日足では順序不明)"
                    _est_amb += 1
                elif _hs:
                    _est, _est_txt = (_op - _stop) * _qty, "損切り"
                elif _ht:
                    _est, _est_txt = (_op - _tgt) * _qty, "利確"
                else:
                    _est, _est_txt = (_op - _cl) * _qty, "引け"
                if _est is not None:
                    _est_sum += _est
                    _est_n += 1
                    _est_vals.append(_est)
            else:
                _est_txt = "日足なし"
        _rs = f"{_real:+,.0f}" if _real is not None else "—"
        _es = f"{_est:+,.0f}" if _est is not None else "—"
        _os = "✅" if _ordered else "—"
        print(f"  {_day:<12}{_s:>6}{_f(r.get('gap_bp')):>+8.1f}bp"
              f"{_os:>5}{_qty:>6}{_rs:>10}{_es:>10}  {_est_txt}")

    print()
    if _real_n:
        print(f"  ★ 実約定できた {_real_n}件 の合計: **{_real_sum:+,.0f}円** "
              f"(1件あたり {_real_sum / _real_n:+,.0f}円)")
    if args.estimate and _est_n:
        _ev = sorted((abs(x), x) for x in _est_vals)
        _top = _ev[-1][1] if _ev else 0
        print(f"  ★ 日足で推定できた {_est_n}件 の合計: **{_est_sum:+,.0f}円** "
              f"(1件あたり {_est_sum / _est_n:+,.0f}円)"
              + (f" / 判定不能 {_est_amb}件" if _est_amb else "")
              + (f" / 株数0で除外 {_est_zero}件" if _est_zero else ""))
        if _est_n > 1 and abs(_top) > abs(_est_sum) * 0.4:
            print(f"     ⛔ **最大の1件({_top:+,.0f}円)が合計の "
                  f"{abs(_top) / max(1, abs(_est_sum)) * 100:.0f}% を占めています。**"
                  f" 除くと 1件あたり {(_est_sum - _top) / max(1, _est_n - 1):+,.0f}円。"
                  f"この件数では平均に意味がありません")
    if not args.estimate:
        print("  （--estimate を付けると、発注しなかったぶんも日足で推定します）")

# ══════════════════════════════════════════════════════════════════════
#  比較: 母集団に残ったほうの損益
# ══════════════════════════════════════════════════════════════════════
_hit_real = [(_f(_fills[(r['_day'], _n(r.get('symbol')))].get("pnl")), r)
             for r in _hit_all
             if (r['_day'], _n(r.get('symbol'))) in _fills]
if _hit_real:
    _hs = sum(x for x, _ in _hit_real)
    print(f"\n{'=' * 78}\n■ 比較 — 実約定した取引を「母集団にある / 無い」で分けると\n{'=' * 78}")
    _ms = sum(_f(_fills[(r['_day'], _n(r.get('symbol')))].get("pnl"))
              for r in _miss_all
              if (r['_day'], _n(r.get('symbol'))) in _fills)
    _mn = sum(1 for r in _miss_all
              if (r['_day'], _n(r.get('symbol'))) in _fills)
    print(f"  {'区分':<26}{'件数':>6}{'実損益':>12}{'1件あたり':>12}")
    print(f"  {'母集団にある(採点される)':<26}{len(_hit_real):>6}"
          f"{_hs:>+12,.0f}{_hs / max(1, len(_hit_real)):>+12,.0f}")
    print(f"  {'⛔ 母集団に無い(消える)':<26}{_mn:>6}"
          f"{_ms:>+12,.0f}{_ms / max(1, _mn):>+12,.0f}")
    print(f"  {'合計(= 実際の成績)':<26}{len(_hit_real) + _mn:>6}"
          f"{_hs + _ms:>+12,.0f}"
          f"{(_hs + _ms) / max(1, len(_hit_real) + _mn):>+12,.0f}")
    if _mn and len(_hit_real):
        _d = _ms / _mn - _hs / len(_hit_real)
        print(f"\n  → 抜けたほうが 1件あたり **{_d:+,.0f}円** "
              f"{'悪い(= レポートは過大評価)' if _d < 0 else '良い(= 理屈と逆)'}")

# ══════════════════════════════════════════════════════════════════════
#  読み方
# ══════════════════════════════════════════════════════════════════════
print(f"""
{'=' * 78}
■ 読み方
{'=' * 78}
  抜け率 …… ライブが建てた銘柄日のうち、バックテストが採点していない割合。
             0% なら問題なし。大きいほどレポートの成績は「別の集合」の話になる。

  ⛔ **件数が少ないうちは何も言えません**。1件の外れ値で符号が反転します。
     10営業日ぶん貯まってから読むこと(§18.49 と同じ作法)。

  ⚠ 「逆(BTのみ)」は **ライブが読んだ銘柄なのに合格させなかった**もの。
     ライブとバックテストで始値やギャップ判定が食い違っていれば ここに出る。
     0 でないなら、その銘柄の k_paper の gap_bp と lss_trades_K の entry_p を
     直接見比べること。

  ★ 推定損益は **日足の高値/安値/終値** から出した近似です。
     損切りと利確の両方に触れた日は順序が分からないので「判定不能」。
     実約定がある行は そちらが正です。
""")
