r"""sweep_h_limit.py — H(前日終値の指値売り)の**指値位置**を掃く。

H の唯一の固有パラメータ
------------------------
H は「前日終値に指値を置いて、株価が上がって到達したら売る」。
指値をどこに置くかは H を定義するパラメータそのものなのに、**一度も掃かれていない**。

  指値を上げる → より高く売れる。ただし到達しない日が増えて**約定率が落ちる**
  指値を下げる → 約定率は上がるが**安く売る**ことになる(現行に近づく)

トレードオフの底がどこかは測らないと分からない。ここを掃く。

出力
----
  オフセット(ティック)ごとに: 発注数 / 約定数 / 約定率 / 板寄せ比率 /
  円件 / 合計 / 勝率 / 損切り比率 を TRAIN と TEST に分けて出す。

  ⚠ **板寄せ比率は必ず見ること。** 板寄せ(寄り >= 指値)は9:00の板寄せで確実に
     約定するのでバックテストと現実が一致するが、ザラ場到達は「高値が指値に
     触れたら約定」という仮定で、実際は板の待ち行列がある(18.33)。
     指値を上げるほどザラ場到達の比率が上がる = 数字が楽観側に寄る。

使い方
------
  .\hsweep                       (推奨。.bat が引数を渡す)
  python sweep_h_limit.py --trades lss_trades.csv --holdout-days 120

⚠ ここは**予算制約なし**の全トレード評価。良さそうなオフセットが見つかったら
   必ず `set LSS_H_LIMIT_TICKS=N` を立てて `.\daily` を回し、**予算400万の
   E/H タブ**で確かめること(18.10:『全部買えるなら得』と『予算内でどれを買うか』
   は別問題)。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

ap = argparse.ArgumentParser(description="H の指値位置(前日終値±Nティック)を掃く")
ap.add_argument("--trades", default="lss_trades.csv",
                help="レポートが出す全取引CSV(母集団の (銘柄,日) を取る)")
ap.add_argument("--offsets", default="-3,-2,-1,0,1,2,3,5",
                help="前日終値からのオフセット。単位は --offset-mode に従う。0=現行")
ap.add_argument("--offset-mode", choices=["ticks", "pct", "atr"], default="ticks",
                help="オフセットの単位。"
                     "ticks=呼値の個数(既定) / pct=前日終値に対する%% / "
                     "atr=前日ATRの倍数。"
                     "⛔ ticks は**株価によって意味が5倍変わる**"
                     "(1,000円株の-2tick=-1.00% vs 4,900円株の-2tick=-0.20%)。"
                     "この価格帯の呼値は 1,000-4,999円=5円 / 5,000円以上=10円 で固定。"
                     "揃えて比べるなら pct か atr を使うこと")
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--gap-guard", type=float, default=0.03)
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--holdout-days", type=int, default=120,
                help="直近N日を TEST に。0で分割しない")
ap.add_argument("--stop-delay-bars", type=int, default=None,
                help="省略でエンジンの実効値(LSS_STOP_DELAY_BARS)")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
a = ap.parse_args()

try:
    import pandas as pd
    import backtest_limit_entry as B
    from backtest_limit_entry import fetch as _fetch, round_to_tick as _r2t, tick_size as _tsz
    from daytrade_data import load_intraday as _li, split_by_day as _sbd
    from intraday_integrity import day_scale_ok as _ig_ok
    from sameday5m_firsttouch import short_exit_5m as _x5
except Exception as e:
    sys.exit(f"[error] 依存モジュールを読めません: {e}")

_DLY = (a.stop_delay_bars if a.stop_delay_bars is not None
        else int(getattr(B, "_LSS_STOP_DELAY_BARS", 0) or 0))
def _num(x):
    try:
        return float(x)
    except Exception:
        return None


OFFS = [v for v in (_num(x) for x in a.offsets.split(",")) if v is not None]
if 0.0 not in OFFS:
    OFFS.append(0.0)
OFFS = sorted(set(OFFS))
_UNIT = {"ticks": "tick", "pct": "%", "atr": "ATR"}[a.offset_mode]


def _limit_of(pc: float, atr: float, off: float) -> float:
    """前日終値 pc からオフセット off ぶんずらした指値。呼値に丸める。"""
    if not off:
        return pc
    if a.offset_mode == "ticks":
        return float(_r2t(pc + off * _tsz(pc)))
    if a.offset_mode == "pct":
        return float(_r2t(pc * (1.0 + off / 100.0)))
    return float(_r2t(pc + off * atr))          # atr

# ── 母集団 (銘柄, 日) ────────────────────────────────────────────────────
if not os.path.exists(a.trades):
    sys.exit(f"[error] {a.trades} がありません。先に .\\daily を回してください。")
pairs: set = set()
with open(a.trades, encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        if (r.get("strategy") or "") == "転換":
            continue                      # 転換は lss ではない(18.26)
        sym = str(r.get("symbol") or "").strip()
        d = str(r.get("entry_date") or "")[:10]
        if sym and len(d) == 10:
            pairs.add((sym, d))
if not pairs:
    sys.exit(f"[error] {a.trades} から (銘柄,日) を取れませんでした")
syms = sorted({s for s, _ in pairs})
_d0, _d1 = min(d for _, d in pairs), max(d for _, d in pairs)
print(f"[母集団] {len(pairs):,}銘柄日 / {len(syms):,}銘柄  {_d0}〜{_d1}  ({a.trades})")
# ⛔ lss_trades.csv はレポートの表示窓ぶんしか書かれない。--days 180 で作った CSV に
#    120日のホールドアウトを切ると TRAIN が60日しか残らず、TRAIN/TEST の比較が
#    成立しない(2026-08-10 に実際に踏んだ: TRAIN 898件 vs TEST 1,950件)。
if a.holdout_days > 0:
    import datetime as _dt
    try:
        _span = (_dt.date.fromisoformat(_d1) - _dt.date.fromisoformat(_d0)).days
        _train_days = _span - a.holdout_days
        if _train_days < a.holdout_days:
            print(f"  ⛔ **TRAIN が {_train_days}日 / TEST が {a.holdout_days}日 で不均衡です。**")
            print(f"     {a.trades} の窓が {_span}日しかありません。1年ぶんで取り直してください:")
            print(f"       .\\dailyfast --no-serve --days 365")
            print(f"     (このまま進めても TRAIN/TEST の一致・不一致は判断材料になりません)")
    except Exception:
        pass
print(f"[条件] sm={a.sm} tm={a.tm} 損切り遅延={_DLY}本 ガード±{a.gap_guard*100:.0f}% "
      f"{a.qty}株 / 価格{a.min_price:,.0f}〜{a.max_price:,.0f} / 手数料"
      f"{B.FEE_PCT_ONE_WAY*100:.3f}%片道")
print(f"[掃く] 指値オフセット {OFFS} {_UNIT} (0=現行) / mode={a.offset_mode}")
if a.offset_mode == "ticks":
    print("  ⚠ ticks は株価で意味が変わる(1,000円株の-2tick=-1.00% / "
          "4,900円株の-2tick=-0.20%)。揃えるなら --offset-mode pct か atr")


def _load(sym):
    try:
        df = _fetch(sym, 900)
        if df is None or df.empty:
            return sym, None, {}
        df = df.copy()
        df.index = pd.to_datetime(df.index).normalize()
        c = {str(x).lower(): x for x in df.columns}
        if any(c.get(x) is None for x in ("open", "high", "low", "close")):
            return sym, None, {}
        o = df.rename(columns={c["open"]: "o", c["high"]: "h",
                               c["low"]: "l", c["close"]: "c"})[
            ["o", "h", "l", "c"]].astype(float)
        pc = o["c"].shift(1)
        tr = pd.concat([o["h"] - o["l"], (o["h"] - pc).abs(),
                        (o["l"] - pc).abs()], axis=1).max(axis=1)
        o["atr"] = tr.ewm(span=14, adjust=False).mean()
    except Exception:
        return sym, None, {}
    for _ in range(2):                     # 並列読込の SystemError 対策(18.33)
        try:
            m5 = _li(sym, days=900, source="local")
            return sym, o, (_sbd(m5) if m5 is not None and len(m5) else {})
        except Exception:
            continue
    return sym, o, {}


daily, i5 = {}, {}
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    _done = 0
    for f in as_completed([ex.submit(_load, s) for s in syms]):
        s, o, m = f.result()
        daily[s], i5[s] = o, m
        _done += 1
        if _done % 100 == 0:
            print(f"  ...{_done}/{len(syms)}銘柄", flush=True)
_miss = [s for s in syms if not i5.get(s)]
for s in _miss:                            # 直列で拾い直す
    _, o, m = _load(s)
    if m:
        daily[s], i5[s] = o, m
print(f"[データ] 5分足 {sum(1 for v in i5.values() if v):,}銘柄 読込"
      + (f" (読めず {sum(1 for v in i5.values() if not v):,})" if _miss else ""))

# ── 各 (銘柄,日) を全オフセットで評価 ────────────────────────────────────
_INF = float("inf")
# stat[off][split] = {n_order, n_fill, n_auction, pnl, win, stop, ...}
stat: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
_ho = None
if a.holdout_days > 0:
    _ho = (pd.Timestamp.today().normalize() - pd.Timedelta(days=a.holdout_days)).date()
_skip = 0

for sym, dstr in sorted(pairs):
    df = daily.get(sym)
    day5 = (i5.get(sym) or {})
    if df is None or not day5:
        _skip += 1
        continue
    try:
        ts = pd.Timestamp(dstr)
    except Exception:
        _skip += 1
        continue
    idx = df.index
    pos = idx.searchsorted(ts)
    if pos <= 0 or pos >= len(idx) or idx[pos] != ts:
        _skip += 1
        continue
    pc = float(df["c"].iloc[pos - 1])
    o1, c1 = float(df["o"].iloc[pos]), float(df["c"].iloc[pos])
    dl, dh = float(df["l"].iloc[pos]), float(df["h"].iloc[pos])
    atr = float(df["atr"].iloc[pos - 1])
    d5 = day5.get(ts.date())
    if not (pc > 0 and o1 > 0 and c1 > 0 and atr == atr and atr > 0):
        _skip += 1
        continue
    if d5 is None or len(d5) == 0 or not _ig_ok(d5, c1):
        _skip += 1
        continue
    try:
        if pd.Timestamp(d5.index[0]).strftime("%H:%M") != "09:00":
            _skip += 1
            continue
    except Exception:
        _skip += 1
        continue
    if a.min_price > 0 and pc < a.min_price:
        _skip += 1
        continue
    if a.max_price > 0 and pc > a.max_price:
        _skip += 1
        continue

    split = "TRAIN" if (_ho is None or ts.date() < _ho) else "TEST"
    for off in OFFS:
        lim = _limit_of(pc, atr, off)
        st = stat[off][split]
        st["n_order"] += 1
        if a.gap_guard > 0 and o1 > lim * (1 + a.gap_guard):
            continue                       # ギャップアップでガード
        ep = o1 if o1 >= lim else lim
        xp, why, _e, _x = _x5(d5, ep, ep + atr * a.sm, ep - atr * a.tm, True,
                              day_low=dl, day_high=dh, day_close=c1,
                              stop_delay_bars=_DLY)
        if xp is None or why in ("no_5m", "no_entry"):
            continue                       # 指値に到達せず建てられない
        fee = (ep + float(xp)) * a.qty * B.FEE_PCT_ONE_WAY
        pnl = (ep - float(xp)) * a.qty - fee
        st["n_fill"] += 1
        st["pnl"] += pnl
        # 板寄せ(確実に約定) と ザラ場到達(タッチ=約定の仮定) を**分けて**貯める。
        # H の利益がどちらに乗っているかで、実運用で取れる額がまったく変わる(18.33)。
        _auc = o1 >= lim
        if _auc:
            st["n_auction"] += 1
            st["pnl_auction"] += pnl
        else:
            st["pnl_intra"] += pnl
        # 平均利益/平均損失/PF を型ごとに出す。『勝率が低いのになぜプラスか』は
        # ペイオフ比を見ないと分からない(勝ち3.4回ぶん = 負け1回、等)。
        if pnl > 0:
            st["win"] += 1
            st["win_auction" if _auc else "win_intra"] += 1
            st["gp_auction" if _auc else "gp_intra"] += pnl
        else:
            st["gl_auction" if _auc else "gl_intra"] += -pnl
        if why == "stop":
            st["stop"] += 1
            # ザラ場到達は『上昇中に建てる』ので損切り率が高いはず。
            # delay1 の無保護窓も上昇の真っ最中に当たる(18.17)。型ごとに出す。
            st["stop_auction" if _auc else "stop_intra"] += 1

print(f"[除外] データ不足/価格外 {_skip:,}銘柄日\n")

# ── 出力 ────────────────────────────────────────────────────────────────
W = 104
_splits = ["TRAIN", "TEST"] if _ho is not None else ["TRAIN"]
print("=" * W)
print("H の指値位置スイープ — 前日終値から何ティックずらすか")
print("=" * W)
if _ho is not None:
    print(f"  TRAIN = 〜{_ho} の手前 / TEST = {_ho} 以降")
print("  ⚠ 板寄せ% が低いほど『高値がタッチ＝約定』の仮定に依存する = 楽観側(18.33)\n")

for sp in _splits:
    print(f"■ {sp}")
    print(f"  {'指値':>6}{'発注':>8}{'約定':>8}{'約定率':>8}{'板寄せ%':>9}"
          f"{'勝率':>7}{'損切%':>7}{'円/件':>10}{'合計':>14}")
    _best = None
    for off in OFFS:
        s = stat[off][sp]
        no, nf = int(s["n_order"]), int(s["n_fill"])
        if not no:
            continue
        cpt = (s["pnl"] / nf) if nf else 0.0
        if _best is None or s["pnl"] > _best[1]:
            _best = (off, s["pnl"], cpt)
        mark = " ←現行" if off == 0 else ""
        print(f"  {off:>+6g}{no:>8,}{nf:>8,}{(nf/no*100 if no else 0):>7.1f}%"
              f"{(s['n_auction']/nf*100 if nf else 0):>8.0f}%"
              f"{(s['win']/nf*100 if nf else 0):>6.0f}%"
              f"{(s['stop']/nf*100 if nf else 0):>6.0f}%"
              f"{cpt:>+10,.0f}{s['pnl']:>+14,.0f}{mark}")
    if _best:
        print(f"  → {sp} 最良: {_best[0]:+g}{_UNIT}  合計 {_best[1]:+,.0f}  "
              f"円/件 {_best[2]:+,.0f}")
    # ── 板寄せ / ザラ場到達 に分けた内訳 ────────────────────────────────
    #    板寄せ分は現実と一致する = **これが確実に取れる額**。
    #    ザラ場分は『高値がタッチ=約定』の仮定に乗っているので、実運用では
    #    待ち行列で取れないことがある。合計だけ見ると差を読み違える。
    print(f"  {'指値':>6}{'板寄せ件':>10}{'板寄せ損益':>14}{'円/件':>10}"
          f"{'勝率':>7}{'損切%':>7}"
          f"{'ザラ場件':>10}{'ザラ場損益':>14}{'円/件':>10}{'勝率':>7}{'損切%':>7}")
    for off in OFFS:
        s2 = stat[off][sp]
        nf = int(s2["n_fill"])
        if not nf:
            continue
        na = int(s2["n_auction"])
        pa, pi = s2["pnl_auction"], s2["pnl_intra"]
        dep = (pi / (pa + pi) * 100) if (pa + pi) else 0.0
        ni = nf - na
        print(f"  {off:>+6g}{na:>10,}{pa:>+14,.0f}{(pa/na if na else 0):>+10,.0f}"
              f"{(s2['win_auction']/na*100 if na else 0):>7.0f}%"
              f"{(s2['stop_auction']/na*100 if na else 0):>7.0f}%"
              f"{ni:>10,}{pi:>+14,.0f}{(pi/ni if ni else 0):>+10,.0f}"
              f"{(s2['win_intra']/ni*100 if ni else 0):>7.0f}%"
              f"{(s2['stop_intra']/ni*100 if ni else 0):>7.0f}%"
              + ("  ←現行" if off == 0 else ""))
    # ── ペイオフの内訳 ────────────────────────────────────────────────
    print(f"  {'指値':>6}{'型':>8}{'平均利益':>11}{'平均損失':>11}{'ペイオフ比':>11}"
          f"{'勝率':>7}{'PF':>7}{'円/件':>10}")
    for off in OFFS:
        s3 = stat[off][sp]
        for _ty, _k in (("板寄せ", "auction"), ("ザラ場", "intra")):
            _n = int(s3["n_auction"]) if _k == "auction" else int(s3["n_fill"] - s3["n_auction"])
            if not _n:
                continue
            _w = int(s3[f"win_{_k}"])
            _gp, _gl = s3[f"gp_{_k}"], s3[f"gl_{_k}"]
            _aw = _gp / _w if _w else 0.0
            _al = _gl / (_n - _w) if (_n - _w) else 0.0
            print(f"  {off:>+6g}{_ty:>8}{_aw:>+11,.0f}{-_al:>+11,.0f}"
                  f"{(_aw / _al if _al else 0):>10.2f}:1"
                  f"{(_w / _n * 100):>6.0f}%{(_gp / _gl if _gl else 0):>7.2f}"
                  f"{((_gp - _gl) / _n):>+10,.0f}"
                  + ("  ←現行" if off == 0 and _k == "auction" else ""))
    print("  ※ 勝率が3割でもプラスになるのは**ペイオフ比**が効いているから。")
    print("     負け=損切0.1ATR(小さく刻む) / 勝ち=利確1.0ATR(大きく取る)。")
    print("     『上に行って損切り』は想定内で、その頻度と損失幅は既に織り込まれている。")
    print()
    print("  ※ 板寄せ = 寄り>=指値。9:00の板寄せで**確実に約定**する(現実と一致)。")
    print("     ザラ場 = 価格が上がってきて指値に当たった = **上昇中に建てている**。")
    print("     『タッチ=約定』の仮定に依存するうえ、損切り0.1ATR(株価の約0.3%)では")
    print("     勢いが続けばすぐ刈られる。delay1 の無保護窓も上昇の真っ最中に当たる(18.17)。")
    print()

if _ho is not None:
    _bt = max(OFFS, key=lambda o: stat[o]["TRAIN"]["pnl"])
    _be = max(OFFS, key=lambda o: stat[o]["TEST"]["pnl"])
    print("─" * W)
    print(f"  TRAIN最良 {_bt:+g}{_UNIT} → TEST 円/件 "
          f"{(stat[_bt]['TEST']['pnl'] / stat[_bt]['TEST']['n_fill'] if stat[_bt]['TEST']['n_fill'] else 0):+,.0f}"
          f" (TEST最良は {_be:+g}{_UNIT})")
    if _bt != _be:
        print("  → **TRAIN と TEST で最良が違う = 順位が再現していない**(18.28)。")
        print("     全窓で一致するオフセットだけ採用すること。--holdout-days を変えて再確認を。")
    else:
        print("  → TRAIN/TEST で最良が一致。次は予算400万で確かめること:")
        print(f"     $env:LSS_H_LIMIT_TICKS = \"{_bt:g}\"  →  .\\dailyfast  → E/H タブの H を比較")
        if a.offset_mode != "ticks":
            print("     ⚠ レポート側の LSS_H_LIMIT_TICKS は**ティック単位**です。"
                  "pct/atr で出した最良は換算が要ります。")

print("─" * W)
print("■ 読み方")
print("─" * W)
print("  ・指値を上げると 円/件 は上がるが約定率が落ちる。**合計**で見ること。")
print("  ・板寄せ% が下がるほど『タッチ＝約定』の仮定に寄りかかる(18.33)。")
print("    円/件 が少し良くても板寄せ%が大きく落ちるなら、実運用では取れない。")
print("  ・**寄指(寄付のみ)** という選択肢: 板寄せ列だけを取り、ザラ場到達を捨てる。")
print("    板の待ち行列リスクも『上昇中に建てる』パターンも消える。上の板寄せ損益が")
print("    そのまま成績になる。PowerShell で試すには:")
print("      $env:LSS_H_AUCTION_ONLY = \"1\"; $env:LSS_H_LIMIT_TICKS = \"-2\"")
print("      .\\dailyfast --no-serve --days 365     (戻すときは両方 $null)")
print("  ⚠ ここは**予算制約なし**。良いオフセットが出たら必ず上のように")
print("     レポートを回して 予算400万のE/Hタブ で確認すること(18.10)。")
