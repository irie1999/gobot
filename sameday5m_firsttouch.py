"""sameday5m_firsttouch.py — 同日決済の5分足 first-touch 判定(純粋関数のみ)。

backtest_limit_entry(約定エンジン)・sameday5m_core(検証ツール)・nikkei_analysis
の全てから使う最下層。**このモジュールは他のプロジェクト内モジュールを import しない**
(pandas のみ) ことで循環インポートを防ぐ。first-touch は「バグの温床」なので実装は
必ずここ1箇所に集約する。
"""
from __future__ import annotations

import pandas as pd


# ── 損切り約定の現実化 (2026-08-07) ─────────────────────────────────────────
# ⛔ これまで損切りは **stop価格ちょうど** で約定させていた = 楽観。
#    逆指値(stop)注文はトリガーで**成行**になるので、実際はそのバーの始値近辺で約定する。
#    特に stop_delay_bars>0(delay1/2) では、無保護窓のあいだに価格が損切りを大きく
#    通過し、武装した瞬間の成行で遠くの値を掴む。
#
#    実測 (.\fills 3営業日25件, CLAUDE.md 18.17): 決済滑りの99%が6件に集中し、
#    **6件すべてが損切り武装バーでの約定**だった。
#      4911 資生堂 08/07  損切り3,410 → 実約定 3,536.5 (3.7%超過)
#      5632 三菱製鋼 08/07 損切り2,315 → 実約定 2,360
#      5632 三菱製鋼 08/06 14:59約定→15:00武装 → 実約定 2,335 (テストは2,280と判定)
#      他3件も同様。残り19件の決済滑りは合計 -290円(-15円/件)でほぼ完璧に一致。
#
#    正しいモデル: **fill = max(stop, そのバーの始値)** (ロングは min)。
#      ・板に置きっぱなしの逆指値 → 通常 opens[j] < stop なので stop 約定(従来と同じ)
#      ・バーが損切りを飛び越えて開く / 武装した瞬間に既に超えている → 始値約定
#    つまり従来の挙動を悪化させるのではなく、**取りこぼしていたケースだけを拾う**。
#
#    LSS_OPTIMISTIC_STOP_FILL=1 で旧挙動(stopちょうど)に戻せる(A/B用)。
_OPTIMISTIC_STOP_FILL: bool = str(
    __import__("os").environ.get("LSS_OPTIMISTIC_STOP_FILL", "") or "").strip() \
    not in ("", "0", "false", "no", "off")


def _stop_fill_short(stop_p: float, bar_open: float) -> float:
    """空売りの損切り(買い戻し)約定値。始値が損切りを超えていれば始値=不利。"""
    if _OPTIMISTIC_STOP_FILL:
        return float(stop_p)
    return float(max(float(stop_p), float(bar_open)))


def _stop_fill_long(stop_p: float, bar_open: float) -> float:
    """ロングの損切り(売り)約定値。始値が損切りを下回っていれば始値=不利。"""
    if _OPTIMISTIC_STOP_FILL:
        return float(stop_p)
    return float(min(float(stop_p), float(bar_open)))


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
    _opens = day_bars["open"].to_numpy(dtype=float)
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
                # 逆指値はトリガーで成行 → そのバーの始値が損切りを超えていれば始値約定。
                # 武装バー(delay明け)で価格が既に走っているケースを拾う(上の説明参照)。
                return ((float(closes[j]) if soc else _stop_fill_short(stop_p, _opens[j])),
                        "stop", ent_ts, times[j])
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


# ─────────────────────────────────────────────────────────────
# ロングデイトレ(逆指値買い=上ブレイク・同日決済)。上の short_* の上下反転(鏡像)。
#   lss(ショート): 前日終値-1tick 逆指値売り→下ブレイク約定→下落継続を取る
#   long(ロング) : 前日終値+1tick 逆指値買い→上ブレイク約定→上昇継続(モメンタム)を取る
# 損切り=約定より下 / 利確=約定より上。first-touch・同バー損切り優先(悲観)・stop_delay_bars対応。
# ─────────────────────────────────────────────────────────────

def long_entry_fill_5m(day_bars, trigger_p, entry_gap_limit=None,
                       day_open=None, day_high=None):
    """ロングの現実的約定価格(逆指値買い=上ブレイク)。
    約定 = max(trigger, その日の始値)。寄りがトリガー超(ギャップアップ)なら始値約定(高い=不利)。
    始値が trigger×(1+limit) を超えるギャップアップは指値上限で約定不可(None)。"""
    if day_bars is None or day_bars.empty:
        return None
    opens = day_bars["open"].to_numpy(dtype=float)
    highs = day_bars["high"].to_numpy(dtype=float)
    n = len(opens)
    ei = None
    for j in range(n):
        if highs[j] >= trigger_p:   # 上昇して逆指値買いに到達
            ei = j
            break
    if ei is None:
        _touched = (day_high is not None and day_high > 0 and day_high >= trigger_p)
        if not _touched:
            return None
    o = float(day_open) if (day_open is not None and day_open > 0) else float(opens[0])
    fill = max(trigger_p, o)
    if entry_gap_limit is not None and fill > trigger_p * (1.0 + entry_gap_limit):
        return None                 # ギャップアップ過大 → 指値上限でキャンセル
    return float(fill)


def long_exit_5m(day_bars, entry_p, stop_p, target_p,
                 no_target=False, no_stop=False,
                 day_high=None, day_close=None, stop_delay_bars=0):
    """ロング(逆指値買い)の同日決済を first-touch で。stop=下側 / target=上側。
    同バー損切り優先(悲観)・stop_delay_bars(約定バーからK本は損切り無効)対応。
    Returns: (exit_price, reason, entry_ts, exit_ts)  reason ∈ {target,stop,close,no_entry,no_5m}
    """
    if day_bars is None or day_bars.empty:
        return None, "no_5m", None, None
    highs = day_bars["high"].to_numpy(dtype=float)
    lows = day_bars["low"].to_numpy(dtype=float)
    closes = day_bars["close"].to_numpy(dtype=float)
    _lopens = day_bars["open"].to_numpy(dtype=float)
    times = day_bars.index
    n = len(highs)
    # 約定バー(上昇してトリガー到達)
    ei = None
    for j in range(n):
        if highs[j] >= entry_p:
            ei = j
            break
    if ei is None:
        _touched = (day_high is not None and day_high > 0 and day_high >= entry_p)
        if not _touched:
            return None, "no_entry", None, None
        ei = 0
    ent_ts = times[ei]
    _start = ei
    _stop_start = ei + max(0, int(stop_delay_bars))
    for j in range(_start, n):
        if not no_stop and j >= _stop_start:
            if lows[j] <= stop_p:          # 下抜け=損切(同バー優先)
                return (_stop_fill_long(stop_p, _lopens[j]), "stop", ent_ts, times[j])
        if not no_target:
            if highs[j] >= target_p:       # 上抜け=利確
                return target_p, "target", ent_ts, times[j]
    _close_px = float(day_close) if (day_close is not None and day_close > 0) else float(closes[-1])
    return _close_px, "close", ent_ts, times[-1]


def long_pnl(entry_p, exit_p, reason, qty, fee_one_way, slip):
    """ロング損益(円)。決済売りは損切り時のみ不利スリッページ(安く売る=不利)。"""
    exit_eff = exit_p * (1.0 - slip) if reason == "stop" else exit_p
    fee = (entry_p + exit_eff) * qty * fee_one_way
    return (exit_eff - entry_p) * qty - fee
