"""
news_sentiment_model.py — ニュースセンチメント × バックテスト相関分析
=======================================================================
WATCHLIST銘柄の全バックテスト取引ログを収集し、
エントリー日前後のニュース・価格フィーチャーとトレード結果の相関を分析する。
ロジスティック回帰（numpy only）でモデルを訓練し、HTMLレポートと
news_model.json を出力する。

使い方:
  python news_sentiment_model.py              # フル分析
  python news_sentiment_model.py --days 365   # 直近365日の取引のみ使用
  python news_sentiment_model.py --workers 8  # 並列ワーカー数
  python news_sentiment_model.py --no-browser # ブラウザを開かない
  python news_sentiment_model.py --skip-news  # Googleニュース取得をスキップ
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

# ── 定数 ─────────────────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()
NEWS_CACHE_DIR = Path("news_cache")

# ── センチメントキーワード ────────────────────────────────────────────────────
POSITIVE_JP = [
    "増益", "上昇", "買い推奨", "好調", "最高値", "増配", "TOB", "黒字化",
    "好業績", "急伸", "上方修正", "過去最高", "強気", "買い場", "大幅高",
    "増収", "目標株価引き上げ", "レーティング引き上げ", "好決算", "自社株買い",
]
NEGATIVE_JP = [
    "減益", "下落", "売り推奨", "赤字", "不正", "急落", "損失", "リストラ",
    "下方修正", "経営危機", "倒産", "警告", "減収", "目標株価引き下げ",
    "レーティング引き下げ", "業績悪化", "大幅安", "不祥事", "行政処分",
]
POSITIVE_EN = [
    "beat", "upgrade", "buy", "outperform", "record", "strong", "growth",
    "raised", "positive", "surge", "rally", "profit",
]
NEGATIVE_EN = [
    "miss", "downgrade", "sell", "underperform", "loss", "weak", "decline",
    "cut", "warning", "crash", "plunge", "deficit",
]

# ── ニュースキャッシュ ────────────────────────────────────────────────────────
NEWS_CACHE_DIR.mkdir(exist_ok=True)


def _cache_key(symbol: str, date_str: str) -> Path:
    safe_sym = symbol.replace(".", "_")
    return NEWS_CACHE_DIR / f"{safe_sym}_{date_str}.json"


def _load_news_cache(symbol: str, date_str: str) -> list[dict] | None:
    p = _cache_key(symbol, date_str)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_news_cache(symbol: str, date_str: str, items: list[dict]) -> None:
    try:
        _cache_key(symbol, date_str).write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ── センチメントスコア計算 ────────────────────────────────────────────────────
def sentiment_score(titles: list[str]) -> float:
    """
    タイトルリストからセンチメントスコアを計算。
    -1 (完全ネガティブ) から +1 (完全ポジティブ)。
    """
    if not titles:
        return 0.0
    pos = 0
    neg = 0
    for title in titles:
        tl = title.lower()
        for kw in POSITIVE_JP:
            if kw in title:
                pos += 1
        for kw in NEGATIVE_JP:
            if kw in title:
                neg += 1
        for kw in POSITIVE_EN:
            if kw in tl:
                pos += 1
        for kw in NEGATIVE_EN:
            if kw in tl:
                neg += 1
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


# ── Google News RSS 取得 ─────────────────────────────────────────────────────
def fetch_news_for_period(
    symbol: str,
    name: str,
    entry_date: date,
    days_before: int = 7,
    skip_news: bool = False,
) -> tuple[int, float, bool]:
    """
    エントリー日前 days_before 日間のニュースを取得。
    Returns: (news_count, sentiment_score, news_missing)
    """
    if skip_news:
        return 0, 0.0, True

    date_str = entry_date.strftime("%Y-%m-%d")
    cached = _load_news_cache(symbol, date_str)
    if cached is not None:
        titles = [a.get("title", "") for a in cached]
        return len(cached), sentiment_score(titles), False

    end_dt   = entry_date
    start_dt = end_dt - timedelta(days=days_before)
    after_s  = start_dt.strftime("%Y-%m-%d")
    before_s = end_dt.strftime("%Y-%m-%d")

    query = f"{name} after:{after_s} before:{before_s}"
    try:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://news.google.com/rss/search"
            f"?q={encoded}&hl=ja&gl=JP&ceid=JP:ja"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; gobot-news/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        root  = ET.fromstring(data)
        items = root.findall(".//item")
        articles: list[dict] = []
        for item in items[:20]:
            title = (item.findtext("title") or "").strip()
            if " - " in title:
                title = title.rsplit(" - ", 1)[0].strip()
            pub_date = (item.findtext("pubDate") or "")
            if title:
                articles.append({"title": title, "date": pub_date[:16]})
        _save_news_cache(symbol, date_str, articles)
        titles = [a["title"] for a in articles]
        return len(articles), sentiment_score(titles), False
    except Exception:
        _save_news_cache(symbol, date_str, [])
        return 0, 0.0, True


# ── 価格フィーチャー計算 ─────────────────────────────────────────────────────
def compute_price_features(df: pd.DataFrame, entry_date: date) -> dict | None:
    """
    エントリー日前日時点の価格フィーチャーを計算。
    df: close/high/low/volume を持つ DataFrame (index=DatetimeTZ)
    """
    if df is None or len(df) < 25:
        return None

    # エントリー日に対応する行を探す
    try:
        ts = pd.Timestamp(entry_date)
        # tz-aware index への対応
        if df.index.tz is not None:
            ts = ts.tz_localize(df.index.tz)
        cands = df.index[df.index <= ts]
        if len(cands) < 25:
            return None
        idx = df.index.get_loc(cands[-1])
    except Exception:
        return None

    if idx < 24:
        return None

    row   = df.iloc[idx]
    close = float(row["close"])
    if close <= 0:
        return None

    # volume_ratio: 当日出来高 / 20日平均出来高
    vol_window = df["volume"].iloc[max(0, idx - 20):idx]
    avg_vol = float(vol_window.mean()) if len(vol_window) > 0 else 0.0
    vol_ratio = float(row["volume"]) / avg_vol if avg_vol > 0 else 1.0

    # momentum_5d: 5日前からの価格変化率 (%)
    if idx >= 5:
        close_5d_ago = float(df["close"].iloc[idx - 5])
        momentum_5d  = (close - close_5d_ago) / close_5d_ago * 100.0 if close_5d_ago > 0 else 0.0
    else:
        momentum_5d = 0.0

    # momentum_20d: 20日前からの価格変化率 (%)
    if idx >= 20:
        close_20d_ago = float(df["close"].iloc[idx - 20])
        momentum_20d  = (close - close_20d_ago) / close_20d_ago * 100.0 if close_20d_ago > 0 else 0.0
    else:
        momentum_20d = 0.0

    # rsi_14: RSI(14) 前日
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(span=14, adjust=False).mean()
    rsi_series = 100 - 100 / (1 + gain / loss.replace(0.0, np.nan))
    rsi_14 = float(rsi_series.iloc[idx]) if not pd.isna(rsi_series.iloc[idx]) else 50.0

    # atr_ratio: ATR(14) / close
    h = df["high"]
    l = df["low"]
    c = df["close"]
    prev_c = c.shift(1)
    tr_df = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr_series = tr_df.ewm(span=14, adjust=False).mean()
    atr_val   = float(atr_series.iloc[idx])
    atr_ratio = atr_val / close if close > 0 else 0.0

    # day_of_week: 0=月曜..4=金曜
    dt_val = df.index[idx]
    if hasattr(dt_val, "date"):
        dow = dt_val.date().weekday()
    else:
        dow = 0

    return dict(
        volume_ratio=vol_ratio,
        momentum_5d=momentum_5d,
        momentum_20d=momentum_20d,
        rsi_14=rsi_14,
        day_of_week=float(dow),
        atr_ratio=atr_ratio,
    )


# ── バックテスト取引ログ収集 ─────────────────────────────────────────────────
def collect_all_trades(days: int = 365) -> list[dict]:
    """
    WATCHLIST全銘柄のバックテスト取引ログを収集し、flat リストを返す。
    """
    from backtest_limit_entry import (
        fetch,
        run_limit_backtest,
        FIXED_QTY,
    )
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk

    all_wl: list[tuple] = list(_stop.WATCHLIST) + list(_brk.WATCHLIST)
    # 重複除去 (symbol+strategy 単位)
    seen: set = set()
    unique_wl: list[tuple] = []
    for sym, name, strat in all_wl:
        key = (sym, strat)
        if key not in seen:
            seen.add(key)
            unique_wl.append((sym, name, strat))

    print(f"[trade_collect] {len(unique_wl)}件 の バックテストを実行中...", flush=True)

    # 戦略パラメータ取得ヘルパー
    def _get_params(strat: str, sym: str):
        # まず stop側を試す
        for params_dict in [_stop.STRATEGY_PARAMS, _brk.STRATEGY_PARAMS]:
            if strat in params_dict:
                return params_dict[strat]
        return None

    cutoff_date = TODAY - timedelta(days=days)
    all_trades: list[dict] = []
    processed = 0

    for sym, name, strat in unique_wl:
        processed += 1
        if processed % 5 == 0:
            print(f"  [{processed}/{len(unique_wl)}] {sym} {strat}...", flush=True)

        params = _get_params(strat, sym)
        if params is None:
            continue
        calc_fn, em, sm, tm = params

        df = fetch(sym, days + 50)
        if df is None or len(df) < 30:
            continue

        try:
            result = run_limit_backtest(
                sym, name, df, calc_fn,
                em, sm, tm, days, strat,
                entry_type="stop",
            )
        except Exception as e:
            print(f"  [WARN] BT失敗 {sym} {strat}: {e}", flush=True)
            continue

        if not result:
            continue

        trade_log = result.get("trade_log", [])
        for trade in trade_log:
            # 保有中・発注中は除外
            reason = trade.get("reason", "")
            if reason in ("発注中", "保有中"):
                continue
            sig_dt = trade.get("signal_dt")
            if sig_dt is None:
                continue
            sig_date = sig_dt.date() if hasattr(sig_dt, "date") else sig_dt
            if sig_date < cutoff_date:
                continue

            entry_dt = trade.get("entry_dt")
            entry_date = entry_dt.date() if hasattr(entry_dt, "date") else entry_dt

            all_trades.append(dict(
                symbol=sym,
                name=name,
                strategy=strat,
                signal_date=sig_date,
                entry_date=entry_date,
                pnl=float(trade.get("pnl", 0.0)),
                win=1 if float(trade.get("pnl", 0.0)) > 0 else 0,
                reason=reason,
                entry_price=float(trade.get("entry_p", 0.0)),
                hold_days=int(trade.get("hold_days", 0)),
                # BT スコアは後で付与
                bt_score=0,
            ))

    print(f"[trade_collect] 完了: {len(all_trades)}件 の 取引", flush=True)
    return all_trades


def attach_bt_scores(trades: list[dict]) -> None:
    """
    取引リストに bt_score を付与する。
    同一 (symbol, strategy) の全取引に同じスコアを付与。
    """
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk

    # symbol+strategy でグループ化し、各グループで period_results を集計
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        groups[(t["symbol"], t["strategy"])].append(t)

    for (sym, strat), group_trades in groups.items():
        # period_results を簡易再構築 (全期間)
        wins   = sum(1 for t in group_trades if t["win"])
        n      = len(group_trades)
        if n == 0:
            continue
        gp = sum(t["pnl"] for t in group_trades if t["pnl"] > 0)
        gl = abs(sum(t["pnl"] for t in group_trades if t["pnl"] < 0))
        pf = gp / gl if gl > 0 else (10.0 if gp > 0 else 0.0)

        # calc_recommend_score に合わせた簡易スコア
        avg_wr   = wins / n * 100
        avg_pf   = min(pf if pf != float("inf") else 10, 10)
        stable   = 1.0 if gp > gl else 0.5
        t_trades = n

        score = round(
            avg_wr * 0.4
            + (avg_pf / 10) * 30
            + stable * 20
            + min(t_trades / 20, 1) * 10
        )
        for t in group_trades:
            t["bt_score"] = score


# ── フィーチャー行列構築 ─────────────────────────────────────────────────────
FEATURE_NAMES = [
    "news_count",
    "news_sentiment",
    "volume_ratio",
    "momentum_5d",
    "momentum_20d",
    "rsi_14",
    "day_of_week",
    "bt_score",
    "atr_ratio",
]


def build_feature_matrix(
    trades: list[dict],
    workers: int = 4,
    skip_news: bool = False,
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """
    取引リストからフィーチャー行列 X と ラベル y を構築。
    Returns: (X, y, enriched_trades)
    """
    from backtest_limit_entry import fetch

    # まず全銘柄の df を事前取得 (再利用のためキャッシュ)
    print("[features] 価格データ取得中...", flush=True)
    sym_set = {(t["symbol"],) for t in trades}
    df_cache: dict[str, pd.DataFrame | None] = {}

    def _fetch_one(sym: str):
        return sym, fetch(sym, 800)

    with ThreadPoolExecutor(max_workers=min(workers, len(sym_set) or 1)) as ex:
        futs = {ex.submit(_fetch_one, sym): sym for (sym,) in sym_set}
        done = 0
        for fut in as_completed(futs):
            sym, df = fut.result()
            df_cache[sym] = df
            done += 1
            if done % 5 == 0:
                print(f"  [{done}/{len(sym_set)}] 価格データ取得中...", flush=True)

    print(f"[features] フィーチャー計算中 ({len(trades)}件)...", flush=True)

    # ニュース取得 (並列)
    def _fetch_news(trade: dict):
        sym   = trade["symbol"]
        name  = trade["name"]
        edate = trade.get("entry_date") or trade.get("signal_date")
        if edate is None:
            return trade["_idx"], 0, 0.0, True
        cnt, sent, missing = fetch_news_for_period(sym, name, edate, skip_news=skip_news)
        return trade["_idx"], cnt, sent, missing

    # インデックス付与
    for i, t in enumerate(trades):
        t["_idx"] = i

    # ニュース取得 (並列, 適度なレート制限)
    news_results: dict[int, tuple] = {}
    batch_size = min(workers, 8)
    for batch_start in range(0, len(trades), batch_size * 4):
        batch = trades[batch_start: batch_start + batch_size * 4]
        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            futs2 = [ex.submit(_fetch_news, t) for t in batch]
            for fut in as_completed(futs2):
                try:
                    idx, cnt, sent, missing = fut.result()
                    news_results[idx] = (cnt, sent, missing)
                except Exception:
                    pass
        # 軽いレート制限 (Google News)
        if not skip_news:
            time.sleep(0.5)

    rows: list[list[float]] = []
    labels: list[int] = []
    enriched: list[dict] = []

    for t in trades:
        idx  = t["_idx"]
        sym  = t["symbol"]
        edate = t.get("entry_date") or t.get("signal_date")

        # 価格フィーチャー
        df = df_cache.get(sym)
        pf = compute_price_features(df, edate) if (df is not None and edate is not None) else None
        if pf is None:
            pf = dict(
                volume_ratio=1.0, momentum_5d=0.0, momentum_20d=0.0,
                rsi_14=50.0, day_of_week=0.0, atr_ratio=0.05
            )

        # ニュースフィーチャー
        nc, ns, nm = news_results.get(idx, (0, 0.0, True))

        row = [
            float(nc),             # news_count
            float(ns),             # news_sentiment
            pf["volume_ratio"],    # volume_ratio
            pf["momentum_5d"],     # momentum_5d
            pf["momentum_20d"],    # momentum_20d
            pf["rsi_14"],          # rsi_14
            pf["day_of_week"],     # day_of_week
            float(t["bt_score"]),  # bt_score
            pf["atr_ratio"],       # atr_ratio
        ]

        rows.append(row)
        labels.append(t["win"])
        enriched.append({**t, "news_count": nc, "news_sentiment": ns, "news_missing": nm, **pf})

    X = np.array(rows, dtype=np.float64)
    y = np.array(labels, dtype=np.float64)
    return X, y, enriched


# ── ロジスティック回帰 (numpy only) ─────────────────────────────────────────
def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))


def train_logistic(
    X_norm: np.ndarray,
    y: np.ndarray,
    lr: float = 0.05,
    epochs: int = 2000,
) -> tuple[np.ndarray, float]:
    """numpy only ロジスティック回帰。"""
    w = np.zeros(X_norm.shape[1])
    b = 0.0
    n = len(y)
    for epoch in range(epochs):
        pred = sigmoid(X_norm @ w + b)
        err  = pred - y
        w   -= lr * (X_norm.T @ err) / n
        b   -= lr * err.mean()
        # 収束チェック (1000エポックごと)
        if epoch % 500 == 499:
            loss = -np.mean(y * np.log(pred + 1e-9) + (1 - y) * np.log(1 - pred + 1e-9))
            if loss < 1e-4:
                break
    return w, b


def normalize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """標準化。ゼロ分散列は0で埋める。"""
    mean = X.mean(axis=0)
    std  = X.std(axis=0)
    std[std == 0] = 1.0
    return (X - mean) / std, mean, std


def compute_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """台形則でROC AUCを計算 (numpy only)。"""
    thresholds = np.sort(np.unique(y_score))[::-1]
    tpr_list = [0.0]
    fpr_list = [0.0]
    pos = y_true.sum()
    neg = len(y_true) - pos
    if pos == 0 or neg == 0:
        return 0.5
    for thr in thresholds:
        pred = (y_score >= thr).astype(float)
        tp = ((pred == 1) & (y_true == 1)).sum()
        fp = ((pred == 1) & (y_true == 0)).sum()
        tpr_list.append(tp / pos)
        fpr_list.append(fp / neg)
    tpr_list.append(1.0)
    fpr_list.append(1.0)
    tpr_arr = np.array(tpr_list)
    fpr_arr = np.array(fpr_list)
    # 台形則 (np.trapz → np.trapezoid in newer numpy; fallback to manual)
    try:
        auc = float(np.trapezoid(tpr_arr, fpr_arr))
    except AttributeError:
        auc = float(np.trapz(tpr_arr, fpr_arr))
    return auc


# ── 相関・p値計算 ────────────────────────────────────────────────────────────
def pearson_corr(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """ピアソン相関係数とp値（t分布近似）を返す。"""
    n = len(x)
    if n < 3:
        return 0.0, 1.0
    mx, my = x.mean(), y.mean()
    sx = x - mx
    sy = y - my
    denom = np.sqrt((sx ** 2).sum() * (sy ** 2).sum())
    if denom == 0:
        return 0.0, 1.0
    r = float((sx * sy).sum() / denom)
    r = max(-1.0, min(1.0, r))
    # t 統計量 (自由度 n-2)
    if abs(r) >= 1.0:
        t_stat = float("inf")
        p_val  = 0.0
    else:
        t_stat = r * np.sqrt(n - 2) / np.sqrt(1 - r ** 2)
        # p値: 正規近似 (|t| > 1.96 → p < 0.05)
        # z = t / sqrt(1 + t^2 / (n-2)) 近似
        z = abs(t_stat) / np.sqrt(1 + t_stat ** 2 / max(n - 2, 1))
        p_val = max(0.0, 2 * (1 - 0.5 * (1 + float(np.tanh(z * 0.8862)))))
    return r, p_val


# ── HTML 生成 ────────────────────────────────────────────────────────────────
_DARK_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0f172a; color: #e2e8f0;
  font-family: 'Segoe UI', system-ui, sans-serif;
  font-size: 14px; line-height: 1.6;
  padding: 24px;
}
h1 { font-size: 1.5rem; color: #f8fafc; margin-bottom: 8px; }
h2 { font-size: 1.15rem; color: #93c5fd; margin: 28px 0 12px; border-bottom: 1px solid #1e293b; padding-bottom: 6px; }
h3 { font-size: 1.0rem; color: #7dd3fc; margin: 20px 0 8px; }
.summary-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px; margin: 16px 0;
}
.summary-card {
  background: #1e293b; border-radius: 8px; padding: 14px 16px;
  border: 1px solid #334155;
}
.summary-card .label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; }
.summary-card .value { font-size: 1.5rem; font-weight: 700; color: #60a5fa; margin-top: 4px; }
.summary-card .sub   { font-size: 0.78rem; color: #64748b; }
table {
  width: 100%; border-collapse: collapse;
  font-size: 0.82rem; margin: 12px 0;
}
thead th {
  background: #1e293b; color: #94a3b8;
  padding: 8px 12px; text-align: left;
  border-bottom: 1px solid #334155;
  font-size: 0.75rem; text-transform: uppercase;
}
tbody tr { border-bottom: 1px solid #1e293b; }
tbody tr:hover { background: #1e293b44; }
tbody td { padding: 7px 12px; color: #cbd5e1; }
.pos { color: #4ade80; }
.neg { color: #f87171; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 0.72rem; font-weight: 700;
}
.note { color: #64748b; font-size: 0.78rem; margin: 8px 0; }
.bucket-grid {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 10px; margin: 12px 0;
}
.bucket-card {
  background: #1e293b; border: 1px solid #334155;
  border-radius: 8px; padding: 12px;
  text-align: center;
}
.bucket-card .q-label { font-size: 0.72rem; color: #64748b; margin-bottom: 4px; }
.bucket-card .wr { font-size: 1.3rem; font-weight: 700; }
.bucket-card .sub { font-size: 0.75rem; color: #94a3b8; margin-top: 4px; }
.rule-box {
  background: #1e293b; border: 1px solid #334155;
  border-radius: 6px; padding: 12px 16px; margin: 6px 0;
  font-size: 0.82rem;
}
.rule-box .wr-badge { margin-left: 8px; }
.top-signals-table tbody tr:nth-child(odd) { background: #0f172a88; }
"""


