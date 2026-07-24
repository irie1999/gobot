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
                  no_target=False, no_stop=False, include_entry_bar=False,
                  day_low=None, day_high=None, day_close=None, stop_delay_bars=0):
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
      include_entry_bar: 【v13(2026-07-24)以降は _start に不使用・後方互換のため残置】
                       旧: True=約定バー(ei)から / False=ei+1から損切り・利確を判定。
                       現: 同バー損切りの取りこぼしを無くすため、ギャップ約定/ザラ場約定に
                       関わらず常に約定バー(ei)から判定する(悲観=損切り優先)。この引数の
                       値に関わらず _start=ei。
      stop_delay_bars: 損切りを約定バーから何本(=何×5分)遅らせるか。0(既定)=約定バーから
                       損切り有効(現行)。1(delay1)=約定した5分足の中は損切りせず、次の足から
                       損切り(寄り1本目のヒゲ刈り回避)。利確・引けは常に約定バーから。
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
        # 5分足がトリガーに触れない = 寄り(09:00-09:05)の欠落バーで触れた/割った可能性。
        # 日足の安値(lss)/高値(mirror)がトリガーに達していれば「寄りで約定」とみなす。
        # 建玉は寄りから開いているので、記録済み5分足の先頭バー(bor0)から損切り/利確を見る。
        if not is_rise_trigger:
            _touched = (day_low is not None and day_low > 0 and day_low <= entry_p)
        else:
            _touched = (day_high is not None and day_high > 0 and day_high >= entry_p)
        if not _touched:
            return None, "no_entry", None, None
        ei = 0
        include_entry_bar = True

    ent_ts = times[ei]
    # 2) first-touch で決済。
    #    【同バー損切り(2026-07-24, v13): 悲観=損切り優先】
    #    5分足はOHLCしか無く、約定バー内で高値(損切)と安値(約定/利確)のどちらが先かは
    #    復元できない。約定した"同じ足"の中で損切りラインに触れるケース(例:4092は約定
    #    5,710→同じ5分足内で5,770へ跳ねて損切り。旧ロジックは無視し後の目標達成+41,901円
    #    と誤計上)を取りこぼさないよう、約定バー(ei)からも損切り/利確を判定する(常に
    #    _start=ei)。区別できない同バーは損切り優先(=下の loop が stop を先に見る)。
    #    ・副作用: 約定前の跳ね(価格が上から降りてきた寄り天)を損切りに誤計上するケースは
    #      出るが受容。実運用は lss_exit_watcher が約定後にOCO損切りを実際に置くため、同バーで
    #      損切りへ触れれば刈られるのが現実に近い(§18.8 楽観バイアス除去とも整合)。
    #    ・利確(lss=下/mirror=上)は約定後にしか触れないので約定バーから見ても先読みでない。
    #      損切り側のみが悲観的仮定。include_entry_bar は後方互換のため残すが _start には不使用。
    #    【損切り遅延(2026-07-24): stop_delay_bars】約定バーから K本(=K×5分)は損切りを
    #    効かせない。K=1(delay1)なら「約定した5分足の中は損切りせず、次の足から損切り」。
    #    寄り1本目の一瞬のヒゲで刈られるのを回避する(lss検証で PF 0.93→1.43 に改善)。
    #    利確・引けは約定バーから通常どおり。実運用は「約定→その5分足が閉じたら逆指値損切りを
    #    設置」に対応(lss_exit_watcher)。K=0(既定)なら現行と完全一致。
    _start = ei
    _stop_start = ei + max(0, int(stop_delay_bars))   # 損切りが効き始めるバー(遅延中は損切り無効)
    for j in range(_start, n):
        if not no_stop and j >= _stop_start:  # no_stop=True or 遅延中(j<_stop_start)は損切りを見ない
            stop_hit = (closes[j] >= stop_p) if soc else (highs[j] >= stop_p)
            if stop_hit:              # 上抜け=損切(同時タッチも優先)
                return (float(closes[j]) if soc else stop_p), "stop", ent_ts, times[j]
        if not no_target:             # no_target=True なら利確を見ない(引けまで持つ)
            tgt_hit = (closes[j] <= target_p) if toc else (lows[j] <= target_p)
            if tgt_hit:               # 下抜け=利確
                return (float(closes[j]) if toc else target_p), "target", ent_ts, times[j]
    # 3) どちらも当たらなければ引け(タイムカット)。lss決済は引け成行(MOC)=その日の公式終値
    #    (引け値)で買戻すため、5分足の最終足終値ではなく日足の確定終値(day_close)を優先する。
    #    引けの寄成/引成が最終足とギャップすること・当日5分足が未同期(場中/部分)なことを吸収。
    #    day_close が無ければ5分足最終足にフォールバック(従来挙動)。
    _close_px = float(day_close) if (day_close is not None and day_close > 0) else float(closes[-1])
    return _close_px, "close", ent_ts, times[-1]


def short_entry_fill_5m(day_bars, trigger_p, is_rise_trigger, entry_gap_limit=None,
                        day_open=None, day_low=None, day_high=None):
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
        # 5分足がトリガーに触れない = 寄り(09:00-09:05)の欠落バーで触れた/割った可能性。
        # 日足の安値(lss)/高値(mirror)がトリガーに達していれば「寄りで約定」とみなし、
        # 約定価格は下の min/max(トリガー, 日足始値) ロジックで決める(欠落バー救済)。
        if not is_rise_trigger:
            _touched = (day_low is not None and day_low > 0 and day_low <= trigger_p)
        else:
            _touched = (day_high is not None and day_high > 0 and day_high >= trigger_p)
        if not _touched:
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
