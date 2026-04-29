"""
replay_yesterday.py  ―  昨日のORB+Donchian同時リプレイ
==================================================================
WATCH_SYMBOLS (Donchian選定20銘柄) について、昨日のデータで
ORBとDonchianの2戦略を実行し、結果をHTML表示する。

【使い方】
  python replay_yesterday.py                       # 自動で直近営業日
  python replay_yesterday.py --date 2026-04-10    # 指定日
"""

from __future__ import annotations

import argparse
import pickle
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

# daytrade_symbols.py から動的読み込み (50銘柄)
try:
    from daytrade_symbols import DAYTRADE_SYMBOLS
    WATCH_SYMBOLS = [(s.replace(".T", ""), n) for s, n in DAYTRADE_SYMBOLS]
except ImportError:
    WATCH_SYMBOLS = [
        ("8032", "日本紙パルプ商事"),
        ("7979", "松風"),
    ]

# 共通パラメータ
BUDGET = 600_000
MAX_RISK = 6_000
GAP_MAX_PCT = 2.0
TRAILING_TRIGGER = 0.5
FORCE_CLOSE = dtime(14, 55)
ENTRY_CUTOFF = dtime(11, 0)
AM_START = dtime(9, 0)

# ORB
OR_MINUTES = 30
ORB_TARGET_K = 1.5

# Donchian
DON_PERIOD = 20
DON_TARGET_R = 2.0          # 目標 R:R = 2.0
DON_WARMUP = 20
# Donchian 複層トレーリング: (進捗率, ロックするリスク倍率)
# 50%進捗で建値, 75%進捗で +0.5Rロック
DON_TRAIL_STEPS = [
    (0.50, 0.0),   # +1R (50%) → 建値 stop
    (0.75, 0.5),   # +1.5R (75%) → +0.5R ロック
]


def calc_qty(entry_p, stop_p, budget=BUDGET, max_risk=MAX_RISK):
    risk = abs(entry_p - stop_p)
    if risk <= 0:
        return 100
    qty = int(max_risk / risk / 100) * 100
    qty = min(qty, int(budget / entry_p / 100) * 100)
    return max(100, qty)


def load_day_data(code5: str, date: str) -> pd.DataFrame | None:
    """指定日のpklデータを読み込み、5分足DataFrame化。"""
    pkl_path = DATA_DIR / f"{code5}.pkl"
    if not pkl_path.exists():
        return None
    try:
        raw = pickle.loads(pkl_path.read_bytes())
    except Exception:
        return None
    if raw is None or raw.empty or "Date" not in raw.columns:
        return None
    day_df = raw[raw["Date"].astype(str).str[:10] == date].copy()
    if day_df.empty:
        return None
    day_df["DateTime"] = pd.to_datetime(
        day_df["Date"].astype(str) + " " + day_df["Time"].astype(str),
        format="%Y-%m-%d %H:%M"
    )
    day_df = day_df.rename(columns={
        "O": "open", "H": "high", "L": "low", "C": "close", "Vo": "volume"
    })
    for c in ["open", "high", "low", "close", "volume"]:
        day_df[c] = pd.to_numeric(day_df[c], errors="coerce")
    day_df = day_df.sort_values("DateTime").set_index("DateTime")
    return day_df


def get_prev_close(code5: str, date: str) -> float | None:
    """前日終値を取得。"""
    pkl_path = DATA_DIR / f"{code5}.pkl"
    if not pkl_path.exists():
        return None
    try:
        raw = pickle.loads(pkl_path.read_bytes())
        raw = raw[raw["Date"].astype(str).str[:10] < date]
        if raw.empty:
            return None
        raw = raw.sort_values(["Date", "Time"])
        return float(pd.to_numeric(raw.iloc[-1]["C"], errors="coerce"))
    except Exception:
        return None