def _sign_color(v: float, positive_good: bool = True) -> str:
    if v > 0:
        return "#4ade80" if positive_good else "#f87171"
    elif v < 0:
        return "#f87171" if positive_good else "#4ade80"
    return "#94a3b8"


def _wr_color(wr: float) -> str:
    if wr >= 65:
        return "#4ade80"
    elif wr >= 50:
        return "#facc15"
    else:
        return "#f87171"


def build_html_report(
    trades: list[dict],
    X: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    bias: float,
    feature_mean: np.ndarray,
    feature_std: np.ndarray,
    auc: float,
    baseline_wr: float,
    n_days: int,
) -> str:
    n_trades = len(trades)
    n_wins   = int(y.sum())

    # ── 1. 相関テーブル ──────────────────────────────────────────────────────
    corr_rows = ""
    corr_data: list[dict] = []
    for i, fname in enumerate(FEATURE_NAMES):
        xi = X[:, i]
        r_win,  p_win  = pearson_corr(xi, y)
        pnl_arr = np.array([t["pnl"] for t in trades])
        r_pnl,  p_pnl  = pearson_corr(xi, pnl_arr)

        # 上半分 vs 下半分の勝率
        median = np.median(xi)
        top_mask = xi >= median
        bot_mask = xi <  median
        wr_top = float(y[top_mask].mean() * 100) if top_mask.sum() > 0 else 0.0
        wr_bot = float(y[bot_mask].mean() * 100) if bot_mask.sum() > 0 else 0.0
        corr_data.append(dict(
            name=fname, r_win=r_win, p_win=p_win,
            r_pnl=r_pnl, p_pnl=p_pnl,
            wr_top=wr_top, wr_bot=wr_bot,
            n_top=int(top_mask.sum()), n_bot=int(bot_mask.sum()),
        ))

    # 勝率相関の絶対値降順でソート
    corr_data.sort(key=lambda x: abs(x["r_win"]), reverse=True)
    for cd in corr_data:
        fname = cd["name"]
        r_w   = cd["r_win"]
        p_w   = cd["p_win"]
        r_p   = cd["r_pnl"]
        p_p   = cd["p_pnl"]
        wr_t  = cd["wr_top"]
        wr_b  = cd["wr_bot"]
        sig_w = "★" if p_w < 0.05 else ""
        sig_p = "★" if p_p < 0.05 else ""

        corr_rows += f"""
<tr>
  <td><strong>{fname}</strong></td>
  <td style="color:{_sign_color(r_w)}">{r_w:+.3f} {sig_w}</td>
  <td style="color:#94a3b8">{p_w:.3f}</td>
  <td style="color:{_sign_color(r_p)}">{r_p:+.3f} {sig_p}</td>
  <td style="color:#94a3b8">{p_p:.3f}</td>
  <td style="color:{_wr_color(wr_t)}">{wr_t:.1f}% (n={cd['n_top']})</td>
  <td style="color:{_wr_color(wr_b)}">{wr_b:.1f}% (n={cd['n_bot']})</td>
</tr>"""

    # ── 2. フィーチャー重要度テーブル ─────────────────────────────────────────
    importance = list(zip(FEATURE_NAMES, weights.tolist()))
    importance.sort(key=lambda x: abs(x[1]), reverse=True)
    imp_rows = ""
    max_abs_w = max(abs(w) for _, w in importance) if importance else 1.0
    for fname, w in importance:
        bar_w = int(abs(w) / max_abs_w * 120)
        color = "#4ade80" if w > 0 else "#f87171"
        bar = f'<div style="height:8px;width:{bar_w}px;background:{color};border-radius:3px"></div>'
        imp_rows += f"""
<tr>
  <td><strong>{fname}</strong></td>
  <td style="color:{color}">{w:+.4f}</td>
  <td>{bar}</td>
</tr>"""

    # ── 3. バケット分析 (news_count / volume_ratio / news_sentiment) ──────────
    bucket_sections = ""
    key_features = ["news_count", "volume_ratio", "news_sentiment", "bt_score"]
    feature_idx  = {n: i for i, n in enumerate(FEATURE_NAMES)}
    for fname in key_features:
        fi = feature_idx[fname]
        xi = X[:, fi]
        q25, q50, q75 = np.percentile(xi, [25, 50, 75])
        buckets = [
            ("Q1 (低)", xi < q25),
            ("Q2",      (xi >= q25) & (xi < q50)),
            ("Q3",      (xi >= q50) & (xi < q75)),
            ("Q4 (高)", xi >= q75),
        ]
        bucket_cards = ""
        for blabel, mask in buckets:
            if mask.sum() == 0:
                continue
            bwr  = float(y[mask].mean() * 100)
            bpnl = float(np.array([t["pnl"] for t in trades])[mask].mean())
            bn   = int(mask.sum())
            clr  = _wr_color(bwr)
            bucket_cards += f"""
<div class="bucket-card">
  <div class="q-label">{blabel}</div>
  <div class="wr" style="color:{clr}">{bwr:.1f}%</div>
  <div class="sub">勝率 (n={bn})</div>
  <div class="sub" style="color:{_sign_color(bpnl)}">avg PnL: ¥{bpnl:+,.0f}</div>
</div>"""
        bucket_sections += f"""
<h3>{fname}</h3>
<div class="bucket-grid">{bucket_cards}</div>"""

    # ── 4. デシジョンルール ────────────────────────────────────────────────────
    # volume_ratio > 2.0 AND news_sentiment > 0 などの組み合わせルール
    pnl_arr = np.array([t["pnl"] for t in trades])
    vi_vol  = feature_idx.get("volume_ratio", 2)
    vi_ns   = feature_idx.get("news_sentiment", 1)
    vi_bt   = feature_idx.get("bt_score", 7)
    vi_m5   = feature_idx.get("momentum_5d", 3)
    vi_rsi  = feature_idx.get("rsi_14", 5)

    RULES = [
        ("volume_ratio > 2.0 AND news_sentiment > 0",
         (X[:, vi_vol] > 2.0) & (X[:, vi_ns] > 0)),
        ("bt_score >= 60 AND volume_ratio > 1.5",
         (X[:, vi_bt] >= 60) & (X[:, vi_vol] > 1.5)),
        ("bt_score >= 60 AND news_sentiment > 0",
         (X[:, vi_bt] >= 60) & (X[:, vi_ns] > 0)),
        ("bt_score >= 60 AND momentum_5d > 0",
         (X[:, vi_bt] >= 60) & (X[:, vi_m5] > 0)),
        ("rsi_14 < 30 (過売り)",
         X[:, vi_rsi] < 30),
        ("rsi_14 > 70 (過買い)",
         X[:, vi_rsi] > 70),
        ("volume_ratio > 3.0 (出来高急増)",
         X[:, vi_vol] > 3.0),
        ("news_count > 3 AND news_sentiment > 0",
         (X[:, feature_idx.get("news_count", 0)] > 3) & (X[:, vi_ns] > 0)),
    ]

    rules_html = ""
    for rule_name, mask in RULES:
        n_rule = int(mask.sum())
        if n_rule < 3:
            rules_html += f'<div class="rule-box" style="color:#64748b">{rule_name} — データ不足 (n={n_rule})</div>'
            continue
        wr   = float(y[mask].mean() * 100)
        avg_pnl = float(pnl_arr[mask].mean())
        clr  = _wr_color(wr)
        badge_color = "#166534" if wr >= 60 else "#7c2d12"
        rules_html += f"""
<div class="rule-box">
  <strong style="color:#e2e8f0">{rule_name}</strong>
  <span class="badge wr-badge" style="background:{badge_color};color:#fff">
    勝率 {wr:.1f}%
  </span>
  <span class="note" style="margin-left:8px">(n={n_rule}, avg PnL: ¥{avg_pnl:+,.0f})</span>
</div>"""

    # ── 5. モデル予測テーブル (上位20シグナル) ───────────────────────────────
    X_norm = (X - feature_mean) / feature_std
    probs  = sigmoid(X_norm @ weights + bias)

    # 直近シグナル (重複なし)
    seen_sig: set = set()
    signal_rows: list[tuple] = []
    for i, t in enumerate(trades):
        key = (t["symbol"], t["strategy"])
        if key not in seen_sig:
            seen_sig.add(key)
            signal_rows.append((probs[i], i, t))

    signal_rows.sort(key=lambda x: -x[0])
    top20 = signal_rows[:20]

    top_rows = ""
    for rank, (prob, i, t) in enumerate(top20, 1):
        clr = _wr_color(prob * 100)
        win_mark = "○" if t["win"] else "×"
        win_color = "#4ade80" if t["win"] else "#f87171"
        top_rows += f"""
<tr>
  <td>{rank}</td>
  <td><strong>{t['symbol']}</strong></td>
  <td>{t['name']}</td>
  <td>{t['strategy']}</td>
  <td>{str(t.get('entry_date', ''))}</td>
  <td>{t.get('bt_score', 0)}</td>
  <td>{t.get('news_sentiment', 0.0):+.2f}</td>
  <td>{t.get('volume_ratio', 0.0):.1f}</td>
  <td style="color:{clr};font-weight:700">{prob*100:.1f}%</td>
  <td style="color:{win_color}">{win_mark} ¥{t['pnl']:+,.0f}</td>
</tr>"""

    # ── フル HTML ─────────────────────────────────────────────────────────────
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    auc_clr = "#4ade80" if auc >= 0.6 else "#facc15" if auc >= 0.5 else "#f87171"
    wr_clr  = _wr_color(baseline_wr * 100)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>ニュース×バックテスト相関分析 {TODAY}</title>
