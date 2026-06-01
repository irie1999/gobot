"""
compare_gap_entry.py  ─  指値上限 × 複数パターン比較バックテスト
=================================================================
逆指値注文の「指値上限」を複数段階で変えてバックテストし、
成行（上限なし）と一覧比較します。既存スクリプトは変更しません。

【仕組み】
  日本株の逆指値注文には2種類あります:
    逆指値→成行: トリガー価格に達したら「いくらでも買う」
    逆指値→指値: トリガー価格に達したら「X円以下なら買う」

  このスクリプトでは指値上限 (= トリガー価格 × (1+cap)) を
  1%〜成行まで変えてバックテストし、最適な上限を探します。

  例) イビデン(前日終値 23,000円) でシグナル発生:
    トリガー価格  = 23,000円
    指値上限 +3%  = 23,690円  → 始値が23,690円以下なら約定、超えたらスキップ
    指値上限 +5%  = 24,150円  → 始値が24,150円以下なら約定
    成行(上限なし) = 始値がいくらでも約定

  今日のイビデンは大きくギャップアップ → 指値+3%はスキップ → 成行なら約定

Usage:
  python compare_gap_entry.py --html
  python compare_gap_entry.py --caps 0.01 0.03 0.05 0.10 --html
  python compare_gap_entry.py --aggressive --html
  python compare_gap_entry.py --days 180 --html
  python compare_gap_entry.py --all-watchlists --html     # 全WATCHLIST統合(~150銘柄)
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from collections import defaultdict

# ── モード設定 ──────────────────────────────────────────────────────
def _setup_mode() -> str:
    mode = "aggressive" if "--aggressive" in sys.argv else \
           os.getenv("TRADING_MODE", "conservative")
    os.environ["TRADING_MODE"] = mode
    return mode

_MODE = _setup_mode()

import backtest_limit_entry as _bt
import check_signals_stop   as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import WORKERS, fetch, run_limit_backtest

# デフォルト比較パターン: 1% / 3%(現行) / 5% / 7% / 10% / 成行
DEFAULT_CAPS = [0.01, 0.03, 0.05, 0.07, 0.10, 9.99]


# ── CLI ─────────────────────────────────────────────────────────────
def _parse():
    p = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days",    type=int,   default=365)
    p.add_argument("--workers", type=int,   default=WORKERS)
    p.add_argument("--caps",    type=float, nargs="+", default=DEFAULT_CAPS,
                   metavar="CAP",
                   help="指値上限のリスト (例: 0.01 0.03 0.05 9.99)。9.99=成行")
    p.add_argument("--aggressive",      action="store_true")
    p.add_argument("--html",            action="store_true")
    p.add_argument("--no-browser",      action="store_true")
    p.add_argument("--all-watchlists",  action="store_true",
                   help="全WATCHLISTを統合して検証 (stop/brk × conservative/aggressive × WF)")
    p.add_argument("--by-signal",       action="store_true",
                   help="シグナル設定別（既存版/WF/NOLIMIT/統合）でCAP比較")
    return p.parse_args()


# ── 1銘柄バックテスト ────────────────────────────────────────────────
def _run_one(sym, name, strat, days, calc_fn, em, sm, tm):
    try:
        df_raw = fetch(sym, days + 60)
        if df_raw is None or len(df_raw) < 10:
            return None
        result = run_limit_backtest(
            sym, name, df_raw, calc_fn,
            em, sm, tm, days, strat,
            entry_type="stop",
        )
        return result
    except Exception as e:
        print(f"\n  [ERROR] {sym} {strat}: {e}")
        return None


def _run_all(tasks, cap: float, workers: int, label: str) -> list[dict]:
    """指定 cap でバックテスト実行。モジュール定数を一時差し替え。"""
    original = _bt.LIMIT_ENTRY_MARGIN_PCT
    _bt.LIMIT_ENTRY_MARGIN_PCT = cap
    results = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_one, *t): t for t in tasks}
            for i, fut in enumerate(as_completed(futs), 1):
                r = fut.result()
                if r:
                    results.append(r)
                print(f"\r  [{label}] {i}/{len(tasks)} ...", end="", flush=True)
        print()
    finally:
        _bt.LIMIT_ENTRY_MARGIN_PCT = original
    return results


# ── 集計 ──────────────────────────────────────────────────────────────
def _calc_max_drawdown(trade_log: list[dict]) -> float:
    """全トレードを決済日順に並べて最大ドローダウン(円)を計算。"""
    if not trade_log:
        return 0.0
    logs = sorted(trade_log, key=lambda t: t.get("exit_dt") or date.min)
    peak = cum = 0.0
    max_dd = 0.0
    for t in logs:
        cum += t["pnl"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _calc_max_consec_losses(trade_log: list[dict]) -> int:
    """最大連続損切り数。"""
    if not trade_log:
        return 0
    logs = sorted(trade_log, key=lambda t: t.get("exit_dt") or date.min)
    max_streak = cur = 0
    for t in logs:
        if t["pnl"] < 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0
    return max_streak


def _aggregate(results: list[dict]) -> dict:
    trades = signals = wins = 0
    gross_p = gross_l = total_pnl = total_fee = 0.0
    reasons: dict[str, int] = defaultdict(int)
    all_logs: list[dict] = []
    pnl_wins:  list[float] = []
    pnl_losses: list[float] = []

    for r in results:
        signals   += r.get("signals", 0)
        trades    += r.get("trades", 0)
        wins      += r.get("wins", 0)
        total_pnl += r.get("total_pnl", 0)
        total_fee += r.get("total_fee", 0)
        for t in r.get("trade_log", []):
            all_logs.append(t)
            if t["pnl"] > 0:
                gross_p += t["pnl"]
                pnl_wins.append(t["pnl"])
            else:
                gross_l += abs(t["pnl"])
                pnl_losses.append(t["pnl"])
            reasons[t.get("reason", "?")] += 1

    pf       = gross_p / gross_l if gross_l > 0 else float("inf")
    win_rate = wins / trades * 100 if trades > 0 else 0.0
    fill_r   = trades / signals * 100 if signals > 0 else 0.0
    avg_win  = sum(pnl_wins)  / len(pnl_wins)  if pnl_wins  else 0.0
    avg_loss = sum(pnl_losses) / len(pnl_losses) if pnl_losses else 0.0
    rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    max_dd   = _calc_max_drawdown(all_logs)
    max_cons = _calc_max_consec_losses(all_logs)
    # ギャップ約定トレード数 (entry_p > order_limit の近似: entry_p > limit_price * 1.001)
    gap_trades = sum(1 for t in all_logs
                     if t.get("entry_p", 0) > t.get("order_limit", 0) * 1.001)

    return dict(
        signals=signals, trades=trades, wins=wins,
        win_rate=win_rate, pf=pf,
        total_pnl=total_pnl, total_fee=total_fee,
        fill_rate=fill_r,
        avg_win=avg_win, avg_loss=avg_loss, rr_ratio=rr_ratio,
        max_drawdown=max_dd, max_consec_losses=max_cons,
        gap_trades=gap_trades,
        reasons=dict(reasons),
    )


def _aggregate_trades(trades: list[dict]) -> dict:
    """trade_logエントリのリストを直接集計。"""
    n = len(trades)
    if n == 0:
        return dict(trades=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, reasons={})
    wins   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    pnl_w  = [t["pnl"] for t in trades if t.get("pnl", 0) > 0]
    pnl_l  = [t["pnl"] for t in trades if t.get("pnl", 0) < 0]
    gp     = sum(pnl_w) if pnl_w else 0.0
    gl     = sum(abs(v) for v in pnl_l) if pnl_l else 0.0
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[t.get("reason", "?")] += 1
    return dict(
        trades=n, wins=wins,
        win_rate=wins / n * 100,
        pf=gp / gl if gl > 0 else float("inf"),
        total_pnl=sum(t.get("pnl", 0) for t in trades),
        avg_win=gp / len(pnl_w) if pnl_w else 0.0,
        avg_loss=sum(pnl_l) / len(pnl_l) if pnl_l else 0.0,
        reasons=dict(reasons),
    )


def _incremental_analysis(
    all_results: dict[float, list],
    caps: list[float],
) -> tuple[list[dict], list[dict]]:
    """
    各CAP の未約定数と、連続するCAP間の追加約定損益を分析。

    Returns:
        cap_infos: [{cap, label, signals, trades, missed, missed_pct}]
        pairs:     [{label, n_added, agg}]  (追加約定分)
    """
    def _key_map(results):
        m: dict = {}
        for r in results:
            k = (r.get("symbol"), r.get("strategy") or r.get("strategy_name", ""))
            m[k] = r
        return m

    # ── 未約定数 ──
    cap_infos = []
    ref_signals = None
    for cap in caps:
        results = all_results.get(cap, [])
        sigs  = sum(r.get("signals", 0) for r in results)
        fills = sum(r.get("trades",  0) for r in results)
        if ref_signals is None:
            ref_signals = sigs
        missed = ref_signals - fills
        cap_infos.append(dict(
            cap=cap, label=_cap_label(cap),
            signals=ref_signals, trades=fills,
            missed=missed,
            missed_pct=missed / ref_signals * 100 if ref_signals else 0.0,
        ))

    # ── 追加約定分析 ──
    pairs = []
    for i in range(len(caps) - 1):
        cap_A, cap_B = caps[i], caps[i + 1]
        map_A = _key_map(all_results.get(cap_A, []))
        map_B = _key_map(all_results.get(cap_B, []))

        added: list[dict] = []
        for key in set(map_A) | set(map_B):
            r_A = map_A.get(key, {})
            r_B = map_B.get(key, {})
            dates_A = {t.get("entry_dt") for t in r_A.get("trade_log", [])}
            for t in r_B.get("trade_log", []):
                if t.get("entry_dt") not in dates_A:
                    added.append(t)

        pairs.append(dict(
            label=f"{_cap_label(cap_A)} → {_cap_label(cap_B)}",
            cap_A=cap_A, cap_B=cap_B,
            agg=_aggregate_trades(added),
        ))

    return cap_infos, pairs


def _strategy_breakdown(results: list[dict]) -> dict[str, dict]:
    by_strat: dict[str, list] = defaultdict(list)
    for r in results:
        by_strat[r["strategy"]].append(r)
    return {s: _aggregate(rs) for s, rs in sorted(by_strat.items())}


def _symbol_breakdown(results: list[dict]) -> dict[tuple, dict]:
    """(symbol, name, strategy) ごとに集計。"""
    by_sym: dict[tuple, list] = defaultdict(list)
    for r in results:
        key = (r["symbol"], r["name"], r["strategy"])
        by_sym[key].append(r)
    return {k: _aggregate(rs) for k, rs in sorted(by_sym.items())}


def _pf_str(pf):
    return f"{pf:.2f}" if pf != float("inf") else "∞"

def _cap_label(cap: float) -> str:
    return "成行" if cap >= 9.0 else f"+{cap*100:.0f}%"


# ── シグナル設定定義 (--by-signal 用) ─────────────────────────────────
def _build_signal_configs(days: int) -> list[dict]:
    """6シグナル設定のタスクリストを構築。"""

    def _dedup(wl: list) -> list:
        seen: set[tuple] = set()
        out = []
        for item in wl:
            key = (item[0], item[2])
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    def _merge(existing: list, wf: list) -> list:
        ex_keys = {(sym, strat) for sym, _, strat in existing}
        merged = list(existing)
        for item in wf:
            if (item[0], item[2]) not in ex_keys:
                merged.append(item)
        return merged

    def _make_tasks(stop_wl, brk_wl, sp, bp):
        tasks = []
        for sym, name, strat in stop_wl:
            if strat in sp:
                fn, em, sm, tm = sp[strat]
                tasks.append((sym, name, strat, days, fn, em, sm, tm))
        for sym, name, strat in brk_wl:
            if strat in bp:
                fn, em, sm, tm = bp[strat]
                tasks.append((sym, name, strat, days, fn, em, sm, tm))
        return tasks

    try:
        import run_signals_wf as _wf
        wf_sc = _wf._STOP_WATCHLIST_CONSERVATIVE
        wf_sa = _wf._STOP_WATCHLIST_AGGRESSIVE
        wf_bc = _wf._BRK_WATCHLIST_CONSERVATIVE
        wf_ba = _wf._BRK_WATCHLIST_AGGRESSIVE
    except ImportError:
        wf_sc = wf_sa = wf_bc = wf_ba = []

    try:
        import run_signals_nolimit as _nl
        nl_s, nl_b = _nl.STOP_CANDIDATES, _nl.BRK_CANDIDATES
    except ImportError:
        nl_s = nl_b = []

    try:
        import run_signals_merged as _mg
        mg_s = _merge(_stop.WATCHLIST, _mg._WF_STOP)
        mg_b = _merge(_brk.WATCHLIST,  _mg._WF_BRK)
    except ImportError:
        mg_s = _stop.WATCHLIST
        mg_b = _brk.WATCHLIST

    con_sp = _stop.STRATEGY_PARAMS_CONSERVATIVE
    agg_sp = _stop.STRATEGY_PARAMS_AGGRESSIVE
    con_bp = _brk.STRATEGY_PARAMS_CONSERVATIVE
    agg_bp = _brk.STRATEGY_PARAMS_AGGRESSIVE

    configs = [
        {"label": "既存版 conservative", "short": "既存 con",
         "tasks": _dedup(_make_tasks(_stop.WATCHLIST, _brk.WATCHLIST, con_sp, con_bp))},
        {"label": "既存版 aggressive",   "short": "既存 agg",
         "tasks": _dedup(_make_tasks(_stop.WATCHLIST, _brk.WATCHLIST, agg_sp, agg_bp))},
        {"label": "WF conservative",     "short": "WF con",
         "tasks": _dedup(_make_tasks(wf_sc, wf_bc, con_sp, con_bp))},
        {"label": "WF aggressive",       "short": "WF agg",
         "tasks": _dedup(_make_tasks(wf_sa, wf_ba, agg_sp, agg_bp))},
        {"label": "NOLIMIT WF",          "short": "NOLIMIT",
         "tasks": _dedup(_make_tasks(nl_s, nl_b, con_sp, con_bp))},
        {"label": "WF+既存統合",         "short": "統合",
         "tasks": _dedup(_make_tasks(mg_s, mg_b, con_sp, con_bp))},
    ]
    return configs


def _build_by_signal_html(
    sig_data: list[dict],   # [{label, short, cap_results: {cap: agg}, n_sym}]
    caps: list[float],
    days: int,
) -> str:
    today   = date.today().isoformat()
    labels  = [_cap_label(c) for c in caps]
    cur_cap = 0.03

    def _th(c, lbl):
        style = ' style="background:#1a3050"' if c == cur_cap else ""
        note  = '<br><small style="color:#aaa">(現行)</small>' if c == cur_cap else ""
        return f"<th{style}>{lbl}{note}</th>"

    def _cell(val, cap, best_c, cls=""):
        extra = " best" if cap == best_c else (" cur" if cap == cur_cap else "")
        return f'<td class="{cls}{extra}">{val}</td>'

    def _pnl_cell(v, cap, best_c):
        cls   = "pos" if v > 0 else "neg"
        extra = " best" if cap == best_c else (" cur" if cap == cur_cap else "")
        return f'<td class="{cls}{extra}">{v:+,.0f}円</td>'

    def _risk_cell(val, cap, best_c, cls="neg"):
        extra = " best" if cap == best_c else (" cur" if cap == cur_cap else "")
        return f'<td class="{cls} {extra}">{val}</td>'

    sections = ""
    for cfg in sig_data:
        cap_res = cfg["cap_results"]   # {cap: agg_dict}
        n_sym   = cfg["n_sym"]

        best_pnl  = max(caps, key=lambda c: cap_res.get(c, {}).get("total_pnl", 0))
        best_dd   = min(caps, key=lambda c: cap_res.get(c, {}).get("max_drawdown", 1e12))
        best_cons = min(caps, key=lambda c: cap_res.get(c, {}).get("max_consec_losses", 999))
        best_loss = max(caps, key=lambda c: cap_res.get(c, {}).get("avg_loss", -1e12))

        header_cells = "".join(_th(c, lbl) for c, lbl in zip(caps, labels))

        rows = ""
        # ── 利益系 ──
        for metric, key, fmt in [
            ("約定数",         "trades",    lambda v: f"{v:,}"),
            ("ギャップ約定数", "gap_trades", lambda v: f"{v:,}"),
            ("約定率",         "fill_rate",  lambda v: f"{v:.1f}%"),
            ("勝率",           "win_rate",   lambda v: f"{v:.1f}%"),
        ]:
            best_c = max(caps, key=lambda c: cap_res.get(c, {}).get(key, 0))
            rows += f"<tr><td>{metric}</td>"
            rows += "".join(_cell(fmt(cap_res.get(c, {}).get(key, 0)), c, best_c) for c in caps)
            rows += "</tr>\n"

        # PF
        best_c = max(caps, key=lambda c: cap_res.get(c, {}).get("pf", 0))
        rows += "<tr><td>PF</td>"
        for c in caps:
            v = cap_res.get(c, {}).get("pf", 0.0)
            cls = "pos" if v >= 1.5 else ("neg" if v < 1.0 else "")
            extra = " best" if c == best_c else (" cur" if c == cur_cap else "")
            rows += f'<td class="{cls}{extra}">{_pf_str(v)}</td>'
        rows += "</tr>\n"

        # 総損益
        rows += "<tr><td><b>総損益</b></td>"
        rows += "".join(_pnl_cell(cap_res.get(c, {}).get("total_pnl", 0), c, best_pnl) for c in caps)
        rows += "</tr>\n"

        rows += "<tr><td>平均利益/勝ち取引</td>"
        best_c = max(caps, key=lambda c: cap_res.get(c, {}).get("avg_win", 0))
        rows += "".join(_pnl_cell(cap_res.get(c, {}).get("avg_win", 0), c, best_c) for c in caps)
        rows += "</tr>\n"

        # ── リスク指標 ──
        rows += '<tr><td colspan="99" style="background:#2a1010;color:#e07070;font-size:.8em;padding:4px 14px">▼ リスク指標（小さいほど良い）</td></tr>\n'

        rows += "<tr><td>平均損失/負け取引</td>"
        for c in caps:
            v = cap_res.get(c, {}).get("avg_loss", 0.0)
            extra = " best" if c == best_loss else (" cur" if c == cur_cap else "")
            rows += f'<td class="neg {extra}">{v:+,.0f}円</td>'
        rows += "</tr>\n"

        rows += "<tr><td>損益レシオ (W/L)</td>"
        best_c = max(caps, key=lambda c: cap_res.get(c, {}).get("rr_ratio", 0))
        rows += "".join(_cell(f"{cap_res.get(c,{}).get('rr_ratio',0):.2f}", c, best_c) for c in caps)
        rows += "</tr>\n"

        rows += "<tr><td>最大ドローダウン</td>"
        for c in caps:
            v = cap_res.get(c, {}).get("max_drawdown", 0.0)
            extra = " best" if c == best_dd else (" cur" if c == cur_cap else "")
            rows += f'<td class="neg {extra}">{v:,.0f}円</td>'
        rows += "</tr>\n"

        rows += "<tr><td>最大連続損切り</td>"
        for c in caps:
            v = cap_res.get(c, {}).get("max_consec_losses", 0)
            extra = " best" if c == best_cons else (" cur" if c == cur_cap else "")
            rows += f'<td class="neg {extra}">{v}連敗</td>'
        rows += "</tr>\n"

        # ── 決済理由 ──
        rows_reason = ""
        for reason in ["目標達成", "損切り", "タイムカット"]:
            best_c = max(caps, key=lambda c: cap_res.get(c, {}).get("reasons", {}).get(reason, 0))
            rows_reason += f"<tr><td>{reason}</td>"
            for c in caps:
                a  = cap_res.get(c, {})
                n  = a.get("reasons", {}).get(reason, 0)
                t  = a.get("trades") or 1
                extra = " best" if c == best_c else (" cur" if c == cur_cap else "")
                rows_reason += f'<td class="{extra}">{n}件 ({n/t*100:.1f}%)</td>'
            rows_reason += "</tr>\n"

        # ── 未約定・追加約定分析 ──
        cap_raw = cfg.get("cap_raw", {})
        missed_html = ""
        incremental_html = ""
        if cap_raw:
            cap_infos, pairs = _incremental_analysis(cap_raw, caps)

            # 未約定テーブル
            missed_rows = ""
            for ci in cap_infos:
                cur_mark = "<br><small style='color:#aaa'>(現行)</small>" if ci["cap"] == cur_cap else ""
                row_s = ' style="background:#1a3050"' if ci["cap"] == cur_cap else ""
                missed_cls = "neg" if ci["missed_pct"] > 30 else ("" if ci["missed_pct"] > 15 else "pos")
                missed_rows += (
                    f"<tr{row_s}>"
                    f"<td><b>{ci['label']}</b>{cur_mark}</td>"
                    f"<td>{ci['signals']}</td>"
                    f"<td>{ci['trades']}</td>"
                    f'<td class="{missed_cls}">{ci["missed"]}</td>'
                    f'<td class="{missed_cls}">{ci["missed_pct"]:.1f}%</td>'
                    f"</tr>\n"
                )
            missed_html = f"""
