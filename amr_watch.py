"""
amr_watch.py  ―  アダプティブMR専用 シグナル＆バックテスト
==============================================================
監視22銘柄をすべてアダプティブMR戦略でスキャンし、
シグナル確認とバックテスト損益をHTML出力する。

使い方:
  python amr_watch.py                        # 今日のシグナル
  python amr_watch.py --date 2026-03-28      # 指定日のシグナル
  python amr_watch.py --backtest             # バックテスト損益（365日）
  python amr_watch.py --backtest --days 180  # バックテスト期間変更
  python amr_watch.py --no-browser           # ブラウザ起動しない
"""

from __future__ import annotations

import argparse
import math
import webbrowser
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest_adaptive_mr as _amr

JST = timezone(timedelta(hours=9))
LOT = 100

# ── 監視銘柄（全22銘柄・全てアダプティブMR）────────────────────────
STOCKS: list[tuple[str, str]] = [
    ("7012.T", "川崎重工業"),
    ("7013.T", "IHI"),
    ("5981.T", "東京製綱"),
    ("9044.T", "南海電鉄"),
    ("4118.T", "カネカ"),
    ("7952.T", "河合楽器"),
    ("8795.T", "T&Dホールディングス"),
    ("9042.T", "阪急阪神HD"),
    ("5702.T", "大紀アルミニウム工業所"),
    ("9503.T", "関西電力"),
    ("5333.T", "日本碍子"),
    ("1605.T", "INPEX"),
    ("6361.T", "荏原製作所"),
    ("8802.T", "三菱地所"),
    ("2809.T", "キユーピー"),
    ("6058.T", "ベクトル"),
    ("5844.T", "京都フィナンシャルグループ"),
    ("6963.T", "ローム"),
    ("7389.T", "あいちフィナンシャルグループ"),
    ("5741.T", "UACJ"),
    ("9742.T", "アイネス"),
    ("6282.T", "オイレス工業"),
]

# ─────────────────────────────────────────────────────────────────────
# 共通CSS
# ─────────────────────────────────────────────────────────────────────
_CSS = """
body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;
     margin:0;padding:20px;font-size:14px}
h1{font-size:1.4em;border-bottom:1px solid #21262d;padding-bottom:10px;margin-bottom:20px}
h2{font-size:1.1em;color:#58a6ff;margin:24px 0 10px}
table{border-collapse:collapse;width:100%;margin-bottom:24px}
th{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;
   text-align:left;border-bottom:2px solid #21262d;font-size:.85em}
td{padding:7px 10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:#161b22}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.sub{color:#8b949e;font-size:.82em;margin-top:2px}
.badge{border:1px solid #38bdf8;color:#38bdf8;border-radius:4px;
       padding:1px 7px;font-size:.8em}
.signal-card{background:#161b22;border:1px solid #21262d;border-radius:8px;
             padding:16px 20px;margin-bottom:14px}
.signal-card h3{margin:0 0 10px;color:#38bdf8;font-size:1em}
.kv{display:grid;grid-template-columns:100px 1fr;gap:4px 8px;font-size:.9em}
.kv .k{color:#8b949e}
.waiting td{color:#4b5563}
details>summary{cursor:pointer;padding:10px;background:#161b22;
                border-radius:6px;margin-bottom:4px;list-style:none}
details>summary::-webkit-details-marker{display:none}
"""

# ─────────────────────────────────────────────────────────────────────
# シグナル収集
# ─────────────────────────────────────────────────────────────────────

def _fetch_df(symbol: str, fetch_days: int,
              target_date: date | None = None) -> pd.DataFrame | None:
    try:
        df = _amr.fetch(symbol, fetch_days)
        if df is None:
            return None
        df = _amr.calc(df)
        if target_date is not None:
            ts = pd.Timestamp(target_date)
            df = df[df.index <= ts]
        return df
    except Exception:
        return None


