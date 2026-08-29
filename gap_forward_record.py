#!/usr/bin/env python3
"""
gap_forward_record.py — ギャップ反転の前進記録 (発注は一切しません)

測るのは損益ではなく **気配から計算した残差ギャップが、実際の始値から
計算したものとどれだけずれるか** です。判定は誤差の平均ではなく
**発火判定の反転率** と **裾 (90/95%点)** で行います。

3段構え。真ん中 (気配の取得) は証券会社のAPIに依存するので、
CSV の受け渡しで分離してあります。

  1. prepare    前夜。候補と発火トリガー価格を確定させる
  2. (気配取得)  朝。別ツールが CSV を書く。仕様は下記
  3. reconcile  引け後。実際の始値・指数始値・終値と突合
  4. report     反転率と誤差の裾

⛔ 発注機能はありません。売買系のモジュールを import していません。

── 前夜に確定できるもの ──────────────────────────────────────
  前日終値 / ATR20 / β / 前日方向 (同符号条件) はすべて前夜に決まります。
  同符号条件があるので **各銘柄は上下どちらか片側しか発火しません**。
  これで板の購読上限 (同時50銘柄) に対する候補を半分に絞れます。

── 朝の気配CSVの仕様 (別ツールが書く) ────────────────────────
  列: date, symbol, poll_time, quote_price, quote_kind, index_level
    date        YYYY-MM-DD
    symbol      7203.T 形式
    poll_time   HH:MM:SS (JST)
    quote_price その時点の想定約定価格 (気配値)。円
    quote_kind  quote / special_quote / traded  のいずれか
    index_level その時点の指数値。指数が寄っていれば始値、まだなら現在値
  1銘柄につき複数行 (1〜3分おき) を想定します。

使い方:
    python gap_forward_record.py prepare
    python gap_forward_record.py reconcile --quotes quotes_2026-09-01.csv
    python gap_forward_record.py report
    python gap_forward_record.py --self-test
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import gap_reversal_daily as gd

OUT_DIR = Path("forward_records")
GAP_THR = 3.0          # ATR単位。gap_reversal_intraday の FROZEN_GAP_THR と揃える
PREV_THR = 0.0         # 0.0 = 前日の動きと同符号であることを要求 (大きさは問わない)
BETA_WINDOW = gd.BETA_WINDOW
ATR_PERIOD = gd.ATR_PERIOD


# ══════════════════════════════════════════════════════════════════
# 1. 前夜: 候補とトリガー価格
# ══════════════════════════════════════════════════════════════════
def build_candidates(cache_dir: str, index_symbol: str,
                     limit: int | None, workers: int) -> pd.DataFrame:
    """各銘柄の最新営業日までの情報から、翌朝の候補を作る。

    トリガー価格は指数ギャップに依存するので、係数の形で持たせる。

        resid_gap = gap - beta * idx_gap  >= GAP_THR * atr
        gap       = open / prev_close - 1
        → 発火する始値 = prev_close * (1 + GAP_THR*atr + beta*idx_gap)

    朝は `trigger_price(idx_gap)` に指数ギャップを入れるだけで済む。
    """
    syms, has_idx = gd.load_cache_dir(cache_dir, index_symbol)
    if not syms:
        raise SystemExit("日足キャッシュが見つかりません")
    if limit:
        syms = syms[:limit]
    idx_df = gd.fetch_daily(index_symbol)
    if idx_df is None:
        raise SystemExit(f"{index_symbol} が見つかりません")
    idx_ret = idx_df["close"].pct_change()

    # 除外理由を数える。出力が少ないときに推測しないで済むように。
    drop = {k: 0 for k in ("短すぎ", "β算出不可", "ATR異常", "価格帯外",
                           "売買代金不足", "前日リターン欠損", "古いキャッシュ")}
    last_dates = []
    idx_last = idx_df.index[-1]

    rows = []
    for sym in syms:
        df = gd.fetch_daily(sym)
        if df is None or len(df) < BETA_WINDOW + ATR_PERIOD + 5:
            drop["短すぎ"] += 1
            continue
        last_dates.append(df.index[-1])
        c = df["close"]
        ret = c.pct_change()
        # β: 指数と日付が揃う行だけを使う。rolling は窓内に NaN が1つでも
        # あると NaN を返すので、事前に整列してから計算しないと全滅する。
        pair = pd.concat([ret.rename("r"), idx_ret.rename("i")], axis=1,
                         join="inner").dropna()
        if len(pair) < BETA_WINDOW:
            drop["β算出不可"] += 1
            continue
        w = pair.tail(BETA_WINDOW)
        v = float(w["i"].var())
        if not np.isfinite(v) or v <= 0:
            drop["β算出不可"] += 1
            continue
        beta = float(np.clip(w["r"].cov(w["i"]) / v, -3, 3))
        atr = float(gd._atr_pct(df).iloc[-1])
        turnover = float((c * df["volume"]).rolling(20).mean().iloc[-1])
        prev_close = float(c.iloc[-1])
        if not np.isfinite(atr) or atr <= 0.001:
            drop["ATR異常"] += 1          # ATR 0.1% 未満は株式ではあり得ない
            continue
        if not (gd.MIN_PRICE <= prev_close <= gd.MAX_PRICE):
            drop["価格帯外"] += 1
            continue
        if not np.isfinite(turnover) or turnover < gd.MIN_TURNOVER:
            drop["売買代金不足"] += 1
            continue
        # 前日の残差リターン (同符号条件はこれで前夜に決まる)
        r_last = float(ret.iloc[-1])
        ir = idx_ret.get(df.index[-1], np.nan)
        if not np.isfinite(r_last):
            drop["前日リターン欠損"] += 1
            continue
        resid_prev = r_last - beta * (float(ir) if np.isfinite(ir) else 0.0)
        prev_z = resid_prev / atr
        up_ok = prev_z >= PREV_THR
        dn_ok = prev_z <= -PREV_THR
        rows.append({
            "asof": df.index[-1].strftime("%Y-%m-%d"),
            "symbol": sym,
            "prev_close": round(prev_close, 2),
            "atr20_pct": round(atr * 100, 4),
            "beta": round(beta, 4),
            "prev_z": round(prev_z, 4),
            "turnover20_oku": round(turnover / 1e8, 1),
            "eligible_side": ("short" if up_ok and not dn_ok
                              else "long" if dn_ok and not up_ok else "both"),
            "trigger_up_at_idx0": round(prev_close * (1 + GAP_THR * atr), 2),
            "trigger_dn_at_idx0": round(prev_close * (1 - GAP_THR * atr), 2),
            "idx_adj_per_1pct": round(prev_close * beta * 0.01, 4),
        })

    print(f"\n候補の絞り込み: {len(syms):,} 銘柄 → {len(rows):,} 銘柄")
    for k, v in drop.items():
        if v:
            print(f"    除外 {k:<16} {v:>6,}")
    if last_dates:
        ld = pd.Series(last_dates)
        newest = ld.max()
        stale = int((ld < newest).sum())
        print(f"    最終バー: 最新 {newest:%Y-%m-%d} / "
              f"それより古い銘柄 {stale:,} 件")
        if stale > len(ld) * 0.1:
            print("    ⚠ 最終バーの日付が揃っていません。キャッシュの更新漏れです。")
    print(f"    指数 {index_symbol} の最終バー: {idx_last:%Y-%m-%d}")
    if rows:
        a = pd.Series([r["asof"] for r in rows])
        if a.nunique() > 1:
            print(f"    ⚠ 候補の asof が {a.nunique()} 種類あります: "
                  f"{sorted(a.unique())[:5]}")
            print("       同じ日のデータで揃っていないと、翌朝の候補になりません。")

    out = pd.DataFrame(rows)
    return out.sort_values("turnover20_oku", ascending=False).reset_index(drop=True)


def trigger_price(prev_close: float, atr: float, beta: float,
                  idx_gap: float, side: str) -> float:
    """朝、指数ギャップが分かった時点での発火価格。"""
    k = GAP_THR * atr + beta * idx_gap
    return prev_close * (1 + k) if side == "short" else prev_close * (1 - abs(k))


# ══════════════════════════════════════════════════════════════════
# 3. 引け後: 突合
# ══════════════════════════════════════════════════════════════════
def reconcile(cand: pd.DataFrame, quotes: pd.DataFrame,
              cache_dir: str, index_symbol: str) -> pd.DataFrame:
    """気配からの推定と、実際の始値からの値を突き合わせる。"""
    gd.load_cache_dir(cache_dir, index_symbol)
    idx_df = gd.fetch_daily(index_symbol)
    q = quotes.copy()
    q["date"] = pd.to_datetime(q["date"])
    m = q.merge(cand, on="symbol", how="inner", suffixes=("", "_c"))
    if m.empty:
        raise SystemExit("候補と気配CSVが1件も一致しません (symbol の表記を確認)")

    rows = []
    for d, g in m.groupby("date"):
        # 実際の指数ギャップ
        if idx_df is not None and d in idx_df.index:
            i_prev = idx_df["close"].shift(1).get(d, np.nan)
            idx_gap_act = float(idx_df["open"].get(d, np.nan) / i_prev - 1) \
                if np.isfinite(i_prev) else np.nan
            idx_prev = float(i_prev)
        else:
            idx_gap_act, idx_prev = np.nan, np.nan
        for _, r in g.iterrows():
            df = gd.fetch_daily(r["symbol"])
            if df is None or d not in df.index:
                continue
            o_act = float(df["open"].get(d))
            # 気配時点の推定: 指数はその時点の水準から換算
            idx_gap_est = (float(r["index_level"]) / idx_prev - 1) \
                if np.isfinite(idx_prev) and "index_level" in r else np.nan
            gap_est = float(r["quote_price"]) / r["prev_close"] - 1
            gap_act = o_act / r["prev_close"] - 1
            atr = r["atr20_pct"] / 100.0
            rg_est = gap_est - r["beta"] * (idx_gap_est if np.isfinite(idx_gap_est) else 0.0)
            rg_act = gap_act - r["beta"] * (idx_gap_act if np.isfinite(idx_gap_act) else 0.0)
            z_est, z_act = rg_est / atr, rg_act / atr
            side = r["eligible_side"]
            fire = (lambda z: (z >= GAP_THR) if side == "short"
                    else (z <= -GAP_THR) if side == "long"
                    else (abs(z) >= GAP_THR))
            rows.append({
                "date": d, "symbol": r["symbol"], "poll_time": r["poll_time"],
                "quote_kind": r.get("quote_kind", ""),
                "quote_price": r["quote_price"], "official_open": o_act,
                "resid_gap_est_bp": rg_est * 10000,
                "resid_gap_act_bp": rg_act * 10000,
                "err_bp": (rg_est - rg_act) * 10000,
                "z_est": z_est, "z_act": z_act,
                "fire_est": bool(fire(z_est)), "fire_act": bool(fire(z_act)),
                "side": side,
            })
    out = pd.DataFrame(rows)
    if not out.empty:
        out["flip"] = out["fire_est"] != out["fire_act"]
    return out


# ══════════════════════════════════════════════════════════════════
# 4. レポート
# ══════════════════════════════════════════════════════════════════
def report(ev: pd.DataFrame) -> None:
    print("\n" + "=" * 78)
    print(gd.provenance("gap_forward_record.py"))
    print("前進記録レポート (発注していません)")
    print("=" * 78)
    if ev.empty:
        print("突合できた行がありません。")
        return
    print(f"期間 {ev['date'].min():%Y-%m-%d} 〜 {ev['date'].max():%Y-%m-%d}   "
          f"{ev['symbol'].nunique()} 銘柄 / {len(ev):,} 観測 / "
          f"{ev['date'].nunique()} 日")

    print("\n【1】発火判定の反転率 (これが本命)")
    print("  気配で『出す』と判定 → 実際は発火せず、またはその逆。")
    for t, g in ev.groupby(ev["poll_time"].astype(str).str[:5]):
        n, fl = len(g), int(g["flip"].sum())
        fp = int((g["fire_est"] & ~g["fire_act"]).sum())
        fn = int((~g["fire_est"] & g["fire_act"]).sum())
        print(f"    {t}   n={n:>5}  反転 {fl:>4} ({fl/n*100:>5.2f}%)   "
              f"空振り {fp:>4} / 取り逃し {fn:>4}")
    n, fl = len(ev), int(ev["flip"].sum())
    print(f"    全体   n={n:>5}  反転 {fl:>4} ({fl/n*100:>5.2f}%)")

    print("\n【2】残差ギャップの推定誤差 (bp) — 平均ではなく裾を見る")
    print(f"    {'時刻':<8}{'n':>6}{'中央':>9}{'90%点':>9}{'95%点':>9}{'最大':>9}")
    for t, g in ev.groupby(ev["poll_time"].astype(str).str[:5]):
        e = g["err_bp"].abs()
        print(f"    {t:<8}{len(g):>6}{e.median():>9.1f}{e.quantile(.9):>9.1f}"
              f"{e.quantile(.95):>9.1f}{e.max():>9.1f}")
    e = ev["err_bp"].abs()
    print(f"    {'全体':<8}{len(ev):>6}{e.median():>9.1f}{e.quantile(.9):>9.1f}"
          f"{e.quantile(.95):>9.1f}{e.max():>9.1f}")

    print("\n【3】実際に発火した銘柄日だけ (運用で効くのはここ)")
    fired = ev[ev["fire_act"]]
    if len(fired):
        for t, g in fired.groupby(g if False else fired["poll_time"].astype(str).str[:5]):
            miss = int((~g["fire_est"]).sum())
            print(f"    {t}   発火 {len(g):>4} 件中 気配で取り逃し {miss:>3} "
                  f"({miss/len(g)*100:.1f}%)")
    else:
        print("    まだ発火した銘柄日がありません。")

    print("\n" + "=" * 78)
    print("判定: 反転率が数%以下で、95%点の誤差が閾値 (中央値 約1,000bp) に対し")
    print("      十分小さければ、日足の成績を執行可能なものとして扱えます。")
    print("      反転率が二桁なら、気配からの判定は成立しません。")
    print("=" * 78)


# ══════════════════════════════════════════════════════════════════
# 5. 実弾の記録 (小ロット)
# ══════════════════════════════════════════════════════════════════
LIVE_COLS = [
    "date",            # YYYY-MM-DD
    "symbol",          # 7203.T
    "side",            # long / short
    "quote_time",      # 寄り前に気配を見た時刻 HH:MM:SS
    "quote_price",     # そのときの想定約定価格
    "order_time",      # 発注時刻 HH:MM:SS
    "expected_price",  # 発注時点で想定していた約定価格
    "official_open",   # 実際の始値 (引け後に埋める)
    "fill_price",      # 実際の約定価格 (未約定なら空)
    "fill_qty",        # 実際の約定数量 (比例配分だと注文数量より少ない)
    "order_qty",       # 注文数量
    "exit_time",       # 決済時刻
    "exit_price",      # 決済価格 (決済できなければ空)
    "exit_qty",        # 決済数量
    "note",            # 特別気配だった / ストップ張り付き など
]


def live_template(path: Path) -> None:
    """実弾記録の空CSVを作る。"""
    if path.exists():
        print(f"{path} は既にあります。上書きしません。")
        return
    pd.DataFrame(columns=LIVE_COLS).to_csv(path, index=False)
    print(f"実弾記録の雛形を作りました: {path}")
    print("  列: " + ", ".join(LIVE_COLS))


def live_report(live: pd.DataFrame) -> None:
    """小ロット運用の実績。測るのは損益ではなく執行です。"""
    print("\n" + "=" * 78)
    print(gd.provenance("gap_forward_record.py --live"))
    print("実弾記録 (小ロット) — 測るのは執行であって成績ではありません")
    print("=" * 78)
    if live.empty:
        print("記録がありません。")
        return
    n = len(live)
    print(f"{n} 件 / {live['date'].nunique()} 日")

    filled = live[live["fill_price"].notna()]
    print(f"\n【1】約定できたか  {len(filled)} / {n} 件 "
          f"({len(filled)/n*100:.0f}%)")
    if len(filled):
        part = filled[filled["fill_qty"] < filled["order_qty"]]
        print(f"  うち一部約定 (比例配分など): {len(part)} 件")

    print("\n【2】約定価格 と 始値 の差 ← 本命")
    print("  板寄せに参加できていれば 0bp のはずです。")
    f = filled[filled["official_open"].notna()]
    if len(f):
        d = (f["fill_price"] / f["official_open"] - 1).abs() * 10000
        exact = int((d < 0.5).sum())
        print(f"  完全一致: {exact} / {len(f)} 件")
        print(f"  差 (bp): 中央 {d.median():.1f} / 最大 {d.max():.1f}")
        if exact < len(f):
            for _, r in f[(d >= 0.5)].iterrows():
                print(f"    {r['date']} {r['symbol']}  "
                      f"約定 {r['fill_price']:,.1f} / 始値 {r['official_open']:,.1f}  "
                      f"{r.get('note','')}")
    else:
        print("  始値が未入力です。")

    print("\n【3】引けで決済できたか")
    ex = filled[filled["exit_price"].notna()]
    print(f"  決済できた: {len(ex)} / {len(filled)} 件")
    stuck = filled[filled["exit_price"].isna()]
    if len(stuck):
        print(f"  ⛔ 決済できなかった {len(stuck)} 件:")
        for _, r in stuck.iterrows():
            print(f"    {r['date']} {r['symbol']}  {r.get('note','')}")

    if len(ex):
        sgn = np.where(ex["side"] == "long", 1.0, -1.0)
        pnl = sgn * (ex["exit_price"] - ex["fill_price"]) * ex["exit_qty"]
        print(f"\n【4】損益 (参考。件数が少なすぎて統計にはなりません)")
        print(f"  合計 {pnl.sum():,.0f}円 / 1件あたり {pnl.mean():,.0f}円 / "
              f"勝ち {int((pnl>0).sum())} 負け {int((pnl<=0).sum())}")

    print("\n" + "=" * 78)
    print("判定: 【2】が全件0bpなら、特別気配の銘柄でも板寄せに参加できています。")
    print("      そこが確認できたら、残りは廃止銘柄・気配精度・決算分離です。")
    print("=" * 78)


# ══════════════════════════════════════════════════════════════════
def self_test() -> int:
    """合成データで配管を確認する。"""
    rng = np.random.default_rng(3)
    n = 200
    cand = pd.DataFrame({
        "asof": "2026-09-01", "symbol": [f"{1300+i}.T" for i in range(n)],
        "prev_close": 2000.0, "atr20_pct": 2.0, "beta": 1.0,
        "prev_z": rng.normal(0, 1, n),
        "turnover20_oku": 50.0,
        "eligible_side": "long", "trigger_up_at_idx0": 2120.0,
        "trigger_dn_at_idx0": 1880.0, "idx_adj_per_1pct": 20.0,
    })
    # 実際の残差ギャップを決め、気配はそこに誤差を乗せたもの
    true_rg = rng.normal(0, 0.05, n)
    err = rng.normal(0, 0.0065, n)          # 65bp の誤差
    ev = pd.DataFrame({
        "date": pd.Timestamp("2026-09-01"), "symbol": cand["symbol"],
        "poll_time": "08:57:00", "quote_kind": "quote",
        "quote_price": 2000 * (1 + true_rg + err),
        "official_open": 2000 * (1 + true_rg),
        "resid_gap_est_bp": (true_rg + err) * 10000,
        "resid_gap_act_bp": true_rg * 10000,
        "err_bp": err * 10000,
        "z_est": (true_rg + err) / 0.02, "z_act": true_rg / 0.02,
        "side": "long",
    })
    ev["fire_est"] = ev["z_est"] <= -GAP_THR
    ev["fire_act"] = ev["z_act"] <= -GAP_THR
    ev["flip"] = ev["fire_est"] != ev["fire_act"]
    report(ev)
    rate = ev["flip"].mean()
    ok = 0.0 <= rate <= 0.20 and abs(ev["err_bp"]).median() > 10
    print(f"\nSELF-TEST: 誤差 65bp を注入 → 反転率 {rate*100:.1f}% / "
          f"誤差中央 {abs(ev['err_bp']).median():.0f}bp")
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ギャップ反転の前進記録 (発注しません)")
    ap.add_argument("mode", nargs="?",
                    choices=["prepare", "reconcile", "report", "live-init", "live"])
    ap.add_argument("--cache-dir", dest="cache_dir", default=".rsi2_cache")
    ap.add_argument("--index-symbol", dest="index_symbol", default=gd.INDEX_SYM)
    ap.add_argument("--quotes", help="朝の気配CSV")
    ap.add_argument("--candidates", help="前夜の候補CSV (既定は最新)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.mode:
        ap.print_help()
        return 1
    OUT_DIR.mkdir(exist_ok=True)

    if args.mode == "prepare":
        cand = build_candidates(args.cache_dir, args.index_symbol,
                                args.limit, args.workers)
        asof = cand["asof"].iloc[0] if len(cand) else datetime.now().strftime("%Y-%m-%d")
        p = OUT_DIR / f"candidates_{asof}.csv"
        cand.to_csv(p, index=False)
        print(f"\n候補 {len(cand):,} 銘柄 → {p}")
        vc = cand["eligible_side"].value_counts().to_dict()
        print(f"  発火しうる側の内訳: {vc}")
        print("  ※ 同符号条件により各銘柄は片側しか発火しません。")
        print("     板の購読上限 (同時50銘柄) に対しては、売買代金上位から")
        print("     必要な側だけを購読すれば足ります。")
        print(cand.head(10).to_string(index=False))
        return 0

    if args.mode in ("live-init", "live"):
        if args.mode == "live-init":
            live_template(OUT_DIR / "live_trades.csv")
            return 0
        lp = OUT_DIR / "live_trades.csv"
        if not lp.exists():
            print("live_trades.csv がありません。live-init で作ってください。")
            return 1
        live_report(pd.read_csv(lp))
        return 0

    cp = (Path(args.candidates) if args.candidates
          else max(OUT_DIR.glob("candidates_*.csv"), default=None))
    if cp is None:
        print("候補CSVがありません。先に prepare を実行してください。")
        return 1
    cand = pd.read_csv(cp)

    if args.mode == "reconcile":
        if not args.quotes:
            print("--quotes に朝の気配CSVを指定してください。")
            return 1
        q = pd.read_csv(args.quotes)
        ev = reconcile(cand, q, args.cache_dir, args.index_symbol)
        p = OUT_DIR / f"eval_{pd.Timestamp(ev['date'].max()):%Y-%m-%d}.csv"
        ev.to_csv(p, index=False)
        print(f"突合 {len(ev):,} 行 → {p}")
        report(ev)
        return 0

    if args.mode == "live-init":
        live_template(OUT_DIR / "live_trades.csv")
        return 0
    if args.mode == "live":
        lp = OUT_DIR / "live_trades.csv"
        if not lp.exists():
            print("live_trades.csv がありません。live-init で作ってください。")
            return 1
        live_report(pd.read_csv(lp))
        return 0

    files = sorted(OUT_DIR.glob("eval_*.csv"))
    if not files:
        print("評価CSVがありません。先に reconcile を実行してください。")
        return 1
    ev = pd.concat([pd.read_csv(f, parse_dates=["date"]) for f in files],
                   ignore_index=True)
    report(ev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
