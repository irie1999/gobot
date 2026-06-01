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

    # ── 戦略別テーブル (総損益のみ) ──
    all_strats = sorted({s for strat_map in all_strat.values() for s in strat_map})
    rows_strat = ""
    for strat in all_strats:
        rows_strat += f"<tr><td>{strat}</td>"
        for c in caps:
            v = all_strat[c].get(strat, {}).get("total_pnl", 0)
            # この戦略で最大の cap
            best_for_strat = max(caps, key=lambda x: all_strat[x].get(strat, {}).get("total_pnl", 0))
            cls = "pos" if v > 0 else "neg"
            extra = " best" if c == best_for_strat else (" cur" if c == cur_cap else "")
            rows_strat += f'<td class="{cls}{extra}">{v:+,.0f}円</td>'
        rows_strat += "</tr>\n"

    # 戦略 PF 行
    rows_pf = ""
    for strat in all_strats:
        rows_pf += f"<tr><td>{strat}</td>"
        for c in caps:
            v = _pf_str(all_strat[c].get(strat, {}).get("pf", 0))
            extra = " cur" if c == cur_cap else ""
            rows_pf += f'<td class="{extra}">{v}</td>'
        rows_pf += "</tr>\n"

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

<h2>戦略別 総損益</h2>
<table>
<tr><th>戦略</th>{header_cells}</tr>
{rows_strat}
</table>

<h2>戦略別 PF</h2>
<table>
<tr><th>戦略</th>{header_cells}</tr>
{rows_pf}
</table>

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

    all_results: dict[float, list] = {}
    for cap in caps:
        label = _cap_label(cap)
        print(f"▶ [{label}] バックテスト中…")
        res = _run_all(tasks, cap=cap, workers=args.workers, label=label)
        all_results[cap] = res

    if all(len(v) == 0 for v in all_results.values()):
        print("データが取得できませんでした (ネットワーク接続を確認してください)")
        return

    all_agg   = {c: _aggregate(r) for c, r in all_results.items()}
    all_strat = {c: _strategy_breakdown(r) for c, r in all_results.items()}

    print_summary(all_agg, caps, args.days)

    if args.html:
        html = _build_html(all_agg, all_strat, caps, args.days, all_results)
        suffix = "_aggressive" if _MODE == "aggressive" else ""
        fname = f"compare_gap_entry{suffix}_{date.today().isoformat()}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML出力: {fname}")
        if not getattr(args, "no_browser", False):
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(fname)}")


if __name__ == "__main__":
    main()