<style>{_DARK_CSS}</style>
</head>
<body>
<h1>ニュース × バックテスト 相関分析レポート</h1>
<p class="note">生成日時: {now_str} &nbsp;|&nbsp; 分析期間: 直近{n_days}日</p>

<h2>1. サマリー</h2>
<div class="summary-grid">
  <div class="summary-card">
    <div class="label">分析取引数</div>
    <div class="value">{n_trades:,}</div>
    <div class="sub">wins: {n_wins} / losses: {n_trades - n_wins}</div>
  </div>
  <div class="summary-card">
    <div class="label">ベースライン勝率</div>
    <div class="value" style="color:{wr_clr}">{baseline_wr*100:.1f}%</div>
    <div class="sub">全取引の実績</div>
  </div>
  <div class="summary-card">
    <div class="label">モデル AUC</div>
    <div class="value" style="color:{auc_clr}">{auc:.3f}</div>
    <div class="sub">ROC AUC (0.5=ランダム)</div>
  </div>
  <div class="summary-card">
    <div class="label">フィーチャー数</div>
    <div class="value">{len(FEATURE_NAMES)}</div>
    <div class="sub">{', '.join(FEATURE_NAMES[:3])}...</div>
  </div>
</div>

<h2>2. フィーチャー × 結果 相関テーブル</h2>
<p class="note">★ = p &lt; 0.05 (統計的に有意)　上半分/下半分 = フィーチャーの中央値で二分したグループの勝率</p>
<table>
<thead>
<tr>
  <th>フィーチャー</th>
  <th>vs 勝率 r</th>
  <th>p値</th>
  <th>vs PnL r</th>
  <th>p値</th>
  <th>上半分 WR</th>
  <th>下半分 WR</th>
