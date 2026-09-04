r"""analyze_portfolio_stop.py — **ポートフォリオ全体**の損切りを 5分足で検証する。

なぜこれが今までと違うのか (2026-08-28 ユーザー発案)
----------------------------------------------------
これまで否定してきたのは **個別の損切り**:

  ・ATR倍 (sm/tm)      … 滑り0.5%で24セル全滅。分岐点 2.6ティック (§18.55)
  ・固定円 −10,000円/件 … 保険料。平均 −61円/件 でテールを買う (§18.56 ②)

個別が負ける理由は「**銘柄固有のノイズで刈られる**」から。
ところが **合計で判定すると銘柄固有のノイズは打ち消し合う**(12銘柄なら √12 で薄まる)。
残るのは **相場全体の動き**だけ。

そして §18.60 で測ったとおり、**大負けの日は相場**:
  ・大負け日の **81%** が「日経が日中プラスの日」(全日 49%)
  ・同日の日経日中% で **R² 0.235**

**つまりこの案は、実際に見つかっている構造をそのまま狙い撃ちする。**

ヘッジ(§18.61)より優れている点
------------------------------
ヘッジは **毎日コストを払う**(分岐点 1.83bp に対し現実 4.4bp)ので棄却した。
ポートフォリオ損切りは **発火した日しかコストを払わない**。
同じ「予測せず事後で対処する」発想だが、コスト構造が根本的に違う。

⚠ 負ける筋も明確
----------------
§18.55 の決済時刻スイープ: 5分 +10.3bp → 11:05 +10.5bp → **引け +17.8bp**。
**N のエッジは『一日かけて下げ続けること』**なので、途中で降りると削る。

  → 問いは1つ: **悪い日は最後まで悪いのか、それとも戻すのか?**

本ツールはそれを直接出す(「発火した日、引けまで持っていたらどうだったか」)。

使い方
------
  # ① 実際に建てた明細を書き出す(日足の判定。数時間)
  python analyze_gap_edge.py --workers 8 --days 4200 --min-gap-bp 100 \
      --split 2020-09-01 --dump-picks picks.csv

  # ② 5分足でポートフォリオの経路を作り、閾値を掃く(軽い)
  python analyze_portfolio_stop.py --picks picks.csv

⛔ 5分足は 2024-07〜 の約2年しかない(J-Quants の分足は2年ローリング / §18.6)。
   TRAIN/TEST を分けると薄いので、**本ツールは日付で自前に分割する**
   (--split-date。既定は5分足がある期間の真ん中)。

⚠ 照会のみ。発注はしない。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--picks", default="picks.csv",
                help="analyze_gap_edge --dump-picks が出した CSV")
ap.add_argument("--levels", default="20000,30000,50000,80000,100000,150000",
                help="ポートフォリオ損切りの水準(円 / 含み損がこれを超えたら全決済)")
ap.add_argument("--trail", default="",
                help="トレーリング版も掃く(その日の最大益から この額 下げたら全決済)。"
                     "例 30000,50000,80000。空で無効")
ap.add_argument("--split-date", default="",
                help="TRAIN/TEST の境界(yyyy-mm-dd)。空なら5分足のある期間の真ん中")
ap.add_argument("--exit-times", default="14:00,15:00,15:10,15:15,15:20,15:25",
                help="★ **引け成行より早く畳んだらどうか** を掃く時刻。"
                     "§18.55 の決済時刻スイープは 5分→11:05→引け しか見ておらず、"
                     "**引け直前の10〜30分は一度も測っていない**")
ap.add_argument("--slip-pct", type=float, default=0.0005,
                help="発火時の決済スリッページ(0.0005=5bp)。全銘柄を成行で畳むので"
                     "個別損切りより不利に見積もる")
ap.add_argument("--max-dev", type=float, default=0.30,
                help="5分足と日足のズレの上限(0.30=30%%)。これを超えたら"
                     "**株式分割の汚染**(§18.27)としてその銘柄日を捨てる")
ap.add_argument("--min-cover", type=float, default=0.7,
                help="その日の建玉のうち何割が5分足で再現できれば採用するか"
                     "(0.7=7割)。これを割った日は『合計』として意味が無いので捨てる")
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--min-days", type=int, default=60,
                help="TRAIN/TEST に最低これだけの営業日が要る")
a = ap.parse_args()

try:
    from daytrade_data import load_intraday, split_by_day
except Exception as e:                                    # pragma: no cover
    sys.exit(f"[error] daytrade_data を読めません: {e}")

_LV = [float(x) for x in a.levels.split(",") if x.strip()]
_TR = [float(x) for x in a.trail.split(",") if x.strip()]

try:
    P = pd.read_csv(a.picks)
except Exception as e:
    sys.exit(f"[error] {a.picks} を読めません: {e}\n"
             f"  先に: python analyze_gap_edge.py ... --dump-picks {a.picks}")
for _c in ("date", "symbol", "side", "entry_p", "qty", "pnl"):
    if _c not in P.columns:
        sys.exit(f"[error] {a.picks} に列 {_c} がありません")
P["date"] = P["date"].astype(str)

print(f"[info] 明細 {len(P):,}件 / {P['date'].nunique():,}営業日 / "
      f"{P['symbol'].nunique():,}銘柄")
print(f"[info] 日足ベースの合計損益 {P['pnl'].sum():+,.0f}円")
print(f"[info] 発火時のスリッページ {a.slip_pct * 100:.3f}%"
      f"(全銘柄を成行で畳むので個別損切りより不利に見積もる)")

# ── 5分足を読む ───────────────────────────────────────────────────
_syms = sorted(P["symbol"].astype(str).unique())
print(f"\n[5分足] {len(_syms):,}銘柄を読み込みます(ローカルのみ)…")


def _load(sym: str):
    try:
        df = load_intraday(sym, days=1200, source="local")
    except Exception:
        return sym, None
    if df is None or df.empty:
        return sym, None
    return sym, split_by_day(df)


_bars: dict = {}
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    _fs = {ex.submit(_load, s): s for s in _syms}
    for _i, _f in enumerate(as_completed(_fs), 1):
        try:
            _s, _d = _f.result()
        except Exception:
            continue
        if _d:
            _bars[_s] = _d
        if _i % 200 == 0:
            print(f"  … {_i:,}/{len(_syms):,}", flush=True)
print(f"[5分足] 読めた {len(_bars):,}/{len(_syms):,}銘柄")
if not _bars:
    sys.exit("[error] 5分足が1銘柄も読めません(stock_5min の場所を確認)")


# ── 日ごとにポートフォリオの含み損益の**経路**を作る ─────────────────
def _paths_for_day(day: str, grp: pd.DataFrame):
    """その日の (時刻グリッド, 含み損益の経路, 最終損益, 使えた件数) を返す。

    ⛔ 経路は **銘柄の合計**。個別のノイズは打ち消し合い、相場全体の動きが残る。
    ⚠ 5分足が無い銘柄はその日から丸ごと除外する(部分的に混ぜると合計が歪む)。
    """
    _d0 = pd.Timestamp(day).date()
    _series, _fin, _n = [], 0.0, 0
    for _r in grp.itertuples():
        _b = _bars.get(str(_r.symbol), {})
        _df = _b.get(_d0)
        if _df is None or len(_df) < 5:
            _MISS.append((day, str(_r.symbol)))
            continue
        _px = _df["close"].to_numpy(dtype="float64")
        _ts = _df.index
        # ⛔⛔ **株式分割の汚染ガード**(§18.27)。
        #   5分足(J-Quants)は保存時のままで、後から分割が起きても再調整されない。
        #   日足(yfinance)は遡及調整するので、分割銘柄では
        #   **5分足の価格が日足の 2〜10倍**になる。
        #   ショートだと (建値 − 5倍の価格) × 株数 で含み損が爆発し、
        #   予算400万に対して −1億 のような数字が出る(2026-08-28 に実際に出た)。
        #   picks.csv の d1_close(その日の日足終値)と突き合わせて弾く。
        #   ⚠ 2026-08-28: 最初は **日ごと捨てて**いたので 482日 → 193日 と
        #     6割が消えた。捨てすぎ。**その銘柄だけ外す**のが正しい。
        #     『損切りなし』の基準にも同じ銘柄を外して比べるので、
        #     比較は公平に保たれる(欠けた1銘柄ぶんの摂動が両側に等しく乗る)。
        _dc = float(getattr(_r, "d1_close", 0.0) or 0.0)
        if _dc > 0 and abs(float(_px[-1]) / _dc - 1.0) > a.max_dev:
            _BAD.append((day, str(_r.symbol), float(_px[-1]) / _dc))
            continue
        # 建値との整合も見る(寄りの5分足が建値から大きく離れていたら別物)
        if float(_r.entry_p) > 0 and \
                abs(float(_px[0]) / float(_r.entry_p) - 1.0) > a.max_dev:
            _BAD.append((day, str(_r.symbol),
                         float(_px[0]) / float(_r.entry_p)))
            continue
        # ショート(side=1) は 建値-現値、ロング(side=-1) は 現値-建値
        _sgn = 1.0 if int(_r.side) > 0 else -1.0
        _pl = (float(_r.entry_p) - _px) * _sgn * int(_r.qty)
        _series.append(pd.Series(_pl, index=_ts))
        _fin += float(_r.pnl)
        _n += 1
    # ⚠ 外しすぎた日は捨てる。**ポートフォリオの合計**を見るのが目的なので、
    #   半分以上欠けた日は『合計』として意味を持たない。
    if not _series or _n < max(1, int(len(grp) * a.min_cover)):
        return None
    _m = pd.concat(_series, axis=1).ffill().bfill()
    if _m.isna().any().any():
        return None
    return _m.index, _m.sum(axis=1).to_numpy(dtype="float64"), _fin, _n


_BAD: list = []                # 分割汚染などで弾いた (日, 銘柄, 倍率)
_MISS: list = []               # 5分足が無くて外した (日, 銘柄)

# 日ごとの建玉(スリッページの基準)。閾値×日 の二重ループで毎回計算しないよう先に持つ
_NOTIONAL = (P.assign(_n=P["entry_p"] * P["qty"])
             .groupby("date")["_n"].sum().to_dict())

print("\n[経路] ポートフォリオの含み損益を組み立てます…")
_days: dict = {}
_skip = 0
for _day, _g in P.groupby("date"):
    _p = _paths_for_day(_day, _g)
    if _p is None:
        _skip += 1
        continue
    _days[_day] = _p
print(f"[経路] 作れた {len(_days):,}営業日 / 除外 {_skip:,}日")
if _BAD:
    _bs = sorted({b[1] for b in _BAD})
    print(f"  ⛔ **5分足と日足のズレで弾いた {len(_BAD):,}銘柄日 / "
          f"{len(_bs):,}銘柄**(株式分割の汚染 / §18.27)")
    for _d, _sy, _rt in sorted(_BAD, key=lambda x: -abs(x[2]))[:5]:
        print(f"     {_d} {_sy} … 5分足/日足 = **{_rt:.2f}倍**")
    print(f"     ⚠ **その銘柄だけ**外します(日ごとは捨てない)。"
          f"基準の『損切りなし』にも同じ銘柄を外して比べるので比較は公平です")
if _MISS:
    print(f"  ⚠ 5分足が無くて外した {len(_MISS):,}銘柄日")

# ★ 自己検算: 経路の最終値の合計 は その日の pnl の合計 と一致するはず。
#   (最後の5分足の終値 ≒ 日足の終値なので)
_e = [abs(float(_v[1][-1]) - float(_v[2])) for _v in _days.values()]
_ee = float(np.mean(_e)) if _e else 0.0
print(f"  ★ 自己検算: 経路の最終値 vs 日足の損益 … 平均誤差 "
      f"**{_ee:,.0f}円/日**")
if _ee > 20000:
    print(f"     ⛔ **誤差が大きすぎます**。5分足と日足が別物を指している"
          f"可能性があります(--max-dev を下げて再実行)")
if len(_days) < a.min_days * 2:
    sys.exit(f"[error] 営業日が少なすぎます({len(_days)}日)。"
             f"5分足は 2024-07〜 の約2年しかありません(§18.6)")

_ks = sorted(_days)
_cut = a.split_date or _ks[len(_ks) // 2]
_tr = [k for k in _ks if k < _cut]
_te = [k for k in _ks if k >= _cut]
print(f"[分割] TRAIN {_tr[0]}〜{_tr[-1]} ({len(_tr)}日) / "
      f"TEST {_te[0]}〜{_te[-1]} ({len(_te)}日)  境界 {_cut}")
if min(len(_tr), len(_te)) < a.min_days:
    sys.exit(f"[error] どちらかの窓が {a.min_days}日 未満です。--split-date で調整を")


def _apply(day: str, level: float = 0.0, trail: float = 0.0):
    """その日にルールを当てたときの損益と、発火したかを返す。

    level … 含み損が -level を割ったら全決済
    trail … その日の最大益から trail 下げたら全決済
    ⚠ 発火は **その5分足の終値** で検知し、決済は **次の足の始値** ではなく
      同じ終値に slip を掛ける(次足始値は5分後で、実運用の成行より甘くなる)。
    """
    _ts, _path, _fin, _n = _days[day]
    if level <= 0 and trail <= 0:
        return _fin, False, -1
    _hit = -1
    if level > 0:
        _w = np.where(_path <= -level)[0]
        if len(_w):
            _hit = int(_w[0])
    if trail > 0:
        _peak = np.maximum.accumulate(_path)
        _w = np.where(_path <= _peak - trail)[0]
        if len(_w):
            _hit = int(_w[0]) if _hit < 0 else min(_hit, int(_w[0]))
    if _hit < 0:
        return _fin, False, -1
    # 発火時点の含み損益。スリッページは呼び出し側で建玉に対して引く
    return float(_path[_hit]), True, _hit


def _stat(days: list, level: float = 0.0, trail: float = 0.0,
          cap: float = 0.0):
    """窓全体の集計。cap は建玉(スリッページの基準)。"""
    _tot, _fire, _saved = 0.0, 0, 0.0
    _daily = []
    for _d in days:
        _ts, _path, _fin, _n = _days[_d]
        _v, _f, _i = _apply(_d, level, trail)
        if _f:
            # 建玉に対するスリッページ。全銘柄を成行で畳むので個別より不利に見積もる
            _v -= float(_NOTIONAL.get(_d, 0.0)) * a.slip_pct
            _fire += 1
            _saved += (_v - _fin)
        _tot += _v
        _daily.append((_d, _v))
    _m: dict = {}
    for _d, _v in _daily:
        _m[_d[:7]] = _m.get(_d[:7], 0.0) + _v
    _mv = np.array([_m[k] for k in sorted(_m)], float)
    _mu = float(_mv.mean()) if len(_mv) else 0.0
    _sd = float(_mv.std(ddof=1)) if len(_mv) > 1 else 0.0
    return {"tot": _tot, "fire": _fire, "saved": _saved,
            "mu": _mu, "sd": _sd, "ratio": (_mu / _sd if _sd else 0.0),
            "pos": int((_mv > 0).sum()), "nm": len(_mv),
            "worst": float(_mv.min()) if len(_mv) else 0.0}


def _judge(_s: dict, _b: dict) -> str:
    """✅ の判定。

    ⛔ 2026-08-28: `ratio > base * 1.10` と書いていたので、**base がマイナスの
       ときに不等号が反転**し、発火0回(=現行と完全に同一)の行に ✅ が付いた。
       比率ではなく **差** で比べ、発火が無い行は必ず — にする。
    """
    if _s["fire"] <= 0:
        return "  —"
    _d = _s["ratio"] - _b["ratio"]
    return "  ✅" if _d >= 0.10 else ""


def _table(days: list, label: str):
    print(f"\n  ── {label} ({len(days)}営業日) ──")
    _b = _stat(days)
    print(f"    {'ルール':<22}{'合計':>13}{'月平均':>11}{'月次σ':>11}"
          f"{'÷σ':>7}{'発火':>6}{'最悪月':>12}")
    print(f"    {'損切りなし ★現行':<22}{_b['tot']:>+13,.0f}{_b['mu']:>+11,.0f}"
          f"{_b['sd']:>11,.0f}{_b['ratio']:>7.2f}{'—':>6}"
          f"{_b['worst']:>+12,.0f}")
    _out = []
    for _l in _LV:
        _s = _stat(days, level=_l)
        _mk = _judge(_s, _b)
        print(f"    {'合計 −' + f'{_l:,.0f}円':<22}{_s['tot']:>+13,.0f}"
              f"{_s['mu']:>+11,.0f}{_s['sd']:>11,.0f}{_s['ratio']:>7.2f}"
              f"{_s['fire']:>6}{_s['worst']:>+12,.0f}{_mk}")
        _out.append(("level", _l, _s))
    for _t in _TR:
        _s = _stat(days, trail=_t)
        _mk = _judge(_s, _b)
        print(f"    {'最大益から −' + f'{_t:,.0f}円':<22}{_s['tot']:>+13,.0f}"
              f"{_s['mu']:>+11,.0f}{_s['sd']:>11,.0f}{_s['ratio']:>7.2f}"
              f"{_s['fire']:>6}{_s['worst']:>+12,.0f}{_mk}")
        _out.append(("trail", _t, _s))
    return _b, _out


print(f"\n{'=' * 78}")
print(f"■ ポートフォリオ損切り — **合計**で判定する(個別ではない)")
print(f"{'=' * 78}")
print(f"  ⛔ 個別の損切りは全滅している(§18.55 / §18.56)。合計で判定すると"
      f"**銘柄固有のノイズが打ち消し合う**ので別物")
print(f"  ⚠ 見るのは **月平均÷σ**。合計だけで選ぶと『降りて損を減らした』が"
      f"『利益も減らした』を隠す")

_btr, _otr = _table(_tr, f"TRAIN {_tr[0]}〜{_tr[-1]}")
_bte, _ote = _table(_te, f"TEST  {_te[0]}〜{_te[-1]}")

# ── ★ 核心の診断: 発火した日、引けまで持っていたらどうだったか ────────
print(f"\n{'=' * 78}")
print(f"■ ★ 発火した日、引けまで持っていたらどうだったか")
print(f"{'=' * 78}")
print(f"  §18.55 で『早く降りるほど悪い』と出ている(5分 +10.3bp → 引け +17.8bp)。")
print(f"  **悪い日は最後まで悪いのか、それとも戻すのか** — これが本質的な問い")
print(f"    {'水準':<14}{'発火':>6}{'降りた損益':>13}{'引けまで':>13}"
      f"{'差':>13}{'正解率':>8}")
for _l in _LV:
    _f, _cut_pnl, _hold_pnl, _win = 0, 0.0, 0.0, 0
    for _d in _ks:
        _v, _fired, _i = _apply(_d, level=_l)
        if not _fired:
            continue
        _ts, _path, _fin, _n = _days[_d]
        _v -= float(_NOTIONAL.get(_d, 0.0)) * a.slip_pct
        _f += 1
        _cut_pnl += _v
        _hold_pnl += _fin
        if _v > _fin:
            _win += 1
    if not _f:
        print(f"    {'−' + f'{_l:,.0f}円':<14}{0:>6}{'—':>13}")
        continue
    print(f"    {'−' + f'{_l:,.0f}円':<14}{_f:>6}{_cut_pnl:>+13,.0f}"
          f"{_hold_pnl:>+13,.0f}{_cut_pnl - _hold_pnl:>+13,.0f}"
          f"{_win / _f * 100:>7.0f}%")
print(f"  ⚠ 『正解率』= 降りたほうが良かった日の割合。50%前後なら**コイン投げ**")
print(f"  ⚠ 全期間(TRAIN+TEST)の集計。採否は上の TRAIN/TEST 表で判断すること")

# ══════════════════════════════════════════════════════════════════════
# ★★ 決済時刻 — 引け成行より早く畳んだらどうか (2026-09-04 ユーザー発案)
# ══════════════════════════════════════════════════════════════════════
#   「明らかに、引け成行より 15:20 とかに売る方が多く利益が出てる気がする」
#
#   ⛔ これは損切りではない。**発火条件が無く、毎日必ず その時刻に畳む**。
#     上の損切り表と混ぜないこと(あちらは条件付き)。
#
#   ⚠ 非対称なコストがある。**引け成行(MOC)は板寄せなのでスプレッドを払わない**
#     (約定値 = その日の終値 = バックテストの基準そのもの)。早く畳むのは
#     ザラ場の成行なので **必ずスプレッドを払う**。--slip-pct を掛けるのは
#     早い側だけ。ここを揃えると早い側が不当に有利に出る。
_ET = [t.strip() for t in a.exit_times.split(",") if t.strip()]


def _tod(x):
    return pd.Timestamp(x).time()


# ★ 5分足の時刻ラベルが『バーの開始』か『終了』かを**実データで**確かめる。
#   開始ラベルなら 15:20 のバーの終値は **15:25 の価格**。ここを取り違えると
#   「15:20 に売る」が実際には別の時刻になる(§18.32 の教訓)。
_lastlab: dict = {}
for _d in _ks:
    _k = _days[_d][0][-1].strftime("%H:%M")
    _lastlab[_k] = _lastlab.get(_k, 0) + 1
_lab = sorted(_lastlab.items(), key=lambda x: -x[1])
print(f"\n{'=' * 78}")
print(f"■ ★★ 決済時刻 — 引け成行より早く畳んだらどうか")
print(f"{'=' * 78}")
print(f"  5分足の最終バーのラベル: "
      + " / ".join(f"{k} ({v}日)" for k, v in _lab[:3]))
print(f"    → 最終バーの終値 = その日の終値。**このラベル以降を指定しても"
      f"引けと同じ**になります")


def _exit_stat(days: list, hhmm: str = ""):
    """毎日 hhmm に全部畳んだときの集計。hhmm が空なら引け(=現行)。"""
    _tot, _same, _late = 0.0, 0, 0
    _daily, _labs = [], {}
    for _d in days:
        _ts, _path, _fin, _n = _days[_d]
        if not hhmm:
            _v = _fin
        else:
            _t = _tod(f"2000-01-01 {hhmm}")
            _idx = [k for k, x in enumerate(_ts) if x.time() <= _t]
            if not _idx:
                _late += 1                      # その時刻より前のバーが無い
                _v = _fin
            else:
                _j = _idx[-1]
                _labs[_ts[_j].strftime("%H:%M")] = \
                    _labs.get(_ts[_j].strftime("%H:%M"), 0) + 1
                if _j == len(_ts) - 1:
                    _same += 1                  # 実質 引けと同じ
                _v = float(_path[_j]) \
                    - float(_NOTIONAL.get(_d, 0.0)) * a.slip_pct
        _tot += _v
        _daily.append((_d, _v))
    _m: dict = {}
    for _d, _v in _daily:
        _m[_d[:7]] = _m.get(_d[:7], 0.0) + _v
    _mv = np.array([_m[k] for k in sorted(_m)], float)
    _mu = float(_mv.mean()) if len(_mv) else 0.0
    _sd = float(_mv.std(ddof=1)) if len(_mv) > 1 else 0.0
    _bar = max(_labs.items(), key=lambda x: x[1])[0] if _labs else "—"
    return {"tot": _tot, "mu": _mu, "sd": _sd,
            "ratio": (_mu / _sd if _sd else 0.0),
            "pos": int((_mv > 0).sum()), "nm": len(_mv),
            "worst": float(_mv.min()) if len(_mv) else 0.0,
            "same": _same, "late": _late, "bar": _bar, "n": len(days)}


def _etable(days: list, label: str) -> tuple:
    print(f"\n  ── {label} ({len(days)}営業日) ──")
    _b = _exit_stat(days)
    print(f"    {'決済':<10}{'使うバー':>10}{'合計':>13}{'月平均':>11}"
          f"{'月次σ':>11}{'÷σ':>7}{'最悪月':>12}{'引けとの差':>13}")
    print(f"    {'引け ★現行':<10}{_b['bar']:>10}{_b['tot']:>+13,.0f}"
          f"{_b['mu']:>+11,.0f}{_b['sd']:>11,.0f}{_b['ratio']:>7.2f}"
          f"{_b['worst']:>+12,.0f}{'—':>13}")
    _r = {}
    for _t in _ET:
        _s = _exit_stat(days, _t)
        _r[_t] = _s
        _note = ""
        if _s["same"] >= len(days) * 0.5:
            _note = f"  ⛔実質引け({_s['same']}日)"
        elif _s["ratio"] - _b["ratio"] >= 0.10:
            _note = "  ✅"
        print(f"    {_t:<10}{_s['bar']:>10}{_s['tot']:>+13,.0f}"
              f"{_s['mu']:>+11,.0f}{_s['sd']:>11,.0f}{_s['ratio']:>7.2f}"
              f"{_s['worst']:>+12,.0f}{_s['tot'] - _b['tot']:>+13,.0f}{_note}")
    return _b, _r


_btr_e, _etr = _etable(_tr, f"TRAIN {_tr[0]}〜{_tr[-1]}")
_bte_e, _ete = _etable(_te, f"TEST  {_te[0]}〜{_te[-1]}")

print(f"\n  ⚠ **早い決済にだけ スリッページ {a.slip_pct * 100:.2f}% を掛けています。**")
print(f"     引け成行(MOC)は板寄せの単一価格なのでスプレッドを払いません。")
print(f"     揃えると早い側が不当に有利に出ます(実運用と食い違う)")


def _beat(t: str, key: str) -> bool:
    """TRAIN/TEST の両方で引けを上回ったか。実質引けの行は対象外。"""
    if not (_etr.get(t) and _ete.get(t)):
        return False
    if _etr[t]["same"] >= len(_tr) * 0.5 or _ete[t]["same"] >= len(_te) * 0.5:
        return False
    return (_etr[t][key] > _btr_e[key]) and (_ete[t][key] > _bte_e[key])


# ⛔ 採否は **月平均÷σ**。合計だけで見ると「降りて利益を増やした」と
#   「ばらつきを増やした」を区別できない。§18.38 で walk-forward が合計で
#   選んで σ 最悪の設定を掴んだのと同じ罠。両方出して食い違いを可視化する。
_ag_r = [t for t in _ET if _beat(t, "ratio")]
_ag_t = [t for t in _ET if _beat(t, "tot")]
print(f"\n  ★ TRAIN と TEST の**両方**で引けを上回った時刻")
print(f"      月平均÷σ（**これが採否の基準**）: "
      + (", ".join(_ag_r) if _ag_r else "**なし**"))
print(f"      合計（参考。これだけで選ばない）: "
      + (", ".join(_ag_t) if _ag_t else "なし"))
if _ag_r:
    print(f"  ★ §18.55 の『早く降りるほど悪い』と逆です。**引け直前だけ別**"
          f"かもしれないので、次は 5分刻みで詰める価値があります")
elif _ag_t:
    print(f"  ⚠ **合計では上回るが 月平均÷σ では上回りません。**"
          f"利益と一緒にばらつきも増えています。採用しないこと")
else:
    print(f"  ⛔ **どちらの基準でも上回った時刻はありません。**")
    print(f"     §18.55(5分 +10.3bp → 11:05 +10.5bp → 引け +17.8bp)と同じ向き。"
          f"引け成行のままでよい")

print(f"\n  {'=' * 68}")
print(f"  ★ 判定(§18.36 のルール):")
print(f"    ① **月平均÷σ が『損切りなし』より明確に高い**水準があるか")
print(f"    ② その水準が **TRAIN と TEST で同じ**か(違えば固定できない)")
print(f"    ③ 『引けまで持っていたら』の差がプラスか(降りて正解だったか)")
print(f"  ⛔ ①②が揃わなければ採用しない。合計が増えただけでは不十分")
print(f"  ⚠ 5分足は約2年しかないので、どちらの窓も1年程度。**弱い検定**です")
print(f"  {'=' * 68}")
