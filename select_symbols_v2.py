"""
V2 銘柄選定スクリプト — 3戦略 × 4期間バックテスト自動実行・銘柄選定
────────────────────────────────────────────────────────────────────────
実行:
  python select_symbols_v2.py             # 全戦略 × 4期間 でバックテスト
  python select_symbols_v2.py --macd      # MACD のみ
  python select_symbols_v2.py --a7        # A7 のみ
  python select_symbols_v2.py --rsi2      # RSI2 のみ
  python select_symbols_v2.py --top 25    # 各戦略 25 銘柄選定

出力:
  - コンソールに各戦略の推奨銘柄一覧を表示
  - symbols_watch_macd_v2.py    (MACD 推奨銘柄ファイル)
  - symbols_watch_a7_v2.py      (A7 推奨銘柄ファイル)
  - symbols_watch_rsi2_v2.py    (RSI2 推奨銘柄ファイル)
  - select_v2_report.html       (総合 HTML レポート・自動で開く)
"""

import io
import sys

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import importlib
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── 各戦略モジュール（起動時に v1/v2 を切替） ─────────────────
import backtest_macd_scan_v2         as macd_mod
import backtest_stoch_atr_trail_v2   as a7_mod
import rsi2_hv_v2                    as rsi2_mod

PERIODS = [
    (30,  "1ヶ月"),
    (90,  "3ヶ月"),
    (180, "6ヶ月"),
    (365, "1年"),
]

WORKERS = 16


# ══════════════════════════════════════════════════════════════════════
# MACD バックテスト
# ══════════════════════════════════════════════════════════════════════

def run_macd_period(sym: str, name: str, df, days: int) -> dict | None:
    """1銘柄・1期間の MACD バックテスト結果を返す"""
    try:
        r = macd_mod.run_backtest(sym, name, df, days)
        if r is None:
            return None
        return {
            "symbol":    sym,
            "name":      name,
            "trades":    r["trades"],
            "win_rate":  r["win_rate"],
            "ret_pct":   r["ret_pct"],
            "pf":        r["pf"],
            "avg_hold":  r["avg_hold"],
            "trade_log": r.get("trade_log", []),
        }
    except Exception:
        return None


def _is_valid_df(df) -> bool:
    """DataFrameが正常なデータを含んでいるか検証（価格変動あり）"""
    if df is None or df.empty or len(df) < 10:
        return False
    close = df["close"]
    price_range = float(close.max() - close.min())
    return price_range > 0.01 * float(close.mean())


def run_macd_all(target: list[tuple], top_n: int) -> tuple[list[dict], dict]:
    """全対象銘柄 × 4期間 MACD バックテストを実行し、(推奨銘柄リスト, stock_data) を返す"""
    total = len(target)
    print(f"\n  [MACD] データ取得中 ({total}銘柄)...")
    stock_data: dict[str, tuple] = {}
    for i, (sym, name) in enumerate(target, 1):
        df = macd_mod.fetch_df(sym, backtest_days=365)
        if df is not None and _is_valid_df(df):
            stock_data[sym] = (name, df)
        print(f"  {i}/{total}", end="\r", flush=True)
    print(f"  完了 {len(stock_data)}銘柄      ")

    # 4期間バックテスト
    scores: dict[str, dict] = {}
    for days, label in PERIODS:
        print(f"  [MACD] {label} バックテスト中...")
        tasks = [(s, n, d, days) for s, (n, d) in stock_data.items()]

        def _bt(task):
            return run_macd_period(task[0], task[1], task[2], task[3])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_bt, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                sym = r["symbol"]
                if sym not in scores:
                    scores[sym] = {
                        "symbol": sym, "name": r["name"],
                        "periods": {}, "score": 0,
                    }
                scores[sym]["periods"][label] = r

    # スコアリング + 直近価格を d に格納
    for d in scores.values():
        sc = 0
        ret_sum = 0.0
        for label, r in d["periods"].items():
            if r["ret_pct"] > 0:  sc += 3
            if r["win_rate"] >= 60: sc += 2
            if r["trades"] >= 2:    sc += 1
            if r["pf"] >= 1.5:      sc += 1
            ret_sum += r["ret_pct"]
        d["score"]   = sc
        d["ret_sum"] = ret_sum
        d["pos_cnt"] = sum(1 for r in d["periods"].values() if r["ret_pct"] > 0)
        # 直近の始値・終値を銘柄ごとに格納
        sym = d["symbol"]
        if sym in stock_data:
            _df = stock_data[sym][1]
            if not _df.empty:
                d["last_open"]  = float(_df.iloc[-1]["open"])
                d["last_close"] = float(_df.iloc[-1]["close"])

    ranked = sorted(scores.values(),
                    key=lambda x: (-x["score"], -x["ret_sum"]))
    return ranked[:top_n], stock_data
# ══════════════════════════════════════════════════════════════════════

def run_a7_period(sym: str, name: str, df, days: int) -> dict | None:
    try:
        r = a7_mod.run_backtest(sym, name, df, days)
        if r is None:
            return None
        tlog = r.get("trade_log", [])
        pf = (sum(t["pnl"] for t in tlog if t["pnl"] > 0) /
              abs(sum(t["pnl"] for t in tlog if t["pnl"] < 0))
              if any(t["pnl"] < 0 for t in tlog) else float("inf"))
        return {
            "symbol":    sym,
            "name":      name,
            "trades":    r["trades"],
            "win_rate":  r["win_rate"],
            "ret_pct":   r["ret_pct"],
            "pf":        pf,
            "avg_hold":  r["avg_hold"],
            "trade_log": tlog,
        }
    except Exception:
        return None


def run_a7_all(target: list[tuple], top_n: int) -> tuple[list[dict], dict]:
    total = len(target)
    print(f"\n  [A7] データ取得中 ({total}銘柄)...")
    stock_data: dict[str, tuple] = {}
    for i, (sym, name) in enumerate(target, 1):
        df = a7_mod.fetch_df(sym, backtest_days=365)
        if df is not None and _is_valid_df(df):
            stock_data[sym] = (name, df)
        print(f"  {i}/{total}", end="\r", flush=True)
    print(f"  完了 {len(stock_data)}銘柄      ")

    scores: dict[str, dict] = {}
    for days, label in PERIODS:
        print(f"  [A7] {label} バックテスト中...")
        tasks = [(s, n, d, days) for s, (n, d) in stock_data.items()]

        def _bt(task):
            return run_a7_period(task[0], task[1], task[2], task[3])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_bt, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                sym = r["symbol"]
                if sym not in scores:
                    scores[sym] = {
                        "symbol": sym, "name": r["name"],
                        "periods": {}, "score": 0,
                    }
                scores[sym]["periods"][label] = r

    for d in scores.values():
        sc = 0
        ret_sum = 0.0
        for label, r in d["periods"].items():
            if r["ret_pct"] > 0:    sc += 3
            if r["win_rate"] >= 60: sc += 2
            if r["trades"] >= 2:    sc += 1
            if r["pf"] >= 1.5:      sc += 1
            ret_sum += r["ret_pct"]
        d["score"]   = sc
        d["ret_sum"] = ret_sum
        d["pos_cnt"] = sum(1 for r in d["periods"].values() if r["ret_pct"] > 0)
        # 直近の始値・終値を銘柄ごとに格納
        sym = d["symbol"]
        if sym in stock_data:
            _df = stock_data[sym][1]
            if not _df.empty:
                d["last_open"]  = float(_df.iloc[-1]["open"])
                d["last_close"] = float(_df.iloc[-1]["close"])

    ranked = sorted(scores.values(),
                    key=lambda x: (-x["score"], -x["ret_sum"]))
    return ranked[:top_n], stock_data


