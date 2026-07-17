"""
lss_exit_watcher.py — lss(逆指値空売り・同日決済)の日中OCO決済ウォッチャー
==============================================================================

kabu はOCO(利確と損切を同時に置く)機能を持たない(建玉に返済注文は1つしか
紐付けられない)。そこで **損切(上)は逆指値買いを建玉に置きっぱなし**にして板側で
自動発火させ(遅延ゼロ)、**利確(下)は現在値をポーリング**して到達したら成行で
買い戻す(このとき send_buy が損切の逆指値を自動取消する)。どちらも当たらず
引け近く(既定14:55)になったら『引け成行(MOC)』で買い戻す。

  約定(3,020以下で売り) →
    損切(上): 逆指値買い(信用返済)を @>=損切 に置きっぱなし → 上抜けで自動発火(成行)
    利確(下): 現在値 <= 利確 になったら成行買戻し(損切逆指値を取消してから)
    14:55以降どちらも未達 → 引け成行で買戻し(損切逆指値を取消してから)

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

使い方(場中に起動して放置):
  python lss_exit_watcher.py                    # dry-run: 監視対象と判定を表示(発注なし)
  python lss_exit_watcher.py --execute          # デモ口座に決済発注
  python lss_exit_watcher.py --execute --prod   # 本番口座 (要明示)
  python lss_exit_watcher.py --poll 5           # ポーリング間隔(秒, 既定5)
  python lss_exit_watcher.py --close-at 14:55   # 引け成行に切替える時刻(既定14:55)
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
MARKET_END = dtime(15, 0)     # 大引け(東証)。これを過ぎたらループ終了

# ── 多重起動防止(タスクスケジューラの重複起動・手動起動が重なっても1つだけ動かす) ──
# lock ファイルの mtime を各ループで更新(ハートビート)。_LOCK_STALE 秒より古い lock は
# 「死んだインスタンス」とみなして無視する(クラッシュ後の残骸で永久ブロックしない)。
_LOCK = _BASE / ".lss_watcher.lock"
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

    def _add(sym, entry, qty, strat, name, stop, target):
        sym = str(sym).upper().removesuffix(".T").split(".")[0]
        if not sym:
            return
        out.setdefault(sym, []).append(
            {"entry": entry, "qty": qty, "strategy": strat, "name": name,
             "stop": stop, "target": target})

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
                     _num(r.get("target_price")))
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
                     _num(r.get("stop")), _num(r.get("target")))
        except Exception:
            continue

    return out


def _match_lss(sym: str, avg_price: float, lss_map: dict, tol: float) -> dict | None:
    """kabu売建(sym, 平均約定値)が lss発注記録に一致すれば記録を返す。"""
    cands = lss_map.get(sym)
    if not cands:
        return None
    if avg_price <= 0:
        return cands[0]
    best, best_diff = None, 1e9
    for c in cands:
        e = c["entry"]
        if e <= 0:
            if best is None:
                best, best_diff = c, tol
            continue
        diff = abs(avg_price - e) / e
        if diff < best_diff:
            best, best_diff = c, diff
    return best if (best is not None and best_diff <= tol) else None


def _parse_hhmm(s: str) -> dtime:
    try:
        hh, mm = s.split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        return dtime(14, 55)


def _lss_shorts(cli, lss_map: dict, tol: float, closed: set) -> list[dict]:
    """kabu建玉から、対象の lss 売建(未決済)を抽出。"""
    try:
        positions = cli.get_positions(product=2)   # 信用建玉
    except Exception as e:
        print(f"  [!] 建玉取得失敗: {e}")
        return []
    out = []
    for kp in positions:
        sym = str(kp.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        if str(kp.get("Side", "")) != "1":     # 売建のみ(買建=ロングは触らない)
            continue
        qty = int(kp.get("LeavesQty") or kp.get("Qty") or 0)
        if qty <= 0 or sym in closed:
            continue
        avg = _num(kp.get("AveragePrice") or kp.get("Price"))
        rec = _match_lss(sym, avg, lss_map, tol)
        if rec is None:
            continue   # lss記録に一致しない売建(メインショート等) → 触らない
        out.append({
            "sym": sym, "name": rec.get("name", "") or (kp.get("SymbolName") or sym),
            "qty": qty, "avg": avg, "strategy": rec.get("strategy", ""),
            "stop": rec.get("stop", 0.0), "target": rec.get("target", 0.0),
            "hold_id": str(kp.get("ExecutionID") or kp.get("HoldID") or "").strip(),
        })
    return out


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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="lss(同日決済)の日中OCO決済ウォッチャー")
    ap.add_argument("--execute", action="store_true",
                    help="実際に決済発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="ポーリング間隔(秒, 既定5)")
    ap.add_argument("--close-at", default="14:55",
                    help="引け成行に切替える時刻 HH:MM (既定 14:55)")
    ap.add_argument("--tol", type=float, default=0.08,
                    help="建玉の平均約定値とlss発注価格の一致許容(既定0.08=±8%%)")
    ap.add_argument("--margin-type", type=int, default=3,
                    help="信用取引区分(建玉と一致必須) 1=制度 / 2=一般(長期) / 3=一般(デイトレ)。既定3")
    ap.add_argument("--all-dates", action="store_true",
                    help="今日以外の日付で発注したlss建玉も対象にする")
    ap.add_argument("--once", action="store_true",
                    help="1回だけ判定して終了(監視ループを回さない・デバッグ用)")
    args = ap.parse_args()

    close_at = _parse_hhmm(args.close_at)
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実決済★" if args.execute else "dry-run (発注なし)"
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    print("=" * 66)
    print(f"lss 日中OCO決済ウォッチャー  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label} / 接続先: {env_label} / 損切(上)・利確(下) 先着で成行買戻し")
    print(f"引け成行切替: {args.close_at} / ポーリング: {args.poll}秒 / 一致許容: ±{args.tol*100:.0f}%")
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

    closed: set = set()        # 決済済み(このセッションで買戻した銘柄)
    stop_placed: set = set()   # 損切の逆指値買いを建玉に設置済みの銘柄
    while True:
        now = datetime.now(JST)
        before_open = now.time() < MARKET_START      # 寄り前は成行/逆指値が通らないので発火しない
        after_close = now.time() >= close_at
        lss_map = _load_lss_orders(today, args.all_dates)
        shorts = _lss_shorts(cli, lss_map, args.tol, closed) if lss_map else []

        if shorts and before_open:
            print(f"  {now:%H:%M:%S} 寄り前(9:00前): lss売建 {len(shorts)}件を待機中(発火なし)")
        elif shorts:
            for p in shorts:
                sym, qty, hid = p["sym"], p["qty"], p["hold_id"]
                cur = cli.get_current_price(sym)
                _curs = f"{cur:,.0f}" if cur else "?"
                # 引け: 損切逆指値を取消して引け成行で買戻し
                if after_close:
                    print(f"  [引け] {sym} {p['name']} 売建{qty} 現在{_curs} → 引け成行買戻し")
                    if _close_moc(cli, sym, qty, hid):
                        closed.add(sym)
                    continue
                # ① 損切(上): 逆指値買い(信用返済)を建玉に『置きっぱなし』(1回だけ設置)。
                #    以降は板側で自動発火するのでポーリング遅延ゼロ。発火したら建玉が消える。
                if p["stop"] and sym not in stop_placed:
                    _r = _place_stop_buy(cli, sym, qty, hid, p["stop"])
                    if _r in ("ok", "exists"):
                        stop_placed.add(sym)
                        print(f"  [損切設置] {sym} {p['name']} 逆指値買い @>={p['stop']:,.0f} を建玉に設置"
                              f"{'(既存)' if _r == 'exists' else ''} → 以降は自動で損切")
                # ② 利確(下): ポーリングで到達したら成行買戻し(send_buy が損切逆指値を自動取消)。
                if cur is None or cur <= 0:
                    continue
                if p["target"] and cur <= p["target"]:
                    print(f"  [利確] {sym} {p['name']} 現在{_curs} <= 利確{p['target']:,.0f} "
                          f"→ 損切逆指値を取消して成行買戻し")
                    if _close_buy(cli, sym, qty, hid, "利確"):
                        closed.add(sym)
                else:
                    _st = f"損切{p['stop']:,.0f}(逆指値)" if p['stop'] else "損切-"
                    _tg = f"利確{p['target']:,.0f}(監視)" if p['target'] else "利確-"
                    print(f"  [監視] {sym} {p['name']} 現在{_curs} ({_st} / {_tg})")
        else:
            print(f"  {now:%H:%M:%S} 対象のlss売建なし(未約定 or 全決済済み)")

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
