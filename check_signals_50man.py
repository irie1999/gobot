"""
check_signals_50man.py  ―  50万円予算選定銘柄のシグナルチェック＆バックテスト
==========================================================================
build_watchlist_budget.py で選定した15銘柄（50万円予算・スコア35以上）を対象に
check_signals.py と同じ形式でシグナル確認とバックテストを行う。

【今日のシグナル確認】
  python check_signals_50man.py
  python check_signals_50man.py --date 2026-03-28
  python check_signals_50man.py --no-browser

【バックテスト（成績確認）】
  python check_signals_50man.py --backtest --days 365
  python check_signals_50man.py --backtest --days 90
"""

from __future__ import annotations

import argparse
import math
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest_strong_pullback as _sp

JST = timezone(timedelta(hours=9))
LOT = 100

# ── 監視リスト（50万円予算 / 4期間スコア35以上 / 15銘柄）────────────────
WATCHLIST: list[tuple[str, str]] = [
    # score45
    ("3636.T", "三菱総合研究所"),
    ("5108.T", "ブリヂストン"),
    ("6183.T", "ベルシステム２４ホールディングス"),
    ("7012.T", "川崎重工業"),
    ("8153.T", "モスフードサービス"),
    ("8218.T", "コメリ"),
    ("9046.T", "神戸電鉄"),
    # score43
    ("9405.T", "朝日放送グループホールディングス"),
    # score40
    ("3946.T", "トーモク"),
    ("4025.T", "多木化学"),
    # score39
    ("6196.T", "ストライク"),
    ("6454.T", "マックス"),
    # score38
    ("8081.T", "カナデン"),
    ("8194.T", "ライフコーポレーション"),
    # score35
    ("9381.T", "エーアイテイー"),
]

_CSS = """
body{font-family:'Meiryo',sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:20px}
h1{font-size:1.2em;color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:16px}
h2{font-size:1.0em;color:#8b949e;margin:24px 0 8px}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:.75em;font-weight:bold;border:1px solid}
table{border-collapse:collapse;width:100%;font-size:.82em;margin-top:4px}
th{background:#161b22;color:#8b949e;padding:7px 12px;border:1px solid #30363d;text-align:right;white-space:nowrap}
th:first-child,th:nth-child(2),th:nth-child(3){text-align:left}
td{padding:6px 12px;border:1px solid #21262d;text-align:right;vertical-align:top}
td:first-child,td:nth-child(2),td:nth-child(3){text-align:left}
tr:nth-child(even){background:#0d1117}
tr:nth-child(odd){background:#161b22}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.note{font-size:.78em;color:#8b949e;margin-top:2px}
.waiting td{color:#4b5563}
details>summary{cursor:pointer;padding:10px 14px;background:#161b22;
                border-radius:6px;margin-bottom:4px;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{background:#1f2937}
"""


def _rr_float(entry: float, stop: float, target: float) -> float | None:
    risk = entry - stop
    if risk <= 0:
        return None
    return (target - entry) / risk


def _rr(entry: float, stop: float, target: float) -> str:
    v = _rr_float(entry, stop, target)
    return f"{v:.1f}R" if v is not None else "—"


def _pct(a: float, b: float) -> str:
    return f"{(a - b) / b * 100:+.2f}%" if b else "—"


def _rr_color(v: float | None) -> str:
    if v is None:
        return "neu"
    return "up" if v >= 2.0 else ("dn" if v < 1.0 else "neu")


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _fetch(symbol: str, fetch_days: int, target_date: date | None) -> pd.DataFrame | None:
    try:
        df = _sp.fetch(symbol, fetch_days)
        if df is None or len(df) == 0:
            return None
        df = _sp.calc(df)
        if target_date is not None:
            df = df.loc[df.index <= pd.Timestamp(target_date)]
            if len(df) == 0:
                return None
        return df
    except Exception as e:
        print(f"  ※ {symbol} エラー: {e}", file=sys.stderr)
    return None


# ── シグナル収集 ──────────────────────────────────────────────

def _collect_signals(target_date: date | None) -> tuple[list, list]:
    today      = datetime.now(JST).date()
    ref        = target_date or today
    fetch_days = (today - ref).days + 365 + 60

    active:  list[tuple[str, str, dict]] = []
    waiting: list[tuple[str, str]]       = []

    for symbol, name in WATCHLIST:
        df = _fetch(symbol, fetch_days, target_date)
        if df is None:
            waiting.append((symbol, name))
            continue
        sig = _sp._has_signal_today(df)
        if sig:
            entry_p  = sig.get("limit_p",  0) or sig.get("limit_price", 0)
            stop_p   = sig.get("stop_p",   0) or sig.get("stop_loss",   0)
            target_p = sig.get("target_p", 0) or sig.get("profit_target", 0)
            atr      = sig.get("atr",  0)
            rsi5     = sig.get("rsi5", 0)
            close    = sig.get("close", float(df.iloc[-1]["close"]))
            active.append((symbol, name, dict(
                close=close, entry=entry_p, stop=stop_p,
                target=target_p, atr=atr,
                note=f"RSI5={rsi5:.1f}", expire=3,
            )))
        else:
            waiting.append((symbol, name))

    return active, waiting