def simulate_exit(day_df, entry_idx, entry_p, entry_dt,
                  stop_p, target_p, qty, strategy,
                  force_close=FORCE_CLOSE, trail_steps=None):
    """決済シミュレート。
    trail_steps: [(進捗率, ロックするリスク倍率), ...] の昇順リスト。
                 None なら従来の単段 (0.5進捗で建値) 挙動。
    """
    highs = day_df["high"].to_numpy(dtype=float)
    lows = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times = day_df.index
    orig_stop = stop_p
    # 従来挙動の互換: trail_steps 未指定なら単段 (建値のみ)
    steps = trail_steps if trail_steps is not None else [(TRAILING_TRIGGER, 0.0)]
    trail_level = 0   # 0=初期, 1以降=ロック
    stop_labels = ("損切り", "建値撤退", "+0.5Rロック", "トレール")
    risk = entry_p - orig_stop

    for i in range(entry_idx, len(day_df)):
        t = times[i].time()
        hi, lo, cl = highs[i], lows[i], closes[i]

        if t >= force_close:
            return _trade(entry_dt, times[i], entry_p, cl, orig_stop,
                          target_p, qty, strategy, "引け強制")

        # 1. 決済判定 (現在の stop で保守的に)
        exit_reason = stop_labels[min(trail_level, len(stop_labels) - 1)]
        if lo <= stop_p and hi >= target_p:
            return _trade(entry_dt, times[i], entry_p, stop_p, orig_stop,
                          target_p, qty, strategy, exit_reason)
        if hi >= target_p:
            return _trade(entry_dt, times[i], entry_p, target_p, orig_stop,
                          target_p, qty, strategy, "目標達成")
        if lo <= stop_p:
            return _trade(entry_dt, times[i], entry_p, stop_p, orig_stop,
                          target_p, qty, strategy, exit_reason)

        # 2. 複層トレーリング更新 (バー高値で次バー以降に適用)
        if target_p > entry_p and risk > 0:
            progress = (hi - entry_p) / (target_p - entry_p)
            for idx, (trigger, lock_r) in enumerate(steps):
                if progress >= trigger and trail_level <= idx:
                    new_stop = entry_p + risk * lock_r
                    if new_stop > stop_p:
                        stop_p = new_stop
                        trail_level = idx + 1

    return _trade(entry_dt, times[-1], entry_p, float(closes[-1]),
                  orig_stop, target_p, qty, strategy, "引け強制")


def _trade(entry_dt, exit_dt, entry_p, exit_p, stop_p, target_p,
           qty, strategy, reason):
    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p,
        stop_p=stop_p, target_p=target_p,
        qty=qty, pnl=pnl, pct=pct,
        strategy=strategy, reason=reason,
    )


# ─────────────────────────────────────────────────────────────
# ORB戦略
# ─────────────────────────────────────────────────────────────

def run_orb(day_df, prev_close=None):
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=OR_MINUTES)).time()
    or_bars = day_df[day_df.index.time < or_end]
    rest = day_df[day_df.index.time >= or_end]
    if len(or_bars) < 2 or rest.empty:
        return None, None

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w = or_hi - or_lo
    if or_w <= 0:
        return None, {"or_hi": or_hi, "or_lo": or_lo, "or_w": 0}

    open_p = float(or_bars.iloc[0]["open"])
    if prev_close and prev_close > 0:
        if abs(open_p - prev_close) / prev_close * 100 > GAP_MAX_PCT:
            return None, {"or_hi": or_hi, "or_lo": or_lo, "or_w": or_w,
                          "skip": "ギャップ超過"}

    bars = list(rest.itertuples())
    or_info = {"or_hi": or_hi, "or_lo": or_lo, "or_w": or_w}

    for i, bar in enumerate(bars):
        t = bar.Index.time()
        if t >= ENTRY_CUTOFF:
            return None, or_info
        cl = float(bar.close)
        if cl > or_hi and i + 1 < len(bars):
            nxt = bars[i + 1]
            entry_p = float(nxt.open)
            stop_p = or_lo
            target_p = entry_p + or_w * ORB_TARGET_K
            if entry_p <= stop_p:
                return None, or_info
            qty = calc_qty(entry_p, stop_p)
            # エントリーインデックスを day_df に戻す
            entry_abs_idx = len(or_bars) + i + 1
            trade = simulate_exit(day_df, entry_abs_idx, entry_p, nxt.Index,
                                  stop_p, target_p, qty, "ORB")
            return trade, or_info
    return None, or_info


# ─────────────────────────────────────────────────────────────
# Donchian戦略
# ─────────────────────────────────────────────────────────────

