"""sameday5m_firsttouch.py — 同日決済の5分足 first-touch 判定(純粋関数のみ)。

backtest_limit_entry(約定エンジン)・sameday5m_core(検証ツール)・nikkei_analysis
の全てから使う最下層。**このモジュールは他のプロジェクト内モジュールを import しない**
(pandas のみ) ことで循環インポートを防ぐ。first-touch は「バグの温床」なので実装は
必ずここ1箇所に集約する。
"""
from __future__ import annotations

import pandas as pd


def short_exit_5m(day_bars, entry_p, stop_p, target_p, is_rise_trigger,
                  on_close=False, stop_on_close=None, target_on_close=None,
                  no_target=False, no_stop=False):
    """約定日の5分足からショートの決済(価格・理由・時刻)を first-touch で求める。

    Args:
      day_bars       : その約定日の5分足(open/high/low/close 昇順)
      entry_p        : 約定価格(=注文価格)。約定バーの特定に使う
      stop_p         : ショートの損切(上側)
      target_p       : ショートの利確(下側)
      is_rise_trigger: True=価格が上昇して entry_p に到達で約定(mirror・指値空売り)
                       False=価格が下落して entry_p に到達で約定(lss・逆指値空売り)
      on_close       : False(既定)=タッチ判定(高値≥stop / 安値≤target で発火。現行挙動)。
                       True=損切り・利確とも終値判定(5分足終値でライン超えたバーで発火)。
                       下の stop_on_close / target_on_close を両方 True にするのと同義
                       (それらが None のときの既定値として使われる)。
      stop_on_close  : 損切りだけを終値判定にするか。None=on_close に従う。
      target_on_close: 利確だけを終値判定にするか。None=on_close に従う。
                       → 「損切りだけ終値」= stop_on_close=True, target_on_close=False。
                          「利確だけ終値」  = stop_on_close=False, target_on_close=True。
                       終値判定側の発火価格は『そのバーの終値』、タッチ側はライン価格ちょうど。
                       約定(トリガー到達)は常にタッチ判定(注文の性質上変えない)。
      no_target      : True=利確(target)を一切見ない。損切りだけ判定し、当たらなければ
                       引け決済(=同日lssで『利確を日足終値=引けに委ねる/利確を置かない』
                       運用に相当)。損切りは soc に従う(既定タッチ)。
      no_stop        : True=損切り(stop)を一切見ない。利確だけ判定し、当たらなければ
                       引け決済(=同日lssで『損切りを日足終値=引けに委ねる』運用に相当)。
                       損失が引けまで走るので通常は不利側の検証用。
    Returns: (exit_price, reason, entry_ts, exit_ts)
      reason ∈ {"target","stop","close","no_entry","no_5m"}
    """
    if day_bars is None or day_bars.empty:
        return None, "no_5m", None, None
    soc = on_close if stop_on_close is None else stop_on_close
    toc = on_close if target_on_close is None else target_on_close
    highs = day_bars["high"].to_numpy(dtype=float)
    lows = day_bars["low"].to_numpy(dtype=float)
    closes = day_bars["close"].to_numpy(dtype=float)
    times = day_bars.index
    n = len(highs)

    # 1) 約定バー(トリガー到達)。約定はタッチ判定のまま。
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
    # 2) 約定バーの次バー以降で first-touch(約定前ヒットの先読み回避)。
    #    損切り(上)・利確(下)は独立に close/タッチを選べる。損切り優先は維持。
    for j in range(ei + 1, n):
        if not no_stop:               # no_stop=True なら損切りを見ない(引けまで持つ)
            stop_hit = (closes[j] >= stop_p) if soc else (highs[j] >= stop_p)
            if stop_hit:              # 上抜け=損切(同時タッチも優先)
                return (float(closes[j]) if soc else stop_p), "stop", ent_ts, times[j]
        if not no_target:             # no_target=True なら利確を見ない(引けまで持つ)
            tgt_hit = (closes[j] <= target_p) if toc else (lows[j] <= target_p)
            if tgt_hit:               # 下抜け=利確
                return (float(closes[j]) if toc else target_p), "target", ent_ts, times[j]
    # 3) どちらも当たらなければ引け
    return float(closes[-1]), "close", ent_ts, times[-1]