# ══════════════════════════════════════════════════════════════════════
# RSI2 バックテスト
# ══════════════════════════════════════════════════════════════════════

def run_rsi2_period(sym: str, name: str, df_raw, days: int, params: dict) -> dict | None:
    try:
        df_c   = rsi2_mod.calc(df_raw)
        trades = rsi2_mod.backtest_hv(
            df_c, days, params,
            use_ibs=True, use_consec=False,
        )
        if not trades:
            return None
        wins     = [t for t in trades if t["pnl"] > 0]
        loss     = [t for t in trades if t["pnl"] <= 0]
        total    = sum(t["pnl"] for t in trades)
        ret_pct  = sum(t["pct"] for t in trades)
        wr       = len(wins) / len(trades) * 100
        pf       = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                    if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        avg_hold = sum(t["hold"] for t in trades) / len(trades)
        return {
            "symbol":    sym,
            "name":      name,
            "trades":    len(trades),
            "win_rate":  wr,
            "ret_pct":   ret_pct,
            "pf":        pf,
            "avg_hold":  avg_hold,
            "trade_log": trades,
        }
    except Exception:
        return None


def run_rsi2_all(target: list[tuple], top_n: int) -> tuple[list[dict], dict, dict, str]:
    # 日経データでモード判定
    print(f"\n  [RSI2] 日経データ取得・モード判定中...")
    nk_df  = rsi2_mod.fetch_nikkei(365)
    mkt    = rsi2_mod._market_info(nk_df)
    if mkt.get("ok") and mkt["above200"]:
        params     = rsi2_mod.NORMAL
        mode_label = "通常モード"
    else:
        params     = rsi2_mod.HV
        mode_label = "高ボラモード"
    print(f"  モード: {mode_label}  (RSI2_ENTRY={params['RSI2_ENTRY']})")

    total = len(target)
    print(f"  [RSI2] データ取得中 ({total}銘柄)...")
    stock_data: dict[str, tuple] = {}
    for i, (sym, name) in enumerate(target, 1):
        df = rsi2_mod.fetch(sym, backtest_days=365)
        if df is not None and _is_valid_df(df):
            stock_data[sym] = (name, df)
        print(f"  {i}/{total}", end="\r", flush=True)
    print(f"  完了 {len(stock_data)}銘柄      ")

    scores: dict[str, dict] = {}
    for days, label in PERIODS:
        print(f"  [RSI2] {label} バックテスト中...")
        tasks = [(s, n, d, days, params) for s, (n, d) in stock_data.items()]

        def _bt(task):
            return run_rsi2_period(task[0], task[1], task[2], task[3], task[4])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_bt, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                sym = r["symbol"]
                if sym not in scores:
                    scores[sym] = {
                        "symbol": sym, "name": r["name"],
                        "periods": {}, "score": 0,
                    }
                scores[sym]["periods"][label] = r

    for d in scores.values():
        sc = 0
        ret_sum = 0.0
        for label, r in d["periods"].items():
            if r["ret_pct"] > 0:    sc += 3
            if r["win_rate"] >= 60: sc += 2
            if r["trades"] >= 2:    sc += 1
            if r["pf"] >= 1.5:      sc += 1
            ret_sum += r["ret_pct"]
        d["score"]   = sc
        d["ret_sum"] = ret_sum
        d["pos_cnt"] = sum(1 for r in d["periods"].values() if r["ret_pct"] > 0)
        # 直近の始値・終値を銘柄ごとに格納
        sym = d["symbol"]
        if sym in stock_data:
            _df = stock_data[sym][1]
            if not _df.empty:
                d["last_open"]  = float(_df.iloc[-1]["open"])
                d["last_close"] = float(_df.iloc[-1]["close"])

    ranked = sorted(scores.values(),
                    key=lambda x: (-x["score"], -x["ret_sum"]))
    return ranked[:top_n], stock_data, params, mode_label


# ══════════════════════════════════════════════════════════════════════
# 出力: symbols_watch_*_v2.py
# ══════════════════════════════════════════════════════════════════════

def write_symbols_file(selected: list[dict], strategy: str, filename: str) -> Path:
    """選定銘柄を symbols_watch_*_v2.py として書き出す"""
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [
        f'"""',
        f'{strategy} 戦略 監視対象銘柄 V2 — 4期間バックテスト選定 ({today})',
        f'select_symbols_v2.py により自動生成',
        f'"""',
        f'SYMBOLS = [',
    ]

    # スコア別にコメントを付ける
    for d in selected:
        pos = d["pos_cnt"]
        ret = d["ret_sum"]
        if pos == 4:
            comment = f"# ★4期間全プラス  累積{ret:+.1f}%"
        elif pos == 3:
            comment = f"# ★3期間プラス    累積{ret:+.1f}%"
        elif pos == 2:
            comment = f"# 2期間プラス      累積{ret:+.1f}%"
        else:
            comment = f"# 1期間プラス      累積{ret:+.1f}%"
        lines.append(f'    ("{d["symbol"]}", "{d["name"]}"),  {comment}')

    lines.append(']')
    path = Path(filename)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# コンソール表示
# ══════════════════════════════════════════════════════════════════════

def print_results(selected: list[dict], strategy: str, top_n: int) -> None:
    print()
    print("=" * 76)
    print(f"  {strategy} 推奨銘柄 TOP{top_n}  (4期間スコア順)")
    print("=" * 76)
    print(f"  {'#':<3} {'銘柄':<24} {'スコア':>5} {'期間+':>5}  "
          f"{'1M%':>7} {'3M%':>7} {'6M%':>7} {'1Y%':>7}  1M取引  3M取引  6M取引  1Y取引")
    print("  " + "-" * 96)

    for i, d in enumerate(selected, 1):
        p = d["periods"]
        def _r(label):
            r = p.get(label)
            if r is None:
                return "    N/A", "  -"
            sign = "+" if r["ret_pct"] >= 0 else ""
            return f"{sign}{r['ret_pct']:>6.1f}%", f"{r['trades']:>3}回"

        r1m_s, t1m = _r("1ヶ月")
        r3m_s, t3m = _r("3ヶ月")
        r6m_s, t6m = _r("6ヶ月")
        r1y_s, t1y = _r("1年")

        mark = "★" if d["pos_cnt"] >= 3 else " "
        label = f"{d['name']}({d['symbol']})"
        print(f" {mark}{i:<3} {label:<24} {d['score']:>5}  {d['pos_cnt']}/4  "
              f"{r1m_s} {r3m_s} {r6m_s} {r1y_s}   "
              f"{t1m}  {t3m}  {t6m}  {t1y}")

    print()


# ══════════════════════════════════════════════════════════════════════
# シグナルスキャン（選定銘柄を対象）
# ══════════════════════════════════════════════════════════════════════