<h3 style="margin-top:20px">未約定シグナル数（スキップ）</h3>
<table>
<tr><th>指値上限</th><th>シグナル数</th><th>約定数</th><th>未約定数</th><th>未約定率</th></tr>
{missed_rows}</table>"""

            # 追加約定テーブル
            inc_rows = ""
            for p in pairs:
                a = p["agg"]
                t, wr, pf_, pn, aw, al = (
                    a["trades"], a["win_rate"], a["pf"],
                    a["total_pnl"], a["avg_win"], a["avg_loss"],
                )
                if t == 0:
                    inc_rows += (
                        f"<tr><td>{p['label']}</td>"
                        f'<td class="dim" colspan="6">追加約定なし</td></tr>\n'
                    )
                    continue
                pnl_cls = "pos" if pn > 0 else "neg"
                pf_cls  = "pos" if pf_ >= 1.5 else ("neg" if pf_ < 1.0 else "")
                # 決済理由
                tgt = a["reasons"].get("目標達成", 0)
                stp = a["reasons"].get("損切り",   0)
                inc_rows += (
                    f"<tr>"
                    f"<td>{p['label']}</td>"
                    f"<td>{t}</td>"
                    f"<td>{wr:.1f}%</td>"
                    f'<td class="{pf_cls}">{_pf_str(pf_)}</td>'
                    f'<td class="{pnl_cls}">{pn:+,.0f}円</td>'
                    f"<td>{aw:+,.0f}円</td>"
                    f"<td>{al:+,.0f}円</td>"
                    f"<td>{tgt}件 / {stp}件</td>"
                    f"</tr>\n"
                )
            incremental_html = f"""
