"""
bt_score_cache.py  ―  BTスコア・バックテスト結果キャッシュ
==================================================================
逆指値ロング (run_signals_holdout_all.py の _FROZEN_BT_SCORES + signal_score_cache.json)
を5分足デイトレ用に移植。

【役割】
- 初回シグナル検出時の BTスコアを凍結 (signal_date 単位)
- 同じ signal_date のシグナルは古いスコアを使い続ける (一貫性)
- signal_date が変わった (新規シグナル) ら 新規スコアで上書き
- backtest結果も disk pickle にキャッシュ (.holdout_bt_cache/)

【ファイル】
- signal_score_cache.json: {symbol::strategy::signal_date: {bt_score, first_seen}}
- .cache_daytrade/{strategy_short}_{date}.pkl: backtest結果

【使い方】
  from bt_score_cache import (
      load_score_cache, save_score_cache,
      get_frozen_score, register_signal,
      load_bt_cache, save_bt_cache, with_bt_cache,
  )

  score_cache = load_score_cache()
  frozen = get_frozen_score(score_cache, "5803.T", "MACD", "2026-06-12", current_bt=75)
  # frozen=70 (古い) または 75 (新規)
"""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

SCORE_CACHE_PATH = Path("signal_score_cache_daytrade.json")
BT_CACHE_DIR = Path(".cache_daytrade_bt")
BT_CACHE_DIR.mkdir(exist_ok=True)


# ── スコアキャッシュ ──────────────────────────────────────────
def load_score_cache(path=None):
    """シグナルスコアキャッシュを読み込み。

    戻り値: {symbol::strategy::signal_date: {bt_score, first_seen}}
    """
    p = Path(path or SCORE_CACHE_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_score_cache(cache, path=None):
    """シグナルスコアキャッシュを書き込み。"""
    p = Path(path or SCORE_CACHE_PATH)
    try:
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return True
    except Exception:
        return False


def cache_key(symbol, strategy, signal_date):
    """キャッシュキー生成。"""
    return f"{symbol}::{strategy}::{signal_date}"


def get_frozen_score(score_cache, symbol, strategy, signal_date, current_bt=None):
    """凍結スコア取得。

    存在すれば古い値を返す。なければ current_bt で新規登録して current_bt を返す。
    """
    key = cache_key(symbol, strategy, signal_date)
    if key in score_cache:
        return score_cache[key].get("bt_score", current_bt)
    if current_bt is not None:
        score_cache[key] = {
            "bt_score": current_bt,
            "first_seen": str(datetime.now(JST).date()),
            "signal_date": signal_date,
        }
        return current_bt
    return None


def register_signal(score_cache, symbol, strategy, signal_date, bt_score):
    """新規シグナルをキャッシュに登録。"""
    key = cache_key(symbol, strategy, signal_date)
    if key not in score_cache:
        score_cache[key] = {
            "bt_score": bt_score,
            "first_seen": str(datetime.now(JST).date()),
            "signal_date": signal_date,
        }
        return True
    return False


def get_latest_signal_date(score_cache, symbol, strategy):
    """指定銘柄×戦略の最新シグナル日を取得。"""
    latest = None
    for key, info in score_cache.items():
        parts = key.split("::")
        if len(parts) != 3:
            continue
        sym, strat, sdate = parts
        if sym == symbol and strat == strategy:
            if latest is None or sdate > latest:
                latest = sdate
    return latest


def cleanup_old_signals(score_cache, days=180):
    """古いシグナル (N日以上前) をキャッシュから削除。"""
    cutoff = (datetime.now(JST).date() - timedelta(days=days)).isoformat()
    keys_to_del = []
    for key in score_cache:
        parts = key.split("::")
        if len(parts) == 3 and parts[2] < cutoff:
            keys_to_del.append(key)
    for key in keys_to_del:
        del score_cache[key]
    return len(keys_to_del)


# ── BTキャッシュ (バックテスト結果) ───────────────────────────
def _bt_cache_path(label, date_str=None):
    if date_str is None:
        date_str = datetime.now(JST).strftime("%Y-%m-%d")
    return BT_CACHE_DIR / f"bt_{label}_{date_str}.pkl"


def load_bt_cache(label, date_str=None):
    """BTキャッシュを読み込み。"""
    path = _bt_cache_path(label, date_str)
    if not path.exists():
        return {}
    try:
        return pickle.loads(path.read_bytes())
    except Exception:
        return {}


def save_bt_cache(cache, label, date_str=None):
    """BTキャッシュを書き込み。"""
    path = _bt_cache_path(label, date_str)
    try:
        path.write_bytes(pickle.dumps(cache, protocol=pickle.HIGHEST_PROTOCOL))
        return True
    except Exception:
        return False


class BTCache:
    """BTキャッシュ管理クラス (with-statement対応)。

    使い方:
        with BTCache("long") as cache:
            result = cache.get_or_compute("5803.T", "MACD", lambda: backtest(...))
    """
    def __init__(self, label, date_str=None, save_interval=50):
        self.label = label
        self.date_str = date_str
        self.save_interval = save_interval
        self.cache = {}
        self.dirty_count = 0

    def __enter__(self):
        self.cache = load_bt_cache(self.label, self.date_str)
        return self

    def __exit__(self, *args):
        if self.dirty_count > 0:
            save_bt_cache(self.cache, self.label, self.date_str)

    def get_or_compute(self, key, compute_fn):
        """キャッシュから取得 or 計算して保存。"""
        if key in self.cache:
            return self.cache[key]
        result = compute_fn()
        self.cache[key] = result
        self.dirty_count += 1
        if self.dirty_count % self.save_interval == 0:
            save_bt_cache(self.cache, self.label, self.date_str)
        return result

    def keys(self):
        return self.cache.keys()

    def __len__(self):
        return len(self.cache)
