"""
lss_exit_watcher.py — lss(逆指値空売り・同日決済)の日中OCO決済ウォッチャー
==============================================================================

kabu はOCO(利確と損切を同時に置く)機能を持たない(建玉に返済注文は1つしか
紐付けられない)。そこで **損切(上)は逆指値買いを建玉に置きっぱなし**にして板側で
自動発火させ(遅延ゼロ)、**利確(下)は現在値をポーリング**して到達したら成行で
買い戻す(このとき send_buy が損切の逆指値を自動取消する)。どちらも当たらず
引け近く(既定 --close-at 15:20)になったら『引け成行(MOC)』で買い戻す。

  約定(3,020以下で売り) →
    損切(上): 逆指値買い(信用返済)を @>=損切 に置きっぱなし → 上抜けで自動発火(成行)
    利確(下): 現在値 <= 利確 になったら成行買戻し(損切逆指値を取消してから)
    15:20以降どちらも未達 → 引け成行で買戻し(損切逆指値を取消してから)

  ※ 損切をrestingにするのは、損切0.1ATR(タイト)がポーリング遅延に最も弱いため。
    利確1.0ATR(広い)は遅延の影響が小さいのでポーリングで足りる。

【誤決済防止】
  対象は「今日lssとして発注した売建」だけ:
    - ordered_signals_lss.csv (kabu_send_lss)        : stop_price/target_price
    - placed_orders_<日付>.csv (レポート発注ボタン)  : side=short かつ 戦略が*_Sでない行の stop/target
  かつ 建玉の平均約定値が発注価格に近いもの(--tol 既定8%)だけ。
  ロング(買建)とメインショート(*_S・多日保有)は絶対に触らない。

【なぜ二重約定しないか】
  send_buy(order_type="market", cash_margin=CASH_MARGIN_CLOSE) は発注前に
  cancel_open_close_orders で既存の返済注文を取消し、建玉を1回だけ返済する。
  一度決済した建玉は positions から消えるので、次のループでは対象外になる。

【安全設計】(kabu_send_signals / close_stop_guard と同じ)
  - 既定 dry-run。--execute のときだけ実決済。
  - --execute でも接続先は既定デモ(18081)。本番(18080)は --prod 明示必須。

【運用の鉄則(このセッションで確定)】
  - **大引け(15:30)まで止めない**。既定 --close-at 15:20 で MOC 切替→15:30 まで
    30秒ごとにリトライ。途中で閉じると引け決済が飛ぶ(過去にメドレーで取りこぼし)。
    東証は 2024/11/5 以降 大引け=15:30(MARKET_END)。MOCは15:25-15:30の板寄せで約定。
  - **発注サーバ(order_server) と同時に動かさない**。kabu の有効トークンは1つで、
    同時起動すると 401 の取り合いになり決済失敗する。朝は「発注→監視に切替」で
    片方ずつ(レポートの切替ボタン)。
  - close_lss_guard.py は watcher を動かさない日の“バックアップ”。watcher 常駐日は
    実行しない(二重決済防止)。
  - 発注は 一般信用(デイトレ)=MarginTradeType 3 が在庫・コスト面で有利
    (同日返済前提)。同日決済なので逆日歩は基本かからない。

使い方(場中に起動して放置):
  python lss_exit_watcher.py                    # dry-run: 監視対象と判定を表示(発注なし)
  python lss_exit_watcher.py --execute          # デモ口座に決済発注
  python lss_exit_watcher.py --execute --prod   # 本番口座 (要明示)
  python lss_exit_watcher.py --poll 5           # ポーリング間隔(秒, 既定5)
  python lss_exit_watcher.py --close-at 15:20   # 引け決済に切替える時刻(既定15:20)
  python lss_exit_watcher.py --immediate        # 引けを即時成行に(既定は引け成行MOC)
  python lss_exit_watcher.py --all-dates        # 過去日発注の取りこぼしlss建玉も対象
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
import time as _time
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path

# Windows の cp932 コンソール/ログに [!] >= <= ✓ 等(cp932非対応文字)を出すと
# UnicodeEncodeError でプロセスごと落ちる(常駐ウォッチャーが死ぬと決済されない)。
# 既定エンコード(cp932)のまま errors=replace にして、非対応文字は '?' に置換し落とさない
# (Get-Content 既定でも日本語はそのまま読める)。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

from kabu_api import KabuClient, CASH_MARGIN_CLOSE

JST = timezone(timedelta(hours=9))
_BASE = Path(__file__).resolve().parent
MARKET_START = dtime(9, 0)    # 寄り(東証)。これより前は成行が通らないので発火しない
MARKET_END = dtime(15, 30)    # 大引け(東証, 2024/11/5以降は15:30)。これを過ぎたらループ終了
                              # ※15:25-15:30はクロージング・オークション

# ── 多重起動防止(タスクスケジューラの重複起動・手動起動が重なっても1つだけ動かす) ──
# lock ファイルの mtime を各ループで更新(ハートビート)。_LOCK_STALE 秒より古い lock は
# 「死んだインスタンス」とみなして無視する(クラッシュ後の残骸で永久ブロックしない)。
# 損切りが不整合(平均約定値以下)になったときの緊急幅。--emergency-stop-pct で変更。
# ショートで損切り無しは青天井なので、0 にしない限り必ず何かしらの損切りを置く。
_EMERG_STOP_PCT = 0.01

# H の指値を前日終値から何ティックずらしたか。前日終値の復元に使う(--h-limit-ticks)。
# レポート/発注側の LSS_H_LIMIT_TICKS と必ず同じ値にすること。
_H_LIMIT_TICKS = -5

_LOCK = _BASE / ".lss_watcher.lock"
# 建玉の初回検知時刻を日付つきで残す。**場中に再起動しても delay の窓を作り直さない**
# ため(2026-08-13: 09:06 に再起動したら武装が 09:05→09:10 に後ろ倒しになり、
# 無保護窓が5分→10分に伸びた)。
_SEEN = _BASE / ".lss_watcher_seen.json"
_LOCK_STALE = 180


def _lock_alive() -> bool:
    if not _LOCK.exists():
        return False
    try:
        return (_time.time() - _LOCK.stat().st_mtime) < _LOCK_STALE
    except Exception:
        return False


def _touch_lock() -> None:
    try:
        _LOCK.write_text(str(os.getpid()))
    except Exception:
        pass


def _release_lock() -> None:
    try:
        _LOCK.unlink()
    except Exception:
        pass


def _tick(p: float) -> float:
    """東証の呼値。取れなければ 1 円(安全側=前日終値の復元幅を小さめに見積もる)。"""
    try:
        from backtest_limit_entry import tick_size
        return float(tick_size(float(p)))
    except Exception:
        return 1.0


def _ceil_tick(p: float) -> float:
    """ライン直上のティック(レポートの _c2t と同じ)。"""
    import math
    t = _tick(p)
    try:
        return math.ceil(float(p) / t) * t
    except Exception:
        return float(p)


def _load_seen(today: str) -> dict:
    """前回起動が残した『初回検知時刻』を読む(今日ぶんだけ)。"""
    import json
    try:
        d = json.loads(_SEEN.read_text(encoding="utf-8"))
        return {k: datetime.fromisoformat(v)
                for k, v in (d.get(today) or {}).items()}
    except Exception:
        return {}


def _save_seen(today: str, seen: dict) -> None:
    import json
    try:
        _SEEN.write_text(json.dumps(
            {today: {k: v.isoformat() for k, v in seen.items()}},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _load_lss_orders(today: str, all_dates: bool) -> dict[str, list[dict]]:
    """今日lss発注した銘柄を symbol -> [{entry, qty, strategy, name, stop, target}]。

    stop = 損切(上) / target = 利確(下)。lss は損切=上/目標=下(空売り)。
    """
    out: dict[str, list[dict]] = {}

    def _add(sym, entry, qty, strat, name, stop, target, date="",
             mode="stop", atr=0.0, sm=0.0, tm=0.0):
        """mode/atr/sm/tm は H案(寄指)用。stop/target を実約定価格から組み直すのに使う。
        既存の CSV(列が無い)は mode="stop" のまま = 従来どおり注文価格基準。"""
        sym = str(sym).upper().removesuffix(".T").split(".")[0]
        if not sym:
            return
        out.setdefault(sym, []).append(
            {"entry": entry, "qty": qty, "strategy": strat, "name": name,
             "stop": stop, "target": target, "date": str(date)[:10],
             "mode": str(mode or "stop"), "atr": float(atr or 0.0),
             "sm": float(sm or 0.0), "tm": float(tm or 0.0)})

    # A) ordered_signals_lss.csv (kabu_send_lss)
    p = _BASE / "ordered_signals_lss.csv"
    if p.exists():
        try:
            for r in csv.DictReader(open(p, encoding="utf-8")):
                if str(r.get("family", "")).strip() != "lss":
                    continue
                d = str(r.get("record_date", "")).strip()
                if not all_dates and d != today:
                    continue
                _add(r.get("symbol"), _num(r.get("order_price")),
                     int(_num(r.get("qty")) or 100), r.get("strategy", ""),
                     r.get("name", ""), _num(r.get("stop_price")),
                     _num(r.get("target_price")), date=d,
                     mode=(r.get("entry_mode") or "stop"),
                     atr=_num(r.get("atr")), sm=_num(r.get("sm")),
                     tm=_num(r.get("tm")))
        except Exception as e:
            print(f"  [!] ordered_signals_lss.csv 読込失敗 ({e})")

    # B) placed_orders_<date>.csv (レポート発注ボタン = order_server)
    for fp in sorted(glob.glob(str(_BASE / "placed_orders_*.csv"))):
        try:
            for r in csv.DictReader(open(fp, encoding="utf-8")):
                if str(r.get("side", "")).strip() != "short":
                    continue
                strat = str(r.get("strategy", "")).strip()
                if strat.upper().endswith("_S"):
                    continue   # メインショート(多日保有) → lssではない
                d = str(r.get("placed_at", "")).strip()[:10]
                if not all_dates and d != today:
                    continue
                _add(r.get("symbol"), _num(r.get("entry")),
                     int(_num(r.get("qty")) or 100), strat, r.get("name", ""),
                     _num(r.get("stop")), _num(r.get("target")), date=d,
                     mode=(r.get("entry_mode") or "stop"),
                     atr=_num(r.get("atr")), sm=_num(r.get("sm")),
                     tm=_num(r.get("tm")))
        except Exception:
            continue

    return out


def _date_ord(d: str) -> int:
    """YYYY-MM-DD → 比較用整数(新しいほど大)。不正は0。"""
    try:
        y, m, dd = str(d)[:10].split("-")
        return int(y) * 10000 + int(m) * 100 + int(dd)
    except Exception:
        return 0


def _match_lss(sym: str, avg_price: float, qty: int, lss_map: dict,
               tol: float) -> dict | None:
    """kabu売建(sym, 平均約定値, 数量)を lss発注記録に紐づける。

    ★複数日ぶんの注文が残っている場合の誤マッチ対策(2026-07-31):
      旧実装は「平均約定値に最も近い entry」だけで選んだ。今日の玉がギャップ約定して
      前日の注文 entry に近づくと、前日の注文行(=古い/逆側の損切・利確)に誤マッチし、
      今日の玉に前日の損切りが当たる事故が起きた(例: 2674 今日3,025玉に7/29の損切2,910)。
      lssは同日決済が原則なので、優先度を
        1) 建玉数量が一致する注文  2) 発注日が新しい注文  3) 平均約定値が近い注文
      に変更する(数量と日付で「どの日の玉か」を確定してから価格で詰める)。
    """
    cands = lss_map.get(sym)
    if not cands:
        return None

    def _within(c) -> bool:
        e = float(c.get("entry", 0.0) or 0.0)
        if avg_price <= 0 or e <= 0:
            return True
        return abs(avg_price - e) / e <= tol

    def _prox(c) -> float:
        e = float(c.get("entry", 0.0) or 0.0)
        if avg_price <= 0 or e <= 0:
            return tol
        return abs(avg_price - e) / e

    pool = [c for c in cands if _within(c)]
    if not pool:
        return None
    pool.sort(key=lambda c: (
        0 if int(c.get("qty", 0) or 0) == int(qty or 0) else 1,   # 1) 数量一致
        -_date_ord(c.get("date", "")),                            # 2) 新しい発注日
        _prox(c),                                                 # 3) 平均約定値が近い
    ))
    return pool[0]


def _parse_hhmm(s: str) -> dtime:
    try:
        hh, mm = s.split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        return dtime(15, 20)


def _stop_arm_time(first_seen: datetime, delay_bars: int) -> datetime:
    """損切りを効かせ始める時刻 = 約定検知した5分足が閉じる次の5分グリッド × delay_bars本。
    例) 09:01約定検知・delay=1 → 09:05 / 09:06約定検知・delay=1 → 09:10。
    5分足ラベルは left(区間の始まり)なので、検知時刻を5分床にして delay_bars×5分 を足す。"""
    floored = first_seen.replace(minute=(first_seen.minute // 5) * 5,
                                 second=0, microsecond=0)
    return floored + timedelta(minutes=5 * max(1, delay_bars))


def _lss_shorts(cli, lss_map: dict, tol: float) -> list[dict]:
    """kabu建玉から、対象の lss 売建(未決済)を抽出。毎回 kabu の実建玉を読むので、
    部分約定で減った残玉(LeavesQty)もそのまま拾える(=残玉を監視し続ける)。"""
    try:
        positions = cli.get_positions(product=2)   # 信用建玉
    except Exception as e:
        # 401(Unauthorized) = トークン失効(他プロセスが /token を取り直した等)。
        # 一度だけ再接続してトークンを取り直し、リトライする(競合が消えれば自己回復)。
        if "401" in str(e) or "Unauthorized" in str(e):
            try:
                print("  [再接続] トークン失効(401)を検知 → kabu再接続してリトライ")
                cli.connect()
                positions = cli.get_positions(product=2)
            except Exception as e2:
                print(f"  [!] 建玉取得失敗(再接続後も): {e2}")
                return []
        else:
            print(f"  [!] 建玉取得失敗: {e}")
            return []
    # 銘柄単位で合算する。kabuの信用返済は『銘柄単位で建玉を自動選択し、発注時に既存の
    # 返済注文を取消す』ため、建玉ごとに決済すると片方の返済注文(MOC/逆指値)がもう片方の
    # 発注取消で消え、100株しか決済されない。合計数量で1回だけ決済/板逆指値すれば回避できる。
    agg: dict = {}
    for kp in positions:
        sym = str(kp.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        if str(kp.get("Side", "")) != "1":     # 売建のみ(買建=ロングは触らない)
            continue
        qty = int(kp.get("LeavesQty") or kp.get("Qty") or 0)
        if qty <= 0:        # LeavesQty<=0 は決済済み。残玉(部分約定後)は qty>0 で拾い続ける
            continue
        avg = _num(kp.get("AveragePrice") or kp.get("Price"))
        rec = _match_lss(sym, avg, qty, lss_map, tol)
        if rec is None:
            continue   # lss記録に一致しない売建(メインショート等) → 触らない
        # ── H案(寄指)は OCO を **実約定価格(寄り値)基準** で組み直す ──────────
        # 板寄せの約定値は指値**以上**になる(寄りが指値より上なら始値で売れる)。
        # 指値基準の stop をそのまま使うと約定値より下に来てしまい、建てた瞬間に
        # 損切り条件を満たす。バックテスト(eh_trades)も ep 基準:
        #     stop = ep + atr*sm  /  target = ep - atr*tm
        # 現行の逆指値(mode="stop")は注文価格基準のままでよい(約定値は
        # トリガー以下 = 損切りは必ず上に来るので破綻しない)。
        if str(rec.get("mode", "stop")) in ("auction", "limit"):
            _atr, _sm, _tm = (float(rec.get("atr", 0) or 0),
                              float(rec.get("sm", 0) or 0),
                              float(rec.get("tm", 0) or 0))
            if avg > 0 and _atr > 0 and _sm > 0 and _tm > 0:
                rec = dict(rec)
                _os, _ot = rec.get("stop", 0.0), rec.get("target", 0.0)
                rec["stop"] = avg + _atr * _sm
                rec["target"] = avg - _atr * _tm
                print(f"  [{rec.get('mode')}] {sym} 実約定{avg:,.0f} から OCO を再計算: "
                      f"損切 {_os:,.0f}→{rec['stop']:,.0f} / "
                      f"利確 {_ot:,.0f}→{rec['target']:,.0f} "
                      f"(ATR{_atr:,.1f} sm{_sm} tm{_tm})")
            elif avg > 0 and _sm > 0 and _tm > 0:
                # ── ATR が記録に無いとき: **前日終値を復元して** ATR を逆算する ──
                # レポートの損切/利確は **前日終値基準** (nikkei_analysis:2493-2495):
                #     損切 = ceil_tick(前日終値 + ATR*sm) / 利確 = ceil_tick(前日終値 - ATR*tm)
                # H の指値は 前日終値 + h_ticks*tick なので、逆に足せば前日終値に戻せる。
                # ⛔ 2026-08-13 の初版は差分を**指値**から測っていたため、
                #    5ティック(=25円)ぶん損切りが広がった。sm=0.1 の幅は 5〜12円しか
                #    ないので、幅が実質3倍になり sm≈0.3 相当で回っていた。
                _ep0 = float(rec.get("entry", 0) or 0)
                _os, _ot = (float(rec.get("stop", 0) or 0),
                            float(rec.get("target", 0) or 0))
                _cl = _ep0 - _H_LIMIT_TICKS * _tick(_ep0) if _ep0 > 0 else 0.0
                _sw, _tw = (_os - _cl), (_cl - _ot)
                if _cl > 0 and _sw > 0 and _tw > 0:
                    rec = dict(rec)
                    rec["stop"] = _ceil_tick(avg + _sw)
                    rec["target"] = _ceil_tick(avg - _tw)
                    print(f"  [{rec.get('mode')}] {sym} ATR不明 → 指値{_ep0:,.0f}から"
                          f"前日終値{_cl:,.0f}を復元({-_H_LIMIT_TICKS}tick戻し)して"
                          f"実約定{avg:,.0f}基準に: "
                          f"損切 {_os:,.0f}→{rec['stop']:,.0f} (幅{_sw:,.1f}) / "
                          f"利確 {_ot:,.0f}→{rec['target']:,.0f} (幅{_tw:,.1f})")
                else:
                    print(f"  [!] {sym} 前日終値を復元できません"
                          f"(指値={_ep0} 損切={_os} 利確={_ot}) → 注文価格基準のまま")
            else:
                # ⛔ ATR が無くても『注文価格基準のまま』にしてはいけない(2026-08-12 事故)。
                #    H は寄りが指値より上で約定するので、注文価格基準の損切りは
                #    ほぼ必ず約定値より下に来る → 直後の安全ガードで**損切りが丸ごと
                #    無効化**される(= 無防備で引けまで持つ)。実際 2026-08-13 の寄りで
                #    4銘柄すべてがこの状態になった。
                #    ATR が無くても、記録にある (注文価格, 損切, 利確) の**差分**は
                #    そのまま使える。元が stop = 注文価格 + ATR*sm で作られている以上、
                #    差分を実約定値に平行移動すれば ATR 基準の再計算と完全に一致する。
                _ep0 = float(rec.get("entry", 0) or 0)
                _os, _ot = (float(rec.get("stop", 0) or 0),
                            float(rec.get("target", 0) or 0))
                if avg > 0 and _ep0 > 0 and _os > _ep0 and 0 < _ot < _ep0:
                    rec = dict(rec)
                    rec["stop"] = avg + (_os - _ep0)
                    rec["target"] = avg - (_ep0 - _ot)
                    print(f"  [{rec.get('mode')}] {sym} ATR不明 → 注文価格{_ep0:,.0f}からの"
                          f"**差分**を実約定{avg:,.0f}へ平行移動: "
                          f"損切 {_os:,.0f}→{rec['stop']:,.0f} "
                          f"(+{_os - _ep0:,.0f}) / "
                          f"利確 {_ot:,.0f}→{rec['target']:,.0f} "
                          f"(-{_ep0 - _ot:,.0f})")
                else:
                    print(f"  [!] {sym} H({rec.get('mode')})だが ATR も 注文価格/損切/利確 も"
                          f"取れません(atr={_atr} sm={_sm} tm={_tm} "
                          f"entry={_ep0} stop={_os} target={_ot}) → "
                          f"注文価格基準のまま。**損切りが無効化される可能性が高いので"
                          f"手動で逆指値買いを置いてください**")
        # 安全ガード: 売建(ショート)の損切は必ず平均約定値より上。
        # ⛔ 2026-08-13 事故を受けて **fail-open をやめた**。
        #    旧実装は不整合なら損切りを 0 にして黙って無効化していた。ショートで
        #    損切り無しは上方向に青天井なので、『誤決済を防ぐ』ために『無防備で
        #    引けまで持つ』のは取り違えた優先順位だった。
        #    H(limit/auction) は寄りが指値より上で約定するのが**正常**なので、
        #    この不整合は日常的に起きうる。よって無効化ではなく **持ち上げる**。
        _stop_v = _num(rec.get("stop", 0.0))
        if avg > 0 and (not _stop_v or _stop_v <= avg):
            if str(rec.get("mode", "stop")) in ("auction", "limit"):
                _ep0 = float(rec.get("entry", 0) or 0)
                _off = (_stop_v - _ep0) if (_stop_v and _ep0 and _stop_v > _ep0) else 0.0
                _emg = avg * _EMERG_STOP_PCT
                _new = avg + max(_off, _emg)
                _why = (f"記録の差分 +{_off:,.0f}" if _off >= _emg and _off > 0
                        else f"緊急幅 +{_EMERG_STOP_PCT * 100:.1f}%")
                rec = dict(rec)
                if _new <= avg:      # --emergency-stop-pct 0 かつ差分も無い = 旧挙動
                    print(f"  [!] {sym} 損切{_stop_v:,.0f} が平均約定{avg:,.0f}以下だが"
                          f"引き上げ幅が0 → 無効化(利確・引けのみ)。"
                          f"--emergency-stop-pct を 0 にしていませんか")
                    rec["stop"] = 0.0
                else:
                    print(f"  [!] {sym} 損切{_stop_v:,.0f} が平均約定{avg:,.0f}以下 → "
                          f"**無効化せず {_new:,.0f} に引き上げ**({_why})。"
                          f"ショートで損切り無しは青天井なので、無防備にはしない")
                    rec["stop"] = _new
            elif _stop_v:
                # 旧lss(逆指値)は約定値がトリガー以下になるので、ここに来るのは
                # 本当に逆側/陳腐化した注文。従来どおり無効化する。
                print(f"  [!] {sym} 損切{_stop_v:,.0f} が平均約定{avg:,.0f}以下"
                      f"(ショート逆側/古い注文の疑い) → この損切りは無効化"
                      f"(利確・引けのみ)。注文記録を確認してください")
                rec = dict(rec); rec["stop"] = 0.0
        a = agg.get(sym)
        if a is None:
            agg[sym] = {
                "sym": sym, "name": rec.get("name", "") or (kp.get("SymbolName") or sym),
                "qty": qty, "avg": avg, "strategy": rec.get("strategy", ""),
                "stop": rec.get("stop", 0.0), "target": rec.get("target", 0.0),
                # 決済は close_positions=None で kabu が銘柄内の建玉を自動選択(合計数量分)。
                "hold_id": "", "pkey": sym,
                # 返済は建玉と同じ信用区分でないと Code 8。同一銘柄の建玉は同区分想定。
                "margin_type": int(kp.get("MarginTradeType") or 1),
            }
        else:
            a["qty"] += qty   # 同一銘柄の複数建玉を合算(200株=1回で決済)
    return list(agg.values())


def _place_stop_buy(cli, sym: str, qty: int, hold_id: str, stop_p: float) -> str:
    """損切(上)を『逆指値買い(信用返済)』で建玉に置きっぱなしにする。
    発火後は成行(after_hit_price=None)。返り値: "ok" / "exists"(建玉拘束=既に設置済) / "fail"。"""
    cp = [{"HoldID": hold_id, "Qty": qty}] if hold_id else None
    try:
        res = cli.send_stop_buy(sym, qty=qty, trigger_price=stop_p,
                                cash_margin=CASH_MARGIN_CLOSE, close_positions=cp)
    except Exception as e:
        print(f"  [!] {sym} 損切逆指値の設置失敗: {e}")
        return "fail"
    if (res.get("Result") == 0) or res.get("_dry_run"):
        return "ok"
    if str(res.get("Code")) == "4001005":   # 建玉拘束 = 既に返済注文(=損切)あり
        return "exists"
    print(f"  [!] {sym} 損切逆指値の設置応答エラー: {res}")
    return "fail"


def _close_buy(cli, sym: str, qty: int, hold_id: str, reason: str) -> bool:
    """信用返済成行買いで決済。close_positions=None にして send_buy に既存の返済注文
    (=置きっぱなしの損切逆指値)を自動取消させてから買い戻す(二重返済にならない)。"""
    try:
        res = cli.send_buy(sym, qty=qty, order_type="market",
                           cash_margin=CASH_MARGIN_CLOSE, close_positions=None)
    except Exception as e:
        print(f"  [!] {sym} {reason} 決済失敗: {e}")
        return False
    ok = (res.get("Result") == 0) or res.get("_dry_run")
    return bool(ok)


def _close_moc(cli, sym: str, qty: int, hold_id: str) -> bool:
    """引け成行(MOC)で買戻し決済。close_positions=None で既存の損切逆指値を自動取消。"""
    try:
        res = cli.send_moc(sym, qty=qty, side="buy",
                           cash_margin=CASH_MARGIN_CLOSE, close_positions=None)
    except Exception as e:
        print(f"  [!] {sym} 引け成行 決済失敗: {e}")
        return False
    return bool((res.get("Result") == 0) or res.get("_dry_run"))


def _regen_holdings(cli) -> None:
    """実建玉から保有銘柄HTML(holdings_latest.html)を再生成する(📌保有タブ用)。
    watcher が kabu トークンを握っている間、run_signals_holdout_all を別接続すると
    401(トークン競合)になるため、同じ接続でここが保有タブ(損益)を更新する。
    kabu APIが一時失敗(空[])したときは前回表示を維持(保有ゼロ誤表示を防ぐ)。"""
    try:
        import close_stop_guard as _csg
        from pathlib import Path
        positions = _csg.load_positions_from_kabu(cli, product=0, verbose=False)
        if not _csg.LAST_KABU_FETCH_OK:
            return   # 取得失敗 → 上書きしない(前回表示を維持)
        html = _csg._build_holdings_html(positions, datetime.now(JST),
                                         price_fn=cli.get_current_price)
        out = Path(__file__).resolve().parent / "holdings_latest.html"
        out.write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"  [!] 保有タブ(holdings_latest.html)更新失敗: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="lss(同日決済)の日中OCO決済ウォッチャー")
    ap.add_argument("--execute", action="store_true",
                    help="実際に決済発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="ポーリング間隔(秒, 既定5)")
    ap.add_argument("--close-at", default="15:20",
                    help="引け決済に切替える時刻 HH:MM (既定 15:20 = 15:25の"
                         "クロージング・オークション直前。それまで損切/利確を優先)")
    ap.add_argument("--tol", type=float, default=0.08,
                    help="建玉の平均約定値とlss発注価格の一致許容(既定0.08=±8%%)")
    ap.add_argument("--margin-type", type=int, default=3,
                    help="信用取引区分(建玉と一致必須) 1=制度 / 2=一般(長期) / 3=一般(デイトレ)。既定3")
    ap.add_argument("--all-dates", action="store_true",
                    help="今日以外の日付で発注したlss建玉も対象にする")
    ap.add_argument("--once", action="store_true",
                    help="1回だけ判定して終了(監視ループを回さない・デバッグ用)")
    ap.add_argument("--no-holdings", action="store_true",
                    help="保有タブ(holdings_latest.html)の定期更新をしない")
    ap.add_argument("--immediate", action="store_true",
                    help="引けの決済を即時成行にする(既定は引け成行MOC=15:30終値約定)")
    ap.add_argument("--stop-delay-bars", type=int, default=0,
                    help="損切り遅延(delay1)。約定した5分足の間は損切りを設置せず、次の5分グリッド"
                         "(09:05/09:10…)から損切りを有効にする。1=約定バーの次足から(寄り1本目の"
                         "ヒゲ刈り回避。BT30以上でPF1.63→1.95)。0(既定)=約定検知後すぐ損切り(現行)。"
                         "バックテストの LSS_STOP_DELAY_BARS と対応。利確・引けは常に有効")
    ap.add_argument("--no-cancel-gap", dest="cancel_gap", action="store_false", default=True,
                    help="寄り深ギャップ取消を無効化。既定ON=未約定のlss逆指値のうち『現在値<トリガー"
                         "-X%%(X=_INTRADAY_5M_ENTRY_GAP_LIMIT=3%%)』のものを毎ループ取消(analyze_gap_bt "
                         "の OOS検証で>3%%超の深ギャップ約定は不利=バックテストと一致させる)。--execute時のみ実取消")
    ap.add_argument("--budget-cap", type=float, default=0.0,
                    help="予算上限管理(over-subscribe運用)。同時保有lss売建の合計時価(平均約定値×残玉数)が"
                         "この額(円)に達したら、未発動のlss新規売り逆指値を全取消する。例3000000=300万で頭打ち。"
                         "0(既定)=無効。予算より多め(BT降順)に発注しておき、同時保有が上限に達したら残りを自動キャンセル。"
                         "決済済みポジションは除外(同時保有金額=一瞬でも閾値超で発動)。--execute時のみ実取消")
    ap.add_argument("--entry-cutoff", type=str, default=None,
                    help="引け間際エントリー見送り。HH:MM(例 14:30)以降は未発動のlss新規売り逆指値を"
                         "取消し、新規を建てない。lssは同日決済なので遅い約定ほど『利確まで走る時間が"
                         "無いのに損切りだけ効く』非対称になる(2026-08-06 三菱製鋼 14:59約定→15:00"
                         "損切り -9,600円=当日実損の47%%)。未指定(既定)=OFF。--execute時のみ実取消")
    ap.add_argument("--emergency-stop-pct", type=float, default=1.0,
                    help="損切りが平均約定値以下になったときに引き上げる緊急幅(%%)。既定1.0。"
                         "H(指値売り)は寄りが指値より上で約定するのが正常なので、注文価格基準の"
                         "損切りは約定値より下に来る。旧実装はそれを黙って無効化していたが"
                         "(2026-08-13 事故: 4銘柄すべて無防備)、ショートの損切り無しは青天井なので"
                         "**必ず引き上げる**。記録に (注文価格,損切) の差分があればそちらを優先し、"
                         "無いときだけこの%%を使う。0=旧挙動(無効化)")
    ap.add_argument("--h-limit-ticks", type=int,
                    default=int(os.environ.get("LSS_H_LIMIT_TICKS", "-5") or -5),
                    help="H の指値を前日終値から何ティックずらして出したか(既定 -5 / "
                         "env LSS_H_LIMIT_TICKS)。発注記録に ATR が無いとき、指値から"
                         "前日終値を復元して損切/利確の幅を測るのに使う。⛔ レポート・発注側と"
                         "必ず同じ値にすること(ズレるとその分だけ損切り幅がずれる)")
    args = ap.parse_args()

    global _EMERG_STOP_PCT, _H_LIMIT_TICKS
    _EMERG_STOP_PCT = max(0.0, float(args.emergency_stop_pct)) / 100.0
    _H_LIMIT_TICKS = int(args.h_limit_ticks)

    close_at = _parse_hhmm(args.close_at)
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実決済★" if args.execute else "dry-run (発注なし)"
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    print("=" * 66)
    print(f"lss 日中OCO決済ウォッチャー  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label} / 接続先: {env_label} / 損切(上)・利確(下) 先着で成行買戻し")
    _close_kind = "即時成行" if args.immediate else "引け成行(MOC)"
    print(f"引け決済({_close_kind})発注: {args.close_at} / ポーリング: {args.poll}秒 / 一致許容: ±{args.tol*100:.0f}%")
    if args.stop_delay_bars > 0:
        print(f"損切り遅延(delay{args.stop_delay_bars}): 約定検知の5分足の間は損切り無効 → "
              f"次の5分グリッド({args.stop_delay_bars * 5}分後)から損切り有効(寄りヒゲ刈り回避)")
    else:
        print("損切り遅延: なし(約定検知後すぐ損切り=base)。本番は delay2 → --stop-delay-bars 2")
    print("=" * 66)

    # 多重起動防止: 既に別インスタンスが稼働中(新鮮なlock)なら何もせず終了。
    if _lock_alive():
        print("別の lss_exit_watcher が稼働中です(lockあり)。二重起動を避けて終了します。")
        return 0
    _touch_lock()
    try:
        return _run(args, close_at, today)
    finally:
        _release_lock()


def _handoff_to_order(prod: bool) -> str:
    """watcherを止めて order_server(発注サーバ)を新ウィンドウで起動する逆ハンドオフ。
    レポートの🚀発注が再び使えるようになる(kabuトークンは order_server 側へ移る)。"""
    import subprocess
    import threading as _th
    import os as _os
    cmd = [sys.executable, "-u", str(_BASE / "order_server.py"), "--execute"]
    if prod:
        cmd.append("--prod")
    try:
        kw: dict = {"cwd": str(_BASE)}
        if _os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # 別ウィンドウ表示
        else:
            kw["start_new_session"] = True
        subprocess.Popen(cmd, **kw)
    except Exception as e:
        return f"発注サーバ起動失敗: {e}(watcherは継続します)"

    def _bye():
        _time.sleep(1.2)
        print("🚀 発注サーバにハンドオフ → watcherを終了します(kabu接続を解放)。")
        try:
            _release_lock()
        except Exception:
            pass
        _os._exit(0)
    _th.Thread(target=_bye, daemon=True).start()
    return ("発注サーバ(order_server --execute" + (" --prod" if prod else "")
            + ")を新しいウィンドウで起動しました。\n"
            "watcherは間もなく停止します。レポートの🚀発注ボタンが再び使えます。")


def _start_control_server(prod: bool) -> None:
    """watcherに制御用HTTPサーバ(127.0.0.1:8766)を持たせる。レポートの
    『🚀発注に切替』ボタンが /handoff-order を叩くと、watcherを止めて発注サーバに戻せる
    (発注⇄監視の双方向トグル)。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse as _up
    import threading as _th

    class _H(BaseHTTPRequestHandler):
        def _t(self, msg, code=200):
            b = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()

        def do_GET(self):
            p = _up(self.path).path
            if p in ("/", "/health"):
                self._t("lss_exit_watcher 稼働中")
            elif p == "/handoff-order":
                self._t(_handoff_to_order(prod))
            else:
                self._t("404", 404)

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 8766), _H)
    except Exception as e:
        print(f"  [!] 制御サーバ(8766)起動失敗: {e} → 『🚀発注に切替』ボタンは使えません")
        return
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    print("  制御サーバ: http://127.0.0.1:8766/handoff-order (『🚀発注に切替』ボタン用)")


