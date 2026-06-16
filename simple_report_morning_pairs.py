"""
simple_report_morning_pairs.py  - 朝スキャン選定銘柄バックテストレポート
==========================================================================
gen_watchlist_from_walkforward.py が生成した daytrade_winners_*.py を読み込み、
その (sym, name, strat) ペアだけをバックテストして OOS 評価する。

朝のパイプライン:
  scan_walkforward_daytrade.py --all-modes --workers 8
  gen_watchlist_from_walkforward.py --mode standard --min-pf 1.3 --min-trades 20
  [このスクリプト]  ← simple_report_all_shifts.py スタイルの結果レポート

【使い方】
  # daytrade_winners_*.py を自動検出
  python simple_report_morning_pairs.py

  # ファイル直接指定
  python simple_report_morning_pairs.py --watchlist daytrade_winners_2026-06-16.py

  # データソース指定
  python simple_report_morning_pairs.py --source hybrid

  # キャッシュ再作成
  python simple_report_morning_pairs.py --refresh-cache
"""

from __future__ import annotations

import argparse
import glob
import html
import importlib.util
import pickle
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT

JST = timezone(timedelta(hours=9))
ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}
LONG_STRATS = set(STRATEGIES.keys())
SHORT_STRATS = set(STRATEGIES_SHORT.keys())

PERIODS = [30, 60, 90, 120, 150, 180]
CACHE_FILE = Path(".cache_morning_pairs.pkl")
YF_SNAP_DIR = Path("data/yfinance_snapshots")


# ----------------------------------------------------------------- helpers

def detect_latest_snapshot_date() -> str | None:
    if not YF_SNAP_DIR.exists():
        return None
    dates = []
    for d in YF_SNAP_DIR.iterdir():
        if d.is_dir():
            try:
                datetime.strptime(d.name, "%Y-%m-%d")
                dates.append(d.name)
            except Exception:
                pass
    return max(dates) if dates else None