def _collect_signals(
    target_date: date | None,
) -> tuple[list[tuple[str, str, dict]], list[tuple[str, str]]]:
    fetch_days = 600
    active: list[tuple[str, str, dict]] = []
    waiting: list[tuple[str, str]]      = []

    def _check(sym: str, nm: str):
        df = _fetch_df(sym, fetch_days, target_date)
        if df is None or len(df) < 2:
            return sym, nm, None
        sig = _amr._has_signal_today(df)
        return sym, nm, sig

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_check, s, n): (s, n) for s, n in STOCKS}
        for f in as_completed(futs):
            sym, nm, sig = f.result()
            if sig:
                active.append((sym, nm, sig))
            else:
                waiting.append((sym, nm))

    active.sort(key=lambda x: x[0])
    waiting.sort(key=lambda x: x[0])
    return active, waiting


# ─────────────────────────────────────────────────────────────────────
# シグナルHTML
# ─────────────────────────────────────────────────────────────────────

def _pct(a: float, b: float) -> str:
    return f"{(a - b) / b * 100:+.2f}%" if b else "—"


def _rr(entry: float, stop: float, target: float) -> str:
    risk = entry - stop
    if risk <= 0:
        return "—"
    return f"{(target - entry) / risk:.1f}R"


def build_signal_html(
    active: list, waiting: list,
    target_date: date | None,
) -> str:
    now_str   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    ref       = target_date or datetime.now(JST).date()
    ref_label = str(ref)

    # シグナルカード
    cards = ""
    for sym, nm, r in active:
        close  = r["close"]
        entry  = r["limit_price"]
        stop   = r["stop_loss"]
        target = entry + r["atr"] * _amr.TARGET_ATR_MULT
        risk   = entry - stop
        regime_label = {"low": "低ボラ", "normal": "通常", "high": "高ボラ"}.get(r["regime"], r["regime"])
        trigger = []
        if r.get("sig_rsi2"):
            trigger.append(f"RSI(2)={r['rsi2']:.1f}")
        if r.get("sig_ibs"):
            trigger.append(f"IBS={r['ibs']:.2f}")

        cards += f"""
<div class="signal-card">
  <h3>★ {sym}　{nm}</h3>
  <div class="kv">
    <span class="k">現在値</span>   <span>¥{close:,.0f}</span>
    <span class="k">指値</span>     <span class="up">¥{entry:,.0f} <span class="sub">({_pct(entry, close)})</span></span>
    <span class="k">損切</span>     <span class="dn">¥{stop:,.0f} <span class="sub">リスク¥{risk:,.0f} / {risk*LOT:,.0f}円</span></span>
    <span class="k">利確目標</span> <span class="up">¥{target:,.0f} <span class="sub">RR={_rr(entry, stop, target)} / ATR×{_amr.TARGET_ATR_MULT}</span></span>
    <span class="k">有効期限</span> <span>{r['expire_days']}営業日</span>
    <span class="k">レジーム</span> <span>{regime_label}（ATRパーセンタイル {r.get('atr_pct', float('nan')):.0f}%）</span>
    <span class="k">トリガー</span> <span>{"　".join(trigger)}</span>
  </div>
</div>"""

    if not cards:
        cards = "<p style='color:#8b949e'>シグナル発生なし</p>"

    wait_rows = "".join(
        f"<tr class='waiting'><td>{s}</td><td>{n}</td></tr>"
        for s, n in waiting
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>AMR シグナル {ref_label}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>アダプティブMR シグナルチェック
  <span style="font-size:.75em;color:#8b949e">
    判定日: {ref_label} &nbsp;|&nbsp; {len(STOCKS)}銘柄 &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>
<p style="font-size:.85em;color:#4b5563;margin-bottom:16px">
  ※ 指値・損切逆指値・利確指値をIFD-OCO注文で翌営業日の寄り付き前に設定
</p>
<h2>▶ シグナル発生 {len(active)}件</h2>
{cards}
<h2>▷ 待機中 {len(waiting)}銘柄</h2>
<table>
<thead><tr><th>コード</th><th>銘柄名</th></tr></thead>
<tbody>{wait_rows}</tbody>
</table>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# バックテスト
# ─────────────────────────────────────────────────────────────────────

def _run_backtest(symbol: str, days: int) -> list[dict]:
    try:
        df = _amr.fetch(symbol, days)
        if df is None:
            return []
        df = _amr.calc(df)
        return _amr.backtest_amr(df, days)
    except Exception:
        return []


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wins=0, wr=0.0, pf=0.0, total=0.0,
                    avg=0.0, max_win=0.0, max_loss=0.0,
                    max_dd=0.0, max_consec_loss=0)
    sorted_t   = sorted(trades, key=lambda t: t.get("exit_dt") or "")
    pnls       = [t["pnl"] for t in sorted_t]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]
    gross_w    = sum(wins)
    gross_l    = abs(sum(losses))
    pf         = gross_w / gross_l if gross_l > 0 else float("inf")

    # 最大ドローダウン
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # 最大連敗数
    max_consec = cur_consec = 0
    for p in pnls:
        if p <= 0:
            cur_consec += 1
            max_consec  = max(max_consec, cur_consec)
        else:
            cur_consec  = 0

    return dict(
        n              = len(pnls),
        wins           = len(wins),
        wr             = len(wins) / len(pnls) * 100,
        pf             = pf,
        total          = sum(pnls),
        avg            = sum(pnls) / len(pnls),
        max_win        = max(pnls),
        max_loss       = min(pnls),
        max_dd         = max_dd,
        max_consec_loss= max_consec,
    )


