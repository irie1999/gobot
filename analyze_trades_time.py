r"""analyze_trades_time.py — レポートのlss取引CSV(LSS_TRADES_CSV 出力)を読んで、
約定時刻別の成績と時間カットオフを集計する。#9の"決定版"。

なぜこれが正しいか:
  CSVは HTML(レポート)と"同一のトレード"（本物のBT=凍結シグナル時スコア / delay1 / v13 /
  現実的約定 / 正しいpnl / 約定時刻 entry_time）を持つ。エンジンを再実装しないので、
  集計は必ずHTMLと一致する(前回 analyze_fill_time は5分足を再実装してdelay1等がズレていた)。

前準備(あなたの機械): レポートを LSS_TRADES_CSV 付きで1回生成し entry_time 列付きCSVを作る:
  set LSS_TRADES_CSV=lss_trades.csv
  .\daily
  (または: python run_signals_holdout_all.py --long-stop-short --lss-proposal lss_proposal_cumul.py
           --days 400 --min-price 1000 --price-ranges 6000 --force --no-news --no-risk --no-serve)

使い方:
  python analyze_trades_time.py lss_trades.csv --bt-min 40
  python analyze_trades_time.py lss_trades.csv --bt-min 40 --oos          # 純OOS(基準月以降のみ)
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss取引CSV(entry_time付き)の約定時刻別成績と時間カットオフ")
ap.add_argument("csv", help="LSS_TRADES_CSV で出力した取引CSV(entry_time列が必要)")
ap.add_argument("--bt-min", type=float, default=40.0, help="BTスコア下限(CSVのbt列=本物の凍結BT)")
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--oos", action="store_true", help="純OOS: 各ペアの選定基準月より後の約定のみ")
ap.add_argument("--oos-proposal", type=str, default="lss_proposal_cumul.py",
                help="SOURCE_BASES を持つ提案ファイル(--oos時に参照)")
args = ap.parse_args()

_BUCKETS = ["1) 寄り 09:00-09:15", "2) 09:15-09:30", "3) 09:30-10:00", "4) 10:00-10:30",
            "5) 10:30-前引", "6) 後場-13:30", "7) 13:30-大引"]
_CUTOFFS = [(9, 15), (9, 30), (10, 0), (10, 30), (11, 30), (15, 30)]


def _bucket(mod: int) -> str:
    if mod < 9 * 60 + 15:   return _BUCKETS[0]
    if mod < 9 * 60 + 30:   return _BUCKETS[1]
    if mod < 10 * 60:       return _BUCKETS[2]
    if mod < 10 * 60 + 30:  return _BUCKETS[3]
    if mod < 11 * 60 + 30:  return _BUCKETS[4]
    if mod < 13 * 60 + 30:  return _BUCKETS[5]
    return _BUCKETS[6]


def _mod(hhmm: str):
    s = str(hhmm).strip()
    if ":" not in s:
        return None
    try:
        h, m = s.split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _month_end(m: int) -> pd.Timestamp:
    m = int(m)
    year = 2025 if m >= 7 else 2026   # 累積2025-09..2026-06前提
    return pd.Period(f"{year}-{m:02d}", "M").end_time.normalize()


def _load_oos_bases(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[error] --oos 用の {path} が無い(SOURCE_BASES を持つ提案ファイルを指定)")
    ns: dict = {}
    exec(p.read_text(encoding="utf-8"), ns)
    sb = ns.get("SOURCE_BASES") or {}
    out = {}
    for k, lab in sb.items():
        months = [int(x) for x in str(lab).replace(" ", "").split("/") if x.strip().isdigit()]
        if not months:
            continue
        code = str(k[0]).upper().removesuffix(".T").split(".")[0]
        out[(code, str(k[1]))] = max(_month_end(x) for x in months)
    print(f"[OOS] {path} の SOURCE_BASES {len(out)}ペア → 各ペアの基準月より後のみ")
    return out


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return n, sum(pnls), sum(pnls) / n, len(wins) / n * 100, pf


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    d = pd.read_csv(args.csv)
    if "entry_time" not in d.columns:
        raise SystemExit("[error] CSVに entry_time 列が無い。レポートを LSS_TRADES_CSV 付きで再生成してください"
                         "(エンジン更新後の版で entry_time が出ます)")
    d["pnl"] = pd.to_numeric(d["pnl"], errors="coerce")
    d["bt"] = pd.to_numeric(d.get("bt"), errors="coerce")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["ol"] = pd.to_numeric(d.get("order_limit"), errors="coerce")
    d = d[d["pnl"].notna() & d["entry_date"].notna()]
    n0 = len(d)
    # 決済済みのみ(発注中/保有中は pnl NaN で既に除外されるはず)
    d = d[~d["reason"].astype(str).isin(["発注中", "保有中"])]
    # BT・価格フィルタ
    d = d[d["bt"] >= args.bt_min]
    if args.min_price > 0:
        d = d[d["ol"] >= args.min_price]
    if args.max_price < 1e9:
        d = d[d["ol"] <= args.max_price]

    # OOS
    if args.oos:
        bases = _load_oos_bases(args.oos_proposal)
        def _ok(r):
            code = str(r["symbol"]).upper().removesuffix(".T").split(".")[0]
            lbe = bases.get((code, str(r["strategy"])))
            return (lbe is not None) and (r["entry_date"] > lbe)
        d = d[d.apply(_ok, axis=1)]

    # 約定時刻 → 分
    d["mod"] = d["entry_time"].map(_mod)
    d_t = d[d["mod"].notna()].copy()
    n_notime = len(d) - len(d_t)
    print(f"[分析] CSV {n0}件 → BT{args.bt_min:.0f}以上"
          + ("・純OOS" if args.oos else "")
          + f" で {len(d)}件 / うち約定時刻あり {len(d_t)}件"
          + (f"(時刻無し{n_notime}件は5分足未集計=除外)" if n_notime else ""))
    if len(d_t) == 0:
        print("[結果] 約定時刻付きトレードが無い。LSS_TRADES_CSV を entry_time 対応版で再生成してください。")
        return

    # 時間帯別
    buckets = defaultdict(list)
    for _, r in d_t.iterrows():
        buckets[_bucket(int(r["mod"]))].append(r["pnl"])
    print(f"\n=== 約定時刻別(BT{args.bt_min:.0f}以上・{len(d_t)}トレード) ===")
    print(f"{'時間帯':<18}{'件数':>6}{'総損益':>12}{'1取引':>8}{'勝率':>6}{'PF':>6}")
    for b in _BUCKETS:
        s = _stat(buckets.get(b, []))
        if not s:
            continue
        n, tot, per, wr, pf = s
        print(f"{b:<18}{n:>6}{tot:>+12,.0f}{per:>+8,.0f}{wr:>5.0f}%{_pf(pf):>6}")

    # 時間カットオフ
    print(f"\n=== 時間カットオフ(◯時までに約定した分だけ採用) ===")
    print(f"{'締切':<10}{'件数':>6}{'総損益':>12}{'1取引':>8}{'勝率':>6}{'PF':>6}")
    for h, m in _CUTOFFS:
        lim = h * 60 + m
        pnls = d_t[d_t["mod"] < lim]["pnl"].tolist()
        s = _stat(pnls)
        if not s:
            continue
        n, tot, per, wr, pf = s
        print(f"{f'〜{h:02d}:{m:02d}':<10}{n:>6}{tot:>+12,.0f}{per:>+8,.0f}{wr:>5.0f}%{_pf(pf):>6}")

    # 月別: 寄り(〜09:15) vs 遅い
    _CUT = 9 * 60 + 15
    d_t["ym"] = d_t["entry_date"].dt.strftime("%Y-%m")
    print(f"\n=== 月別: 寄り(〜09:15) vs 遅い(09:15以降) ===")
    print(f"{'月':>9}{'寄り件':>6}{'寄り損益':>12}{'遅い件':>6}{'遅い損益':>12}")
    n_early_pos = n_late_neg = n_tot = 0
    for ym, g in d_t.groupby("ym"):
        e = g[g["mod"] < _CUT]["pnl"]; l = g[g["mod"] >= _CUT]["pnl"]
        es, ls = e.sum(), l.sum()
        n_tot += 1; n_early_pos += 1 if es > 0 else 0; n_late_neg += 1 if ls < 0 else 0
        print(f"{ym:>9}{len(e):>6}{es:>+12,.0f}{len(l):>6}{ls:>+12,.0f}")
    print(f"  → 寄りがプラス {n_early_pos}/{n_tot} / 遅いがマイナス(切ると得) {n_late_neg}/{n_tot}")


if __name__ == "__main__":
    main()
