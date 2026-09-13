r"""check_signal_vs_test.py — 朝のシグナル/板/発注 と バックテストの明細 を突き合わせる。

■ 何を確かめるか (2026-08-18 ユーザー依頼「昨日のシグナル通りにテストが
  出ているかも確認したい」)

  朝の J は4段階で絞る。どこかで食い違えば、レポートの数字は実運用と別物になる:

    ① 候補     k_signals_<日付>.csv     …前夜のシグナル(--collect の出力)
    ② 読んだ   k_paper_<日付>.csv       …09:00 に板を読んだ50件(watch50)
    ③ 合格     同上 pass_gap=1          …始値が前日終値 +50bp 以上
    ④ 発注     同上 ordered=1           …実際に注文を出した

  バックテスト側は lss_trades_K.csv(J) / _H.csv(H) / lss_trades.csv(現行lss)。
  **①〜④とテストが同じ銘柄を指しているか**を銘柄単位で並べる。

  ⛔ .\fills は『実約定 vs テスト』しか見ない。実約定は④のさらに先なので、
    ①〜③のどこでズレたかは見えない。そこを埋めるのがこれ。

■ 使い方

    python check_signal_vs_test.py                    # 今日
    python check_signal_vs_test.py --date 2026-08-18
    python check_signal_vs_test.py --date 2026-08-18 --trades lss_trades_K.csv
    python check_signal_vs_test.py --date 2026-08-18 --all   # 全銘柄を出す

■ 読み方

  「合格したのにテストに無い」が出たら、その銘柄が **バックテストから
   落ちている**。J の損益はそのぶん実運用と食い違う。
  「テストにあるのに候補に無い」なら **母集団がズレている**(選定 / 価格帯 /
   空売り可否 / 5分足の欠損)。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
from pathlib import Path

ap = argparse.ArgumentParser(description="朝のシグナル/板/発注 と テストを突合")
ap.add_argument("--date", type=str, default="", help="対象日 (既定=今日)")
ap.add_argument("--trades", type=str, default="",
                help="テストの明細CSV。既定は lss_trades_K.csv → _H.csv → "
                     "lss_trades.csv の順に探す")
ap.add_argument("--all", action="store_true", help="合格以外の銘柄も全部出す")
ap.add_argument("--show", type=int, default=40, help="各セクションの表示上限")
args = ap.parse_args()

_D = (args.date or f"{_dt.date.today()}")[:10]
_DIG = _D.replace("-", "")
_BASE = Path(__file__).resolve().parent


def _sym(v) -> str:
    return str(v or "").upper().removesuffix(".T").split(".")[0]


def _rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    try:
        return list(_csv.DictReader(open(p, encoding="utf-8-sig")))
    except Exception as e:
        print(f"  ⚠ {p.name} を読めません: {e}")
        return []


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


# ── ① 候補 ───────────────────────────────────────────────────────────
_sig_p = _BASE / f"k_signals_{_DIG}.csv"
_sig = _rows(_sig_p)
_cand: dict = {}
for r in _sig:
    _s = _sym(r.get("symbol"))
    if not _s:
        continue
    _c = _cand.setdefault(_s, {"name": r.get("name", ""), "strats": [],
                               "liq": 0.0, "atr": 0.0})
    _st = str(r.get("strategy") or "").strip()
    if _st and _st not in _c["strats"]:
        _c["strats"].append(_st)
    _c["liq"] = max(_c["liq"], _f(r.get("liquidity")))
    _c["atr"] = _c["atr"] or _f(r.get("atr"))

# ── ②③④ 板・合格・発注 ─────────────────────────────────────────────
_brd_p = _BASE / f"k_paper_{_DIG}.csv"
if not _brd_p.exists():
    _arc = _BASE / "k_morning" / f"{_D}_board.csv"
    if _arc.exists():
        _brd_p = _arc
        print(f"[info] k_paper が無いのでアーカイブを使います: {_arc}")
_brd = {_sym(r.get("symbol")): r for r in _rows(_brd_p) if _sym(r.get("symbol"))}
_read = set(_brd)
_pass = {s for s, r in _brd.items() if str(r.get("pass_gap")) == "1"}
_ordered = {s for s, r in _brd.items() if str(r.get("ordered")) == "1"}

# ── テストの明細 ─────────────────────────────────────────────────────
_cands_f = ([args.trades] if args.trades else
            ["lss_trades_K.csv", "lss_trades_H.csv", "lss_trades.csv"])
_tp = None
_test: dict = {}
for _c in _cands_f:
    _p = _BASE / _c
    _rs = [r for r in _rows(_p) if str(r.get("entry_date") or "")[:10] == _D]
    if _rs:
        _tp = _p
        for r in _rs:
            _s = _sym(r.get("symbol"))
            if _s:
                _test.setdefault(_s, []).append(r)
        break
    if _p.exists() and _tp is None:
        _tp = _tp or None

print(f"""
{'=' * 78}
■ 朝のシグナル/板/発注 vs テスト — {_D}
{'=' * 78}
  ① 候補(前夜のシグナル)   {_sig_p.name:<28} {len(_cand):>5}銘柄 / {len(_sig):>5}ペア
  ② 09:00 に読んだ         {_brd_p.name:<28} {len(_read):>5}銘柄
  ③ 合格(+50bp以上)        {'':<28} {len(_pass):>5}銘柄
  ④ 実発注                 {'':<28} {len(_ordered):>5}銘柄
  ⑤ テストの明細           {(_tp.name if _tp else '(見つからない)'):<28} {len(_test):>5}銘柄""")

if not _test:
    print(f"""
  ⛔ テストの明細に {_D} の取引がありません。
     ・lss_trades_K.csv が無い → .\\dailyfast を1回流す
     ・あるのに当日が無い     → J がその日を1件も作っていない
       (5分足の09:00バー / 合格ゼロ / watch / 予算 のどれか)
       → python check_open_bar.py --date {_D}
       → .\\dailyfast の [日別ファンネル] を見る""")

# ── A. 合格銘柄がテストにあるか ─────────────────────────────────────
_look = sorted(_cand) if args.all else sorted(_pass)
if _look:
    print(f"\n{'=' * 78}\n■ A. {'全候補' if args.all else '合格銘柄'} が"
          f"テストにあるか\n{'=' * 78}")
    print(f"  {'銘柄':<7}{'名前':<16}{'戦略':<20}{'読':>3}{'合':>3}{'発':>3}"
          f"{'ギャップ':>9}{'始値':>9}{'テスト約定':>11}{'差':>8}  理由")
    _miss = []
    for _s in _look[:args.show]:
        _c = _cand.get(_s, {})
        _b = _brd.get(_s, {})
        _t = _test.get(_s) or []
        _op = _f(_b.get("open_p"))
        _te = _f(_t[0].get("entry_p")) if _t else 0.0
        _dif = (_te - _op) if (_te and _op) else 0.0
        if not _t:
            _miss.append(_s)
        print(f"  {_s:<7}{str(_c.get('name', ''))[:15]:<16}"
              f"{'/'.join(_c.get('strats', []))[:19]:<20}"
              f"{'✓' if _s in _read else '-':>3}"
              f"{'✓' if _s in _pass else '-':>3}"
              f"{'✓' if _s in _ordered else '-':>3}"
              f"{_f(_b.get('gap_bp')):>+8.0f}bp{_op:>9,.1f}"
              + (f"{_te:>11,.1f}{_dif:>+8.1f}  {_t[0].get('reason', '')}"
                 if _t else f"{'—':>11}{'—':>8}  ⛔ **テストに無い**"))
    if len(_look) > args.show:
        print(f"  … 他 {len(_look) - args.show}銘柄 (--show で増やせます)")
    if _miss:
        print(f"\n  ⛔ **合格したのにテストに無い: {len(_miss)}銘柄** "
              f"({', '.join(_miss[:15])}{' …' if len(_miss) > 15 else ''})")
        print( "     → その銘柄はバックテストから落ちています。J の損益は")
        print( "       そのぶん実運用と食い違います。")

# ── B. テストにあるが朝の候補に無い ─────────────────────────────────
_extra = sorted(set(_test) - set(_cand))
if _extra:
    print(f"\n{'=' * 78}\n■ B. テストにあるが **朝の候補に無い**: {len(_extra)}銘柄"
          f"\n{'=' * 78}")
    print( "  → 母集団がズレています(選定 / 価格帯 / 空売り可否 / 5分足)")
    for _s in _extra[:args.show]:
        _t = _test[_s][0]
        print(f"  {_s:<7}{str(_t.get('name', ''))[:15]:<16}"
              f"{str(_t.get('strategy', ''))[:10]:<12}"
              f"約定 {_f(_t.get('entry_p')):>9,.1f}  {_t.get('reason', '')}")
    if len(_extra) > args.show:
        print(f"  … 他 {len(_extra) - args.show}銘柄")

# ── C. 読んだが合格しなかった理由の内訳 ─────────────────────────────
if _brd:
    _late = sum(1 for r in _brd.values() if str(r.get("late")) == "1")
    _grd = sum(1 for r in _brd.values() if str(r.get("guard_ng")) == "1")
    _ng = len(_read) - len(_pass)
    print(f"\n{'=' * 78}\n■ C. 読んだ {len(_read)}銘柄 の内訳\n{'=' * 78}")
    print(f"  合格            {len(_pass):>5}")
    print(f"  不合格          {_ng:>5}   うち ガード超過 {_grd} / 09:00未寄 {_late}")
    print(f"  発注            {len(_ordered):>5}   "
          f"(合格しても予算/株数で0になれば発注しない)")
    _unread = sorted(set(_cand) - _read)
    if _unread:
        print(f"  ⚠ 読まなかった候補 {len(_unread):>3}銘柄 "
              f"(watch50 の外。流動性下位)")
