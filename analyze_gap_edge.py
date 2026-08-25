r"""analyze_gap_edge.py — 「上げて寄った銘柄は、その日 下げるか」を最も安全な形で測る

════════════════════════════════════════════════════════════════════════════
★★★ 事前宣言 (2026-08-25)。**測る前に書いた。以後この基準を動かさない。**
════════════════════════════════════════════════════════════════════════════

これまで負けてきた原因は「測ってから良かった方を採る」を繰り返したこと。
方式を4回(lss→E→H→J / 2026-08-10〜16)、パラメータを数十回。その多重検定を
帰無較正していないので、勝った設定が本物かノイズか区別がつかなくなった。

**今回は測る前に合格ラインを決め、それをこのファイルに埋め込む。**
結果を見てからここを書き換えたら、この測定は無効になる。

── 仮説 (1つだけ) ──────────────────────────────────────────
  シグナルが出た銘柄が、翌営業日に **ギャップアップして寄る** と、
  その日の日中(寄り→引け)は下げる。

── 測るもの ────────────────────────────────────────────────
  C = (D+1 の始値 − D+1 の終値) × 100株     ← ショート。**バリアを1つも持たない**
      sm / tm / delay / 資金均等 / 予算 / 発注順 / 選定 が全部無い。
      だから **パラメータ選択のバイアスが構造的に入らない**。

── 母集団 (lss を1ミリも通らない) ──────────────────────────
  6戦略(MACDTF/A7/RSI2/DON/VOLTF/MOM)の `entry_sig` が True の日 D をすべて。
  **lss が約定したかどうかと無関係**。ここが §18.50 で汚染された部分:
    - `run_limit_backtest` の trade_log は「発注中」を捨てる
    - `scan_lss_universe._scan_symbol` も `reason in ("発注中","保有中")` を捨てる
    - `eh_trades` は trades+nofills から拾うので、期限切れが落ちていた
  → **どれも「ギャップアップして前日終値まで戻らなかった日」= ショートに有利な日
    を系統的に落としていた**。本ツールはシグナルから直接作るので起こりえない。

── 対照群 (これが無いと何も言えない) ───────────────────────
  同じ銘柄の **シグナルが出ていない日**。同じギャップ帯で同じ計算。
  §18.19 で「無条件の日中ドリフトはゼロ」(604,626銘柄日 / t=0.59)と確定済み。
  だから対照がプラスに出たら、それは測定の誤りかレジームであって仮説の支持ではない。

════════════════════════════════════════════════════════════════════════════
★ 合格ライン — **5つ全部**を満たしたときだけ「土台がある」と言う
════════════════════════════════════════════════════════════════════════════

  ① bp/件 ≥ +15.0bp
       呼値の往復が 6.6〜13.2bp(§18.31 / 建値2,600〜3,100円で2〜4ティック)。
       C は寄りと引けで2回板を叩くので、これを明確に超えないと執行で消える。
       ⚠ 呼値は下限。実スプレッドはこれ以上。

  ② 日クラスタ頑健 t ≥ +2.0
       同じ日の全銘柄はその日の方向を共有するので、実効サンプルは
       件数ではなく **営業日数**(§18.13)。件数ベースの t は使わない。

  ③ TRAIN と TEST の両方で ①② を満たす
       ホールドアウトは **上限で切る**(§18.25 の事故: 下限しか動かさず TRAIN ⊇ TEST
       になっていた)。窓を変えて TRAIN の数字が動くことを必ず確認する。

  ④ ギャップ帯で単調
       帯を7つ切れば偶然どれかは良く見える。**特定の帯を選ぶのは多重検定**。
       単調でなければ軸として機能していない(§18.31 の流動性・§18.38 の建値が
       まさにそうだった)。Spearman ≥ +0.7 を単調とみなす。

  ⑤ 対照群がゼロ (|t| < 2.0) かつ 帰無較正の95%点を超える
       帰無は **同じ日の中で帯ラベルをシャッフル**(日効果を完全に保つ / §18.48⑪)。
       日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなり、偽陽性を
       過小評価する(§18.13)。

  ⛔ 1つでも落ちたら **不合格。パラメータを足して救わない。**
     救おうとした瞬間に、これまでと同じ多重検定が始まる。

════════════════════════════════════════════════════════════════════════════
⚠ このツールが測っていないもの (合格しても残る未知数)
════════════════════════════════════════════════════════════════════════════
  * スプレッド・執行遅延 — §18.44 の実測で 1分 −15.8bp / 5分 −36.6bp。
    C の bp から**引く**こと。バックテストでは永遠に確定しない。
  * 空売り在庫・貸株可否 — 今日のリストしか無い(§18.51 B5)。
  * 生存バイアス — 今日の上場銘柄しか見ていない(§18.51 B6)。
  * 決算・特別気配・IPO — 全部建てる前提。

使い方
------
  python analyze_gap_edge.py --workers 8
  python analyze_gap_edge.py --workers 8 --split 2026-02-01     # TRAIN/TEST
  python analyze_gap_edge.py --workers 8 --limit 200            # デバッグ
  python analyze_gap_edge.py --workers 8 --no-control           # 対照を省く(速い)

⚠ 照会のみ。発注も設定変更もしない。日足だけで完結する(5分足不要)ので軽い。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# ── 事前宣言した合格ライン。**結果を見てから書き換えない** ──────────────
PASS_BP = 15.0        # ① bp/件 の下限 (呼値往復 6.6〜13.2bp を明確に超える)
PASS_T = 2.0          # ② 日クラスタ頑健 t の下限
PASS_RHO = 0.7        # ④ ギャップ帯の単調性 (Spearman)
CTRL_T = 2.0          # ⑤ 対照群がゼロと言える上限 |t|

# ギャップ帯 (bp)。J の合格判定 +75bp をまたぐように切ってある。
GAP_EDGES = [(-1e9, 0.0), (0.0, 25.0), (25.0, 50.0), (50.0, 75.0),
             (75.0, 100.0), (100.0, 150.0), (150.0, 1e9)]

ap = argparse.ArgumentParser(
    description="ギャップアップ後の日中ショートに地力があるかを、lss を通さず測る")
ap.add_argument("--days", type=int, default=800, help="遡及日数")
ap.add_argument("--split", type=str, default="",
                help="TRAIN/TEST の境界 yyyy-MM-dd (この日より前=TRAIN)。"
                     "空なら分割しない")
ap.add_argument("--min-price", type=float, default=1000.0, help="建値の下限")
ap.add_argument("--max-price", type=float, default=6000.0, help="建値の上限")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部)")
ap.add_argument("--seeds", type=int, default=200, help="帰無較正の本数")
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--no-control", action="store_true",
                help="対照群(シグナルが出ていない日)を計算しない。⛔ 合格判定はできない")
ap.add_argument("--out", type=str, default="", help="銘柄日の明細をCSVに書く")
a = ap.parse_args()

import backtest_limit_entry as ble                       # noqa: E402
from daytrade_data import available_local_symbols        # noqa: E402
from sameday5m_core import mod_for                       # noqa: E402

# エンジンのモードグローバルは触らせない(pnl は自前で日足から計算する)
ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._INTRADAY_5M = False

STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _jq_to_yf(code: str) -> str:
    c = str(code).strip()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


def _scan(sym: str) -> list[dict]:
    """1銘柄の全営業日について (ギャップ, 日中ショート損益, シグナル有無) を返す。

    ⛔ lss のバックテスト(run_limit_backtest)を**呼ばない**。日足のシグナル列だけを
       読むので、「lss が約定したか」は母集団に一切影響しない。
    """
    try:
        df = ble.fetch(sym, a.days + 420)
    except Exception:
        return []
    if df is None or len(df) < 250:
        return []
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()

    # 各戦略のシグナル日を集める(和集合。どれか1つでも出ていればシグナル日)
    sig_days: dict[pd.Timestamp, list[str]] = {}
    for st in STRATS:
        try:
            mod = mod_for(st)
            params = getattr(mod, "STRATEGY_PARAMS", {}).get(st)
            if not params:
                continue
            cf = params[0]
            d = cf(df.copy())
        except Exception:
            continue
        if "entry_sig" not in d.columns:
            continue
        try:
            for ts in d.index[d["entry_sig"].fillna(False).astype(bool)]:
                sig_days.setdefault(pd.Timestamp(ts).normalize(), []).append(st)
        except Exception:
            continue

    # 全営業日について D → D+1 を測る。シグナル日かどうかはフラグで持つだけ。
    out: list[dict] = []
    _idx = df.index
    _cut = _idx[-1] - pd.Timedelta(days=a.days)
    for pos in range(1, len(_idx) - 1):
        d0 = _idx[pos]                    # D  (シグナル判定日 = 終値で判定)
        if d0 < _cut:
            continue
        d1 = _idx[pos + 1]                # D+1(建てる日)
        try:
            pc = float(df["close"].iloc[pos])
            o1 = float(df["open"].iloc[pos + 1])
            c1 = float(df["close"].iloc[pos + 1])
        except Exception:
            continue
        if not (pc > 0 and o1 > 0 and c1 > 0):
            continue
        # 建値(= D+1 の始値)で価格フィルタ。実運用と同じく「その日いくらで建てるか」
        if o1 < a.min_price or o1 > a.max_price:
            continue
        _st = sig_days.get(d0, [])
        out.append({
            "date": str(d1.date()),
            "symbol": sym,
            "entry_p": o1,
            "gap_bp": (o1 - pc) / pc * 10_000.0,
            "pnl": (o1 - c1) * a.qty,          # C: 寄りで売って引けで買い戻す
            "sig": 1 if _st else 0,
            "strats": ",".join(sorted(set(_st))),
        })
    return out


def _band(g: float) -> str:
    for lo, hi in GAP_EDGES:
        if lo <= g < hi:
            return ("< 0" if hi == 0.0 else
                    f"{int(lo)}〜" if hi > 1e8 else f"{int(lo)}〜{int(hi)}")
    return "?"


def _cluster_t(sub: pd.DataFrame) -> float:
    """日クラスタ頑健 t。実効サンプル = 営業日数(件数ではない / §18.13)。"""
    if sub.empty:
        return 0.0
    dm = sub.groupby("date")["pnl"].mean()
    if len(dm) < 2:
        return 0.0
    sd = dm.std(ddof=1)
    if not (sd == sd) or sd <= 0:
        return 0.0
    return float(dm.mean() / (sd / (len(dm) ** 0.5)))


def _bp(sub: pd.DataFrame) -> float:
    if sub.empty:
        return 0.0
    return float((sub["pnl"] / (sub["entry_p"] * a.qty) * 10_000.0).mean())


def _spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0

    def _rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        for pos, i in enumerate(order):
            rk[i] = float(pos)
        return rk

    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return float(num / (dx * dy)) if dx > 0 and dy > 0 else 0.0


def _band_table(r: pd.DataFrame, title: str) -> list[tuple[str, int, float, float, float]]:
    """ギャップ帯別の表を出し、(帯, 件数, 円/件, bp, t) を返す。"""
    print(f"\n  {title}")
    print(f"    {'ギャップ(bp)':<13}{'件数':>9}{'円/件':>10}{'bp/件':>9}{'日t':>8}")
    print("    " + "-" * 49)
    rows = []
    for lo, hi in GAP_EDGES:
        lbl = ("< 0" if hi == 0.0 else
               f"{int(lo)}〜" if hi > 1e8 else f"{int(lo)}〜{int(hi)}")
        sub = r[r["band"] == lbl]
        if len(sub) < 30:                 # 30件未満は出さない(読み違えのもと)
            continue
        _b, _t = _bp(sub), _cluster_t(sub)
        rows.append((lbl, len(sub), float(sub["pnl"].mean()), _b, _t))
        print(f"    {lbl:<13}{len(sub):>9,}{sub['pnl'].mean():>+10,.0f}"
              f"{_b:>+9.1f}{_t:>+8.2f}")
    return rows


def _null_calib(r: pd.DataFrame, hi_lbl: str, lo_lbl: str) -> tuple[float, float, float]:
    """帰無較正: **同じ日の中で** 帯ラベルをシャッフル(日効果を完全に保つ)。

    ⛔ 日をまたぐシャッフルは日内相関を壊し、帰無分布が狭くなって偽陽性を
       過小評価する(§18.13)。日ブロックは必ず保つ。
    """
    import random as _rnd
    rng = _rnd.Random(a.seed)
    obs_hi = r[r["band"] == hi_lbl]
    obs_lo = r[r["band"] == lo_lbl]
    obs = _bp(obs_hi) - _bp(obs_lo)
    groups = [g for _, g in r.groupby("date")]
    spreads = []
    for _ in range(max(1, a.seeds)):
        hi_v, lo_v = [], []
        for g in groups:
            bands = list(g["band"])
            rng.shuffle(bands)
            _bpv = (g["pnl"] / (g["entry_p"] * a.qty) * 10_000.0).tolist()
            for bnd, v in zip(bands, _bpv):
                if bnd == hi_lbl:
                    hi_v.append(v)
                elif bnd == lo_lbl:
                    lo_v.append(v)
        if hi_v and lo_v:
            spreads.append(sum(hi_v) / len(hi_v) - sum(lo_v) / len(lo_v))
    if not spreads:
        return obs, 0.0, 0.0
    spreads.sort()
    p95 = spreads[min(len(spreads) - 1, int(len(spreads) * 0.95))]
    med = spreads[len(spreads) // 2]
    return obs, med, p95


# ── 実行 ────────────────────────────────────────────────────────────────
_seen, universe = set(), []
for s in available_local_symbols():
    y = _jq_to_yf(s)
    if y not in _seen:
        _seen.add(y)
        universe.append(y)
if a.limit > 0:
    universe = universe[:a.limit]
if not universe:
    sys.exit("[error] stock_5min に銘柄がありません(available_local_symbols が空)")

print(f"[info] 母集団 {len(universe):,}銘柄 × {len(STRATS)}戦略 / "
      f"遡及{a.days}日 / 建値 {a.min_price:,.0f}〜{a.max_price:,.0f}円 / {a.qty}株")
print(f"[info] 測るもの = C(寄りで売って引けで買い戻す)。"
      f"**sm/tm/delay/資金均等/予算/発注順/選定を1つも持たない**")
print(f"[info] 母集団は **シグナルから直接**作る(lss のバックテストを通らない / §18.50)")
print(f"[info] 合格ライン(事前宣言・不変): bp≥{PASS_BP} / t≥{PASS_T} / "
      f"単調ρ≥{PASS_RHO} / 対照|t|<{CTRL_T} / TRAIN・TEST 両方")

_rows: list[dict] = []
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    futs = {ex.submit(_scan, s): s for s in universe}
    for i, f in enumerate(as_completed(futs), 1):
        try:
            _rows.extend(f.result() or [])
        except Exception:
            pass
        if i % 200 == 0:
            print(f"  … {i}/{len(universe)}銘柄 / {len(_rows):,}銘柄日", flush=True)

if not _rows:
    sys.exit("[error] 1件も集まりませんでした")
r_all = pd.DataFrame(_rows)
r_all["band"] = r_all["gap_bp"].map(_band)
if a.out:
    r_all.to_csv(a.out, index=False, encoding="utf-8-sig")
    print(f"[info] 明細を書きました: {a.out} ({len(r_all):,}行)")

_sig = r_all[r_all["sig"] == 1]
_ctl = r_all[r_all["sig"] == 0]
print(f"\n{'=' * 78}")
print(f"■ 母集団 — シグナル {len(_sig):,}銘柄日 / 対照 {len(_ctl):,}銘柄日 / "
      f"{r_all['date'].nunique():,}営業日")
print(f"{'=' * 78}")
print(f"  ⚠ 対照は『同じ銘柄のシグナルが出ていない日』。§18.19 で無条件の日中ドリフトは"
      f"ゼロと確定済み\n    (604,626銘柄日 / t=0.59)。ここがプラスに出たら測定を疑うこと")

# ── TRAIN / TEST。上限で切る(§18.25) ────────────────────────────────
_windows = [("全期間", r_all)]
if a.split:
    _cut = str(pd.Timestamp(a.split).date())
    _tr = r_all[r_all["date"] < _cut]
    _te = r_all[r_all["date"] >= _cut]
    print(f"\n  TRAIN = date < {_cut} ({_tr['date'].nunique():,}営業日) / "
          f"TEST = date >= {_cut} ({_te['date'].nunique():,}営業日)")
    print(f"  ⚠ ホールドアウトは **上限で切る**。窓を変えて TRAIN の数字が動くことを"
          f"必ず確認すること(§18.25 の事故)")
    _windows += [("TRAIN", _tr), ("TEST", _te)]

_verdict: dict[str, dict] = {}
for _wname, _w in _windows:
    if _w.empty:
        continue
    print(f"\n{'=' * 78}\n■ {_wname}\n{'=' * 78}")
    _ws = _w[_w["sig"] == 1]
    _wc = _w[_w["sig"] == 0]
    _rows_sig = _band_table(_ws, f"シグナルあり ({len(_ws):,}銘柄日)")
    _rows_ctl = []
    if not a.no_control and not _wc.empty:
        _rows_ctl = _band_table(_wc, f"対照: シグナルなし ({len(_wc):,}銘柄日)")
    # 単調性(帯の中央値 vs bp)
    _rho = 0.0
    if len(_rows_sig) >= 3:
        _xs = list(range(len(_rows_sig)))
        _rho = _spearman([float(x) for x in _xs], [x[3] for x in _rows_sig])
    _all_bp, _all_t = _bp(_ws), _cluster_t(_ws)
    _ctl_bp = _bp(_wc) if not _wc.empty else 0.0
    _ctl_t = _cluster_t(_wc) if not _wc.empty else 0.0
    print(f"\n    {'合計(シグナルあり)':<22}{len(_ws):>9,}"
          f"{_ws['pnl'].mean():>+10,.0f}{_all_bp:>+9.1f}{_all_t:>+8.2f}")
    if not _wc.empty:
        print(f"    {'合計(対照)':<22}{len(_wc):>9,}"
              f"{_wc['pnl'].mean():>+10,.0f}{_ctl_bp:>+9.1f}{_ctl_t:>+8.2f}")
    print(f"    単調性 Spearman = {_rho:+.2f} (合格 ≥ {PASS_RHO})")
    _verdict[_wname] = {"bp": _all_bp, "t": _all_t, "rho": _rho,
                        "ctl_bp": _ctl_bp, "ctl_t": _ctl_t, "n": len(_ws),
                        "rows": _rows_sig}

# ── 帰無較正 (全期間・シグナルあり) ─────────────────────────────────
_null = None
if not _sig.empty and len(_verdict.get("全期間", {}).get("rows", [])) >= 2:
    _rws = _verdict["全期間"]["rows"]
    _hi, _lo = _rws[-1][0], _rws[0][0]
    print(f"\n{'=' * 78}\n■ 帰無較正 — 同じ日の中で帯ラベルをシャッフル ({a.seeds}本)\n{'=' * 78}")
    print(f"  ⛔ 日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなり、"
          f"偽陽性を過小評価する(§18.13)")
    _obs, _med, _p95 = _null_calib(_sig, _hi, _lo)
    print(f"\n  スプレッド『{_hi}』−『{_lo}』")
    print(f"    実測      {_obs:+8.1f} bp")
    print(f"    帰無 中央 {_med:+8.1f} bp   ← 0 中心とは限らない。ここと比べること")
    print(f"    帰無 95%  {_p95:+8.1f} bp")
    _null = (_obs, _med, _p95)
    print(f"    → {'✅ 帰無の95%点を超えた' if _obs > _p95 else '⛔ 帰無の中。軸として機能していない'}")

# ── 判定 (事前宣言した5条件。ここを書き換えたらこの測定は無効) ──────────
print(f"\n{'=' * 78}\n■ 判定 — 事前宣言した5条件\n{'=' * 78}")
_need = ["TRAIN", "TEST"] if a.split else ["全期間"]
_pass = []
for _k in _need:
    v = _verdict.get(_k)
    if not v:
        _pass.append((f"{_k}", False, "窓が空"))
        continue
    _pass.append((f"① {_k} bp/件 ≥ {PASS_BP}", v["bp"] >= PASS_BP, f"{v['bp']:+.1f}bp"))
    _pass.append((f"② {_k} 日クラスタ t ≥ {PASS_T}", v["t"] >= PASS_T, f"t={v['t']:+.2f}"))
    _pass.append((f"④ {_k} 単調 ρ ≥ {PASS_RHO}", v["rho"] >= PASS_RHO, f"ρ={v['rho']:+.2f}"))
if not a.no_control:
    v = _verdict.get("全期間", {})
    _ct = v.get("ctl_t", 0.0)
    _pass.append((f"⑤ 対照群がゼロ |t| < {CTRL_T}", abs(_ct) < CTRL_T,
                  f"t={_ct:+.2f} / {v.get('ctl_bp', 0):+.1f}bp"))
if _null:
    _pass.append(("⑤ 帰無の95%点を超える", _null[0] > _null[2],
                  f"{_null[0]:+.1f} vs {_null[2]:+.1f}bp"))
else:
    _pass.append(("⑤ 帰無較正", False, "計算できず"))
if a.no_control:
    _pass.append(("⑤ 対照群", False, "--no-control で省略した。合格判定はできない"))

for _lbl, _ok, _det in _pass:
    print(f"  {'✅' if _ok else '⛔'} {_lbl:<34}{_det}")
_ok_all = all(x[1] for x in _pass)
print(f"\n  {'=' * 60}")
if _ok_all:
    print(f"  ✅ **5条件すべて合格。土台がある。**")
    print(f"     次に測るのは執行コスト(§18.44: 1分 −15.8bp / 5分 −36.6bp)。")
    print(f"     上の bp から**引いて**、まだプラスかを見ること。")
    print(f"     ⛔ ここでパラメータ(sm/tm/delay/予算)を足して最適化しないこと。")
    print(f"        足した瞬間に、これまでと同じ多重検定が始まる。")
else:
    print(f"  ⛔ **不合格。この方向は追わない。**")
    print(f"     パラメータを足して救おうとしないこと。救おうとした瞬間に")
    print(f"     『測ってから良かった方を採る』が再開する(方式4回・パラメータ数十回の再現)。")
print(f"  {'=' * 60}")

print(f"""
{'=' * 78}
■ このツールが測っていないもの (合格しても残る未知数)
{'=' * 78}
  * スプレッド・執行遅延 — §18.44 実測 1分 −15.8bp / 3分 −29.4bp / 5分 −36.6bp。
    しかも 60〜71%が不利側。**上の bp から引くこと。**
  * 空売り在庫・貸株可否 — 今日のリストしか無い
  * 生存バイアス — 今日の上場銘柄しか見ていない
  * 決算・特別気配・IPO — 全部建てる前提
""")
