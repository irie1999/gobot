r"""analyze_uncond_break.py — 『信号は要るのか』『トリガーはどれだけ深く待つべきか』を測る。

なぜ必要か (2026-08-08 時点で残った唯一の問い)
------------------------------------------------
これまでの測定で、lss を構成する要素のうち **効いていないと確定したもの**:

  * 銘柄選定  … 15属性×5分位=78検定で候補ゼロ (18.13)
  * BTスコア  … 勝率が全帯 40.8-42.5% でフラット (18.12)
  * 日中ドリフト … 511営業日・604,626銘柄日で 95%CI が0をまたぐ = 存在しない (18.19)
  * ロング方向  … 全9,022ペア・159,585取引で TRAIN/TEST とも負け (18.18)

**残っているのは2つだけ**:
  ① トリガー: 前日終値-1ティックの逆指値ショート (= 下ブレイク待ち)
  ② 決済    : sm0.1 / tm1.0 の非対称OCO

18.19 で分かったのは「無条件のドリフトはゼロだが、**下に割った銘柄は下げ続ける**」
という **条件付き** の非対称。だとすると当然こう問うべき:

  Q1. **6つのロング信号(MACDTF/A7/RSI2/DON/VOLTF/MOM)は、そもそも要るのか?**
      信号を一切使わず「全銘柄・毎日・前日終値割れで売る」だけで同じ期待値が出るなら、
      WF選定・提案ファイル・BTスコア・WATCHLIST の**全機構が不要**になる。
      銘柄数が桁違いに増えるので容量も維持コストも劇的に改善する。
      逆に大きく下回るなら、**信号こそが唯一効いている部品**と確定する。

  Q2. **トリガーはどれだけ深く待つべきか?**
      現行は前日終値-1ティック = ほぼ「少しでも割れたら売る」。エッジが
      『下ブレイクへの反応』にあるなら、深さは最重要パラメータのはずだが
      **一度もスイープされていない**(全戦略 entry_atr_mult=0.0 固定)。

本ツールは信号を使わず、全銘柄×全営業日に同じ機械的ルールを当てて上の2つを測る。

測り方 (ライブと同じ約定モデル)
--------------------------------
  トリガー  = 前日終値 × (1 - 深さ) を呼値に丸め (深さ0 = 前日終値-1ティック)
  約定      = short_entry_fill_5m … min(トリガー, その日の始値) / -3%指値ガード (18.8)
  損切/利確 = トリガー ± ATR×sm / tm  (ATR = 日足TRのEWM(span=14) = エンジンと同じ)
  決済      = short_exit_5m … 5分足 first-touch / stop_delay_bars で無保護窓を再現
  日足      = 5分足から生成 (yfinance に依存しない。ATRも同じ足から)

統計 (18.13 の作法)
--------------------
同日決済は日内で強く相関するので、日ごとに等ウェイト平均して **日数** で検定する。
件数を n にすると実効サンプルを数十倍に誤認する。TRAIN/TEST 分割も出す。

使い方
------
  python analyze_uncond_break.py --limit 200 --workers 8            # まず動作確認
  python analyze_uncond_break.py --workers 8 --split 2026-02-01     # 本番
  python analyze_uncond_break.py --depths 0,0.005,0.01,0.02 --workers 8

読み方
------
  * 深さ0 の期待値が lss の **+418円/件** に近い → 信号は要らない。機構だけで説明できる。
  * 大きく下回る → 信号が効いている。lss の中で唯一生きている部品はそこ。
  * 深さを増やして期待値が上がる → トリガーが浅すぎた。ここが未開拓のレバー。
"""
from __future__ import annotations

import argparse
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from daytrade_data import DATA_DIR, available_local_symbols, load_intraday
from backtest_limit_entry import round_to_tick, tick_size
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl

ap = argparse.ArgumentParser(
    description="信号なしの下ブレイクショートを全銘柄×全営業日で測る")
