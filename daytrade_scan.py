"""
daytrade_scan.py  ―  全銘柄ORBスキャン → 勝ち銘柄選定
==================================================================
プライム市場 ~1800銘柄に ORB バックテストを実行し、
PF/勝率/損益でランキングして上位銘柄を選定する。

【使い方】
  python daytrade_scan.py --source yfinance --days 60 --budget 600000
  python daytrade_scan.py --source local --days 730 --budget 600000
  python daytrade_scan.py --top 30              # 上位30銘柄を出力
  python daytrade_scan.py --save                # 結果をCSV+watchlist保存

【出力】
  コンソール: PF順ランキング Top N
  HTML: 全銘柄ランキング
  --save: scan_result.csv + watchlist自動更新
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

# ── ORB パラメータ (最適化済み) ──────────────────────────────
OR_MINUTES     = 30
TARGET_K       = 1.5
STOP_BUFFER_K  = 0.0
GAP_MAX_PCT    = 2.0
FORCE_CLOSE    = dtime(14, 55)
ENTRY_CUTOFF   = dtime(11, 0)
AM_START       = dtime(9, 0)
TRAILING_TRIGGER = 0.5
BUDGET         = 600_000
MAX_RISK       = 6_000
FIXED_QTY      = 100


# ─────────────────────────────────────────────────────────────
# 全プライム銘柄リスト取得
# ─────────────────────────────────────────────────────────────

def get_prime_symbols() -> list[str]:
    """プライム市場の全銘柄コードを取得。"""
    # J-Quants から取得を試みる
    try:
        import os
        for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
                break

        import jquantsapi
        api_key = os.environ.get("JQUANTS_API_KEY")
        if api_key:
            cli = jquantsapi.ClientV2(api_key=api_key)
            df = cli.get_eq_master()
            if df is not None and not df.empty:
                # プライム市場フィルタ (V2列名: MktNm)
                for col in ["MktNm", "MarketCodeName", "MarketCode", "market_code_name"]:
                    if col in df.columns:
                        prime = df[df[col].astype(str).str.contains(
                            "プライム", na=False)]
                        if not prime.empty:
                            codes = [str(c)[:4] + ".T"
                                     for c in prime["Code"].tolist()
                                     if len(str(c)) >= 4]
                            print(f"  J-Quants: プライム {len(codes)}銘柄", flush=True)
                            return codes
                # フィルタ列がない場合は全銘柄
                codes = [str(c)[:4] + ".T" for c in df["Code"].tolist()
                         if len(str(c)) >= 4]
                print(f"  J-Quants: 全 {len(codes)}銘柄", flush=True)
                return codes
    except Exception as e:
        print(f"  [warn] J-Quants: {e}", file=sys.stderr)

    # フォールバック: symbols_all.py から取得
    try:
        from symbols_all import SYMBOLS
        codes = [s[0] for s in SYMBOLS if s[0].endswith(".T")]
        print(f"  symbols_all.py: {len(codes)}銘柄", flush=True)
        return codes
    except ImportError:
        pass

    print("[ERROR] 銘柄リストが取得できません", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# ORB バックテスト (1日分)
# ─────────────────────────────────────────────────────────────

def backtest_orb_day(day_df, prev_close=None):
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=OR_MINUTES)).time()

    or_bars = day_df[day_df.index.time < or_end]
    rest = day_df[day_df.index.time >= or_end]
    if len(or_bars) < 2 or rest.empty:
        return None

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w = or_hi - or_lo
    if or_w <= 0:
        return None

    open_p = float(or_bars.iloc[0]["open"])
    if prev_close and prev_close > 0:
        gap = abs(open_p - prev_close) / prev_close * 100
        if gap > GAP_MAX_PCT:
            return None

    stop_p = or_lo
    state = "idle"
    entry_p = target_p = 0.0
    exit_p = None
    reason = None
    trailing = False

    bars = list(rest.itertuples())
    for i, bar in enumerate(bars):
        t = bar.Index.time()
        hi, lo, cl = float(bar.high), float(bar.low), float(bar.close)

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p, reason = cl, "引け"
                break
            if not trailing and target_p > entry_p:
                if (cl - entry_p) / (target_p - entry_p) >= TRAILING_TRIGGER:
                    stop_p = entry_p
                    trailing = True
            if lo <= stop_p and hi >= target_p:
                exit_p, reason = stop_p, "損切"
                break
            if hi >= target_p:
                exit_p, reason = target_p, "目標"
                break
            if lo <= stop_p:
                exit_p = stop_p
                reason = "建値" if trailing else "損切"
                break
            continue

        if t >= ENTRY_CUTOFF:
            break
        if cl > or_hi and i + 1 < len(bars):
            entry_p = float(bars[i + 1].open)
            target_p = entry_p + or_w * TARGET_K
            if entry_p <= stop_p:
                break
            state = "in_pos"
            trailing = False
            nhi, nlo = float(bars[i+1].high), float(bars[i+1].low)
            if nlo <= stop_p:
                exit_p, reason = stop_p, "損切"
                break
            if nhi >= target_p:
                exit_p, reason = target_p, "目標"
                break

    if state == "in_pos" and exit_p is None:
        exit_p, reason = float(bars[-1].close), "引け"

    if exit_p is None or entry_p <= 0:
        return None

    qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)
    pnl = (exit_p - entry_p) * qty
    return {"pnl": pnl, "entry_p": entry_p, "reason": reason}


def scan_symbol(sym, df):
    """1銘柄のスキャン結果を返す。"""
    daily = split_by_day(df)
    if len(daily) < 5:
        return None

    dates = sorted(daily.keys())
    trades = []
    prev_close = None
    for d in dates:
        t = backtest_orb_day(daily[d], prev_close)
        if t:
            trades.append(t)
        prev_close = float(daily[d].iloc[-1]["close"])

    n = len(trades)
    if n < 3:
        return None

    wins = sum(1 for t in trades if t["pnl"] > 0)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    total = sum(t["pnl"] for t in trades)
    latest_price = float(df.iloc[-1]["close"])

    return {
        "symbol": sym, "price": latest_price,
        "n": n, "wins": wins, "wr": wins / n * 100,
        "pf": pf, "total_pnl": total,
        "avg_pnl": total / n,
    }


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


# ─────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────

def build_html(results, days, source, budget, top_n):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    profitable = [r for r in results if r["pf"] >= 1.0]

    rows = ""
    for i, r in enumerate(results[:top_n]):
        cls = "profit" if r["total_pnl"] >= 0 else "loss"
        pf_cls = "profit" if r["pf"] >= 1.3 else ("" if r["pf"] >= 1.0 else "loss")
        rows += f"""<tr>
          <td>{i+1}</td>
          <td class="sym">{r['symbol']}</td>
          <td>{r['price']:,.0f}円</td>
          <td>{r['n']}</td><td>{r['wr']:.0f}%</td>
          <td class="{pf_cls}">{_pf(r['pf'])}</td>
          <td class="{cls}">{r['total_pnl']:+,.0f}</td>
          <td>{r['avg_pnl']:+,.0f}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>ORB 全銘柄スキャン — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#fbbf24;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#fbbf24;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #fbbf24;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
