"""
check_signals_budget.py  ―  予算別監視銘柄のシグナルチェック＆バックテスト
==========================================================================
build_watchlist_budget.py で選定した銘柄（50万円予算）を対象に
シグナル確認とバックテストを行う。check_signals.py とは独立。

【今日のシグナル確認】
  python check_signals_budget.py
  python check_signals_budget.py --date 2026-03-28
  python check_signals_budget.py --no-browser

【過去期間のシグナル＆バックテスト】
  python check_signals_budget.py --history --days 90
  python check_signals_budget.py --history --days 180

【バックテストのみ（成績確認）】
  python check_signals_budget.py --backtest --days 365
  python check_signals_budget.py --backtest --days 90
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest_strong_pullback as _sp

JST = timezone(timedelta(hours=9))
LOT = 100

# ── 監視リスト（50万円予算 / 4期間スコア35以上）─────────────────────────
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
body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;
     margin:0;padding:20px;font-size:14px}
h1{font-size:1.4em;border-bottom:1px solid #21262d;padding-bottom:10px;margin-bottom:16px}
h2{font-size:1.1em;color:#58a6ff;margin:24px 0 10px}
table{border-collapse:collapse;width:100%;margin-bottom:20px}
th{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;
   text-align:left;border-bottom:2px solid #21262d;font-size:.85em;
   position:sticky;top:0;z-index:1;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #21262d;white-space:nowrap}
tr:hover td{background:#161b22}
.num{text-align:right}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.sub{color:#8b949e;font-size:.78em;margin-top:1px}
details>summary{cursor:pointer;padding:10px 14px;background:#161b22;
                border-radius:6px;margin-bottom:4px;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{background:#1f2937}
a{color:#38bdf8;text-decoration:none}
a:hover{text-decoration:underline}
.signal-row{background:#0d2818;border-left:3px solid #3fb950}
"""


def _pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "∞"
    return f"{pf:.2f}"


def _trade_rows(trades: list[dict]) -> str:
    if not trades:
        return "<tr><td colspan='10' style='color:#4b5563;text-align:center'>トレードなし</td></tr>"
    rows = ""
    for t in sorted(trades, key=lambda x: x.get("exit_dt") or ""):
        pnl    = t["pnl"]
        cls    = "up" if pnl > 0 else "dn"
        entry_p = t.get("entry_p", 0) or t.get("limit_p", 0)
        stop_p  = t.get("stop_p",  0)
        target  = t.get("target_p", 0)
        rows += (
            f"<tr>"
            f"<td>{str(t.get('signal_dt',''))[:10]}</td>"
            f"<td>{str(t.get('entry_dt',''))[:10]}</td>"
            f"<td>{str(t.get('exit_dt', ''))[:10]}</td>"
            f"<td class='up num'>¥{entry_p:,.0f}</td>"
            f"<td class='dn num'>¥{stop_p:,.0f}</td>"
            f"<td class='up num'>¥{target:,.0f}</td>"
            f"<td class='num'>¥{t.get('exit_p',0):,.0f}</td>"
            f"<td class='{cls} num'>{pnl:+,.0f}円</td>"
            f"<td class='num'>{t.get('hold',0)}日</td>"
            f"<td>{t.get('reason','')}</td>"
            f"</tr>"
        )
    return rows


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wr=0.0, pf=0.0, total=0.0, avg=0.0, max_dd=0.0, max_cl=0)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    pf     = gw / gl if gl > 0 else float("inf")
    cum, peak, dd = 0.0, 0.0, 0.0
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


# ── シグナルチェック ───────────────────────────────────────────