# ── シグナルHTML ──────────────────────────────────────────────

def build_signal_html(active: list, waiting: list, target_date: date | None) -> str:
    ref_label = str(target_date) if target_date else datetime.now(JST).strftime("%Y-%m-%d")
    now_str   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    if active:
        rows = ""
        for sym, nm, r in active:
            risk   = r["entry"] - r["stop"]
            rr_val = _rr_float(r["entry"], r["stop"], r["target"])
            rr_cls = _rr_color(rr_val)
            rr_str = f"{rr_val:.1f}R" if rr_val is not None else "—"
            rows += f"""
<tr>
  <td>{sym}</td>
  <td>{nm}</td>
  <td><span class="badge" style="color:#4ade80;border-color:#4ade80">強い押し目</span></td>
  <td>¥{r['close']:,.0f}</td>
  <td>¥{r['entry']:,.0f}<div class="note">{_pct(r['entry'], r['close'])}</div></td>
  <td class="dn">¥{r['stop']:,.0f}<div class="note">リスク ¥{risk:,.0f} / ¥{risk*LOT:,.0f}</div></td>
  <td class="up">¥{r['target']:,.0f}</td>
  <td class="{rr_cls}">{rr_str}</td>
  <td>{r['expire']}営業日</td>
  <td class="note" style="text-align:left">{r['note']}</td>
</tr>"""
        signal_block = f"""
<h2>▶ シグナル発生 {len(active)}件</h2>
<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>戦略</th><th>現在値</th>
  <th>指値（エントリー）</th><th>損切</th><th>目標</th><th>RR</th>
  <th>有効期限</th><th>指標メモ</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
    else:
        signal_block = "<h2>▶ シグナル発生なし</h2><p style='color:#4b5563;padding:8px 0'>条件を満たす銘柄はありませんでした。</p>"

    wait_rows = "".join(
        f"<tr class='waiting'><td>{sym}</td><td>{nm}</td></tr>"
        for sym, nm in waiting
    )
    wait_block = f"""
<h2>▷ 待機中（シグナルなし） {len(waiting)}銘柄</h2>
<table style="width:auto">
<thead><tr><th>コード</th><th>銘柄名</th></tr></thead>
<tbody>{wait_rows}</tbody>
</table>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>シグナルチェック {ref_label} ― 50万円予算</title>
<style>{_CSS}</style>
</head>
<body>
<h1>監視銘柄シグナルチェック
  <span style="font-size:.8em;color:#8b949e">
    判定日: {ref_label} &nbsp;|&nbsp; プリセット: 50万円予算 &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>
<p style="font-size:.8em;color:#4b5563;margin-bottom:16px">
  ※ 指値注文は翌営業日の寄り付き前に設定 &nbsp;/&nbsp;
  損切は同時に逆指値注文（OCO推奨）&nbsp;/&nbsp;
  有効期限内に未約定はキャンセル
