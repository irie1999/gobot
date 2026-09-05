#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新方式N の **計算の中身**。表示は持たない。

★ なぜ切り出すのか (2026-09-06)
  `.\dailyfast` は 5分足の lss/J/K タブが重く、N には1バイトも要らない
  (N は日足だけ)。資金スイープだけ見たいときに丸ごと回すのは無駄。
  → 計算をここに置き、`nikkei_analysis.py`(レポート) と
    `n_capital.py`(スタンドアロン) の **両方がこれを import** する。
  ⛔ コピペで二重管理にしない。CLAUDE.md §10 の `check_signal_on_date` が
     stop/breakout にコピペで残って食い違った前例がある。

含むもの: 定数 / 銘柄コード変換 / 日足スキャン / 予算シミュ / 資金スイープ
含まないもの: HTML・タブ・テキスト整形(それは呼び出し側)
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

_NG_RET1 = float(os.environ.get("LSS_NEWGAP_RET1", "1.753"))     # 前日リターン下限(%)
_NG_GAP_BP = float(os.environ.get("LSS_NEWGAP_BP", "100"))       # ギャップ下限(bp)
_NG_WATCH = int(os.environ.get("LSS_NEWGAP_WATCH", "50"))        # 朝読める上限(0=無制限)
_NG_BUDGET = float(os.environ.get("LSS_NEWGAP_BUDGET", "400"))   # 予算(万円)
_NG_QTY = 100
_NG_WORKERS = int(os.environ.get("LSS_NEWGAP_WORKERS", "8"))


def _newgap_yf(code: str) -> str:
    """J-Quants の5桁コード(末尾0)を yfinance の `NNNN.T` に直す。

    ⛔ これを忘れると `21200` のような **存在しないシンボル**を yfinance に
       投げることになり、全銘柄が `possibly delisted; no timezone found` で
       失敗する(2026-08-25 に実際やった)。`analyze_gap_edge._jq_to_yf` と同じ。
    """
    c = str(code).strip()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


def _newgap_scan_one(sym: str, days: int, min_price: float, max_price: float) -> list:
    """1銘柄の全営業日について (日付, 前日リターン, ギャップ, 損益, 流動性) を返す。

    ⛔ 5分足も lss のバックテストも使わない。日足の始値・終値だけ。
    ⚠ 流動性は **その日までの20日平均**(as-of)。`_liquidity_of` は今日の
       120日平均なので先読みになる(§18.51 B4)。ここでは使わない。
    """
    try:
        from backtest_limit_entry import fetch as _f
        # ⛔⛔ min_start_date を渡さないと **キャッシュの開始日を見ない**ので、
        #   days を伸ばしても黙って短いまま返る(§18.53 で判定を1回壊した)。
        import datetime as _dtm
        _msd = (_dtm.datetime.now().date()
                - _dtm.timedelta(days=int(days + 400)))
        df = _f(sym, days + 260, min_start_date=_msd)
    except Exception:
        return []
    if df is None or len(df) < 60:
        return []
    try:
        _idx = pd.to_datetime(df.index).normalize()
        _c, _o = df["close"], df["open"]
        _v = df["volume"] if "volume" in df.columns else pd.Series(0.0, index=df.index)
        _ret1 = _c.pct_change(1) * 100.0                 # D 時点で確定
        _turn = (_c * _v).rolling(20).mean()             # D 時点までの20日平均
    except Exception:
        return []
    out = []
    _cut = _idx[-1] - pd.Timedelta(days=days)
    for pos in range(21, len(_idx) - 1):
        d0 = _idx[pos]
        if d0 < _cut:
            continue
        try:
            pc = float(_c.iloc[pos])
            o1 = float(_o.iloc[pos + 1])
            c1 = float(_c.iloc[pos + 1])
            r1 = float(_ret1.iloc[pos])
            lq = float(_turn.iloc[pos])
        except Exception:
            continue
        if not (pc > 0 and o1 > 0 and c1 > 0 and r1 == r1):
            continue
        # ⛔⛔ ここで価格帯を切ってはいけない(2026-09-06 Codex 指摘、実在した)。
        #   o1 は **D+1 の始値** = 前夜には知りえない。しかも価格帯フィルタは
        #   watch50 より前に効くので、「前夜に選ぶ50件の顔ぶれ」が未来の始値で
        #   決まっていた = 先読み。価格帯は _newgap_build 側で
        #   **前日終値(pc)** に対して掛ける。
        if min_price > 0 or max_price < 1e11:
            if o1 < min_price or o1 > max_price:
                continue
        out.append({
            "date": str(_idx[pos + 1].date()),
            "symbol": sym,
            "ret1": r1,                                   # 前夜に確定
            "liq": lq if lq == lq else 0.0,               # 前夜に確定(as-of)
            "prev_close": pc,                             # ★ 前夜に確定(価格帯はこれで切る)
            "entry_p": o1,                                # D+1 の始値
            "gap_bp": (o1 - pc) / pc * 10_000.0,
            "pnl": (o1 - c1) * _NG_QTY,                   # 寄りで売って引けで買い戻す
        })
    return out