ap.add_argument("--source", type=str, default="local")
ap.add_argument("--days", type=int, default=800)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄(デバッグ)")
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(現行0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(現行1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=1,
                help="無保護窓の5分足本数(live watch.bat = 1)")
ap.add_argument("--depths", type=str, default="0,0.0025,0.005,0.01,0.015,0.02",
                help="トリガーの深さ(前日終値からの下落率)。0 = 前日終値-1ティック")
ap.add_argument("--gap-limit", type=float, default=0.03, help="指値ガード(既定-3%%)")
ap.add_argument("--min-price", type=float, default=1000.0, help="前日終値で下限")
ap.add_argument("--max-price", type=float, default=6000.0, help="前日終値で上限")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--slip", type=float, default=0.0)
ap.add_argument("--fee", type=float, default=0.0, help="片道手数料率(実口座は0)")
ap.add_argument("--split", type=str, default="", help="TRAIN/TEST の境目 YYYY-MM-DD")
ap.add_argument("--atr-period", type=int, default=14)
ap.add_argument("--csv", type=str, default="", help="全トレードをCSV保存")
args = ap.parse_args()

DEPTHS = [float(x) for x in args.depths.split(",") if x.strip() != ""]
LSS_REF = 418.0          # 予算タブ11ヶ月 +883,421円 / 2,113件 (18.15)


def _jq_to_yf(code: str) -> str:
    c = str(code).strip().upper().removesuffix(".T")
    if len(c) == 5 and c.isdigit() and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


def _one_symbol(sym: str) -> list[tuple] | None:
    """1銘柄・全営業日・全深さの (深さ, 日付, pnl, 約定したか) を返す。"""
    try:
        df = load_intraday(sym, days=args.days, source=args.source)
    except Exception:
        return None
    if df is None or df.empty or len(df) < 200:
        return None

    # ── 5分足から日足を作り、ATR(EWM span=14) を出す ────────────────
    # エンジン(backtest_limit_entry:504-505)と同じ TR / EWM。yfinance に依存しない。
    g = df.groupby(df.index.normalize())
    daily = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(),
        "low": g["low"].min(), "close": g["close"].last(),
    })
    daily = daily[g.size() >= 6]                       # 半日立会などを除外
    if len(daily) < args.atr_period + 5:
        return None
    pc = daily["close"].shift(1)
    tr = pd.concat([daily["high"] - daily["low"],
                    (daily["high"] - pc).abs(),
                    (daily["low"] - pc).abs()], axis=1).max(axis=1)
    daily["atr"] = tr.ewm(span=args.atr_period, adjust=False).mean()
    daily["prev_close"] = pc
    daily["prev_atr"] = daily["atr"].shift(1)          # 前日ATR(当日は未確定)

    out = []
    days = list(daily.index)
    for i in range(args.atr_period + 1, len(days)):
        d = days[i]
        row = daily.loc[d]
        prev_c, prev_a = float(row["prev_close"]), float(row["prev_atr"])
        if not (prev_c > 0 and prev_a > 0):
            continue
        # 価格フィルタは **前日終値** で判定 = 発注前に分かる値。先読みなし(18.16)。
        if args.min_price > 0 and prev_c < args.min_price:
            continue
        if args.max_price > 0 and prev_c > args.max_price:
            continue
        day_bars = df[df.index.normalize() == d]
        if len(day_bars) < 6:
            continue
        for dep in DEPTHS:
            # 深さ0 = 前日終値-1ティック(現行lss)。それ以外は前日終値×(1-深さ)。
            raw = prev_c - tick_size(prev_c) if dep == 0 else prev_c * (1.0 - dep)
            trig = float(round_to_tick(raw))
            if trig <= 0:
                continue
            fill = short_entry_fill_5m(day_bars, trig, False,
                                       entry_gap_limit=args.gap_limit)
            if fill is None:
                out.append((dep, d, 0.0, False))       # 未約定(発注枠は消費)
                continue
            stop_p = trig + prev_a * args.sm
            target_p = trig - prev_a * args.tm
            xp, reason, _e, _x = short_exit_5m(
                day_bars, fill, stop_p, target_p, False,
                stop_delay_bars=args.stop_delay_bars)
            if reason in ("no_5m", "no_entry"):
                continue
            pnl = short_pnl(fill, xp, reason, args.qty, args.fee, args.slip)
            out.append((dep, d, float(pnl), True))
    return out or None