</p>
{signal_block}
{wait_block}
</body>
</html>"""


# ── バックテスト ──────────────────────────────────────────────

def _calc_stats(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wr=0.0, pf=0.0, total=0.0, avg=0.0, max_dd=0.0, max_cl=0)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    pf     = gw / gl if gl > 0 else float("inf")
    cum = peak = dd = 0.0
    max_c = cur_c = 0
    for t in sorted(trades, key=lambda x: x.get("exit_dt") or ""):
        cum  += t["pnl"]
        peak  = max(peak, cum)
        dd    = max(dd, peak - cum)
        if t["pnl"] <= 0:
            cur_c += 1; max_c = max(max_c, cur_c)
        else:
            cur_c = 0
    return dict(
        n=len(trades), wr=len(wins)/len(trades)*100,
        pf=pf, total=sum(t["pnl"] for t in trades),
        avg=sum(t["pnl"] for t in trades)/len(trades),
        max_dd=dd, max_cl=max_c,
    )


def _trade_rows(trades: list[dict]) -> str:
    if not trades:
        return "<tr><td colspan='11' style='color:#4b5563;text-align:center'>トレードなし</td></tr>"
    rows = ""
    for t in sorted(trades, key=lambda x: x.get("exit_dt") or ""):
        pnl     = t["pnl"]
        cls     = "up" if pnl > 0 else "dn"
        entry_p = t.get("entry_p", 0) or t.get("limit_p", 0)
        stop_p  = t.get("stop_p",  0)
        target  = t.get("target_p", 0)
        rr_val  = _rr_float(entry_p, stop_p, target)
        rr_str  = f"{rr_val:.1f}R" if rr_val else "—"
        rows += (
            f"<tr>"
            f"<td>{str(t.get('signal_dt',''))[:10]}</td>"
            f"<td>{str(t.get('entry_dt',''))[:10]}</td>"
            f"<td>{str(t.get('exit_dt', ''))[:10]}</td>"
            f"<td class='up'>¥{entry_p:,.0f}</td>"
            f"<td class='dn'>¥{stop_p:,.0f}</td>"
            f"<td class='up'>¥{target:,.0f}</td>"
            f"<td class='neu'>{rr_str}</td>"
            f"<td>¥{t.get('exit_p',0):,.0f}</td>"
            f"<td class='{cls}'>{pnl:+,.0f}円</td>"
            f"<td>{t.get('hold',0)}日</td>"
            f"<td>{t.get('reason','')}</td>"
            f"</tr>"
        )
    return rows


def build_backtest_html(all_results: list[dict], days: int) -> str:
    now_str  = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    active   = sorted([r for r in all_results if r["stats"]["n"] > 0],
                      key=lambda r: -r["stats"]["total"])
    no_trade = [r for r in all_results if r["stats"]["n"] == 0]
    all_trades = [t for r in active for t in r["trades"]]
    tot = _calc_stats(all_trades)

    sum_rows = ""
    for r in active:
        s  = r["stats"]
        tc = "up" if s["total"] >= 0 else "dn"
        sum_rows += (
            f"<tr>"
            f"<td>{r['sym']}</td><td>{r['name']}</td>"
            f"<td>{s['n']}</td><td>{s['wr']:.1f}%</td>"
            f"<td>{_pf_str(s['pf'])}</td>"
            f"<td class='{tc}'>{s['total']:+,.0f}円</td>"
            f"<td>{s['avg']:+,.0f}円</td>"
            f"<td class='dn'>▼{s['max_dd']:,.0f}円</td>"
            f"<td>{s['max_cl']}連敗</td>"
            f"</tr>"
        )
    tc2 = "up" if tot["total"] >= 0 else "dn"
    sum_rows += (
        f"<tr style='font-weight:600;border-top:2px solid #30363d'>"
        f"<td colspan='2'>合計（{len(active)}銘柄）</td>"
        f"<td>{tot['n']}</td><td>{tot['wr']:.1f}%</td>"
        f"<td>{_pf_str(tot['pf'])}</td>"
        f"<td class='{tc2}'>{tot['total']:+,.0f}円</td>"
        f"<td>{tot['avg']:+,.0f}円</td>"
        f"<td class='dn'>▼{tot['max_dd']:,.0f}円</td>"
        f"<td>{tot['max_cl']}連敗</td>"
        f"</tr>"
    )
    if no_trade:
        codes = " / ".join(r["sym"] for r in no_trade)
        sum_rows += f"<tr><td colspan='9' class='neu' style='font-size:.8em;text-align:left'>取引なし: {codes}</td></tr>"

    details_html = ""
    for r in active:
        s  = r["stats"]
        tc = "up" if s["total"] >= 0 else "dn"
        details_html += f"""
<details>
  <summary>
    <b>{r['sym']}</b> {r['name']} &nbsp;|&nbsp;
    {s['n']}件 / 勝率{s['wr']:.0f}% / PF{_pf_str(s['pf'])} /
    <span class='{tc}'>{s['total']:+,.0f}円</span>
  </summary>
  <table style="margin-top:6px;font-size:.85em">
    <thead><tr>
      <th style="text-align:left">シグナル日</th>
      <th style="text-align:left">約定日</th>
      <th style="text-align:left">決済日</th>
      <th>指値</th><th>損切</th><th>利確目標</th><th>RR</th>
      <th>決済価格</th><th>損益</th><th>保有</th>
      <th style="text-align:left">理由</th>
    </tr></thead>
    <tbody>{_trade_rows(r['trades'])}</tbody>
  </table>
</details>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>バックテスト {days}日 ― 50万円予算</title>
<style>{_CSS}</style>
</head>
<body>
<h1>強い押し目 バックテスト
  <span style="font-size:.8em;color:#8b949e">
    直近{days}日 &nbsp;|&nbsp; 50万円予算選定 {len(WATCHLIST)}銘柄 &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>
<p style="font-size:.8em;color:#4b5563;margin-bottom:16px">
  ※ ロット100株固定 / 合計損益の高い順に表示