def _load_watch_file(path: str) -> list[tuple[str, str]]:
    """symbols_watch_*_v2.py からシンボルリストを読み込む"""
    p = Path(path)
    if not p.exists():
        return []
    import importlib.util
    spec = importlib.util.spec_from_file_location("_watch_tmp", p)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(getattr(mod, "SYMBOLS", []))


def _fetch_signal_data(symbols: list[tuple], fetch_fn) -> dict:
    """シグナルスキャン専用: watchlistのデータを取得（バックテストなし）"""
    stock_data: dict = {}
    print(f"  データ取得中... ({len(symbols)}銘柄)")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_fn, sym, backtest_days=365): (sym, name)
                for sym, name in symbols}
        for fut in as_completed(futs):
            sym, name = futs[fut]
            try:
                df = fut.result()
                if df is not None and _is_valid_df(df):
                    stock_data[sym] = (name, df)
            except Exception:
                pass
    return stock_data


def _selected_data_map(selected: list[dict], stock_data: dict) -> dict:
    """選定銘柄のみの stock_data_map を返す"""
    return {d["symbol"]: stock_data[d["symbol"]]
            for d in selected if d["symbol"] in stock_data}


def _run_90d_backtest_list(selected: list[dict], stock_data: dict,
                           bt_fn) -> list[dict]:
    """選定銘柄の90日バックテスト結果リストを返す（保有中検出用）"""
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {
            ex.submit(bt_fn, d["symbol"], d["name"],
                      stock_data[d["symbol"]][1], 90): d["symbol"]
            for d in selected if d["symbol"] in stock_data
        }
        for fut in as_completed(futs):
            r = fut.result()
            if r is not None:
                results.append(r)
    return results


def scan_macd_signals(selected: list[dict], stock_data: dict) -> dict:
    """選定MACD銘柄のシグナルスキャン"""
    print("  [MACD] シグナルスキャン中...")
    sdm      = _selected_data_map(selected, stock_data)
    results  = _run_90d_backtest_list(selected, stock_data, macd_mod.run_backtest)
    nikkei   = macd_mod.fetch_nikkei(90)
    return macd_mod.scan_today_signals(sdm, results, nikkei)


def scan_a7_signals(selected: list[dict], stock_data: dict) -> dict:
    """選定A7銘柄のシグナルスキャン"""
    print("  [A7] シグナルスキャン中...")
    sdm     = _selected_data_map(selected, stock_data)
    results = _run_90d_backtest_list(selected, stock_data, a7_mod.run_backtest)
    return a7_mod.scan_signals_a7(sdm, results)


def scan_rsi2_signals(selected: list[dict], stock_data: dict,
                      params: dict) -> dict:
    """選定RSI2銘柄のシグナルスキャン"""
    print("  [RSI2] シグナルスキャン中...")
    sdm     = _selected_data_map(selected, stock_data)
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {
            ex.submit(
                rsi2_mod.backtest_hv,
                rsi2_mod.calc(stock_data[d["symbol"]][1]),
                90, params, True, False,
            ): d for d in selected if d["symbol"] in stock_data
        }
        for fut in as_completed(futs):
            d = futs[fut]
            trades = fut.result()
            if trades:
                results.append({"symbol": d["symbol"], "name": d["name"],
                                "trade_log": [{"reason": t.get("reason", "")}
                                              for t in trades]})
    return rsi2_mod.scan_signals_rsi2(sdm, results, params,
                                       use_ibs=True, use_consec=False,
                                       vix_rsi=None)


def _signal_section_html(sig: dict | None, strategy: str, color: str) -> str:
    """シグナル結果のHTMLセクションを生成"""
    if sig is None:
        return ""
    buy   = sig.get("buy",  [])
    sell  = sig.get("sell", [])
    hold  = sig.get("hold", [])
    today = sig.get("today", "")

    def _extra_cols(s: dict) -> str:
        """戦略固有の指標を追加列として返す"""
        cols = ""
        if "macd_hist" in s:
            cls = "pos" if s["macd_hist"] >= 0 else "neg"
            cols += f'<td class="num {cls}">{s["macd_hist"]:+.3f}</td>'
            cols += f'<td class="num">{s.get("rsi", 0):.1f}</td>'
            cols += f'<td class="num">{s.get("vol_ratio", 0):.1f}x</td>'
        elif "stoch_k" in s:
            cols += f'<td class="num">{s["stoch_k"]:.1f}</td>'
            cols += f'<td class="num">{s.get("stoch_d", 0):.1f}</td>'
            cols += f'<td class="num">{s.get("atr_pct", 0):.1f}%</td>'
        elif "rsi2" in s:
            cols += f'<td class="num pos">{s["rsi2"]:.1f}</td>'
            cols += f'<td class="num">{s.get("ibs", 0):.2f}</td>'
            cols += f'<td class="num">{s.get("atr_pct", 0):.1f}%</td>'
        return cols

    def _extra_headers(items: list) -> str:
        if not items:
            return ""
        s = items[0]
        if "macd_hist" in s:
            return "<th>MACD</th><th>RSI</th><th>出来高比</th>"
        elif "stoch_k" in s:
            return "<th>%K</th><th>%D</th><th>ATR%</th>"
        elif "rsi2" in s:
            return "<th>RSI2</th><th>IBS</th><th>ATR%</th>"
        return ""

    all_items = buy + sell + hold
    extra_h = _extra_headers(all_items)

    def _buy_rows(items):
        if not items:
            return f'<tr><td colspan="6" style="color:#64748b;text-align:center">買いシグナルなし</td></tr>'
        rows = ""
        for s in items:
            open_s  = f'{s["open"]:,.0f}' if "open" in s else "—"
            close_s = f'{s["close"]:,.0f}' if "close" in s else "—"
            rows += (f'<tr class="buy-row">'
                     f'<td class="name">{s.get("name","")}<br><small>{s.get("symbol","")}</small></td>'
                     f'<td class="num">{open_s}</td>'
                     f'<td class="num">{close_s}</td>'
                     + _extra_cols(s) +
                     f'<td style="color:#4ade80;font-weight:700">買い</td></tr>\n')
        return rows

    def _sell_rows(items, label, cls):
        if not items:
            return ""
        rows = ""
        for s in items:
            open_s    = f'{s["open"]:,.0f}' if "open" in s else "—"
            close_s   = f'{s["close"]:,.0f}' if "close" in s else "—"
            entry_s   = f'{s["entry_price"]:,.0f}' if "entry_price" in s else "—"
            unreal    = s.get("unrealized", 0)
            u_cls     = "pos" if unreal >= 0 else "neg"
            sign      = "+" if unreal >= 0 else ""
            hold_days = s.get("hold_days", "—")
            rows += (f'<tr class="{cls}">'
                     f'<td class="name">{s.get("name","")}<br><small>{s.get("symbol","")}</small></td>'
                     f'<td class="num">{open_s}</td>'
                     f'<td class="num">{close_s}</td>'
                     + _extra_cols(s) +
                     f'<td class="num">{entry_s}</td>'
                     f'<td class="num {u_cls}">{sign}{unreal:.1f}%</td>'
                     f'<td class="num">{hold_days}日</td>'
                     f'<td style="color:#f87171;font-weight:700">{label}</td></tr>\n')
        return rows

    buy_body  = _buy_rows(buy)
    sell_body = _sell_rows(sell, "売り", "sell-row") + _sell_rows(hold, "保有継続", "hold-row")

    sell_section = ""
    if sell or hold:
        sell_section = (
            f'<table style="margin-top:6px"><thead><tr>'
            f'<th>銘柄</th><th>始値</th><th>終値</th>{extra_h}'
            f'<th>買値</th><th>含み損益</th><th>保有日</th><th>区分</th>'
            f'</tr></thead><tbody>{sell_body}</tbody></table>'
        )

    return (
        f'<h2 style="color:{color};border-left:4px solid {color};padding-left:10px;margin:30px 0 10px">'
        f'  {strategy} 本日シグナル（選定銘柄対象） — {today}</h2>'
        f'<table><thead><tr>'
        f'<th>銘柄</th><th>始値</th><th>終値</th>{extra_h}<th>区分</th>'
        f'</tr></thead><tbody>{buy_body}</tbody></table>'
        + sell_section
    )


