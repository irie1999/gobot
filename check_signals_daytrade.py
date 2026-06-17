"""
check_signals_daytrade.py  ―  デイトレ シグナル判定・HTMLレポート
==================================================================
スイング版 check_signals_stop.py のデイトレ対応版。

【戦略: PREV_CLOSE_BREAK (前日終値ブレイク)】
  前日の引け値を逆指値トリガーとして、当日寄り後にブレイクしたらロング。
  スイング版の em=0.0 (終値逆指値) と同じ論理構造をデイトレに適用。

【BTスコア】
  スイング版と同一計算式。30/60/90/120/150/180日スライスで平均。
  ランク: ★★★≥80, ★★≥60, ★≥40, △<40

【使い方】
  # シグナル確認 (今日)
  python check_signals_daytrade.py

  # 過去日のシグナル確認
  python check_signals_daytrade.py --date 2026-06-16

  # 期間指定
  python check_signals_daytrade.py --days 90

  # ブラウザを開かない
  python check_signals_daytrade.py --no-browser

  # データソース指定
  python check_signals_daytrade.py --source local
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_intraday import (
    BACKTEST_DAYS, FIXED_QTY, FEE_PCT_ONE_WAY,
    PERIODS, SLIPPAGE_STOP_PCT,
    run_intraday_backtest, calc_recommend_score,
    apply_atr_penalty, calc_atr_5m, _get_daily_groups,
)
from daytrade_data import load_intraday_batch

JST = timezone(timedelta(hours=9))

WORKERS = 4

# ── WATCHLIST ─────────────────────────────────────────────────
# 初期リスト: daytrade_symbols.py + check_signals_stop.py の WATCHLIST より選定。
# 流動性が高く、前日終値ブレイク戦略に向く銘柄。
# WalkForward スキャン後に随時更新予定。
WATCHLIST: list[tuple[str, str]] = [
    # 高流動性 (出来高上位)
    ("7203.T", "トヨタ自動車"),
    ("9984.T", "ソフトバンクグループ"),
    ("6758.T", "ソニーグループ"),
    ("8306.T", "三菱UFJフィナンシャル・グループ"),
    ("7267.T", "本田技研工業"),
    # 中流動性・適度なボラ
    ("8058.T", "三菱商事"),
    ("7259.T", "アイシン"),
    ("5741.T", "ＵＡＣＪ"),
    ("5707.T", "東邦亜鉛"),
    ("4385.T", "メルカリ"),
    ("6027.T", "弁護士ドットコム"),
    ("8179.T", "ロイヤルホールディングス"),
    ("8012.T", "長瀬産業"),
    ("7282.T", "豊田合成"),
    ("6471.T", "日本精工"),
    ("8051.T", "山善"),
    ("8346.T", "東邦銀行"),
    ("5975.T", "東プレ"),
    ("9048.T", "名古屋鉄道"),
    ("8609.T", "岡三証券グループ"),
]

# ── 戦略パラメータ (em, sm, tm) ──────────────────────────────
# conservative モード (デフォルト): tm=3.0 (2R設定)
STRATEGY_PARAMS_CONSERVATIVE = {
    "PREV_CLOSE_BREAK": (0.0, 1.5, 3.0),  # prev_close逆指値, stop=1.5ATR, target=3ATR
}
# aggressive モード: tm=2.0 (回転率優先)
STRATEGY_PARAMS_AGGRESSIVE = {
    "PREV_CLOSE_BREAK": (0.0, 1.5, 2.0),
}

import os as _os
TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()
if TRADING_MODE == "aggressive":
    STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
else:
    STRATEGY_PARAMS = STRATEGY_PARAMS_CONSERVATIVE
    TRADING_MODE = "conservative"


# ── シグナル判定 ───────────────────────────────────────────────

def check_signal_on_date(
    symbol: str,
    strategy: str,
    df_5m: pd.DataFrame | None,
    target_date=None,
) -> dict | None:
    """
    指定日のシグナル（発注価格・損切り・目標）を返す。

    PREV_CLOSE_BREAK では「前日終値を超えたら買い」なので、
    シグナルは常に存在する (毎日エントリー候補)。

    Args:
        symbol      : 銘柄コード
        strategy    : 戦略名 ("PREV_CLOSE_BREAK")
        df_5m       : 5分足 DataFrame (load_intraday_batch 済み)
        target_date : None=今日 / date/str=指定日

    Returns:
        dict with order_price, stop_price, target_price, prev_close, atr_5m, ...
        or None (データ不足)
    """
    if df_5m is None or df_5m.empty:
        return None
    if strategy not in STRATEGY_PARAMS:
        return None

    em, sm, tm = STRATEGY_PARAMS[strategy]

    # ATR を全バー横断で計算
    df_atr = calc_atr_5m(df_5m)

    if target_date is None:
        # 今日モード: 最新取引日の終値・ATR を使用
        dates = sorted(set(df_atr.index.date))
        if len(dates) < 2:
            return None
        ref_date = dates[-1]       # 直近の取引日 (今日または前取引日)
        signal_date = ref_date
    else:
        ts_target = pd.Timestamp(target_date)
        ref_date = None
        for d in sorted(set(df_atr.index.date), reverse=True):
            if pd.Timestamp(d) <= ts_target:
                ref_date = d
                break
        if ref_date is None:
            return None
        # target_date の前日終値を使う
        dates_before = sorted(d for d in set(df_atr.index.date) if d < ref_date)
        if not dates_before:
            return None
        prev_d   = dates_before[-1]
        ref_date = prev_d
        signal_date = ref_date

    ref_bars = df_atr[df_atr.index.date == ref_date]
    if ref_bars.empty:
        return None

    prev_close = float(ref_bars["close"].iloc[-1])
    atr_v      = float(ref_bars["atr"].iloc[-1])

    if pd.isna(prev_close) or prev_close <= 0:
        return None
    if pd.isna(atr_v) or atr_v <= 0:
        return None

    order_p       = prev_close + atr_v * em   # em=0.0 なら prev_close そのまま
    stop_p        = order_p   - atr_v * sm
    target_p      = order_p   + atr_v * tm
    stop_loss_pct = (order_p - stop_p) / order_p * 100 if order_p > 0 else 0.0
    qty           = FIXED_QTY
    position_v    = round(order_p * qty)

    return dict(
        strategy=strategy,
        symbol=symbol,
        signal_date=str(signal_date),
        order_price=round(order_p, 1),
        stop_price=round(stop_p, 1),
        target_price=round(target_p, 1),
        prev_close=prev_close,
        atr_5m=round(atr_v, 2),
        stop_loss_pct=round(stop_loss_pct, 2),
        qty=qty,
        position_value=position_v,
    )


# ── バックテスト一括実行 ────────────────────────────────────────

def _run_one(sym: str, name: str, df_5m: pd.DataFrame,
             strategy: str, backtest_days: int) -> dict:
    """1銘柄×1戦略のバックテスト + BTスコア計算。"""
    em, sm, tm = STRATEGY_PARAMS[strategy]
    result = run_intraday_backtest(
        sym, name, df_5m,
        entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
        backtest_days=backtest_days,
        strategy_name=strategy,
    )
    score, rank = calc_recommend_score(result["period_results"])

    # 代表損切り幅 (直近 30d の平均 stop_loss_pct)
    trades_30 = result["period_results"].get(30, {}).get("trade_log", [])
    avg_sl_pct = (sum(t.get("stop_loss_pct", 0) for t in trades_30) / len(trades_30)
                  if trades_30 else 0.0)
    adj_score, atr_note = apply_atr_penalty(score, avg_sl_pct)

    result.update(
        bt_score=adj_score,
        bt_rank=rank,
        atr_note=atr_note,
    )
    return result


def run_backtest_all(
    symbols: list[tuple[str, str]] | None = None,
    days: int = BACKTEST_DAYS,
    strategies: list[str] | None = None,
    source: str = "auto",
    workers: int = WORKERS,
) -> list[dict]:
    """
    WATCHLIST 全銘柄のバックテストを並列実行。

    Returns:
        list of result dicts (BTスコア降順)
    """
    if symbols is None:
        symbols = WATCHLIST
    if strategies is None:
        strategies = list(STRATEGY_PARAMS.keys())

    sym_codes = [s for s, _ in symbols]
    print(f"データ取得: {len(sym_codes)}銘柄  source={source}  days={days}")
    dfs = load_intraday_batch(sym_codes, days=days, source=source)

    tasks: list[tuple[str, str, pd.DataFrame, str]] = []
    for sym, name in symbols:
        df = dfs.get(sym)
        if df is None or df.empty:
            continue
        for strat in strategies:
            tasks.append((sym, name, df, strat))

    results: list[dict] = []
    print(f"バックテスト実行: {len(tasks)}タスク  workers={workers}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_run_one, sym, name, df, strat, days): (sym, name, strat)
            for sym, name, df, strat in tasks
        }
        for i, fut in enumerate(as_completed(futures), 1):
            sym, name, strat = futures[fut]
            try:
                res = fut.result()
                results.append(res)
            except Exception as e:
                print(f"  [warn] {sym} {strat}: {e}", file=sys.stderr)
            if i % 5 == 0 or i == len(tasks):
                print(f"  {i}/{len(tasks)} 完了", flush=True)

    results.sort(key=lambda r: r.get("bt_score", 0), reverse=True)
    return results


# ── HTML 生成 ──────────────────────────────────────────────────

def _pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "∞"
    if pf == 0.0:
        return "-"
    return f"{pf:.2f}"


def _pnl_color(v: float) -> str:
    if v > 0:
        return "#16a34a"
    if v < 0:
        return "#dc2626"
    return "#6b7280"


def build_html(
    results: list[dict],
    show_days: int = 90,
    date_label: str = "",
    target_date=None,
    dfs: dict[str, pd.DataFrame] | None = None,
) -> str:
    """
    デイトレシグナルレポートの HTML を生成。

    Args:
        results    : run_backtest_all の戻り値
        show_days  : サマリーに表示する期間 (デフォルト 90d)
        date_label : レポート日付ラベル
        target_date: シグナル確認日 (None=今日)
        dfs        : 5分足データ dict (signal 取得用)
    """
    today_str  = date_label or datetime.now(JST).strftime("%Y-%m-%d")
    mode_label = "積極 (aggressive)" if TRADING_MODE == "aggressive" else "標準 (conservative)"

    # 各銘柄のシグナルを取得
    signals: dict[tuple[str, str], dict | None] = {}
    if dfs is not None:
        for res in results:
            sym   = res["symbol"]
            strat = res["strategy"]
            df    = dfs.get(sym)
            sig   = check_signal_on_date(sym, strat, df, target_date)
            signals[(sym, strat)] = sig

    # ── スタイル ────────────────────────────────────────────────
    style = """
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Hiragino Sans', 'Yu Gothic', sans-serif; font-size: 13px;
       background: #f8fafc; color: #1e293b; padding: 16px; }