def _newgap_sim(rows: list, budget_man: float, watch: int,
                gap_bp: float, ret1_min: float,
                qty_mode: str = "fixed", qty: int = 0,
                max_pct: float = 0.0) -> dict:
    """日ごとに 候補 → watch上限 → ギャップ判定 → 予算 の順で建てる。

    ★ この順番が実運用そのもの。**watch上限を先に掛ける**のが肝で、
      「候補は多いが朝50件しか板を読めない」という制約をここで再現する。

    qty_mode:
      "fixed" … 常に qty 株(既定 _NG_QTY=100)。**現行**
      "equal" … 予算 ÷ その日の合格件数 を 100株単位で切り捨て(最低1単元)。
                ⛔ 合格1件の日に予算の全部が1銘柄に入る。max_pct で頭を切る
                   (§18.38 #3b で J が実際にこれを踏んだ)。
    max_pct: 1銘柄の上限(予算に対する%)。0=無制限。
    ⚠ 合格件数は 09:00 に確定するので、それで割るのは **先読みではない**。
    """
    _q0 = int(qty or _NG_QTY)
    if not rows:
        return {}
    _df = pd.DataFrame(rows)
    _cap = budget_man * 10_000.0
    _days, _det = [], []
    for _d, _g in _df.groupby("date"):
        # ① 前夜の候補(前日リターン)。ここまでは前夜に確定している
        _cand = _g[_g["ret1"] >= ret1_min]
        _n_cand = len(_cand)
        if _n_cand == 0:
            _days.append({"date": _d, "cand": 0, "watched": 0, "hit": 0,
                          "built": 0, "used": 0.0, "pnl": 0.0, "missed": 0})
            continue
        # ② 朝に板を読める上限(kabu 登録上限50件 / §18.44)。流動性降順
        _w = _cand.sort_values("liq", ascending=False, na_position="last")
        _watched = _w if watch <= 0 else _w.head(watch)
        # ③ 09:00 の始値でギャップ判定
        _hit = _watched[_watched["gap_bp"] >= gap_bp]
        # ④ watch で切り捨てたぶんのうち、本当は建てられた件数(機会損失)
        _missed = len(_cand[_cand["gap_bp"] >= gap_bp]) - len(_hit)
        # ⑤ 予算。ギャップ降順(強い順)に埋める
        _hit = _hit.sort_values("gap_bp", ascending=False)
        # ★ 資金均等: 予算 ÷ 合格件数(09:00 に確定するので先読みではない)
        _lot_cap = (_cap * max_pct / 100.0) if max_pct > 0 else _cap
        _slot = (_cap / max(1, len(_hit))) if qty_mode == "equal" else 0.0
        _cash, _p, _n = _cap, 0.0, 0
        for _r in _hit.itertuples():
            if qty_mode == "equal":
                _unit = float(_r.entry_p) * 100.0
                # 1銘柄の上限(予算比)。⛔ 無いと合格1件の日に全額が1銘柄へ
                _q = int(min(_slot, _lot_cap) // _unit) * 100
                _q = max(100, _q)             # 最低1単元は建てる
            else:
                _q = _q0
            _cost = float(_r.entry_p) * _q
            if _cost > _cash:
                continue                      # 貪欲(§18.33: レポート側に揃える)
            _cash -= _cost
            _p += float(_r.pnl) / _NG_QTY * _q     # pnl は100株ぶんで持っている
            _n += 1
            _det.append({"date": _d, "symbol": _r.symbol, "ret1": _r.ret1,
                         "gap_bp": _r.gap_bp, "entry_p": _r.entry_p,
                         "qty": _q,
                         "pnl": float(_r.pnl) / _NG_QTY * _q, "liq": _r.liq})
        _days.append({"date": _d, "cand": _n_cand, "watched": len(_watched),
                      "hit": len(_hit), "built": _n, "used": _cap - _cash,
                      "pnl": _p, "missed": _missed,
                      # ★ 予算で落とした件数。**これが 0 なら資金は律速していない**
                      "budget_drop": len(_hit) - _n})
    return {"days": pd.DataFrame(_days), "det": pd.DataFrame(_det)}


def _newgap_capital_sweep(rows: list, side: str) -> list:
    """★ 資金が増えたらどうなるか (2026-09-06 ユーザー仕様)。

    ★★ 本体は **追加枠**。総成績が良くても、最初の400万が稼いで追加分が
      負けている可能性がある。予算Bで建てた集合は予算A(<B)の**上位集合**に
      なる(同じ順で埋め、Bのほうが常に現金が多いため)ので、差分がそのまま
      「その追加資金が拾った取引」になる。
    ★ 第1段階は **100株固定のまま銘柄数だけ増やす**(集中を増やさない)。
      200株化は同じ表に混ぜない。損益も事故も2倍になり、100株で測った
      実約定データを転用できないため **別戦略**として下に分離する。
    ⛔ 1銘柄に200株以上入らないことは自動的に保証される
      ((日付,銘柄)は1行しか無いので、100株固定なら1銘柄=100株)。
    """
    import numpy as _np
    _maxp = float(os.environ.get("LSS_NEWGAP_CAP_MAXPCT", "25"))
    _INF = 1e6                                   # 万円。実質 無制限
    _bud = [float(x) for x in
            os.environ.get("LSS_NEWGAP_CAP_BUDGETS",
                           "400,600,800,1200,2000").split(",") if x.strip()]
    _bud = _bud + [_INF]
    _slips = [float(x) for x in
              os.environ.get("LSS_NEWGAP_CAP_SLIPS", "0,15,34").split(",")
              if x.strip()]
    _L: list = []
    _w = _L.append

    def _lbl(b: float) -> str:
        return "無制限" if b >= _INF else f"{b:,.0f}万"

    def _pnl_slip(_det, _bp: float):
        """執行差 _bp を引いた損益(円)。建玉に比例して引く。"""
        if _det.empty:
            return _det.assign(_p2=[])
        _q = _det["qty"] if "qty" in _det.columns else _NG_QTY
        return _det.assign(_p2=_det["pnl"] - _det["entry_p"] * _q * _bp / 1e4)

    def _stat(_dd, _det, _b: float) -> dict:
        _o: dict = {"b": _b}
        _o["n"] = int(len(_det))
        _o["nd"] = int(len(_dd))
        _o["per_day"] = _o["n"] / max(1, _o["nd"])
        _o["tot"] = float(_dd["pnl"].sum())
        _q = _det["qty"] if ("qty" in _det.columns and not _det.empty) else _NG_QTY
        _o["bp"] = (float((_det["pnl"] / (_det["entry_p"] * _q)).mean()) * 1e4
                    if not _det.empty else 0.0)
        _o["u_mean"] = float(_dd["used"].mean())
        _o["u_med"] = float(_dd["used"].median())
        _o["u_max"] = float(_dd["used"].max())
        # 「予算が効いた日」= 合格したのに建てられなかった日
        _o["bind_days"] = float((_dd["budget_drop"] > 0).mean()) * 100.0
        _o["drop"] = int(_dd["budget_drop"].sum())
        _m = _dd.copy()
        _m["month"] = _m["date"].str[:7]
        _mm = _m.groupby("month")["pnl"].sum()
        if len(_mm) > 2:
            _mm = _mm.iloc[:-1]                  # 最後の月は日数が欠ける
        _o["mu"] = float(_mm.mean()) if len(_mm) else 0.0
        _o["sd"] = float(_mm.std(ddof=1)) if len(_mm) > 1 else 0.0
        _o["ratio"] = (_o["mu"] / _o["sd"]) if _o["sd"] else 0.0
        _v = _dd["pnl"].to_numpy(dtype="float64")
        _s = _np.sort(_v)
        _o["worst"] = float(_s[0]) if len(_s) else 0.0
        _o["cvar"] = float(_s[:max(1, len(_s) // 20)].mean()) if len(_s) else 0.0
        # 最大ドローダウン(日次の累積曲線)
        _c = _np.cumsum(_v)
        _o["mdd"] = float((_c - _np.maximum.accumulate(_c)).min()) if len(_c) else 0.0
        # CVaR を投入額で正規化(規模が違う予算どうしを比べるため)
        _o["cvar_n"] = (_o["cvar"] / _o["u_med"] * 100.0) if _o["u_med"] else 0.0
        _h = len(_dd) // 2
        _o["h1"] = float(_dd["pnl"].iloc[:_h].sum())
        _o["h2"] = float(_dd["pnl"].iloc[_h:].sum())
        return _o

    _res: dict = {}
    for _b in _bud:
        try:
            _s = _newgap_sim(rows, _b, _NG_WATCH, _NG_GAP_BP, _NG_RET1,
                             qty_mode="fixed", qty=_NG_QTY, max_pct=0.0)
            if _s and not _s["days"].empty:
                _res[_b] = (_s["days"], _s["det"], _stat(_s["days"], _s["det"], _b))
        except Exception as _e:                       # noqa: BLE001
            _w(f"  ⛔ {_lbl(_b)}: {type(_e).__name__}: {str(_e)[:60]}")

    if not _res:
        return ["  対象なし"]
    _ks = sorted(_res)
    _base = _res[_ks[0]][2]

    # ── 表1: 銘柄数拡大型 ─────────────────────────────────────────
    _w("  ── 表1 銘柄数拡大型（100株固定・watch50固定・ギャップ降順）──")
    _w("     ⛔ 1銘柄=100株。(日付,銘柄)は1行しか無いので200株以上は入らない")
    _h1 = (f"    {'予算':<9}{'件数':>8}{'件/日':>7}{'投入平均':>11}{'投入中央':>11}"
           f"{'投入最大':>11}{'効いた日':>8}{'合計':>14}{'月平均':>11}{'bp/件':>8}"
           f"{'月次σ':>11}{'÷σ':>7}{'CVaR5%':>11}{'最悪日':>12}{'MaxDD':>13}")
    _w(_h1)
    _w("    " + "-" * (len(_h1) - 4))
    for _b in _ks:
        _o = _res[_b][2]
        _w(f"    {_lbl(_b):<9}{_o['n']:>8,}{_o['per_day']:>7.1f}"
           f"{_o['u_mean']:>11,.0f}{_o['u_med']:>11,.0f}{_o['u_max']:>11,.0f}"
           f"{_o['bind_days']:>7.0f}%{_o['tot']:>+14,.0f}{_o['mu']:>+11,.0f}"
           f"{_o['bp']:>+8.1f}{_o['sd']:>11,.0f}{_o['ratio']:>7.2f}"
           f"{_o['cvar']:>+11,.0f}{_o['worst']:>+12,.0f}{_o['mdd']:>+13,.0f}")
    _w("")
    _w("    効いた日 = 合格したのに予算で建てられなかった日の割合。")
    _w("    **これが小さければ資金は律速していない**(増資しても何も起きない)。")
    _w("    前半/後半:")
    for _b in _ks:
        _o = _res[_b][2]
        _sg = "✓同符号" if (_o["h1"] > 0) == (_o["h2"] > 0) else "⛔符号が逆"
        _w(f"      {_lbl(_b):<9} 前半 {_o['h1']:>+13,.0f} / 後半 "
           f"{_o['h2']:>+13,.0f}   {_sg}")
    _w("")

    # ── 表2: 追加枠だけ(本体) ──────────────────────────────────────
    _w("  ── 表2 ★★ 追加枠だけ（その追加資金が拾った取引だけを見る）──")
    _w("     予算Bの集合は予算A(<B)の上位集合。差分がそのまま『追加枠』。")
    _w("     ⛔ 総成績が良くても、稼いでいるのが最初の400万だけなら増資は無意味")
    _h2 = (f"    {'区間':<16}{'追加件数':>9}{'追加損益':>14}{'bp/件':>8}"
           f"{'前半bp':>9}{'後半bp':>9}{'勝率':>7}{'必要な追加資金':>15}{'判定':>6}")
    _w(_h2)
    _w("    " + "-" * (len(_h2) - 4))
    _marg: dict = {}
    for _i in range(1, len(_ks)):
        _a, _b = _ks[_i - 1], _ks[_i]
        _da, _db = _res[_a][1], _res[_b][1]
        _key = set(zip(_da["date"], _da["symbol"])) if not _da.empty else set()
        _mk = _db[[(_d, _s) not in _key
                   for _d, _s in zip(_db["date"], _db["symbol"])]] \
            if not _db.empty else _db
        _marg[(_a, _b)] = _mk
        if _mk.empty:
            _w(f"    {_lbl(_a) + '→' + _lbl(_b):<16}{0:>9,}{'—':>14}"
               f"{'—':>8}{'—':>9}{'—':>9}{'—':>7}"
               f"{(_b - _a) * 1e4:>15,.0f}{'⛔なし':>6}")
            continue
        _q = _mk["qty"] if "qty" in _mk.columns else _NG_QTY
        _bp = float((_mk["pnl"] / (_mk["entry_p"] * _q)).mean()) * 1e4
        _md = sorted(_mk["date"].unique())
        _cut = _md[len(_md) // 2]
        _f = _mk[_mk["date"] < _cut]
        _s2 = _mk[_mk["date"] >= _cut]
        _qf = _f["qty"] if "qty" in _f.columns else _NG_QTY
        _qs = _s2["qty"] if "qty" in _s2.columns else _NG_QTY
        _b1 = (float((_f["pnl"] / (_f["entry_p"] * _qf)).mean()) * 1e4
               if not _f.empty else float("nan"))
        _b2 = (float((_s2["pnl"] / (_s2["entry_p"] * _qs)).mean()) * 1e4
               if not _s2.empty else float("nan"))
        _ok = "✅" if (_b1 > 0 and _b2 > 0) else "⛔"
        _w(f"    {_lbl(_a) + '→' + _lbl(_b):<16}{len(_mk):>9,}"
           f"{float(_mk['pnl'].sum()):>+14,.0f}{_bp:>+8.1f}{_b1:>+9.1f}"
           f"{_b2:>+9.1f}{float((_mk['pnl'] > 0).mean()) * 100:>6.0f}%"
           f"{(_b - _a) * 1e4:>15,.0f}{_ok:>6}")
    _w("")
    _w("    判定 ✅ = 追加枠の bp が **前半・後半とも** プラス(採用基準①)")
    _w("")

    # ── 表3: 執行差の感度(追加枠) ─────────────────────────────────
    _w("  ── 表3 執行差を入れたら追加枠は残るか ──")
    _w("     ⚠ N の実約定は1件も無い。-10.4bp は N 母集団のモデル曲線、")
    _w("        -34bp は **J の実約定**(4営業日/10件)。同じ量ではない")
    _h3 = f"    {'区間':<16}" + "".join(f"{'-' + str(int(s)) + 'bp':>14}" for s in _slips)
    _w(_h3)
    _w("    " + "-" * (len(_h3) - 4))
    for (_a, _b), _mk in _marg.items():
        if _mk.empty:
            continue
        _cells = []
        for _sl in _slips:
            _adj = _pnl_slip(_mk, _sl)
            _cells.append(f"{float(_adj['_p2'].sum()):>+14,.0f}")
        _w(f"    {_lbl(_a) + '→' + _lbl(_b):<16}" + "".join(_cells))
    _w("")

    # ── 表4: 採用基準の判定 ───────────────────────────────────────
    _w("  ── 表4 採用基準（先に宣言した5条件）──")
    for _i in range(1, len(_ks)):
        _a, _b = _ks[_i - 1], _ks[_i]
        _mk = _marg.get((_a, _b))
        _ob = _res[_b][2]
        if _mk is None or _mk.empty:
            _w(f"    {_lbl(_a)}→{_lbl(_b)}: ⛔ 追加される取引がありません"
               f"(= 資金は律速していない)")
            continue
        _q = _mk["qty"] if "qty" in _mk.columns else _NG_QTY
        _md = sorted(_mk["date"].unique())
        _cut = _md[len(_md) // 2]
        _f, _s2 = _mk[_mk["date"] < _cut], _mk[_mk["date"] >= _cut]
        _qf = _f["qty"] if "qty" in _f.columns else _NG_QTY
        _qs = _s2["qty"] if "qty" in _s2.columns else _NG_QTY
        _b1 = (float((_f["pnl"] / (_f["entry_p"] * _qf)).mean()) * 1e4
               if not _f.empty else 0.0)
        _b2 = (float((_s2["pnl"] / (_s2["entry_p"] * _qs)).mean()) * 1e4
               if not _s2.empty else 0.0)
        _c1 = _b1 > 0 and _b2 > 0
        _c2 = _ob["ratio"] >= _base["ratio"] - 0.02
        _c3 = _ob["cvar_n"] >= _base["cvar_n"] - 0.05
        _s15 = float(_pnl_slip(_mk, 15.0)["_p2"].sum())
        _c4 = _s15 > 0
        _c5 = all(_res[_ks[_j]][2]["ratio"] >= _res[_ks[_j - 1]][2]["ratio"] - 0.03
                  for _j in range(1, _i + 1))
        _w(f"    {_lbl(_a)}→{_lbl(_b)}: "
           + " ".join(f"{'✅' if _c else '⛔'}{_n}" for _c, _n in
                      ((_c1, "①追加bp両期間+"), (_c2, "②÷σ非悪化"),
                       (_c3, "③正規化CVaR非悪化"), (_c4, "④-15bpでも+"),
                       (_c5, "⑤単調")))
           + ("   → **採用可**" if all((_c1, _c2, _c3, _c4, _c5))
              else "   → 不採用"))
    _w("")
    _w(f"    ②③の基準値: ÷σ {_base['ratio']:.2f} / "
       f"正規化CVaR {_base['cvar_n']:.2f}%(投入中央比) … いずれも 400万")
    _w("")

    # ── 表5: 参考(200株化) ───────────────────────────────────────
    _qs2 = [int(x) for x in
            os.environ.get("LSS_NEWGAP_CAP_QTYS", "200,300").split(",")
            if x.strip() and int(x) != _NG_QTY]
    if _qs2:
        _w("  ── 表5 参考: 200株以上（**別戦略**。上の表と混ぜない）──")
        _w("     ⛔ 損益も銘柄固有の事故も比例して増える。空売り在庫・部分約定・")
        _w("        板への影響が変わるので、100株で測った実約定を転用できない。")
        _w("     ⚠ ÷σ が 100株と同じなら **純レバレッジ**。改善ではない")
        _h5 = (f"    {'方式':<14}{'件数':>8}{'合計':>14}{'月平均':>11}{'月次σ':>11}"
               f"{'÷σ':>7}{'最悪日':>12}{'最大1銘柄':>12}")
        _w(_h5)
        _w("    " + "-" * (len(_h5) - 4))
        for _q2 in [_NG_QTY] + _qs2:
            try:
                _s = _newgap_sim(rows, 400.0, _NG_WATCH, _NG_GAP_BP, _NG_RET1,
                                 qty_mode="fixed", qty=_q2, max_pct=0.0)
                if not _s or _s["days"].empty:
                    continue
                _o = _stat(_s["days"], _s["det"], 400.0)
                _dt2 = _s["det"]
                _big = (float((_dt2["entry_p"] * _dt2["qty"]).max())
                        if not _dt2.empty else 0.0)
                _w(f"    {'400万/' + str(_q2) + '株':<14}{_o['n']:>8,}"
                   f"{_o['tot']:>+14,.0f}{_o['mu']:>+11,.0f}{_o['sd']:>11,.0f}"
                   f"{_o['ratio']:>7.2f}{_o['worst']:>+12,.0f}{_big:>12,.0f}")
            except Exception as _e:                   # noqa: BLE001
                _w(f"    400万/{_q2}株: ⛔ {str(_e)[:50]}")
        _w("")
    return _L