# ── 母集団 ──────────────────────────────────────────────────────────
_seen, universe = set(), []
for _s in available_local_symbols():
    y = _jq_to_yf(_s)
    if y not in _seen:
        _seen.add(y)
        universe.append(y)
if args.limit > 0:
    universe = universe[:args.limit]
if not universe:
    sys.exit(f"[error] 5分足が見つかりません (DATA_DIR={DATA_DIR})")

print(f"[母集団] {len(universe):,}銘柄 (信号フィルタなし=全銘柄×全営業日)")
print(f"[設定] sm={args.sm} tm={args.tm} delay={args.stop_delay_bars} "
      f"指値ガード{args.gap_limit:.0%} / 手数料{args.fee:.3%} 滑り{args.slip:.3%} "
      f"/ 価格{args.min_price:,.0f}〜{args.max_price:,.0f}(前日終値)")
print(f"[深さ] {', '.join(f'{d:.2%}' if d else '1ティック' for d in DEPTHS)}")

rows = []
with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
    futs = {ex.submit(_one_symbol, s): s for s in universe}
    done = 0
    for f in as_completed(futs):
        done += 1
        if done % 100 == 0:
            print(f"  ...{done}/{len(universe)}銘柄", flush=True)
        r = f.result()
        if r:
            rows.extend(r)

if not rows:
    sys.exit("[error] 有効なデータがありませんでした")

tr = pd.DataFrame(rows, columns=["depth", "date", "pnl", "filled"])
tr["date"] = pd.to_datetime(tr["date"])
print(f"\n[入力] {len(tr):,}行 / {tr['date'].min().date()}〜{tr['date'].max().date()} "
      f"/ {tr['date'].nunique():,}営業日")
if args.csv:
    tr.to_csv(args.csv, index=False, encoding="utf-8-sig")
    print(f"[保存] {args.csv}")


def _stats(d: pd.DataFrame) -> dict | None:
    """約定分だけの成績 + 日クラスタ頑健な t。"""
    f = d[d["filled"]]
    if len(f) < 30:
        return None
    p = f["pnl"].to_numpy(dtype=float)
    wins = p[p > 0]
    gl = -p[p <= 0].sum()
    pf = wins.sum() / gl if gl > 0 else float("inf")
    daily = f.groupby("date")["pnl"].mean()
    n_days = len(daily)
    sd = float(daily.std(ddof=1)) if n_days > 2 else float("nan")
    se = sd / math.sqrt(n_days) if sd and sd > 0 else float("nan")
    mu_d = float(daily.mean())
    return {"n": len(f), "fill": len(f) / len(d) * 100,
            "exp": float(p.mean()), "pnl": float(p.sum()),
            "wr": len(wins) / len(p) * 100, "pf": pf,
            "days": n_days, "t": (mu_d / se) if se == se and se > 0 else float("nan"),
            "ci_lo": mu_d - 1.96 * se, "ci_hi": mu_d + 1.96 * se}


