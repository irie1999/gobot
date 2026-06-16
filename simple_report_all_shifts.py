"""
simple_report_all_shifts.py - 6 shift OOS 結果を1つの HTML にまとめ

各 shift 用 watchlist で backtest し、対応する OOS 期間 (直近 N日) の取引のみを
表示。Long / Short タブと、各 shift のサブタブで切替。

【使い方】
  # 自動検出 (最新日付の daytrade_winners_<date>_shift*.py を全部読む)
  python simple_report_all_shifts.py

  # 日付指定
  python simple_report_all_shifts.py --date 2026-06-16

  # データソース
  python simple_report_all_shifts.py --source hybrid
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

SHIFTS = [30, 60, 90, 120, 150, 180]
# 全体タブ用のキー (union of all shifts)
SHIFT_ALL = "all"  # 全体タブ識別子

CACHE_FILE = Path(".cache_simple_report.pkl")
YF_SNAP_DIR = Path("data/yfinance_snapshots")


def detect_latest_snapshot_date() -> str | None:
    """data/yfinance_snapshots 配下の最新日付ディレクトリを返す."""
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


def load_backtest_cache() -> dict:
    """バックテスト結果キャッシュをロード."""
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


def _open_in_edge(path: Path) -> None:
    """Microsoft Edge で HTML を開く。失敗時はデフォルトブラウザ."""
    url = f"file:///{str(path).replace(chr(92), '/')}"
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidates:
        if Path(exe).exists():
            try:
                import subprocess
                subprocess.Popen([exe, url])
                return
            except Exception:
                continue
    # フォールバック: webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


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


def detect_latest_date() -> str | None:
    dates = set()
    for fp in glob.glob("daytrade_winners_*_shift*d.py"):
        stem = Path(fp).stem
        # daytrade_winners_<date>_shift<N>d
        parts = stem.split("_")
        for p in parts:
            if len(p) == 10 and p.count("-") == 2:
                try:
                    datetime.strptime(p, "%Y-%m-%d")
                    dates.add(p)
                except Exception:
                    pass
    return max(dates) if dates else None


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


def dedup_same_time_same_symbol(trades: list) -> list:
    """同じ時刻に同じ銘柄の取引を1つに集約。

    entry_dt + symbol が同じなら、エンジンが複数戦略で同じシグナルを
    出している = 実運用では1ポジションしか持てない。
    決定論的に最初のもの (戦略名アルファベット順) を残す。
    """
    seen = set()
    out = []
    for t in sorted(trades, key=lambda x: (x["entry_dt"],
                                             x["symbol"],
                                             x["strategy"])):
        key = (t["symbol"], t["entry_dt"])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def apply_same_day_lock(pair_trades: dict) -> dict:
    """同日同銘柄に複数戦略がエントリーした場合、最も早い entry_dt の戦略のみ残す。
    ロング側/ショート側で独立に適用する。"""
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


def build_pair_selection(all_specs_by_shift: dict) -> dict:
    """(sym, strat) → そのペアを選んだ shift 集合."""
    sel = defaultdict(set)
    for s, pairs in all_specs_by_shift.items():
        for sym, _name, strat in pairs:
            sel[(sym, strat)].add(s)
    return sel


def compute_pair_score(trades_full: list, selecting_shifts: set,
                        today) -> dict:
    """各 shift が除外していた直近 N日 (= OOS) の成績で
    (sym, strategy) ペアをスコアリング.

    score = 100 × robustness × oos_pf × oos_wr × sample

    - robustness: 選ばれた shift 数 / 6
    - oos_pf:     各選定 shift の直近 N日の PF を平均し正規化
    - oos_wr:     各選定 shift の直近 N日の勝率を平均し正規化
    - sample:     最長 shift の OOS 取引数 / 30 (信頼度)

    考察: 同じ (sym, strat) が複数 shift で選ばれた場合、
    各 shift の直近 N日成績を「独立した OOS 評価」として
    平均し、選定 shift 数で robustness を加算する.
    すなわち同一ペアは shift をまたいで 1つのスコアに集約.
    """
    info = {
        "score": 0.0, "robustness": 0.0,
        "avg_oos_pf": 0.0, "avg_oos_winrate": 0.0,
        "oos_trades": 0, "oos_pnl": 0.0,
        "selecting_shifts": sorted(selecting_shifts),
        "n_shifts": len(selecting_shifts),
        "per_shift_pf": {},
        "per_shift_pnl": {},
    }
    if not selecting_shifts:
        return info

    # 最長 shift = 最大 OOS 窓 (取引総数と総 PnL に使用)
    max_shift = max(selecting_shifts)
    cutoff_max = today - timedelta(days=max_shift)
    oos_max = [t for t in trades_full
                if hasattr(t.get("entry_dt"), "date")
                and t["entry_dt"].date() >= cutoff_max]
    info["oos_trades"] = len(oos_max)
    info["oos_pnl"] = sum(t["pnl"] for t in oos_max)
    info["oos_profit"] = sum(t["pnl"] for t in oos_max if t["pnl"] > 0)
    info["oos_loss"] = -sum(t["pnl"] for t in oos_max if t["pnl"] < 0)
    info["robustness"] = len(selecting_shifts) / 6

    per_pf = []
    per_wr = []
    for s in sorted(selecting_shifts):
        cutoff = today - timedelta(days=s)
        oos = [t for t in trades_full
                if hasattr(t.get("entry_dt"), "date")
                and t["entry_dt"].date() >= cutoff]
        if not oos:
            info["per_shift_pf"][s] = 0.0
            info["per_shift_pnl"][s] = 0.0
            continue
        wins = sum(t["pnl"] for t in oos if t["pnl"] > 0)
        losses = -sum(t["pnl"] for t in oos if t["pnl"] < 0)
        pf = wins / losses if losses > 0 else (3.0 if wins > 0 else 0.0)
        winrate = sum(1 for t in oos if t["pnl"] > 0) / len(oos)
        per_pf.append(pf)
        per_wr.append(winrate)
        info["per_shift_pf"][s] = pf
        info["per_shift_pnl"][s] = sum(t["pnl"] for t in oos)

    if not per_pf:
        return info

    avg_pf = sum(per_pf) / len(per_pf)
    avg_wr = sum(per_wr) / len(per_wr)
    info["avg_oos_pf"] = avg_pf
    info["avg_oos_winrate"] = avg_wr

    pf_factor = max(0.0, min(1.0, (avg_pf - 0.8) / 1.5))
    wr_factor = max(0.0, min(1.0, avg_wr / 0.6))
    sample_factor = min(1.0, info["oos_trades"] / 30)

    info["score"] = (100 * info["robustness"]
                      * pf_factor * wr_factor * sample_factor)
    return info


# ユニバース厳選フィルタ (案A)
# robustness ≥ N/6 AND 平均OOS PF ≥ PF AND OOS損益 > 0 を満たすペアのみ採用
PAIR_FILTER_MIN_ROBUSTNESS = 3 / 6
PAIR_FILTER_MIN_PF = 1.3
PAIR_FILTER_MIN_PNL = 0.0

MARKET_FILTER_THR = 1.5  # 前日日経変化率の閾値 (%): 超えた逆方向取引を除外


def pair_passes_filter(sc: dict) -> bool:
    """ユニバース厳選: 複数 shift でロバストかつ OOS で勝てているペアのみ."""
    return (sc.get("robustness", 0) >= PAIR_FILTER_MIN_ROBUSTNESS
             and sc.get("avg_oos_pf", 0) >= PAIR_FILTER_MIN_PF
             and sc.get("oos_pnl", 0) > PAIR_FILTER_MIN_PNL)


def _score_color(score: float) -> str:
    if score >= 50:
        return "#4ade80"
    if score >= 30:
        return "#facc15"
    if score >= 10:
        return "#94a3b8"
    return "#64748b"


def _q_color(q: float) -> str:
    if q >= 75:
        return "#10b981"
    if q >= 65:
        return "#4ade80"
    if q >= 55:
        return "#facc15"
    return "#f87171"


def render_pair_table(pair_scores: dict, side: str,
                       sym_name_map: dict | None = None) -> str:
    """ペア別スコアランキング (1 side 分)."""
    if side == "long":
        items = [(k, v) for k, v in pair_scores.items()
                  if k[1] in LONG_STRATS]
    else:
        items = [(k, v) for k, v in pair_scores.items()
                  if k[1] in SHORT_STRATS]
    items.sort(key=lambda x: (-x[1]["score"], -x[1]["oos_pnl"]))

    n_pass = sum(1 for _, sc in items if pair_passes_filter(sc))
    H = []
    H.append(
        f"<details style='margin:0.8em 0;'>"
        f"<summary style='cursor:pointer;font-size:1.05em;font-weight:bold;"
        f"padding:0.4em 0.6em;border-radius:4px;"
        f"background:#1e293b;list-style:none;display:flex;align-items:center;gap:0.5em;'>"
        f"<span style='font-size:0.85em;color:#94a3b8;'>▶</span>"
        f"ペア別スコア ({n_pass}/{len(items)} 採用 / 高スコア順)"
        f"</summary>"
    )
    H.append("<p class='note' style='margin-top:0.5em;'>"
              "スコア = 100 × robustness × OOS PF × OOS 勝率 × サンプル数. "
              "各 shift が除外していた直近 N日 (= OOS) の成績で評価. "
              "同一 (銘柄, 戦略) は shift をまたいで 1スコアに集約. "
              f"採用条件: robustness ≥ "
              f"{PAIR_FILTER_MIN_ROBUSTNESS*6:.0f}/6 AND "
              f"OOS PF ≥ {PAIR_FILTER_MIN_PF:.1f} AND OOS 損益 > 0."
              "</p>")
    H.append("<table><thead><tr>")
    H.append("<th class='center'>採用</th>"
              "<th>銘柄</th><th>戦略</th>"
              "<th class='right'>選定 shift</th>"
              "<th class='right'>OOS 取引</th>"
              "<th class='right'>OOS PF</th>"
              "<th class='right'>OOS 勝率</th>"
              "<th class='right pos'>OOS 利益</th>"
              "<th class='right neg'>OOS 損失</th>"
              "<th class='right'>OOS 損益</th>"
              "<th class='right'>スコア</th>"
              "</tr></thead><tbody>")
    for (sym, strat), sc in items:
        shifts_lbl = "/".join(str(s) for s in sc["selecting_shifts"])
        score = sc["score"]
        color = _score_color(score)
        pnl_c = "pos" if sc["oos_pnl"] >= 0 else "neg"
        name = sym_name_map.get(sym, sym) if sym_name_map else sym
        passed = pair_passes_filter(sc)
        mark = "✓" if passed else "✗"
        mark_color = "#10b981" if passed else "#64748b"
        row_style = "" if passed else "opacity:0.55;"
        H.append(
            f"<tr style='{row_style}'>"
            f"<td class='center' "
            f"style='color:{mark_color};font-weight:bold;'>{mark}</td>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(strat)}</td>"
            f"<td class='right'>{shifts_lbl}</td>"
            f"<td class='right'>{sc['oos_trades']}</td>"
            f"<td class='right'>{sc['avg_oos_pf']:.2f}</td>"
            f"<td class='right'>{sc['avg_oos_winrate']*100:.0f}%</td>"
            f"<td class='right pos'>+{sc['oos_profit']:,.0f}</td>"
            f"<td class='right neg'>-{sc['oos_loss']:,.0f}</td>"
            f"<td class='right {pnl_c}'>{sc['oos_pnl']:+,.0f}</td>"
            f"<td class='right' "
            f"style='color:{color};font-weight:bold;'>"
            f"{score:.1f}</td>"
            f"</tr>"
        )
    H.append("</tbody></table>")
    H.append("</details>")
    return "\n".join(H)


def annotate_streaks(pair_trades: dict) -> None:
    """各 (sym, strat) の取引を時系列で並べ、エントリー直前までの
    連敗数を t['prev_losses'] に書き込む.

    連敗ストリークは同一 (銘柄, 戦略) の損失取引が連続したカウントで、
    勝ち取引が出るとリセット.
    """
    for trades in pair_trades.values():
        sorted_t = sorted(trades, key=lambda x: x["entry_dt"])
        streak = 0
        for t in sorted_t:
            t["prev_losses"] = streak
            if t["pnl"] < 0:
                streak += 1
            else:
                streak = 0


def _time_factor(dt) -> float:
    """エントリー時刻による品質係数. 中盤 (10:30-13:30) を最良とする."""
    try:
        t = dt.hour * 60 + dt.minute
    except Exception:
        return 0.8
    if 9 * 60 <= t < 9 * 60 + 35:
        return 0.7
    if 9 * 60 + 35 <= t < 9 * 60 + 45:
        return 0.5
    if 9 * 60 + 45 <= t < 10 * 60 + 30:
        return 0.8
    if 10 * 60 + 30 <= t < 13 * 60 + 30:
        return 1.0
    if 13 * 60 + 30 <= t < 14 * 60 + 30:
        return 0.8
    return 0.4


def _stop_pct(entry_p: float, stop_p: float) -> float:
    try:
        return abs(entry_p - stop_p) / entry_p * 100
    except Exception:
        return 99.0


def _stop_factor(entry_p: float, stop_p: float) -> float:
    """ストップまでの距離 (%) による品質係数. 0.7%以内が最良."""
    dist = _stop_pct(entry_p, stop_p)
    if dist < 0.7:
        return 1.0
    if dist < 1.5:
        return 0.85
    if dist < 2.5:
        return 0.5
    return 0.2


def _streak_factor(prev_losses: int) -> float:
    """直前連敗数による品質係数. 連敗後は新規エントリーを抑制."""
    if prev_losses <= 0:
        return 1.0
    if prev_losses == 1:
        return 0.85
    if prev_losses == 2:
        return 0.65
    return 0.4


def compute_trade_quality(t, pair_score: float) -> dict:
    """1取引のエントリー品質を多要素で評価.

    trade_score = pair_score × time × stop × streak

    - time:   エントリー時刻 (中盤が最良)
    - stop:   ストップ距離 (狭いほど良)
    - streak: 直前の連敗 (連敗中は減点)
    """
    tf = _time_factor(t["entry_dt"])
    sf = _stop_factor(t["entry_p"], t["stop_p"])
    rf = _streak_factor(t.get("prev_losses", 0))
    sp = _stop_pct(t["entry_p"], t["stop_p"])
    return {
        "trade_score": pair_score * tf * sf * rf,
        "pair_score": pair_score,
        "time_factor": tf,
        "stop_factor": sf,
        "streak_factor": rf,
        "stop_pct": sp,
        "prev_losses": t.get("prev_losses", 0),
    }


def annotate_q_state(pair_trades: dict) -> None:
    """採用ユニバース全体の取引を日付ごとに時系列で並べ、
    各取引のエントリー直前までの look-ahead-free な日中状態を注釈.

    - same_day_n:        その日その銘柄で何回目のエントリーか (1始まり)
    - same_day_loss_prior: その日その銘柄で既に損切が出ているか
    - daily_pnl_prior:   全銘柄合算の当日累積 PnL (このエントリー直前まで)

    同時刻ペア (同じ銘柄を同じ時刻に複数戦略で拾うケース) は
    same_day_n を 1 回として扱う.
    """
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
            # 同時刻同銘柄は 1 回と数える
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
    """時間帯による Q 補正. 寄付直後/大引け前は減点."""
    try:
        m = dt.hour * 60 + dt.minute
    except Exception:
        return 0
    if m < 9 * 60 + 35 or m >= 14 * 60 + 30:
        return -5
    return 5


def _q_same_day_count_adj(n: int) -> int:
    """同日同銘柄エントリー回数による Q 補正."""
    if n <= 1:
        return 10
    if n == 2:
        return 0
    if n == 3:
        return -10
    return -15


def compute_q_score(t, pair_score: float) -> dict:
    """1取引の Q スコア (look-ahead-free シグナル品質).

    Q = pair_score + 時間帯 + 同日同銘柄回数 + 同銘柄当日損切歴
        + 当日累積PnL

    朝の Q スコア (BT + 同日同銘柄/損切歴/累積PnL/時間帯) を踏襲.
    """
    time_adj = _q_time_adj(t["entry_dt"])
    n = t.get("same_day_n", 1)
    count_adj = _q_same_day_count_adj(n)
    loss_adj = -10 if t.get("same_day_loss_prior") else 0
    pnl_prior = t.get("daily_pnl_prior", 0)
    if pnl_prior >= 10_000:
        pnl_adj = 5
    elif pnl_prior <= -10_000:
        pnl_adj = -5
    else:
        pnl_adj = 0
    q = max(0.0, min(100.0,
                       pair_score + time_adj + count_adj + loss_adj + pnl_adj))
    sp = _stop_pct(t["entry_p"], t["stop_p"])
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
        "stop_pct": sp,
        "prev_losses": t.get("prev_losses", 0),
    }


def trade_tier(pair_score: float, q_score: float) -> str:
    """朝レポート相当の S/A/B/C ティア分類. 閾値は本システムのスケールに合わせ調整.
    pair_score が 0-40 範囲なので Q 閾値は朝レポートより 20 低く設定."""
    if pair_score >= 35 and q_score >= 30:
        return "S"
    if pair_score >= 25 and q_score >= 25:
        return "A"
    if pair_score >= 15 and q_score >= 20:
        return "B"
    return "C"


def _tier_color(tier: str) -> str:
    return {"S": "#10b981", "A": "#4ade80",
             "B": "#facc15", "C": "#64748b"}.get(tier, "#94a3b8")


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


def render_section(trades, label, pair_scores=None, tab_uid="",
                   nikkei_data=None):
    """1セクション分: サマリ + 日付別損益 + 全取引.

    pair_scores:  {(sym, strat): score_info_dict} - 各ペアのスコア情報
    tab_uid:      スコアフィルタボタンの JS で使う一意 ID prefix
    nikkei_data:  nikkei_filter_daytrade.load_history() の戻り値
    """
    overall = calc_stats(trades)
    pf = overall["pf"]
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
    pf_color = "#4ade80" if pf >= 1.5 else ("#facc15" if pf >= 1.0
                                              else "#f87171")
    daily = aggregate_daily(trades)
    daily_sorted = list(reversed(daily))
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)

    def _pair_score(t):
        if pair_scores is None:
            return 0.0
        sc = pair_scores.get((t["symbol"], t["strategy"]), {})
        return sc.get("score", 0.0)

    def _q(t):
        return compute_q_score(t, _pair_score(t))

    # Q スコア降順 → 日時降順
    if pair_scores is not None:
        trades_sorted = sorted(
            trades,
            key=lambda x: (-_q(x)["q_score"], x["entry_dt"]),
        )
    else:
        trades_sorted = sorted(trades, key=lambda x: x["entry_dt"],
                                reverse=True)

    H = []
    # サマリーカード
    H.append("<div class='summary'>")
    H.append(f"<div class='card'><div class='lbl'>総取引</div>"
             f"<div class='val'>{overall['n']:,}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>勝率</div>"
             f"<div class='val'>{overall['win_rate']:.0f}%</div></div>")
    H.append(f"<div class='card'><div class='lbl'>PF</div>"
             f"<div class='val' style='color:{pf_color}'>{pf_str}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>総利益</div>"
             f"<div class='val pos'>+{gross_profit:,.0f}円</div></div>")
    H.append(f"<div class='card'><div class='lbl'>総損失</div>"
             f"<div class='val neg'>-{gross_loss:,.0f}円</div></div>")
    pnl_cls = "pos" if overall["total_pnl"] >= 0 else "neg"
    H.append(f"<div class='card'><div class='lbl'>差引損益</div>"
             f"<div class='val {pnl_cls}'>{overall['total_pnl']:+,.0f}円</div></div>")
    H.append(f"<div class='card'><div class='lbl'>平均利益</div>"
             f"<div class='val pos'>{overall['avg_win']:+,.0f}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>平均損失</div>"
             f"<div class='val neg'>{overall['avg_loss']:+,.0f}</div></div>")
    H.append(f"<div class='card'><div class='lbl'>最大DD</div>"
             f"<div class='val neg'>{overall['max_dd']:.1f}%</div></div>")
    H.append("</div>")

    # Q スコア閾値別フィルタサマリ
    if pair_scores is not None and trades:
        for threshold, label_cls in [(45, "推奨 (Q>=45)"),
                                       (35, "標準 (Q>=35)"),
                                       (25, "緩 (Q>=25)")]:
            hq = [t for t in trades if _q(t)["q_score"] >= threshold]
            if not hq:
                continue
            hq_st = calc_stats(hq)
            hq_pf = hq_st["pf"]
            hq_pf_s = ("∞" if hq_pf == float("inf") else f"{hq_pf:.2f}")
            hq_profit = sum(t["pnl"] for t in hq if t["pnl"] > 0)
            hq_loss = -sum(t["pnl"] for t in hq if t["pnl"] < 0)
            hq_pnl_cls = "pos" if hq_st["total_pnl"] >= 0 else "neg"
            hq_pf_color = ("#4ade80" if hq_pf >= 1.5
                            else ("#facc15" if hq_pf >= 1.0 else "#f87171"))
            H.append(f"<h3 style='color:#10b981;'>{label_cls}</h3>")
            H.append("<div class='summary'>")
            H.append(f"<div class='card'><div class='lbl'>取引</div>"
                     f"<div class='val'>{hq_st['n']:,}"
                     f"<span class='small'> / {overall['n']:,}</span>"
                     f"</div></div>")
            H.append(f"<div class='card'><div class='lbl'>勝率</div>"
                     f"<div class='val'>{hq_st['win_rate']:.0f}%</div></div>")
            H.append(f"<div class='card'><div class='lbl'>PF</div>"
                     f"<div class='val' style='color:{hq_pf_color}'>"
                     f"{hq_pf_s}</div></div>")
            H.append(f"<div class='card'><div class='lbl'>総利益</div>"
                     f"<div class='val pos'>+{hq_profit:,.0f}円</div></div>")
            H.append(f"<div class='card'><div class='lbl'>総損失</div>"
                     f"<div class='val neg'>-{hq_loss:,.0f}円</div></div>")
            H.append(f"<div class='card'><div class='lbl'>差引損益</div>"
                     f"<div class='val {hq_pnl_cls}'>"
                     f"{hq_st['total_pnl']:+,.0f}円</div></div>")
            H.append(f"<div class='card'><div class='lbl'>最大DD</div>"
                     f"<div class='val neg'>"
                     f"{hq_st['max_dd']:.1f}%</div></div>")
            H.append("</div>")

    # 地合いフィルタ (前日日経トレンドで方向フィルタ)
    if nikkei_data and trades:
        side0 = trades[0].get("side", "long")
        mf = []
        for _t in trades:
            _dt = _t.get("entry_dt")
            if not hasattr(_dt, "date"):
                mf.append(_t)
                continue
            _info = nikkei_data.get(_dt.date())
            if _info is None:
                mf.append(_t)
                continue
            _pc = _info.get("prev_close_change_pct", 0.0)
            if side0 == "short" and _pc >= MARKET_FILTER_THR:
                continue  # 上昇地合い日のショートを除外
            if side0 == "long" and _pc <= -MARKET_FILTER_THR:
                continue  # 下落地合い日のロングを除外
            mf.append(_t)
        if mf:
            mf_st = calc_stats(mf)
            mf_pf = mf_st["pf"]
            mf_pf_s = "∞" if mf_pf == float("inf") else f"{mf_pf:.2f}"
            mf_profit = sum(_t["pnl"] for _t in mf if _t["pnl"] > 0)
            mf_loss = -sum(_t["pnl"] for _t in mf if _t["pnl"] < 0)
            mf_pnl_cls = "pos" if mf_st["total_pnl"] >= 0 else "neg"
            mf_pf_color = ("#4ade80" if mf_pf >= 1.5
                            else ("#facc15" if mf_pf >= 1.0 else "#f87171"))
            excluded = len(trades) - len(mf)
            direction = "上昇地合い除外" if side0 == "short" else "下落地合い除外"
            H.append(
                f"<h3 style='color:#60a5fa;'>地合いフィルタあり "
                f"({direction}: 前日日経±{MARKET_FILTER_THR}%超)</h3>"
            )
            H.append("<div class='summary'>")
            H.append(f"<div class='card'><div class='lbl'>取引</div>"
                     f"<div class='val'>{mf_st['n']:,}"
                     f"<span class='small'> / {overall['n']:,}</span>"
                     f"</div></div>")
            H.append(f"<div class='card'><div class='lbl'>除外</div>"
                     f"<div class='val neg'>{excluded:,}件</div></div>")
            H.append(f"<div class='card'><div class='lbl'>勝率</div>"
                     f"<div class='val'>{mf_st['win_rate']:.0f}%</div></div>")
            H.append(f"<div class='card'><div class='lbl'>PF</div>"
                     f"<div class='val' style='color:{mf_pf_color}'>"
                     f"{mf_pf_s}</div></div>")
            H.append(f"<div class='card'><div class='lbl'>総利益</div>"
                     f"<div class='val pos'>+{mf_profit:,.0f}円</div></div>")
            H.append(f"<div class='card'><div class='lbl'>総損失</div>"
                     f"<div class='val neg'>-{mf_loss:,.0f}円</div></div>")
            H.append(f"<div class='card'><div class='lbl'>差引損益</div>"
                     f"<div class='val {mf_pnl_cls}'>"
                     f"{mf_st['total_pnl']:+,.0f}円</div></div>")
            H.append(f"<div class='card'><div class='lbl'>最大DD</div>"
                     f"<div class='val neg'>{mf_st['max_dd']:.1f}%</div></div>")
            H.append("</div>")

    if not trades:
        H.append("<p style='color:#94a3b8'>取引なし</p>")
        return "\n".join(H)

    # 日付 → 取引リスト (詳細展開用)
    trades_by_date: dict = defaultdict(list)
    for t in trades_sorted:
        trades_by_date[t["entry_dt"].date()].append(t)

    nk_col = bool(nikkei_data)
    ncols = 8 if nk_col else 7
    nk_header = "<th class='right small'>日経前日比</th>" if nk_col else ""

    # 日付別損益 (クリックで詳細展開)
    H.append("<h3>日付別損益 (行クリックで取引詳細)</h3>")
    H.append("<table><thead><tr>")
    H.append(f"<th>日付</th>{nk_header}<th class='right'>取引</th>"
             "<th class='right'>勝率</th>"
             "<th class='right pos'>利益</th>"
             "<th class='right neg'>損失</th>"
             "<th class='right'>差引</th>"
             "<th class='right'>累積</th></tr></thead><tbody>")
    for d in daily_sorted:
        pnl_c = "pos" if d["pnl"] >= 0 else "neg"
        cum_c = "pos" if d["cumulative"] >= 0 else "neg"
        date_key = str(d["date"]).replace("-", "")
        detail_id = f"{tab_uid}-day-{date_key}" if tab_uid else f"day-{date_key}"
        if nk_col:
            _nki = nikkei_data.get(d["date"])
            _nkp = _nki.get("prev_close_change_pct", 0.0) if _nki else None
            if _nkp is not None:
                _nkc = "pos" if _nkp >= 0 else "neg"
                nk_td = f"<td class='right small {_nkc}'>{_nkp:+.1f}%</td>"
            else:
                nk_td = "<td class='right small'>-</td>"
        else:
            nk_td = ""
        H.append(
            f"<tr onclick=\"toggleDay('{detail_id}',this)\" style='cursor:pointer;'>"
            f"<td><span data-tri style='font-size:9px;color:#64748b;"
            f"display:inline-block;margin-right:4px;'>+</span>{d['date']}</td>"
            f"{nk_td}"
            f"<td class='right'>{d['n']}</td>"
            f"<td class='right'>{d['win_rate']:.0f}%</td>"
            f"<td class='right pos'>+{d['profit']:,.0f}</td>"
            f"<td class='right neg'>-{d['loss']:,.0f}</td>"
            f"<td class='right {pnl_c}'>{d['pnl']:+,.0f}</td>"
            f"<td class='right {cum_c}'>{d['cumulative']:+,.0f}</td>"
            f"</tr>"
        )
        # 詳細サブ行
        day_trades = trades_by_date.get(d["date"], [])
        H.append(
            f"<tr id='{detail_id}' style='display:none;'>"
            f"<td colspan='{ncols}' style='padding:0 0 0 24px;background:#0f172a;'>"
        )
        H.append("<table style='margin:4px 0 4px;width:calc(100% - 24px);'>"
                 "<thead><tr>"
                 "<th>銘柄</th><th>戦略</th>"
                 "<th class='right'>Q</th>"
                 "<th class='center'>T</th>"
                 "<th class='center'>entry</th>"
                 "<th class='right'>保有</th>"
                 "<th class='right'>損益</th>"
                 "<th class='right'>%</th>"
                 "<th>理由</th>"
                 "</tr></thead><tbody>")
        for t in day_trades:
            pnl_c2 = "pos" if t["pnl"] >= 0 else "neg"
            if pair_scores is not None:
                q2 = _q(t)
                qs2 = q2["q_score"]
                ps2 = q2["pair_score"]
                tier2 = trade_tier(ps2, qs2)
            else:
                qs2 = 0.0
                tier2 = "C"
            tier_c2 = _tier_color(tier2)
            qs_c2 = _q_color(qs2)
            H.append(
                f"<tr>"
                f"<td>{html.escape(t['name'])}</td>"
                f"<td>{html.escape(t['strategy'])}</td>"
                f"<td class='right' style='color:{qs_c2};font-weight:bold;'>"
                f"{qs2:.0f}</td>"
                f"<td class='center' style='color:{tier_c2};font-weight:bold;'>"
                f"{tier2}</td>"
                f"<td class='center small'>{_fmt_dt(t['entry_dt'])}</td>"
                f"<td class='right'>{_hold_min(t)}分</td>"
                f"<td class='right {pnl_c2}'>{t['pnl']:+,.0f}</td>"
                f"<td class='right {pnl_c2}'>{t['pct']:+.2f}%</td>"
                f"<td>{html.escape(t['reason'])}</td>"
                f"</tr>"
            )
        H.append("</tbody></table></td></tr>")
    H.append("</tbody></table>")

    # Q スコアフィルタ
    if pair_scores is not None and tab_uid:
        H.append(f"<h3>📋 全取引 (<span id='{tab_uid}-cnt'>"
                  f"{len(trades_sorted):,}</span>件)</h3>")
        H.append(f"<div class='score-filters'>"
                  f"<span style='color:#94a3b8;font-size:11px;margin-right:8px;'>"
                  f"Q フィルタ:</span>"
                  f"<button class='score-btn active' "
                  f"onclick=\"filterScore('{tab_uid}', 0, this)\">全件</button>"
                  f"<button class='score-btn' "
                  f"onclick=\"filterScore('{tab_uid}', 25, this)\">&gt;=25</button>"
                  f"<button class='score-btn' "
                  f"onclick=\"filterScore('{tab_uid}', 35, this)\">&gt;=35</button>"
                  f"<button class='score-btn' "
                  f"onclick=\"filterScore('{tab_uid}', 45, this)\">&gt;=45</button>"
                  f"<button class='score-btn' "
                  f"onclick=\"filterScore('{tab_uid}', 55, this)\">&gt;=55</button>"
                  f"</div>")
        H.append(f"<table id='{tab_uid}-tbl'><thead><tr>")
    else:
        H.append(f"<h3>📋 全取引 ({len(trades_sorted):,}件)</h3>")
        H.append("<table><thead><tr>")

    H.append("<th>銘柄</th><th>戦略</th>"
              "<th class='right'>銘柄</th>"
              "<th class='right'>Q</th>"
              "<th class='center'>ティア</th>"
              "<th class='center'>時刻</th>"
              "<th class='center'>同日</th>"
              "<th class='center'>損切歴</th>"
              "<th class='center'>累積</th>"
              "<th class='center'>Side</th>"
              "<th>Entry</th><th>Exit</th><th class='right'>保有</th>"
              "<th class='right'>買値</th><th class='right'>損切</th>"
              "<th class='right'>目標</th><th class='right'>決済</th>"
              "<th class='right'>損益</th><th class='right'>%</th>"
              "<th>理由</th></tr></thead><tbody>")
    for t in trades_sorted:
        pnl_c = "pos" if t["pnl"] >= 0 else "neg"
        held = _hold_min(t)
        side_s = "L" if t["side"] == "long" else "S"
        if pair_scores is not None:
            q = _q(t)
        else:
            q = {"q_score": 0.0, "pair_score": 0.0,
                  "time_adj": 0, "count_adj": 0, "loss_adj": 0, "pnl_adj": 0,
                  "same_day_n": 1, "same_day_loss_prior": False,
                  "daily_pnl_prior": 0, "stop_pct": 0.0, "prev_losses": 0}
        ps = q["pair_score"]
        qs = q["q_score"]
        tier = trade_tier(ps, qs)
        qs_color = _q_color(qs)
        ps_color = _score_color(ps)
        tier_color = _tier_color(tier)
        loss_mark = "✗" if q["same_day_loss_prior"] else "—"
        pnl_prior = q["daily_pnl_prior"]
        pnl_prior_lbl = f"{pnl_prior/1000:+.0f}K"
        tip = (f"Q = pair({ps:.0f}) "
                f"+ 時刻({q['time_adj']:+d}) "
                f"+ 同日{q['same_day_n']}回目({q['count_adj']:+d}) "
                f"+ 当日損切歴({q['loss_adj']:+d}) "
                f"+ 累積{pnl_prior_lbl}({q['pnl_adj']:+d}) "
                f"= {qs:.0f} / 損切幅 {q['stop_pct']:.2f}%")
        H.append(
            f"<tr class='trade-row' data-score='{qs:.2f}'>"
            f"<td>{html.escape(t['name'])}</td>"
            f"<td>{html.escape(t['strategy'])}</td>"
            f"<td class='right' "
            f"style='color:{ps_color};'>{ps:.0f}</td>"
            f"<td class='right' title='{html.escape(tip)}' "
            f"style='color:{qs_color};font-weight:bold;'>{qs:.0f}</td>"
            f"<td class='center' "
            f"style='color:{tier_color};font-weight:bold;'>{tier}</td>"
            f"<td class='center small'>{q['time_adj']:+d}</td>"
            f"<td class='center small'>{q['same_day_n']}回</td>"
            f"<td class='center small'>{loss_mark}</td>"
            f"<td class='center small'>{pnl_prior_lbl}</td>"
            f"<td class='center'>{side_s}</td>"
            f"<td>{_fmt_dt(t['entry_dt'])}</td>"
            f"<td>{_fmt_dt(t['exit_dt'])}</td>"
            f"<td class='right'>{held}分</td>"
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


def render_html(*, shift_trades_by_side: dict, date_label: str,
                source: str, watchlist_summary: dict,
                pair_scores: dict | None = None,
                sym_name_map: dict | None = None,
                nikkei_data: dict | None = None) -> str:
    """完全な HTML 生成 (Long/Short タブ + shift サブタブ)."""
    H = []
    H.append("<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>")
    H.append(f"<title>6シフト統合レポート ({date_label})</title>")
    H.append("""
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a;color:#e2e8f0;padding:24px;margin:0;}
  h1{font-size:20px;margin:0 0 4px;color:#10b981;}
  h2{font-size:16px;margin:18px 0 8px;color:#cbd5e1;
     border-left:3px solid #10b981;padding-left:10px;}
  h3{font-size:14px;margin:12px 0 6px;color:#a3a3a3;}
  .meta{color:#94a3b8;font-size:13px;margin-bottom:16px;}
  .summary{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
  .card{background:#1e293b;padding:10px 14px;border-radius:6px;
        min-width:90px;}
  .card .lbl{color:#94a3b8;font-size:11px;}
  .card .val{font-size:16px;font-weight:bold;margin-top:3px;}
  table{width:100%;border-collapse:collapse;background:#1e293b;
        font-size:12px;margin-bottom:14px;}
  th{background:#334155;color:#cbd5e1;padding:6px 8px;text-align:left;
     font-weight:600;}
  td{padding:4px 8px;border-top:1px solid #334155;}
  tr:hover td{background:#293548;}
  .pos{color:#4ade80;} .neg{color:#f87171;}
  .small{font-size:10px;color:#94a3b8;}
  .right{text-align:right;}
  .center{text-align:center;}

  /* タブ */
  .tabs{display:flex;gap:4px;margin:14px 0 12px;border-bottom:1px solid #334155;
        flex-wrap:wrap;}
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
  .note{color:#94a3b8;font-size:11px;margin:4px 0 8px;}

  /* スコアフィルタ */
  .score-filters{margin:6px 0 10px;display:flex;gap:4px;align-items:center;
                  flex-wrap:wrap;}
  .score-btn{padding:4px 10px;background:#334155;color:#cbd5e1;border:none;
              cursor:pointer;font-size:11px;border-radius:4px;}
  .score-btn.active{background:#10b981;color:#fff;}

  /* ペア別スコア折りたたみ */
  details summary::-webkit-details-marker{display:none;}
  details[open] summary span:first-child{transform:rotate(90deg);}
  details summary span:first-child{display:inline-block;transition:transform 0.15s;}
</style>
<script>
function showSide(side){
  document.querySelectorAll('.side-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.side-content').forEach(c=>c.classList.remove('active'));
  document.getElementById('side-tab-'+side).classList.add('active');
  document.getElementById('side-content-'+side).classList.add('active');
}
function showShift(side, shift){
  document.querySelectorAll('.shift-tab-'+side).forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.shift-content-'+side).forEach(c=>c.classList.remove('active'));
  document.getElementById('shift-tab-'+side+'-'+shift).classList.add('active');
  document.getElementById('shift-content-'+side+'-'+shift).classList.add('active');
}
function filterScore(tabUid, minScore, btn){
  const tbl = document.getElementById(tabUid + '-tbl');
  if(!tbl) return;
  const rows = tbl.querySelectorAll('tbody tr.trade-row');
  let visible = 0;
  rows.forEach(r => {
    const s = parseFloat(r.dataset.score || '0');
    if(s >= minScore){ r.style.display = ''; visible++; }
    else{ r.style.display = 'none'; }
  });
  const cnt = document.getElementById(tabUid + '-cnt');
  if(cnt) cnt.textContent = visible.toLocaleString();
  if(btn){
    const parent = btn.parentElement;
    parent.querySelectorAll('.score-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }
}
function toggleDay(id, row){
  const detail = document.getElementById(id);
  if(!detail) return;
  const open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : '';
  const tri = row && row.querySelector('[data-tri]');
  if(tri) tri.textContent = open ? '+' : '-';
}
</script>""")
    H.append("</head><body>")
    H.append(f"<h1>6 シフト統合レポート — OOS 評価</h1>")
    H.append(f"<div class='meta'>")
    H.append(f"  watchlist 日付: {date_label} / source: {source} / "
             f"各 shift の OOS 期間 (直近 N日) の取引のみ表示 / "
             f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    H.append("</div>")

    # watchlist サマリ
    H.append("<h2>📊 各 shift の watchlist 構成</h2>")
    H.append("<table><thead><tr>")
    H.append("<th>shift</th><th class='right'>銘柄数</th>"
             "<th class='right'>(銘柄×戦略)ペア</th>"
             "<th class='right'>Long ペア</th>"
             "<th class='right'>Short ペア</th>"
             "</tr></thead><tbody>")
    for s in SHIFTS:
        info = watchlist_summary.get(s, {})
        H.append(f"<tr>"
                 f"<td>shift={s}日</td>"
                 f"<td class='right'>{info.get('syms', 0)}</td>"
                 f"<td class='right'>{info.get('pairs', 0)}</td>"
                 f"<td class='right'>{info.get('long_pairs', 0)}</td>"
                 f"<td class='right'>{info.get('short_pairs', 0)}</td>"
                 f"</tr>")
    H.append("</tbody></table>")

    # 上位タブ: Long / Short
    H.append("<h2>取引結果 (OOS)</h2>")
    H.append("<div class='tabs'>")
    H.append("<button class='tab side-tab active' id='side-tab-long' "
             "onclick=\"showSide('long')\">📈 ロング</button>")
    H.append("<button class='tab side-tab' id='side-tab-short' "
             "onclick=\"showSide('short')\">📉 ショート</button>")
    H.append("</div>")

    # サブタブの順番: 全体 → 直近30 → 60 → 90 → 120 → 150 → 180
    sub_tabs = [(SHIFT_ALL, "全体")] + [(s, f"直近{s}日") for s in SHIFTS]

    for side in ("long", "short"):
        active_cls = "active" if side == "long" else ""
        side_label = "ロング" if side == "long" else "ショート"
        H.append(f"<div class='tab-content side-content {active_cls}' "
                 f"id='side-content-{side}'>")

        # ペア別スコアランキング
        if pair_scores is not None:
            H.append(render_pair_table(pair_scores, side,
                                          sym_name_map=sym_name_map))

        # サブタブ
        H.append("<div class='sub-tabs'>")
        for i, (s, lbl) in enumerate(sub_tabs):
            ac = "active" if i == 0 else ""
            H.append(f"<button class='sub-tab shift-tab-{side} {ac}' "
                     f"id='shift-tab-{side}-{s}' "
                     f"onclick=\"showShift('{side}', '{s}')\">"
                     f"{lbl}</button>")
        H.append("</div>")

        # 各サブタブの内容
        for i, (s, lbl) in enumerate(sub_tabs):
            ac = "active" if i == 0 else ""
            H.append(f"<div class='sub-content shift-content-{side} {ac}' "
                     f"id='shift-content-{side}-{s}'>")
            trades = shift_trades_by_side.get((s, side), [])
            if s == SHIFT_ALL:
                desc = (f"全 6 shift 銘柄 union / {side_label} / 全期間 / "
                        f"同時刻同銘柄の重複除外済み")
            else:
                desc = (f"全 6 shift 銘柄 union / {side_label} / 直近{s}日 / "
                        f"同時刻同銘柄の重複除外済み")
            H.append(f"<p style='color:#94a3b8;font-size:12px;'>{desc}</p>")
            tab_uid = f"sec-{side}-{s}"
            H.append(
                "<p class='note'>"
                "Q スコア = 銘柄スコア + 時間帯 (寄付/引け前=-5, 通常=+5) "
                "+ 同日同銘柄回数 (初=+10, 2回目=0, 3回目=-10, 4回目以上=-15) "
                "+ 同銘柄当日損切歴 (1回以上=-10) "
                "+ 当日累積PnL (+10K以上=+5, -10K以下=-5). "
                "S級=銘柄>=35 & Q>=30, A級=銘柄>=25 & Q>=25, "
                "B級=銘柄>=15 & Q>=20, それ以外=C級."
                "</p>"
            )
            H.append(render_section(trades, lbl,
                                      pair_scores=pair_scores,
                                      tab_uid=tab_uid,
                                      nikkei_data=nikkei_data))
            H.append("</div>")

        H.append("</div>")

    H.append("</body></html>")
    return "\n".join(H)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None,
                   help="watchlist 日付 (YYYY-MM-DD)。省略時自動検出")
    p.add_argument("--source", default="hybrid",
                   choices=["auto", "local", "yfinance", "hybrid"],
                   help="データソース (デフォルト hybrid)")
    p.add_argument("--days", type=int, default=540)
    p.add_argument("--snapshot-date", default=None,
                   help="使用するスナップショット日 (YYYY-MM-DD)。"
                        "省略時は最新の既存スナップショットを自動採用 "
                        "(翌日実行しても結果が変わらない)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--output", default=None)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--refresh-cache", action="store_true",
                   help="バックテストキャッシュを破棄して全て再計算")
    p.add_argument("--min-robustness", type=int, default=None,
                   metavar="N",
                   help="OOS フィルタ: 何 shift 以上で選ばれたか (1-6, デフォルト 3)")
    p.add_argument("--min-oos-pf", type=float, default=None,
                   metavar="PF",
                   help="OOS フィルタ: 平均 OOS PF の下限 (デフォルト 1.3)")
    p.add_argument("--min-oos-pnl", type=float, default=None,
                   metavar="PNL",
                   help="OOS フィルタ: OOS 損益の下限円 (デフォルト 0)")
    args = p.parse_args()

    global PAIR_FILTER_MIN_ROBUSTNESS, PAIR_FILTER_MIN_PF, PAIR_FILTER_MIN_PNL
    if args.min_robustness is not None:
        PAIR_FILTER_MIN_ROBUSTNESS = args.min_robustness / 6
    if args.min_oos_pf is not None:
        PAIR_FILTER_MIN_PF = args.min_oos_pf
    if args.min_oos_pnl is not None:
        PAIR_FILTER_MIN_PNL = args.min_oos_pnl

    # snapshot_date: 省略時は最新の既存スナップショットを採用
    if args.snapshot_date is None:
        latest_snap = detect_latest_snapshot_date()
        if latest_snap:
            args.snapshot_date = latest_snap
            print(f"  snapshot_date 自動検出: {latest_snap} "
                  f"(最新の既存スナップショット)", flush=True)
        else:
            args.snapshot_date = datetime.now(JST).date().isoformat()
            print(f"  snapshot_date デフォルト: {args.snapshot_date} "
                  f"(既存スナップショットなし、新規作成)", flush=True)

    date_label = args.date or detect_latest_date()
    if not date_label:
        print("[error] watchlist 日付を検出できません。--date 指定してください")
        return

    # 6 watchlist 読込
    watchlist_summary = {}
    all_specs_by_shift = {}  # shift -> list of (sym, name, strat)
    all_syms = set()
    for s in SHIFTS:
        wl_path = Path(f"daytrade_winners_{date_label}_shift{s}d.py")
        if not wl_path.exists():
            print(f"  [warn] {wl_path.name} 不在 → shift={s} スキップ")
            watchlist_summary[s] = {"syms": 0, "pairs": 0,
                                      "long_pairs": 0, "short_pairs": 0}
            continue
        pairs = load_watchlist(wl_path)
        syms = set(p[0] for p in pairs)
        long_pairs = sum(1 for _, _, strat in pairs if strat in LONG_STRATS)
        short_pairs = sum(1 for _, _, strat in pairs if strat in SHORT_STRATS)
        watchlist_summary[s] = {
            "syms": len(syms), "pairs": len(pairs),
            "long_pairs": long_pairs, "short_pairs": short_pairs,
        }
        all_specs_by_shift[s] = pairs
        all_syms.update(syms)

    if not all_syms:
        print("[error] watchlist 銘柄なし")
        return

    print(f"検出日付: {date_label}")
    print(f"ユニーク銘柄 (全 shift 合計): {len(all_syms)}", flush=True)

    # 候補ペア (data load 前)
    candidate_pairs = set()
    for s, pairs in all_specs_by_shift.items():
        for sym, name, strat in pairs:
            candidate_pairs.add((sym, name, strat))

    # バックテストキャッシュチェック
    cache_full = {} if args.refresh_cache else load_backtest_cache()
    cache_key_prefix = f"{args.snapshot_date}::{args.source}"

    print(f"\n[Step 1] キャッシュ確認 ({len(candidate_pairs)}候補ペア)",
          flush=True)
    if args.refresh_cache:
        print(f"  [--refresh-cache] キャッシュ破棄", flush=True)
    print(f"  既存キャッシュ: {len(cache_full)}件 "
          f"(key prefix: {cache_key_prefix})", flush=True)

    pair_trades = {}
    pairs_needing_backtest = []
    for sym, name, strat in candidate_pairs:
        cache_key = f"{cache_key_prefix}::{sym}::{strat}"
        if cache_key in cache_full:
            pair_trades[(sym, strat)] = cache_full[cache_key]
        else:
            pairs_needing_backtest.append((sym, name, strat))

    n_hits = len(pair_trades)
    n_miss = len(pairs_needing_backtest)
    print(f"  ヒット: {n_hits}/{len(candidate_pairs)} → "
          f"再計算必要: {n_miss}ペア", flush=True)

    # データロード (キャッシュミスがある場合のみ)
    if pairs_needing_backtest:
        syms_to_load = sorted(set(p[0] for p in pairs_needing_backtest))
        print(f"\n[Step 2] データロード ({len(syms_to_load)}銘柄, "
              f"source={args.source})", flush=True)
        fetched = load_intraday_batch(
            syms_to_load, args.days, source=args.source,
            snapshot_date=args.snapshot_date,
        )
        print(f"  ロード: {len(fetched)}/{len(syms_to_load)}銘柄",
              flush=True)

        # バックテスト
        print(f"\n[Step 3] バックテスト ({len(pairs_needing_backtest)}ペア)",
              flush=True)
        specs = [(sym, name, strat, fetched[sym])
                 for sym, name, strat in pairs_needing_backtest
                 if sym in fetched]
        if len(specs) < len(pairs_needing_backtest):
            missing = len(pairs_needing_backtest) - len(specs)
            print(f"  [warn] {missing}ペアはデータなしでスキップ",
                  flush=True)

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
                cache_key = f"{cache_key_prefix}::{sym}::{strat_key}"
                cache_full[cache_key] = trades
                if done % 20 == 0 or done == len(specs):
                    print(f"  {done}/{len(specs)} 完了", flush=True)

        save_backtest_cache(cache_full)
        print(f"  キャッシュ保存: {len(cache_full)}件 → {CACHE_FILE}",
              flush=True)
    else:
        print(f"\n[Step 2] バックテスト全スキップ "
              f"(全ペアがキャッシュ済み)", flush=True)

    # 各 (sym, strat) の取引に「直前連敗数」を注釈
    annotate_streaks(pair_trades)

    # SAME_DAY_LOCK: 同日同銘柄で複数戦略が発火した場合、最初の entry_dt の戦略のみ残す
    # ロング側/ショート側で独立に適用
    long_pt = {k: v for k, v in pair_trades.items() if k[1] in LONG_STRATS}
    short_pt = {k: v for k, v in pair_trades.items() if k[1] in SHORT_STRATS}
    before_lock = sum(len(v) for v in pair_trades.values())
    long_pt = apply_same_day_lock(long_pt)
    short_pt = apply_same_day_lock(short_pt)
    pair_trades = {**long_pt, **short_pt}
    after_lock = sum(len(v) for v in pair_trades.values())
    print(f"  [SAME_DAY_LOCK] {before_lock:,} → {after_lock:,} "
          f"(-{before_lock - after_lock:,}件 重複排除)", flush=True)

    today = datetime.now(JST).date()

    # 日経225 前日比データ (地合いフィルタ用)
    try:
        from nikkei_filter_daytrade import load_history as _load_nk
        nikkei_data = _load_nk(today=today)
        print(f"  日経データ: {len(nikkei_data)}日分読込", flush=True)
    except Exception as _e:
        print(f"  [warn] 日経データ取得失敗 ({_e}) → 地合いフィルタ無効",
              flush=True)
        nikkei_data = {}

    shift_trades_by_side = defaultdict(list)

    # 全 6 shift の union ペア
    union_pairs = set()
    for s in SHIFTS:
        for sym, name, strat in all_specs_by_shift.get(s, []):
            union_pairs.add((sym, strat))
    n_union_pairs = len(union_pairs)
    print(f"  union ペア: {n_union_pairs} "
          f"(long={sum(1 for _,st in union_pairs if st in LONG_STRATS)}, "
          f"short={sum(1 for _,st in union_pairs if st in SHORT_STRATS)})",
          flush=True)

    # ペアスコア計算 (各 shift が除外していた直近 N日 = OOS の成績)
    print(f"\n[Step 4] ペアスコア計算 ({len(union_pairs)}ペア)", flush=True)
    pair_selection = build_pair_selection(all_specs_by_shift)
    # 銘柄コード → 銘柄名のマップ
    sym_name_map = {}
    for s, pairs in all_specs_by_shift.items():
        for sym, name, _strat in pairs:
            if name:
                sym_name_map[sym] = name
    pair_scores = {}
    for sym, strat in union_pairs:
        trades_full = pair_trades.get((sym, strat), [])
        selecting = pair_selection.get((sym, strat), set())
        pair_scores[(sym, strat)] = compute_pair_score(
            trades_full, selecting, today)

    # ユニバース厳選 (案A): pass フィルタを通ったペアのみ採用
    filtered_pairs = {p for p, sc in pair_scores.items()
                       if pair_passes_filter(sc)}
    print(f"\n[Step 5] ユニバース厳選 "
          f"(robustness≥{PAIR_FILTER_MIN_ROBUSTNESS*6:.0f}/6 & "
          f"PF≥{PAIR_FILTER_MIN_PF:.1f} & PnL>0)",
          flush=True)
    print(f"  採用: {len(filtered_pairs)}/{len(union_pairs)} ペア",
          flush=True)
    for p in sorted(filtered_pairs, key=lambda x: -pair_scores[x]["score"]):
        sc = pair_scores[p]
        name = sym_name_map.get(p[0], p[0])
        shifts_lbl = "/".join(str(s) for s in sc["selecting_shifts"])
        print(f"    ✓ {name[:14]:<14}/{p[1]:<8} "
              f"score={sc['score']:5.1f} shifts=[{shifts_lbl}] "
              f"PF={sc['avg_oos_pf']:.2f} "
              f"勝率={sc['avg_oos_winrate']*100:.0f}% "
              f"PnL={sc['oos_pnl']:+,.0f}", flush=True)

    # 採用ペアのみで pair_trades を絞り、Q スコアの look-ahead-free 状態を注釈
    filtered_pair_trades = {p: pair_trades.get(p, [])
                              for p in filtered_pairs}
    annotate_q_state(filtered_pair_trades)

    # 全体タブ: 採用ペア × 全期間
    for sym, strat in filtered_pairs:
        trades = pair_trades.get((sym, strat), [])
        side = "short" if strat in SHORT_STRATS else "long"
        shift_trades_by_side[(SHIFT_ALL, side)].extend(trades)

    # shift 別タブ: 採用ペア × 直近 N日 にフィルタ
    for s in SHIFTS:
        cutoff = today - timedelta(days=s)
        for sym, strat in filtered_pairs:
            trades = pair_trades.get((sym, strat), [])
            side = "short" if strat in SHORT_STRATS else "long"
            for t in trades:
                if not hasattr(t.get("entry_dt"), "date"):
                    continue
                if t["entry_dt"].date() < cutoff:
                    continue
                shift_trades_by_side[(s, side)].append(t)

    # 同じ時刻に同じ銘柄の重複除外を全タブに適用
    print(f"  同時刻同銘柄の重複除外を適用中...", flush=True)
    for key in list(shift_trades_by_side.keys()):
        before = len(shift_trades_by_side[key])
        shift_trades_by_side[key] = dedup_same_time_same_symbol(
            shift_trades_by_side[key])
        after = len(shift_trades_by_side[key])
        if before > after:
            print(f"    {key}: {before} → {after} "
                  f"(-{before - after} 重複)", flush=True)

    # HTML 出力
    out = args.output or f"simple_report_all_shifts_{date_label}.html"
    html_text = render_html(
        shift_trades_by_side=shift_trades_by_side,
        date_label=date_label,
        source=args.source,
        watchlist_summary=watchlist_summary,
        pair_scores=pair_scores,
        sym_name_map=sym_name_map,
        nikkei_data=nikkei_data,
    )
    Path(out).write_text(html_text, encoding="utf-8")
    print(f"\n[OK] 出力: {Path(out).resolve()}", flush=True)
    if not args.no_open:
        _open_in_edge(Path(out).resolve())


if __name__ == "__main__":
    main()