# ══════════════════════════════════════════════════════════════════════
# HTML レポート生成
# ══════════════════════════════════════════════════════════════════════

def _strategy_table_html(selected: list[dict], strategy: str, color: str,
                          stock_data: dict | None = None) -> str:
    rows = ""
    for i, d in enumerate(selected, 1):
        p   = d["periods"]
        sym = d["symbol"]

        # 直近の始値・終値（スコアリングループで各銘柄ごとに格納済み）
        open_val  = d.get("last_open")
        close_val = d.get("last_close")

        def cell(label):
            r = p.get(label)
            if r is None:
                return '<td class="num" style="color:#555">N/A</td><td class="num" style="color:#555">-</td><td class="num" style="color:#555">-</td>'
            cls  = "pos" if r["ret_pct"] >= 0 else "neg"
            sign = "+" if r["ret_pct"] >= 0 else ""
            pf_s = "∞" if r["pf"] == float("inf") else f'{r["pf"]:.1f}'
            return (f'<td class="num {cls}">{sign}{r["ret_pct"]:.1f}%</td>'
                    f'<td class="num">{r["trades"]}回</td>'
                    f'<td class="num">{r["win_rate"]:.0f}%/{pf_s}</td>')

        # 期間別取引履歴のトグル行
        detail_rows = ""
        for period_label, r in p.items():
            tlog = r.get("trade_log", [])
            if not tlog:
                continue
            for t in tlog:
                entry_dt  = t.get("entry_dt", t.get("entry_date", ""))
                exit_dt   = t.get("exit_dt",  t.get("exit_date",  ""))
                entry_p   = t.get("entry_price", t.get("entry_p", 0))
                exit_p    = t.get("exit_price",  t.get("exit_p",  0))
                pnl       = t.get("pnl", 0)
                hold      = t.get("hold_days", t.get("hold", 0))
                reason    = t.get("reason", "")
                pct       = (exit_p - entry_p) / entry_p * 100 if entry_p else 0
                t_cls     = "pos" if pnl >= 0 else "neg"
                sign_t    = "+" if pnl >= 0 else ""
                # datetime を文字列に変換
                if hasattr(entry_dt, "strftime"):
                    entry_dt = entry_dt.strftime("%Y-%m-%d")
                if hasattr(exit_dt, "strftime"):
                    exit_dt = exit_dt.strftime("%Y-%m-%d")
                detail_rows += (
                    f'<tr style="background:#060e1a">'
                    f'<td colspan="2" style="color:#64748b;padding-left:20px">{period_label}</td>'
                    f'<td class="num" style="color:#94a3b8">{entry_dt}</td>'
                    f'<td class="num" style="color:#94a3b8">{exit_dt}</td>'
                    f'<td class="num">{entry_p:,.0f}</td>'
                    f'<td class="num">{exit_p:,.0f}</td>'
                    f'<td class="num {t_cls}">{sign_t}{pct:.1f}%</td>'
                    f'<td class="num">{hold}日</td>'
                    f'<td colspan="5" style="color:#64748b">{reason}</td>'
                    f'</tr>\n'
                )

        mark   = "★" if d["pos_cnt"] >= 3 else ""
        label  = f'{d["name"]}<br><small>{sym}</small>'
        open_s  = f'{open_val:,.0f}' if open_val else "—"
        close_s = f'{close_val:,.0f}' if close_val else "—"

        rows += (
            f'<tr class="stock-row" onclick="toggleDetail(\'{sym}\')" style="cursor:pointer">'
            f'<td class="rank">{i}</td>'
            f'<td class="name">{mark}{label}</td>'
            f'<td class="num">{open_s}</td>'
            f'<td class="num">{close_s}</td>'
            f'<td class="num">{d["score"]}</td>'
            f'<td class="num">{d["pos_cnt"]}/4</td>'
            + cell("1ヶ月") + cell("3ヶ月") + cell("6ヶ月") + cell("1年") +
            f'<td class="expand-btn">▼</td>'
            f'</tr>\n'
            f'<tr id="detail-{sym}" style="display:none">'
            f'<td colspan="16" style="padding:0">'
            f'<table style="width:100%;font-size:.8em;border-collapse:collapse">'
            f'<thead><tr style="background:#162032">'
            f'<th colspan="2">期間</th><th>エントリー</th><th>エグジット</th>'
            f'<th>買値</th><th>売値</th><th>損益%</th><th>保有日</th>'
            f'<th colspan="5">決済理由</th></tr></thead>'
            f'<tbody>{detail_rows if detail_rows else "<tr><td colspan=13 style=color:#555;text-align:center>履歴なし</td></tr>"}</tbody>'
            f'</table></td></tr>\n'
        )

    return f"""
<h2 style="color:{color};border-left:4px solid {color};padding-left:10px;margin:30px 0 10px">
  {strategy} 推奨銘柄 <span style="font-size:.8em;color:#64748b;font-weight:400">（銘柄行をクリックで取引履歴を展開）</span>
</h2>
<table>
  <thead>
    <tr>
      <th>#</th><th>銘柄</th><th>始値</th><th>終値</th><th>スコア</th><th>期間+</th>
      <th>1M%</th><th>1M回</th><th>1M勝率/PF</th>
      <th>3M%</th><th>3M回</th><th>3M勝率/PF</th>
      <th>6M%</th><th>6M回</th><th>6M勝率/PF</th>
      <th>1Y%</th><th>1Y回</th><th>1Y勝率/PF</th>
      <th></th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
"""


