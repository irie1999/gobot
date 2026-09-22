"""analyze_lss_losses.py — lss(ロング銘柄ショート/逆指値空売り・同日決済)の
『損切りトレードがなぜ負けたか』を5分足の形状で定量分類・集計する分析ツール。

仮説の検証: 「初めに下へ抜けて約定 → その後の反発(踏み上げ)で損切りに刈られる」
が本当に多いのか、どのくらい利確に近づいてから反発しているのかを測る。

やること:
  1. 選定銘柄(holdout_selected_symbols.py = レポートが実際に発注する銘柄×戦略)を読む。
  2. 各(銘柄,戦略)で run_signals と同じ約定/決済ロジック(short_entry_fill_5m /
     short_exit_5m, v13悲観)で全トレードを再現。損切り(reason=stop)だけ抽出。
  3. 各損切りについて5分足から形状指標を計算:
       - MFE(最大順行): 約定後、損切り到達までに最も安く付けた値。どこまで利益方向へ
         進んだか。target まで何%進んで反発したか(mfe_ratio)。
       - 反発幅 / 損切り時刻 / 同バー損切りか / 寄りギャップ約定か。
  4. 形状バケットに分類して集計(全期間 + 月別)。
  5. 損切り幅(sm)を広げると総損益がどう変わるかの what-if スイープ。
  6. コンソール要約 + CSV + HTML(代表的な損切りチャートのSVGギャラリー)を出力。

使い方(あなたの機械で。5分足データが必要):
  python analyze_lss_losses.py                       # holdout_selected_symbols.py を自動使用
  python analyze_lss_losses.py --days 240            # 遡及日数(既定240≒2026年以降)
  python analyze_lss_losses.py --min-price 1000 --max-price 6000   # daily と同じ価格帯
  python analyze_lss_losses.py --symbols-file lss_watchlist_proposal_2026-07-24.py
  python analyze_lss_losses.py --no-html             # HTMLを作らない
出力:
  * コンソール: バケット別/月別集計・stop幅 what-if
  * lss_loss_analysis_<date>.csv  : 損切りトレード全件 + 指標
  * lss_loss_analysis_<date>.html : 集計 + 代表チャートSVGギャラリー
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss損切りトレードの形状分析(なぜ負けたか)")
ap.add_argument("--symbols-file", type=str, default=None,
                help="(code,name,strat) を持つ .py。既定=holdout_selected_symbols.py を自動検出")
ap.add_argument("--days", type=int, default=240, help="遡及日数(5分足は2024-07以降のみ)")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(lss既定0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(lss既定1.0)")
ap.add_argument("--slip", type=float, default=0.0, help="損切り買い戻しの不利スリップ")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部。デバッグ)")
ap.add_argument("--gallery", type=int, default=24, help="HTMLに載せる代表チャート数")
ap.add_argument("--no-html", action="store_true")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, round_to_tick, tick_size
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl

# scan_lss_universe と同じく、エンジンのモードグローバルは未強制に固定
ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
_GAP_LIMIT = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
_ON_CLOSE = getattr(ble, "_INTRADAY_5M_ON_CLOSE", False)
TODAY = pd.Timestamp.now().normalize()

# 損切り幅 what-if で試す sm 倍率
SM_SWEEP = [args.sm, 0.15, 0.2, 0.3, 0.5]


def _load_selected() -> list[tuple]:
    """選定ファイルから (code, name, strat) を読む。既定は holdout_selected_symbols.py。"""
    path = args.symbols_file
    if path is None:
        for cand in ("holdout_selected_symbols.py", "holdout_selected_symbols_short.py"):
            if Path(cand).exists():
                path = cand
                break
    if path is None:
        # 最新の lss 提案ファイルにフォールバック
        props = sorted(Path(".").glob("lss_watchlist_proposal_*.py"))
        if props:
            path = str(props[-1])
    if path is None or not Path(path).exists():
        sys.exit("[error] 選定ファイルが見つかりません(--symbols-file で指定してください)")
    spec = importlib.util.spec_from_file_location("_sel_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = getattr(mod, "SELECTED", None) or getattr(mod, "SELECTED_PAIRS", None)
    if rows is None:
        sys.exit(f"[error] {path} に SELECTED がありません")
    out = []
    for r in rows:
        if len(r) >= 3:
            out.append((str(r[0]), str(r[1]), str(r[2])))
        elif len(r) == 2:
            out.append((str(r[0]), "", str(r[1])))
    print(f"[選定] {path} から {len(out)} ペア読込")
    return out


def _day_ohlc(df_raw, fd):
    """約定日 fd の日足OHLC(始値/安値/高値/終値)。engine の5分足ブロックと同じ補助。"""
    try:
        drow = df_raw.loc[df_raw.index.normalize() == pd.Timestamp(fd)]
        if len(drow):
            return (float(drow["open"].iloc[0]), float(drow["low"].iloc[0]),
                    float(drow["high"].iloc[0]), float(drow["close"].iloc[0]))
    except Exception:
        pass
    return (None, None, None, None)


def _prices(order_limit, order_stop, order_target):
    """engine(v10-v13)と同じ価格グリッド: トリガー=前日終値round-1tick / 損切・利確=ceil。"""
    base = round_to_tick(order_limit)
    trigger = float(round_to_tick(base - tick_size(base)))
    stop_p = float(ceil_to_tick(max(order_stop, order_target)))
    target_p = float(ceil_to_tick(min(order_stop, order_target)))
    return trigger, stop_p, target_p


def _classify(entry_fill, trigger, stop_p, target_p, mfe_low, stop_idx, ei, gap):
    """損切りの形状バケット分類。"""
    denom = entry_fill - target_p
    ratio = (entry_fill - mfe_low) / denom if denom > 0 else 0.0
    if gap:
        return "ギャップ寄り→反発損切り", ratio
    if stop_idx == ei:
        return "同バー即損切り(寄り天/V字)", ratio
    if ratio >= 0.7:
        return "利確寸前で反発(惜しい)", ratio
    if ratio >= 0.25:
        return "下げてから反発で損切り(だまし下げ)", ratio
    return "ほぼ下げず反発(弱いブレイク)", ratio


def _scan_symbol(sym: str, name: str, strats: list[str]) -> list[dict]:
    """1銘柄・複数戦略の lss を回し、損切りトレードの明細+形状指標を返す。"""
    out: list[dict] = []
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return out
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return out
    if df_raw is None or df_raw.empty:
        return out

    for strat in strats:
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        cf = params[0]
        try:
            df_ind = cf(df_raw.copy())
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + 420, strat,
                                       entry_type="stop_sell", max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            olp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if olp <= 0 or osp <= 0 or otp <= 0:
                continue
            if olp < args.min_price or olp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=args.days):
                continue
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            trigger, stop_p, target_p = _prices(olp, osp, otp)
            d_open, d_low, d_high, d_close = _day_ohlc(df_raw, fd)
            entry_fill = short_entry_fill_5m(db, trigger, False, entry_gap_limit=_GAP_LIMIT,
                                             day_open=d_open, day_low=d_low, day_high=d_high)
            if entry_fill is None:
                continue
            gap = abs(entry_fill - trigger) > 1e-6
            xp, reason, _e, x_ts = short_exit_5m(db, trigger, stop_p, target_p, False,
                                                 on_close=_ON_CLOSE, include_entry_bar=gap,
                                                 day_low=d_low, day_high=d_high, day_close=d_close)
            if reason in ("no_5m", "no_entry"):
                continue
            pnl = short_pnl(entry_fill, xp, reason, QTY, FEE_ONE_WAY, args.slip)
            if reason != "stop":
                continue   # 損切りだけ分析

            # ── 形状指標 ──
            lows = db["low"].to_numpy(dtype=float)
            highs = db["high"].to_numpy(dtype=float)
            closes_a = db["close"].to_numpy(dtype=float)
            times = db.index
            n = len(lows)
            ei = 0
            for j in range(n):
                if lows[j] <= trigger:
                    ei = j
                    break
            # 損切りバー(約定バー含めて最初に高値>=stop)
            stop_idx = ei
            for j in range(ei, n):
                if highs[j] >= stop_p:
                    stop_idx = j
                    break
            mfe_low = float(lows[ei:stop_idx + 1].min()) if stop_idx >= ei else float(lows[ei])
            bucket, ratio = _classify(entry_fill, trigger, stop_p, target_p, mfe_low,
                                      stop_idx, ei, gap)
            try:
                st = x_ts
                stop_min = st.hour * 60 + st.minute - 540 if hasattr(st, "hour") else None
            except Exception:
                stop_min = None
            atr = (osp - olp) / args.sm if args.sm > 0 else 0.0   # 近似ATR(前日終値基準)

            # ── 反実仮想: 約定バー(同バー)の損切りを無視して"持ち続けたら"どうなったか ──
            #    約定バーの"次バー以降"(ei+1〜)だけで損切り/利確を first-touch 判定する
            #    (=v13前の楽観ロジックを手動再現。v13で include_entry_bar が無効化された
            #    ため short_exit_5m では計算できない)。同バー損切りを刈られずに残った場合、
            #    その後 利確 / 後で損切り / 引け のどれになるかを測る。
            rsn_h, xp_h = "close", (d_close if (d_close and d_close > 0) else float(closes_a[-1]))
            for j in range(ei + 1, n):
                _sh = closes_a[j] >= stop_p if _ON_CLOSE else highs[j] >= stop_p
                if _sh:
                    rsn_h, xp_h = "stop", (float(closes_a[j]) if _ON_CLOSE else stop_p)
                    break
                _th = closes_a[j] <= target_p if _ON_CLOSE else lows[j] <= target_p
                if _th:
                    rsn_h, xp_h = "target", (float(closes_a[j]) if _ON_CLOSE else target_p)
                    break
            pnl_h = short_pnl(entry_fill, xp_h, rsn_h, QTY, FEE_ONE_WAY, args.slip)

            # ── stop幅 what-if(この日、sm を広げたら結果はどうなるか) ──
            sweep = {}
            for sm2 in SM_SWEEP:
                if abs(sm2 - args.sm) < 1e-9:
                    sweep[sm2] = pnl
                    continue
                stop2 = float(ceil_to_tick(olp + atr * sm2)) if atr > 0 else stop_p
                xp2, rsn2, _e2, _x2 = short_exit_5m(db, trigger, stop2, target_p, False,
                                                    on_close=_ON_CLOSE, include_entry_bar=gap,
                                                    day_low=d_low, day_high=d_high, day_close=d_close)
                sweep[sm2] = short_pnl(entry_fill, xp2, rsn2, QTY, FEE_ONE_WAY, args.slip)

            out.append({
                "date": str(fd), "month": str(fd)[:7], "sym": sym, "name": name,
                "strat": strat, "entry": round(entry_fill, 1), "trigger": round(trigger, 1),
                "stop": round(stop_p, 1), "target": round(target_p, 1),
                "mfe_low": round(mfe_low, 1), "mfe_ratio": round(ratio, 3),
                "reversal": round(stop_p - mfe_low, 1), "stop_min": stop_min,
                "same_bar": stop_idx == ei, "gap": gap, "pnl": round(pnl, 0),
                "bucket": bucket, "sweep": sweep,
                "held_reason": rsn_h, "held_pnl": round(pnl_h, 0),
                # チャート用に5分足を軽量保存(HTML gallery)
                "_bars": db[["open", "high", "low", "close"]].to_dict("list"),
                "_times": [str(x)[11:16] for x in times],
                "_ei": ei, "_stop_idx": stop_idx,
            })
    return out


# ───────────────────────── 集計・出力 ─────────────────────────

def _yen(x):
    return f"{x:+,.0f}"


def _svg_chart(row) -> str:
    """1トレードの5分足終値ライン + 約定/損切/利確ラインのSVG。"""
    bars = row["_bars"]
    closes = bars["close"]
    highs = bars["high"]
    lows = bars["low"]
    n = len(closes)
    if n < 2:
        return ""
    lo = min(min(lows), row["target"]) * 0.998
    hi = max(max(highs), row["stop"]) * 1.002
    W, H, PAD = 380, 130, 6
    span = (hi - lo) or 1.0

    def x(i):
        return PAD + i * (W - 2 * PAD) / (n - 1)

    def y(v):
        return PAD + (hi - v) * (H - 2 * PAD) / span

    pts = " ".join(f"{x(i):.1f},{y(closes[i]):.1f}" for i in range(n))
    dash = 'stroke-dasharray="3 2"'

    def hline(v, col, extra=""):
        return (f'<line x1="{PAD}" y1="{y(v):.1f}" x2="{W-PAD}" y2="{y(v):.1f}" '
                f'stroke="{col}" stroke-width="1" {extra}/>')
    ei, si = row["_ei"], row["_stop_idx"]
    ex, ey = x(ei), y(row["entry"])
    sx, sy = x(si), y(row["stop"])
    dots = (f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3" fill="#3b82f6"/>'
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="3" fill="#ef4444"/>')
    line_stop = hline(row["stop"], "#ef4444", dash)
    line_entry = hline(row["entry"], "#3b82f6")
    line_tgt = hline(row["target"], "#22c55e", dash)
    return (f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'style="background:#0d1117;border:1px solid #30363d;border-radius:6px">'
            f'{line_stop}{line_entry}{line_tgt}'
            f'<polyline points="{pts}" fill="none" stroke="#e6edf3" stroke-width="1.2"/>'
            f'{dots}</svg>')


def main():
    sel = _load_selected()
    # 銘柄ごとに戦略をまとめる(5分足ロードを銘柄1回に)
    by_sym: dict[str, dict] = {}
    for code, name, strat in sel:
        d = by_sym.setdefault(code, {"name": name, "strats": []})
        if strat not in d["strats"]:
            d["strats"].append(strat)
        if name and not d["name"]:
            d["name"] = name
    syms = list(by_sym.items())
    if args.limit:
        syms = syms[:args.limit]
    print(f"[分析] {len(syms)}銘柄 × 平均{sum(len(v['strats']) for _, v in syms)/max(1,len(syms)):.1f}戦略 "
          f"/ 遡及{args.days}日 / 価格{args.min_price:.0f}〜{args.max_price:.0f}円")

    losses: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_symbol, code, v["name"], v["strats"]): code
                for code, v in syms}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                losses.extend(fut.result())
            except Exception:
                pass
            if done % 25 == 0:
                print(f"  ...{done}/{len(syms)}銘柄  損切り累計{len(losses)}件")

    if not losses:
        print("損切りトレードが見つかりませんでした(データ未同期 or 価格帯が狭い可能性)。")
        return

    total_loss = sum(r["pnl"] for r in losses)
    print("\n" + "=" * 72)
    print(f"損切りトレード {len(losses)}件 / 合計 {_yen(total_loss)}円 / "
          f"平均 {_yen(total_loss/len(losses))}円")
    print("=" * 72)

    # ── バケット別 ──
    print("\n【形状バケット別】(仮説『下げて→反発で損切り』の割合を確認)")
    print(f"{'形状':<32}{'件数':>6}{'割合':>7}{'合計損':>12}{'平均利確接近%':>14}")
    buckets: dict[str, list] = {}
    for r in losses:
        buckets.setdefault(r["bucket"], []).append(r)
    order = ["下げてから反発で損切り(だまし下げ)", "利確寸前で反発(惜しい)",
             "同バー即損切り(寄り天/V字)", "ギャップ寄り→反発損切り", "ほぼ下げず反発(弱いブレイク)"]
    for b in order + [k for k in buckets if k not in order]:
        rs = buckets.get(b)
        if not rs:
            continue
        pct = len(rs) / len(losses) * 100
        avg_ratio = sum(x["mfe_ratio"] for x in rs) / len(rs) * 100
        print(f"{b:<32}{len(rs):>6}{pct:>6.0f}%{sum(x['pnl'] for x in rs):>12,.0f}"
              f"{avg_ratio:>13.0f}%")
    down_then_up = sum(len(buckets.get(b, [])) for b in
                       ["下げてから反発で損切り(だまし下げ)", "利確寸前で反発(惜しい)",
                        "同バー即損切り(寄り天/V字)"])
    print(f"\n→ 『下げてから反発で刈られた』系 = {down_then_up}/{len(losses)}件 "
          f"({down_then_up/len(losses)*100:.0f}%)")
    avg_ratio_all = sum(r["mfe_ratio"] for r in losses) / len(losses) * 100
    print(f"→ 損切りは平均で利確まで {avg_ratio_all:.0f}% 進んでから反発している")
    near = sum(1 for r in losses if r["mfe_ratio"] >= 0.7)
    print(f"→ 利確まで70%以上進んで反発(惜しい)= {near}件 ({near/len(losses)*100:.0f}%)")

    # ── 反実仮想: 同バー損切りを刈られず持ち続けたら? ──
    def _held_report(title, rows):
        if not rows:
            return
        c = {"target": 0, "stop": 0, "close": 0}
        for r in rows:
            c[r["held_reason"]] = c.get(r["held_reason"], 0) + 1
        now_sum = sum(r["pnl"] for r in rows)          # 現状(同バーで損切り)
        held_sum = sum(r["held_pnl"] for r in rows)    # 持ち続けた場合
        tot = len(rows)
        print(f"\n【{title}】{tot}件 — もし約定バーで損切りせず持ち続けたら:")
        print(f"  利確(利益)   : {c['target']:>5}件 ({c['target']/tot*100:>3.0f}%)")
        print(f"  後で損切り    : {c['stop']:>5}件 ({c['stop']/tot*100:>3.0f}%)")
        print(f"  引け(タイムカット): {c['close']:>5}件 ({c['close']/tot*100:>3.0f}%)")
        print(f"  → 損益: 現状(同バー損切り) {now_sum:>+12,.0f}円  "
              f"持ち続け {held_sum:>+12,.0f}円  差 {held_sum-now_sum:>+12,.0f}円")

    same_bar_rows = [r for r in losses if r["same_bar"] and not r["gap"]]
    gap_rows = [r for r in losses if r["gap"]]
    _held_report("同バー即損切り(寄り天/V字)を持ち続けたら", same_bar_rows)
    _held_report("ギャップ寄り→反発損切りを持ち続けたら", gap_rows)
    print("  ※ 差がプラス=同バー損切り(v13)が利益を刈っている / マイナス=v13が正しく損失回避")
    print("  ※ ただし『持ち続け』は実運用で日中OCO損切りを置かない前提。ヒゲで刈られないが")
    print("     深い含み損を引けまで抱えるリスクと引き換え。")

    # ── 損切り時刻 ──
    mins = [r["stop_min"] for r in losses if r["stop_min"] is not None]
    if mins:
        early = sum(1 for m in mins if m <= 30)
        print(f"\n【損切り時刻】寄り後30分以内に刈られた = {early}/{len(mins)}件 "
              f"({early/len(mins)*100:.0f}%) / 平均 寄り後{sum(mins)/len(mins):.0f}分")

    # ── 月別 ──
    print("\n【月別】(過去も同じ傾向か)")
    print(f"{'月':<10}{'件数':>6}{'合計損':>12}{'平均利確接近%':>14}")
    months: dict[str, list] = {}
    for r in losses:
        months.setdefault(r["month"], []).append(r)
    for m in sorted(months):
        rs = months[m]
        avg_ratio = sum(x["mfe_ratio"] for x in rs) / len(rs) * 100
        print(f"{m:<10}{len(rs):>6}{sum(x['pnl'] for x in rs):>12,.0f}{avg_ratio:>13.0f}%")

    # ── stop幅 what-if ──
    print("\n【損切り幅 what-if】(損切りトレードだけを対象に sm を変えたら合計損益は)")
    print("  ※ 利確/引けトレードは不変。ここは現状の損切り群のみ再評価した増減")
    print(f"{'sm(損切ATR)':<12}{'合計損益':>14}{'現状比':>14}")
    base_sum = sum(r["sweep"][args.sm] for r in losses)
    for sm2 in SM_SWEEP:
        s = sum(r["sweep"].get(sm2, r["pnl"]) for r in losses)
        tag = " ← 現状" if abs(sm2 - args.sm) < 1e-9 else ""
        print(f"{sm2:<12.2f}{s:>14,.0f}{s-base_sum:>+14,.0f}{tag}")

    # ── 銘柄別ワースト ──
    print("\n【損切りが多い/大きい銘柄 ワースト15】")
    sym_agg: dict[tuple, list] = {}
    for r in losses:
        sym_agg.setdefault((r["sym"], r["name"]), []).append(r["pnl"])
    worst = sorted(sym_agg.items(), key=lambda kv: sum(kv[1]))[:15]
    print(f"{'銘柄':<14}{'名称':<16}{'件数':>5}{'合計損':>12}")
    for (sym, name), pnls in worst:
        print(f"{sym:<14}{(name or '')[:14]:<16}{len(pnls):>5}{sum(pnls):>12,.0f}")

    # ── CSV ──
    date_s = str(TODAY.date())
    csv_path = Path(f"lss_loss_analysis_{date_s}.csv")
    cols = ["date", "month", "sym", "name", "strat", "entry", "trigger", "stop",
            "target", "mfe_low", "mfe_ratio", "reversal", "stop_min", "same_bar",
            "gap", "pnl", "bucket", "held_reason", "held_pnl"]
    pd.DataFrame([{k: r[k] for k in cols} for r in losses]).sort_values("pnl").to_csv(
        csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[出力] {csv_path}")

    # ── HTML ギャラリー ──
    if not args.no_html:
        gallery = sorted(losses, key=lambda r: r["pnl"])[:args.gallery]
        cards = []
        for r in gallery:
            cards.append(
                f'<div class="card"><div class="hd">{r["sym"]} {r["name"]} '
                f'<span class="st">{r["strat"]}</span> <b class="loss">{_yen(r["pnl"])}円</b></div>'
                f'<div class="meta">{r["date"]} / {r["bucket"]}<br>'
                f'約定{r["entry"]:.0f} → 損切{r["stop"]:.0f} / 利確{r["target"]:.0f} '
                f'(利確まで{r["mfe_ratio"]*100:.0f}%進んで反発)</div>'
                f'{_svg_chart(r)}</div>')
        html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>lss損切り分析 {date_s}</title><style>
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;margin:16px}}
h1{{font-size:18px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:12px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px}}
.hd{{font-size:13px;margin-bottom:4px}} .st{{color:#8b949e}} .loss{{color:#ef4444}}
.meta{{color:#8b949e;font-size:12px;margin-bottom:6px;line-height:1.4}}
.legend span{{margin-right:14px}}
</style></head><body>
<h1>lss 損切りトレード分析 — なぜ負けたか({date_s})</h1>
<div class="sub">損切り{len(losses)}件 / 合計{_yen(total_loss)}円 ・
『下げて→反発で損切り』系 {down_then_up/len(losses)*100:.0f}% ・
平均で利確まで{avg_ratio_all:.0f}%進んで反発</div>
<div class="sub legend">
<span style="color:#3b82f6">━ 約定(青丸=約定バー)</span>
<span style="color:#ef4444">┅ 損切り(赤丸=損切りバー)</span>
<span style="color:#22c55e">┅ 利確</span>
<span style="color:#e6edf3">━ 5分足終値</span></div>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
        html_path = Path(f"lss_loss_analysis_{date_s}.html")
        html_path.write_text(html, encoding="utf-8")
        print(f"[出力] {html_path}  (損失ワースト{len(gallery)}件のチャート)")
        try:
            import webbrowser
            webbrowser.open(html_path.resolve().as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