</p>
<h2>▶ 銘柄別サマリー</h2>
<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>取引数</th><th>勝率</th><th>PF</th>
  <th>合計損益</th><th>平均損益</th><th>最大DD</th><th>最大連敗</th>
</tr></thead>
<tbody>{sum_rows}</tbody>
</table>
<h2>▶ 銘柄別トレード詳細（クリックで展開）</h2>
{details_html}
</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────

def check_today_main(target_date: date | None, no_browser: bool) -> None:
    ref = target_date or datetime.now(JST).date()
    print(f"\n{'='*65}")
    print(f"  シグナルチェック（50万円予算）  判定日: {ref}  ({len(WATCHLIST)}銘柄)")
    print(f"{'='*65}\n")

    active, waiting = _collect_signals(target_date)

    if active:
        print(f"【シグナル発生】 {len(active)}件")
        print("─" * 65)
        for sym, nm, r in active:
            risk = r["entry"] - r["stop"]
            print(f"  ★ {sym}  {nm}")
            print(f"     終値   : ¥{r['close']:,.0f}")
            print(f"     指値   : ¥{r['entry']:,.0f}  ({_pct(r['entry'], r['close'])})")
            print(f"     損切   : ¥{r['stop']:,.0f}  (リスク ¥{risk:,.0f} / ロット ¥{risk*LOT:,.0f})")
            print(f"     目標   : ¥{r['target']:,.0f}  RR={_rr(r['entry'], r['stop'], r['target'])}")
            print(f"     {r['note']}")
            print()
    else:
        print("【シグナル発生なし】\n")
    print("【待機中】 " + "  ".join(s for s, _ in waiting))

    html    = build_signal_html(active, waiting, target_date)
    out     = Path(f"signals_50man_{ref}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out}")
    if not no_browser:
        webbrowser.open(out.resolve().as_uri())


def run_backtest(days: int, no_browser: bool) -> None:
    today_s = datetime.now(JST).strftime("%Y%m%d")
    print(f"\n{'='*65}")
    print(f"  バックテスト（50万円予算）  直近{days}日  ({len(WATCHLIST)}銘柄)")
    print(f"{'='*65}\n")

    all_results: list[dict] = []
    all_trades:  list[dict] = []

    print(f"  {'コード':<10} {'銘柄名':<24} {'取引':>4} {'勝率':>6} {'PF':>6} {'合計損益':>12} {'最大DD':>10}")
    print("  " + "─" * 75)

    for sym, name in WATCHLIST:
        df = _sp.fetch(sym, days + 400)
        if df is None or df.empty:
            print(f"  {sym:<10} {name:<24}  データ取得失敗")
            continue
        trades = _sp.backtest_strong_pullback(_sp.calc(df), days)
        for t in trades:
            t["symbol"] = sym
        s = _calc_stats(trades)
        all_trades.extend(trades)
        all_results.append(dict(sym=sym, name=name, trades=trades, stats=s))

        if s["n"] == 0:
            print(f"  {sym:<10} {name:<24}  取引なし")
            continue
        print(f"  {sym:<10} {name:<24}  {s['n']:>4}件  {s['wr']:>5.1f}%  {_pf_str(s['pf']):>6}  "
              f"{s['total']:>+12,.0f}円  ▼{s['max_dd']:>9,.0f}円")

    print("  " + "─" * 75)
    tot = _calc_stats(all_trades)
    if tot["n"] > 0:
        active_n = sum(1 for r in all_results if r["stats"]["n"] > 0)
        print(f"  {'合計（'+str(active_n)+'銘柄）':<35}  {tot['n']:>4}件  {tot['wr']:>5.1f}%  "
              f"{_pf_str(tot['pf']):>6}  {tot['total']:>+12,.0f}円  ▼{tot['max_dd']:>9,.0f}円")

    html = build_backtest_html(all_results, days)
    out  = Path(f"backtest_50man_{days}d_{today_s}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out}")
    if not no_browser:
        webbrowser.open(out.resolve().as_uri())


def main() -> None:
    parser = argparse.ArgumentParser(description="50万円予算選定銘柄 シグナル＆バックテスト")
    parser.add_argument("--date",       type=str,  help="シグナル確認日（YYYY-MM-DD）")
    parser.add_argument("--days",       type=int,  default=365, help="バックテスト日数（デフォルト365）")
    parser.add_argument("--backtest",   action="store_true", help="バックテストモード")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = parser.parse_args()

    if args.backtest:
        run_backtest(args.days, args.no_browser)
    else:
        d = date.fromisoformat(args.date) if args.date else None
        check_today_main(d, args.no_browser)


if __name__ == "__main__":
    main()
