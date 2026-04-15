"""
donchian_multi.py  ―  Donchian 複数ポジ同時保有 + 3段階トレーリング + 動的キャピタル
============================================================================
A: 複数ポジション同時保有 (最大3ポジ、動的キャピタル割当)
   → 1ポジだけの時は全予算(60万)を使用、2ポジ目以降は残資金を使用
B: 3段階トレーリングストップ
  +50%進捗 → 建値 (損失ゼロ)
  +75%進捗 → +25% リスク分ロック
  +90%進捗 → +50% リスク分ロック

【使い方】
  python donchian_multi.py --days 60
  python donchian_multi.py --from 2026-01-01 --to 2026-04-10
  python donchian_multi.py --max-pos 3 --days 90
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

try:
    from daytrade_symbols import DAYTRADE_SYMBOLS
    WATCH_SYMBOLS = [(s.replace(".T", ""), n) for s, n in DAYTRADE_SYMBOLS]
except ImportError:
    WATCH_SYMBOLS = []

# パラメータ
BUDGET = 600_000
MAX_RISK = 6_000
MAX_POSITIONS = 3
DON_PERIOD = 20
TARGET_R = 2.5   # 目標を広めに設定、トレーリングで段階的に利益確保
GAP_MAX_PCT = 2.0
FORCE_CLOSE = dtime(14, 55)
ENTRY_CUTOFF = dtime(11, 0)

# 3段階トレーリング (進捗ベース、緩め設定)
# 早すぎるトレーリングは勝ちトレードを殺すので閾値を上げる
TRAIL_STEPS = [
    (0.50, 0.0),   # 進捗50% → 建値 (元と同じ)
    (0.75, 0.25),  # 進捗75% → +25%リスクをロック
    (0.90, 0.50),  # 進捗90% → +50%リスクをロック (目標近くで確保)
]


def _load_day(code5: str, date: str) -> pd.DataFrame | None:
    """指定日の5分足を読込、正規化。"""
    pkl = DATA_DIR / f"{code5}.pkl"
    if not pkl.exists():
        return None
    try:
        raw = pickle.loads(pkl.read_bytes())
    except Exception:
        return None
    if "Date" not in raw.columns or raw.empty:
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
    return day_df.sort_values("DateTime").set_index("DateTime")


def _get_prev_close(code5: str, date: str) -> float | None:
    pkl = DATA_DIR / f"{code5}.pkl"
    if not pkl.exists():
        return None
    try:
        raw = pickle.loads(pkl.read_bytes())
        raw = raw[raw["Date"].astype(str).str[:10] < date]
        if raw.empty:
            return None
        raw = raw.sort_values(["Date", "Time"])
        return float(pd.to_numeric(raw.iloc[-1]["C"], errors="coerce"))
    except Exception:
        return None


def _calc_qty(entry_p, stop_p, available_capital, max_risk):
    """ポジションサイジング (固定リスク + 動的資金上限)。
    available_capital: このポジに使える残り資金 (既存ポジが使っていない分)。
    """
    risk = abs(entry_p - stop_p)
    if risk <= 0:
        return 0
    qty_by_risk = int(max_risk / risk / 100) * 100
    qty_by_cap = int(available_capital / entry_p / 100) * 100
    qty = min(qty_by_risk, qty_by_cap)
    # 100株未満なら見送り (資金不足)
    if qty < 100:
        return 0
    return qty


def _update_trailing(pos, bar_close):
    """3段階トレーリングストップを更新。"""
    entry = pos["entry_p"]
    orig_stop = pos["orig_stop"]
    target = pos["target_p"]
    risk = entry - orig_stop
    if target <= entry or risk <= 0:
        return

    progress = (bar_close - entry) / (target - entry)
    trail_level = pos["trail_level"]

    # 逆順で判定 (高い進捗から)
    for i in range(len(TRAIL_STEPS) - 1, -1, -1):
        trigger, lock_pct = TRAIL_STEPS[i]
        if progress >= trigger and trail_level <= i:
            pos["stop_p"] = entry + risk * lock_pct
            pos["trail_level"] = i + 1
            return


def _check_exit(pos, bar, time_now):
    """決済条件チェック。"""
    hi = float(bar["high"])
    lo = float(bar["low"])
    cl = float(bar["close"])

    if time_now.time() >= FORCE_CLOSE:
        return cl, "引け強制"

    # トレーリング更新
    _update_trailing(pos, cl)

    stop_p = pos["stop_p"]
    target_p = pos["target_p"]

    # OCO: 同バーで両方ヒット時は損切り優先 (保守的)
    if lo <= stop_p and hi >= target_p:
        if pos["trail_level"] == 0:
            return stop_p, "損切り"
        return stop_p, f"トレール{pos['trail_level']}"
    if hi >= target_p:
        return target_p, "目標達成"
    if lo <= stop_p:
        if pos["trail_level"] == 0:
            return stop_p, "損切り"
        return stop_p, f"トレール{pos['trail_level']}"
    return None, None


def backtest_day_multi(date: str, max_pos: int, budget: int, max_risk: int):
    """1日分バックテスト (複数ポジ対応、動的キャピタル割当)。"""
    # 全銘柄データを読込
    symbols_data = {}
    prev_closes = {}
    for code4, name in WATCH_SYMBOLS:
        code5 = code4 + "0"
        df = _load_day(code5, date)
        if df is None or df.empty:
            continue
        prev = _get_prev_close(code5, date)
        # ギャップフィルタ
        if prev and prev > 0:
            op = float(df.iloc[0]["open"])
            if abs(op - prev) / prev * 100 > GAP_MAX_PCT:
                continue
        symbols_data[code4] = (name, df)
        prev_closes[code4] = prev

    if not symbols_data:
        return [], []

    # 全タイムスタンプをマージ (時系列順)
    all_times = sorted(set().union(*[set(df.index) for _, df in symbols_data.values()]))

    positions = []  # active positions
    trades = []
    max_concurrent = 0

    for t in all_times:
        # 1. 既存ポジションの決済チェック
        for pos in positions[:]:
            sym = pos["symbol"]
            _, df = symbols_data.get(sym, (None, None))
            if df is None or t not in df.index:
                continue
            bar = df.loc[t]
            exit_p, reason = _check_exit(pos, bar, t)
            if exit_p is not None:
                pos["exit_dt"] = t
                pos["exit_p"] = exit_p
                pos["reason"] = reason
                pos["pnl"] = (exit_p - pos["entry_p"]) * pos["qty"]
                pos["pct"] = (exit_p - pos["entry_p"]) / pos["entry_p"] * 100
                trades.append(pos)
                positions.remove(pos)

        # 2. 強制決済 (14:55)
        if t.time() >= FORCE_CLOSE:
            for pos in positions[:]:
                sym = pos["symbol"]
                _, df = symbols_data.get(sym, (None, None))
                if df is None or t not in df.index:
                    continue
                cl = float(df.loc[t]["close"])
                pos["exit_dt"] = t
                pos["exit_p"] = cl
                pos["reason"] = "引け強制"
                pos["pnl"] = (cl - pos["entry_p"]) * pos["qty"]
                pos["pct"] = (cl - pos["entry_p"]) / pos["entry_p"] * 100
                trades.append(pos)
                positions.remove(pos)
            continue

        # 3. エントリー締切後は新規シグナルなし
        if t.time() >= ENTRY_CUTOFF:
            continue

        # 4. 新規シグナルチェック
        if len(positions) >= max_pos:
            continue

        held_symbols = set(p["symbol"] for p in positions)
        for code4, (name, df) in symbols_data.items():
            if len(positions) >= max_pos:
                break
            if code4 in held_symbols:
                continue
            if t not in df.index:
                continue

            i = df.index.get_loc(t)
            if i < DON_PERIOD:
                continue

            don_hi = df.iloc[i - DON_PERIOD:i]["high"].max()
            don_lo = df.iloc[i - DON_PERIOD:i]["low"].min()
            cl = float(df.iloc[i]["close"])
            op = float(df.iloc[i]["open"])

            if cl > don_hi and cl > op and i + 1 < len(df):
                nxt = df.iloc[i + 1]
                entry_p = float(nxt["open"])
                stop_p = float(don_lo)
                if entry_p <= stop_p:
                    continue
                risk = entry_p - stop_p
                target_p = entry_p + risk * TARGET_R

                # 動的キャピタル: 既存ポジが使っていない残り資金を全投入
                used_capital = sum(p["entry_p"] * p["qty"] for p in positions)
                available_capital = budget - used_capital
                qty = _calc_qty(entry_p, stop_p, available_capital, max_risk)
                if qty < 100:
                    continue

                positions.append({
                    "symbol": code4, "name": name, "date": date,
                    "entry_dt": df.index[i + 1],
                    "entry_p": entry_p,
                    "orig_stop": stop_p, "stop_p": stop_p,
                    "target_p": target_p,
                    "qty": qty,
                    "trail_level": 0,
                    "max_concurrent_at_entry": len(positions) + 1,
                })
                max_concurrent = max(max_concurrent, len(positions))

    # 未決済ポジションは最後のバーで強制決済
    for pos in positions:
        sym = pos["symbol"]
        _, df = symbols_data.get(sym, (None, None))
        if df is not None and not df.empty:
            cl = float(df.iloc[-1]["close"])
            pos["exit_dt"] = df.index[-1]
            pos["exit_p"] = cl
            pos["reason"] = "引け強制"
            pos["pnl"] = (cl - pos["entry_p"]) * pos["qty"]
            pos["pct"] = (cl - pos["entry_p"]) / pos["entry_p"] * 100
            trades.append(pos)

    return trades, max_concurrent


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    # 時系列ソートしてDD計算
    sorted_trades = sorted(trades, key=lambda t: str(t.get("exit_dt", "")))
    for t in sorted_trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=dd)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def get_dates(from_date: str, to_date: str) -> list[str]:
    all_dates = set()
    for code4, _ in WATCH_SYMBOLS:
        code5 = code4 + "0"
        pkl = DATA_DIR / f"{code5}.pkl"
        if not pkl.exists():
            continue
        try:
            df = pickle.loads(pkl.read_bytes())
            if "Date" in df.columns:
                for d in df["Date"].astype(str).str[:10].unique():
                    if from_date <= d <= to_date:
                        all_dates.add(d)
        except Exception:
            pass
    return sorted(all_dates)


def build_html(trades, dates, from_date, to_date, max_pos, budget):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    stats = calc_stats(trades, budget)
    cls = "profit" if stats["total_pnl"] >= 0 else "loss"

    # 日次損益
    daily_stats = {}
    for t in trades:
        d = t["date"]
        daily_stats.setdefault(d, []).append(t)

    daily_rows = ""
    cum = budget
    for d in dates:
        day_trades = daily_stats.get(d, [])
        day_pnl = sum(t["pnl"] for t in day_trades)
        cum += day_pnl
        pnl_cls = "profit" if day_pnl >= 0 else ("loss" if day_pnl < 0 else "")
        max_conc = max((t.get("max_concurrent_at_entry", 1) for t in day_trades), default=0)
        daily_rows += f"""<tr>
          <td>{d}</td>
          <td>{len(day_trades)}</td>
          <td>{max_conc}</td>
          <td class="{pnl_cls}">{day_pnl:+,.0f}</td>
          <td>{cum:,.0f}</td>
        </tr>"""

    # 銘柄別
    by_sym = {}
    for t in trades:
        s = t["symbol"]
        by_sym.setdefault(s, []).append(t)

    sym_rows = ""
    for sym in sorted(by_sym.keys(),
                       key=lambda s: -sum(t["pnl"] for t in by_sym[s])):
        ts = by_sym[sym]
        s_stats = calc_stats(ts, budget)
        c = "profit" if s_stats["total_pnl"] >= 0 else "loss"
        name = ts[0]["name"]
        sym_rows += f"""<tr>
          <td class="sym">{name}<br><small class="code">{sym}.T</small></td>
          <td>{s_stats['n']}</td>
          <td>{s_stats['win_rate']:.0f}%</td>
          <td>{_pf(s_stats['pf'])}</td>
          <td class="{c}">{s_stats['total_pnl']:+,.0f}</td>
        </tr>"""

    # トレード明細
    trade_rows = ""
    for t in sorted(trades, key=lambda x: (x["date"], str(x["entry_dt"]))):
        pc = "profit" if t["pnl"] > 0 else "loss"
        ed = t["entry_dt"].strftime("%H:%M") if hasattr(t["entry_dt"], "strftime") else "-"
        xd = t["exit_dt"].strftime("%H:%M") if hasattr(t["exit_dt"], "strftime") else "-"
        trail_badge = ""
        if t.get("trail_level", 0) >= 1:
            trail_badge = f'<span class="badge">T{t["trail_level"]}</span>'
        trade_rows += f"""<tr>
          <td>{t['date']}</td>
          <td class="sym">{t['name']}<br><small class="code">{t['symbol']}.T</small></td>
          <td>{ed}</td><td>{xd}</td>
          <td>{t['entry_p']:,.0f}</td>
          <td class="loss">{t['orig_stop']:,.0f}</td>
          <td class="profit">{t['target_p']:,.0f}</td>
          <td>{t['exit_p']:,.0f}</td>
          <td>{t['qty']}</td>
          <td class="{pc}">{t['pnl']:+,.0f}</td>
          <td class="{pc}">{t['pct']:+.2f}%</td>
          <td>{t['reason']}{trail_badge}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Donchian Multi — {from_date}〜{to_date}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #10b981;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right}}
