r"""show_one_trade.py — 1銘柄1日を「実際の5分足の動き」から丸ごと出す。

なぜ必要か
----------
現行 lss / E(寄成) / H(前日終値の指値) は **注文の出し方だけ**が違う。
集計の勝った負けたを何度並べても、その差が何をしているかは分からない。
**同じ日の同じ銘柄で、3つが実際どこで入ってどこで出たか**を1本の時系列に
並べれば、判断は自分の目でできる。

出るもの
--------
  1) 日足(前日終値・始値・高値・安値・終値・ATR)
  2) 3方式の 注文値 / 約定値 / 損切 / 利確 — 計算式つき
  3) **5分足の全バー**。各バーに『そのバーで何が起きたか』の印がつく
       ▼E約定  ▼H約定  ▼現行約定   ×損切  ◎利確   ⛨=損切り遅延で無防備な窓
  4) 3方式の損益と、なぜそうなったかの一行説明

使い方
------
  python show_one_trade.py --symbol 6140 --date 2026-08-10
  python show_one_trade.py --symbol 6140              # 最新営業日
  python show_one_trade.py --date 2026-08-10 --from-orders   # orders_<日付>.csv の全銘柄から選ぶ

⚠ 決済条件(sm/tm/損切り遅延/ガード)は環境変数を含めて**エンジンの実効値**を読む。
   .\daily と同じ条件で見たいなら同じ env を立ててから実行すること(18.10.1)。
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

import backtest_limit_entry as B
from daytrade_data import load_intraday, split_by_day
from intraday_integrity import day_scale_ok
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m

ap = argparse.ArgumentParser(description="1銘柄1日を5分足の動きから丸ごと出す")
ap.add_argument("--symbol", help="銘柄コード (6140 でも 6140.T でも可)")
ap.add_argument("--date", help="YYYY-MM-DD。省略で5分足の最新営業日")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(既定0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(既定1.0)")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--stop-delay-bars", type=int, default=None,
                help="省略でエンジンの実効値(LSS_STOP_DELAY_BARS)")
ap.add_argument("--gap-guard", type=float, default=None,
                help="省略でエンジンの実効値(既定0.03=±3%%)")
ap.add_argument("--from-orders", action="store_true",
                help="orders_<日付>.csv を読んで銘柄一覧を出す")
a = ap.parse_args()

_DLY = (a.stop_delay_bars if a.stop_delay_bars is not None
        else int(getattr(B, "_LSS_STOP_DELAY_BARS", 0) or 0))
_GAP = (a.gap_guard if a.gap_guard is not None
        else float(getattr(B, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)))


def _sym(s: str) -> str:
    s = str(s).strip().upper()
    return s if s.endswith(".T") else f"{s}.T"


# ── --from-orders: その日の実発注銘柄を一覧する ─────────────────────────────
if a.from_orders:
    if not a.date:
        sys.exit("[error] --from-orders は --date が要ります")
    f = f"orders_{a.date.replace('-', '')}.csv"
    if not os.path.exists(f):
        sys.exit(f"[error] {f} がありません。先に `.\\fills --date {a.date.replace('-', '')}` を。")
    d = pd.read_csv(f)
    col = next((c for c in ("symbol", "Symbol", "銘柄", "code") if c in d.columns), None)
    print(f"[{f}] {len(d)}行")
    print(d.to_string(index=False, max_colwidth=18))
    if col is not None:
        print(f"\n→ 例: python show_one_trade.py --symbol {d[col].iloc[0]} --date {a.date}")
    sys.exit(0)

if not a.symbol:
    sys.exit("[error] --symbol が要ります (--from-orders で一覧できます)")

sym = _sym(a.symbol)

# ── データ ────────────────────────────────────────────────────────────────
df = B.fetch(sym, 900)
if df is None or df.empty:
    sys.exit(f"[error] {sym} の日足が取れません")
df = df.copy()
df.index = pd.to_datetime(df.index).normalize()
_c = {str(x).lower(): x for x in df.columns}
df = df.rename(columns={_c["open"]: "o", _c["high"]: "h",
                        _c["low"]: "l", _c["close"]: "c"})[["o", "h", "l", "c"]].astype(float)
_pc = df["c"].shift(1)
_tr = pd.concat([df["h"] - df["l"], (df["h"] - _pc).abs(), (df["l"] - _pc).abs()],
                axis=1).max(axis=1)
df["atr"] = _tr.ewm(span=14, adjust=False).mean()

m5 = load_intraday(sym, days=900, source="local")
if m5 is None or not len(m5):
    sys.exit(f"[error] {sym} の5分足がありません (MINUTE_5M_DIR / stock_5min を確認)")
days = split_by_day(m5)
if not days:
    sys.exit(f"[error] {sym} の5分足を日付に分割できません")

if a.date:
    ts = pd.Timestamp(a.date)
else:
    ts = pd.Timestamp(max(days))
    print(f"[info] --date 省略 → 5分足の最新営業日 {ts.date()} を使います")

day5 = days.get(ts.date())
if day5 is None or len(day5) == 0:
    sys.exit(f"[error] {sym} の {ts.date()} の5分足がありません")

pos = df.index.searchsorted(ts)
if pos <= 0 or pos >= len(df.index) or df.index[pos] != ts:
    sys.exit(f"[error] {sym} の {ts.date()} の日足がありません")

pc = float(df["c"].iloc[pos - 1])                   # 前日終値
o1, h1, l1, c1 = (float(df["o"].iloc[pos]), float(df["h"].iloc[pos]),
                  float(df["l"].iloc[pos]), float(df["c"].iloc[pos]))
atr = float(df["atr"].iloc[pos - 1])                # 前日ATR

if not day_scale_ok(day5, c1):
    print("⚠ 5分足と日足の価格基準がズレています(株式分割の未調整)。"
          "エンジンならこの日を捨てます(18.27)。以下は参考値。")

W = 78
print("=" * W)
print(f" {sym}  {ts.date()}   —  現行 lss / E(寄成) / H(前日終値の指値) を同じ日で比べる")
print("=" * W)
print(f"\n■ 日足   前日終値 {pc:,.1f}   始値 {o1:,.1f}   高値 {h1:,.1f}   "
      f"安値 {l1:,.1f}   終値 {c1:,.1f}")
print(f"         前日ATR {atr:,.2f}  (= 株価の {atr / pc * 100:.2f}%)   "
      f"損切 {a.sm}ATR = {atr * a.sm:,.1f}円   利確 {a.tm}ATR = {atr * a.tm:,.1f}円")
print(f"         寄りギャップ {(o1 / pc - 1) * 100:+.2f}%   "
      f"設定: 損切り遅延 {_DLY}本 / ガード ±{_GAP * 100:.0f}% / {a.qty}株 / 手数料 "
      f"{B.FEE_PCT_ONE_WAY * 100:.3f}%片道")

# ── 3方式の注文を作る ──────────────────────────────────────────────────────
# 現行: 損切/利確は **前日終値** を基準に測る(トリガーの-1tickはリスク幅ではない)。
_base = B.round_to_tick(pc)
cur_trig = float(B.round_to_tick(_base - B.tick_size(_base)))
cur_stop = float(B.ceil_to_tick(pc + atr * a.sm))
cur_tgt = float(B.ceil_to_tick(pc - atr * a.tm))

# E/H: OCO の基準は **実約定価格**(寄り値)。それ以外は現行と同一(18.32)。
e_ok = not (_GAP > 0 and o1 < pc * (1 - _GAP))
e_fill = o1
h_fill = o1 if o1 >= pc else pc
h_ok = not (_GAP > 0 and o1 > pc * (1 + _GAP))


def _oco(ep):
    return ep + atr * a.sm, ep - atr * a.tm


e_stop, e_tgt = _oco(e_fill)
h_stop, h_tgt = _oco(h_fill)

# ── 現行の約定 ────────────────────────────────────────────────────────────
cur_fill = short_entry_fill_5m(day5, cur_trig, False, entry_gap_limit=_GAP,
                               day_open=o1, day_low=l1, day_high=h1)

print(f"\n■ 3方式の注文")
print(f"  {'':<8}{'注文':>26}{'約定値':>11}{'損切':>10}{'利確':>10}")
print(f"  {'現行':<8}{'逆指値売 ' + format(cur_trig, ',.1f') + '(前日終値-1tick)':>26}"
      f"{(format(cur_fill, ',.1f') if cur_fill else '約定せず'):>11}"
      f"{cur_stop:>10,.1f}{cur_tgt:>10,.1f}")
print(f"  {'E':<8}{'寄成売り(9:00 板寄せ)':>26}"
      f"{(format(e_fill, ',.1f') if e_ok else 'ガード外'):>11}"
      f"{e_stop:>10,.1f}{e_tgt:>10,.1f}")
print(f"  {'H':<8}{'指値売 ' + format(pc, ',.1f') + '(前日終値)':>26}"
      f"{(format(h_fill, ',.1f') if h_ok else 'ガード外'):>11}"
      f"{h_stop:>10,.1f}{h_tgt:>10,.1f}")
if o1 >= pc:
    print(f"  → 寄り {o1:,.1f} ≥ 前日終値 {pc:,.1f} なので **H も板寄せで始値約定** "
          f"= この日は E と H が同じ")
else:
    print(f"  → 寄り {o1:,.1f} < 前日終値 {pc:,.1f} なので H は"
          f" **前日終値まで戻れば**約定(戻らなければ建てない)")

# ── 決済 ──────────────────────────────────────────────────────────────────
_R = {"stop": "×損切り", "target": "◎利確", "close": "引け成行(タイムカット)",
      "no_entry": "約定せず", "no_5m": "5分足なし"}
res = {}
if cur_fill:
    xp, why, ets, xts = short_exit_5m(day5, cur_trig, cur_stop, cur_tgt, False,
                                      day_low=l1, day_high=h1, day_close=c1,
                                      stop_delay_bars=_DLY)
    res["現行"] = (cur_fill, xp, why, ets, xts)
else:
    res["現行"] = (None, None, "no_entry", None, None)

if e_ok:
    xp, why, ets, xts = short_exit_5m(day5, float("inf"), e_stop, e_tgt, False,
                                      day_low=l1, day_high=h1, day_close=c1,
                                      stop_delay_bars=_DLY)
    res["E"] = (e_fill, xp, why, ets, xts)
else:
    res["E"] = (None, None, "no_entry", None, None)

if h_ok:
    xp, why, ets, xts = short_exit_5m(day5, h_fill, h_stop, h_tgt, True,
                                      day_low=l1, day_high=h1, day_close=c1,
                                      stop_delay_bars=_DLY)
    res["H"] = (h_fill, xp, why, ets, xts)
else:
    res["H"] = (None, None, "no_entry", None, None)


def _hm(t):
    try:
        return pd.Timestamp(t).strftime("%H:%M")
    except Exception:
        return ""


# ── 5分足の全バー ─────────────────────────────────────────────────────────
print(f"\n■ 5分足 — 実際の動き ({len(day5)}本)")
print(f"  {'時刻':<7}{'始値':>9}{'高値':>9}{'安値':>9}{'終値':>9}   できごと")
_mark_in = {}
_mark_out = {}
for k, (fp, xp, why, ets, xts) in res.items():
    if fp:
        _mark_in.setdefault(_hm(ets) if ets is not None else _hm(day5.index[0]), []).append(k)
        if xts is not None:
            _mark_out.setdefault(_hm(xts), []).append((k, why, xp))

# 損切り遅延の無防備な窓(各方式の約定バーから _DLY 本)
_unsafe = set()
if _DLY > 0:
    _idx = [_hm(t) for t in day5.index]
    for k, (fp, xp, why, ets, xts) in res.items():
        if not fp:
            continue
        _t0 = _hm(ets) if ets is not None else _idx[0]
        if _t0 in _idx:
            j = _idx.index(_t0)
            for m in range(j, min(j + _DLY, len(_idx))):
                _unsafe.add(_idx[m])

for t, r in day5.iterrows():
    hm = _hm(t)
    ev = []
    for k in _mark_in.get(hm, []):
        ev.append(f"▼{k}約定")
    for k, why, xp in _mark_out.get(hm, []):
        ev.append(f"{_R.get(why, why)}→{k} @{xp:,.1f}")
    if hm in _unsafe and _DLY > 0:
        ev.append("⛨損切り遅延の無防備な窓")
    print(f"  {hm:<7}{float(r['open']):>9,.1f}{float(r['high']):>9,.1f}"
          f"{float(r['low']):>9,.1f}{float(r['close']):>9,.1f}   {' / '.join(ev)}")

# ── 損益 ──────────────────────────────────────────────────────────────────
print(f"\n■ 損益 ({a.qty}株 / 手数料 片道{B.FEE_PCT_ONE_WAY * 100:.3f}%)")
print(f"  {'':<6}{'約定':>10}{'決済':>10}{'理由':>22}{'決済時刻':>10}{'損益':>12}")
for k in ("現行", "E", "H"):
    fp, xp, why, ets, xts = res[k]
    if not fp or xp is None:
        print(f"  {k:<6}{'—':>10}{'—':>10}{_R.get(why, why):>22}{'—':>10}{'—':>12}")
        continue
    fee = (fp + xp) * a.qty * B.FEE_PCT_ONE_WAY
    pnl = (fp - xp) * a.qty - fee
    print(f"  {k:<6}{fp:>10,.1f}{xp:>10,.1f}{_R.get(why, why):>22}"
          f"{_hm(xts):>10}{pnl:>+12,.0f}円")

print(f"\n{'─' * W}")
print("■ この1件の読み方")
print(f"{'─' * W}")
print("  ・現行は**下がらないと建たない**。トリガーに届かない日は 0円(建てない)。")
print("  ・E は必ず建つので、下げる日は取れるが上げる日も全部かぶる。")
print("  ・H は**前日終値まで戻ったら**建つ。戻らずそのまま下げた日は取り逃がす代わり、")
print("    戻った日は E より高く売れる(その差がそのまま損益差)。")
print("  ⚠ 1件で優劣は決まらない。月次σは10万円規模ある(18.24)。ここで見るのは")
print("    『集計の差が、実際の値動きのどこから出ているか』であって勝敗ではない。")