tr:hover td{{background:#1e293b}}.sym{{text-align:left;font-weight:600}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.3rem;font-weight:700}}
</style></head><body>
<h1>ORB 全銘柄スキャン結果</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円 /
スキャン:{len(results)}銘柄 / PF≥1.0: {len(profitable)}銘柄</p>
<div class="box">
<div class="it"><div class="lb">スキャン銘柄</div><div class="vl">{len(results)}</div></div>
<div class="it"><div class="lb">PF≥1.0</div><div class="vl profit">{len(profitable)}</div></div>
<div class="it"><div class="lb">PF≥1.3</div><div class="vl profit">{len([r for r in results if r['pf']>=1.3])}</div></div>
<div class="it"><div class="lb">PF≥1.5</div><div class="vl profit">{len([r for r in results if r['pf']>=1.5])}</div></div>
</div>
<h2>ランキング (PF順 Top {top_n})</h2>
<table><thead><tr>
<th>#</th><th>銘柄</th><th>株価</th><th>取引</th><th>勝率</th><th>PF</th><th>損益</th><th>平均損益</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ORB 全銘柄スキャン")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--top", type=int, default=50, help="表示上位数")
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--save", action="store_true",
                        help="結果をCSV保存 + watchlist更新")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    print(f"ORB全銘柄スキャン: {args.days}日 / 予算{args.budget:,}円", flush=True)

    # 銘柄リスト取得
    print("銘柄リスト取得中...", flush=True)
    all_symbols = get_prime_symbols()
    print(f"  対象: {len(all_symbols)}銘柄", flush=True)

    # バッチ取得 (100銘柄ずつ)
    print("データ取得中...", flush=True)
    fetched = {}
    batch_size = 100
    for i in range(0, len(all_symbols), batch_size):
        batch = all_symbols[i:i + batch_size]
        print(f"  batch {i//batch_size+1}: {len(batch)}銘柄...", flush=True)
        batch_result = load_intraday_batch(batch, args.days, source=args.source)
        fetched.update(batch_result)

    # 予算フィルタ
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    print(f"  予算フィルタ後: {len(fetched)}銘柄 (≤{max_price:,.0f}円)",
          flush=True)

    # スキャン実行
    print("ORBスキャン実行中...", flush=True)
    results = []
    total = len(fetched)
    for i, (sym, df) in enumerate(fetched.items(), 1):
        r = scan_symbol(sym, df)
        if r:
            results.append(r)
        if i % 100 == 0 or i == total:
            print(f"  {i}/{total} 完了 ({len(results)}銘柄ヒット)", flush=True)

    # PF順ソート
    results.sort(key=lambda x: x["pf"], reverse=True)

    # コンソール
    profitable = [r for r in results if r["pf"] >= 1.0]
    print()
    print(f"スキャン結果: {len(results)}銘柄中 PF≥1.0: {len(profitable)}銘柄")
    print()
    print(f"{'#':>3} {'銘柄':<10} {'株価':>7} {'取引':>4} {'勝率':>5} "
          f"{'PF':>6} {'損益':>10} {'平均':>8}")
    print("-" * 65)
    for i, r in enumerate(results[:args.top]):
        print(f"{i+1:>3} {r['symbol']:<10} {r['price']:>6,.0f} "
              f"{r['n']:>4} {r['wr']:>4.0f}% {_pf(r['pf']):>6} "
              f"{r['total_pnl']:>+10,.0f} {r['avg_pnl']:>+8,.0f}")

    # 保存
    if args.save and results:
        csv_path = Path("scan_orb_result.csv")
        pd.DataFrame(results).to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"\nCSV保存: {csv_path}")

        # 上位銘柄をwatchlistとして保存
        top_syms = [(r["symbol"], r["symbol"]) for r in results
                    if r["pf"] >= 1.3 and r["n"] >= 5]
        wl_path = Path("scan_watchlist.py")
        wl_path.write_text(
            f"# ORBスキャン結果 ({datetime.now(JST).strftime('%Y-%m-%d')})\n"
            f"# PF≥1.3 & 取引≥5 の銘柄\n"
            f"SCAN_WATCHLIST = {top_syms}\n",
            encoding="utf-8",
        )
        print(f"Watchlist保存: {wl_path} ({len(top_syms)}銘柄)")

    # HTML
    out = Path(f"daytrade_scan_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(results, args.days, args.source,
                              args.budget, args.top), encoding="utf-8")
    print(f"\nHTMLレポート: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