.sym{{text-align:left;font-weight:600}}.code{{color:#64748b;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center;min-width:150px}}.box .lb{{color:#94a3b8;font-size:.75rem}}
.box .vl{{font-size:1.5rem;font-weight:700}}
.badge{{display:inline-block;padding:2px 6px;border-radius:4px;background:#7c3aed;color:#fff;font-size:.7rem;margin-left:4px}}
</style></head><body>
<h1>Donchian 複数ポジ + 3段階トレーリング</h1>
<p class="sub">期間: <strong>{from_date} 〜 {to_date}</strong> ({len(dates)}営業日) /
  監視: {len(WATCH_SYMBOLS)}銘柄 / 予算: {budget:,}円 / 最大同時保有: {max_pos}ポジ</p>

<div class="box">
<div class="it"><div class="lb">総損益</div><div class="vl {cls}">{stats['total_pnl']:+,.0f}円</div></div>
<div class="it"><div class="lb">取引数</div><div class="vl">{stats['n']}</div></div>
<div class="it"><div class="lb">勝率</div><div class="vl">{stats['win_rate']:.1f}%</div></div>
<div class="it"><div class="lb">PF</div><div class="vl">{_pf(stats['pf'])}</div></div>
<div class="it"><div class="lb">最大DD</div><div class="vl loss">{stats['max_dd']:+.1f}%</div></div>
<div class="it"><div class="lb">平均利益</div><div class="vl profit">{stats['avg_win']:+,.0f}</div></div>
<div class="it"><div class="lb">平均損失</div><div class="vl loss">{stats['avg_loss']:+,.0f}</div></div>
</div>

<h2>日次損益</h2>
<table><thead><tr><th>日付</th><th>取引数</th><th>最大同時</th><th>日次損益</th><th>累積資金</th></tr></thead>
<tbody>{daily_rows}</tbody></table>

<h2>銘柄別サマリー</h2>
<table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th><th>損益</th></tr></thead>
<tbody>{sym_rows}</tbody></table>

<h2>個別トレード明細</h2>
<p class="sub">T1/T2/T3 = トレーリングレベル (+25%/+50%/+75%進捗でストップ移動)</p>
<table><thead><tr>
<th>日付</th><th>銘柄</th><th>Entry</th><th>Exit</th>
<th>買値</th><th>初期損切</th><th>目標</th><th>決済値</th>
<th>株数</th><th>損益</th><th>%</th><th>理由</th>
</tr></thead><tbody>{trade_rows}</tbody></table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Donchian 複数ポジ+3段階トレーリング")
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-pos", type=int, default=MAX_POSITIONS)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # 日付決定
    if args.from_date and args.to_date:
        from_date, to_date = args.from_date, args.to_date
    else:
        # 最新営業日を取得
        latest = None
        for code4, _ in WATCH_SYMBOLS[:5]:
            code5 = code4 + "0"
            pkl = DATA_DIR / f"{code5}.pkl"
            if pkl.exists():
                try:
                    df = pickle.loads(pkl.read_bytes())
                    if "Date" in df.columns and not df.empty:
                        d = str(df["Date"].max())[:10]
                        if latest is None or d > latest:
                            latest = d
                except Exception:
                    pass
        to_date = args.to_date or latest or datetime.now(JST).strftime("%Y-%m-%d")
        from_dt = datetime.strptime(to_date, "%Y-%m-%d") - timedelta(days=args.days)
        from_date = args.from_date or from_dt.strftime("%Y-%m-%d")

    dates = get_dates(from_date, to_date)
    print(f"期間: {from_date} 〜 {to_date} ({len(dates)}営業日)")
    print(f"監視: {len(WATCH_SYMBOLS)}銘柄 / 最大同時保有: {args.max_pos}ポジ")
    print("=" * 70)

    all_trades = []
    for di, date in enumerate(dates, 1):
        trades, max_conc = backtest_day_multi(date, args.max_pos, args.budget, MAX_RISK)
        all_trades.extend(trades)
        if di % 10 == 0 or di == len(dates):
            day_pnl = sum(t["pnl"] for t in all_trades)
            print(f"  {di}/{len(dates)}: 累計{len(all_trades)}取引 {day_pnl:+,.0f}円", flush=True)

    # サマリー
    stats = calc_stats(all_trades, args.budget)
    print()
    print("=" * 70)
    print(f"  Donchian 複数ポジ ({args.max_pos}) + 3段階トレーリング")
    print("=" * 70)
    print(f"  取引数 : {stats['n']}")
    print(f"  勝率   : {stats['win_rate']:.1f}%")
    print(f"  PF     : {_pf(stats['pf'])}")
    print(f"  総損益 : {stats['total_pnl']:+,.0f}円")
    print(f"  平均利益: {stats['avg_win']:+,.0f}円")
    print(f"  平均損失: {stats['avg_loss']:+,.0f}円")
    print(f"  最大DD : {stats['max_dd']:+.1f}%")
    print("=" * 70)

    # HTML
    out = Path(f"donchian_multi_{from_date.replace('-','')}_{to_date.replace('-','')}.html")
    out.write_text(build_html(all_trades, dates, from_date, to_date,
                              args.max_pos, args.budget), encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
