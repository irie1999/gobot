"""
analyze_stop_hunt.py  ―  ストップ狩り(ヒゲ刈り)の実測 & 損切りルール A/B 比較
=================================================================
本番エンジン (backtest_limit_entry.run_limit_backtest) は一切変更せず、
実際に出たトレード1件ごとに「エントリーを固定したまま損切りルールだけ
差し替えて出口を再評価」することで、ストップ狩りのコストを定量化する。

なぜこれが忠実か:
  - エントリー(シグナル→逆指値→約定)は損切りルールに依存しない → 全モードで同一
  - 各ポジションの決済は他ポジションと独立 → 1トレード単位の出口再評価で十分

【2つの分析】
  分析1: ストップ狩り実測
    実際に「損切り」になったトレードのうち
      - 即反発  : 損切り当日の終値が既に損切り価格より上に戻っていた (典型的ヒゲ狩り)
      - 翌日反発: 翌営業日の終値が損切り価格より上
      - 目標到達余地: 損切り後、元の有効期限内に高値が当初の目標価格に到達していた
    の割合と、刈られ損の推定額を集計。

  分析2: 損切りルール A/B
    各トレードの出口を以下のルールで再シミュレートして損益を比較。
      intraday(現行): 安値<=損切り価格 で約定 (ヒゲでも発火)
      close         : 終値<=損切り価格 のときだけ約定 (引け判定・寄り引け)

【使い方】
  python analyze_stop_hunt.py                 # 全グループ・365日
  python analyze_stop_hunt.py --workers 8
  python analyze_stop_hunt.py --aggressive    # aggressive モードの WATCHLIST/パラメータで
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop            as _stop
import check_signals_breakout        as _brk
import check_signals_short           as _short
import check_signals_short_breakout  as _sbrk
from backtest_limit_entry import (
    fetch,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY,
    MAX_HOLD, ENTRY_EXPIRE,
)

JST = timezone(timedelta(hours=9))

GROUPS = [
    (_stop,  "逆指値B"),
    (_brk,   "BRK"),
    (_short, "ショート"),
    (_sbrk,  "ショートBRK"),
]

# hybrid モードのザラ場ハード損切り余裕 (損切り価格のさらに外側%)。main で上書き。
CRASH_PCT = 0.03

MODES = ["intraday", "close", "hybrid"]
MODE_LABELS = {"intraday": "intraday(現行)", "close": "close(終値)",
               "hybrid": "hybrid(終値+保険)"}


def _is_short_strat(strat: str) -> bool:
    return strat.endswith("_S")


# ── 1トレードの出口を指定ルールで再シミュレート ─────────────────────
def resim_exit(df, entry_dt, entry_p, sp, tp, qty, is_short, stop_mode,
               crash_pct=0.03):
    """エントリーを固定し、損切りルール stop_mode で出口を再評価。
    Returns dict(reason, pnl, hold_days, exit_dt) or None。
    target は常に「ザラ場の指値(resting limit)」として hi/lo で判定。
    stop だけルールを切り替える:
      intraday: 安値/高値が損切り価格にタッチ (現行エンジンと同一)
      close   : 終値が損切り価格を超えたときだけ (引け判定)
      hybrid  : 通常は close 判定。ただし損切り価格の crash_pct 下/上に
                ザラ場ハード損切り(暴落保険)を併設し、そこに触れたら即約定。
    """
    bars = df[df.index >= entry_dt]
    if len(bars) == 0:
        return None
    last_cl = float(bars.iloc[-1]["close"])
    # hybrid 用ハード損切り価格 (損切り価格のさらに crash_pct 外側)
    hard_sp = sp * (1.0 + crash_pct) if is_short else sp * (1.0 - crash_pct)
    for k in range(len(bars)):
        row = bars.iloc[k]
        op = float(row["open"]); hi = float(row["high"])
        lo = float(row["low"]);  cl = float(row["close"])
        hold_days = k

        hit_tgt = (hi >= tp) if not is_short else (lo <= tp)
        # 損切り判定 (mode別)
        hit_hard = False
        if stop_mode == "close":
            hit_stp = (cl <= sp) if not is_short else (cl >= sp)
        elif stop_mode == "hybrid":
            hit_hard = (lo <= hard_sp) if not is_short else (hi >= hard_sp)
            hit_close = (cl <= sp) if not is_short else (cl >= sp)
            hit_stp = hit_hard or hit_close
        else:  # intraday (現行エンジンと同一)
            hit_stp = (lo <= sp) if not is_short else (hi >= sp)

        exit_p = None; reason = None
        if hit_tgt:
            exit_p = tp; reason = "目標達成"
        elif hit_stp:
            reason = "損切り"
            if stop_mode == "hybrid" and hit_hard:
                # 暴落保険: ハード損切り価格にザラ場約定 (寄り割れは寄り)
                if is_short:
                    exit_p = op if op >= hard_sp else hard_sp * (1.0 + SLIPPAGE_STOP_PCT)
                else:
                    exit_p = op if op <= hard_sp else hard_sp * (1.0 - SLIPPAGE_STOP_PCT)
            elif stop_mode in ("close", "hybrid"):
                # 引け成行: 終値にスリッページ
                exit_p = cl * (1.0 + SLIPPAGE_STOP_PCT) if is_short else cl * (1.0 - SLIPPAGE_STOP_PCT)
            else:
                # 現行: 寄りが損切り価格を割っていれば寄り、でなければ損切り価格+スリッページ
                if is_short:
                    exit_p = op if op >= sp else sp * (1.0 + SLIPPAGE_STOP_PCT)
                else:
                    exit_p = op if op <= sp else sp * (1.0 - SLIPPAGE_STOP_PCT)
        elif hold_days >= MAX_HOLD:
            exit_p = cl; reason = "タイムカット"

        if reason:
            if not (entry_p * 0.1 <= exit_p <= entry_p * 10.0):
                return None
            fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
            pnl = ((entry_p - exit_p) if is_short else (exit_p - entry_p)) * qty - fee
            return dict(reason=reason, pnl=pnl, hold_days=hold_days,
                        exit_dt=bars.index[k])
    # 期限内に決済せず（データ末尾）→ 保有中として時価評価
    pnl = ((entry_p - last_cl) if is_short else (last_cl - entry_p)) * qty
    return dict(reason="保有中", pnl=pnl, hold_days=len(bars) - 1,
                exit_dt=bars.index[-1])


# ── 損切りトレードの反事実(刈られ損)を測定 ──────────────────────────
def measure_stop_hunt(df, t, is_short):
    """実際に損切りになった1トレードについて、刈られ損の兆候を測定。
    Returns dict or None。"""
    sp = t.get("order_stop"); tp = t.get("order_target")
    exit_dt = t.get("exit_dt")
    if sp is None or tp is None or exit_dt is None:
        return None
    # 損切り当日のバー
    try:
        same = df.loc[exit_dt]
    except Exception:
        return None
    cl_same = float(same["close"])
    # 即反発: 損切り当日の終値が損切り価格より「助かった側」に戻っている
    if is_short:
        immediate = cl_same < sp     # ショートは上で損切り → 終値が下なら戻り
    else:
        immediate = cl_same > sp     # ロングは下で損切り → 終値が上なら戻り

    # 損切り後〜元の有効期限(シグナル日+ENTRY_EXPIRE+MAX_HOLD)内のバー
    sig_dt = t.get("signal_dt")
    after = df[df.index > exit_dt]
    if sig_dt is not None:
        try:
            import pandas as pd
            win_end = pd.bdate_range(start=pd.to_datetime(sig_dt),
                                     periods=ENTRY_EXPIRE + MAX_HOLD + 1)[-1]
            after = after[after.index <= win_end]
        except Exception:
            pass

    next_day_rebound = False
    reached_target = False
    if len(after) > 0:
        nxt_cl = float(after.iloc[0]["close"])
        next_day_rebound = (nxt_cl < sp) if is_short else (nxt_cl > sp)
        if is_short:
            reached_target = bool((after["low"] <= tp).any())
        else:
            reached_target = bool((after["high"] >= tp).any())

    return dict(immediate=immediate,
                next_day_rebound=next_day_rebound,
                reached_target=reached_target)


# ── 1銘柄を処理 ───────────────────────────────────────────────────
def process_one(mod, label, sym, name, strat):
    bt = mod.backtest_one(sym, name, strat)
    if not bt:
        return None
    prs = bt.get("period_results", {})
    keys = [k for k in prs if isinstance(k, int)]
    if not keys:
        return None
    log = prs[max(keys)].get("trade_log", [])
    df = fetch(sym, 400)
    if df is None:
        return None
    is_short = _is_short_strat(strat)

    rows = []
    for t in log:
        if t.get("reason") == "発注中":
            continue
        entry_dt = t.get("entry_dt"); entry_p = t.get("entry_p")
        sp = t.get("order_stop"); tp = t.get("order_target")
        qty = t.get("qty")
        if not (entry_dt is not None and entry_p and sp and tp and qty):
            continue
        sims = {m: resim_exit(df, entry_dt, entry_p, sp, tp, qty, is_short, m,
                              crash_pct=CRASH_PCT) for m in MODES}
        if any(v is None for v in sims.values()):
            continue
        hunt = None
        if t.get("reason") == "損切り":
            hunt = measure_stop_hunt(df, t, is_short)
        rows.append(dict(
            label=label, sym=sym, name=name, strat=strat, is_short=is_short,
            entry_dt=entry_dt,
            real_reason=t.get("reason"), real_pnl=t.get("pnl", 0.0),
            sims=sims, hunt=hunt,
        ))
    return rows


def main():
    ap = argparse.ArgumentParser(description="ストップ狩り実測 & 損切りルールA/B")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--crash-pct", type=float, default=3.0,
                    help="hybrid: 損切り価格の外側%%にザラ場ハード損切り (デフォルト3%%)")
    ap.add_argument("--aggressive",   action="store_true")
    ap.add_argument("--conservative", action="store_true")
    args = ap.parse_args()

    global CRASH_PCT
    CRASH_PCT = args.crash_pct / 100.0

    print("=" * 90)
    print(f"ストップ狩り実測 & 損切りルール A/B   モード: {_stop.TRADING_MODE}")
    print("=" * 90)

    tasks = []
    for mod, label in GROUPS:
        for sym, name, strat in mod.WATCHLIST:
            tasks.append((mod, label, sym, name, strat))
    print(f"対象: {len(tasks)} 銘柄×戦略  (4グループ)")

    all_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, *task): task for task in tasks}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    all_rows.extend(r)
            except Exception as e:
                pass
            if done % 10 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} 完了", flush=True)

    if not all_rows:
        print("トレードが収集できませんでした。")
        return

    # ── 分析1: ストップ狩り実測 ──
    print("\n" + "=" * 90)
    print("【分析1】 ストップ狩り実測 (実際に損切りになったトレード)")
    print("=" * 90)

    def report_hunt(rows, title):
        stops = [r for r in rows if r["real_reason"] == "損切り" and r["hunt"]]
        n = len(stops)
        if n == 0:
            print(f"\n[{title}] 損切りトレードなし")
            return
        imm = sum(1 for r in stops if r["hunt"]["immediate"])
        nxt = sum(1 for r in stops if r["hunt"]["next_day_rebound"])
        tgt = sum(1 for r in stops if r["hunt"]["reached_target"])
        # 「刈られ損」推定: 当初目標に到達していた損切りトレードの実損失合計
        wasted = sum(r["real_pnl"] for r in stops if r["hunt"]["reached_target"])
        print(f"\n[{title}] 損切りトレード {n} 件")
        print(f"  即反発 (当日終値が損切り価格に復帰) : {imm:>3} 件 ({imm/n*100:5.1f}%)")
        print(f"  翌日反発 (翌営業日終値が復帰)       : {nxt:>3} 件 ({nxt/n*100:5.1f}%)")
        print(f"  目標到達余地 (損切り後に目標へ到達) : {tgt:>3} 件 ({tgt/n*100:5.1f}%)  ← 刈られ損の核心")
        print(f"  上記『目標到達余地あり』の実損失合計: {wasted:+,.0f} 円  (本来取れた可能性)")

    longs  = [r for r in all_rows if not r["is_short"]]
    shorts = [r for r in all_rows if r["is_short"]]
    report_hunt(all_rows, "全体")
    report_hunt(longs,   "ロング")
    report_hunt(shorts,  "ショート")

    # ── 分析2: 損切りルール 3-way 比較 (尾リスク付き) ──
    print("\n" + "=" * 90)
    print(f"【分析2】 損切りルール比較  intraday(現行) / close(終値) / hybrid(終値+{CRASH_PCT*100:.0f}%保険)")
    print("=" * 90)

    def stat(rows, mode):
        done = [r for r in rows if r["sims"][mode]["reason"] != "保有中"]
        n = len(done)
        pnls = [r["sims"][mode]["pnl"] for r in done]
        wins = sum(1 for p in pnls if p > 0)
        gp = sum(p for p in pnls if p > 0)
        gl = abs(sum(p for p in pnls if p < 0))
        pf = gp / gl if gl > 0 else float("inf")
        worst = min(pnls) if pnls else 0.0           # 最大単発損失 (尾リスク)
        bigloss = sum(1 for p in pnls if p < -20000)  # 2万円超の損失件数
        hold = sum(r["sims"][mode]["hold_days"] for r in done) / n if n else 0
        wr = wins / n * 100 if n else 0
        return dict(n=n, wr=wr, pf=pf, pnl=sum(pnls), hold=hold,
                    worst=worst, bigloss=bigloss)

    pf_s = lambda x: "∞" if x == float("inf") else f"{x:.2f}"

    def report_modes(rows, title):
        if not rows:
            return
        base = stat(rows, "intraday")
        print(f"\n[{title}]  (決済済みトレード)")
        print(f"  {'ルール':<18}{'件数':>5}{'勝率':>8}{'PF':>7}{'総損益':>14}"
              f"{'対現行':>12}{'最大損失':>11}{'2万損':>6}{'平均保有':>8}")
        print(f"  {'-'*89}")
        for m in MODES:
            s = stat(rows, m)
            diff = s["pnl"] - base["pnl"]
            diff_s = "—" if m == "intraday" else f"{diff:+,.0f}"
            print(f"  {MODE_LABELS[m]:<18}{s['n']:>5}{s['wr']:>7.1f}%{pf_s(s['pf']):>7}"
                  f"{s['pnl']:>+13,.0f}{diff_s:>12}{s['worst']:>+11,.0f}"
                  f"{s['bigloss']:>5}件{s['hold']:>6.1f}日")

    report_modes(all_rows, "全体")
    report_modes(longs,   "ロング")
    report_modes(shorts,  "ショート")

    # 戦略別 (総損益: 現行 → close → hybrid)
    print("\n  ── 戦略別 (総損益: 現行 → close → hybrid) ──")
    strats = sorted({r["strat"] for r in all_rows})
    for st in strats:
        rs = [r for r in all_rows if r["strat"] == st]
        si = stat(rs, "intraday"); sc = stat(rs, "close"); sh = stat(rs, "hybrid")
        print(f"  {st:<8} n={si['n']:<3} 勝率 {si['wr']:3.0f}%→{sc['wr']:3.0f}%→{sh['wr']:3.0f}%  "
              f"損益 {si['pnl']:+,.0f} → {sc['pnl']:+,.0f} → {sh['pnl']:+,.0f}  "
              f"(close{sc['pnl']-si['pnl']:+,.0f} / hyb{sh['pnl']-si['pnl']:+,.0f})")

    # ── 分析3: 時期分割の頑健性チェック (前半 vs 後半) ──
    # ルール変更(close化)の優位が特定時期だけの偶然でないかを確認。
    print("\n" + "=" * 90)
    print("【分析3】 時期分割 頑健性チェック (entry日で前半/後半に2分割)")
    print("=" * 90)
    dated = [r for r in all_rows if r.get("entry_dt") is not None]
    if dated:
        dates = sorted(r["entry_dt"] for r in dated)
        mid = dates[len(dates) // 2]
        early = [r for r in dated if r["entry_dt"] < mid]
        late  = [r for r in dated if r["entry_dt"] >= mid]

        def split_line(rows, tag):
            si = stat(rows, "intraday"); sc = stat(rows, "close")
            d = sc["pnl"] - si["pnl"]
            mark = "✓close優位" if d > 0 else "✗close劣後"
            print(f"  {tag:<22} n={si['n']:<4} "
                  f"intraday {si['pnl']:>+12,.0f}  →  close {sc['pnl']:>+12,.0f}  "
                  f"({d:>+11,.0f})  {mark}")

        def fmt_date(d):
            return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        print(f"  分割点(entry日): {fmt_date(mid)}  "
              f"[前半 {fmt_date(dates[0])}〜 / 後半 〜{fmt_date(dates[-1])}]\n")
        for tag, sub in [("全体", dated), ("ロング", longs), ("ショート", shorts)]:
            e = [r for r in sub if r.get("entry_dt") is not None and r["entry_dt"] < mid]
            l = [r for r in sub if r.get("entry_dt") is not None and r["entry_dt"] >= mid]
            split_line(e, f"{tag} 前半")
            split_line(l, f"{tag} 後半")
            print()
        print("  → 前半・後半の両方で『close優位』なら、ルール変更の効果は時期に依存せず頑健")

    # ── 分析4: 推奨ポリシー検証 (close、ただしMOMロングはintraday据え置き) ──
    print("\n" + "=" * 90)
    print("【分析4】 推奨ポリシー検証  (close採用、ただし MOM ロングのみ intraday 据え置き)")
    print("=" * 90)

    def policy_mode(r):
        if r["strat"] == "MOM" and not r["is_short"]:
            return "intraday"
        return "close"

    def stat_sel(rows, selector):
        done = [(r, selector(r)) for r in rows]
        done = [(r, m) for r, m in done if r["sims"][m]["reason"] != "保有中"]
        pnls = [r["sims"][m]["pnl"] for r, m in done]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        gp = sum(p for p in pnls if p > 0); gl = abs(sum(p for p in pnls if p < 0))
        pf = gp / gl if gl > 0 else float("inf")
        return dict(n=n, wr=wins / n * 100 if n else 0, pf=pf,
                    pnl=sum(pnls), worst=min(pnls) if pnls else 0.0,
                    bigloss=sum(1 for p in pnls if p < -20000))

    def report_policy(rows, title):
        if not rows:
            return
        base = stat(rows, "intraday")
        pol  = stat_sel(rows, policy_mode)
        d = pol["pnl"] - base["pnl"]
        print(f"\n[{title}]")
        print(f"  現行(全intraday) : 件数{base['n']:>4} 勝率{base['wr']:5.1f}% PF{pf_s(base['pf'])} "
              f"総損益{base['pnl']:>+13,.0f}  最大損失{base['worst']:>+11,.0f} 2万損{base['bigloss']:>3}件")
        print(f"  推奨ポリシー     : 件数{pol['n']:>4} 勝率{pol['wr']:5.1f}% PF{pf_s(pol['pf'])} "
              f"総損益{pol['pnl']:>+13,.0f}  最大損失{pol['worst']:>+11,.0f} 2万損{pol['bigloss']:>3}件")
        print(f"  改善             : {d:>+13,.0f} 円")

    report_policy(all_rows, "全体")
    report_policy(longs,   "ロング")
    report_policy(shorts,  "ショート")

    # ポリシーの時期分割
    if dated:
        print("\n  ── 推奨ポリシーの時期分割 (前半/後半) ──")
        for tag, sub in [("全体", dated), ("ロング", longs), ("ショート", shorts)]:
            for half, lo, hi in [("前半", None, mid), ("後半", mid, None)]:
                seg = [r for r in sub if r.get("entry_dt") is not None
                       and (lo is None or r["entry_dt"] >= lo)
                       and (hi is None or r["entry_dt"] < hi)]
                b = stat(seg, "intraday"); p = stat_sel(seg, policy_mode)
                d = p["pnl"] - b["pnl"]
                mark = "✓改善" if d > 0 else ("─同等" if d == 0 else "✗悪化")
                print(f"  {tag+' '+half:<14} n={b['n']:<4} "
                      f"現行 {b['pnl']:>+12,.0f} → ポリシー {p['pnl']:>+12,.0f} "
                      f"({d:>+11,.0f}) {mark}")
            print()

    # 整合性チェック: intraday 再シミュ総損益 vs 実バックテスト総損益
    real_pnl = sum(r["real_pnl"] for r in all_rows if r["real_reason"] != "保有中")
    resim_pnl = sum(r["sims"]["intraday"]["pnl"] for r in all_rows
                    if r["sims"]["intraday"]["reason"] != "保有中")
    print("\n" + "-" * 90)
    print(f"[整合性] 実バックテスト総損益 {real_pnl:+,.0f} 円  vs  intraday再シミュ {resim_pnl:+,.0f} 円")
    print(f"         (一致なら再シミュは忠実 → close/hybrid の数字も信頼できる)")
    print(f"\n  ※ 最大損失=単発の最悪損益(尾リスク) / 2万損=2万円超の損失件数")
    print(f"  ※ hybrid は損切り価格の {CRASH_PCT*100:.0f}% 外側にザラ場ハード損切りを併設 (--crash-pct で変更可)")


if __name__ == "__main__":
    main()