def _valid(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0


def _trade_rows(trades: list[dict]) -> str:
    if not trades:
        return "<tr><td colspan='12' style='color:#4b5563;text-align:center'>トレードなし</td></tr>"
    rows = ""
    for t in sorted(trades, key=lambda t: t.get("exit_dt") or ""):
        pnl     = t["pnl"]
        cls     = "up" if pnl > 0 else "dn"
        entry_d = str(t.get("entry_dt", ""))[:10]
        exit_d  = str(t.get("exit_dt",  ""))[:10]
        sig_d   = str(t.get("signal_dt", ""))[:10]
        sig_cl  = t.get("signal_close", 0) or 0
        entry_p = t.get("entry_p", 0)
        exit_p  = t.get("exit_p",  0)
        stop_p  = t.get("stop_p",  0)
        target  = t.get("target_p", 0)
        atr     = t.get("atr",     0)
        hold    = t.get("hold",    "—")
        reason  = t.get("reason",  "—")

        risk    = entry_p - stop_p if _valid(stop_p) else 0
        rr_val  = (target - entry_p) / risk if (risk > 0 and _valid(target)) else None
        rr_s    = f"{rr_val:.1f}R" if rr_val else "—"
        target_s = f"¥{target:,.0f}" if _valid(target) else "—"
        stop_s   = f"¥{stop_p:,.0f}" if _valid(stop_p) else "—"
        sig_cl_s = f"¥{sig_cl:,.0f}" if sig_cl else "—"

        rows += (
            f"<tr>"
            f"<td>{sig_d}<div class='sub'>{sig_cl_s}</div></td>"
            f"<td>{entry_d}</td>"
            f"<td>{exit_d}</td>"
            f"<td class='up'>¥{entry_p:,.0f}<div class='sub'>指値</div></td>"
            f"<td class='dn'>{stop_s}<div class='sub'>{'リスク¥' + f'{risk:,.0f}' if risk else ''}</div></td>"
            f"<td class='up'>{target_s}<div class='sub'>{rr_s}</div></td>"
            f"<td>¥{exit_p:,.0f}</td>"
            f"<td class='{cls}'>{pnl:+,.0f}円</td>"
            f"<td>{hold}日</td>"
            f"<td class='neu' style='font-size:.78em'>¥{atr:,.0f}</td>"
            f"<td>{reason}</td>"
            f"</tr>"
        )
    return rows


def _equity_svg(all_trades: list[dict]) -> str:
    sorted_t = sorted(all_trades, key=lambda t: t.get("exit_dt") or "")
    cum = 0.0
    pts = [(0, 0.0)]
    for t in sorted_t:
        cum += t["pnl"]
        pts.append((len(pts), cum))

    if len(pts) < 2:
        return "<p style='color:#4b5563'>データ不足</p>"

    W, H, PAD = 900, 200, 30
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mn, mx = min(ys), max(ys)
    rng = mx - mn or 1

    def px(i): return PAD + (i / max(xs)) * (W - 2 * PAD)
    def py(v): return H - PAD - ((v - mn) / rng) * (H - 2 * PAD)

    coords = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
    zero_y = py(0)
    color  = "#3fb950" if cum >= 0 else "#f85149"

    return (
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px">'
        f'<line x1="{PAD}" y1="{zero_y:.1f}" x2="{W-PAD}" y2="{zero_y:.1f}" '
        f'stroke="#21262d" stroke-width="1"/>'
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>'
        f'<text x="{PAD}" y="{H-5}" fill="#8b949e" font-size="11">0円</text>'
        f'<text x="{W-PAD}" y="20" fill="{color}" font-size="11" text-anchor="end">'
        f'{cum:+,.0f}円</text>'
        f'</svg>'
    )


def build_backtest_html(
    results: list[dict], all_trades: list[dict], days: int,
) -> str:
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    total_s = _stats(all_trades)

    def pf_str(pf): return "∞" if pf == float("inf") else f"{pf:.2f}"

    # サマリーテーブル
    rows = ""
    for r in sorted(results, key=lambda x: (-x["stats"]["total"], x["symbol"])):
        s       = r["stats"]
        cls     = "up" if s["total"] > 0 else ("dn" if s["total"] < 0 else "neu")
        pf_val  = 0 if s["pf"] == float("inf") else s["pf"]
        avg_cls = "up" if s["avg"] > 0 else "dn"
        rows += (
            f"<tr>"
            f"<td>{r['symbol']}</td>"
            f"<td>{r['name']}</td>"
            f"<td data-v='{s['n']}'>{s['n']}</td>"
            f"<td>{s['wins']}</td>"
            f"<td data-v='{s['wr']:.1f}'>{s['wr']:.1f}%</td>"
            f"<td data-v='{pf_val:.2f}'>{pf_str(s['pf'])}</td>"
            f"<td data-v='{s['total']:.0f}' class='{cls}'>{s['total']:+,.0f}円</td>"
            f"<td class='{avg_cls}'>{s['avg']:+,.0f}円</td>"
            f"<td class='up'>{s['max_win']:+,.0f}円</td>"
            f"<td class='dn'>{s['max_loss']:+,.0f}円</td>"
            f"<td class='dn' data-v='{s['max_dd']:.0f}'>▼{s['max_dd']:,.0f}円</td>"
            f"<td data-v='{s['max_consec_loss']}'>{s['max_consec_loss']}連敗</td>"
            f"</tr>"
        )

    total_cls = "up" if total_s["total"] > 0 else ("dn" if total_s["total"] < 0 else "neu")
    rows += (
        f"<tr style='font-weight:600;border-top:2px solid #21262d'>"
        f"<td colspan='2'>合計</td>"
        f"<td>{total_s['n']}</td>"
        f"<td>{total_s['wins']}</td>"
        f"<td>{total_s['wr']:.1f}%</td>"
        f"<td>{pf_str(total_s['pf'])}</td>"
        f"<td class='{total_cls}'>{total_s['total']:+,.0f}円</td>"
        f"<td class='{'up' if total_s['avg']>0 else 'dn'}'>{total_s['avg']:+,.0f}円</td>"
        f"<td class='up'>{total_s['max_win']:+,.0f}円</td>"
        f"<td class='dn'>{total_s['max_loss']:+,.0f}円</td>"
        f"<td class='dn'>▼{total_s['max_dd']:,.0f}円</td>"
        f"<td>{total_s['max_consec_loss']}連敗</td>"
        f"</tr>"
    )

    # トレード詳細（銘柄別折りたたみ）
    details = ""
    for r in sorted(results, key=lambda x: (-x["stats"]["total"], x["symbol"])):
        s = r["stats"]
        if s["n"] == 0:
            continue
        trade_rows = _trade_rows(r["trades"])
        cls = "up" if s["total"] > 0 else "dn"
        details += f"""
<details>
  <summary>
    ▶ {r['symbol']} {r['name']} &nbsp;
    {s['n']}件 / 勝率{s['wr']:.0f}% / PF{pf_str(s['pf'])} /
    <span class='{cls}'>{s['total']:+,.0f}円</span>
  </summary>
  <table style="margin-top:6px">
    <thead><tr>
      <th>シグナル日<br><span style='font-weight:normal;font-size:.85em'>(終値)</span></th>
      <th>約定日</th><th>決済日</th>
      <th>指値<br><span style='font-weight:normal;font-size:.85em'>(注文)</span></th>
      <th>損切<br><span style='font-weight:normal;font-size:.85em'>(逆指値)</span></th>
      <th>利確目標<br><span style='font-weight:normal;font-size:.85em'>(ATR×{_amr.TARGET_ATR_MULT})</span></th>
      <th>決済価格</th><th>損益</th><th>保有日数</th><th>ATR</th><th>理由</th>
    </tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</details>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>AMR バックテスト {days}日</title>
<style>{_CSS}</style>
</head>
<body>
<h1>アダプティブMR バックテスト
  <span style="font-size:.75em;color:#8b949e">
    直近{days}日 &nbsp;|&nbsp; {len(STOCKS)}銘柄 &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>

<h2>累積損益</h2>
{_equity_svg(all_trades)}

<h2>銘柄別サマリー</h2>
<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>取引数</th><th>勝ち</th>
  <th>勝率</th><th>PF</th><th>合計損益</th><th>平均損益</th>
  <th>最大利益</th><th>最大損失</th><th>最大DD</th><th>最大連敗</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>

<h2>銘柄別トレード詳細（クリックで展開）</h2>
{details}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────────────

def _check_signal_on(df: pd.DataFrame) -> bool:
    """df の最終バーでシグナルが立っているか判定（履歴用）。"""
    if len(df) < 2:
        return False
    return _amr._has_signal_today(df) is not None


def run_history(days: int) -> None:
    """過去 days 日間のシグナル発生回数を銘柄ごとに集計して表示。"""
    today      = datetime.now(JST).date()
    from_date  = today - timedelta(days=days)
    fetch_days = days + 400   # 指標計算用に余裕をもたせる

    print(f"\n{'='*70}")
    print(f"  アダプティブMR シグナル発生頻度")
    print(f"  集計期間: {from_date} 〜 {today}  ({days}日間)  {len(STOCKS)}銘柄")
    print(f"{'='*70}\n")

    results = []

    def _scan(sym: str, nm: str):
        df = _fetch_df(sym, fetch_days)
        if df is None or len(df) < 50:
            return sym, nm, [], 0
        idx = [(i, (d.date() if hasattr(d, "date") else d)) for i, d in enumerate(df.index)]
        sig_dates: list[str] = []
        for i, d in idx:
            if d < from_date or d > today or i < 50:
                continue
            if _check_signal_on(df.iloc[:i + 1]):
                sig_dates.append(str(d))
        return sym, nm, sig_dates, len(sig_dates)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_scan, s, n): (s, n) for s, n in STOCKS}
        for f in as_completed(futs):
            sym, nm, sig_dates, cnt = f.result()
            results.append((sym, nm, cnt, sig_dates))

    results.sort(key=lambda x: -x[2])
    total_signals = sum(r[2] for r in results)

    print(f"  {'コード':<10}  {'銘柄名':<22}  {'回数':>4}  直近5件")
    print("  " + "─" * 65)
    for sym, nm, cnt, dates in results:
        recent = "  ".join(dates[-5:]) if dates else "—"
        print(f"  {sym:<10}  {nm:<22}  {cnt:>4}件  {recent}")

    print(f"\n  合計シグナル: {total_signals}件  ({days}日間 / {len(STOCKS)}銘柄)")
    avg_per_month = total_signals / (days / 30)
    print(f"  月平均: {avg_per_month:.1f}件\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="アダプティブMR専用 シグナル＆バックテスト",
    )
    parser.add_argument("--backtest",   action="store_true", help="バックテストモード")
    parser.add_argument("--history",    action="store_true", help="シグナル発生頻度モード")
    parser.add_argument("--days",       type=int, default=365, help="バックテスト/履歴の日数（デフォルト365）")
    parser.add_argument("--date",       type=str, default=None, help="シグナル判定日 YYYY-MM-DD")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザ自動起動しない")
    args = parser.parse_args()

    no_browser  = args.no_browser
    target_date = date.fromisoformat(args.date) if args.date else None

    if args.history:
        run_history(args.days)
        return

    if args.backtest:
        # ── バックテストモード ──────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  アダプティブMR バックテスト  直近{args.days}日  {len(STOCKS)}銘柄")
        print(f"{'='*60}\n")

        results: list[dict] = []
        all_trades: list[dict] = []

        def _bt(sym: str, nm: str):
            trades = _run_backtest(sym, args.days)
            return sym, nm, trades

        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_bt, s, n): (s, n) for s, n in STOCKS}
            for f in as_completed(futs):
                sym, nm, trades = f.result()
                s = _stats(trades)
                results.append(dict(symbol=sym, name=nm, trades=trades, stats=s))
                all_trades.extend(trades)
                if s["n"] > 0:
                    print(f"  {sym}  {nm:<18}  {s['n']}件  勝率{s['wr']:.0f}%  {s['total']:+,.0f}円")

        total = _stats(all_trades)
        print(f"\n  合計: {total['n']}件  勝率{total['wr']:.1f}%  PF{total['pf']:.2f}  {total['total']:+,.0f}円\n")

        html    = build_backtest_html(results, all_trades, args.days)
        today_s = datetime.now(JST).strftime("%Y%m%d")
        out     = Path(f"amr_backtest_{today_s}.html")
        out.write_text(html, encoding="utf-8")
        print(f"HTML: {out.resolve()}")
        if not no_browser:
            open_html(f"file://{out.resolve()}")

    else:
        # ── シグナルチェックモード ──────────────────────────────────
        ref = target_date or datetime.now(JST).date()
        print(f"\n{'='*60}")
        print(f"  アダプティブMR シグナルチェック  判定日: {ref}  {len(STOCKS)}銘柄")
        print(f"{'='*60}\n")

        active, waiting = _collect_signals(target_date)

        if active:
            print(f"【シグナル発生】 {len(active)}件")
            print("─" * 60)
            for sym, nm, r in active:
                entry  = r["limit_price"]
                stop   = r["stop_loss"]
                target = entry + r["atr"] * _amr.TARGET_ATR_MULT
                risk   = entry - stop
                print(f"  ★ {sym}  {nm}")
                print(f"     指値  : ¥{entry:,.0f}  (現在値¥{r['close']:,.0f}から{(entry-r['close'])/r['close']*100:+.2f}%)")
                print(f"     損切  : ¥{stop:,.0f}  (リスク¥{risk:,.0f} / ロット¥{risk*LOT:,.0f})")
                print(f"     利確  : ¥{target:,.0f}  RR={_rr(entry, stop, target)}")
                print(f"     期限  : {r['expire_days']}営業日")
                print()
        else:
            print("【シグナル発生なし】\n")

        if waiting:
            print(f"【待機中】 {len(waiting)}銘柄\n")

        html    = build_signal_html(active, waiting, target_date)
        ref_str = str(ref)
        out     = Path(f"amr_signals_{ref_str}.html")
        out.write_text(html, encoding="utf-8")
        print(f"HTML: {out.resolve()}")
        if not no_browser:
            open_html(f"file://{out.resolve()}")

        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