def run_donchian(day_df, prev_close=None):
    opens = day_df["open"].to_numpy(dtype=float)
    highs = day_df["high"].to_numpy(dtype=float)
    lows = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times = day_df.index
    n = len(day_df)

    if n < DON_WARMUP + 2:
        return None, None

    if prev_close and prev_close > 0:
        if abs(opens[0] - prev_close) / prev_close * 100 > GAP_MAX_PCT:
            return None, {"skip": "ギャップ超過"}

    for i in range(DON_WARMUP, n):
        t = times[i].time()
        if t >= ENTRY_CUTOFF:
            return None, {}
        don_hi = highs[i - DON_PERIOD:i].max()
        don_lo = lows[i - DON_PERIOD:i].min()
        if closes[i] > don_hi and closes[i] > opens[i]:
            if i + 1 >= n:
                return None, {}
            entry_p = opens[i + 1]
            stop_p = don_lo
            if entry_p <= stop_p:
                continue
            target_p = entry_p + (entry_p - stop_p) * DON_TARGET_R
            qty = calc_qty(entry_p, stop_p)
            info = {"don_hi": don_hi, "don_lo": don_lo, "break_bar": times[i]}
            trade = simulate_exit(day_df, i + 1, entry_p, times[i + 1],
                                  stop_p, target_p, qty, "Donchian",
                                  trail_steps=DON_TRAIL_STEPS)
            return trade, info
    return None, {}


# ─────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────