def _charts_html(
    macd_sel: list[dict] | None,
    a7_sel:   list[dict] | None,
    rsi2_sel: list[dict] | None,
) -> str:
    """Chart.js グラフ（期間別平均リターン + 銘柄別散布）を生成"""
    period_keys   = ["1ヶ月", "3ヶ月", "6ヶ月", "1年"]
    period_labels = ["1M",    "3M",    "6M",    "1Y"]

    import json as _json

    def _period_avg(sel):
        """期間ごとの平均リターン [%] を計算"""
        avgs = []
        for pk in period_keys:
            vals = [d["periods"][pk]["ret_pct"]
                    for d in (sel or [])
                    if pk in d.get("periods", {})]
            avgs.append(round(sum(vals) / len(vals), 2) if vals else None)
        return avgs

    def _scatter_data(sel):
        """銘柄ごとの {x: period_index, y: ret_pct, label: name} リスト"""
        points = []
        for d in (sel or []):
            for pi, pk in enumerate(period_keys):
                r = d.get("periods", {}).get(pk)
                if r:
                    points.append({
                        "x": pi, "y": round(r["ret_pct"], 2),
                        "label": f'{d["name"]}({d["symbol"]})'
                    })
        return points

    macd_avgs = _period_avg(macd_sel)
    a7_avgs   = _period_avg(a7_sel)
    rsi2_avgs = _period_avg(rsi2_sel)
    macd_pts  = _scatter_data(macd_sel)
    a7_pts    = _scatter_data(a7_sel)
    rsi2_pts  = _scatter_data(rsi2_sel)

    # 最高スコア銘柄の期間別リターン折れ線
    def _top_lines(sel, color):
        if not sel:
            return "[]"
        datasets = []
        for d in (sel or [])[:5]:
            vals = [d["periods"].get(pk, {}).get("ret_pct") for pk in period_keys]
            datasets.append({
                "label": f'{d["name"]}({d["symbol"]})',
                "data":  vals,
                "borderColor": color,
                "backgroundColor": "transparent",
                "borderWidth": 1,
                "pointRadius": 3,
            })
        return _json.dumps(datasets)

    return f"""
<h2 style="color:#e2e8f0;border-left:4px solid #6366f1;padding-left:10px;margin:30px 0 12px">
  期間別リターン サマリーグラフ
</h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">

  <!-- 戦略比較: 期間別平均リターン -->
  <div style="background:#1e293b;border-radius:8px;padding:16px">
    <p style="color:#94a3b8;font-size:.8em;margin-bottom:8px">
      戦略×期間 平均リターン（選定銘柄 TOP{len(macd_sel or a7_sel or rsi2_sel or [])}の平均）
    </p>
    <canvas id="chartAvg" style="max-height:260px"></canvas>
  </div>

  <!-- 銘柄散布: 各期間のリターン分布 -->
  <div style="background:#1e293b;border-radius:8px;padding:16px">
    <p style="color:#94a3b8;font-size:.8em;margin-bottom:8px">
      銘柄別リターン分布（各点 = 1銘柄×1期間）
    </p>
    <canvas id="chartScatter" style="max-height:260px"></canvas>
  </div>

  <!-- MACD上位銘柄: 期間別折れ線 -->
  <div style="background:#1e293b;border-radius:8px;padding:16px">
    <p style="color:#94a3b8;font-size:.8em;margin-bottom:8px">MACD 上位5銘柄 期間別リターン</p>
    <canvas id="chartMacd" style="max-height:260px"></canvas>
  </div>

  <!-- A7/RSI2上位銘柄: 期間別折れ線 -->
  <div style="background:#1e293b;border-radius:8px;padding:16px">
    <p style="color:#94a3b8;font-size:.8em;margin-bottom:8px">A7 / RSI2 上位5銘柄 期間別リターン</p>
    <canvas id="chartA7rsi2" style="max-height:260px"></canvas>
  </div>

</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
(function(){{
  const labs = {_json.dumps(period_labels)};
  const gridC = '#334155', tickC = '#94a3b8';

  function axOpts(title) {{
    return {{
      grid: {{color: gridC}},
      ticks: {{color: tickC}},
      title: {{display: !!title, text: title, color: tickC, font: {{size:11}}}}
    }};
  }}

  // ── 戦略比較棒グラフ ──────────────────────────
  new Chart(document.getElementById('chartAvg'), {{
    type: 'bar',
    data: {{
      labels: labs,
      datasets: [
        {{label:'MACD', data:{_json.dumps(macd_avgs)}, backgroundColor:'rgba(56,189,248,0.7)', borderColor:'#38bdf8', borderWidth:1}},
        {{label:'A7',   data:{_json.dumps(a7_avgs)},   backgroundColor:'rgba(74,222,128,0.7)', borderColor:'#4ade80', borderWidth:1}},
        {{label:'RSI2', data:{_json.dumps(rsi2_avgs)}, backgroundColor:'rgba(245,158,11,0.7)', borderColor:'#f59e0b', borderWidth:1}},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:true,
      plugins: {{legend:{{labels:{{color:tickC}}}}, tooltip:{{callbacks:{{label:ctx=>ctx.dataset.label+': '+(ctx.parsed.y!=null?ctx.parsed.y.toFixed(1)+'%':'N/A')}}}}}},
      scales: {{
        x: axOpts('期間'),
        y: {{...axOpts('平均リターン (%)'), ticks: {{color:tickC, callback:v=>v+'%'}}}}
      }}
    }}
  }});

  // ── 散布図 ──────────────────────────────────
  const pMap = {{'0':'1M','1':'3M','2':'6M','3':'1Y'}};
  new Chart(document.getElementById('chartScatter'), {{
    type: 'scatter',
    data: {{
      datasets: [
        {{label:'MACD', data:{_json.dumps(macd_pts)}, backgroundColor:'rgba(56,189,248,0.5)'}},
        {{label:'A7',   data:{_json.dumps(a7_pts)},   backgroundColor:'rgba(74,222,128,0.5)'}},
        {{label:'RSI2', data:{_json.dumps(rsi2_pts)}, backgroundColor:'rgba(245,158,11,0.5)'}},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:true,
      plugins: {{
        legend:{{labels:{{color:tickC}}}},
        tooltip:{{callbacks:{{label:ctx=>ctx.raw.label+' '+pMap[ctx.raw.x]+': '+(ctx.raw.y?.toFixed(1)??'N/A')+'%'}}}}
      }},
      scales: {{
        x: {{...axOpts('期間'), min:-0.5, max:3.5,
              ticks:{{color:tickC, callback:v=>(['1M','3M','6M','1Y'][Math.round(v)]||'')}}}},
        y: {{...axOpts('リターン (%)'), ticks:{{color:tickC, callback:v=>v+'%'}}}}
      }}
    }}
  }});

  // ── MACD折れ線 ──────────────────────────────
  new Chart(document.getElementById('chartMacd'), {{
    type: 'line',
    data: {{labels: labs, datasets: {_top_lines(macd_sel, "#38bdf8")}}},
    options: {{
      responsive:true, maintainAspectRatio:true,
      plugins:{{legend:{{labels:{{color:tickC,font:{{size:10}}}}}},
               tooltip:{{callbacks:{{label:ctx=>ctx.dataset.label+': '+(ctx.parsed.y!=null?ctx.parsed.y.toFixed(1)+'%':'N/A')}}}}}},
      scales:{{ x:axOpts(), y:{{...axOpts('リターン (%)'), ticks:{{color:tickC,callback:v=>v+'%'}}}}}}
    }}
  }});

  // ── A7+RSI2折れ線 ──────────────────────────
  const a7ds   = {_top_lines(a7_sel,   "#4ade80")};
  const rsi2ds = {_top_lines(rsi2_sel, "#f59e0b")};
  new Chart(document.getElementById('chartA7rsi2'), {{
    type: 'line',
    data: {{labels: labs, datasets: [...a7ds, ...rsi2ds]}},
    options: {{
      responsive:true, maintainAspectRatio:true,
      plugins:{{legend:{{labels:{{color:tickC,font:{{size:10}}}}}},
               tooltip:{{callbacks:{{label:ctx=>ctx.dataset.label+': '+(ctx.parsed.y!=null?ctx.parsed.y.toFixed(1)+'%':'N/A')}}}}}},
      scales:{{ x:axOpts(), y:{{...axOpts('リターン (%)'), ticks:{{color:tickC,callback:v=>v+'%'}}}}}}
    }}
  }});
}})();
</script>
"""


