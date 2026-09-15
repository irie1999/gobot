#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""1分足2年で **指値@始値の約定率と取り逃しの損益** を測る（発注しない）。

★ なぜ要るのか
  §18.70② は「約定率を上げても損益は改善しない」と結論したが、実データは
  **2026年8月の歩み値・20営業日・181注文だけ**。1ヶ月＝1レジームで、しかも
  判定に使った帯（ランダム順 σ=35,576）は **発注順のばらつき**であって
  指値深さの検定になっていない。→ §18.70② は
  **「8月では差が小さかった」という暫定結果**に格下げする。

  1分足（~/.jquants_cache/minute / 2年ローリング / §18.6）があれば、
  歩み値を買い足さずに **20〜50倍のサンプル**で測り直せる。

★ 1分足で約定を判定するときの落とし穴（2026-09-15 レビュー指摘）
  ① 判定の起点は「09:01」ではなく **その銘柄が寄ったバーの次**。
     09:03 に寄った銘柄なら 09:04 以降。
  ② **寄りバーは使えない。** 高値には板寄せの約定（＝始値そのもの）が必ず
     含まれるので、使うと約定率が機械的に100%になる。
  ③ ライブの注文は 09:10 まで残っていなかった（N2 まで）。
     **取消時刻を2通り**測らないとライブと条件が合わない。

★ 3分類（1分足で分かるところまで正直に）
     約定      … 寄りの次のバー〜取消 のどれかで **高値 ≥ 始値**
     不約定    … 上が無く、かつ **寄りバーの高値 ≤ 始値**
                 （板寄せが寄りバーの高値＝その1分の中で始値以上の約定が
                   板寄せ以外に1件も無かった）
     判定不能  … 上が無く、寄りバーの高値 > 始値
                 （寄りの1分に始値以上の約定はあったが、自分の注文の
                   前か後か 1分足では分からない）
  → 判定不能は **楽観(約定扱い)と悲観(不約定扱い)の両方**で出す。

★ 3つの損益（同じ日・同じ予算・同じ発注順で）
     A 現行    … 約定と判定されたものだけ 始値で建てる
     B 理想上界… 合格を **全部** 始値で建てる（§18.70② が使った甘い前提）
     C 悲観    … 約定しなかったものを **寄りの次のバーの安値**で建てる
                 （ショートなので安く売るほど不利＝実行可能側の下界）

⛔ 採用条件（回す前に宣言。結果を見て緩めない）
     理想上界(B−A)でも **日クラスタ t < 2** なら現行維持。
     上界が有意でも、**悲観(C−A)と前半・後半の両方**で残らなければ変更しない。

⛔ 朝の発注コードには一切触らない。オフライン専用。

使い方:
    python check_1m_data.py --symbols symbols_listed_prime.py   # 先に在庫確認
    python analyze_fill_1m.py --days 760 --workers 8
    python analyze_fill_1m.py --days 760 --calibrate tick_truth.csv
