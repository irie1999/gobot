r"""sweep_oos_budget.py — 予算(建てられる件数)のスイープを、判定に必要な形で出す。

なぜ必要か
----------
`sim_oos_budget.py --budget N` を手で並べると合計損益しか見えない。予算を上げれば
取引が増えるので合計は必ず増える。**増えたぶんが本当に価値があるのか**を決めるには
3つが要る:

  ① 月次の t / 95%CI     … 10ヶ月の月次σは10万円規模(18.24)。合計の差だけでは
                            判定できない
  ② 限界トレードの 円/件 … 予算を上げて **新たに入った取引だけ** を取り出す。
                            合計ではなく、この限界の質が投資判断そのもの
  ③ 限界トレードの bp     … このシムは slip=0(18.21)。流動性降順で埋めるので
                            **増えたぶんは必ず薄い側**。限界の 円/件 を建玉で
                            割って bp に直し、**東証の呼値**(制度上の最小刻み)と
                            比べる。想定スプレッドではなく制度値を基準にするのは、
                            推測値で結論を動かさないため

さらに **証拠金のピーク**(同時保有の最大)も出す。予算を上げるとは口座に
その額を置くことなので、月平均の増加だけ見て決めてはいけない。

使い方
------
  python sweep_oos_budget.py --raw "oos_raw_fold*.csv"
  python sweep_oos_budget.py --raw "oos_raw_fold*.csv" --budgets 400,600,800,1200
  python sweep_oos_budget.py --raw "oos_raw_fold*.csv" --bt-min 30 --liq-floors 1,3,5,10

⚠ 発注順・BT下限・転換の扱いは `sim_oos_budget` と同一の関数を使う(乖離防止)。
   シムタイプは実運用に最も近い **通常予算**(不約定も枠を消費)のみ。
"""
from __future__ import annotations

import argparse
import glob as _glob
import statistics as _st
import sys
from collections import defaultdict

try:
    from sim_oos_budget import _bt_pass, _liq_of, _order_key, load_raw_csv
except Exception as e:  # pragma: no cover
    sys.exit(f"[error] sim_oos_budget を import できません: {e}")


def _tick(price: float) -> float:
    """東証の呼値の単位(通常銘柄)。TOPIX100 の特例は無視(こちらのほうが保守的)。"""
    if price <= 3_000:
        return 1.0
    if price <= 5_000:
        return 5.0
    if price <= 30_000:
        return 10.0
    return 50.0


def _liq(t) -> float:
    """発注順と同じ経路で流動性を引く(古い生CSVには liquidity 列が無い)。"""
    v = t.get("liquidity")
    if v in (None, "", 0, 0.0):
        v = _liq_of(str(t.get("symbol", "")))
    return float(v or 0.0)

ap = argparse.ArgumentParser(description="予算スイープを限界トレード込みで判定する")
ap.add_argument("--raw", required=True, help="生トレードCSV(グロブ可)")
ap.add_argument("--budgets", default="400,600,800,1200,1600",
                help="万円。カンマ区切り")
ap.add_argument("--bt-min", type=float, default=30.0)
ap.add_argument("--spread-bp", type=float, default=0.0,
                help="往復スプレッドの想定(bp)。既定0=使わない(判定は東証の呼値で行う)")
ap.add_argument("--liq-floors", default="",
                help="選定に流動性の足切りを入れた場合(億円, カンマ区切り)。"
                     "例 1,3,5,10。指定すると --budgets の先頭1つで比較する")
a = ap.parse_args()

files = sorted(_glob.glob(a.raw)) if any(c in a.raw for c in "*?[") else [a.raw]
if not files:
    sys.exit(f"[error] {a.raw} に一致するファイルがありません")
rows: list[dict] = []
for f in files:
    rows.extend(load_raw_csv(f))
print(f"[入力] {len(files)}ファイル / {len(rows):,}行  ({files[0]} 〜 {files[-1]})")

# fold × OOS月 × 日 でグルーピング(sim_oos_budget.run_sim と同じ切り方)
groups: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
for r in rows:
    groups[(r["fold"], r["train_months"], r["oos_month"])][r["entry_date"]].append(r)