h1 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
.subtitle { color: #64748b; font-size: 12px; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 8px; overflow: hidden;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 24px; }
th { background: #1e293b; color: #fff; padding: 8px 10px;
     text-align: right; font-weight: 600; white-space: nowrap; }
th:first-child, th:nth-child(2) { text-align: left; }
td { padding: 7px 10px; border-bottom: 1px solid #f1f5f9;
     text-align: right; white-space: nowrap; }
td:first-child, td:nth-child(2) { text-align: left; }
tr:hover td { background: #f8fafc; }
.rank-3 { color: #f59e0b; font-weight: 700; }
.rank-2 { color: #10b981; font-weight: 700; }
.rank-1 { color: #3b82f6; }
.rank-0 { color: #94a3b8; }
.score-high { background: #fef3c7; border-radius: 4px; padding: 1px 6px; }
.score-mid  { background: #d1fae5; border-radius: 4px; padding: 1px 6px; }
.tag { display: inline-block; font-size: 10px; border-radius: 3px;
       padding: 1px 5px; margin: 1px; font-weight: 600; }
.tag-pcb  { background: #dbeafe; color: #1d4ed8; }
.tag-long { background: #dcfce7; color: #166534; }
.section-title { font-size: 14px; font-weight: 700; margin: 20px 0 8px; color: #334155; }
.warn { color: #f59e0b; font-size: 11px; }
.no-signal { color: #94a3b8; font-size: 11px; }
</style>
"""

    # ── サマリーテーブル ────────────────────────────────────────
    rows = []
    for res in results:
        sym   = res["symbol"]
        name  = res.get("name", sym)
        strat = res.get("strategy", "?")
        score = res.get("bt_score", 0)
        rank  = res.get("bt_rank", "-")

        pr = res["period_results"].get(show_days, {})
        n   = pr.get("trades", 0)
        wr  = pr.get("win_rate", 0)
        pf  = pr.get("pf", 0)
        pnl = pr.get("total_pnl", 0)

        sig = signals.get((sym, strat))

        rank_cls = ("rank-3" if rank == "★★★" else
                    "rank-2" if rank == "★★"   else
                    "rank-1" if rank == "★"     else "rank-0")
        score_cls = "score-high" if score >= 60 else ("score-mid" if score >= 40 else "")

        sig_html = ""
        if sig:
            op = sig["order_price"]
            sp = sig["stop_price"]
            tp = sig["target_price"]
            sl = sig["stop_loss_pct"]
            sig_html = (f"<b>{op:,.0f}</b> 円  "
                        f"<span style='color:#dc2626'>▼{sp:,.0f}</span>  "
                        f"<span style='color:#16a34a'>▲{tp:,.0f}</span>  "
                        f"<span class='warn'>(損切{sl:.1f}%)</span>")
        else:
            sig_html = "<span class='no-signal'>データ不足</span>"

        pnl_color = _pnl_color(pnl)
        pf_display = _pf_str(pf)

        rows.append(f"""
<tr>
  <td><b>{sym.replace('.T','')}</b></td>
  <td>{name}<br><span class='tag tag-pcb'>{strat}</span></td>
  <td class='{rank_cls}'><span class='{score_cls}'>{score}</span> {rank}</td>
  <td style='text-align:left'>{sig_html}</td>
  <td>{n}</td>
  <td>{wr:.0f}%</td>
  <td>{pf_display}</td>
  <td style='color:{pnl_color}'>{pnl:+,.0f}</td>
</tr>""")

    rows_html = "\n".join(rows) if rows else "<tr><td colspan='8'>データなし</td></tr>"

    # ── 取引明細テーブル (直近 show_days の全取引) ─────────────
    all_trades: list[dict] = []
    for res in results:
        pr = res["period_results"].get(show_days, {})
        for t in pr.get("trade_log", []):
            t2 = dict(t)
            t2["symbol"] = res["symbol"]
            t2["name"]   = res.get("name", res["symbol"])
            t2["strategy"] = res.get("strategy", "?")
            all_trades.append(t2)
    all_trades.sort(key=lambda t: t["entry_dt"], reverse=True)

    detail_rows = []
    for t in all_trades[:200]:
        c = _pnl_color(t["pnl"])
        reason_color = ("#16a34a" if t["reason"] == "目標達成" else
                        "#dc2626" if t["reason"] == "損切り"  else "#6b7280")
        detail_rows.append(f"""
<tr>
  <td>{t['entry_dt'].strftime('%m/%d %H:%M')}</td>
  <td>{t['symbol'].replace('.T','')}</td>
  <td>{t.get('name','')}</td>
  <td>{t['entry_p']:,.0f}</td>
  <td>{t['exit_p']:,.0f}</td>
  <td style='color:{reason_color}'>{t['reason']}</td>
  <td style='color:{c}'>{t['pnl']:+,.0f}</td>
</tr>""")

    detail_html = "\n".join(detail_rows) if detail_rows else "<tr><td colspan='7'>取引なし</td></tr>"

    # ── 全体損益サマリー ────────────────────────────────────────
    total_pnl  = sum(t["pnl"] for t in all_trades)
    total_wins = sum(1 for t in all_trades if t["pnl"] > 0)
    total_n    = len(all_trades)
    total_wr   = total_wins / total_n * 100 if total_n > 0 else 0
    total_gp   = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
    total_gl   = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
    total_pf   = total_gp / total_gl if total_gl > 0 else float("inf")
    pnl_color  = _pnl_color(total_pnl)

    summary_html = f"""
<div style='background:#fff;border-radius:8px;padding:12px 16px;
            box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px;
            display:flex;gap:24px;flex-wrap:wrap;'>
  <div><span style='color:#64748b'>期間</span> <b>{show_days}日</b></div>
  <div><span style='color:#64748b'>取引数</span> <b>{total_n}</b></div>
  <div><span style='color:#64748b'>勝率</span> <b>{total_wr:.0f}%</b></div>
  <div><span style='color:#64748b'>PF</span> <b>{_pf_str(total_pf)}</b></div>
  <div><span style='color:#64748b'>総損益</span>
       <b style='color:{pnl_color}'>{total_pnl:+,.0f}円</b></div>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>デイトレ シグナル ({today_str})</title>
{style}
</head>
<body>
<h1>デイトレ シグナルレポート</h1>
<p class="subtitle">
  {today_str}  ／  モード: {mode_label}  ／
  スリッページ: {SLIPPAGE_STOP_PCT*100:.1f}%  ／
  手数料: 片道 {FEE_PCT_ONE_WAY*100:.2f}%
</p>

{summary_html}

<div class="section-title">銘柄別シグナル・BTスコア ({show_days}日)</div>
<table>
<thead>
<tr>
  <th>コード</th><th>銘柄</th><th>BTスコア</th>
  <th>発注価格 / 損切 / 目標</th>
  <th>取引</th><th>勝率</th><th>PF</th><th>損益</th>
</tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>

<div class="section-title">取引明細 (直近 {show_days}日 ・最新200件)</div>
<table>
<thead>
<tr>
  <th>エントリー日時</th><th>コード</th><th>銘柄</th>
  <th>エントリー</th><th>決済</th><th>理由</th><th>損益</th>
</tr>
</thead>
<tbody>
{detail_html}
</tbody>
</table>

<p style='color:#94a3b8;font-size:11px;margin-top:8px'>
  ※ バックテスト結果は過去の実績であり、将来の利益を保証するものではありません。<br>
  ※ 前日終値ブレイク戦略: 前日終値を逆指値トリガーとし、当日15:25で強制決済。
</p>
</body>
</html>
"""
    return html


# ── CLI ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="デイトレ シグナル判定・BTスコアレポート"
    )
    parser.add_argument("symbols", nargs="*",
                        help="銘柄コード (省略時は WATCHLIST)")
    parser.add_argument("--date",     help="シグナル確認日 (YYYY-MM-DD, 省略=今日)")
    parser.add_argument("--days",     type=int, default=90,
                        help="表示・BTスコア計算期間 (日, デフォルト90)")
    parser.add_argument("--source",   default="auto",
                        choices=["auto", "local", "yfinance"],
                        help="データソース")
    parser.add_argument("--workers",  type=int, default=WORKERS,
                        help="並列数")
    parser.add_argument("--no-browser", action="store_true",
                        help="HTMLを生成するがブラウザを開かない")
    parser.add_argument("--signal-only", action="store_true",
                        help="HTMLを生成せずシグナルのみ表示")
    parser.add_argument("--aggressive", action="store_true",
                        help="積極利確モード (tm=2.0)")
    args = parser.parse_args()

    # モード切替
    if args.aggressive:
        import os
        os.environ["TRADING_MODE"] = "aggressive"
        global STRATEGY_PARAMS, TRADING_MODE
        STRATEGY_PARAMS = STRATEGY_PARAMS_AGGRESSIVE
        TRADING_MODE    = "aggressive"

    target_date = args.date or None

    # 銘柄リスト
    if args.symbols:
        symbols = [(s if s.endswith(".T") else s + ".T", s) for s in args.symbols]
    else:
        symbols = WATCHLIST

    backtest_days = max(args.days, BACKTEST_DAYS)

    # データ取得 (バックテスト期間 + バッファ)
    sym_codes = [s for s, _ in symbols]
    print(f"データ取得中... ({len(sym_codes)}銘柄  source={args.source})")
    dfs = load_intraday_batch(sym_codes, days=backtest_days, source=args.source)

    # バックテスト実行
    strategies = list(STRATEGY_PARAMS.keys())
    tasks = []
    for sym, name in symbols:
        df = dfs.get(sym)
        if df is None or df.empty:
            print(f"  [skip] {sym}: データなし")
            continue
        for strat in strategies:
            tasks.append((sym, name, df, strat))

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(_run_one, sym, name, df, strat, backtest_days): (sym, strat)
            for sym, name, df, strat in tasks
        }
        for i, fut in enumerate(as_completed(futures), 1):
            sym, strat = futures[fut]
            try:
                res = fut.result()
                results.append(res)
            except Exception as e:
                print(f"  [warn] {sym} {strat}: {e}", file=sys.stderr)
            if i % 5 == 0 or i == len(tasks):
                print(f"  {i}/{len(tasks)} 完了", flush=True)

    results.sort(key=lambda r: r.get("bt_score", 0), reverse=True)

    # シグナル表示
    today_str = (datetime.now(JST).strftime("%Y-%m-%d")
                 if target_date is None else str(target_date))
    print(f"\n{'='*60}")
    print(f"  デイトレ シグナル  {today_str}  (戦略: PREV_CLOSE_BREAK)")
    print(f"{'='*60}")
    print(f"  {'コード':<10} {'BTスコア':>8}  発注価格    損切り    目標")
    print(f"  {'-'*55}")

    has_signal = False
    for res in results:
        sym   = res["symbol"]
        strat = res["strategy"]
        score = res.get("bt_score", 0)
        rank  = res.get("bt_rank", "-")
        sig   = check_signal_on_date(sym, strat, dfs.get(sym), target_date)
        if sig is None:
            continue
        has_signal = True
        op = sig["order_price"]
        sp = sig["stop_price"]
        tp = sig["target_price"]
        sl = sig["stop_loss_pct"]
        print(f"  {sym:<12} {score:>4} {rank:<4}  "
              f"{op:>8,.0f}  {sp:>8,.0f}  {tp:>8,.0f}  (損切{sl:.1f}%)")

    if not has_signal:
        print("  シグナルなし")

    if args.signal_only:
        return

    # HTML 生成
    suffix = "_aggressive" if TRADING_MODE == "aggressive" else ""
    html_path = Path(f"daytrade_signals{suffix}_{today_str}.html")
    html_content = build_html(
        results,
        show_days=args.days,
        date_label=today_str,
        target_date=target_date,
        dfs=dfs,
    )
    html_path.write_text(html_content, encoding="utf-8")
    print(f"\nHTMLレポート: {html_path}")

    if not args.no_browser:
        try:
            from _open_html import open_html
            open_html(html_path)
        except ImportError:
            webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