<h3 style="margin-top:16px">指値上限を広げた場合の追加約定分析</h3>
<p style="font-size:.82em;color:#7090b0;margin:4px 0 8px">
  例:「+3%→+5%」= +3%では約定しなかったが+5%なら約定した取引だけ抜き出した成績
</p>
<table>
<tr>
  <th>上限拡大</th><th>追加約定数</th><th>勝率</th><th>PF</th>
  <th>追加損益</th><th>平均利益/勝</th><th>平均損失/負</th><th>目標達成/損切り</th>
</tr>
{inc_rows}</table>"""

        sections += f"""
<h2>{cfg['label']} <small style="color:#7090b0;font-weight:normal;font-size:.75em">({n_sym}銘柄)</small></h2>
<table>
<tr><th>項目</th>{header_cells}</tr>
{rows}</table>
<h3 style="margin-top:12px">決済理由内訳</h3>
<table>
<tr><th>決済理由</th>{header_cells}</tr>
{rows_reason}</table>
{missed_html}
{incremental_html}
<hr style="border:none;border-top:1px solid #2a3a5c;margin:32px 0">
"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>シグナル別 CAP比較 {today}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>シグナル設定別 指値上限 比較バックテスト</h1>
<div class="info">
  期間: 直近 {days}日 ｜ モード: {_MODE} ｜ 生成日: {today}<br>
  比較パターン: {" / ".join(labels)}<br>
  黄色ハイライト: 各指標で最良のCAP値 ／ 青色: 現行設定(+3%) ／ リスク指標は値が小さい行が黄色
