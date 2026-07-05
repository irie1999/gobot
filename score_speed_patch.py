"""
score_speed_patch.py — BTスコアに目標達成速度ボーナス(最大+10点)を追加するパッチ

既存コードを一切変更せず、import するだけで
check_signals_stop / check_signals_breakout の
calc_recommend_score を差し替える。

速度ボーナス計算:
  target_trades   = 全期間のトレードログから「目標達成」のみ抽出
  avg_target_days = 目標達成時の平均保有日数
  speed_bonus     = max(0, 1 - avg_target_days / 15) × 10点  最大+10点
    avg= 1日 → +10.0点
    avg= 5日 → +6.7点
    avg= 8日 → +4.7点
    avg=15日 →  0.0点

BTスコア = 既存スコア(最大100点) + speed_bonus (最大+10点 = 合計最大110点)

使い方:
  import score_speed_patch          # これだけで両モジュールにパッチが当たる
  # 以降 calc_recommend_score は速度ボーナス込みのスコアを返す

  # パッチを外したい場合:
  score_speed_patch.unpatch()

TODO: 将来のBTスコア抜本改修について
  - 年率期待値ベースへの刷新 (勝率×平均利益 - 負け率×平均損失) / 平均保有日数 × 250
  - 安定型をフィルター条件化（スコア加算ではなく選定前提条件に）
  - scan_walkforward.py の composite_score も年率期待値ベースへ
  - build_watchlist.py に --min-stability オプション追加
  → 上記が揃ったら本パッチは不要になる。別ファイルで実装予定。
"""
from __future__ import annotations

import check_signals_stop     as _stop
import check_signals_breakout as _brk

_orig_stop = _stop.calc_recommend_score
_orig_brk  = _brk.calc_recommend_score
_patched   = False


def _speed_bonus(period_results: dict) -> float:
    """全期間のトレードログから目標達成時の平均保有日数を計算しボーナス点を返す。"""
    target_days: list[int] = []
    for r in period_results.values():
        if not r:
            continue
        for t in r.get("trade_log", []):
            if t.get("reason") == "目標達成" and t.get("hold_days", 0) > 0:
                target_days.append(t["hold_days"])
    if not target_days:
        return 0.0
    avg_td = sum(target_days) / len(target_days)
    return max(0.0, 1.0 - avg_td / 15.0) * 10.0


def _make_patched(orig_fn):
    def _patched_score(period_results: dict) -> tuple[int, str]:
        base_score, _ = orig_fn(period_results)
        bonus     = _speed_bonus(period_results)
        new_score = min(110, round(base_score + bonus))
        rank = "★★★" if new_score >= 80 else "★★" if new_score >= 60 else "★" if new_score >= 40 else "△"
        return new_score, rank
    return _patched_score


def patch():
    global _patched
    if _patched:
        return
    _stop.calc_recommend_score = _make_patched(_orig_stop)
    _brk.calc_recommend_score  = _make_patched(_orig_brk)
    _patched = True
    print("[score_speed_patch] BTスコアに速度ボーナス(最大+10点)を適用しました")


def unpatch():
    global _patched
    _stop.calc_recommend_score = _orig_stop
    _brk.calc_recommend_score  = _orig_brk
    _patched = False
    print("[score_speed_patch] 速度ボーナスを解除しました")


# import するだけで自動適用
patch()