def detect_latest_winners() -> Path | None:
    """daytrade_winners_*.py (シフトなし) の最新を返す。"""
    candidates = []
    for fp in glob.glob("daytrade_winners_*.py"):
        stem = Path(fp).stem
        if "_shift" in stem:
            continue
        parts = stem.split("_")
        for p in parts:
            if len(p) == 10 and p.count("-") == 2:
                try:
                    datetime.strptime(p, "%Y-%m-%d")
                    candidates.append((p, Path(fp)))
                except Exception:
                    pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def load_watchlist(path: Path) -> list[tuple[str, str, str]]:
    spec = importlib.util.spec_from_file_location("wl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "SYMBOLS", [])
    out = []
    for e in raw:
        if isinstance(e, tuple) and len(e) >= 3:
            out.append((e[0], e[1], e[2]))
        elif isinstance(e, tuple) and len(e) == 2:
            out.append((e[0], e[1], ""))
    return out


def load_backtest_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return pickle.loads(CACHE_FILE.read_bytes())
    except Exception:
        return {}


def save_backtest_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_bytes(pickle.dumps(cache))
    except Exception as e:
        print(f"  [warn] キャッシュ保存失敗: {e}", file=sys.stderr)


def dedup_same_time_same_symbol(trades: list) -> list:
    """同一エントリー時刻・同一銘柄の重複取引を除去する。
    バックテストエンジンが引け強制決済を 2 件生成する場合の対処。"""
    seen: set = set()
    out = []
    for t in sorted(trades, key=lambda x: (x["entry_dt"], x["symbol"], x["strategy"])):
        key = (t["symbol"], t["entry_dt"])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def apply_same_day_lock(pair_trades: dict) -> dict:
    """同日同銘柄に複数戦略がエントリーした場合、最も早い entry_dt の戦略のみ残す。
    holdout_periods_report_daytrade.py と同じロジック (DAYTRADE_SAME_DAY_LOCK=1)。"""
    earliest: dict = {}
    for (sym, strat), trades in pair_trades.items():
        for t in trades:
            dt = t.get("entry_dt")
            if not hasattr(dt, "date"):
                continue
            key = (sym, dt.date())
            cur = earliest.get(key)
            if cur is None or dt < cur[0] or (dt == cur[0] and strat < cur[1]):
                earliest[key] = (dt, strat)
    new_pt: dict = {}
    for (sym, strat), trades in pair_trades.items():
        kept = []
        for t in trades:
            dt = t.get("entry_dt")
            if not hasattr(dt, "date"):
                kept.append(t)
                continue
            sel = earliest.get((sym, dt.date()))
            if sel and sel[1] == strat:
                kept.append(t)
        new_pt[(sym, strat)] = kept
    return new_pt


# TRAIN 合格条件 (holdout_periods_report_daytrade.py と同じ)
PASS_TRAIN_TRADES = 20
PASS_TRAIN_PF = 1.3


def _pass_train(trades: list) -> bool:
    if not trades:
        return False
    st = calc_stats(trades)
    return (st["n"] >= PASS_TRAIN_TRADES
            and st["pf"] >= PASS_TRAIN_PF
            and st["total_pnl"] > 0)


def slice_trades(trades, start_days_ago, end_days_ago, today):
    """end_days_ago 〜 start_days_ago 前の取引を返す。"""
    start = today - timedelta(days=start_days_ago)
    end = today - timedelta(days=end_days_ago)
    inclusive_end = (end_days_ago == 0)
    out = []
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = dt.date()
        if inclusive_end:
            if start <= d <= end:
                out.append(t)
        else:
            if start <= d < end:
                out.append(t)
    return out


# ----------------------------------------------------------------- backtest

def _run_one(sym, name, strat_key, df):
    strat_fn = ALL_STRATEGIES.get(strat_key)
    if strat_fn is None:
        return strat_key, sym, []
    res = backtest_symbol_5m(sym, name, df, strat_fn,
                              strategy_params={"name": strat_key})
    if not res:
        return strat_key, sym, []
    trades = []
    for t in res["trades"]:
        trades.append({
            "symbol": sym, "name": name, "strategy": strat_key,
            "entry_dt": t["entry_dt"], "exit_dt": t["exit_dt"],
            "entry_p": t["entry_p"], "exit_p": t["exit_p"],
            "stop_p": t["stop_p"], "target_p": t["target_p"],
            "qty": t["qty"], "pnl": t["pnl"], "pct": t["pct"],
            "side": t["side"], "reason": t["reason"],
        })
    return strat_key, sym, trades


# ----------------------------------------------------------------- pair score (BT ベース)

def compute_bt_pair_score(trades_full: list, today) -> float:
    """全履歴から BT スコア的なペアスコアを算出 (0-60 スケール)。

    6 期間それぞれの stats を平均した win_rate と PF を使って
    Q スコアの base として使えるスケール (0-60) にマッピングする。

    ペア選定済みなので pair_score は「朝スキャンで通過した」という
    事実を反映しつつ、各期間の OOS PF で微調整する。
    """
    if not trades_full:
        return 30.0  # デフォルト中立値

    pf_list = []
    wr_list = []
    for P in PERIODS:
        sl = slice_trades(trades_full, P, 0, today)
        if not sl:
            continue
        wins = sum(t["pnl"] for t in sl if t["pnl"] > 0)
        losses = -sum(t["pnl"] for t in sl if t["pnl"] < 0)
        pf = wins / losses if losses > 0 else (3.0 if wins > 0 else 0.0)
        wr = sum(1 for t in sl if t["pnl"] > 0) / len(sl)
        pf_list.append(min(pf, 4.0))
        wr_list.append(wr)

    if not pf_list:
        return 30.0

    avg_pf = sum(pf_list) / len(pf_list)
    avg_wr = sum(wr_list) / len(wr_list)
    # PF 1.5 / WR 0.50 → score 45 が目安
    pf_factor = max(0.0, min(1.0, (avg_pf - 0.8) / 2.5))
    wr_factor = max(0.0, min(1.0, avg_wr / 0.65))
    score = 20.0 + 40.0 * pf_factor * wr_factor
    return round(score, 1)


# ----------------------------------------------------------------- Q スコア

def annotate_q_state(pair_trades: dict) -> None:
    """全取引を日付ごとに時系列処理し look-ahead-free な日中状態を付与。"""
    all_trades = []
    for trades in pair_trades.values():
        all_trades.extend(trades)
    by_day = defaultdict(list)
    for t in all_trades:
        if hasattr(t.get("entry_dt"), "date"):
            by_day[t["entry_dt"].date()].append(t)
    for day_trades in by_day.values():
        day_trades.sort(key=lambda x: x["entry_dt"])
        sym_count = defaultdict(int)
        sym_had_loss = defaultdict(bool)
        sym_last_entry_dt = {}
        cum_pnl = 0
        for t in day_trades:
            sym = t["symbol"]
            if sym_last_entry_dt.get(sym) == t["entry_dt"]:
                t["same_day_n"] = sym_count[sym]
            else:
                sym_count[sym] += 1
                t["same_day_n"] = sym_count[sym]
                sym_last_entry_dt[sym] = t["entry_dt"]
            t["same_day_loss_prior"] = sym_had_loss[sym]
            t["daily_pnl_prior"] = cum_pnl
            if t["pnl"] < 0:
                sym_had_loss[sym] = True
            cum_pnl += t["pnl"]


def _q_time_adj(dt) -> int:
    try:
        m = dt.hour * 60 + dt.minute
    except Exception:
        return 0
    if m < 9 * 60 + 35 or m >= 14 * 60 + 30:
        return -5
    return 5


def _q_count_adj(n: int) -> int:
    if n <= 1:
        return 10
    if n == 2:
        return 0
    if n == 3:
        return -10
    return -15


def compute_q_score(t, pair_score: float) -> dict:
    time_adj = _q_time_adj(t["entry_dt"])
    n = t.get("same_day_n", 1)
    count_adj = _q_count_adj(n)
    loss_adj = -10 if t.get("same_day_loss_prior") else 0
    pnl_prior = t.get("daily_pnl_prior", 0)
    pnl_adj = 5 if pnl_prior >= 10_000 else (-5 if pnl_prior <= -10_000 else 0)
    stop_pct = 0.0
    try:
        stop_pct = abs(t["entry_p"] - t["stop_p"]) / t["entry_p"] * 100
    except Exception:
        pass
    q = max(0.0, min(100.0,
                      pair_score + time_adj + count_adj + loss_adj + pnl_adj))
    return {
        "q_score": q,
        "pair_score": pair_score,
        "time_adj": time_adj,
        "count_adj": count_adj,
        "loss_adj": loss_adj,
        "pnl_adj": pnl_adj,
        "same_day_n": n,
        "same_day_loss_prior": t.get("same_day_loss_prior", False),
        "daily_pnl_prior": pnl_prior,
        "stop_pct": stop_pct,
    }


def trade_tier(pair_score: float, q_score: float) -> str:
    if pair_score >= 50 and q_score >= 60:
        return "S"
    if pair_score >= 40 and q_score >= 50:
        return "A"
    if pair_score >= 30 and q_score >= 40:
        return "B"
    return "C"


def _tier_color(tier: str) -> str:
    return {"S": "#10b981", "A": "#4ade80",
             "B": "#facc15", "C": "#64748b"}.get(tier, "#94a3b8")


def _q_color(q: float) -> str:
    if q >= 75:
        return "#10b981"
    if q >= 65:
        return "#4ade80"
    if q >= 55:
        return "#facc15"
    return "#f87171"


def _ps_color(s: float) -> str:
    if s >= 50:
        return "#4ade80"
    if s >= 35:
        return "#facc15"
    return "#94a3b8"


# ----------------------------------------------------------------- HTML helpers

def _fmt_dt(dt):
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def _hold_min(t):
    try:
        return int((t["exit_dt"] - t["entry_dt"]).total_seconds() / 60)
    except Exception:
        return 0


def aggregate_daily(trades):
    by_date = defaultdict(list)
    for t in trades:
        d = t["entry_dt"].date()
        by_date[d].append(t)
    out = []
    cumulative = 0
    for d in sorted(by_date.keys()):
        days = by_date[d]
        pnl = sum(t["pnl"] for t in days)
        profit = sum(t["pnl"] for t in days if t["pnl"] > 0)
        loss = -sum(t["pnl"] for t in days if t["pnl"] < 0)
        wins = sum(1 for t in days if t["pnl"] > 0)
        cumulative += pnl
        out.append({
            "date": d, "n": len(days), "wins": wins,
            "win_rate": wins / len(days) * 100,
            "pnl": pnl, "profit": profit, "loss": loss,
            "cumulative": cumulative,
        })
    return out


def _pf_color(pf: float) -> str:
    if pf == float("inf") or pf >= 1.5:
        return "#4ade80"
    if pf >= 1.0:
        return "#facc15"
    return "#f87171"


def _pf_str(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def render_section(trades: list, label: str,
                    pair_scores: dict, tab_uid: str) -> str:
    """1セクション: サマリ + Q閾値サマリ + 日付別損益 + 全取引テーブル。"""
    if not trades:
        return "<p style='color:#94a3b8'>取引なし</p>"

    overall = calc_stats(trades)
    pf = overall["pf"]
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)

    def _qs(t):
        ps = pair_scores.get((t["symbol"], t["strategy"]), 30.0)
        return compute_q_score(t, ps)

    trades_sorted = sorted(trades, key=lambda x: (-_qs(x)["q_score"],
                                                    x["entry_dt"]))

    H = []
    # サマリ
    H.append("<div class='summary'>")
    for lbl, val, cls in [
        ("総取引", f"{overall['n']:,}", ""),
        ("勝率", f"{overall['win_rate']:.0f}%", ""),
        ("PF", _pf_str(pf), f"style='color:{_pf_color(pf)}'"),
        ("総利益", f"+{gross_profit:,.0f}円", "pos"),
        ("総損失", f"-{gross_loss:,.0f}円", "neg"),
        ("差引損益", f"{overall['total_pnl']:+,.0f}円",
         "pos" if overall["total_pnl"] >= 0 else "neg"),
        ("平均利益", f"{overall['avg_win']:+,.0f}", "pos"),
        ("平均損失", f"{overall['avg_loss']:+,.0f}", "neg"),
        ("最大DD", f"{overall['max_dd']:.1f}%", "neg"),
    ]:
        style = cls if cls.startswith("style=") else (
            f"class='{cls}'" if cls else "")
        H.append(f"<div class='card'><div class='lbl'>{lbl}</div>"
                 f"<div class='val {style.replace(chr(39),'')}'>{val}</div></div>")
    H.append("</div>")

    # Q 閾値別サマリ
    for threshold, label_str in [(65, "Q>=65 推奨"),
                                   (55, "Q>=55 標準"),
                                   (45, "Q>=45 緩")]:
        hq = [t for t in trades if _qs(t)["q_score"] >= threshold]
        if not hq:
            continue
        hst = calc_stats(hq)
        hgp = sum(t["pnl"] for t in hq if t["pnl"] > 0)
        hgl = -sum(t["pnl"] for t in hq if t["pnl"] < 0)
        H.append(f"<h3>{label_str}</h3>")
        H.append("<div class='summary'>")
        for lbl, val, cls in [
            ("取引", f"{hst['n']:,} / {overall['n']:,}", ""),
            ("勝率", f"{hst['win_rate']:.0f}%", ""),
            ("PF", _pf_str(hst['pf']),
             f"color:{_pf_color(hst['pf'])}"),
            ("総利益", f"+{hgp:,.0f}円", "pos"),
            ("総損失", f"-{hgl:,.0f}円", "neg"),
            ("差引損益", f"{hst['total_pnl']:+,.0f}円",
             "pos" if hst["total_pnl"] >= 0 else "neg"),
            ("最大DD", f"{hst['max_dd']:.1f}%", "neg"),
        ]:
            H.append(f"<div class='card'><div class='lbl'>{lbl}</div>"
                     f"<div class='val' style='color:inherit'>{val}</div></div>")
        H.append("</div>")

    # 日付別損益
    H.append("<h3>日付別損益</h3>")
    H.append("<table><thead><tr>"
             "<th>日付</th><th class='right'>取引</th>"
             "<th class='right'>勝率</th>"
             "<th class='right pos'>利益</th>"
             "<th class='right neg'>損失</th>"
             "<th class='right'>差引</th>"
             "<th class='right'>累積</th>"
             "</tr></thead><tbody>")
    for d in reversed(aggregate_daily(trades)):
        pnl_c = "pos" if d["pnl"] >= 0 else "neg"
        cum_c = "pos" if d["cumulative"] >= 0 else "neg"
        H.append(f"<tr>"
                 f"<td>{d['date']}</td>"
                 f"<td class='right'>{d['n']}</td>"
                 f"<td class='right'>{d['win_rate']:.0f}%</td>"
                 f"<td class='right pos'>+{d['profit']:,.0f}</td>"
                 f"<td class='right neg'>-{d['loss']:,.0f}</td>"
                 f"<td class='right {pnl_c}'>{d['pnl']:+,.0f}</td>"
                 f"<td class='right {cum_c}'>{d['cumulative']:+,.0f}</td>"
                 f"</tr>")
    H.append("</tbody></table>")

    # 全取引テーブル (Q スコアフィルタ付き)
    H.append(f"<h3>全取引 (<span id='{tab_uid}-cnt'>"
              f"{len(trades_sorted):,}</span>件)</h3>")
    H.append(f"<div class='score-filters'>"
              f"<span style='color:#94a3b8;font-size:11px;margin-right:8px;'>"
              f"Q スコア:</span>"
              f"<button class='score-btn active' "
              f"onclick=\"filterScore('{tab_uid}',0,this)\">全件</button>"
              f"<button class='score-btn' "
              f"onclick=\"filterScore('{tab_uid}',45,this)\">>=45</button>"
              f"<button class='score-btn' "
              f"onclick=\"filterScore('{tab_uid}',55,this)\">>=55</button>"
              f"<button class='score-btn' "
              f"onclick=\"filterScore('{tab_uid}',65,this)\">>=65</button>"
              f"<button class='score-btn' "
              f"onclick=\"filterScore('{tab_uid}',75,this)\">>=75</button>"
              f"</div>")
    H.append(f"<table id='{tab_uid}-tbl'><thead><tr>"
              "<th>銘柄</th><th>戦略</th>"
              "<th class='right'>Pair</th>"
              "<th class='right'>Q</th>"
              "<th class='center'>Tier</th>"
              "<th class='center'>時刻</th>"
              "<th class='center'>同日</th>"
              "<th class='center'>損切歴</th>"
              "<th class='center'>累積</th>"
              "<th class='center'>Side</th>"
              "<th>Entry</th><th>Exit</th>"
              "<th class='right'>保有</th>"
              "<th class='right'>買値</th>"
              "<th class='right'>損切</th>"
              "<th class='right'>目標</th>"
              "<th class='right'>決済</th>"
              "<th class='right'>損益</th>"
              "<th class='right'>%</th>"
              "<th>理由</th>"
              "</tr></thead><tbody>")
    for t in trades_sorted:
        q = _qs(t)
        ps = q["pair_score"]
        qs = q["q_score"]
        tier = trade_tier(ps, qs)
        pnl_c = "pos" if t["pnl"] >= 0 else "neg"
        side_s = "L" if t["side"] == "long" else "S"
        loss_mark = "x" if q["same_day_loss_prior"] else "-"
        pnl_prior_lbl = f"{q['daily_pnl_prior']/1000:+.0f}K"
        tip = (f"Q = pair({ps:.0f})"
                f"+time({q['time_adj']:+d})"
                f"+count({q['count_adj']:+d})"
                f"+loss({q['loss_adj']:+d})"
                f"+pnl({q['pnl_adj']:+d})"
                f"={qs:.0f} stop={q['stop_pct']:.2f}%")
        H.append(
            f"<tr class='trade-row' data-score='{qs:.2f}'>"
            f"<td>{html.escape(t['name'])}</td>"
            f"<td>{html.escape(t['strategy'])}</td>"
            f"<td class='right' style='color:{_ps_color(ps)};'>{ps:.0f}</td>"
            f"<td class='right' title='{html.escape(tip)}' "
            f"style='color:{_q_color(qs)};font-weight:bold;'>{qs:.0f}</td>"
            f"<td class='center' style='color:{_tier_color(tier)};font-weight:bold;'>"
            f"{tier}</td>"
            f"<td class='center small'>{q['time_adj']:+d}</td>"
            f"<td class='center small'>{q['same_day_n']}回</td>"
            f"<td class='center small'>{loss_mark}</td>"
            f"<td class='center small'>{pnl_prior_lbl}</td>"
            f"<td class='center'>{side_s}</td>"
            f"<td>{_fmt_dt(t['entry_dt'])}</td>"
            f"<td>{_fmt_dt(t['exit_dt'])}</td>"
            f"<td class='right'>{_hold_min(t)}分</td>"
            f"<td class='right'>{t['entry_p']:,.0f}</td>"
            f"<td class='right'>{t['stop_p']:,.0f}</td>"
            f"<td class='right'>{t['target_p']:,.0f}</td>"
            f"<td class='right'>{t['exit_p']:,.0f}</td>"
            f"<td class='right {pnl_c}'>{t['pnl']:+,.0f}</td>"
            f"<td class='right {pnl_c}'>{t['pct']:+.2f}%</td>"
            f"<td>{html.escape(t['reason'])}</td>"
            f"</tr>"
        )
    H.append("</tbody></table>")
    return "\n".join(H)


# ----------------------------------------------------------------- HTML

CSS = """
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a;color:#e2e8f0;padding:24px;margin:0;}
  h1{font-size:20px;margin:0 0 4px;color:#10b981;}
  h2{font-size:16px;margin:18px 0 8px;color:#cbd5e1;
     border-left:3px solid #10b981;padding-left:10px;}
  h3{font-size:14px;margin:12px 0 6px;color:#a3a3a3;}
  .meta{color:#94a3b8;font-size:13px;margin-bottom:16px;}
  .summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
  .card{background:#1e293b;padding:10px 14px;border-radius:6px;min-width:90px;}
  .card .lbl{color:#94a3b8;font-size:11px;}
  .card .val{font-size:16px;font-weight:bold;margin-top:3px;}
  table{width:100%;border-collapse:collapse;background:#1e293b;
        font-size:12px;margin-bottom:14px;}
  th{background:#334155;color:#cbd5e1;padding:6px 8px;text-align:left;font-weight:600;}
  td{padding:4px 8px;border-top:1px solid #334155;}
  tr:hover td{background:#293548;}
  .pos{color:#4ade80;} .neg{color:#f87171;}
  .small{font-size:10px;color:#94a3b8;}
  .right{text-align:right;} .center{text-align:center;}
  .tabs{display:flex;gap:4px;margin:14px 0 12px;
        border-bottom:1px solid #334155;flex-wrap:wrap;}
  .tab{padding:8px 14px;background:#1e293b;color:#94a3b8;border:none;
       cursor:pointer;font-size:13px;border-radius:6px 6px 0 0;}
  .tab.active{background:#10b981;color:#fff;}
  .tab-content{display:none;}
  .tab-content.active{display:block;}
  .sub-tabs{display:flex;gap:3px;margin:8px 0;flex-wrap:wrap;}
  .sub-tab{padding:5px 10px;background:#334155;color:#cbd5e1;border:none;
           cursor:pointer;font-size:12px;border-radius:4px;}
  .sub-tab.active{background:#0ea5e9;color:#fff;}
  .sub-content{display:none;}
  .sub-content.active{display:block;}
  .score-filters{margin:6px 0 10px;display:flex;gap:4px;align-items:center;flex-wrap:wrap;}
  .score-btn{padding:4px 10px;background:#334155;color:#cbd5e1;border:none;
              cursor:pointer;font-size:11px;border-radius:4px;}
  .score-btn.active{background:#10b981;color:#fff;}
  .pair-tbl td,.pair-tbl th{padding:3px 6px;}
</style>
"""

JS = """
<script>
function showSide(side){
  document.querySelectorAll('.side-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.side-content').forEach(c=>c.classList.remove('active'));
  document.getElementById('side-tab-'+side).classList.add('active');
  document.getElementById('side-content-'+side).classList.add('active');
}
function showPeriod(side,period){
  document.querySelectorAll('.period-tab-'+side).forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.period-content-'+side).forEach(c=>c.classList.remove('active'));
  document.getElementById('period-tab-'+side+'-'+period).classList.add('active');
  document.getElementById('period-content-'+side+'-'+period).classList.add('active');
}
function filterScore(tabUid,minScore,btn){
  const tbl=document.getElementById(tabUid+'-tbl');
  if(!tbl)return;
  const rows=tbl.querySelectorAll('tbody tr.trade-row');
  let visible=0;
  rows.forEach(r=>{
    const s=parseFloat(r.dataset.score||'0');
    if(s>=minScore){r.style.display='';visible++;}
    else{r.style.display='none';}
  });
  const cnt=document.getElementById(tabUid+'-cnt');
  if(cnt)cnt.textContent=visible.toLocaleString();
  if(btn){
    btn.parentElement.querySelectorAll('.score-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  }
}
</script>
"""


def render_pair_summary(pairs: list, pair_scores: dict) -> str:
    """採用ペア一覧テーブル (スコア付き)。"""
    long_pairs = [(sym, name, strat) for sym, name, strat in pairs
                   if strat in LONG_STRATS]
    short_pairs = [(sym, name, strat) for sym, name, strat in pairs
                    if strat in SHORT_STRATS]
    H = []
    H.append("<h2>採用ペア一覧 (朝スキャン選定)</h2>")
    H.append(f"<p class='meta'>ロング {len(long_pairs)} ペア / "
              f"ショート {len(short_pairs)} ペア / "
              f"合計 {len(pairs)} ペア</p>")
    H.append("<table class='pair-tbl'><thead><tr>"
              "<th>銘柄</th><th>コード</th><th>戦略</th>"
              "<th class='right'>Pair スコア</th>"
              "<th class='right'>Side</th>"
              "</tr></thead><tbody>")
    sorted_pairs = sorted(pairs,
                           key=lambda x: -pair_scores.get((x[0], x[2]), 30.0))
    for sym, name, strat in sorted_pairs:
        ps = pair_scores.get((sym, strat), 30.0)
        side = "Long" if strat in LONG_STRATS else "Short"
        H.append(f"<tr>"
                 f"<td>{html.escape(name)}</td>"
                 f"<td>{sym}</td>"
                 f"<td>{html.escape(strat)}</td>"
                 f"<td class='right' style='color:{_ps_color(ps)};'>{ps:.1f}</td>"
                 f"<td class='right'>{side}</td>"
                 f"</tr>")
    H.append("</tbody></table>")
    return "\n".join(H)


def render_html(*, pairs: list, pair_trades: dict, pair_scores: dict,
                period_trades_by_side: dict, today, source: str,
                watchlist_label: str) -> str:
    """メイン HTML 生成。"""
    H = []
    H.append("<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>")
    H.append(f"<title>朝スキャン銘柄レポート ({today})</title>")
    H.append(CSS)
    H.append(JS)
    H.append("</head><body>")
    H.append(f"<h1>朝スキャン選定銘柄 バックテストレポート</h1>")
    H.append(f"<div class='meta'>"
              f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} / "
              f"watchlist: {watchlist_label} / "
              f"source: {source} / "
              f"銘柄数: {len(set(sym for sym,_,_ in pairs))} / "
              f"ペア数: {len(pairs)}"
              f"</div>")

    H.append(render_pair_summary(pairs, pair_scores))

    # Long / Short タブ
    H.append("<h2>取引結果 (OOS 期間別)</h2>")
    H.append("<p class='meta'>"
              "各期間タブ: その直近 N日の取引のみ表示。"
              "「全体」は 180 日分の全取引。"
              "Pair スコア = BT 全履歴から算出した 0-60 の品質指標。"
              "Q スコア = Pair + 時刻 + 同日回数 + 損切歴 + 累積 PnL。"
              "</p>")
    H.append("<div class='tabs'>")
    H.append("<button class='tab side-tab active' id='side-tab-long' "
              "onclick=\"showSide('long')\">Long</button>")
    H.append("<button class='tab side-tab' id='side-tab-short' "
              "onclick=\"showSide('short')\">Short</button>")
    H.append("</div>")

    period_labels = [("all", "全体 (180日)")] + [
        (str(P), f"直近{P}日") for P in PERIODS]

    for side in ("long", "short"):
        active_cls = "active" if side == "long" else ""
        H.append(f"<div class='tab-content side-content {active_cls}' "
                  f"id='side-content-{side}'>")

        H.append("<div class='sub-tabs'>")
        for i, (pid, plabel) in enumerate(period_labels):
            ac = "active" if i == 0 else ""
            H.append(f"<button class='sub-tab period-tab-{side} {ac}' "
                      f"id='period-tab-{side}-{pid}' "
                      f"onclick=\"showPeriod('{side}','{pid}')\">"
                      f"{plabel}</button>")
        H.append("</div>")

        for i, (pid, plabel) in enumerate(period_labels):
            ac = "active" if i == 0 else ""
            H.append(f"<div class='sub-content period-content-{side} {ac}' "
                      f"id='period-content-{side}-{pid}'>")
            trades = period_trades_by_side.get((pid, side), [])
            H.append(f"<h3>{plabel} — {side.upper()} ({len(trades):,}件)</h3>")
            tab_uid = f"sec-{side}-{pid}"
            H.append(render_section(trades, plabel, pair_scores, tab_uid))
            H.append("</div>")

        H.append("</div>")

    H.append("</body></html>")
    return "\n".join(H)