def analyze_cross_strategy(
    macd_sel: list[dict] | None,
    a7_sel:   list[dict] | None,
    rsi2_sel: list[dict] | None,
) -> list[dict]:
    """3戦略横断で監視銘柄をスコアリングして返す。
    戻り値: [{"symbol", "name", "strategies": [...], "ranks": {...}, "n_strat", "total_score"}, ...]
    """
    all_syms: dict[str, dict] = {}
    for sel, label in [(macd_sel, "MACD"), (a7_sel, "A7"), (rsi2_sel, "RSI2")]:
        for rank, d in enumerate(sel or [], 1):
            sym = d["symbol"]
            if sym not in all_syms:
                all_syms[sym] = {
                    "symbol": sym, "name": d["name"],
                    "strategies": [], "ranks": {},
                    "periods": d.get("periods", {}),
                    "score": d.get("score", 0),
                    "ret_sum": d.get("ret_sum", 0.0),
                    "pos_cnt": d.get("pos_cnt", 0),
                    "last_open":  d.get("last_open"),
                    "last_close": d.get("last_close"),
                }
            all_syms[sym]["strategies"].append(label)
            all_syms[sym]["ranks"][label] = rank

    for d in all_syms.values():
        n = len(d["strategies"])
        rank_score = sum(21 - r for r in d["ranks"].values())
        d["n_strat"]     = n
        d["rank_score"]  = rank_score
        d["total_score"] = n * 100 + rank_score

    return sorted(all_syms.values(), key=lambda x: -x["total_score"])


def print_watchlist(watch: list[dict]) -> None:
    """横断分析結果をコンソールに表示"""
    print()
    print("=" * 70)
    print("  ★ 横断分析 監視銘柄 推奨リスト")
    print("=" * 70)

    tiers = [
        ("★★★ 3戦略一致 — 最優先",  [d for d in watch if d["n_strat"] == 3]),
        ("★★  2戦略一致 — 高優先度", [d for d in watch if d["n_strat"] == 2]),
        ("★   1戦略 上位5位以内",    [d for d in watch
                                       if d["n_strat"] == 1 and min(d["ranks"].values()) <= 5]),
    ]
    for title, items in tiers:
        if not items:
            continue
        print(f"\n  [{title}]")
        print(f"  {'銘柄':<22} {'コード':<10} {'戦略(ランク)'}")
        print("  " + "-" * 56)
        for d in items:
            ranks_str = "  ".join(f"{k}:{v}位" for k, v in d["ranks"].items())
            print(f"  {d['name']:<22} {d['symbol']:<10} {ranks_str}")
    print()


def _watchlist_section_html(watch: list[dict]) -> str:
    """横断分析結果の HTML セクションを生成"""
    if not watch:
        return ""

    def _tier(n):
        if n == 3: return "★★★", "#f59e0b", "3戦略一致"
        if n == 2: return "★★",  "#4ade80", "2戦略一致"
        return "★",    "#38bdf8", "1戦略"

    _bg  = {"MACD": "#1e3a5f", "A7": "#1a3d2b", "RSI2": "#3d2a00"}
    _fg  = {"MACD": "#38bdf8", "A7": "#4ade80", "RSI2": "#f59e0b"}

    rows = ""
    for i, d in enumerate(watch, 1):
        star, color, tier_label = _tier(d["n_strat"])
        strat_badges = "".join(
            f'<span style="background:{_bg[s]};color:{_fg[s]};'
            f'border-radius:3px;padding:1px 5px;font-size:.75em;margin-right:3px">'
            f'{s} #{d["ranks"][s]}</span>'
            for s in d["strategies"]
        )
        open_s  = f'{d["last_open"]:,.0f}'  if d.get("last_open")  else "—"
        close_s = f'{d["last_close"]:,.0f}' if d.get("last_close") else "—"
        ret_s   = f'+{d["ret_sum"]:.1f}%' if d["ret_sum"] >= 0 else f'{d["ret_sum"]:.1f}%'
        ret_cls = "pos" if d["ret_sum"] >= 0 else "neg"
        rows += (
            f'<tr>'
            f'<td class="rank">{i}</td>'
            f'<td style="color:{color};font-weight:700;text-align:center">{star}</td>'
            f'<td class="name">{d["name"]}<br><small>{d["symbol"]}</small></td>'
            f'<td>{strat_badges}</td>'
            f'<td class="num">{open_s}</td>'
            f'<td class="num">{close_s}</td>'
            f'<td class="num">{d["pos_cnt"]}/4</td>'
            f'<td class="num {ret_cls}">{ret_s}</td>'
            f'</tr>\n'
        )

    return f"""
<div style="background:#162032;border:1px solid #334155;border-radius:8px;padding:16px;margin-bottom:24px">
<h2 style="color:#f59e0b;border-left:4px solid #f59e0b;padding-left:10px;margin:0 0 12px">
  ★ 横断分析 監視銘柄 推奨リスト
  <span style="font-size:.75em;color:#64748b;font-weight:400;margin-left:8px">
    （複数戦略で選ばれた銘柄を優先）
  </span>
</h2>
<table>
  <thead><tr>
    <th>#</th><th>評価</th><th>銘柄</th><th>選定戦略（順位）</th>
    <th>始値</th><th>終値</th><th>期間+</th><th>累積リターン</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
"""


