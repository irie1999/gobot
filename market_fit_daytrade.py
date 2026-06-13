"""
market_fit_daytrade.py  ―  戦略×相場局面 適合度マトリックス
==================================================================
逆指値ロング (market_fit.py) を5分足デイトレ用に移植。

【役割】
過去のデータを使って、戦略×相場局面の適合度を計算。
どの戦略がどの相場で機能するかを可視化する。

【出力】
- 戦略 × (トレンド×ボラ) の9セル マトリックス
- 各セルの PF/勝率/取引数
- 推奨戦略マップ (REGIME_STRATEGY_MAP) の更新候補

【使い方】
  python market_fit_daytrade.py
  python market_fit_daytrade.py --watchlist daytrade_combined_watchlist.py
  python market_fit_daytrade.py --days 730
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from check_signals_daytrade import _load_watchlist, ALL_STRATEGIES
from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m
from risk_metrics_5m import enrich_stats

JST = timezone(timedelta(hours=9))

REGIMES = [
    ("up", "high"),
    ("up", "mid"),
    ("up", "low"),
    ("sideways", "high"),
    ("sideways", "mid"),
    ("sideways", "low"),
    ("down", "high"),
    ("down", "mid"),
    ("down", "low"),
]


def fetch_nikkei_regime_daily(days=730):
    """日経の日次レジーム判定 (up/sideways/down × high/mid/low)。

    戻り値: {date: (trend, vol_level)}
    """
    try:
        import yfinance as yf
        df = yf.download("^N225", period=f"{days+60}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return {}
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]

        ma10 = close.rolling(10).mean()
        ma25 = close.rolling(25).mean()
        # 14日ローリングボラ
        rolling_vol = close.pct_change().rolling(14).std() * 100

        result = {}
        for dt in close.index:
            try:
                c = float(close.loc[dt])
                m10 = float(ma10.loc[dt])
                m25 = float(ma25.loc[dt])
                v = float(rolling_vol.loc[dt])
            except Exception:
                continue
            if pd.isna(m10) or pd.isna(m25) or pd.isna(v):
                continue
            if c > m10 and m10 > m25:
                trend = "up"
            elif c < m10 and m10 < m25:
                trend = "down"
            else:
                trend = "sideways"
            if v > 1.5:
                vol_l = "high"
            elif v > 0.8:
                vol_l = "mid"
            else:
                vol_l = "low"
            result[dt.date()] = (trend, vol_l)
        return result
    except Exception:
        return {}


def classify_trades_by_regime(trades, regime_map):
    """トレード列を局面別に分類。"""
    by_regime = {r: [] for r in REGIMES}
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = dt.date()
        if d in regime_map:
            r = regime_map[d]
            if r in by_regime:
                by_regime[r].append(t)
    return by_regime


def main():
    parser = argparse.ArgumentParser(description="戦略×相場 適合度マトリックス")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # WATCHLIST
    watchlist = _load_watchlist(args.watchlist)
    if not watchlist:
        print("[error] WATCHLIST 空")
        return

    print(f"WATCHLIST: {len(watchlist)}件 / 期間 {args.days}日")

    # 日経レジーム
    print("\n[Step 1] 日経 日次レジーム判定", flush=True)
    regime_map = fetch_nikkei_regime_daily(args.days)
    print(f"  期間: {min(regime_map.keys())} 〜 {max(regime_map.keys())}")
    counts = {}
    for r in regime_map.values():
        counts[r] = counts.get(r, 0) + 1
    for (t, v), n in sorted(counts.items()):
        print(f"  {t}/{v}: {n}日")

    # WATCHLIST × 戦略 backtest → 戦略別 トレード集約
    print(f"\n[Step 2] WATCHLIST backtest", flush=True)
    targets = []
    for entry in watchlist:
        if len(entry) >= 3:
            sym, name, strat = entry[:3]
        else:
            sym, name = entry[:2]
            strat = "DON"
        targets.append((sym, name, strat))

    # 戦略別 トレード集約: {strategy: [trades]}
    by_strategy = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for sym, name, strat in targets:
            fetched = load_intraday_batch([sym], args.days, source="local")
            df = fetched.get(sym)
            if df is None:
                continue
            fn = ALL_STRATEGIES.get(strat)
            if fn is None:
                continue
            futs[ex.submit(backtest_symbol_5m, sym, name, df, fn,
                            {"name": strat})] = strat
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
                if r and r["trades"]:
                    strat = futs[fut]
                    by_strategy.setdefault(strat, []).extend(r["trades"])
            except Exception:
                pass
            if i % 20 == 0:
                print(f"  {i}", flush=True)

    # 戦略 × 局面 マトリックス計算
    print(f"\n[Step 3] マトリックス集計", flush=True)
    matrix = {}  # {strategy: {regime: stats}}
    for strat, trades in by_strategy.items():
        by_regime = classify_trades_by_regime(trades, regime_map)
        matrix[strat] = {r: enrich_stats(ts) for r, ts in by_regime.items()}

    # HTML 出力
    today = datetime.now(JST).strftime("%Y-%m-%d")

    # 戦略一覧
    strats = sorted(matrix.keys())
    if not strats:
        print("[warn] データなし")
        return

    # マトリックス HTML
    matrix_rows = ""
    for strat in strats:
        cells = ""
        for r in REGIMES:
            s = matrix[strat].get(r, {})
            n = s.get("n", 0)
            pf = s.get("pf", 0)
            pnl = s.get("total_pnl", 0)
            wr = s.get("win_rate", 0)
            if n == 0:
                cells += '<td style="color:#475569">—</td>'
            else:
                pf_color = ("#4ade80" if pf >= 1.5 else
                            "#facc15" if pf >= 1.0 else "#f87171")
                cells += (f'<td style="color:{pf_color}" '
                          f'title="{n}取引 / 勝率{wr:.0f}% / 損益{pnl:+,.0f}">'
                          f'{pf:.2f}<br>'
                          f'<small style="color:#94a3b8">({n})</small></td>')
        matrix_rows += f'<tr><td><strong>{strat}</strong></td>{cells}</tr>'

    # 局面ヘッダ
    regime_headers = ""
    for trend, vol in REGIMES:
        t_label = {"up": "上昇", "sideways": "横ばい", "down": "下落"}[trend]
        v_label = {"high": "高", "mid": "中", "low": "低"}[vol]
        regime_headers += f'<th>{t_label}<br><small>ボラ{v_label}</small></th>'

    # 推奨戦略マップ生成
    map_rows = ""
    for trend, vol in REGIMES:
        # 各局面で PF >= 1.3 & 取引 >= 10 の戦略を抽出
        candidates = []
        for strat in strats:
            s = matrix[strat].get((trend, vol), {})
            if s.get("n", 0) >= 10 and s.get("pf", 0) >= 1.3:
                candidates.append((strat, s["pf"], s["total_pnl"]))
        candidates.sort(key=lambda x: -x[1])
        recs = ", ".join(c[0] for c in candidates[:5])
        t_label = {"up": "上昇", "sideways": "横ばい", "down": "下落"}[trend]
        v_label = {"high": "高", "mid": "中", "low": "低"}[vol]
        map_rows += (f'<tr><td>{t_label}/{v_label}</td>'
                     f'<td style="text-align:left;color:#10b981">{recs or "—"}</td></tr>')

    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; }
h1 { color: #10b981; font-size: 1.5rem; }
h2 { color: #10b981; font-size: 1.1rem; margin-top: 24px;
     border-left: 3px solid #10b981; padding-left: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem;
        margin-bottom: 14px; }
th { background: #1e293b; color: #94a3b8; padding: 6px 8px;
     text-align: center; border: 1px solid #334155; }
td { padding: 8px 10px; border: 1px solid #1e293b;
     text-align: center; }
.subtitle { color: #94a3b8; font-size: 0.85rem; margin-bottom: 20px; }
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>戦略×相場 適合度 — {today}</title>
<style>{css}</style></head><body>
<h1>🎯 戦略 × 相場局面 適合度マトリックス</h1>
<p class="subtitle">生成: {today} / 期間: 過去{args.days}日 / 戦略数: {len(strats)} /
   WATCHLIST: {len(watchlist)}件</p>

<h2>マトリックス (各セル: PF / 取引数)</h2>
<table>
  <thead><tr><th>戦略\\局面</th>{regime_headers}</tr></thead>
  <tbody>{matrix_rows}</tbody>
</table>
<p style="color:#64748b;font-size:0.78rem">
  💡 色: 緑=PF≥1.5 / 黄=PF≥1.0 / 赤=PF<1.0 / グレー=取引なし<br>
  💡 ホバーで詳細 (取引数・勝率・損益)
</p>

<h2>推奨戦略マップ (PF≥1.3 & 取引≥10)</h2>
<table>
  <thead><tr><th>局面</th><th>推奨戦略 (PF順)</th></tr></thead>
  <tbody>{map_rows}</tbody>
</table>
<p style="color:#64748b;font-size:0.78rem">
  💡 このマップを REGIME_STRATEGY_MAP (nikkei_filter_daytrade.py) と比較<br>
  💡 大きく異なる場合は、現状のマップ更新を検討
</p>
</body></html>"""

    out = Path(f"market_fit_daytrade_{today}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