# ----------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--watchlist", default=None,
                   help="daytrade_winners_*.py のパス。省略時自動検出")
    p.add_argument("--source", default="hybrid",
                   choices=["auto", "local", "yfinance", "hybrid"])
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--snapshot-date", default=None)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--output", default=None)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--refresh-cache", action="store_true")
    args = p.parse_args()

    # snapshot_date
    if args.snapshot_date is None:
        snap = detect_latest_snapshot_date()
        if snap:
            args.snapshot_date = snap
            print(f"  snapshot_date 自動検出: {snap}", flush=True)
        else:
            args.snapshot_date = datetime.now(JST).date().isoformat()

    # watchlist
    if args.watchlist:
        wl_path = Path(args.watchlist)
    else:
        wl_path = detect_latest_winners()
    if wl_path is None or not wl_path.exists():
        print("[error] daytrade_winners_*.py が見つかりません。"
              "gen_watchlist_from_walkforward.py を先に実行するか "
              "--watchlist で指定してください。")
        return

    print(f"watchlist: {wl_path}", flush=True)
    pairs = load_watchlist(wl_path)
    if not pairs:
        print("[error] watchlist が空です")
        return

    long_p = [(s, n, st) for s, n, st in pairs if st in LONG_STRATS]
    short_p = [(s, n, st) for s, n, st in pairs if st in SHORT_STRATS]
    print(f"ペア数: {len(pairs)} (Long {len(long_p)}, Short {len(short_p)})",
          flush=True)

    # バックテストキャッシュ
    cache_full = {} if args.refresh_cache else load_backtest_cache()
    cache_key_prefix = f"{args.snapshot_date}::{args.source}"

    pair_trades: dict[tuple, list] = {}
    pairs_needing_bt = []
    for sym, name, strat in pairs:
        ck = f"{cache_key_prefix}::{sym}::{strat}"
        if ck in cache_full:
            pair_trades[(sym, strat)] = cache_full[ck]
        else:
            pairs_needing_bt.append((sym, name, strat))

    print(f"キャッシュヒット: {len(pair_trades)}/{len(pairs)} / "
          f"再計算: {len(pairs_needing_bt)}ペア", flush=True)

    if pairs_needing_bt:
        syms_to_load = sorted(set(s for s, _, _ in pairs_needing_bt))
        print(f"\n[Step 1] データロード ({len(syms_to_load)}銘柄, "
              f"source={args.source})", flush=True)
        fetched = load_intraday_batch(
            syms_to_load, args.days, source=args.source,
            snapshot_date=args.snapshot_date,
        )
        print(f"  ロード: {len(fetched)}/{len(syms_to_load)}銘柄", flush=True)

        specs = [(sym, name, strat, fetched[sym])
                 for sym, name, strat in pairs_needing_bt
                 if sym in fetched]
        print(f"\n[Step 2] バックテスト ({len(specs)}ペア)", flush=True)
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_run_one, *s): s for s in specs}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    strat_key, sym, trades = fut.result()
                except Exception as e:
                    spec = futs[fut]
                    print(f"  [error] {spec[0]}/{spec[2]}: {e}")
                    continue
                pair_trades[(sym, strat_key)] = trades
                ck = f"{cache_key_prefix}::{sym}::{strat_key}"
                cache_full[ck] = trades
                if done % 10 == 0 or done == len(specs):
                    print(f"  {done}/{len(specs)} 完了", flush=True)

        save_backtest_cache(cache_full)
        print(f"  キャッシュ保存: {len(cache_full)}件", flush=True)

    # 重複取引排除 (引け強制決済が 2 件生成される場合の対処)
    for key in list(pair_trades.keys()):
        pair_trades[key] = dedup_same_time_same_symbol(pair_trades[key])

    # SAME_DAY_LOCK: 同日同銘柄で複数戦略が発火した場合、最初の entry_dt の戦略のみ残す
    # holdout_periods_report_daytrade.py と同じ (ロング側/ショート側で独立に適用)
    long_pt = {k: v for k, v in pair_trades.items() if k[1] in LONG_STRATS}
    short_pt = {k: v for k, v in pair_trades.items() if k[1] in SHORT_STRATS}
    before_lock = sum(len(v) for v in pair_trades.values())
    long_pt = apply_same_day_lock(long_pt)
    short_pt = apply_same_day_lock(short_pt)
    pair_trades = {**long_pt, **short_pt}
    after_lock = sum(len(v) for v in pair_trades.values())
    print(f"  [SAME_DAY_LOCK] {before_lock:,} → {after_lock:,} (-{before_lock - after_lock:,}件 重複排除)", flush=True)

    # ペアスコア計算
    today = datetime.now(JST).date()
    print(f"\n[Step 3] ペアスコア計算", flush=True)
    pair_scores: dict[tuple, float] = {}
    for sym, name, strat in pairs:
        trades_full = pair_trades.get((sym, strat), [])
        pair_scores[(sym, strat)] = compute_bt_pair_score(trades_full, today)

    # Q state 注釈 (全取引に対して一括)
    annotate_q_state(pair_trades)

    # 期間別 × side 別に取引を振り分け (TRAIN ゲート付き)
    # holdout_periods_report_daytrade.py と同じ:
    #   各期間 P で TRAIN (P日より前の全取引) が PF>=1.3 & 取引>=20 & 損益>0 を
    #   満たすペアのみ TEST (直近 P 日) の取引をカウントする
    print(f"\n[Step 4] 期間別集計 (TRAIN ゲート付き)", flush=True)
    period_trades_by_side: dict[tuple, list] = defaultdict(list)

    for (sym, strat), trades in pair_trades.items():
        if not trades:
            continue
        side = "long" if strat in LONG_STRATS else "short"

        # "all" (180日) タブ: TRAIN = 180日より前
        train_180 = [t for t in trades
                     if hasattr(t.get("entry_dt"), "date")
                     and t["entry_dt"].date() < today - timedelta(days=180)]
        if _pass_train(train_180):
            all_in_180 = slice_trades(trades, 180, 0, today)
            period_trades_by_side[("all", side)].extend(all_in_180)

        for P in PERIODS:
            test_cutoff = today - timedelta(days=P)
            train_trades = [t for t in trades
                            if hasattr(t.get("entry_dt"), "date")
                            and t["entry_dt"].date() < test_cutoff]
            if not _pass_train(train_trades):
                continue
            sl = slice_trades(trades, P, 0, today)
            period_trades_by_side[(str(P), side)].extend(sl)

    # 集計サマリ表示
    for pid, plabel in [("all", "全体(180日)")] + [(str(P), f"直近{P}日") for P in PERIODS]:
        lt = period_trades_by_side.get((pid, "long"), [])
        st = period_trades_by_side.get((pid, "short"), [])
        if lt or st:
            l_st = calc_stats(lt)
            s_st = calc_stats(st)
            print(f"  {plabel}: Long {l_st['n']}件 PF {_pf_str(l_st['pf'])} "
                  f"/ Short {s_st['n']}件 PF {_pf_str(s_st['pf'])}", flush=True)

    # HTML 生成
    html_str = render_html(
        pairs=pairs,
        pair_trades=pair_trades,
        pair_scores=pair_scores,
        period_trades_by_side=period_trades_by_side,
        today=today,
        source=args.source,
        watchlist_label=wl_path.name,
    )

    out_path = Path(args.output or f"morning_pairs_report_{today}.html")
    out_path.write_text(html_str, encoding="utf-8")
    print(f"\n出力: {out_path}", flush=True)

    if not args.no_open:
        url = f"file:///{str(out_path).replace(chr(92), '/')}"
        try:
            from _open_html import open_html
            open_html(out_path)
        except Exception:
            try:
                webbrowser.open(url)
            except Exception:
                pass


if __name__ == "__main__":
    main()