def _sim(budget: float, liq_floor: float = 0.0) -> dict:
    """通常予算(不約定も枠消費)。選ばれた取引そのものを返す。

    liq_floor > 0 なら、**候補に入る前に** 売買代金がその額未満の銘柄を落とす。
    = 選定の段階で流動性の足切りを入れた場合の再現(発注順の変更とは別物)。
    """
    picked: list[dict] = []
    by_month: dict[str, float] = defaultdict(float)
    peaks: list[float] = []
    for key, by_date in groups.items():
        for dstr, day in by_date.items():
            cand = [t for t in day
                    if t.get("strategy") != "転換" and _bt_pass(t, a.bt_min)]
            if liq_floor > 0:
                cand = [t for t in cand if _liq(t) >= liq_floor]
            cand.sort(key=_order_key)
            used = held = 0.0
            for t in cand:
                cost = t["entry_p"] * 100
                if cost <= 0:
                    cost = budget
                if used + cost > budget:
                    break
                used += cost
                if t["filled"] == 1:
                    picked.append(t)
                    by_month[key[2]] += t["pnl"]
                    held += cost        # 実際に建った額 = 証拠金の実需
            peaks.append(held)
    peaks.sort()
    return {"picked": picked, "by_month": dict(by_month),
            "peak": peaks[-1] if peaks else 0.0,
            "p95": peaks[int(len(peaks) * 0.95)] if peaks else 0.0}


def _key(t) -> tuple:
    return (t["fold"], t["oos_month"], t["entry_date"], t["symbol"], t["strategy"])


budgets = [float(x) for x in a.budgets.split(",") if x.strip()]
res = {b: _sim(b * 10_000) for b in budgets}

print(f"\n■ 予算スイープ (BT>={a.bt_min:.0f} / 通常予算 / 転換除外 / "
      f"{len(next(iter(res.values()))['by_month'])}ヶ月)")
print(f"  {'予算':>7}{'取引':>7}{'勝率':>7}{'合計':>13}{'月平均':>11}"
      f"{'月次t':>7}{'95%CI(月)':>26}{'建玉P95':>10}{'建玉最大':>10}")
for b in budgets:
    r = res[b]
    pk = r["picked"]
    m = list(r["by_month"].values())
    mu = _st.mean(m) if m else 0.0
    sd = _st.stdev(m) if len(m) > 1 else 0.0
    se = sd / (len(m) ** 0.5) if len(m) > 1 else 0.0
    t = mu / se if se > 0 else 0.0
    lo, hi = mu - 1.96 * se, mu + 1.96 * se
    wr = sum(1 for x in pk if x["pnl"] > 0) / len(pk) * 100 if pk else 0.0
    print(f"  {b:>6,.0f}万{len(pk):>7,}{wr:>6.1f}%{sum(m):>+13,.0f}{mu:>+11,.0f}"
          f"{t:>+7.2f}{f'{lo:+,.0f} 〜 {hi:+,.0f}':>26}"
          f"{r['p95']/10_000:>8,.0f}万{r['peak']/10_000:>8,.0f}万")

# ── 限界トレード: 予算を上げて **新たに入った取引だけ** ────────────────
print(f"\n■ 限界トレード (1段上げて新たに入ったぶんだけ)")
print(f"  {'区間':>14}{'件数':>7}{'勝率':>7}{'損益':>13}{'円/件':>9}"
      f"{'建値平均':>11}{'往復bp':>9}{'流動性中央':>12}")
