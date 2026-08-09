"""
analyze_margin_pressure.py  —  信用倍率の代理変数（売り圧力）× トレード結果の相関分析

「信用倍率が高い ≈ 直前に大きく下落した株に信用買いが集まっている」
という仮定のもと、シグナル前5日・10日の下落率 + 出来高増加率 を
"売り圧力スコア" として、バックテスト済みトレードの結果と相関を見る。

出力:
  - コンソール: 売り圧力帯別の勝率 / PF / 結果内訳
  - HTML:       同上 + 散布図テーブル

Usage:
  python analyze_margin_pressure.py
  python analyze_margin_pressure.py --days 365 --workers 8
  python analyze_margin_pressure.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ── モード設定 ────────────────────────────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")
import check_signals_stop     as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import fetch, run_limit_backtest, WORKERS, JST


# ── CLI ──────────────────────────────────────────────────────────────────────
def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int, default=365)
    p.add_argument("--workers",    type=int, default=WORKERS)
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


# ── バックテスト実行 ──────────────────────────────────────────────────────────
def _run_one(sym, name, strat, calc_fn, em, sm, tm, days, entry_type):
    try:
        df = fetch(sym, days + 120)
        if df is None or len(df) < 30:
            return None
        r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm, days,
                               strat, entry_type=entry_type)
        if not r or not r.get("trade_log"):
            return None
        return sym, name, strat, df, r["trade_log"]
    except Exception:
        return None


def collect_trades(days: int, workers: int) -> list[dict]:
    """全 WATCHLIST × 全戦略のトレードログを収集し、売り圧力指標を付加する。"""
    items = []
    for sym, name, strat in _stop.WATCHLIST:
        p = _stop.STRATEGY_PARAMS[strat]
        items.append((sym, name, strat, p[0], p[1], p[2], p[3], days,
                      _stop.ENTRY_TYPE))
    for sym, name, strat in _brk.WATCHLIST:
        p = _brk.STRATEGY_PARAMS[strat]
        items.append((sym, name, strat, p[0], p[1], p[2], p[3], days,
                      _brk.ENTRY_TYPE))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, *it): it for it in items}
        for fut in as_completed(futs):
            r = fut.result()
            if not r:
                continue
            sym, name, strat, df, trade_log = r
            for t in trade_log:
                if t.get("exit_dt") is None:
                    continue  # 未決済スキップ
                sig_dt = t["signal_dt"]
                if isinstance(sig_dt, datetime):
                    sig_dt = sig_dt.date()

                # ── シグナル日の df 位置を特定 ────────────────────────────
                try:
                    idx = df.index.get_indexer([pd.Timestamp(sig_dt)],
                                               method="ffill")[0]
                except Exception:
                    continue
                if idx < 10:
                    continue

                # ── 売り圧力指標を計算 ────────────────────────────────────
                close_now   = float(df["close"].iloc[idx])
                close_5d    = float(df["close"].iloc[idx - 5])
                close_10d   = float(df["close"].iloc[idx - 10])
                vol_recent  = float(df["volume"].iloc[idx - 4: idx + 1].mean())
                vol_base    = float(df["volume"].iloc[idx - 20: idx - 5].mean())

                mom_5d  = (close_now - close_5d)  / (close_5d  + 1e-8) * 100
                mom_10d = (close_now - close_10d) / (close_10d + 1e-8) * 100
                vol_rat = vol_recent / (vol_base + 1)

                # ── 結果判定 ──────────────────────────────────────────────
                reason = t.get("reason", "")
                if "目標" in str(reason):
                    outcome = "target"
                elif "損切" in str(reason):
                    outcome = "stop"
                else:
                    outcome = "timeout"

                results.append({
                    "symbol":  sym,
                    "name":    name,
                    "strategy": strat,
                    "sig_date": str(sig_dt),
                    "mom_5d":   round(mom_5d, 2),
                    "mom_10d":  round(mom_10d, 2),
                    "vol_ratio": round(vol_rat, 2),
                    "outcome":  outcome,
                    "pnl":      t["pnl"],
                    "win":      1 if t["pnl"] > 0 else 0,
                })
    return results


# ── 集計ヘルパー ─────────────────────────────────────────────────────────────
def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {}
    n    = len(trades)
    wins = sum(t["win"] for t in trades)
    pnl  = sum(t["pnl"] for t in trades)
    gp   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    tgt  = sum(1 for t in trades if t["outcome"] == "target")
    stp  = sum(1 for t in trades if t["outcome"] == "stop")
    tmo  = sum(1 for t in trades if t["outcome"] == "timeout")
    return dict(n=n, wr=wins/n*100, pf=pf, pnl=pnl,
                tgt_pct=tgt/n*100, stp_pct=stp/n*100, tmo_pct=tmo/n*100)


def _bin_label_5d(mom: float) -> str:
    if mom <= -10: return "① -10%以下 (大暴落)"
    if mom <= -5:  return "② -10〜-5% (急落)"
    if mom <= -2:  return "③ -5〜-2% (下落)"
    if mom <=  0:  return "④ -2〜 0% (小幅下落)"
    return              "⑤  0%超 (上昇中)"


def _bin_vol(vol: float) -> str:
    if vol >= 3.0: return "A 出来高3倍超"
    if vol >= 2.0: return "B 出来高2〜3倍"
    if vol >= 1.5: return "C 出来高1.5〜2倍"
    return               "D 出来高1.5倍未満"


# ── HTML 生成 ────────────────────────────────────────────────────────────────
_CSS = """
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;padding:24px}
h1{color:#38bdf8;font-size:1.4rem}
h2{color:#94a3b8;font-size:1.05rem;margin-top:2rem}
table{border-collapse:collapse;width:100%;margin-bottom:2rem;font-size:0.85rem}
th{background:#1e293b;color:#94a3b8;padding:8px 12px;text-align:right;white-space:nowrap}
th:first-child{text-align:left}
td{padding:7px 12px;border-bottom:1px solid #1e293b;text-align:right}
td:first-child{text-align:left;color:#e2e8f0;font-weight:600}
tr:hover td{background:#1e293b55}
.good{color:#4ade80}.bad{color:#f87171}.warn{color:#fbbf24}.gray{color:#64748b}
.note{color:#64748b;font-size:0.8rem;margin:-0.5rem 0 1.5rem}
"""

def _row(label, s, highlight=False):
    if not s:
        return f"<tr><td>{label}</td><td colspan='7' style='color:#475569'>データなし</td></tr>"
    wr_c  = "good" if s["wr"] >= 55 else ("warn" if s["wr"] >= 45 else "bad")
    pf_s  = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    pf_c  = "good" if s["pf"] >= 1.5 else ("warn" if s["pf"] >= 1.0 else "bad")
    pnl_c = "good" if s["pnl"] >= 0 else "bad"
    bg    = " style='background:#1e3a5f22'" if highlight else ""
    return (f"<tr{bg}>"
            f"<td>{label}</td>"
            f"<td>{s['n']}</td>"
            f"<td class='{wr_c}'>{s['wr']:.1f}%</td>"
            f"<td class='{pf_c}'>{pf_s}</td>"
            f"<td class='{pnl_c}'>{s['pnl']:+,.0f}円</td>"
            f"<td class='good'>{s['tgt_pct']:.1f}%</td>"
            f"<td class='bad'>{s['stp_pct']:.1f}%</td>"
            f"<td class='gray'>{s['tmo_pct']:.1f}%</td>"
            f"</tr>")

def _table(title, rows_data: list[tuple]):
    head = ("<table><thead><tr>"
            "<th style='text-align:left'>区分</th>"
            "<th>取引数</th><th>勝率</th><th>PF</th><th>合計損益</th>"
            "<th style='color:#4ade80'>目標達成%</th>"
            "<th style='color:#f87171'>損切り%</th>"
            "<th style='color:#94a3b8'>タイムカット%</th>"
            "</tr></thead><tbody>")
    body = "".join(_row(lbl, s, i == 0) for i, (lbl, s) in enumerate(rows_data))
    return f"<h2>{title}</h2>{head}{body}</tbody></table>"


def build_html(trades: list[dict], days: int) -> str:
    today = datetime.now(JST).date()

    # ── 全体サマリー ──────────────────────────────────────────────────
    overall = _stats(trades)

    # ── シグナル前5日モメンタム別 ─────────────────────────────────────
    bins_5d: dict[str, list] = defaultdict(list)
    for t in trades:
        bins_5d[_bin_label_5d(t["mom_5d"])].append(t)
    rows_5d = [(k, _stats(v)) for k, v in sorted(bins_5d.items())]

    # ── 出来高スパイク別 ──────────────────────────────────────────────
    bins_vol: dict[str, list] = defaultdict(list)
    for t in trades:
        bins_vol[_bin_vol(t["vol_ratio"])].append(t)
    rows_vol = [(k, _stats(v)) for k, v in sorted(bins_vol.items())]

    # ── 複合スコア: 下落 × 出来高スパイク ────────────────────────────
    # 「急落 + 出来高急増」= 信用倍率が高い状態の最良プロキシ
    high_pressure = [t for t in trades if t["mom_5d"] <= -5 and t["vol_ratio"] >= 1.5]
    mid_pressure  = [t for t in trades if -5 < t["mom_5d"] <= -2 and t["vol_ratio"] >= 1.2]
    low_pressure  = [t for t in trades if t["mom_5d"] >  -2 or t["vol_ratio"] <  1.2]
    rows_combo = [
        ("🔴 高圧力 (急落-5%以上 + 出来高1.5倍以上)", _stats(high_pressure)),
        ("🟡 中圧力 (下落-2〜-5% + 出来高1.2倍以上)", _stats(mid_pressure)),
        ("🟢 低圧力 (それ以外)",                        _stats(low_pressure)),
    ]

    # ── RSI2 のみ抽出して同様分析 ──────────────────────────────────────
    rsi2_trades = [t for t in trades if t["strategy"] == "RSI2"]
    rsi2_hp = [t for t in rsi2_trades if t["mom_5d"] <= -5 and t["vol_ratio"] >= 1.5]
    rsi2_mp = [t for t in rsi2_trades if -5 < t["mom_5d"] <= -2 and t["vol_ratio"] >= 1.2]
    rsi2_lp = [t for t in rsi2_trades if t["mom_5d"] >  -2 or t["vol_ratio"] <  1.2]
    rows_rsi2 = [
        ("🔴 高圧力 (RSI2)",  _stats(rsi2_hp)),
        ("🟡 中圧力 (RSI2)",  _stats(rsi2_mp)),
        ("🟢 低圧力 (RSI2)",  _stats(rsi2_lp)),
    ]

    # ── 全体 overall 行 ────────────────────────────────────────────────
    ovr_row = _row("全トレード合計", overall)

    t5  = _table("① シグナル前5日の株価変動別（売り圧力の強さ）", rows_5d)
    tv  = _table("② 直前の出来高スパイク別（群衆参加度）", rows_vol)
    tc  = _table("③ 複合売り圧力スコア別（最良プロキシ: 急落 × 出来高急増）", rows_combo)
    tr2 = _table("④ RSI2 戦略のみ — 複合売り圧力別（逆張り戦略への影響）", rows_rsi2)

    summary_tbl = (
        "<table><thead><tr>"
        "<th style='text-align:left'>-</th>"
        "<th>取引数</th><th>勝率</th><th>PF</th><th>合計損益</th>"
        "<th style='color:#4ade80'>目標%</th>"
        "<th style='color:#f87171'>損切り%</th>"
        "<th style='color:#94a3b8'>タイムカット%</th>"
        "</tr></thead><tbody>"
        f"{ovr_row}"
        "</tbody></table>"
    )

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>売り圧力 × トレード結果 相関分析</title>
<style>{_CSS}</style></head>
<body>
<h1>売り圧力（信用倍率プロキシ）× トレード結果 相関分析</h1>
<p class="note">
  分析期間: 直近{days}日 / 全WATCHLIST({len(set(t['symbol'] for t in trades))}銘柄) /
  総トレード数: {len(trades)}件<br>
  ※ 信用倍率の実績データは取得困難なため、「シグナル前5日株価変動」+「出来高スパイク」を代理変数として使用。
  これらは信用買いが集まりやすい環境（=追証リスクが高い状態）と高い相関を持つ。
</p>
{summary_tbl}
{t5}
<p class="note">↑「①大暴落」の区分で損切り率が上がるなら、急落後シグナルは避けるべきという証拠</p>
{tv}
<p class="note">↑「A 出来高3倍超」の区分で勝率が下がるなら、群衆参加（信用買い集中）は危険信号</p>
{tc}
<p class="note">↑ 最重要テーブル。🔴高圧力（急落+出来高急増）で損切り率が高いなら、信用倍率警告は有効</p>
{tr2}
<p class="note">↑ RSI2は逆張り戦略のため、特に売り圧力との関係が重要。高圧力時に損切りが増えるなら警告は意味あり</p>
</body></html>"""


# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = _args()
    print(f"売り圧力分析: 直近{args.days}日 / workers={args.workers}", flush=True)
    print("トレードデータ収集中...", flush=True)

    trades = collect_trades(args.days, args.workers)
    print(f"収集完了: {len(trades)}件のトレード", flush=True)

    if not trades:
        print("トレードデータなし")
        sys.exit(1)

    # コンソール出力
    from collections import defaultdict
    bins = defaultdict(list)
    for t in trades:
        bins[_bin_label_5d(t["mom_5d"])].append(t)
    print("\n=== シグナル前5日モメンタム別 ===")
    print(f"{'区分':<30} {'取引':>5} {'勝率':>7} {'PF':>6} {'目標%':>7} {'損切%':>7}")
    for k in sorted(bins):
        s = _stats(bins[k])
        pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"{k:<30} {s['n']:>5} {s['wr']:>6.1f}% {pf_s:>6} "
              f"{s['tgt_pct']:>6.1f}% {s['stp_pct']:>6.1f}%")

    html = build_html(trades, args.days)
    out  = Path(f"margin_pressure_{datetime.now(JST).date()}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML出力: {out}", flush=True)
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())