"""
from __future__ import annotations

# ⛔ Windows で `> out.txt` にリダイレクトすると stdout が cp932 になり、
#   ⛔ ⚠ ✅ のような記号で UnicodeEncodeError を出して落ちる。
import console_safe  # noqa: F401

import argparse
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(
    description="1分足で 指値@始値 の約定率と取り逃しの損益を測る（発注しない）")
ap.add_argument("--days", type=int, default=760,
                help="遡る日数（1分足は2年ローリングなので760が上限目安）")
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--budget", type=float, default=400.0, help="予算(万円)")
ap.add_argument("--watch", type=int, default=50, help="朝に読める上限")
ap.add_argument("--gap-bp", type=float, default=100.0)
ap.add_argument("--ret1", type=float, default=1.753)
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--cancel", default="09:10",
                help="仕様どおりの取消時刻（既定 09:10）。**主判定はこれ1本**")
# ★ 締切スイープ (2026-09-15 レビュー指摘)。
#   「09:10 が仕様として正しい」ことと「09:10 が最も儲かる」ことは別問題。
#   ライブは 09:10 に固定したまま、**どこで切るのが良かったか**はここで測る。
#   ⛔ この表を見て --cancel を選び直さないこと。選び直すと同じ2年で
#     設定を決めることになる(§18.28 の作法)。判定は別に TRAIN/TEST を割る。
ap.add_argument("--cancel-sweep", default="09:03,09:05,09:10,09:15,09:30",
                help="締切をいくつか並べて約定率と損益を出す（比較用・主判定には使わない）")
ap.add_argument("--live-grace", type=int, default=11,
                help="ライブ再現の取消 = 最後のwatch銘柄が寄った時刻 + この秒数"
                     "（実測 2026-09-15: 09:02:59 に寄って 09:03:10 に取消）")
ap.add_argument("--split-tol-bp", type=float, default=200.0,
                help="1分足の寄り値と日足始値のズレがこれを超えたら **除外**"
                     "（1分足は分割未調整 / §18.27）")
ap.add_argument("--exec-bps", default="0,5,10,20",
                help="執行コストの感応度(bp)")
ap.add_argument("--calibrate", default="",
                help="正解CSV (date,symbol,filled) と突合して混同行列を出す")
ap.add_argument("--save", default="fill_1m_days.csv",
                help="日次の結果（空で書かない）")
ap.add_argument("--save-det", default="fill_1m_det.csv",
                help="銘柄日ごとの分類（較正に使う / 空で書かない）")
ap.add_argument("--limit", type=int, default=0, help="銘柄数を絞る(デバッグ)")
a = ap.parse_args()

# 締切スイープの時刻。主判定(--cancel)と重複しても害はないので除かない
# （同じ値が2行出ると「主判定と一致しているか」の検算になる）
_SWEEP = [t.strip() for t in a.cancel_sweep.split(",") if t.strip()]
for _t0 in _SWEEP:
    if len(_t0) != 5 or _t0[2] != ":" or not _t0.replace(":", "").isdigit():
        sys.exit(f"[error] --cancel-sweep の {_t0!r} は HH:MM ではありません")

# ══════════════════════════════════════════════════════════════════════
# 1分足の読み込み
# ══════════════════════════════════════════════════════════════════════
try:
    from tenkan_sim import find_minute_dirs
    _D1 = find_minute_dirs()[1]
except Exception:
    _D1 = None
if _D1 is None or not Path(_D1).exists():
    sys.exit("[error] 1分足のフォルダがありません。python check_1m_data.py で確認を")
_D1 = Path(_D1)


def _load_1m(sym: str):
    """<4桁>0_1m.pkl を読む。**キャッシュしない**（1銘柄で数十MB）。"""
    code = str(sym).upper().replace(".T", "")
    for _n in (f"{code}0_1m.pkl", f"{code}_1m.pkl"):
        p = _D1 / _n
        if p.exists():
            break
    else:
        return None
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
        if df is None or len(df) == 0:
            return None
        df.columns = [c.lower() for c in df.columns]
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("Asia/Tokyo")
        else:
            df.index = df.index.tz_convert("Asia/Tokyo")
        return df
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# 母集団 = N のバックテストと同じ（日足から直接）
# ══════════════════════════════════════════════════════════════════════
from newgap_core import _newgap_scan_one, _newgap_yf          # noqa: E402

try:
    from daytrade_data import available_local_symbols as _als
    _SYMS = sorted({_newgap_yf(s) for s in _als()})
except Exception as _e:                                       # noqa: BLE001
    sys.exit(f"[error] 銘柄リストを作れません: {_e}")
if a.limit:
    _SYMS = _SYMS[:a.limit]

print(f"■ 母集団 {len(_SYMS):,}銘柄 / 遡り {a.days}日 / "
      f"建値 {a.min_price:,.0f}〜{a.max_price:,.0f}円")
print(f"  条件: 前日リターン ≥ +{a.ret1}% / ギャップ ≥ +{a.gap_bp:.0f}bp / "
      f"watch{a.watch} / 予算 {a.budget:,.0f}万 / {a.qty}株固定")
print(f"  ⛔ 発注しません。1分足と日足を読むだけです")

from concurrent.futures import ThreadPoolExecutor as _TPE     # noqa: E402

_rows: list = []
with _TPE(max_workers=a.workers) as ex:
    for i, r in enumerate(ex.map(
            lambda s: _newgap_scan_one(s, a.days, a.min_price, a.max_price),
            _SYMS), 1):
        _rows.extend(r or [])
        if i % 300 == 0:
            print(f"  … {i}/{len(_SYMS)}銘柄 / {len(_rows):,}銘柄日", flush=True)
if not _rows:
    sys.exit("[error] 母集団が空です")
_df = pd.DataFrame(_rows)
print(f"  → {len(_df):,}銘柄日 / {_df['date'].nunique():,}営業日")

# ── 候補 → watch上限 → 合格（ライブと同じ順番）────────────────────────
_cand = _df[_df["ret1"] >= a.ret1].copy()
_watch: dict[str, pd.DataFrame] = {}
for _d, _g in _cand.groupby("date"):
    _w = _g.sort_values("liq", ascending=False, na_position="last")
    _watch[str(_d)] = _w if a.watch <= 0 else _w.head(a.watch)
print(f"  候補 {len(_cand):,}銘柄日 → watch{a.watch} で "
      f"{sum(len(v) for v in _watch.values()):,}銘柄日")

# ══════════════════════════════════════════════════════════════════════
# 1分足から必要な**スカラーだけ**取る
# ══════════════════════════════════════════════════════════════════════
#   ⛔ バーの DataFrame をそのまま持つと数百MBになる。必要なのは
#     (寄り時刻 / 寄り値 / 寄りバー高値 / 始値に最初に触れた時刻 / 次バー安値)
#     の5つだけ。
#   ⛔ 取消時刻を決めるのは「**最後に寄った watch 銘柄**」なので、
#     合格していない銘柄の寄り時刻も要る。
_need: dict[str, set] = defaultdict(set)
_op_of: dict = {}                    # (date, symbol) -> 日足の始値(=指値)
for _d, _w in _watch.items():
    for _r in _w.itertuples():
        _s = str(_r.symbol)
        _need[_s].add(_d)
        if float(_r.gap_bp) >= a.gap_bp:
            _op_of[(_d, _s)] = float(_r.entry_p)

print(f"\n■ 1分足を読みます（{len(_need):,}銘柄 / うち合格 {len(_op_of):,}銘柄日）")

_bar: dict = {}
_miss = {"ファイルなし": 0, "その日のバーなし": 0}


def _one(sym: str):
    df = _load_1m(sym)
    if df is None:
        return sym, None
    out = {}
    _dates = pd.Series(df.index.date, index=df.index)
    for _d in _need[sym]:
        try:
            _tgt = pd.Timestamp(_d).date()
        except Exception:
            continue
        _b = df[(_dates == _tgt).values]
        if len(_b) == 0:
            out[_d] = None
            continue
        # ★ 寄りバー = その日の最初の **出来高 > 0** のバー
        if "volume" in _b.columns:
            _nz = _b.index[_b["volume"].fillna(0) > 0]
            if len(_nz):
                _b = _b.loc[_nz[0]:]
        _o = _b.iloc[0]
        _rec = {"open_ts": _b.index[0], "o1_open": float(_o["open"]),
                "o1_high": float(_o["high"]),
                "touch_ts": None, "next_low": float("nan")}
        _op = _op_of.get((_d, sym))
        if _op is not None and len(_b) > 1:
            # ⛔ **寄りバーは使わない**(落とし穴②)。次のバー以降だけ見る
            _post = _b.iloc[1:]
            _rec["next_low"] = float(_post["low"].iloc[0])
            _hit = _post.index[_post["high"] >= _op - 1e-9]
            if len(_hit):
                _rec["touch_ts"] = _hit[0]
        out[_d] = _rec
    return sym, out


with _TPE(max_workers=a.workers) as ex:
    for _i, (_s, _o) in enumerate(ex.map(_one, list(_need)), 1):
        if _o is None:
            _miss["ファイルなし"] += len(_need[_s])
            continue
        for _d, _v in _o.items():
            if _v is None:
                _miss["その日のバーなし"] += 1
            else:
                _bar[(_d, _s)] = _v
        if _i % 200 == 0:
            print(f"  … {_i}/{len(_need)}銘柄", flush=True)

print(f"  読めた {len(_bar):,}銘柄日"
      + ("".join(f" / {k} {v:,}" for k, v in _miss.items() if v) or ""))

# ══════════════════════════════════════════════════════════════════════
# 判定と予算シミュ
# ══════════════════════════════════════════════════════════════════════
_CAPY = a.budget * 10_000.0
_days_out: list = []
_det: list = []
_skip_split = 0


def _cls_of(rec: dict, op: float, cut) -> str:
    """3分類。cut までに寄りの次のバーで始値に触れたか。"""
    _t = rec["touch_ts"]
    if _t is not None and _t <= cut:
        return "約定"
    # 板寄せが寄りバーの高値 = その1分に始値以上の約定が他に無い
    if rec["o1_high"] <= op + 1e-9:
        return "不約定"
    return "判定不能"


for _d in sorted(_watch):
    _w = _watch[_d]
    # ── ライブの取消時刻 = watch の中で **最後に寄った**時刻 + grace ──────
    _ots = [_bar[(_d, str(r.symbol))]["open_ts"]
            for r in _w.itertuples() if (_d, str(r.symbol)) in _bar]
    if not _ots:
        continue
    _live_cut = max(_ots) + pd.Timedelta(seconds=a.live_grace)
    try:
        _spec_cut = pd.Timestamp(f"{_d} {a.cancel}", tz="Asia/Tokyo")
    except Exception:
        continue

    _hits = []
    for _r in _w.itertuples():
        _s = str(_r.symbol)
        if float(_r.gap_bp) < a.gap_bp or (_d, _s) not in _bar:
            continue
        _b = _bar[(_d, _s)]
        _op = float(_r.entry_p)
        # ⛔ 1分足は **分割未調整**(§18.27)。日足始値と大きくずれたら捨てる
        if _op > 0 and abs(_b["o1_open"] - _op) / _op * 1e4 > a.split_tol_bp:
            _skip_split += 1
            continue
        _hits.append((_r, _b, _op))
    if not _hits:
        continue
    # ── ライブ順 = 寄り時刻グループ順 → グループ内 |ギャップ| 降順 ────────
    _hits.sort(key=lambda x: (x[1]["open_ts"], -float(x[0].gap_bp)))

    _row = {"date": _d, "watched": len(_w), "pass": len(_hits),
            "live_cut": str(_live_cut)[11:19], "spec_cut": a.cancel}
    # ★ 主判定は live / spec の2本。以降は締切スイープ(比較用)
    _scen = [("live", _live_cut), ("spec", _spec_cut)]
    for _ct in _SWEEP:
        try:
            _scen.append((f"c{_ct.replace(':', '')}",
                          pd.Timestamp(f"{_d} {_ct}", tz="Asia/Tokyo")))
        except Exception:
            pass
    for _cn, _cut in _scen:
        _cash = _CAPY
        _A = _Aopt = _B = _C = 0.0
        _nA = _nB = 0
        _cc = {"約定": 0, "不約定": 0, "判定不能": 0}
        for _r, _b, _op in _hits:
            _cost = _op * a.qty
            if _cost > _cash:
                continue              # 予算切れ。⛔ 埋め直さない(発注時に消費)
            _cash -= _cost
            _nB += 1
            _close = _op - float(_r.pnl) / a.qty         # ショート: 当日終値
            _k = _cls_of(_b, _op, _cut)
            _cc[_k] += 1

            def _pat(px: float, _c=_close) -> float:
                return (px - _c) * a.qty                 # ショートの損益

            _B += _pat(_op)                              # 理想上界
            if _k == "約定":
                _A += _pat(_op)
                _Aopt += _pat(_op)
                _C += _pat(_op)
                _nA += 1
            else:
                if _k == "判定不能":
                    _Aopt += _pat(_op)                   # 楽観
                _lo = _b["next_low"]
                _C += _pat(_op if (_lo != _lo) else _lo)  # 悲観(次バー安値)
            if _cn == "spec":
                _det.append({
                    "date": _d, "symbol": str(_r.symbol), "cls": _k,
                    "gap_bp": float(_r.gap_bp), "open_p": _op,
                    "o1_high": _b["o1_high"], "next_low": _b["next_low"],
                    "touch_ts": (str(_b["touch_ts"])[11:19]
                                 if _b["touch_ts"] is not None else ""),
                    "open_ts": str(_b["open_ts"])[11:19],
                    "pnl_at_open": _pat(_op),
                })
        _row.update({
            f"{_cn}_built": _nB, f"{_cn}_filled": _nA,
            f"{_cn}_A": _A, f"{_cn}_Aopt": _Aopt,
            f"{_cn}_B": _B, f"{_cn}_C": _C,
            f"{_cn}_約定": _cc["約定"], f"{_cn}_不約定": _cc["不約定"],
            f"{_cn}_判定不能": _cc["判定不能"]})
    _days_out.append(_row)

if not _days_out:
    sys.exit("[error] 判定できた日が1日もありません")
_R = pd.DataFrame(_days_out)
_DET = pd.DataFrame(_det)
print(f"\n■ 判定できた {len(_R):,}営業日 / {len(_DET):,}銘柄日"
      + (f" / ⛔ 分割ずれで除外 {_skip_split:,}銘柄日" if _skip_split else ""))


# ══════════════════════════════════════════════════════════════════════
def _t(v) -> tuple:
    v = list(v)
    n = len(v)
    if n < 2:
        return 0.0, 0.0, 0.0
    m = sum(v) / n
    sd = (sum((x - m) ** 2 for x in v) / (n - 1)) ** 0.5
    return m, sd, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


print(f"\n{'=' * 78}\n■ 約定の3分類（仕様 {a.cancel} 取消）\n{'=' * 78}")
_tot = len(_DET)
for _k in ("約定", "不約定", "判定不能"):
    _v = int((_DET["cls"] == _k).sum())
    print(f"  {_k:8} {_v:>7,}件  ({_v / max(1, _tot) * 100:5.1f}%)")
print(f"  ⚠ **判定不能** = 寄りの1分の中でしか始値以上の約定が無かったもの。"
      f"\n     1分足では自分の注文の前か後か分からないので、"
      f"楽観(A_opt)と悲観(A)の両方を出します")

print(f"\n{'=' * 78}\n■ 取消時刻で約定率がどう変わるか\n{'=' * 78}")
print(f"  ⛔ N2 までのライブは **watch の最後の銘柄が寄った時刻**で取消していた"
      f"（2026-09-15 発覚）。N3 で {a.cancel} に直した。その差:")
print(f"  {'取消':16}{'建てた':>9}{'約定':>9}{'約定率':>9}")
for _c, _lbl in (("live", "ライブ(N2)"), ("spec", f"仕様 {a.cancel}(N3)")):
    _nb, _nf = int(_R[f"{_c}_built"].sum()), int(_R[f"{_c}_filled"].sum())
    print(f"  {_lbl:16}{_nb:>9,}{_nf:>9,}{_nf / max(1, _nb) * 100:>8.1f}%")

# ── 締切スイープ（比較用。主判定には使わない） ─────────────────────────
if _SWEEP:
    print(f"\n{'=' * 78}\n■ 締切スイープ（**参考**。ライブは {a.cancel} 固定）"
          f"\n{'=' * 78}")
    print(f"  ⛔ 「{a.cancel} が仕様として正しい」と「{a.cancel} が最も儲かる」は"
          f"別問題。\n     ここは後者だけを見る表で、**この表で締切を選ばない**"
          f"（同じ2年で設定を決めることになる / §18.28）")
    print(f"\n  {'締切':>8}{'建てた':>9}{'約定':>9}{'約定率':>9}"
          f"{'A 現行':>16}{'C 悲観':>16}")
    for _ct in _SWEEP:
        _c = f"c{_ct.replace(':', '')}"
        if f"{_c}_built" not in _R.columns:
            continue
        _nb, _nf = int(_R[f"{_c}_built"].sum()), int(_R[f"{_c}_filled"].sum())
        print(f"  {_ct:>8}{_nb:>9,}{_nf:>9,}"
              f"{_nf / max(1, _nb) * 100:>8.1f}%"
              f"{_R[f'{_c}_A'].sum():>+16,.0f}{_R[f'{_c}_C'].sum():>+16,.0f}")
    print(f"\n  ⚠ 遅く切るほど約定率は上がるが、**無防備な時間も伸びる**"
          f"（建ててから引けMOC を置くまで watcher が無い / §18.46）。"
          f"\n     この表にその費用は入っていません")

print(f"\n{'=' * 78}\n■ ★ 主判定 — 日ごとの対応差（仕様 {a.cancel}）\n{'=' * 78}")
print(f"  A 現行(指値@始値) / B 理想上界(全部 始値) / C 悲観(未約定を次バー安値)")
_half = len(_R) // 2
_res = {}
for _lbl, _x, _y in (("B − A（理想上界）", "spec_B", "spec_A"),
                     ("C − A（実行可能な悲観）", "spec_C", "spec_A"),
                     ("A_opt − A（判定不能を約定扱い）", "spec_Aopt", "spec_A")):
    _dd = (_R[_x] - _R[_y]).tolist()
    _m, _sd, _tv = _t(_dd)
    _h1, _h2 = _t(_dd[:_half])[0], _t(_dd[_half:])[0]
    _same = (_h1 > 0) == (_h2 > 0)
    _res[_x] = (_m, _tv, _same)
    print(f"\n  {_lbl}")
    print(f"    平均 {_m:+,.0f}円/日 / 日クラスタ t = **{_tv:+.2f}** / "
          f"合計 {sum(_dd):+,.0f}円")
    print(f"    前半 {_h1:+,.0f} / 後半 {_h2:+,.0f}  "
          + ("✓同符号" if _same else "⛔符号が逆"))

print(f"\n{'=' * 78}\n■ 執行コスト感応度\n{'=' * 78}")
print(f"  ⚠ 取り逃した銘柄を拾うには **その銘柄ぶんの執行コスト**を払う。"
      f"建値ごとに引いた B − A:")
_add_amt = []           # 拾う銘柄の建玉合計(円) を日ごとに
for _d, _g in _DET.groupby("date"):
    _add_amt.append(float((_g[_g["cls"] != "約定"]["open_p"] * a.qty).sum()))
_add_amt = pd.Series(_add_amt, index=sorted(_DET["date"].unique()))
_baseBA = (_R.set_index("date")["spec_B"] - _R.set_index("date")["spec_A"])
_add_amt = _add_amt.reindex(_baseBA.index).fillna(0.0)
print(f"  {'bp':>5}{'B−A 合計':>16}{'日次t':>9}")
for _bp in [float(x) for x in a.exec_bps.split(",") if x.strip()]:
    _v = (_baseBA - _add_amt * _bp / 1e4).tolist()
    _m, _sd, _tv = _t(_v)
    print(f"  {_bp:>5.0f}{sum(_v):>+16,.0f}{_tv:>+9.2f}")

print(f"\n{'=' * 78}\n■ ⛔ 採用条件（回す前に宣言済み）\n{'=' * 78}")
_mB, _tB, _okB = _res["spec_B"]
_mC, _tC, _okC = _res["spec_C"]
print(f"  ① 理想上界(B−A)の日クラスタ t ≥ 2   … t={_tB:+.2f}  "
      f"{'✅' if _tB >= 2 else '⛔'}")
print(f"  ② 悲観(C−A)も プラス                … {_mC:+,.0f}円/日  "
      f"{'✅' if _mC > 0 else '⛔'}")
print(f"  ③ B−A が前半・後半で同符号          … {'✅' if _okB else '⛔'}")
if _tB < 2:
    print(f"\n  ▶ **現行維持**。理想的な上界でさえ t={_tB:+.2f} < 2。"
          f"\n    指値@始値のまま。§18.70② の暫定結論が2年でも支持されました")
elif _mC <= 0 or not _okB:
    print(f"\n  ▶ **現行維持**。上界は有意ですが "
          + ("悲観側で消えます" if _mC <= 0 else "半期で符号が逆です"))
else:
    print(f"\n  ▶ **変更を検討する価値あり**。ただし採用の前に予算・上限"
          f"キャンセル込みで再確認すること(§18.10)")

if a.save:
    _R.to_csv(a.save, index=False, encoding="utf-8-sig")
    print(f"\n  → {a.save} ({len(_R):,}日)")
if a.save_det:
    _DET.to_csv(a.save_det, index=False, encoding="utf-8-sig")
    print(f"  → {a.save_det} ({len(_DET):,}銘柄日 / 較正に使います)")

# ══════════════════════════════════════════════════════════════════════
# 較正（歩み値などの正解と突合）
# ══════════════════════════════════════════════════════════════════════
if a.calibrate:
    _p = Path(a.calibrate)
    if not _p.exists():
        print(f"\n⚠ {a.calibrate} がありません（較正は飛ばします）")
    else:
        import csv as _csv
        _truth = {}
        with open(_p, encoding="utf-8-sig", newline="") as f:
            for r0 in _csv.DictReader(f):
                _k = (str(r0.get("date") or "")[:10],
                      str(r0.get("symbol") or "").upper().replace(".T", ""))
                _truth[_k] = str(r0.get("filled") or "").strip() in (
                    "1", "True", "true", "○", "YES", "yes")
        print(f"\n{'=' * 78}\n■ 較正 — 1分足の分類 vs 正解 ({a.calibrate})"
              f"\n{'=' * 78}")
        print(f"  正解 {len(_truth):,}件"
              f"  ⚠ 列は (date, symbol, filled)。歩み値から作る場合は"
              f"\n     『注文時刻以降に 始値以上の約定があったか』で filled を立てる")
        _m = {}
        for _r in _DET.itertuples():
            _k = (str(_r.date)[:10], str(_r.symbol).upper().replace(".T", ""))
            if _k in _truth:
                _m[(_r.cls, _truth[_k])] = _m.get((_r.cls, _truth[_k]), 0) + 1
        if not _m:
            print(f"  ⛔ 突合できた銘柄日が **0件**。日付や銘柄コードの書式を"
                  f"確認してください（date は YYYY-MM-DD / symbol は 4桁 or 4桁.T）")
        else:
            print(f"\n  {'1分足の分類':12}{'正解=約定':>10}{'正解=不約定':>12}"
                  f"{'一致率':>9}")
            for _k in ("約定", "不約定", "判定不能"):
                _y, _n = _m.get((_k, True), 0), _m.get((_k, False), 0)
                _acc = (_y / (_y + _n) if _k == "約定" else
                        _n / max(1, _y + _n)) if (_y + _n) else 0.0
                print(f"  {_k:12}{_y:>10,}{_n:>12,}"
                      + (f"{_acc * 100:>8.1f}%" if _k != "判定不能" else
                         f"{'—':>9}"))
            _tot2 = sum(_m.values())
            _ok = _m.get(("約定", True), 0) + _m.get(("不約定", False), 0)
            _und = _m.get(("判定不能", True), 0) + _m.get(("判定不能", False), 0)
            print(f"\n  確定した分の一致率 "
                  f"**{_ok / max(1, _tot2 - _und) * 100:.1f}%** "
                  f"({_ok:,}/{_tot2 - _und:,}) / 判定不能 {_und:,}件")
            print(f"  ⚠ 一致率が低ければ、この1分足分類は使えません。"
                  f"上の主判定もそのぶん割り引くこと")