def _run(args, close_at, today) -> int:
    # 返済は建玉と同じ信用区分でないと通らない。lssエントリーは一般信用デイトレ(3)
    # で建てるので、決済(買戻し)も 3 に合わせる。--margin-type で上書き可。
    cli = KabuClient(prod=args.prod, dry_run=not args.execute,
                     margin_type=args.margin_type)
    # kabuステーション未ログインでも待機して再接続を試みる(後でログインするケースに対応)。
    # 実運用(--execute)は大引けまで30秒ごとにリトライ。dry-run/--once は1回だけ。
    while True:
        try:
            cli.connect()   # 建玉・現在値の取得に接続が要る(発注のみ dry_run)
            break
        except Exception as e:
            _retry = args.execute and not args.once and datetime.now(JST).time() < MARKET_END
            if not _retry:
                print(f"[X] kabu 接続失敗: {e}")
                print("  (現在値・建玉の監視には kabuステーション起動+ログインが必要です)")
                return 1
            print(f"  kabu未接続 ({e}) → kabuステーションのログイン待ち。30秒後に再試行...",
                  flush=True)
            _touch_lock()
            _time.sleep(30)

    # 制御サーバ(8766)を起動: レポートの『🚀発注に切替』ボタンで発注サーバへ戻せる。
    if not args.once:
        _start_control_server(args.prod)

    stop_placed: set = set()   # 板逆指値を設置済みの銘柄(pkey=銘柄)。銘柄単位で合計数量に
                               #   1本だけ置く(同一銘柄200株=2建玉でも1本で全建玉をカバー)。
    moc_placed: set = set()    # 引けMOCを出した銘柄(pkey)。引けは銘柄ごとに1回だけ(重複キュー防止)。
    close_cool: dict = {}      # pkey(銘柄) -> 直近に成行決済を送った時刻(unix秒)。部分約定の
                               #   残玉を再決済しつつ、in-flightの二重成行を防ぐクールダウン用。
    first_seen: dict = _load_seen(today)
                               # pkey -> この建玉を最初に検知した時刻(datetime)。delay1の損切り
                               #   設置開始時刻(次の5分グリッド)の起点。lssは寄り約定+watcherは
                               #   寄りから常駐なので検知≒約定時刻。決済で消して再エントリーに備える。
                               # ★ 場中に再起動しても窓を作り直さないよう、ファイルから復元する
                               #   (2026-08-13: 09:06 再起動で武装が 09:05→09:10 に後ろ倒しになり、
                               #    無保護窓が5分→10分に伸びた)。
    if first_seen:
        print(f"  前回起動の初回検知時刻を復元: "
              + " / ".join(f"{k}={v:%H:%M}" for k, v in sorted(first_seen.items())))
    _CLOSE_COOLDOWN = 20.0     # 秒。成行決済を送ったら同じ建玉にはこの秒数だけ再送しない
                               #   (約定 or 取消の確定待ち)。残玉が残ればクールダウン後に再決済。
    _hcycle = 0                # 保有タブ更新用のサイクルカウンタ
    # ※ 起動直後の _regen_holdings(保有ごとに現在値APIを叩く=遅い)はここでは"やらない"。
    #    寄り付き付近で損切が決まる場合があるため、監視ループ(=損切設置)を最優先で即開始する。
    #    保有タブはループ1周目の末尾(下の _hcycle 判定)で生成される(=損切設置の後)。
    # 寄り深ギャップ取消(既定ON): 未約定のlss逆指値のうち『現在値<トリガー-X%』を毎ループ取消する。
    #   X=_INTRADAY_5M_ENTRY_GAP_LIMIT(=3%)。バックテストは>X%超を除外(約定不可)しており、
    #   analyze_gap_bt の OOS検証で 3% が最適=深ギャップ(>3%)約定は不利。実運用も取り消して一致させる。
    _gap_done: dict = {}
    try:
        import backtest_limit_entry as _ble_g
        _gap_pct = getattr(_ble_g, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
    except Exception:
        _gap_pct = 0.03
    _gap_sweep = None
    if args.cancel_gap:
        try:
            from cancel_gap_orders import _sweep as _gap_sweep
            print(f"  寄り深ギャップ取消: ON (現在値<トリガー-{_gap_pct*100:.0f}% の未約定lss逆指値を"
                  f"毎ループ取消{'' if args.execute else ' / dry-run'})")
        except Exception as _e:
            print(f"  [!] 深ギャップ取消の読込失敗({_e}) → 無効")
            _gap_sweep = None
    else:
        print("  寄り深ギャップ取消: OFF (--no-cancel-gap)")
    # 予算上限管理(over-subscribe運用): 同時保有金額が --budget-cap 到達で未発動lss逆指値を全取消。
    #   同時保有 = 現在の未決済lss売建の (平均約定値×残玉数) の合計。決済済みは除外。
    _budget_sweep = None
    _budget_done: dict = {}
    # 引け間際エントリー見送り(--entry-cutoff HH:MM)。未指定=OFF(従来どおり終日エントリー)。
    _entry_cutoff = _parse_hhmm(args.entry_cutoff) if args.entry_cutoff else None
    _cutoff_done_flag = False
    if (args.budget_cap and args.budget_cap > 0) or _entry_cutoff is not None:
        try:
            from cancel_gap_orders import _budget_sweep as _budget_sweep
            if args.budget_cap and args.budget_cap > 0:
                print(f"  予算上限管理: ON (同時保有 ≥ {args.budget_cap/1e4:.0f}万 で未発動lss逆指値を"
                      f"全取消{'' if args.execute else ' / dry-run'})")
            if _entry_cutoff is not None:
                print(f"  発注カットオフ: ON ({args.entry_cutoff} 以降は未発動lss逆指値を取消"
                      f"{'' if args.execute else ' / dry-run'})")
        except Exception as _e:
            print(f"  [!] 未発動注文の取消機能の読込失敗({_e}) → 無効")
            _budget_sweep = None
    while True:
        now = datetime.now(JST)
        before_open = now.time() < MARKET_START      # 寄り前は成行/逆指値が通らないので発火しない
        after_close = now.time() >= close_at
        lss_map = _load_lss_orders(today, args.all_dates)
        # 寄り深ギャップ取消: 未約定lss逆指値のうち 現在値<トリガー-X% を取り消す(=バックテストの
        # >X%超除外に一致)。ザラ場中のみ(寄り前は現在値が無い/引け後は不要)。--execute時のみ実取消。
        if _gap_sweep is not None and lss_map and not before_open and not after_close:
            try:
                _gap_sweep(cli, lss_map, _gap_pct, _gap_done, dry=not args.execute)
            except Exception as _e:
                print(f"  [!] 深ギャップ取消でエラー(継続): {_e}")
        shorts = _lss_shorts(cli, lss_map, args.tol) if lss_map else []

        # 予算上限管理: 同時保有lss売建の合計時価(平均約定値×残玉数)が --budget-cap に達したら、
        #   未発動のlss新規売り逆指値を全取消。決済済みポジションは除外(同時保有ベース)。終日有効。
        if _budget_sweep is not None and not before_open and not after_close:
            filled_notional = sum(_num(p.get("avg", 0)) * int(p.get("qty", 0))
                                  for p in shorts if _num(p.get("avg", 0)) > 0)
            _reached = filled_notional >= args.budget_cap
            print(f"  {now:%H:%M:%S} 予算: 同時保有 {filled_notional/1e4:.0f}万 / 上限 "
                  f"{args.budget_cap/1e4:.0f}万{' ★到達→残り取消' if _reached else ''}")
            if _reached:
                try:
                    _bt, _bs = _budget_sweep(cli, lss_map, _budget_done, dry=not args.execute)
                    if _bt:
                        print(f"    予算上限取消: 対象{_bt}件 / 送信{_bs}件")
                except Exception as _e:
                    print(f"  [!] 予算上限取消でエラー(継続): {_e}")

        # 引け間際エントリー見送り: entry_cutoff 以降は未発動の lss 新規売り逆指値を全取消。
        # lss は同日決済なので、遅い約定ほど「利確まで走る時間が無いのに損切りだけ効く」
        # 非対称になる(2026-08-06 三菱製鋼 14:59約定→15:00損切り -9,600円 = 当日実損の47%)。
        if _entry_cutoff is not None and _budget_sweep is not None \
                and not before_open and now.time() >= _entry_cutoff:
            if not _cutoff_done_flag:
                print(f"  {now:%H:%M:%S} 発注カットオフ {args.entry_cutoff} 到達 "
                      f"→ 未発動lss新規売り逆指値を取消(以降は新規を建てない)")
            try:
                _ct, _cs = _budget_sweep(cli, lss_map, _budget_done, dry=not args.execute)
                if _ct:
                    print(f"    カットオフ取消: 対象{_ct}件 / 送信{_cs}件")
            except Exception as _e:
                print(f"  [!] カットオフ取消でエラー(継続): {_e}")
            _cutoff_done_flag = True

        if shorts and before_open:
            print(f"  {now:%H:%M:%S} 寄り前(9:00前): lss売建 {len(shorts)}件を待機中(発火なし)")
        elif shorts:
            for p in shorts:
                sym, qty, hid = p["sym"], p["qty"], p["hold_id"]
                pk = p["pkey"]   # 建玉単位の管理キー(HoldID優先)
                # 返済は建玉と同じ信用区分でないと Code 8(決済指定内容に誤り)。
                # 建玉ごとに MarginTradeType(制度=1/一般長期=2/デイトレ=3)を合わせる。
                cli.margin_trade_type = p.get("margin_type", args.margin_type)
                cur = cli.get_current_price(sym)
                _curs = f"{cur:,.0f}" if cur else "?"
                # 成行決済のクールダウン中(直近に送った)か。部分約定の残玉は次サイクルで
                # qty が減って再度拾われるので、クールダウンだけで二重成行を防ぐ。
                _cooling = (now.timestamp() - close_cool.get(pk, 0.0)) < _CLOSE_COOLDOWN
                # 引け: 損切逆指値を取消して引け成行で買戻し
                if after_close:
                    # 既定は「引け成行(MOC)」= 15:30のクロージング・オークション(終値)で約定。
                    # 発注時刻に関係なく終値で約定するのでバックテスト(終値決済)と一致。
                    # --immediate で即時成行(数秒で約定・目視確認可・5秒ごと自動リトライ)。
                    if args.immediate:
                        if _cooling:
                            continue   # 直近に送った成行の約定待ち(残玉あれば次で再送)
                        print(f"  [引け] {sym} {p['name']} 売建{qty} 現在{_curs} → 即時成行で買戻し")
                        if _close_buy(cli, sym, qty, hid, "引け"):
                            close_cool[pk] = now.timestamp()
                            stop_placed.discard(pk)
                    elif pk not in moc_placed:
                        print(f"  [引け] {sym} {p['name']} 売建{qty} 現在{_curs} → 引け成行(MOC)買戻し")
                        if _close_moc(cli, sym, qty, hid):
                            moc_placed.add(pk)   # MOCは建玉ごとに1回(重複キュー防止)
                    continue
                if cur is None or cur <= 0:
                    print(f"  [監視] {sym} {p['name']} 現在値取得不可 → 次回再試行")
                    continue
                # delay1: 約定を検知した5分足の間は損切りを一切効かせない(①②とも無効)。
                #   次の5分グリッド(09:05/09:10…)から損切りを有効化。寄り1本目のヒゲ刈り回避。
                #   利確(③)・引けはこの間も有効。検知≒約定時刻(lssは寄り約定+寄りから常駐)。
                if pk not in first_seen:
                    first_seen[pk] = now
                    _save_seen(today, first_seen)   # 再起動しても窓を作り直さない
                _stop_armed = True
                if args.stop_delay_bars > 0:
                    _arm = _stop_arm_time(first_seen[pk], args.stop_delay_bars)
                    _stop_armed = now >= _arm
                    if not _stop_armed:
                        print(f"  [損切待機] {sym} {p['name']} 現在{_curs} "
                              f"寄り{args.stop_delay_bars * 5}分は損切り無効 → {_arm:%H:%M} から有効"
                              f"(利確・引けは有効)")
                if _stop_armed:
                    # ① 損切(上): 現在値が既に損切ライン以上なら『今すぐ成行で損切』。
                    #    (逆指値買いは現在値以上のトリガーだと即約定/決済誤り(Code8)で置けないため)
                    if p["stop"] and cur >= p["stop"]:
                        if _cooling:
                            print(f"  [損切待ち] {sym} {p['name']} 現在{_curs} >= 損切{p['stop']:,.0f} "
                                  f"(直近成行の約定待ち)")
                            continue
                        print(f"  [損切] {sym} {p['name']} 売建{qty} 現在{_curs} >= 損切{p['stop']:,.0f} → 成行買戻し")
                        if _close_buy(cli, sym, qty, hid, "損切"):
                            close_cool[pk] = now.timestamp()
                            stop_placed.discard(pk)   # 部分約定の残玉は次サイクルで板逆指値を再設置
                        continue
                    # ② 損切(上): 現在値 < 損切 のときだけ逆指値買いを建玉に置きっぱなし。
                    #    設置できれば板側で自動発火(遅延ゼロ)。失敗しても①のポーリング成行が担保する。
                    #    部分約定で取消された残玉は stop_placed から外れているので再設置される。
                    if p["stop"] and pk not in stop_placed:
                        stop_placed.add(pk)
                        _r = _place_stop_buy(cli, sym, qty, hid, p["stop"])
                        if _r in ("ok", "exists"):
                            print(f"  [損切設置] {sym} {p['name']} 売建{qty} 逆指値買い @>={p['stop']:,.0f} を設置"
                                  f"{'(既存)' if _r == 'exists' else ''} → 以降は自動で損切")
                        else:
                            print(f"  [損切] {sym} {p['name']} 逆指値設置不可 → ポーリング成行で損切を担保")
                # ③ 利確(下): ポーリングで到達したら成行買戻し(send_buy が損切逆指値を自動取消)。
                if p["target"] and cur <= p["target"]:
                    if _cooling:
                        print(f"  [利確待ち] {sym} {p['name']} 現在{_curs} <= 利確{p['target']:,.0f} "
                              f"(直近成行の約定待ち)")
                        continue
                    print(f"  [利確] {sym} {p['name']} 売建{qty} 現在{_curs} <= 利確{p['target']:,.0f} "
                          f"→ 損切逆指値を取消して成行買戻し")
                    if _close_buy(cli, sym, qty, hid, "利確"):
                        close_cool[pk] = now.timestamp()
                        stop_placed.discard(pk)   # 部分約定の残玉は次サイクルで板逆指値を再設置
                else:
                    _st = f"損切{p['stop']:,.0f}" if p['stop'] else "損切-"
                    _tg = f"利確{p['target']:,.0f}" if p['target'] else "利確-"
                    print(f"  [監視] {sym} {p['name']} 現在{_curs} ({_st} / {_tg})")
        else:
            print(f"  {now:%H:%M:%S} 対象のlss売建なし(未約定 or 全決済済み)")

        # 保有タブ(損益)を定期更新: 約6サイクルごと(poll=5秒なら約30秒)。
        # watcher が握る同一トークンで生成するので token 競合(401)にならない。
        # 起動直後(最初の~6サイクル)は生成せず監視だけに専念=寄り付き付近の損切/利確を最優先。
        _hcycle += 1
        if not args.no_holdings and _hcycle % 6 == 0:
            _regen_holdings(cli)

        # 終了判定
        if args.once:
            break
        if now.time() >= MARKET_END:
            print("大引けを過ぎたので監視を終了します。")
            break
        if not args.execute:
            # dry-run は1巡して終了(監視ループは実行時のみ)
            print("dry-run のため1巡で終了します。--execute で常時監視+決済します。")
            break
        _touch_lock()   # ハートビート(多重起動防止のlockを更新)
        _time.sleep(max(1.0, args.poll))

    print("終了しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