_verdicts = []
for i in range(1, len(budgets)):
    lo_b, hi_b = budgets[i - 1], budgets[i]
    base = {_key(t) for t in res[lo_b]["picked"]}
    marg = [t for t in res[hi_b]["picked"] if _key(t) not in base]
    if not marg:
        print(f"  {f'{lo_b:,.0f}→{hi_b:,.0f}万':>14}{0:>7}   (増分なし)")
        continue
    pnl = sum(t["pnl"] for t in marg)
    per = pnl / len(marg)
    ep = sum(t["entry_p"] for t in marg) / len(marg)
    bp = per / max(ep * 100, 1) * 10_000
    wr = sum(1 for t in marg if t["pnl"] > 0) / len(marg) * 100
    liq = sorted(_liq(t) for t in marg)
    lq = liq[len(liq) // 2] / 1e8
    print(f"  {f'{lo_b:,.0f}→{hi_b:,.0f}万':>14}{len(marg):>7,}{wr:>6.1f}%"
          f"{pnl:>+13,.0f}{per:>+9,.0f}{ep:>10,.0f}円{bp:>8.1f}{lq:>10,.0f}億")
    _verdicts.append((lo_b, hi_b, per, bp, ep))

print(f"\n■ 判定")
print(f"  基準は**東証の呼値**から出す(想定値ではなく制度上の最小値)。"
      f"往復で板を1回ずつ叩くと 2ティック、")
print(f"  常に不利側で約定すると 4ティック。限界の bp がこれを下回るなら、"
      f"バックテスト上の増分は板の刻みに埋もれる。")
for lo_b, hi_b, per, bp, ep in _verdicts:
    tk = _tick(ep) / max(ep, 1) * 10_000          # 1ティック = 何bp
    if per <= 0:
        v = "⛔ 限界がマイナス。この増額は損"
    elif bp < tk * 2:
        v = f"⛔ {bp:.1f}bp < 2ティック({tk*2:.1f}bp)。**板の刻みに埋もれる**"
    elif bp < tk * 4:
        v = (f"△ {bp:.1f}bp。2ティック({tk*2:.1f}bp)は超えるが "
             f"4ティック({tk*4:.1f}bp)未満。実測で確かめてから")
    else:
        v = (f"✅ {bp:.1f}bp。4ティック({tk*4:.1f}bp)の"
             f"{bp/(tk*4):.1f}倍。板の刻みでは説明できない")
    if a.spread_bp > 0:
        v += f"  [--spread-bp {a.spread_bp:.0f} なら " \
             + ("可" if bp >= a.spread_bp else "不可") + "]"
    print(f"  {lo_b:,.0f}→{hi_b:,.0f}万  {v}")

# ── 選定に流動性の足切りを入れた場合 ─────────────────────────────
if a.liq_floors.strip():
    _floors = [float(x) for x in a.liq_floors.split(",") if x.strip()]

    # ① 予算 × 足切り の行列。足切りは候補を減らすので予算の効き方も変える。
    #    片方ずつ動かして決めると、相互作用を見落とす。
    print(f"\n■ 予算 × 流動性足切り (合計損益 / 10ヶ月)")
    print(f"  {'予算':>8}{'足切りなし':>13}" +
          "".join(f"{f'{f:,.0f}億':>13}" for f in _floors))
    _mat: dict[tuple, dict] = {}
    for b in budgets:
        line = f"  {b:>6,.0f}万{sum(t['pnl'] for t in res[b]['picked']):>+13,.0f}"
        for f in _floors:
            rr = _sim(b * 10_000, liq_floor=f * 1e8)
            _mat[(b, f)] = rr
            line += f"{sum(t['pnl'] for t in rr['picked']):>+13,.0f}"
        print(line)
    _best = max([(sum(t["pnl"] for t in res[b]["picked"]), b, 0.0) for b in budgets]
                + [(sum(t["pnl"] for t in _mat[(b, f)]["picked"]), b, f)
                   for b in budgets for f in _floors])
    print(f"  → 最良 予算{_best[1]:,.0f}万 × 足切り"
          f"{('なし' if _best[2] == 0 else f'{_best[2]:,.0f}億')}  {_best[0]:+,.0f}円")
    print(f"  ⚠ 18.24 のノイズ帯は σ=124,660円/10ヶ月。この表の差がそれ未満なら"
          f"『測れていない』。")

    # ② 流動性の帯ごとに、実際に建った取引を分解する。
    #    ⛔ 円/件 だけ見てはいけない。100株固定なので値がさ株ほど建玉が大きく、
    #       同じ%の値動きでも円が大きく出る。**bp(建玉比)を必ず併記する。**
    b0 = budgets[0]
    base_r = res[b0]
    print(f"\n■ 流動性の帯ごとの成績 (予算{b0:,.0f}万で実際に建った取引を分解)")
    print(f"  {'売買代金':>12}{'件数':>7}{'損益':>13}{'円/件':>9}{'bp/件':>8}"
          f"{'建値平均':>11}{'勝率':>7}{'日クラスタt':>11}")
    _edges = [0.0] + [f * 1e8 for f in _floors] + [float("inf")]
    _lbls = ([f"〜{_floors[0]:,.0f}億"]
             + [f"{_floors[i]:,.0f}〜{_floors[i+1]:,.0f}億"
                for i in range(len(_floors) - 1)]
             + [f"{_floors[-1]:,.0f}億〜"])
    _bands = []
    for _i, _lb in enumerate(_lbls):
        sub = [t for t in base_r["picked"]
               if _edges[_i] <= _liq(t) < _edges[_i + 1]]
        if not sub:
            continue
        pnl = sum(t["pnl"] for t in sub)
        per = pnl / len(sub)
        ep = sum(t["entry_p"] for t in sub) / len(sub)
        bp = per / max(ep * 100, 1) * 10_000
        wr = sum(1 for t in sub if t["pnl"] > 0) / len(sub) * 100
        # 日クラスタ頑健 t: lss は同日決済なので下げた日は全銘柄まとめて勝つ。
        # 件数で t を出すと実効サンプルを誤認する(18.13 の作法)。
        byd: dict[str, list] = defaultdict(list)
        for t in sub:
            byd[t["entry_date"]].append(t["pnl"])
        dm = [sum(v) / len(v) for v in byd.values()]
        tt = (_st.mean(dm) / (_st.stdev(dm) / (len(dm) ** 0.5))
              if len(dm) > 1 and _st.stdev(dm) > 0 else 0.0)
        _bands.append((_lb, len(sub), per, bp, tt))
        print(f"  {_lb:>12}{len(sub):>7,}{pnl:>+13,.0f}{per:>+9,.0f}{bp:>+8.1f}"
              f"{ep:>10,.0f}円{wr:>6.1f}%{tt:>+11.2f}")
    print(f"  ⚠ 円/件 は建玉の大きさに比例する(100株固定)。**bp/件 が単調でなければ"
          f"、円/件 の単調は建値の単調にすぎない。**")

    # ③ 帰無較正: 日ごとに流動性ラベルだけシャッフルする。
    #    日内の相関(その日の相場)は保ったまま「流動性と損益の対応」だけ壊す。
    _obs = _bands[-1][3] - _bands[0][3] if len(_bands) >= 2 else 0.0
    import random as _rnd
    _null = []
    _byday: dict[str, list] = defaultdict(list)
    for t in base_r["picked"]:
        _byday[t["entry_date"]].append(t)
    for _s in range(200):
        rg = _rnd.Random(_s)
        lo_v, hi_v = [], []
        for day in _byday.values():
            liqs = [_liq(t) for t in day]
            rg.shuffle(liqs)
            for t, lq in zip(day, liqs):
                r_ = t["pnl"] / max(t["entry_p"] * 100, 1) * 10_000
                (lo_v if lq < _edges[1] else hi_v).append(r_)
        if lo_v and hi_v:
            _null.append(sum(hi_v) / len(hi_v) - sum(lo_v) / len(lo_v))
    if _null:
        _null.sort()
        # ⛔ 帰無は 0 中心ではない。流動性降順で埋めているため、薄い帯の取引は
        #    『予算が飽和しなかった日』からしか出てこない。その日の偏り(= 18.24 で
        #    確認した日の重み付け)だけで差が生まれる。だから 0 との比較ではなく
        #    **帰無分布の中での位置**で判定する。
        _ge = sum(1 for x in _null if x >= _obs) / len(_null)
        _p = 2 * min(_ge, 1 - _ge)
        print(f"\n  帰無較正 (日ごとに流動性ラベルだけシャッフル ×{len(_null)})")
        print(f"    実測の差 (最上位帯 − 最下位帯, bp/件)  {_obs:+.1f}bp")
        print(f"    帰無 (日の構成だけで出る差)  中央 {_null[len(_null)//2]:+.1f}bp"
              f"   90%区間 [{_null[int(len(_null)*0.05)]:+.1f} 〜 "
              f"{_null[int(len(_null)*0.95)]:+.1f}]")
        print(f"    **実測は帰無の上位 {_ge*100:.1f}% / 両側p = {_p:.3f}**  "
              + ("→ 偶然では出ない。流動性に識別力がある"
                 if _p < 0.05 else
                 "→ 偶然でも出る。18.13/18.24 の『候補ゼロ』と矛盾しない"))

    # ④ 候補ペア数(スキャン計算量)
    print(f"\n  足切りごとの候補ペア数 (= スキャン計算量。成績とは別の価値)")
    for f in [0.0] + _floors:
        n = len({(t["symbol"], t["strategy"]) for t in rows
                 if t.get("strategy") != "転換" and _bt_pass(t, a.bt_min)
                 and _liq(t) >= f * 1e8})
        print(f"    {('なし' if f == 0 else f'{f:,.0f}億'):>8}  {n:>6,}ペア")

print(f"\n{'─'*78}")
print("■ 読み方")
print(f"{'─'*78}")
print("  ・合計は予算を上げれば必ず増える。**限界トレードの 円/件 と bp** で判断する。")
print("  ・流動性降順で埋めるので、増えたぶんは必ず薄い側。このシムは slip=0 なので")
print("    その執行コストが一切入っていない(18.21)。bp がスプレッド想定を下回るなら")
print("    バックテスト上の増分は現実には存在しない。")
print("  ・証拠金ピークは口座に置く必要のある額。月平均の増加と必ず並べて見ること。")
print("  ・月次tが2に届かないなら、その水準自体がまだ実証されていない(18.24)。")
print("  ⚠ 実測のスプレッドは .\\fills の slip_daily_log.csv からしか出ない。")
print("    --spread-bp は想定値であって測定値ではない。")