</tr>
</thead>
<tbody>{corr_rows}</tbody>
</table>

<h2>3. フィーチャー重要度 (ロジスティック回帰 |weight|)</h2>
<p class="note">正の重み = 勝ちに貢献、負の重み = 負けに関連</p>
<table>
<thead><tr><th>フィーチャー</th><th>重み</th><th>相対重要度</th></tr></thead>
<tbody>{imp_rows}</tbody>
</table>

<h2>4. バケット分析 (四分位別 勝率・平均損益)</h2>
<p class="note">各フィーチャーを4分割し、各バケットの勝率と平均損益を表示</p>
{bucket_sections}

<h2>5. デシジョンルール</h2>
<p class="note">シンプルな条件組み合わせごとの実績 — 実際の発注フィルターとして使える洞察</p>
{rules_html}

<h2>6. モデル予測 上位20シグナル</h2>
<p class="note">モデルの予測勝率降順 (全取引から銘柄×戦略の重複を排除)</p>
<table class="top-signals-table">
<thead>
<tr>
  <th>#</th><th>コード</th><th>銘柄名</th><th>戦略</th>
  <th>エントリー日</th><th>BTスコア</th>
  <th>ニュース感情</th><th>出来高比</th>
  <th>予測勝率</th><th>実績</th>
