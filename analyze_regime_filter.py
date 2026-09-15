#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N の日次損益を「大局レジーム(上げ/横ばい/下げ)」で切って、
   **その日は建てない** というフィルターが成立するかを検定する。

きっかけ (2026-09-01 ユーザー質問):
    レポートの月別表で 2026/06(上げ) が最良 +497,050、2026/02(上げ) が最悪
    −183,365。同じラベルの中で最良も最悪も出ている。月次13点では何も言えない
    ので、**日次**で・作法どおりに測り直す。

使い方:
    # ① 日次損益を出す(レポート側)
    $env:LSS_NEWGAP_DAYS_CSV = "n_days.csv"
    .\\dailyfast --days 730 --no-serve
    $env:LSS_NEWGAP_DAYS_CSV = $null

    # ② 検定
    python analyze_regime_filter.py --days-csv n_days.csv

⛔ レジームの判定式は nikkei_analysis.get_regime を **そのまま呼ぶ**。
   別実装にすると「レポートのラベル」と別物を検定することになる。

★ 守る作法 (CLAUDE.md §18.13 / §18.24 / §18.25 / §18.34b):
   1. **先読みなし**。各日のレジームは「その日までの終値」だけで判定する
   2. **日次で測る**。N は同日決済なので1日=1標本(月次13点では検出力ゼロ)
   3. **TRAIN/TEST を上限で切る**(下限だけ動かすと TRAIN ⊇ TEST になる)
   4. **帰無較正は日ブロックを保つ**。レジームは数週間続くので、日をばらばらに
      シャッフルすると帰無分布が狭くなり偽陽性を過小評価する
      → **巡回シフト**でラベルの持続構造ごとずらす
   5. 判定は「捨てた日の損益」ではなく **捨てたあとの合計** で見る
