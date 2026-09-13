r"""verify_open_price.py — 板の始値 = 5分足の始値 か を突合する

⛔ 照会のみ。ネットワークは5分足の取得にしか使わない(発注しない)。

■ なぜ最優先か (2026-08-17)

K/J のバックテストは **5分足の最初のバーの始値** を約定価格として使っている。
一方 実運用は **kabu の板が返す OpeningPrice** を見て判定・発注する。
この2つが一致していることが **バックテストの前提そのもの**で、ズレるなら

    ・ギャップ判定(+50bp)が変わる  → 建てる銘柄が変わる
    ・約定価格が変わる            → 損益が変わる
    ・OCO の基準が変わる          → 損切り/利確の位置が変わる

と、J の全数字が影響を受ける。**実弾に移る前に必ず潰す。**

■ 使い方

    python verify_open_price.py                 # 今日のぶん
    python verify_open_price.py --date 20260817
    python verify_open_price.py --glob "k_paper_*.csv"   # 貯まったぶん全部
    python verify_open_price.py --source yfinance        # 5分足の出所を固定

■ 見るところ

  ① 誤差の分布   … 中央値が 0bp 近辺なら一致している
  ② **判定の反転** … 板で合格→5分足で不合格(誤って建てる) / その逆(取り逃す)
                     ここがゼロなら「板で判定してよい」が確定する
  ③ 先頭バーの時刻 … 09:00 でないと『始値』の意味が違う(§18.32 の罠)

■ ⚠ 5分足の出所に注意

  バックテストが使うのは **J-Quants のローカル5分足**(stock_5min)。
  当日ぶんがまだ取れていない場合 `--source auto` は **yfinance に落ちる**ので、
  どちらで突合したかを必ず表示する。yfinance と J-Quants がそもそも違う
  可能性もあるので、**ローカルが揃ってから測り直す**のが本筋。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
import glob as _glob
import math
from pathlib import Path

ap = argparse.ArgumentParser(
    description="板の始値と5分足の始値を突合する(照会のみ)")
ap.add_argument("--date", type=str, default="", help="yyyyMMdd。既定は今日")
ap.add_argument("--glob", type=str, default="",
                help="複数日をまとめて突合(例 'k_paper_*.csv')")
ap.add_argument("--source", type=str, default="auto",
                choices=["auto", "local", "yfinance"],
                help="5分足の出所。既定 auto(ローカル優先→yfinance)")
ap.add_argument("--gap-bp", type=float, default=50.0,
                help="合格判定の閾値(bp)。反転の集計に使う")
ap.add_argument("--days", type=int, default=0,
                help="5分足を何日ぶん読むか(0=自動)")
ap.add_argument("--show", type=int, default=15,
                help="ズレの大きい順に何件出すか")
args = ap.parse_args()

_files = (sorted(_glob.glob(args.glob)) if args.glob
          else [f"k_paper_{args.date or f'{_dt.date.today():%Y%m%d}'}.csv"])
_files = [f for f in _files if Path(f).exists()]
if not _files:
    raise SystemExit(
        "[error] k_paper_<日付>.csv がありません。\n"
        "  朝に `python k_open_confirm.py --prod --poll` を走らせて作ります。")

_rows: list[dict] = []
for _f in _files:
    for r in _csv.DictReader(open(_f, encoding="utf-8-sig")):
        r["_src_file"] = _f
        _rows.append(r)
if not _rows:
    raise SystemExit("[error] CSV が空です")


def _f2(r: dict, k: str) -> float:
    try:
        return float(r.get(k) or 0)
    except Exception:
        return 0.0


_syms = sorted({str(r.get("symbol") or "") for r in _rows if r.get("symbol")})
_dates = sorted({str(r.get("date") or "") for r in _rows if r.get("date")})
_days = args.days or (
    (_dt.date.today() - _dt.date.fromisoformat(_dates[0])).days + 10
    if _dates else 10)

print(f"""
{'=' * 92}
■ 板の始値 vs 5分足の始値 — {', '.join(Path(f).name for f in _files)}
{'=' * 92}
  板の記録 {len(_rows):,}行 / {len(_syms):,}銘柄 / {len(_dates)}営業日
  5分足を読みます (source={args.source} / {_days}日ぶん)…""", flush=True)

try:
    import daytrade_data as _dd
except Exception as e:
    raise SystemExit(f"[error] daytrade_data を読めません: {e}")

_bat = _dd.load_intraday_batch([f"{s}.T" for s in _syms], days=_days,
                               source=args.source)
# (date, symbol) -> (始値, 先頭バーの時刻)
_open: dict = {}
for _sy, _df in (_bat or {}).items():
    if _df is None or len(_df) == 0:
        continue
    _c = str(_sy).upper().removesuffix(".T").split(".")[0]
    for _d, _dd2 in _dd.split_by_day(_df).items():
        if not len(_dd2):
            continue
        _open[(str(_d)[:10], _c)] = (float(_dd2["open"].iloc[0]),
                                     f"{_dd2.index[0]:%H:%M}")

# ── 突合 ───────────────────────────────────────────────────────────────
_hit: list = []
_miss: list = []
for r in _rows:
    _s = str(r.get("symbol") or "")
    _d = str(r.get("date") or "")
    _bo = _f2(r, "open_p")       # 板の始値
    _pc = _f2(r, "prev_close")
    _got = _open.get((_d, _s))
    if _bo <= 0 or _pc <= 0 or not _got:
        _miss.append((_d, _s, "板の始値なし" if _bo <= 0 else "5分足なし"))
        continue
    _fo, _hm = _got
    if _fo <= 0:
        _miss.append((_d, _s, "5分足の始値が0"))
        continue
    _err = (_fo - _bo) / _bo * 1e4                 # 5分足 − 板 (bp)
    _g_b = (_bo - _pc) / _pc * 1e4                 # 板から見たギャップ
    _g_f = (_fo - _pc) / _pc * 1e4                 # 5分足から見たギャップ
    _hit.append({"date": _d, "sym": _s, "board": _bo, "five": _fo,
                 "err": _err, "gap_b": _g_b, "gap_f": _g_f, "hm": _hm,
                 "late": str(r.get("late") or "")})

if not _hit:
    print(f"\n⛔ 突合できた行がありません(未突合 {len(_miss):,}行)。")
    for _d, _s, _w in _miss[:10]:
        print(f"   {_d} {_s}: {_w}")
    print("\n  当日の5分足はまだ取れていない可能性があります。"
          "\n  ローカル(J-Quants)が揃ってから測り直してください。"
          "\n  暫定で見るなら --source yfinance。")
    raise SystemExit(0)

_n = len(_hit)
_errs = sorted(abs(h["err"]) for h in _hit)
_same = sum(1 for h in _hit if abs(h["err"]) < 0.5)      # 実質一致(0.5bp未満)


def _q(p: float) -> float:
    return _errs[min(_n - 1, int(_n * p))]


print(f"""
{'-' * 92}
① 誤差の分布 (5分足の始値 − 板の始値)
{'-' * 92}
  突合 {_n:,}件 / 未突合 {len(_miss):,}件
  **完全一致(<0.5bp)  {_same:,}件 ({_same / _n * 100:.1f}%)**
  誤差 中央 {_q(0.5):.2f}bp / 90%点 {_q(0.9):.2f}bp / 95%点 {_q(0.95):.2f}bp """
      f"""/ 最大 {_errs[-1]:.2f}bp""")

# ── ② 判定の反転 ───────────────────────────────────────────────────────
_th = float(args.gap_bp)
_fp = [h for h in _hit if h["gap_b"] >= _th > h["gap_f"]]   # 板で合格・実は不合格
_fn = [h for h in _hit if h["gap_b"] < _th <= h["gap_f"]]   # 板で不合格・実は合格
print(f"""
{'-' * 92}
② ★ 判定の反転 ({_th:+.0f}bp 以上で合格)
{'-' * 92}
  板で合格 → 5分足では不合格 (**誤って建てる**)  {len(_fp):,}件
  板で不合格 → 5分足では合格 (**取り逃す**)      {len(_fn):,}件
  **反転率 {(len(_fp) + len(_fn)) / _n * 100:.2f}%**""")
for _h in (_fp + _fn)[:args.show]:
    print(f"    {_h['date']} {_h['sym']}: 板 {_h['gap_b']:+.1f}bp "
          f"/ 5分足 {_h['gap_f']:+.1f}bp  ({_h['board']:,.1f} vs {_h['five']:,.1f})")

# ── ③ 先頭バーの時刻 ───────────────────────────────────────────────────
_by_hm: dict = {}
for _h in _hit:
    _by_hm[_h["hm"]] = _by_hm.get(_h["hm"], 0) + 1
print(f"""
{'-' * 92}
③ 5分足の先頭バーの時刻 (09:00 でないと『始値』の意味が違う)
{'-' * 92}""")
for _k2, _v2 in sorted(_by_hm.items(), key=lambda x: -x[1])[:8]:
    print(f"  {_k2}  {_v2:,}件 ({_v2 / _n * 100:.1f}%)")

# ── ズレの大きい順 ─────────────────────────────────────────────────────
_bad = sorted(_hit, key=lambda h: -abs(h["err"]))[:args.show]
if _bad and abs(_bad[0]["err"]) >= 0.5:
    print(f"""
{'-' * 92}
ズレの大きい順 {min(args.show, len(_bad))}件
{'-' * 92}
  {'日付':<12}{'銘柄':<8}{'板の始値':>11}{'5分足':>11}{'誤差':>10}{'先頭バー':>10}{'遅寄':>6}""")
    for _h in _bad:
        if abs(_h["err"]) < 0.5:
            break
        print(f"  {_h['date']:<12}{_h['sym']:<8}{_h['board']:>11,.1f}"
              f"{_h['five']:>11,.1f}{_h['err']:>+9.1f}bp{_h['hm']:>10}"
              f"{'★' if _h['late'] == '1' else '':>6}")

# ── 判定 ───────────────────────────────────────────────────────────────
_rev = (len(_fp) + len(_fn)) / _n * 100
print(f"""
{'=' * 92}
■ 判定
{'=' * 92}""")
if _same / _n >= 0.98 and _rev < 0.5:
    print("  ✅ **板の始値 = 5分足の始値**。バックテストの前提は成立しています。\n"
          "     板で判定・発注してよく、J の数字はこの点では信用できます。")
elif _rev < 2.0:
    print(f"  △ おおむね一致しますが反転が {_rev:.2f}% あります。\n"
          f"     件数が少ないうちは断定せず、数営業日ぶん貯めて再確認してください。")
else:
    print(f"  ⛔ **反転が {_rev:.2f}% あります。前提が崩れています。**\n"
          f"     板で判定すると建てる銘柄がバックテストと変わるので、\n"
          f"     J の数字(月+29万〜34万)は そのままでは使えません。原因を特定すること。")
print(f"""
  ⚠ 5分足の出所は **source={args.source}**。バックテストが使うのは
    J-Quants のローカル(stock_5min)です。yfinance に落ちていた場合は、
    ローカルが揃ってから測り直してください(ログの『ローカル: N/M銘柄』を確認)。
  ★ 1日ぶんでは足りません。**数営業日ぶん貯めてから** --glob "k_paper_*.csv"
    でまとめて見るのが本番の判定です。
""")