</tr>
</thead>
<tbody>{top_rows}</tbody>
</table>

</body>
</html>"""
    return html


# ── load_and_apply_model (fetch_signal_news から呼ばれる) ──────────────────
def load_and_apply_model(
    symbol: str,
    name: str,
    entry_date,
    bt_score: int,
    skip_news: bool = False,
) -> dict:
    """
    news_model.json を読み込み、指定シグナルの予測スコアを返す。
    Returns: {news_score, predicted_win_prob, news_count, news_sentiment}
    """
    model_path = Path("news_model.json")
    if not model_path.exists():
        return dict(news_score=0.0, predicted_win_prob=0.5, news_count=0, news_sentiment=0.0)
    try:
        model = json.loads(model_path.read_text(encoding="utf-8"))
        w     = np.array(model["weights"])
        b     = float(model["bias"])
        fmean = np.array(model["feature_mean"])
        fstd  = np.array(model["feature_std"])
        fnames = model["feature_names"]
    except Exception:
        return dict(news_score=0.0, predicted_win_prob=0.5, news_count=0, news_sentiment=0.0)

    from backtest_limit_entry import fetch
    edate = entry_date if isinstance(entry_date, date) else date.fromisoformat(str(entry_date))
    nc, ns, _ = fetch_news_for_period(symbol, name, edate, skip_news=skip_news)
    df = fetch(symbol, 400)
    pf = compute_price_features(df, edate) if df is not None else None
    if pf is None:
        pf = dict(volume_ratio=1.0, momentum_5d=0.0, momentum_20d=0.0,
                  rsi_14=50.0, day_of_week=0.0, atr_ratio=0.05)

    row = np.array([
        float(nc),
        float(ns),
        pf["volume_ratio"],
        pf["momentum_5d"],
        pf["momentum_20d"],
        pf["rsi_14"],
        pf["day_of_week"],
        float(bt_score),
        pf["atr_ratio"],
    ], dtype=np.float64)

    fstd_safe = np.where(fstd == 0, 1.0, fstd)
    row_norm  = (row - fmean) / fstd_safe
    prob      = float(sigmoid(row_norm @ w + b))
    news_score = ns * min(nc, 10) / 10.0  # ニューススコア = 感情 × 記事数(上限10)

    return dict(
        news_score=round(news_score, 3),
        predicted_win_prob=round(prob, 3),
        news_count=nc,
        news_sentiment=round(ns, 3),
    )


# ── メイン ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ニュース × バックテスト 相関分析")
    parser.add_argument("--days",        type=int,  default=365)
    parser.add_argument("--workers",     type=int,  default=4)
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--skip-news",   action="store_true",
                        help="Google News 取得をスキップ (価格フィーチャーのみ)")
    parser.add_argument("--lr",          type=float, default=0.05)
    parser.add_argument("--epochs",      type=int,   default=2000)
    args = parser.parse_args()

    # ── Step 1: 取引ログ収集 ─────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"news_sentiment_model.py  [{TODAY}]  days={args.days}")
    print(f"{'='*65}")

    trades = collect_all_trades(days=args.days)
    if not trades:
        print("[ERROR] 取引データが取得できませんでした", flush=True)
        sys.exit(1)

    attach_bt_scores(trades)
    print(f"取引数: {len(trades)} (wins={sum(t['win'] for t in trades)})", flush=True)

    # ── Step 2: フィーチャー行列構築 ─────────────────────────────────────────
    print("\nフィーチャー構築中...", flush=True)
    X, y, enriched_trades = build_feature_matrix(
        trades, workers=args.workers, skip_news=args.skip_news
    )
    print(f"フィーチャー行列: {X.shape}", flush=True)

    baseline_wr = float(y.mean())
    print(f"ベースライン勝率: {baseline_wr*100:.1f}%", flush=True)

    # ── Step 3: ロジスティック回帰 ───────────────────────────────────────────
    print("\nモデル訓練中...", flush=True)
    X_norm, fmean, fstd = normalize(X)
    weights, bias = train_logistic(X_norm, y, lr=args.lr, epochs=args.epochs)

    probs = sigmoid(X_norm @ weights + bias)
    auc   = compute_auc(y, probs)
    print(f"AUC: {auc:.4f}", flush=True)

    # ── Step 4: モデル保存 ───────────────────────────────────────────────────
    model_dict = dict(
        weights=weights.tolist(),
        bias=float(bias),
        feature_names=FEATURE_NAMES,
        feature_mean=fmean.tolist(),
        feature_std=fstd.tolist(),
        trained_at=str(TODAY),
        n_trades=len(enriched_trades),
        baseline_win_rate=round(baseline_wr, 4),
        auc=round(auc, 4),
        skip_news=args.skip_news,
    )
    model_path = Path("news_model.json")
    model_path.write_text(json.dumps(model_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"モデル保存: {model_path}", flush=True)

    # ── Step 5: HTML レポート生成 ────────────────────────────────────────────
    print("\nHTMLレポート生成中...", flush=True)
    html = build_html_report(
        enriched_trades, X, y,
        weights, bias, fmean, fstd,
        auc, baseline_wr, args.days,
    )
    out_path = Path(f"news_correlation_{TODAY}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"レポート保存: {out_path}", flush=True)

    # ── Step 6: ブラウザ ─────────────────────────────────────────────────────
    if not args.no_browser:
        try:
            from _open_html import open_html
            open_html(str(out_path))
        except Exception:
            try:
                import webbrowser
                webbrowser.open(out_path.resolve().as_uri())
            except Exception:
                pass

    print(f"\n{'='*65}")
    print(f"完了  AUC={auc:.3f}  baseline_WR={baseline_wr*100:.1f}%  n={len(enriched_trades)}")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