def check_today(check_date: date | None, no_browser: bool) -> None:
    target = check_date or datetime.now(JST).date()
    today_str = target.strftime("%Y-%m-%d")
    now_str   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    print(f"\n強い押し目 シグナルチェック ({today_str})")
    print(f"対象: {len(WATCHLIST)}銘柄（50万円予算選定）\n")

    signal_rows = ""
    no_signal_rows = ""

    for sym, name in WATCHLIST:
        df = _sp.fetch(sym, 400)
        if df is None or df.empty:
            print(f"  {sym} データ取得失敗")
            continue
        df = _sp.calc(df)

        sig = _sp._has_signal_today(df)
        close = float(df.iloc[-1]["close"])
        last_date = df.index[-1].date()

        if sig and last_date == target:
            entry_p  = sig.get("limit_p",  0)
            stop_p   = sig.get("stop_p",   0)
            target_p = sig.get("target_p", 0)
            atr      = sig.get("atr",      0)
            print(f"  ★ {sym} {name}  終値¥{close:,.0f}  指値¥{entry_p:,.0f}  損切¥{stop_p:,.0f}  利確¥{target_p:,.0f}")
            signal_rows += f"""<tr class='signal-row'>
              <td>★</td>
              <td><a href='https://finance.yahoo.co.jp/quote/{sym}' target='_blank'>{sym}</a></td>
              <td>{name}</td>
              <td class='num'>¥{close:,.0f}</td>
              <td class='up num'>¥{entry_p:,.0f}</td>
              <td class='dn num'>¥{stop_p:,.0f}</td>
              <td class='up num'>¥{target_p:,.0f}</td>
              <td class='num neu'>¥{atr:,.0f}</td>
            </tr>"""
        else:
            no_signal_rows += f"""<tr>
              <td>—</td>
              <td><a href='https://finance.yahoo.co.jp/quote/{sym}' target='_blank'>{sym}</a></td>
              <td>{name}</td>
              <td class='num'>¥{close:,.0f}</td>
              <td colspan='4' class='neu' style='text-align:center'>シグナルなし</td>
            </tr>"""

    if not signal_rows:
        print("  シグナルなし\n")

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>シグナルチェック {today_str}</title>
<style>{_CSS}</style></head>
<body>
<h1>強い押し目 シグナルチェック ― 50万円予算選定銘柄
  <span style="font-size:.75em;color:#8b949e">{today_str} / {now_str}</span>
</h1>
<table>
<thead><tr>
  <th>状態</th><th>コード</th><th>銘柄名</th><th>終値</th>
  <th>指値（買い）</th><th>損切（逆指値）</th><th>利確目標</th><th>ATR</th>
</tr></thead>
<tbody>
{signal_rows}
{no_signal_rows}
</tbody>
</table>
</body></html>"""

    out = Path(f"signals_budget_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML: {out}")
    if not no_browser:
        webbrowser.open(out.resolve().as_uri())


# ── バックテスト ───────────────────────────────────────────────

def run_backtest(days: int, no_browser: bool) -> None:
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    today_s = datetime.now(JST).strftime("%Y%m%d")

    print(f"\n強い押し目 バックテスト（直近{days}日）")
    print(f"対象: {len(WATCHLIST)}銘柄（50万円予算選定）\n")

    all_results: list[dict] = []
    all_trades:  list[dict] = []

    header = f"  {'コード':<10} {'銘柄名':<22} {'取引':>4} {'勝率':>6} {'PF':>6} {'合計損益':>12} {'最大DD':>10}"
    print(header)
    print("  " + "─" * 75)

    for sym, name in WATCHLIST:
        df = _sp.fetch(sym, days + 400)
        if df is None or df.empty:
            print(f"  {sym:<10} {name:<22}  データ取得失敗")
            continue
        df_calc = _sp.calc(df)
        trades  = _sp.backtest_strong_pullback(df_calc, days)
        for t in trades:
            t["symbol"] = sym
        s = _stats(trades)
        all_trades.extend(trades)
        all_results.append(dict(sym=sym, name=name, trades=trades, stats=s))

        if s["n"] == 0:
            print(f"  {sym:<10} {name:<22}  取引なし")
            continue
        pf_s = _pf_str(s["pf"])
        tc   = "+" if s["total"] >= 0 else ""
        print(f"  {sym:<10} {name:<22}  {s['n']:>4}件  {s['wr']:>5.1f}%  {pf_s:>6}  "
              f"{tc}{s['total']:>+11,.0f}円  ▼{s['max_dd']:>9,.0f}円")

    print("  " + "─" * 75)
    tot = _stats(all_trades)
    if tot["n"] > 0:
        print(f"  {'合計':<33}  {tot['n']:>4}件  {tot['wr']:>5.1f}%  {_pf_str(tot['pf']):>6}  "
              f"{tot['total']:>+12,.0f}円  ▼{tot['max_dd']:>9,.0f}円")

    # ── HTML ──────────────────────────────────────────────────────
    active = [r for r in all_results if r["stats"]["n"] > 0]
    active.sort(key=lambda r: -r["stats"]["total"])
    no_trade = [r for r in all_results if r["stats"]["n"] == 0]

    sum_rows = ""
    for r in active:
        s   = r["stats"]
        tc  = "up" if s["total"] >= 0 else "dn"
        pf_s = _pf_str(s["pf"])
        sum_rows += (
            f"<tr>"
            f"<td>{r['sym']}</td><td>{r['name']}</td>"
            f"<td class='num'>{s['n']}</td>"
            f"<td class='num'>{s['wr']:.1f}%</td>"
            f"<td class='num'>{pf_s}</td>"
            f"<td class='{tc} num'>{s['total']:+,.0f}円</td>"
            f"<td class='num'>{s['avg']:+,.0f}円</td>"
            f"<td class='dn num'>▼{s['max_dd']:,.0f}円</td>"
            f"<td class='num'>{s['max_cl']}連敗</td>"
            f"</tr>"
        )
    tc2 = "up" if tot["total"] >= 0 else "dn"
    sum_rows += (
        f"<tr style='font-weight:600;border-top:2px solid #21262d'>"
        f"<td colspan='2'>合計（{len(active)}銘柄）</td>"
        f"<td class='num'>{tot['n']}</td>"
        f"<td class='num'>{tot['wr']:.1f}%</td>"
        f"<td class='num'>{_pf_str(tot['pf'])}</td>"
        f"<td class='{tc2} num'>{tot['total']:+,.0f}円</td>"
        f"<td class='num'>{tot['avg']:+,.0f}円</td>"
        f"<td class='dn num'>▼{tot['max_dd']:,.0f}円</td>"
        f"<td class='num'>{tot['max_cl']}連敗</td>"
        f"</tr>"
    )
    if no_trade:
        codes = " / ".join(r["sym"] for r in no_trade)
        sum_rows += f"<tr><td colspan='9' class='neu' style='font-size:.8em'>取引なし: {codes}</td></tr>"

    details_html = ""
    for r in active:
        s    = r["stats"]
        tc   = "up" if s["total"] >= 0 else "dn"
        rows = _trade_rows(r["trades"])
        details_html += f"""