</div>
{sections}
</body>
</html>"""


# ── HTML 出力 ─────────────────────────────────────────────────────────
_CSS = """
body { font-family: 'Helvetica Neue', sans-serif; margin: 24px 32px;
       background: #1a1a2e; color: #e0e0e0; }
h1 { color: #e0e0e0; border-bottom: 3px solid #3498db; padding-bottom: 8px; }
h2 { color: #e0e0e0; margin-top: 40px; border-left: 4px solid #3498db;
     padding-left: 10px; font-size: 1.1em; }
h3 { color: #a0b8d0; margin-top: 28px; font-size: 1em; }
.info { background:#16213e; padding:12px 20px; border-radius:8px; margin-bottom:20px;
        border:1px solid #2a3a5c; font-size:.9em; color:#b0b8c8; line-height:1.7; }
.explain { background:#0d1b2a; border:1px solid #2a4a6c; border-radius:8px;
           padding:16px 24px; margin:16px 0; font-size:.88em; color:#a0bcd0;
           line-height:1.8; }
.explain b { color:#5dade2; }
.explain code { background:#1a3050; padding:1px 6px; border-radius:3px;
                font-family:monospace; font-size:.95em; color:#7fb3d3; }
table { border-collapse:collapse; width:100%; margin-top:12px; background:#16213e;
        border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,.4); }
th { background:#0f3460; color:#e0e0e0; padding:8px 14px; text-align:center;
     font-size:.83em; white-space:nowrap; }
td { padding:7px 14px; border-bottom:1px solid #2a3a5c; text-align:right;
     font-size:.86em; color:#d0d8e8; white-space:nowrap; }
td:first-child { text-align:left; }
tr:hover { background:#1e2d4a; }
.best { background:#0a3020 !important; }
.cur  { background:#1a2a3a; }
.pos  { color:#2ecc71; font-weight:bold; }
.neg  { color:#e74c3c; font-weight:bold; }
.hi   { color:#f1c40f; font-weight:bold; background:#1a2800 !important; }
.dim  { color:#4a6080; }
.tag  { display:inline-block; padding:2px 8px; border-radius:4px; font-size:.8em;
        font-weight:bold; color:#fff; }
.tag-macd { background:#2980b9; }
.tag-a7   { background:#8e44ad; }
.tag-rsi2 { background:#16a085; }
.tag-don  { background:#d35400; }
.tag-vol  { background:#c0392b; }
.tag-mom  { background:#27ae60; }
.hi   { color:#f1c40f; font-weight:bold; }
.dim  { color:#6080a0; }
"""

def _build_incremental_section(
    all_results: dict[float, list],
    caps: list[float],
    cur_cap: float,
    header_cells: str,
) -> str:
    """未約定テーブル + 追加約定分析テーブルのHTMLを生成。"""
    cap_infos, pairs = _incremental_analysis(all_results, caps)

    # ── 未約定テーブル ──
    missed_rows = ""
    for ci in cap_infos:
        row_s    = ' style="background:#1a3050"' if ci["cap"] == cur_cap else ""
        cur_mark = "<br><small style='color:#aaa'>(現行)</small>" if ci["cap"] == cur_cap else ""
        mc       = "neg" if ci["missed_pct"] > 30 else ("" if ci["missed_pct"] > 15 else "pos")
        missed_rows += (
            f"<tr{row_s}><td><b>{ci['label']}</b>{cur_mark}</td>"
            f"<td>{ci['signals']}</td>"
            f"<td>{ci['trades']}</td>"
            f'<td class="{mc}">{ci["missed"]}</td>'
            f'<td class="{mc}">{ci["missed_pct"]:.1f}%</td>'
            f"</tr>\n"
        )

    # ── 追加約定テーブル ──
    inc_rows = ""
    for p in pairs:
        a = p["agg"]
        t, wr, pf_, pn, aw, al = (
            a["trades"], a["win_rate"], a["pf"],
            a["total_pnl"], a["avg_win"], a["avg_loss"],
        )
        if t == 0:
            inc_rows += (f"<tr><td>{p['label']}</td>"
                         f'<td class="dim" colspan="7">追加約定なし</td></tr>\n')
            continue
        pnl_cls = "pos" if pn > 0 else "neg"
        pf_cls  = "pos" if pf_ >= 1.5 else ("neg" if pf_ < 1.0 else "")
        tgt = a["reasons"].get("目標達成", 0)
        stp = a["reasons"].get("損切り",   0)
        inc_rows += (
            f"<tr>"
            f"<td>{p['label']}</td>"
            f"<td>{t}</td>"
            f"<td>{wr:.1f}%</td>"
            f'<td class="{pf_cls}">{_pf_str(pf_)}</td>'
            f'<td class="{pnl_cls}">{pn:+,.0f}円</td>'
            f"<td>{aw:+,.0f}円</td>"
            f"<td>{al:+,.0f}円</td>"
            f"<td>{tgt}件 / {stp}件</td>"
            f"</tr>\n"
        )

    return f"""
<h2>未約定シグナル数</h2>
<table>
<tr><th>指値上限</th><th>シグナル数</th><th>約定数</th><th>未約定数</th><th>未約定率</th></tr>
{missed_rows}</table>

<h2>指値上限を広げた場合の追加約定分析</h2>
<p style="font-size:.85em;color:#7090b0;margin-top:4px">
  「A→B」= Aでは約定しなかったが、BのCAPまで広げると約定した取引の成績だけを抜き出した結果。<br>
  これがプラスなら上限を広げるメリットあり。マイナスなら「約定数は増えるが割に合わない」。
</p>
<table>
<tr>
  <th>上限拡大</th><th>追加約定数</th><th>勝率</th><th>PF</th>
  <th>追加損益</th><th>平均利益/勝</th><th>平均損失/負</th><th>目標達成/損切り</th>
</tr>
{inc_rows}</table>"""


def _build_html(all_agg: dict[float, dict], all_strat: dict[float, dict],
                caps: list[float], days: int,
                all_results: dict[float, list] | None = None) -> str:
    today = date.today().isoformat()
    labels = [_cap_label(c) for c in caps]
    cur_cap = 0.03  # 現行設定

    # 最大総損益の cap を "best" として強調
    best_cap = max(all_agg, key=lambda c: all_agg[c]["total_pnl"])

    # ── サマリーテーブル ──
    def cell(val, cap, cls=""):
        extra = " best" if cap == best_cap else (" cur" if cap == cur_cap else "")
        return f'<td class="{cls}{extra}">{val}</td>'

    def pnl_cell(v, cap):
        cls = "pos" if v > 0 else "neg"
        extra = " best" if cap == best_cap else (" cur" if cap == cur_cap else "")
        return f'<td class="{cls}{extra}">{v:+,.0f}円</td>'

    def _th(c, lbl):
        style = ' style="background:#1a3050"' if c == cur_cap else ""
        note  = '<br><small style="color:#aaa">(現行)</small>' if c == cur_cap else ""
        return f"<th{style}>{lbl}{note}</th>"
    header_cells = "".join(_th(c, lbl) for c, lbl in zip(caps, labels))

    # 損失系は値が小さい方が良い → best_cap を逆転
    best_cap_dd   = min(all_agg, key=lambda c: all_agg[c]["max_drawdown"])
    best_cap_cons = min(all_agg, key=lambda c: all_agg[c]["max_consec_losses"])
    best_cap_loss = max(all_agg, key=lambda c: all_agg[c]["avg_loss"])  # avg_loss は負値、大きい方が損失小

    def cell_risk(val, cap, best_c):
        extra = " best" if cap == best_c else (" cur" if cap == cur_cap else "")
        return f'<td class="{extra}">{val}</td>'

    rows_summary = ""
    # ── 利益系 ──
    for metric, fmt_fn in [
        ("約定数",           lambda c: f"{all_agg[c]['trades']:,}"),
        ("ギャップ約定数",   lambda c: f"{all_agg[c]['gap_trades']:,}"),
        ("約定率",           lambda c: f"{all_agg[c]['fill_rate']:.1f}%"),
        ("勝率",             lambda c: f"{all_agg[c]['win_rate']:.1f}%"),
        ("PF",               lambda c: _pf_str(all_agg[c]['pf'])),
    ]:
        rows_summary += f"<tr><td>{metric}</td>"
        rows_summary += "".join(cell(fmt_fn(c), c) for c in caps)
        rows_summary += "</tr>\n"

    rows_summary += "<tr><td><b>総損益</b></td>"
    rows_summary += "".join(pnl_cell(all_agg[c]["total_pnl"], c) for c in caps)
    rows_summary += "</tr>\n"

    rows_summary += "<tr><td>平均利益/勝ち取引</td>"
    rows_summary += "".join(pnl_cell(all_agg[c]["avg_win"], c) for c in caps)
    rows_summary += "</tr>\n"

    # ── 損失系 (セクション区切り) ──
    rows_summary += '<tr><td colspan="99" style="background:#2a1010;color:#e07070;font-size:.8em;padding:4px 14px">▼ リスク指標（小さいほど良い）</td></tr>\n'

    rows_summary += "<tr><td>平均損失/負け取引</td>"
    for c in caps:
        v = all_agg[c]["avg_loss"]
        extra = " best" if c == best_cap_loss else (" cur" if c == cur_cap else "")
        rows_summary += f'<td class="neg{" " + extra if extra else ""}">{v:+,.0f}円</td>'
    rows_summary += "</tr>\n"

    rows_summary += "<tr><td>損益レシオ (W/L)</td>"
    rows_summary += "".join(cell(f"{all_agg[c]['rr_ratio']:.2f}", c) for c in caps)
    rows_summary += "</tr>\n"

    rows_summary += "<tr><td>最大ドローダウン</td>"
    for c in caps:
        v = all_agg[c]["max_drawdown"]
        extra = " best" if c == best_cap_dd else (" cur" if c == cur_cap else "")
        rows_summary += f'<td class="neg{" " + extra if extra else ""}">{v:,.0f}円</td>'
    rows_summary += "</tr>\n"

    rows_summary += "<tr><td>最大連続損切り</td>"
    for c in caps:
        v = all_agg[c]["max_consec_losses"]
        extra = " best" if c == best_cap_cons else (" cur" if c == cur_cap else "")
        rows_summary += f'<td class="neg{" " + extra if extra else ""}">{v}連敗</td>'
    rows_summary += "</tr>\n"

    # ── 決済理由テーブル ──
    rows_reason = ""
    for reason in ["目標達成", "損切り", "タイムカット"]:
        rows_reason += f"<tr><td>{reason}</td>"
        for c in caps:
            n = all_agg[c]["reasons"].get(reason, 0)
            t = all_agg[c]["trades"] or 1
            extra = " best" if c == best_cap else (" cur" if c == cur_cap else "")
            rows_reason += f'<td class="{extra}">{n}件 ({n/t*100:.1f}%)</td>'
        rows_reason += "</tr>\n"

    # ── 戦略別 詳細テーブル（行=CAP, 列=指標, 1戦略ごとに1テーブル）──
    strat_order = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    all_strats = sorted(
        {s for strat_map in all_strat.values() for s in strat_map},
        key=lambda s: strat_order.index(s) if s in strat_order else 99,
    )

    def _strat_detail_table(strat: str) -> str:
        best_c = max(caps, key=lambda c: all_strat[c].get(strat, {}).get("total_pnl", 0))
        rows = ""
        for c in caps:
            a    = all_strat[c].get(strat, {})
            lbl  = _cap_label(c)
            t    = a.get("trades", 0)
            w    = a.get("wins", 0)
            wr   = a.get("win_rate", 0.0)
            pf   = a.get("pf", 0.0)
            pn   = a.get("total_pnl", 0.0)
            pf_s = _pf_str(pf)
            cur_mark = "<br><small style='color:#aaa'>(現行)</small>" if c == cur_cap else ""
            row_cls  = ' style="background:#1a3050"' if c == cur_cap else \
                       (' style="background:#0a3020"' if c == best_c else "")
            pnl_cls  = "pos" if pn > 0 else "neg"
            pf_cls   = "pos" if pf >= 1.5 else ("neg" if pf < 1.0 else "")
            if t == 0:
                rows += (f"<tr{row_cls}>"
                         f"<td><b>{lbl}</b>{cur_mark}</td>"
                         f'<td class="dim" colspan="5">データなし</td></tr>\n')
            else:
                rows += (f"<tr{row_cls}>"
                         f"<td><b>{lbl}</b>{cur_mark}</td>"
                         f"<td>{t}</td>"
                         f"<td>{w}</td>"
                         f"<td>{wr:.1f}%</td>"
                         f'<td class="{pf_cls}">{pf_s}</td>'
                         f'<td class="{pnl_cls}">{pn:+,.0f}円</td>'
                         f"</tr>\n")
        tag_cls = f"tag-{strat.lower()}"
        return (f'<h3><span class="tag {tag_cls}" style="font-size:1em;padding:4px 12px">{strat}</span></h3>\n'
                f"<table>\n"
                f"<tr><th>指値上限</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>総損益</th></tr>\n"
                f"{rows}</table>\n")

    # ── 銘柄別最適CAP テーブル（戦略別セクション）──
    all_sym_keys = sorted({k for c in caps for k in _symbol_breakdown(all_results[c])})

    # 戦略の表示順
    strat_order = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    all_strats_sym = sorted({k[2] for k in all_sym_keys},
                            key=lambda s: strat_order.index(s) if s in strat_order else 99)

    def _sym_row(sym, name, strat):
        pnls = {}
        trades_by_cap = {}
        for c in caps:
            sb = _symbol_breakdown(all_results[c])
            d  = sb.get((sym, name, strat), {})
            pnls[c]          = d.get("total_pnl", None)
            trades_by_cap[c] = d.get("trades", 0)
        valid = {c: v for c, v in pnls.items() if v is not None}
        if not valid:
            return ""
        best_c   = max(valid, key=lambda c: valid[c])
        best_pnl = valid[best_c]
        n_cur    = trades_by_cap.get(cur_cap, 0)
        rel_color = "#e74c3c" if n_cur <= 3 else ("#f39c12" if n_cur <= 6 else "#2ecc71")
        rel_mark  = " ⚠️" if n_cur <= 3 else (" △" if n_cur <= 6 else "")

        row  = "<tr>"
        row += f'<td>{sym}</td><td>{name}</td>'
        row += f'<td style="color:{rel_color};font-weight:bold;text-align:center">{n_cur}回{rel_mark}</td>'
        row += f'<td style="color:#f1c40f;font-weight:bold;text-align:center">{_cap_label(best_c)}</td>'
        row += f'<td class="{"pos" if best_pnl > 0 else "neg"}">{best_pnl:+,.0f}円</td>'
        for c in caps:
            v = pnls.get(c)
            n = trades_by_cap.get(c, 0)
            if v is None:
                row += '<td class="dim">─</td>'
            else:
                cls  = "pos" if v > 0 else "neg"
                hi   = " hi"  if c == best_c  else ""
                cur2 = " cur" if c == cur_cap else ""
                row += (f'<td class="{cls}{hi}{cur2}" title="{n}回取引">'
                        f'{v:+,.0f}<br>'
                        f'<span style="font-size:.75em;color:#7090b0">{n}回</span></td>')
        row += "</tr>\n"
        return row

    def _strat_subtotal(strat):
        """戦略の合計行。"""
        total_pnl = {c: 0.0 for c in caps}
        total_n   = {c: 0   for c in caps}
        keys = [k for k in all_sym_keys if k[2] == strat]
        for sym, name, s in keys:
            for c in caps:
                sb = _symbol_breakdown(all_results[c])
                d  = sb.get((sym, name, s), {})
                total_pnl[c] += d.get("total_pnl", 0)
                total_n[c]   += d.get("trades", 0)
        best_c = max(caps, key=lambda c: total_pnl[c])
        row  = '<tr style="background:#0f2030;font-weight:bold;">'
        row += f'<td colspan="2" style="color:#7fb3d3">合計 ({len(keys)}銘柄)</td>'
        row += f'<td style="text-align:center;color:#7090b0">{total_n.get(cur_cap,0)}回</td>'
        row += f'<td style="color:#f1c40f;text-align:center">{_cap_label(best_c)}</td>'
        best_pnl = total_pnl[best_c]
        row += f'<td class="{"pos" if best_pnl>0 else "neg"}">{best_pnl:+,.0f}円</td>'
        for c in caps:
            v    = total_pnl[c]
            cls  = "pos" if v > 0 else "neg"
            hi   = " hi"  if c == best_c  else ""
            cur2 = " cur" if c == cur_cap else ""
            row += f'<td class="{cls}{hi}{cur2}">{v:+,.0f}<br><span style="font-size:.75em;color:#7090b0">{total_n[c]}回</span></td>'
        row += "</tr>\n"
        return row

    # 戦略別セクションごとに HTML を構築
    sym_sections = ""
    for strat in all_strats_sym:
        tag_cls  = f"tag-{strat.lower()}"
        keys_s   = sorted([k for k in all_sym_keys if k[2] == strat])
        body_rows = "".join(_sym_row(*k) for k in keys_s)
        if not body_rows:
            continue
        sym_sections += f"""
<h3><span class="tag {tag_cls}" style="font-size:1em;padding:4px 12px">{strat}</span></h3>
<table>
<tr>
  <th>コード</th><th>銘柄名</th>
  <th>取引回数<br><small style="color:#aaa">(+3%基準)</small></th>
  <th style="color:#f1c40f">最適CAP</th>
  <th style="color:#f1c40f">最大損益</th>
  {"".join(f'<th>{lbl}<br><small style="color:#aaa">損益/回数</small></th>' for lbl in labels)}
</tr>
{body_rows}{_strat_subtotal(strat)}
</table>
"""

    # ── 仕組み解説 ──
    explain = f"""
<div class="explain">
<b>【仕組みの解説】逆指値注文の「指値上限」とは</b><br><br>
日本株の逆指値注文には2種類あります:<br>
&nbsp;&nbsp;① <b>逆指値→指値</b>: 「トリガー価格に達したら、<code>X円以下</code>で買う」<br>
&nbsp;&nbsp;② <b>逆指値→成行</b>: 「トリガー価格に達したら、<code>いくらでも</code>買う」<br><br>

このシステムでは <b>トリガー価格 = 前日終値</b>、指値上限 = トリガー × (1 + cap) です。<br><br>

<b>例) イビデン(前日終値 23,000円)でシグナル発生:</b><br>
&nbsp;&nbsp;トリガー価格  = <code>23,000円</code>（ここを超えたら注文発動）<br>
&nbsp;&nbsp;指値上限 +3%  = <code>23,690円</code> → 始値が23,690円以下なら約定、超えたらスキップ<br>
&nbsp;&nbsp;指値上限 +5%  = <code>24,150円</code> → 始値が24,150円以下なら約定<br>
&nbsp;&nbsp;成行(上限なし) = 始値がいくらでも約定（今日はこれが有利だった）<br><br>

<b>ギャップアップ時の損益への影響:</b><br>
&nbsp;&nbsp;• ギャップ約定すると損切りラインまでの距離が縮まる（＝リスクが増える）<br>
&nbsp;&nbsp;&nbsp;&nbsp;例) +5%ギャップ約定 → 損切りまでが実質 -9% でなく -4% になる<br>
&nbsp;&nbsp;• そのため総損益だけでなく <b>最大ドローダウン・最大連続損切り</b> も重要な比較指標<br>
&nbsp;&nbsp;• 成行が「利益は多いが損失も大きい」か「リスク込みで有利」かをこのレポートで確認できます<br><br>

<b>黄色ハイライト</b>: 各指標で最良のCAP値  ／  <b>青色</b>: 現行設定(+3%)  ／  リスク指標は値が小さい行が黄色
</div>
"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>指値上限 比較 {today}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>指値上限 比較バックテスト</h1>
<div class="info">
  期間: 直近 {days}日 ｜ モード: {_MODE} ｜ 生成日: {today}<br>
  比較パターン: {" / ".join(labels)}<br>
  黄色セル = 各行で最良の設定　青色セル = 現行設定(+3%)
</div>
{explain}

<h2>全体サマリー</h2>
<table>
<tr><th>項目</th>{header_cells}</tr>
{rows_summary}
</table>

<h2>決済理由内訳</h2>
<table>
<tr><th>決済理由</th>{header_cells}</tr>
{rows_reason}
</table>

{_build_incremental_section(all_results, caps, cur_cap, header_cells) if all_results else ""}

<h2>戦略別 詳細比較</h2>
<p style="font-size:.85em;color:#7090b0;margin-top:4px">
  黄色セル = 各戦略で最も総損益が高いCAP値　青色セル = 現行設定(+3%)
</p>
{"".join(_strat_detail_table(s) for s in all_strats)}

<h2>銘柄別 最適指値上限（戦略別）</h2>
<p style="font-size:.85em;color:#7090b0;margin-top:4px">
  黄色 = その銘柄で最も総損益が高いCAP値　青 = 現行(+3%)　数値単位: 円<br>
  ⚠️ <span style="color:#e74c3c">赤=取引3回以下</span>（少ない取引数での最適CAPは偶然の可能性が高い）
  　△ <span style="color:#f39c12">橙=取引4〜6回</span>（参考値）
  　<span style="color:#2ecc71">緑=7回以上</span>（信頼度高）
  　合計行 = その戦略全銘柄の集計
</p>
{sym_sections if all_results else '<p class="dim">銘柄別データなし</p>'}

</body>
</html>"""


# ── テキスト表示 ──────────────────────────────────────────────────────
def print_summary(all_agg: dict[float, dict], caps: list[float], days: int):
    print()
    print("=" * 75)
    print(f"  指値上限 比較  ({days}日  モード: {_MODE})")
    print("=" * 75)
    labels = [_cap_label(c) for c in caps]
    header = f"  {'':12s}" + "".join(f"{l:>13s}" for l in labels)
    print(header)
    print("  " + "-" * (12 + 13 * len(caps)))

    for metric, fmt_fn in [
        ("約定数",  lambda c: f"{all_agg[c]['trades']:,}"),
        ("約定率",  lambda c: f"{all_agg[c]['fill_rate']:.1f}%"),
        ("勝率",    lambda c: f"{all_agg[c]['win_rate']:.1f}%"),
        ("PF",      lambda c: _pf_str(all_agg[c]['pf'])),
        ("総損益",  lambda c: f"{all_agg[c]['total_pnl']:+,.0f}"),
    ]:
        row = f"  {metric:12s}" + "".join(f"{fmt_fn(c):>13s}" for c in caps)
        print(row)


# ── メイン ──────────────────────────────────────────────────────────
def main():
    args = _parse()
    caps = sorted(set(args.caps))

    # ── タスクリスト構築 ──────────────────────────────────────────────
    if getattr(args, "all_watchlists", False):
        # 全WATCHLIST(stop/brk × base + WF conservative + WF aggressive)を統合・重複除去
        try:
            import run_signals_wf as _wf
            wf_stop_lists = [
                _wf._STOP_WATCHLIST_CONSERVATIVE,
                _wf._STOP_WATCHLIST_AGGRESSIVE,
            ]
            wf_brk_lists = [
                _wf._BRK_WATCHLIST_CONSERVATIVE,
                _wf._BRK_WATCHLIST_AGGRESSIVE,
            ]
        except ImportError:
            wf_stop_lists = []
            wf_brk_lists  = []

        seen: set[tuple[str, str]] = set()
        raw_tasks: list[tuple] = []

        def _add_stop(wlist):
            for sym, name, strat in wlist:
                key = (sym, strat)
                if key not in seen:
                    seen.add(key)
                    fn, em, sm, tm = _stop.STRATEGY_PARAMS[strat]
                    raw_tasks.append((sym, name, strat, args.days, fn, em, sm, tm))

        def _add_brk(wlist):
            for sym, name, strat in wlist:
                key = (sym, strat)
                if key not in seen:
                    seen.add(key)
                    fn, em, sm, tm = _brk.STRATEGY_PARAMS[strat]
                    raw_tasks.append((sym, name, strat, args.days, fn, em, sm, tm))

        _add_stop(_stop.WATCHLIST)
        for wl in wf_stop_lists:
            _add_stop(wl)
        _add_brk(_brk.WATCHLIST)
        for wl in wf_brk_lists:
            _add_brk(wl)

        tasks = raw_tasks
        print(f"モード: {_MODE}  期間: {args.days}日  銘柄数: {len(tasks)} (全WATCHLIST統合)")
    else:
        tasks = []
        for sym, name, strat in _stop.WATCHLIST:
            fn, em, sm, tm = _stop.STRATEGY_PARAMS[strat]
            tasks.append((sym, name, strat, args.days, fn, em, sm, tm))
        for sym, name, strat in _brk.WATCHLIST:
            fn, em, sm, tm = _brk.STRATEGY_PARAMS[strat]
            tasks.append((sym, name, strat, args.days, fn, em, sm, tm))

        print(f"モード: {_MODE}  期間: {args.days}日  銘柄数: {len(tasks)}")
    print(f"比較パターン: {[_cap_label(c) for c in caps]}")
    print()

    # ── --by-signal: 6設定ごとにCAP比較 ──────────────────────────────
    sig_data_list: list[dict] = []
    if getattr(args, "by_signal", False):
        signal_configs = _build_signal_configs(args.days)
        for cfg in signal_configs:
            cfg_tasks = cfg["tasks"]
            if not cfg_tasks:
                print(f"  [{cfg['short']}] スキップ (銘柄なし)")
                sig_data_list.append({**cfg, "cap_results": {}, "n_sym": 0})
                continue
            print(f"▶ [{cfg['short']}] {len(cfg_tasks)}銘柄 × {len(caps)}パターン")
            cap_results: dict[float, dict] = {}
            cap_raw: dict[float, list]    = {}
            for cap in caps:
                lbl = f"{cfg['short']} {_cap_label(cap)}"
                res = _run_all(cfg_tasks, cap=cap, workers=args.workers, label=lbl)
                cap_results[cap] = _aggregate(res)
                cap_raw[cap]     = res
            sig_data_list.append({**cfg, "cap_results": cap_results,
                                   "cap_raw": cap_raw, "n_sym": len(cfg_tasks)})
        # --by-signal 時は all_results も tasks ベースで計算（print_summary 用）
        all_results: dict[float, list] = {}
        for cap in caps:
            all_results[cap] = []   # skip individual symbol-level detail
    else:
        all_results: dict[float, list] = {}
        for cap in caps:
            label = _cap_label(cap)
            print(f"▶ [{label}] バックテスト中…")
            res = _run_all(tasks, cap=cap, workers=args.workers, label=label)
            all_results[cap] = res

    if getattr(args, "by_signal", False):
        no_data = all(
            all(a.get("trades", 0) == 0 for a in cfg["cap_results"].values())
            for cfg in sig_data_list
        )
    else:
        no_data = all(len(v) == 0 for v in all_results.values())
    if no_data:
        print("データが取得できませんでした (ネットワーク接続を確認してください)")
        return

    if not getattr(args, "by_signal", False):
        all_agg   = {c: _aggregate(r) for c, r in all_results.items()}
        all_strat = {c: _strategy_breakdown(r) for c, r in all_results.items()}
        print_summary(all_agg, caps, args.days)
    else:
        all_agg   = {}
        all_strat = {}

    if args.html:
        if getattr(args, "by_signal", False):
            html = _build_by_signal_html(sig_data_list, caps, args.days)
        else:
            html = _build_html(all_agg, all_strat, caps, args.days, all_results)
        suffix = "_aggressive" if _MODE == "aggressive" else ""
        mode_sfx = "_by_signal" if getattr(args, "by_signal", False) else ""
        fname = f"compare_gap_entry{suffix}{mode_sfx}_{date.today().isoformat()}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML出力: {fname}")
        if not getattr(args, "no_browser", False):
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(fname)}")


if __name__ == "__main__":
    main()
