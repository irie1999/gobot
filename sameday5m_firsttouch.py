"""sameday5m_firsttouch.py — 同日決済の5分足 first-touch 判定(純粋関数のみ)。

backtest_limit_entry(約定エンジン)・sameday5m_core(検証ツール)・nikkei_analysis
の全てから使う最下層。**このモジュールは他のプロジェクト内モジュールを import しない**
(pandas のみ) ことで循環インポートを防ぐ。first-touch は「バグの温床」なので実装は
必ずここ1箇所に集約する。
"""
from __future__ import annotations

import pandas as pd


def short_exit_5m(day_bars, entry_p, stop_p, target_p, is_rise_trigger):
    """約定日の5分足からショートの決済(価格・理由・時刻)を first-touch で求める。

    Args:
      day_bars       : その約定日の5分足(open/high/low/close 昇順)
      entry_p        : 約定価格(=注文価格)。約定バーの特定に使う
      stop_p         : ショートの損切(上側)
      target_p       : ショートの利確(下側)
      is_rise_trigger: True=価格が上昇して entry_p に到達で約定(mirror・指値空売り)
                       False=価格が下落して entry_p に到達で約定(lss・逆指値空売り)
    Returns: (exit_price, reason, entry_ts, exit_ts)
      reason ∈ {"target","stop","close","no_entry","no_5m"}
    """
    if day_bars is None or day_bars.empty:
        return None, "no_5m", None, None
    highs = day_bars["high"].to_numpy(dtype=float)
    lows = day_bars["low"].to_numpy(dtype=float)
    closes = day_bars["close"].to_numpy(dtype=float)
    times = day_bars.index
    n = len(highs)

    # 1) 約定バー(トリガー到達)
    ei = None
    for j in range(n):
        if is_rise_trigger:
            if highs[j] >= entry_p:   # 上昇して指値売りに到達(mirror)
                ei = j
                break
        else:
            if lows[j] <= entry_p:    # 下落して逆指値売りに到達(lss)
                ei = j
                break
    if ei is None:
        return None, "no_entry", None, None

    ent_ts = times[ei]
    # 2) 約定バーの次バー以降で first-touch(約定前ヒットの先読み回避)
    for j in range(ei + 1, n):
        if highs[j] >= stop_p:        # 上抜け=損切(同時タッチも優先)
            return stop_p, "stop", ent_ts, times[j]
        if lows[j] <= target_p:       # 下抜け=利確
            return target_p, "target", ent_ts, times[j]
    # 3) どちらも当たらなければ引け
    return float(closes[-1]), "close", ent_ts, times[-1]


def short_exit_daily(hi, lo, cl, entry_p, stop_p, target_p, is_rise_trigger, tie="stop"):
    """日足近似の決済(比較用)。tie="stop"=保守(下限)/"target"=楽観(上限)。"""
    if is_rise_trigger:
        if hi < entry_p:
            return None, "no_entry"
    else:
        if lo > entry_p:
            return None, "no_entry"
    hit_stop = hi >= stop_p
    hit_tgt = lo <= target_p
    if tie == "target":
        if hit_tgt:
            return target_p, "target"
        if hit_stop:
            return stop_p, "stop"
    else:
        if hit_stop:
            return stop_p, "stop"
        if hit_tgt:
            return target_p, "target"
    return cl, "close"


def short_pnl(entry_p, exit_p, reason, qty, fee_one_way, slip):
    """ショート損益(円)。買い戻し(exit)は損切り時のみ不利スリッページ。
    エントリーは注文価格ちょうど(ミラーの幻スリッページ排除)。"""
    exit_eff = exit_p * (1.0 + slip) if reason == "stop" else exit_p
    fee = (entry_p + exit_eff) * qty * fee_one_way
    return (entry_p - exit_eff) * qty - fee