<details>
  <summary>
    <b>{r['sym']}</b> {r['name']} &nbsp;|&nbsp;
    {s['n']}件 / 勝率{s['wr']:.0f}% / PF{_pf_str(s['pf'])} /
    <span class='{tc}'>{s['total']:+,.0f}円</span>
  </summary>
  <table style="margin-top:6px;font-size:.88em">
    <thead><tr>
      <th>シグナル日</th><th>約定日</th><th>決済日</th>
      <th>指値</th><th>損切</th><th>利確目標</th>
      <th>決済価格</th><th>損益</th><th>保有</th><th>理由</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</details>"""

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>バックテスト {days}日 ― 50万円予算選定</title>
<style>{_CSS}
details>summary{{cursor:pointer;padding:10px;background:#161b22;
                border-radius:6px;margin-bottom:4px;list-style:none}}
details>summary::-webkit-details-marker{{display:none}}
</style></head>
<body>
<h1>強い押し目 バックテスト ― 50万円予算選定銘柄
  <span style="font-size:.75em;color:#8b949e">
    直近{days}日 / {len(WATCHLIST)}銘柄 / {now_str}
  </span>
</h1>
<h2>銘柄別サマリー</h2>
<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>取引数</th><th>勝率</th><th>PF</th>
  <th>合計損益</th><th>平均損益</th><th>最大DD</th><th>最大連敗</th>
</tr></thead>
<tbody>{sum_rows}</tbody>
</table>
<h2>銘柄別トレード詳細（クリックで展開）</h2>
{details_html}
</body></html>"""

    out = Path(f"backtest_budget_{days}d_{today_s}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out}")
    if not no_browser:
        webbrowser.open(out.resolve().as_uri())


# ── 履歴モード ────────────────────────────────────────────────

def check_history(days: int, no_browser: bool) -> None:
    today    = datetime.now(JST).date()
    from_date = today - timedelta(days=days)
    run_backtest(days, no_browser)


# ── メイン ────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="50万円予算選定銘柄 シグナル＆バックテスト")
    parser.add_argument("--date",       type=str,  help="シグナル確認日（YYYY-MM-DD）")
    parser.add_argument("--days",       type=int,  default=365, help="バックテスト日数（デフォルト365）")
    parser.add_argument("--backtest",   action="store_true", help="バックテストモード")
    parser.add_argument("--history",    action="store_true", help="--backtest と同じ（互換）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = parser.parse_args()

    if args.backtest or args.history:
        run_backtest(args.days, args.no_browser)
    else:
        d = date.fromisoformat(args.date) if args.date else None
        check_today(d, args.no_browser)


if __name__ == "__main__":
    main()