"""
from __future__ import annotations

import argparse
import math
import statistics as st
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(
    description="N の日次損益をレジームで切れるか検定する")
ap.add_argument("--days-csv", default="n_days.csv",
                help="LSS_NEWGAP_DAYS_CSV で出した日次損益")
ap.add_argument("--index", default="^N225", help="レジーム判定に使う指数")
ap.add_argument("--split", default="",
                help="TRAIN/TEST の境目 YYYY-MM-DD (既定=日数の前半/後半)")
ap.add_argument("--nulls", type=int, default=2000,
                help="帰無較正の巡回シフト回数")
a = ap.parse_args()


# ── レジーム判定は本体からそのまま借りる ─────────────────────────────
def _load_get_regime():
    """nikkei_analysis.get_regime を import せずに取り出す。

    ⛔ nikkei_analysis はトップレベルで重い処理をするので import しない。
      関数の定義だけを抜き出して exec する。
    """
    import re
    _src = Path(__file__).resolve().parent / "nikkei_analysis.py"
    _t = _src.read_text(encoding="utf-8")
    _m = re.search(r"\ndef get_regime\(close: pd\.Series\) -> dict:.*?"
                   r"(?=\n\n# ═)", _t, re.S)
    if not _m:
        sys.exit("[error] nikkei_analysis.get_regime を取り出せません")
    _ns = {"pd": pd, "np": np}
    exec(_m.group(0), _ns)
    return _ns["get_regime"]


get_regime = _load_get_regime()


def _fetch_index(sym: str, start) -> pd.Series:
    try:
        import yfinance as yf
    except Exception:
        sys.exit("[error] yfinance がありません")
    _d = yf.download(sym, start=str(start), progress=False, auto_adjust=False)
    if _d is None or _d.empty:
        sys.exit(f"[error] {sym} を取得できません")
    _c = _d["Close"]
    if isinstance(_c, pd.DataFrame):
        _c = _c.iloc[:, 0]
    return _c.dropna()


def _cluster(v: pd.Series) -> dict:
    """1日=1標本なので、そのまま平均・t・95%CI。"""
    _v = pd.Series(v).dropna().astype(float)
    _n = len(_v)
    if _n < 2:
        return {"n": _n, "mean": float(_v.mean()) if _n else 0.0,
                "t": 0.0, "lo": 0.0, "hi": 0.0}
    _m = float(_v.mean())
    _se = float(_v.std(ddof=1)) / math.sqrt(_n)
    return {"n": _n, "mean": _m, "t": (_m / _se) if _se else 0.0,
            "lo": _m - 1.96 * _se, "hi": _m + 1.96 * _se}


def main() -> int:
    _p = Path(a.days_csv)
    if not _p.exists():
        sys.exit(f"[error] {_p} がありません。先にレポート側で\n"
                 f"  $env:LSS_NEWGAP_DAYS_CSV = \"{_p.name}\"\n"
                 f"  .\\dailyfast --days 730 --no-serve\n"
                 f"を実行してください")
    d = pd.read_csv(_p)
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    d = d[d["built"] > 0].reset_index(drop=True)      # 建てなかった日は対象外
    if d.empty:
        sys.exit("[error] 建てた日が1日もありません")

    # ── 各日のレジーム(先読みなし: その日までの終値だけ) ──────────────
    _need = 260                                       # get_regime に必要な本数
    _idx = _fetch_index(a.index, d["date"].min() - pd.Timedelta(days=_need * 2))
    _reg = []
    for _dt in d["date"]:
        _c = _idx[_idx.index <= _dt]                  # ★ その日まで
        if len(_c) < 230:
            _reg.append("")
            continue
        try:
            _reg.append(get_regime(_c)["macro"])
        except Exception:
            _reg.append("")
    d["macro"] = _reg
    d = d[d["macro"] != ""].reset_index(drop=True)
    _JA = {"up": "上げ", "sideways": "横ばい", "down": "下げ"}

    print(f"\n■ N の日次損益 × 大局レジーム")
    print(f"  {len(d):,}営業日 / {d['date'].min():%Y-%m-%d}〜{d['date'].max():%Y-%m-%d}"
          f" / 合計 {d['pnl'].sum():+,.0f}円\n")
    print(f"  ⛔ レジームは **その日までの終値だけ**で判定(先読みなし)")
    print(f"  ⛔ 1日=1標本。月次13点では検出力がありません\n")

    # ── ① レジーム別 ────────────────────────────────────────────────
    print(f"{'レジーム':<8}{'日数':>6}{'勝日':>7}{'平均/日':>11}"
          f"{'t':>7}{'95%CI(円/日)':>24}{'合計':>13}")
    _base = _cluster(d["pnl"])
    for _k in ("up", "sideways", "down"):
        _g = d[d["macro"] == _k]
        if _g.empty:
            continue
        _s = _cluster(_g["pnl"])
        _w = (_g["pnl"] > 0).mean() * 100
        print(f"{_JA[_k]:<8}{_s['n']:>6}{_w:>6.0f}%{_s['mean']:>11,.0f}"
              f"{_s['t']:>7.2f}   [{_s['lo']:>+8,.0f},{_s['hi']:>+8,.0f}]"
              f"{_g['pnl'].sum():>13,.0f}")
    print(f"{'全体':<8}{_base['n']:>6}{(d['pnl'] > 0).mean() * 100:>6.0f}%"
          f"{_base['mean']:>11,.0f}{_base['t']:>7.2f}"
          f"   [{_base['lo']:>+8,.0f},{_base['hi']:>+8,.0f}]"
          f"{d['pnl'].sum():>13,.0f}")

    # ── ② 「そのレジームの日は建てない」を試す ──────────────────────
    print(f"\n■ 『そのレジームの日は建てない』としたら\n")
    print(f"{'捨てる':<10}{'残る日数':>8}{'合計':>13}{'全体との差':>13}"
          f"{'平均/日':>10}{'差のt':>8}")
    _res = {}
    for _k in ("up", "sideways", "down"):
        _g = d[d["macro"] != _k]
        if _g.empty or (d["macro"] == _k).sum() == 0:
            continue
        _s = _cluster(_g["pnl"])
        _diff = _g["pnl"].sum() - d["pnl"].sum()
        # 差の t = 捨てた日の平均が 0 と違うか(捨てる=その日を失う)
        _drop = _cluster(d[d["macro"] == _k]["pnl"])
        _res[_k] = _diff
        print(f"{_JA[_k] + 'を捨てる':<10}{_s['n']:>8}{_g['pnl'].sum():>13,.0f}"
              f"{_diff:>+13,.0f}{_s['mean']:>10,.0f}{-_drop['t']:>8.2f}")

    # ── ③ 帰無較正(日ブロックを保つ巡回シフト) ──────────────────────
    print(f"\n■ 帰無較正 — レジームの並びを **巡回シフト** ({a.nulls:,}回)")
    print(f"  ⛔ ばらばらにシャッフルしない。レジームは数週間続くので、"
          f"持続構造を壊すと\n     帰無分布が狭くなり偽陽性を過小評価する(§18.13)\n")
    _pnl = d["pnl"].to_numpy(float)
    _lab = d["macro"].to_numpy()
    _n = len(d)
    _rng = np.random.default_rng(42)
    for _k in ("up", "sideways", "down"):
        if (_lab == _k).sum() == 0:
            continue
        _act = _res.get(_k)
        if _act is None:
            continue
        _null = []
        for _ in range(a.nulls):
            _sh = int(_rng.integers(1, _n))
            _l2 = np.roll(_lab, _sh)
            _null.append(float(_pnl[_l2 != _k].sum() - _pnl.sum()))
        _null = np.array(_null)
        _p = float((np.abs(_null) >= abs(_act)).mean())
        _z = ((_act - _null.mean()) / _null.std(ddof=1)) if _null.std(ddof=1) else 0.0
        _v = "✅ 帰無の外" if _p < 0.05 else "⛔ 帰無の中"
        print(f"  {_JA[_k] + 'を捨てる':<12}実測 {_act:>+11,.0f}  "
              f"帰無 中央 {np.median(_null):>+11,.0f}  "
              f"95%点 {np.percentile(np.abs(_null), 95):>10,.0f}  "
              f"z={_z:>+5.2f}  両側p={_p:.3f}  {_v}")

    # ── ④ TRAIN / TEST ─────────────────────────────────────────────
    _cut = (pd.Timestamp(a.split) if a.split
            else d["date"].iloc[len(d) // 2])
    print(f"\n■ TRAIN / TEST (境目 {_cut:%Y-%m-%d} / **上限で切る**)")
    print(f"  ⛔ 下限だけずらすと TRAIN ⊇ TEST になる(§18.25 の事故)\n")
    print(f"{'捨てる':<10}{'TRAIN 差':>13}{'(該当日)':>8}"
          f"{'TEST 差':>13}{'(該当日)':>8}{'符号一致':>9}")
    for _k in ("up", "sideways", "down"):
        if (d["macro"] == _k).sum() == 0:
            continue
        _o, _cnt = [], []
        for _s in (d[d["date"] < _cut], d[d["date"] >= _cut]):
            _o.append(_s[_s["macro"] != _k]["pnl"].sum() - _s["pnl"].sum())
            _cnt.append(int((_s["macro"] == _k).sum()))
        # ⚠ 片側に該当日がほとんど無ければ、符号一致を見ても意味が無い。
        _thin = min(_cnt) < 10
        _ok = ("⚠ 日数不足" if _thin
               else ("✅" if (_o[0] > 0) == (_o[1] > 0) else "⛔"))
        print(f"{_JA[_k] + 'を捨てる':<10}{_o[0]:>+13,.0f}{_cnt[0]:>8}"
              f"{_o[1]:>+13,.0f}{_cnt[1]:>8}{_ok:>9}")
    print(f"\n  ⚠ 『該当日』が片側10日未満なら符号一致は判定材料になりません"
          f"(レジームは数週間\n     続くので、期間の切り方次第で片側に寄ります)")

    print(f"\n■ 判定")
    print(f"  採用できるのは次を **すべて** 満たすときだけです:")
    print(f"    1. 捨てたあとの合計が **増える**")
    print(f"    2. 帰無較正で **両側 p < 0.05**")
    print(f"    3. TRAIN と TEST で **符号が一致**")
    print(f"  1つでも欠けたら、レジームでは切れないという結論です。")
    print(f"  ⚠ 参考: 同種の軸は 41本 + 交互作用820ペアを掃いて候補ゼロ"
          f"(§18.34b / §18.60)。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