def generate_html(
    macd_sel:   list[dict] | None,
    a7_sel:     list[dict] | None,
    rsi2_sel:   list[dict] | None,
    macd_sig:   dict | None = None,
    a7_sig:     dict | None = None,
    rsi2_sig:   dict | None = None,
    macd_data:  dict | None = None,
    a7_data:    dict | None = None,
    rsi2_data:  dict | None = None,
    watchlist:  list[dict] | None = None,
) -> Path:
    today = datetime.today().strftime("%Y-%m-%d")

    body = ""
    # 横断分析 監視銘柄（最上部に表示）
    if watchlist:
        body += _watchlist_section_html(watchlist)
    # グラフセクション（バックテスト結果がある場合のみ）
    if macd_sel or a7_sel or rsi2_sel:
        body += _charts_html(macd_sel, a7_sel, rsi2_sel)
    # バックテストテーブル + シグナル（sel がある場合）
    if macd_sel:
        body += _strategy_table_html(macd_sel, "MACD V2", "#38bdf8", macd_data)
    if a7_sel:
        body += _strategy_table_html(a7_sel,   "A7 V2",   "#4ade80", a7_data)
    if rsi2_sel:
        body += _strategy_table_html(rsi2_sel, "RSI2 V2", "#f59e0b", rsi2_data)
    # シグナルセクションは sel の有無に関わらず表示
    if macd_sig:
        body += _signal_section_html(macd_sig,  "MACD V2", "#38bdf8")
    if a7_sig:
        body += _signal_section_html(a7_sig,    "A7 V2",   "#4ade80")
    if rsi2_sig:
        body += _signal_section_html(rsi2_sig,  "RSI2 V2", "#f59e0b")
    if not body:
        body = '<p style="color:#64748b;padding:24px">本日のシグナルはありません。</p>'

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V2 銘柄選定レポート {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Hiragino Kaku Gothic ProN',Meiryo,sans-serif;
     background:#0f172a;color:#e2e8f0;padding:24px;font-size:13px}}
h1{{font-size:1.4em;color:#fff;margin-bottom:6px}}
.subtitle{{color:#94a3b8;font-size:.85em;margin-bottom:24px}}
h2{{font-size:1.1em;margin:0}}
table{{width:100%;border-collapse:collapse;margin-bottom:10px}}
th{{background:#1e293b;padding:7px 10px;text-align:center;color:#94a3b8;
    font-weight:600;white-space:nowrap;border-bottom:1px solid #334155}}
td{{padding:7px 10px;border-bottom:1px solid #1e293b;white-space:nowrap}}
tr:hover{{background:#1e293b}}
.rank{{text-align:center;color:#64748b;font-weight:700}}
.name{{font-weight:600}}
.name small{{color:#64748b;font-weight:400;display:block}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:#4ade80}}
.neg{{color:#f87171}}
.note{{color:#94a3b8;font-size:.8em;margin-top:20px}}
.buy-row td:last-child{{color:#4ade80;font-weight:700}}
.sell-row td:last-child{{color:#f87171;font-weight:700}}
.hold-row td:last-child{{color:#94a3b8;font-weight:700}}
.stock-row:hover{{background:#1e293b;cursor:pointer}}
.expand-btn{{text-align:center;color:#475569;width:28px}}
</style>
<script>
function toggleDetail(sym){{
  var el = document.getElementById('detail-' + sym);
  el.style.display = el.style.display === 'none' ? '' : 'none';
}}
</script>
</head>
<body>
<h1>V2 銘柄選定レポート</h1>
<p class="subtitle">
  生成: {today} &nbsp;|&nbsp;
  スコア = 期間プラス×3 + 勝率60%以上×2 + 取引2回以上×1 + PF1.5以上×1<br>
  ★ = 3期間以上プラス &nbsp;|&nbsp; use_consec=False, use_ibs=True (RSI2)
</p>
{body}
<p class="note">
  ※ 各戦略の V2 パラメータ使用<br>
  ※ MACD V2: VOL_SPIKE×1.0 / RSI上限65 / MA200フィルター無効<br>
  ※ A7 V2: STOCH_K=9 / 50MA トレンドフィルター / 過熱判定80<br>
  ※ RSI2 V2: NORMAL ENTRY≤15 / HV ENTRY≤8 / 連続RSI確認なし
</p>
</body>
</html>"""

    path = Path(f"select_v2_report_{today}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    global macd_mod, a7_mod, rsi2_mod  # --v1 で差し替え可能にする

    parser = argparse.ArgumentParser(
        description="V2 銘柄選定スクリプト — 3戦略×4期間バックテスト自動実行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python select_symbols_v2.py                     # 監視リストからシグナル判定（毎日）
  python select_symbols_v2.py --backtest          # V2 フルバックテスト + 銘柄更新（週1回）
  python select_symbols_v2.py --backtest --v1     # V1（日経225）フルバックテスト
  python select_symbols_v2.py --v1                # V1 監視リストからシグナル判定
  python select_symbols_v2.py --backtest --v1 --universe 225  # V1 で 225 銘柄スキャン
  python select_symbols_v2.py --top 20            # 全戦略一括で 20 銘柄
  python select_symbols_v2.py --top-rsi2 40       # RSI2 のみ 40 銘柄に変更
  python select_symbols_v2.py --universe prime     # プライム上場銘柄
  python select_symbols_v2.py --universe standard  # プライム+スタンダード
  python select_symbols_v2.py --universe all       # 全上場銘柄
  python select_symbols_v2.py --universe 225       # 日経225のみ（デフォルト）

  ※ prime/standard/all を使う場合は先に以下を実行:
    python fetch_listed_symbols.py --market prime
""")
    parser.add_argument("--backtest", action="store_true",
                        help="フルバックテストを実行して監視銘柄を更新（週1回程度）")
    parser.add_argument("--macd",   action="store_true", help="MACD 戦略のみ（--backtest 時）")
    parser.add_argument("--a7",     action="store_true", help="A7 戦略のみ（--backtest 時）")
    parser.add_argument("--rsi2",   action="store_true", help="RSI2 戦略のみ（--backtest 時）")
    parser.add_argument("--top",    type=int, default=None,
                        help="全戦略の選定銘柄数を一括指定（--backtest 時）")
    parser.add_argument("--top-macd",  type=int, default=10,  dest="top_macd",
                        help="MACD 選定銘柄数 (default: 10, --backtest 時)")
    parser.add_argument("--top-a7",    type=int, default=25,  dest="top_a7",
                        help="A7 選定銘柄数 (default: 25, --backtest 時)")
    parser.add_argument("--top-rsi2",  type=int, default=35,  dest="top_rsi2",
                        help="RSI2 選定銘柄数 (default: 35, --backtest 時)")
    parser.add_argument("--universe", default=None,
                        choices=["225", "prime", "standard", "all"],
                        help="スキャン対象 (default: 全上場ファイルがあれば使用、なければ225, --backtest 時)")
    parser.add_argument("--v1", action="store_true",
                        help="V1 モジュール使用（日経225対象・旧パラメータ）")
    args = parser.parse_args()

    # ── V1/V2 モジュール切替 ──────────────────────────────────
    if args.v1:
        global macd_mod, a7_mod, rsi2_mod
        macd_mod = importlib.import_module("backtest_macd_scan")
        a7_mod   = importlib.import_module("backtest_stoch_atr_trail")
        rsi2_mod = importlib.import_module("rsi2_hv")
        # --v1 時のデフォルト監視リストファイル名
        macd_watch_file  = "symbols_watch_macd_v1.py"
        a7_watch_file    = "symbols_watch_a7_v1.py"
        rsi2_watch_file  = "symbols_watch_rsi2_v1.py"
        ver_label = "V1（日経225）"
        # --v1 時は universe デフォルトを 225 に
        if args.universe is None:
            args.universe = "225"
    else:
        macd_watch_file  = "symbols_watch_macd_v2.py"
        a7_watch_file    = "symbols_watch_a7_v2.py"
        rsi2_watch_file  = "symbols_watch_rsi2_v2.py"
        ver_label = "V2"

    today = datetime.today().strftime("%Y-%m-%d")

    # ══════════════════════════════════════════════════════════
    # デフォルト: 既存の監視リストからシグナルのみ出力
    # ══════════════════════════════════════════════════════════
    if not args.backtest:
        print()
        print("=" * 60)
        print(f"  シグナルスキャン [{ver_label}]  ({today})")
        print(f"  対象: 既存の監視リスト")
        print("=" * 60)

        macd_syms = _load_watch_file(macd_watch_file)
        a7_syms   = _load_watch_file(a7_watch_file)
        rsi2_syms = _load_watch_file(rsi2_watch_file)

        if not (macd_syms or a7_syms or rsi2_syms):
            print("\n  監視リストが見つかりません。")
            print("  先に以下を実行して監視銘柄を選定してください:")
            v1_flag = " --v1" if args.v1 else ""
            print(f"    python select_symbols_v2.py --backtest{v1_flag}\n")
            return

        macd_sig = a7_sig = rsi2_sig = None
        rsi2_params = rsi2_mod.NORMAL
        rsi2_mode   = "通常モード"

        if macd_syms:
            print(f"\n  [MACD] {len(macd_syms)}銘柄")
            macd_data = _fetch_signal_data(macd_syms, macd_mod.fetch_df)
            macd_sel  = [{"symbol": s, "name": n} for s, n in macd_syms
                         if s in macd_data]
            macd_sig  = scan_macd_signals(macd_sel, macd_data)
            macd_mod.print_signals(macd_sig)

        if a7_syms:
            print(f"\n  [A7] {len(a7_syms)}銘柄")
            a7_data = _fetch_signal_data(a7_syms, a7_mod.fetch_df)
            a7_sel  = [{"symbol": s, "name": n} for s, n in a7_syms
                       if s in a7_data]
            a7_sig  = scan_a7_signals(a7_sel, a7_data)
            a7_mod.print_signals_a7(a7_sig)

        if rsi2_syms:
            print(f"\n  [RSI2] {len(rsi2_syms)}銘柄")
            nk_df = rsi2_mod.fetch_nikkei(90)
            mkt   = rsi2_mod._market_info(nk_df)
            if mkt.get("ok") and mkt["above200"]:
                rsi2_params = rsi2_mod.NORMAL
                rsi2_mode   = "通常モード"
            else:
                rsi2_params = rsi2_mod.HV
                rsi2_mode   = "高ボラモード"
            print(f"  市場モード: {rsi2_mode}")
            rsi2_data = _fetch_signal_data(rsi2_syms, rsi2_mod.fetch)
            rsi2_sel  = [{"symbol": s, "name": n} for s, n in rsi2_syms
                         if s in rsi2_data]
            rsi2_sig  = scan_rsi2_signals(rsi2_sel, rsi2_data, rsi2_params)
            rsi2_mod.print_signals_rsi2(rsi2_sig, rsi2_mode, rsi2_params)

        html_path = generate_html(None, None, None,
                                   macd_sig, a7_sig, rsi2_sig,
                                   {}, {}, {}, watchlist=None)
        print(f"\n  HTMLレポート: {html_path.resolve()}")
        webbrowser.open(html_path.resolve().as_uri())
        print()
        return

    # ══════════════════════════════════════════════════════════
    # --backtest: フルバックテスト実行
    # ══════════════════════════════════════════════════════════
    run_all = not (args.macd or args.a7 or args.rsi2)
    do_macd = run_all or args.macd
    do_a7   = run_all or args.a7
    do_rsi2 = run_all or args.rsi2

    top_macd = args.top if args.top else args.top_macd
    top_a7   = args.top if args.top else args.top_a7
    top_rsi2 = args.top if args.top else args.top_rsi2

    # ── 対象銘柄ユニバースを決定 ───────────────────────────────
    universe_label = ""
    all_symbols: list[tuple]

    if args.universe == "225" or args.universe is None:
        for candidate in ["symbols_listed_prime.py",
                          "symbols_listed_standard.py",
                          "symbols_listed_all.py"]:
            p = Path(candidate)
            if p.exists() and args.universe is None:
                import importlib.util
                spec = importlib.util.spec_from_file_location("_listed", p)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                all_symbols    = mod.SYMBOLS
                universe_label = f"{candidate} ({len(all_symbols)}銘柄)"
                break
        else:
            all_symbols    = macd_mod._ALL_SYMBOLS
            universe_label = f"日経225 ({len(all_symbols)}銘柄)"
    else:
        fname = f"symbols_listed_{args.universe}.py"
        p = Path(fname)
        if not p.exists():
            print(f"\n  エラー: {fname} が見つかりません。")
            print(f"  先に以下を実行してください:")
            print(f"    python fetch_listed_symbols.py --market {args.universe}\n")
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("_listed", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        all_symbols    = mod.SYMBOLS
        universe_label = f"{fname} ({len(all_symbols)}銘柄)"

    print()
    print("=" * 60)
    print(f"  銘柄選定 + シグナル [{ver_label}]  ({today})")
    print(f"  対象ユニバース: {universe_label}")
    print(f"  期間: 1M / 3M / 6M / 1Y")
    print(f"  選定数: MACD={top_macd} / A7={top_a7} / RSI2={top_rsi2}")
    print("=" * 60)

    macd_sel = a7_sel = rsi2_sel = None
    macd_sig = a7_sig = rsi2_sig = None
    macd_data = a7_data = rsi2_data = {}
    rsi2_params = rsi2_mod.NORMAL
    rsi2_mode   = "通常モード"

    if do_macd:
        macd_sel, macd_data = run_macd_all(all_symbols, top_macd)
        print_results(macd_sel, f"MACD {ver_label}", top_macd)
        p = write_symbols_file(macd_sel, f"MACD {ver_label}", macd_watch_file)
        print(f"  → {p} を出力しました")
        macd_sig = scan_macd_signals(macd_sel, macd_data)
        macd_mod.print_signals(macd_sig)

    if do_a7:
        a7_sel, a7_data = run_a7_all(all_symbols, top_a7)
        print_results(a7_sel, f"A7 {ver_label}", top_a7)
        p = write_symbols_file(a7_sel, f"A7 {ver_label}", a7_watch_file)
        print(f"  → {p} を出力しました")
        a7_sig = scan_a7_signals(a7_sel, a7_data)
        a7_mod.print_signals_a7(a7_sig)

    if do_rsi2:
        rsi2_sel, rsi2_data, rsi2_params, rsi2_mode = run_rsi2_all(all_symbols, top_rsi2)
        print_results(rsi2_sel, f"RSI2 {ver_label}", top_rsi2)
        p = write_symbols_file(rsi2_sel, f"RSI2 {ver_label}", rsi2_watch_file)
        print(f"  → {p} を出力しました")
        rsi2_sig = scan_rsi2_signals(rsi2_sel, rsi2_data, rsi2_params)
        rsi2_mod.print_signals_rsi2(rsi2_sig, rsi2_mode, rsi2_params)

    # ── 横断分析 ──────────────────────────────────────────────
    watchlist = None
    if sum(1 for s in [macd_sel, a7_sel, rsi2_sel] if s) >= 2:
        watchlist = analyze_cross_strategy(macd_sel, a7_sel, rsi2_sel)
        print_watchlist(watchlist)
        lines = [
            '"""',
            f'横断分析 監視銘柄 — 3戦略バックテスト選定 ({today})',
            'select_symbols_v2.py により自動生成',
            '"""',
            'SYMBOLS = [',
        ]
        for d in watchlist:
            star = "★★★" if d["n_strat"] == 3 else ("★★" if d["n_strat"] == 2 else "★")
            strat_str = "/".join(f'{s}#{d["ranks"][s]}' for s in d["strategies"])
            lines.append(f'    ("{d["symbol"]}", "{d["name"]}"),  # {star} [{strat_str}]')
        lines.append(']')
        Path("symbols_watch_combined.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  → symbols_watch_combined.py を出力しました")

    html_path = generate_html(macd_sel, a7_sel, rsi2_sel,
                               macd_sig, a7_sig, rsi2_sig,
                               macd_data, a7_data, rsi2_data,
                               watchlist)
    print(f"\n  HTMLレポート: {html_path.resolve()}")
    webbrowser.open(html_path.resolve().as_uri())
    print()


if __name__ == "__main__":
    main()