def short_entry_fill_5m(day_bars, trigger_p, is_rise_trigger, entry_gap_limit=None,
                        day_open=None):
    """空売りの『現実的な約定価格』を5分足から求める(ギャップ考慮)。

    現行バックテストは常に trigger 価格で約定させるが、実際は寄りが既にトリガーを
    割って/超えて始まると『始値』で約定する。それを再現する。

    Args:
      day_bars       : 約定日の5分足(open/high/low/close 昇順)
      trigger_p      : 逆指値/指値の注文価格(トリガー)
      is_rise_trigger: False=lss(逆指値売り・下落約定) / True=mirror(指値売り・上昇約定)
      entry_gap_limit: 指値ガード(例0.03=±3%)。lssはトリガー×(1-limit)を下回るギャップ
                       ダウンなら『指値下限で約定不可』としてスキップ(None返す)。
                       mirrorはトリガー×(1+limit)を超えるギャップアップでスキップ。
                       None=ガードなし。
    Returns: fill_price(float) / None(約定せず or ギャップ過大でキャンセル)

    lss(下落約定)の考え方:
      約定 = min(trigger, その日の始値[opens[0]])。
        寄り(その日の始値)が trigger 以下(寄りギャップダウン) → 約定=始値(より安い=不利)
        寄りが trigger 超 → ザラ場で trigger を下抜けた瞬間に約定 → 約定=trigger
      ※ ここは『その日の始値(opens[0])』を使う。約定バー(ザラ場)の始値ではない。
        逆指値売りはトリガーを下抜けた"瞬間"に約定するので、約定バーの始値(トリガーより
        更に下)を使うと空売りに不利すぎる過小評価になる(実約定はトリガー近辺になる)。
      始値がトリガー×(1-limit)未満なら約定不可(指値下限ガード)。
    """
    if day_bars is None or day_bars.empty:
        return None
    opens = day_bars["open"].to_numpy(dtype=float)
    highs = day_bars["high"].to_numpy(dtype=float)
    lows = day_bars["low"].to_numpy(dtype=float)
    n = len(opens)
    ei = None
    for j in range(n):
        if is_rise_trigger:
            if highs[j] >= trigger_p:
                ei = j; break
        else:
            if lows[j] <= trigger_p:
                ei = j; break
    if ei is None:
        return None
    # 約定価格の基準は『その日の始値』= 寄り。約定バー(ザラ場)の始値ではない。
    # 逆指値/指値はトリガーを抜けた瞬間に約定するので、寄りがトリガー超なら約定=トリガー。
    # 寄り値は日足の始値(day_open)を優先する。5分足の寄り(opens[0])は稀に壊れる
    # (寄り付き付近のバー欠落・異常値。例: 実際は2,869で寄ったのに5分足が2,819始まり)ので、
    # 信頼できる日足始値があればそれを使う。無ければ5分足のopens[0]にフォールバック。
    o = float(day_open) if (day_open is not None and day_open > 0) else float(opens[0])
    if is_rise_trigger:
        fill = max(trigger_p, o)             # 上昇約定: 寄りギャップアップは始値(高い=有利)
        if entry_gap_limit is not None and fill > trigger_p * (1.0 + entry_gap_limit):
            return None                      # 寄りギャップアップ過大 → キャンセル
    else:
        fill = min(trigger_p, o)             # 下落約定: 寄りギャップダウンは始値(安い=不利)
        if entry_gap_limit is not None and fill < trigger_p * (1.0 - entry_gap_limit):
            return None                      # 寄りギャップダウン過大 → 指値下限でキャンセル
    return float(fill)


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