def build_html(results, date):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # 戦略別集計
    orb_trades = [r["orb_trade"] for r in results if r.get("orb_trade")]
    don_trades = [r["don_trade"] for r in results if r.get("don_trade")]

    def _stats(trades):
        if not trades:
            return "0取引 / 損益: 0円"
        total = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        return f"{len(trades)}取引 / 勝{wins} / {'+' if total>=0 else ''}{total:,.0f}円"

    orb_total = sum(t["pnl"] for t in orb_trades)
    don_total = sum(t["pnl"] for t in don_trades)

    # 実運用想定 (最初のシグナル1銘柄)
    first_orb = None
    first_don = None
    for r in results:
        if r.get("orb_trade") and (first_orb is None or
                                    r["orb_trade"]["entry_dt"] < first_orb[1]["entry_dt"]):
            first_orb = (r["name"], r["orb_trade"], r["symbol"])
        if r.get("don_trade") and (first_don is None or
                                    r["don_trade"]["entry_dt"] < first_don[1]["entry_dt"]):
            first_don = (r["name"], r["don_trade"], r["symbol"])

    # 銘柄ごとの行
    rows = ""
    for r in results:
        orb_t = r.get("orb_trade")
        don_t = r.get("don_trade")

        def _cell(t):
            if t is None:
                return '<td>-</td><td>-</td><td>-</td><td>-</td>'
            cls = "profit" if t["pnl"] > 0 else "loss"
            entry_time = t["entry_dt"].strftime("%H:%M") if hasattr(t["entry_dt"], "strftime") else "-"
            return (f'<td>{entry_time}</td>'
                    f'<td>{t["entry_p"]:,.0f}</td>'
                    f'<td>{t["exit_p"]:,.0f}</td>'
                    f'<td class="{cls}">{t["pnl"]:+,.0f}</td>')

        rows += f"""<tr>
          <td class="sym">{r['name']}<br><small class="code">{r['symbol']}.T</small></td>
          <td>{r.get('or_hi', '-')}</td>
          {_cell(orb_t)}
          {_cell(don_t)}
        </tr>"""

    # 最初のシグナル (実運用想定)
    def _first_section(first, label, color):
        if not first:
            return f'<p style="color:{color}">● {label}: シグナルなし → トレードなし</p>'
        name, t, sym = first
        cls = "profit" if t["pnl"] > 0 else "loss"
        mark = "○" if t["pnl"] > 0 else "●"
        return f"""<p style="color:{color}"><strong>{mark} {label}: {name} ({sym}.T)</strong><br>
          {t['entry_dt'].strftime('%H:%M')} 買値 {t['entry_p']:,.0f} × {t['qty']}株<br>
          {t['exit_dt'].strftime('%H:%M')} 決済 {t['exit_p']:,.0f} ({t['reason']})<br>
          <span class="{cls}" style="font-size:1.3rem;font-weight:bold">損益: {t['pnl']:+,.0f}円</span></p>"""

    orb_cls = "profit" if orb_total >= 0 else "loss"
    don_cls = "profit" if don_total >= 0 else "loss"

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>{date} ORB+Donchian リプレイ</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#60a5fa;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#60a5fa;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #60a5fa;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right}}
.sym{{text-align:left;font-weight:600}}.code{{color:#64748b;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.5rem;font-weight:700}}
.strat-orb{{color:#fbbf24}}.strat-don{{color:#10b981}}
.comp-box{{background:#1e293b;padding:16px;border-radius:8px;margin-bottom:14px}}
</style></head><body>
<h1>ORB + Donchian 戦略比較リプレイ</h1>
<p class="sub">対象日: <strong>{date}</strong> / 監視: {len(results)}銘柄 / 予算: {BUDGET:,}円 / リスク: {MAX_RISK:,}円</p>

<div class="box">
<div class="it"><div class="lb strat-orb">ORB 合計</div>
<div class="vl {orb_cls}">{orb_total:+,.0f}円</div>
<div class="lb">{_stats(orb_trades)}</div></div>
<div class="it"><div class="lb strat-don">Donchian 合計</div>
<div class="vl {don_cls}">{don_total:+,.0f}円</div>
<div class="lb">{_stats(don_trades)}</div></div>
</div>

<h2>実運用想定 (最初のシグナル1銘柄)</h2>
<div class="comp-box">
{_first_section(first_orb, "ORB", "#fbbf24")}
{_first_section(first_don, "Donchian", "#10b981")}
</div>

<h2>銘柄別結果 (全シグナル採用)</h2>
<table><thead>
<tr><th rowspan="2">銘柄</th><th rowspan="2">OR高値</th>
<th colspan="4" style="color:#fbbf24">ORB</th>
<th colspan="4" style="color:#10b981">Donchian</th></tr>
<tr><th>Entry</th><th>買値</th><th>決済値</th><th>損益</th>
<th>Entry</th><th>買値</th><th>決済値</th><th>損益</th></tr>
</thead><tbody>{rows}</tbody></table>
</body></html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def _find_latest_date():
    """データ内の最新営業日を検出。"""
    # 日立のpklから最終日取得
    for code4, _ in WATCH_SYMBOLS:
        code5 = code4 + "0"
        pkl_path = DATA_DIR / f"{code5}.pkl"
        if pkl_path.exists():
            try:
                df = pickle.loads(pkl_path.read_bytes())
                if "Date" in df.columns and not df.empty:
                    return str(df["Date"].max())[:10]
            except Exception:
                continue
    return datetime.now(JST).strftime("%Y-%m-%d")


def main():
    parser = argparse.ArgumentParser(description="昨日のORB+Donchianリプレイ")
    parser.add_argument("--date", default=None, help="日付 (YYYY-MM-DD)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    date = args.date or _find_latest_date()
    print(f"リプレイ対象日: {date}")
    print(f"監視銘柄: {len(WATCH_SYMBOLS)}銘柄")
    print("=" * 70)

    results = []
    for i, (code4, name) in enumerate(WATCH_SYMBOLS, 1):
        code5 = code4 + "0"
        day_df = load_day_data(code5, date)
        if day_df is None or day_df.empty:
            print(f"[{i:>2}/{len(WATCH_SYMBOLS)}] {name} ({code4}): データなし")
            continue

        prev_close = get_prev_close(code5, date)

        orb_trade, orb_info = run_orb(day_df, prev_close)
        don_trade, don_info = run_donchian(day_df, prev_close)

        results.append({
            "symbol": code4, "name": name,
            "or_hi": f"{orb_info.get('or_hi', 0):,.0f}" if orb_info else "-",
            "orb_trade": orb_trade,
            "don_trade": don_trade,
        })

        orb_str = (f"ORB {orb_trade['pnl']:+,.0f}円 ({orb_trade['reason']})"
                   if orb_trade else "ORB -")
        don_str = (f"Don {don_trade['pnl']:+,.0f}円 ({don_trade['reason']})"
                   if don_trade else "Don -")
        print(f"[{i:>2}/{len(WATCH_SYMBOLS)}] {name:<18} ({code4}): {orb_str} / {don_str}")

    # サマリー
    orb_trades = [r["orb_trade"] for r in results if r.get("orb_trade")]
    don_trades = [r["don_trade"] for r in results if r.get("don_trade")]
    orb_total = sum(t["pnl"] for t in orb_trades)
    don_total = sum(t["pnl"] for t in don_trades)

    print()
    print("=" * 70)
    print(f"  全シグナル採用時の合計損益")
    print("=" * 70)
    print(f"  ORB     : {len(orb_trades)}取引  {orb_total:+,.0f}円")
    print(f"  Donchian: {len(don_trades)}取引  {don_total:+,.0f}円")
    print(f"  2戦略合計: {orb_total + don_total:+,.0f}円")

    # HTML出力
    out = Path(f"replay_yesterday_{date.replace('-', '')}.html")
    out.write_text(build_html(results, date), encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