def _table(d: pd.DataFrame, title: str) -> None:
    print(f"\n{'=' * 104}")
    print(f"■ {title}")
    print(f"{'=' * 104}")
    print(f"  {'深さ':<10}{'約定':>9}{'約定率':>8}{'期待値/件':>11}{'勝率':>7}"
          f"{'PF':>7}{'合計損益':>15}{'t(日)':>8}{'95%CI(円/日)':>20}")
    print("  " + "-" * 100)
    for dep in DEPTHS:
        st = _stats(d[d["depth"] == dep])
        lbl = "1ティック" if dep == 0 else f"-{dep:.2%}"
        if not st:
            print(f"  {lbl:<10}{'データ不足':>9}")
            continue
        _pf = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
        mark = " ★" if abs(st["t"]) >= 2.0 else ""
        print(f"  {lbl:<10}{st['n']:>9,}{st['fill']:>7.1f}%{st['exp']:>+11,.0f}"
              f"{st['wr']:>6.1f}%{_pf:>7}{st['pnl']:>+15,.0f}{st['t']:>8.2f}"
              f"{st['ci_lo']:>+10,.0f}{st['ci_hi']:>+10,.0f}{mark}")
    print("  " + "-" * 100)
    print(f"  ★ = |t| >= 2.0 (日クラスタ頑健)。CIが0をまたぐ深さは『ゼロと区別できない』")


_table(tr, f"全期間 (信号なし・全銘柄×全営業日)")

if args.split:
    cut = pd.Timestamp(args.split)
    _table(tr[tr["date"] < cut], f"TRAIN (〜{cut.date()})")
    _table(tr[tr["date"] >= cut], f"TEST  ({cut.date()}〜)")

# ── 判定 ────────────────────────────────────────────────────────────
print(f"\n{'─' * 104}")
print("■ 判定")
print(f"{'─' * 104}")
_base = _stats(tr[tr["depth"] == DEPTHS[0]])
if _base:
    print(f"  現行と同じ深さ(1ティック)の期待値 = {_base['exp']:+,.0f}円/件 "
          f"(t={_base['t']:.2f} / CI {_base['ci_lo']:+,.0f}〜{_base['ci_hi']:+,.0f})")
    print(f"  lss(信号あり・選定あり)の実績     = **{LSS_REF:+,.0f}円/件** (18.15)")
    if _base["ci_lo"] <= 0 <= _base["ci_hi"]:
        print(f"  → **信号なしではゼロ。6つのロング信号が唯一効いている部品。**")
        print(f"     WF選定/BTは無効(18.12/18.13)でも、信号そのものは機能している。")
    elif _base["exp"] >= LSS_REF * 0.8:
        print(f"  → **信号は要らない。** 機構(トリガー+OCO)だけで同水準。")
        print(f"     WF選定・提案ファイル・BTスコア・WATCHLIST を全部捨てられる。")
        print(f"     母集団が桁違いに増えるので容量・維持コストとも劇的に改善する。")
    elif _base["ci_lo"] > 0:
        print(f"  → 信号なしでも有意にプラスだが lss を下回る。"
              f"差 {_base['exp'] - LSS_REF:+,.0f}円/件 が信号の付加価値。")
    else:
        print(f"  → 信号なしでは有意にマイナス。信号は必須。")

_best = max((d for d in DEPTHS if _stats(tr[tr["depth"] == d])),
            key=lambda d: _stats(tr[tr["depth"] == d])["exp"], default=None)
if _best is not None and len(DEPTHS) > 1:
    _bs = _stats(tr[tr["depth"] == _best])
    _lbl = "1ティック" if _best == 0 else f"-{_best:.2%}"
    print(f"\n  期待値が最大の深さ = **{_lbl}** ({_bs['exp']:+,.0f}円/件 / 約定率 {_bs['fill']:.1f}%)")
    if _best != DEPTHS[0]:
        print(f"  → 現行(1ティック)より深く待つ方が良い可能性。ただし約定率が下がるので")
        print(f"     **総額** ({_bs['pnl']:+,.0f}円) と TRAIN/TEST の一致で判断すること。")
        print(f"     TRAIN だけで最良の深さを選ぶと過剰適合する(18.10 の教訓)。")
    else:
        print(f"  → 現行の深さが最良。トリガーの深さは効かない。")
print("\n  ※ これは資金制約なしの全トレード。実運用は予算400万で1日十数件しか建てられない。")
print("     採否は必ず sim_portfolio_lss.py 相当(予算・上限キャンセル込み)で確認すること(18.10)。")
