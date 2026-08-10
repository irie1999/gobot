"""
order_server.py — シグナルレポートからのワンクリック発注専用サーバ
────────────────────────────────────────────────────────────────────────
position_server.py を経由せず、これ単体で kabu に逆指値エントリーを発注する
最小サーバ。signals レポート(file://)の「🚀 発注」ボタンが fetch で叩く。

  ロング  = 逆指値買い  (send_stop_buy / 翌日高値が逆指値以上で発動)
  ショート = 逆指値売り  (send_stop_sell + 信用新規 / 翌日安値が逆指値以下で発動)

安全設計 (kabu_send_signals.py と同じポリシー):
  - 既定 dry-run (発注内容を返すだけ・kabu に接続しない)
  - 既定デモ(18081)。本番(18080)は --prod 明示必須
  - 実発注は --execute 明示必須
  - ロングは既定 現物。--margin で信用新規

使い方:
  python order_server.py                  # dry-run + デモ (安全・接続なし)
  python order_server.py --execute        # デモ口座に実発注
  python order_server.py --execute --prod # 本番口座に実発注 (要明示)
  python order_server.py --port 8765      # ポート変更 (既定 8765)
"""

import argparse
import sys
import threading
import time as _time
from datetime import datetime, timezone, timedelta, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Windows の cp932 出力で非対応文字(⚠等)を出しても落ちないようにする。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HOST = "127.0.0.1"
PORT = 8765
JST = timezone(timedelta(hours=9))

EXECUTE  = False   # True なら実発注。False なら dry-run (接続なし・内容のみ)
PROD     = False   # True なら本番(18080)。False ならデモ(18081)
GENBUTSU = False   # True ならロングを現物で発注。False(既定) ならロングも信用新規

# ── 約定監視 (エントリー約定 → 利確指値を即発注) ──────────────────────
POLL_SEC = 10                    # 約定チェック間隔(秒)
# 14:50の損切りタスク(kabu_close_guard)とkabuトークンを取り合わないため、
# この時間帯は order_server 側がkabuアクセスを一時停止してタスクに譲る。
GUARD_PAUSE_START = dtime(14, 48)
GUARD_PAUSE_END   = dtime(14, 53)
_pending = []                    # 約定待ち: [{symbol, side, qty, target, strategy}]
_pending_lock = threading.Lock()
_CAP_WARNED: set = set()         # 値幅キャップ通知を(銘柄,価格)ごとに1回だけ出すための記録


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_NOT_SHORTABLE_CACHE: set | None = None


def _load_not_shortable() -> set:
    """not_shortable.py(check_shortable が kabu 照会で作成)の NOT_SHORTABLE を読む。
    プロセス内で1回だけ読み込む。ファイルが無ければ空集合(=除外なし・後方互換)。"""
    global _NOT_SHORTABLE_CACHE
    if _NOT_SHORTABLE_CACHE is not None:
        return _NOT_SHORTABLE_CACHE
    out: set = set()
    try:
        from pathlib import Path
        p = Path(__file__).resolve().parent / "not_shortable.py"
        if p.exists():
            ns: dict = {}
            exec(p.read_text(encoding="utf-8"), ns)
            out = {str(x).upper().removesuffix(".T").split(".")[0]
                   for x in ns.get("NOT_SHORTABLE", [])}
    except Exception as e:
        print(f"  ⚠ not_shortable.py 読み込み失敗: {e}")
    _NOT_SHORTABLE_CACHE = out
    return out


def _append_not_shortable(symbol: str, reason: str = "") -> bool:
    """恒久的にlss不可な銘柄(例: 一般信用デイトレ売り非対応=Code4002013)を not_shortable.py に
    追記する。check_shortable が作る形式(NOT_SHORTABLE=[...])を維持するので、レポート選定
    (run_signals_holdout_all)・発注(place_order)の両方で次回から事前除外される。
    プロセス内キャッシュも破棄して同一セッション内でも即反映する。"""
    global _NOT_SHORTABLE_CACHE
    from pathlib import Path
    sym = str(symbol).upper().removesuffix(".T").split(".")[0]
    p = Path(__file__).resolve().parent / "not_shortable.py"
    try:
        cur: list = []
        if p.exists():
            ns: dict = {}
            exec(p.read_text(encoding="utf-8"), ns)
            cur = list(ns.get("NOT_SHORTABLE", []))
        cur_set = {str(x).upper().removesuffix(".T").split(".")[0] for x in cur}
        if sym in cur_set:
            return False
        cur_set.add(sym)
        d = datetime.now(JST).strftime("%Y-%m-%d")
        p.write_text(
            '"""not_shortable.py — 空売り不可(非貸借/取扱なし/一般デイトレ売り非対応)銘柄。\n'
            f"check_shortable が生成 + order_server が発注時の恒久リジェクトを自動追記。"
            f"最終更新: {d}。lss 選定・発注の除外リストに使う。\"\"\"\n"
            f"NOT_SHORTABLE = {sorted(cur_set)!r}\n", encoding="utf-8")
        _NOT_SHORTABLE_CACHE = None
        print(f"  [i] not_shortable.py に {sym} を追加({reason})。次回から事前除外。")
        return True
    except Exception as e:
        print(f"  ⚠ not_shortable.py 追記失敗: {e}")
        return False


def _record_lss_skip(symbol: str, code, reason: str) -> None:
    """発注時の想定内スキップ(売建規制/デイトレ非対応)を日次CSVに記録(監査用)。"""
    import csv
    from pathlib import Path
    d = datetime.now(JST).strftime("%Y-%m-%d")
    path = Path(__file__).resolve().parent / f"lss_order_skipped_{d}.csv"
    new = not path.exists()
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "symbol", "code", "reason"])
            w.writerow([datetime.now(JST).strftime("%H:%M:%S"), symbol, code, reason])
    except Exception as e:
        print(f"  ⚠ スキップ記録失敗: {e}")


def _log_placed_order(rec: dict) -> None:
    """発注内容を placed_orders_<date>.csv に追記する(保有銘柄タブのソース)。"""
    import csv
    from pathlib import Path
    d = datetime.now(JST).strftime("%Y-%m-%d")
    path = Path(__file__).resolve().parent / f"placed_orders_{d}.csv"
    # entry_mode/atr/sm/tm は H案(指値売り)用。H は板寄せの約定値が指値**以上**に
    # なるので、注文価格基準の損切りは約定値より下に来て建てた瞬間に発火する。
    # lss_exit_watcher がこれらを読んで**実約定価格から OCO を組み直す**。
    cols = ["placed_at", "symbol", "name", "strategy", "side", "qty",
            "entry", "stop", "target", "bt", "env",
            "entry_mode", "atr", "sm", "tm"]
    new = not path.exists()
    try:
        # 列が増えたとき、ヘッダが古いままだと行がズレる。既存を読み直して揃える。
        if not new:
            with path.open(newline="", encoding="utf-8") as f:
                _rows = list(csv.DictReader(f))
            if _rows and [c for c in cols if c not in _rows[0]]:
                with path.open("w", newline="", encoding="utf-8") as f:
                    _w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                    _w.writeheader()
                    for _r in _rows:
                        _w.writerow({c: _r.get(c, "") for c in cols})
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerow({k: rec.get(k, "") for k in cols})
    except Exception as e:
        print(f"  ⚠ 発注ログ書き込み失敗: {e}")


def place_order(symbol: str, entry: float, qty: int, side: str,
                strat: str = "", target: float = 0.0,
                stop: float = 0.0, name: str = "", bt: str = "",
                entry_mode: str = "stop", atr: float = 0.0,
                sm: float = 0.0, tm: float = 0.0) -> str:
    """エントリーを発注し、結果メッセージを返す。
    target>0 かつ実発注なら、約定監視に登録して約定後に利確指値を自動発注する。

    entry_mode:
      "stop"  = 現行。逆指値(ショートなら下がってきたら約定)。
      "limit" = H案。**指値売り**(上がってきたら/寄りが既に上なら約定)。ショート専用。
                現値との比較(即約定回避)も -3%下限ガードも**しない**:
                指値売りはザラ場で即約定しても構わないし(むしろ高く売れる)、
                下限ガードは逆指値が発動した後の話なので指値には存在しない。
    """
    symbol = (symbol or "").split(".")[0].strip()
    side = "short" if side == "short" else "long"
    if not symbol or entry <= 0 or qty <= 0:
        return "発注失敗: 銘柄・逆指値・株数が不正です"

    # 空売り不可(非貸借/取扱なし)銘柄はショート発注を止める(最終ガード)。
    # check_shortable.py が作る not_shortable.py の NOT_SHORTABLE を参照。
    if side == "short" and symbol in _load_not_shortable():
        return f"発注中止: {symbol} は空売り不可(非貸借/取扱なし)。lss対象外です。"

    # 既定は信用新規(2)。ロングで --genbutsu 指定時のみ現物(1)。ショートは常に信用。
    cash_margin = 1 if (side == "long" and GENBUTSU) else 2

    # lss(同日決済ショート = side=short かつ 戦略が *_S でない)は一般信用デイトレ(3)で売る。
    # 制度信用(1)の空売りは貸借銘柄限定で、非貸借銘柄(例4662)は MarginTradeType不正で弾かれる。
    _is_lss = (side == "short" and not str(strat).upper().endswith("_S"))
    _margin_type = 3 if _is_lss else 1

    try:
        from kabu_api import KabuClient
        cli = KabuClient(prod=PROD, dry_run=not EXECUTE, margin_type=_margin_type)
        if EXECUTE:                 # dry-run は接続不要 (内容プレビューのみ)
            cli.connect()
    except Exception as e:
        return f"発注失敗: kabu 接続エラー ({e})"

    # ── トリガー価格を呼値(ティック)に丸める ──
    # kabu は無効な呼値を『下方向』に floor する(例: 端数3,024→3,020)ため、
    # 必ず最寄りの有効呼値に丸めてから送る(トリガー=終値ぴったりにする)。
    try:
        from backtest_limit_entry import tick_size, round_to_tick
        entry = round_to_tick(entry)
    except Exception:
        pass

    # ── 即約定回避 (必須) ──
    # 逆指値買いはトリガーが現在値より上、逆指値売りは現在値より下でないと
    # 「即座に市場に発注されてしまう」と弾かれる(kabu Code 100217)。引け後は
    # 現在値=前日終値なので、ショートのトリガーを終値ちょうどにすると必ず弾かれる。
    # → 現在値以上(買いは以下)なら現値±1ティックに調整する。現在値が取れない場合も
    #   即約定弾きを避けるため 1ティック ずらしておく(引け後は現値=終値のため)。
    adj_note = ""
    if EXECUTE:
        cur = cli.get_current_price(symbol) if entry_mode != "limit" else 0
        if cur and cur > 0:
            tick = tick_size(cur)
            if side == "long" and entry <= cur:
                new = round_to_tick(cur + tick)
                adj_note = f" ※現値{cur:,.0f}≦逆指値→{new:,.0f}に引上げ"
                entry = new
            elif side == "short" and entry >= cur:
                new = round_to_tick(cur - tick)
                adj_note = f" ※現値{cur:,.0f}≧逆指値→{new:,.0f}に引下げ"
                entry = new
        else:
            # 現在値が取れない(引け後で板が価格を返さない等)。トリガー=終値のままだと
            # kabu が即約定(Code 100217)で弾くので、1ティック ずらす。
            t = tick_size(entry)
            if side == "short":
                new = round_to_tick(entry - t)
                adj_note = f" ※現値不明→{new:,.0f}に1ティック引下げ"
                entry = new
            elif side == "long":
                new = round_to_tick(entry + t)
                adj_note = f" ※現値不明→{new:,.0f}に1ティック引上げ"
                entry = new

    # 逆指値付き指値: 発火後は指値で約定（ロング:+X%上限 / ショート:-X%下限）。
    # ギャップが大きすぎる時は約定せず見送る＝バックテストの「±X%超キャンセル」と一致。
    # X はバックテストのガード(_INTRADAY_5M_ENTRY_GAP_LIMIT)と必ず同値にする(2026-07-31: 2%)。
    try:
        from backtest_limit_entry import (round_to_tick as _r2t,
                                          _INTRADAY_5M_ENTRY_GAP_LIMIT as _gap)
        _mult = (1.0 - _gap) if side == "short" else (1.0 + _gap)
        limit_p = _r2t(entry * _mult)
    except Exception:
        _gap = 0.02
        _mult = (1.0 - _gap) if side == "short" else (1.0 + _gap)
        limit_p = round(entry * _mult)

    try:
        if side == "short" and entry_mode == "limit":
            # H案: 前日終値-5ティックの**指値売り**。寄りが既に上なら板寄せで、
            # そうでなければ日中に上がってきたときに約定する。
            # ⚠ 損切り・利確は**実約定価格(寄り値)基準**で組み直す必要がある
            #   (板寄せの約定値は指値以上になるので、指値基準の損切りは約定値より
            #    下に来て建てた瞬間に発火する)。lss_exit_watcher が
            #    ordered_signals_lss.csv の entry_mode/atr/sm/tm を見て再計算する。
            res = cli.send_sell(symbol, qty=qty, price=entry,
                                order_type="limit", cash_margin=cash_margin)
            dir_label = f"指値売り(信用新規) @{entry:,.0f} [H案]"
        elif side == "short":
            res = cli.send_stop_sell(symbol, qty=qty, trigger_price=entry,
                                     cash_margin=cash_margin, after_hit_price=limit_p)
            dir_label = f"逆指値売り→指値(信用新規) 発動≤{entry:,.0f}/指値≥{limit_p:,.0f}{adj_note}"
        else:
            res = cli.send_stop_buy(symbol, qty=qty, trigger_price=entry,
                                    cash_margin=cash_margin, after_hit_price=limit_p)
            kind = "信用新規" if cash_margin == 2 else "現物"
            dir_label = f"逆指値買い→指値({kind}) 発動≥{entry:,.0f}/指値≤{limit_p:,.0f}{adj_note}"
    except Exception as e:
        return f"発注失敗: {symbol} ({e})"

    env = "本番" if PROD else "デモ"
    if not EXECUTE:
        return (f"🧪 dry-run: {symbol} {strat} {dir_label} x{qty}株 "
                f"({env}) — 実発注は --execute で起動")
    ok = (res.get("Result") == 0) or res.get("_dry_run")
    if ok:
        watch_note = ""
        # lss(_is_lss は上で判定済み)は自動利確を置かない。日中の利確(下)・損切(上)は
        # lss_exit_watcher.py が監視して決済する(resting利確を置くと二重管理・建玉拘束の元)。
        if EXECUTE and target and target > 0 and not _is_lss:
            with _pending_lock:
                _pending.append({"symbol": symbol, "side": side, "qty": qty,
                                 "target": float(target), "strategy": strat})
            watch_note = f" / 約定したら利確@{float(target):,.0f}を自動発注(監視中)"
        elif _is_lss:
            watch_note = " / lss決済は lss_exit_watcher.py が監視(利確下・損切上・先着で成行)"
        if EXECUTE:
            _log_placed_order({
                "placed_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "name": name, "strategy": strat, "side": side,
                "qty": qty, "entry": f"{entry:.0f}", "stop": f"{stop:.0f}" if stop else "",
                "target": f"{target:.0f}" if target else "", "bt": str(bt or ""), "env": env,
                "entry_mode": entry_mode, "atr": (f"{atr:.2f}" if atr else ""),
                "sm": (f"{sm}" if sm else ""), "tm": (f"{tm}" if tm else ""),
            })
        return (f"🚀 発注完了: {symbol} {strat} {dir_label} x{qty}株 "
                f"({env}口座) OrderId={res.get('OrderId','')}{watch_note}")
    # ── 想定内のリジェクトは分かりやすいスキップにする(一括発注を止めない・パニックしない) ──
    _code = res.get("Code")
    try:
        _code_i = int(_code)
    except Exception:
        _code_i = None
    _msg = str(res.get("Message", ""))
    if _code_i == 100302 or "売建規制" in _msg:
        # 売建規制は当日限りの動的規制。恒久除外はしない(翌営業日は解除のことが多い)。
        _record_lss_skip(symbol, _code, "売建規制(100302)")
        return (f"⏭ スキップ: {symbol} は本日『売建規制』(Code100302)で発注不可。"
                f"当日限りの規制のため恒久除外はしません(翌営業日に解除の可能性)。")
    if _code_i == 4002013 or "MarginTradeType" in _msg:
        # 一般信用デイトレ売り非対応(在庫対象外)。銘柄固有で安定 → not_shortable に恒久追加し、
        # 次回からレポート選定・発注の両方で事前除外する。
        _append_not_shortable(symbol, "一般デイトレ売り非対応(4002013)")
        _record_lss_skip(symbol, _code, "一般デイトレ売り非対応(4002013)")
        return (f"⏭ スキップ: {symbol} は一般信用デイトレ売り非対応(Code4002013)。"
                f"not_shortable に追加し、次回からシグナル・発注の両方で事前除外します。")
    return f"⚠ 発注応答エラー: {symbol} {res}"


# ── 約定監視ワーカー ──────────────────────────────────────────────
def _watch_build_client():
    from kabu_api import KabuClient
    c = KabuClient(prod=PROD, dry_run=not EXECUTE)
    if EXECUTE:
        c.connect()
    return c


def _is_filled(cli, symbol: str, side: str) -> bool:
    """建玉が出来ているか(=約定)。long→買建(Side2)/short→売建(Side1)。"""
    try:
        positions = cli.get_positions(product=0)
    except Exception:
        return False
    want = "2" if side == "long" else "1"
    for p in positions:
        if str(p.get("Symbol", "")).split(".")[0] != symbol:
            continue
        if str(p.get("Side", "")) == want and int(p.get("LeavesQty") or 0) > 0:
            return True
    return False


def _has_active_close_order(cli, symbol: str, side: str) -> bool:
    """利確(決済)注文が既に出ているか。long利確=売(1)/short利確=買戻(2)。"""
    return _active_close_order(cli, symbol, side)[0] is not None


def _active_close_order(cli, symbol: str, side: str):
    """アクティブな利確注文の (order_id, price) を返す。無ければ (None, None)。
    long利確=売(1)/short利確=買戻(2)。"""
    try:
        orders = cli.get_orders()
    except Exception:
        return None, None
    want = "1" if side == "long" else "2"
    # OrderState: 1待機/2処理中/3処理済/4訂正取消送信中/5終了(=約定・失効・取消)。
    # 5(終了)は有効な板上の注文でないため除外。
    ACTIVE = {1, 2, 3, 4}
    for o in orders:
        if str(o.get("Symbol", "")).split(".")[0] != symbol:
            continue
        if str(o.get("Side", "")) != want:
            continue
        if int(o.get("OrderState") or o.get("State") or 0) not in ACTIVE:
            continue
        oid = o.get("ID", "") or o.get("OrderId", "")
        try:
            price = float(o.get("Price") or 0)
        except Exception:
            price = 0.0
        return oid, price
    return None, None


# kabu が受け付けた最長の ExpireDay 営業日オフセットをキャッシュ (毎回の探索を避ける)
_EXPIRE_OK_BDAYS: int | None = None

# 「場が引けました」検知後、利確補完を一時休止する期限 (429の嵐を防ぐ)。
_BACKFILL_COOLDOWN_UNTIL = None  # datetime|None


def _expire_int(bdays: int) -> int:
    """今日+bdays営業日 を YYYYMMDD int で返す (0=当日)。"""
    if bdays <= 0:
        return 0
    import pandas as pd
    try:
        today = pd.Timestamp(datetime.now(JST).date())
        return int((today + pd.tseries.offsets.BDay(bdays)).strftime("%Y%m%d"))
    except Exception:
        return 0


def _place_target_now(cli, p: dict, existing=None) -> str:
    """約定後の利確指値を発注。重複は出さない。
    ExpireDay は「kabu が受け付ける最長」を自動採用する:
      朝一に order_server を起動できなくても、前営業日に置いた利確指値が
      board に残り、寄りの上昇でも約定できるようにするため。
      希望(max_hold営業日)から順に短くして試し、Code5(有効期限エラー)なら
      次の候補へ。通った最長をキャッシュして次回以降の再探索を省く。
    """
    global _EXPIRE_OK_BDAYS
    symbol, side, qty, target, strat = (p["symbol"], p["side"], p["qty"],
                                        p["target"], p["strategy"])
    # 本来の利確目標を当日の値幅で丸めた「発注すべき価格」。値幅超なら上限に自動キャップ。
    # 取得失敗時(429等)は弾かれる値を送らずスキップ(ストーム防止)。
    price = _cap_to_price_limit(cli, symbol, side, target)
    if price is None:
        return "skip(値幅取得失敗→次回再試行)"

    # 建玉個別(hold_id あり): ClosePositions でその建玉だけに利確を出す。
    # /orders は返済注文の対象建玉を返せず (sym,side) 単位の既存判定では別建玉を
    # 誤って据置/取消してしまうため、建玉個別のときは既存チェックを行わず素直に
    # 発注し、既に利確がある建玉は kabu が 4001005(建玉拘束)を返す→exists 扱いにする。
    hid = str(p.get("hold_id") or "").strip()
    cp = [{"HoldID": hid, "Qty": qty}] if hid else None

    _relabel = ""
    if not hid:
        # 既存利確があれば価格を比較。値幅が広がって本来目標に近づけられる/狭まって
        # 値幅内に収め直す必要がある場合は、キャンセルして置き直す(=目標更新)。
        # existing=(oid,price) が渡されればそれを使う(get_orders再取得を避け429回避)。
        if existing is not None:
            _oid, _cur = existing
        else:
            _oid, _cur = _active_close_order(cli, symbol, side)
        if _oid is not None:
            from backtest_limit_entry import tick_size as _tsz
            if abs(_cur - price) <= _tsz(price):
                return "exists"                     # 現行と実質同じ → 据置
            # 置き直し: 既存をキャンセル(非送出化済み)してから発注
            try:
                cli.cancel_order(_oid)
            except Exception:
                pass
            _relabel = (f"目標更新↑({_cur:,.0f}→{price:,.0f})" if price > _cur
                        else f"値幅内へ修正↓({_cur:,.0f}→{price:,.0f})")

    from backtest_limit_entry import default_max_hold
    # タイムカット無効時 default_max_hold は 100000(実質無限) を返す。そのまま
    # ExpireDay に使うと「約定日+100000営業日=西暦2409年」となり kabu が
    # 「返済期日超過(Code45)」で拒否する。信用の返済期日内に収まる現実的な上限で
    # キャップし、実際に受け付けられる最長は下の候補探索で決める。
    mh = min(default_max_hold(strat), 25)
    if _EXPIRE_OK_BDAYS is not None:
        # 既知の最長のみ試す (だめなら当日)
        cand = [_EXPIRE_OK_BDAYS] + ([0] if _EXPIRE_OK_BDAYS != 0 else [])
    else:
        # 長い順に候補。kabuが受け付ける最長を探す
        cand = [n for n in (mh, 10, 5, 4, 3, 2, 1, 0)]
        _seen: set = set()
        cand = [n for n in cand if not (n in _seen or _seen.add(n))]

    def _send(expire):
        # quiet=True: 既に利確があって毎回 Code8 等で失敗する定期補完のログ氾濫を抑制
        # (失敗は戻り値 res で判定し、成功時のみ上位が表示する)。
        if side == "short":
            return cli.send_buy(symbol, qty=qty, price=price, order_type="limit",
                                cash_margin=3, expire_day=expire,
                                close_positions=cp, quiet=True)   # 信用返済(買戻)
        cm = 1 if GENBUTSU else 3   # 現物売(1) / 信用返済売(3)
        return cli.send_sell(symbol, qty=qty, price=price, order_type="limit",
                             cash_margin=cm, expire_day=expire, close_positions=cp,
                             quiet=True)

    res = None
    for n in cand:
        try:
            res = _send(_expire_int(n))
        except Exception as e:
            return f"fail({e})"
        if (res.get("Result") == 0) or res.get("_dry_run"):
            _EXPIRE_OK_BDAYS = n   # 通った最長をキャッシュ
            return f"{_relabel or 'placed'}(exp={n}営業日)"
        # 建玉個別で既に利確がある建玉 = 4001005(建玉拘束)。二重ではなく据置扱い。
        if hid and str(res.get("Code")) == "4001005":
            return "exists(建玉拘束)"
        # 有効期限系エラーなら短い候補へ。Code5(有効期限エラー)に加え、
        # Code45(注文期限が返済期日超過)や期限/返済期日を含むメッセージも対象。
        _msg = str(res.get("Message", ""))
        if (res.get("Code") in (5, 45) or "有効期限" in _msg
                or "返済期日" in _msg or "注文期限" in _msg):
            continue
        return f"fail({res})"   # それ以外(価格エラー等)は即中断(スパム防止)
    return f"fail({res})"


# 東証 制限値幅テーブル: 基準値段(前日終値) → 片側の値幅
# https://www.jpx.co.jp/equities/trading/domestic/06.html
_TSE_LIMIT_TABLE = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150),
    (1500, 300), (2000, 400), (3000, 500), (5000, 700), (7000, 1000),
    (10000, 1500), (15000, 3000), (20000, 4000), (30000, 5000),
    (50000, 7000), (70000, 10000), (100000, 15000), (150000, 30000),
    (200000, 40000), (300000, 50000), (500000, 70000), (700000, 100000),
    (1000000, 150000), (1500000, 300000), (2000000, 400000),
    (3000000, 500000), (5000000, 700000),
]


def _tse_price_limit_width(base: float) -> float:
    """基準値段(前日終値)に対する東証の制限値幅(片側)。"""
    for threshold, width in _TSE_LIMIT_TABLE:
        if base < threshold:
            return width
    return 1000000.0  # 1000万円以上 (フォールバック)


def _cap_to_price_limit(cli, symbol: str, side: str, target: float):
    """値幅制限(ストップ高/安=指値で指定可能な最大/最小値)でキャップする。

    kabu の board は UpperLimit/LowerLimit を返さないので、board の PreviousClose
    (前日終値=基準値段) に東証の制限値幅を足し引きして算出する。
    例: 5726 前日終値2700 → 値幅±500 → 上限3200/下限2200。
    前日終値が取れない(429/時間外)ときは None → 呼び出し側でスキップ(429ストーム防止)。"""
    base = None
    for i in range(4):
        try:
            board = cli.get_board(symbol)
        except Exception:
            board = None
        if board:
            base = (board.get("PreviousClose") or board.get("CalcPrice")
                    or board.get("CurrentPrice"))
        if base:
            break
        _time.sleep(1.0 + i)   # register/board の 429 をバックオフ再試行
    if not base:
        print(f"  ⚠ {symbol} 前日終値が取得できず → 今回スキップ(次回再試行)")
        return None
    width = _tse_price_limit_width(float(base))
    upper = float(base) + width
    lower = float(base) - width

    # 呼値(tick)に丸める。値幅上限/下限は端数だと kabu の値段チェック(トリガチェック
    # エラー 4002004 / 呼値エラー)で弾かれるため、売りは切り捨て・買い戻しは切り上げで
    # 必ず値幅内かつ呼値刻みに収める。通常の利確価格も同様に呼値へ丸める。
    import math as _math
    from backtest_limit_entry import tick_size as _tsz

    def _floor_tick(p: float) -> int:
        t = _tsz(float(p))
        return int(p // t) * t

    def _ceil_tick(p: float) -> int:
        t = _tsz(float(p))
        return int(_math.ceil(p / t)) * t

    if side == "short":
        # 買い戻し利確: 下限以上・呼値刻み(切り上げ)
        capped = max(float(target), lower)
        price = _ceil_tick(capped)
        if price < lower:
            price = _ceil_tick(lower)
        if float(target) < lower and (symbol, price) not in _CAP_WARNED:
            _CAP_WARNED.add((symbol, price))   # 銘柄+価格ごとに1回だけ(10秒ループのスパム防止)
            print(f"  ↘ {symbol} 利確{target:,.0f}が下限{lower:,.0f}割れ "
                  f"(前日終値{base:,.0f}-値幅{width:,.0f}) → 下限{price:,d}で発注")
        return price
    else:
        # 売り利確: 上限以下・呼値刻み(切り捨て)
        capped = min(float(target), upper)
        price = _floor_tick(capped)
        if float(target) > upper and (symbol, price) not in _CAP_WARNED:
            _CAP_WARNED.add((symbol, price))   # 銘柄+価格ごとに1回だけ(10秒ループのスパム防止)
            print(f"  ↗ {symbol} 利確{target:,.0f}が上限{upper:,.0f}超 "
                  f"(前日終値{base:,.0f}+値幅{width:,.0f}) → 上限{price:,d}で発注")
        return price


def _kabu_get(fn, *a, tries=4, **k):
    """429(レート制限)時にバックオフ再試行する get 系ラッパ。"""
    last = None
    for i in range(tries):
        try:
            return fn(*a, **k)
        except Exception as e:
            last = e
            if "429" in str(e) or "Too Many" in str(e):
                _time.sleep(1.0 + i)   # 1,2,3,4秒
                continue
            raise
    raise last


def _load_manual_targets() -> dict:
    """manual_targets.csv (手動指定の最優先override) → (sym,side)->{target,stop,strategy}。
    placed_orders にも my_positions.csv にも記録が無い保有(手動発注・記録欠落)の
    正しい利確/損切を、ユーザーがレポート取引明細の値で直接指定するためのファイル。
    列: symbol, side(long/short), target, stop, strategy(任意)。ヘッダ必須。"""
    import csv
    from pathlib import Path
    p = Path(__file__).resolve().parent / "manual_targets.csv"
    out: dict = {}
    if not p.exists():
        return out
    try:
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sym = str(row.get("symbol", "")).split(".")[0]
                side = "short" if str(row.get("side", "")).strip() == "short" else "long"
                tp = _f(row.get("target"))
                sp = _f(row.get("stop"))
                if sym and tp > 0:
                    out[(sym, side)] = {"target": tp, "stop": sp,
                                        "strategy": row.get("strategy", "")}
    except Exception:
        pass
    return out


def _load_placed_orders() -> dict:
    """placed_orders_*.csv(発注ボタンが記録したエントリー注文)から
    (sym, side) -> {"target":, "stop":, "strategy":} を返す。日付昇順で読み、
    同一(sym,side)は『最も新しい発注』で上書き。
    = エントリー時に記録した固定 target/stop で、レポート取引明細と同値。
      当日再計算(check_signal_on_date)のようにモード/データでズレない、
      最も信頼できる利確/損切の情報源。"""
    import csv, glob
    from pathlib import Path
    base = Path(__file__).resolve().parent
    out: dict = {}
    for fp in sorted(glob.glob(str(base / "placed_orders_*.csv"))):
        try:
            with open(fp, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sym = str(row.get("symbol", "")).split(".")[0]
                    side = "short" if str(row.get("side", "")).strip() == "short" else "long"
                    tp = _f(row.get("target"))
                    sp = _f(row.get("stop"))
                    if sym and tp > 0:
                        out[(sym, side)] = {"target": tp, "stop": sp,
                                            "strategy": row.get("strategy", "")}
        except Exception:
            continue
    return out


def _load_signal_targets() -> dict:
    """(symbol, side) -> {"target":利確価格, "strategy":戦略} を作る。

    保有中ポジションの利確目標は『エントリー時に確定した目標』を最優先する。
    = my_positions.csv の target_price (発注/登録時に記録=HTML取引明細が示す
      order_target と同値)。

    ※ 2026-07-05 是正: 以前は当日シグナル(signals_latest.json)を優先していたが、
      当日シグナルの目標は entry 後に前日終値/ATR/値幅が変わると再計算されて
      『本来より高く』上振れする(例: 4088 エントリー目標3,111 → 当日シグナル3,235)。
      逆指値戦略の利確目標はエントリー時に固定される値なので、保有ポジションには
      当日シグナルを一切使わない。ここでは my_positions.csv のエントリー目標だけを
      返し、CSV に無い保有は呼び出し側(_backfill_targets)が『約定値から当日以前の
      エントリーシグナルを逆引き』(lookup_stop_from_signal)で補完する。"""
    import csv
    from pathlib import Path
    out: dict = {}
    base = Path(__file__).resolve().parent
    # my_positions.csv = 発注/登録時に記録したエントリー目標(HTML取引明細の
    # order_target と同値)。保有ポジションの利確はこれを使う。
    pcsv = base / "my_positions.csv"
    if pcsv.exists():
        try:
            with open(pcsv, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("status", "")).strip() not in ("holding", "filled"):
                        continue
                    sym = str(row.get("symbol", "")).split(".")[0]
                    side = "short" if str(row.get("side", "")).strip() == "short" else "long"
                    tp = _f(row.get("target_price"))
                    if sym and tp > 0:
                        out[(sym, side)] = {"target": tp, "strategy": row.get("strategy", "")}
        except Exception:
            pass
    return out


def _active_close_map(cli) -> dict:
    """利確(決済)注文がある (symbol, side) → (order_id, price) の辞書。
    get_ordersは1回だけ(429回避)。long利確=売(1)/short利確=買戻(2)。"""
    out: dict = {}
    try:
        orders = _kabu_get(cli.get_orders)
    except Exception as e:
        print(f"  ⚠ 利確補完: 注文一覧取得失敗 ({e}) → 重複チェックなしで続行")
        return out
    # 5(終了=約定・失効・取消)は有効注文でない。除外。
    ACTIVE = {1, 2, 3, 4}
    for o in orders or []:
        if int(o.get("OrderState") or o.get("State") or 0) not in ACTIVE:
            continue
        sym = str(o.get("Symbol", "")).split(".")[0]
        s = str(o.get("Side", ""))
        side = "long" if s == "1" else "short" if s == "2" else None
        if side is None:
            continue
        try:
            price = float(o.get("Price") or 0)
        except Exception:
            price = 0.0
        oid = o.get("ID", "") or o.get("OrderId", "")
        out[(sym, side)] = (oid, price)   # 同一(sym,side)は1件想定
    return out


def _backfill_targets(cli) -> None:
    """実建玉を調べ、利確(決済)注文が無いポジションに利確指値を補完発注する。

    order_server が約定の瞬間に起動していなくても(9:00常駐不要)、接続時に保有を点検し
    取りこぼしを埋める。目標価格は signals_latest.json + my_positions.csv で照合。
    get_orders/get_positions は1回だけ叩き、429時はバックオフ再試行する。"""
    global _BACKFILL_COOLDOWN_UNTIL
    if not EXECUTE:
        return
    # 場が引けている時間帯(kabu:「場が引けました」)は利確指値を置けない。
    # 直前に検知したクールダウン中は丸ごとスキップ(30秒ごとの無限リトライ=429の嵐を防ぐ)。
    # 保有タブの更新は _regen_holdings が別途行うので影響しない。
    if _BACKFILL_COOLDOWN_UNTIL is not None and datetime.now(JST) < _BACKFILL_COOLDOWN_UNTIL:
        return
    try:
        positions = _kabu_get(cli.get_positions, product=0)
    except Exception as e:
        print(f"  ⚠ 利確補完: 建玉取得失敗のためスキップ ({e})")
        return
    if not positions:
        return
    # ── 建玉ごと(信用=建玉別行 / 現物=合算1行)に利確を補完 ──
    # 目標は「約定値(Price)に最も近い記録行」を引き当てる(_csv_entry_stop_target が
    # manual>placed>my_positions の優先順＋fill_price で建玉判別)。これで同一銘柄を
    # 複数建玉で持っても、建玉ごとに別々の目標で利確を発注できる。ExecutionID を
    # ClosePositions に渡し、その建玉だけを決済する。
    from close_stop_guard import _csv_entry_stop_target as _cst
    try:
        from close_stop_guard import lookup_stop_from_signal as _lookup
    except Exception:
        _lookup = None
    for p in positions:
        sym = str(p.get("Symbol", "")).split(".")[0]
        qty = int(p.get("LeavesQty") or p.get("HoldQty") or 0)
        if not sym or qty <= 0:
            continue
        side = "long" if str(p.get("Side", "")) == "2" else "short"
        _fp = float(p.get("Price") or 0)
        hold_id = str(p.get("ExecutionID", "") or "").strip()

        # 約定値マッチで固定目標を引き当て(建玉ごとに別々の値になる)
        _stop, _tgt, _strat, _d, _bt = _cst(sym, side, fill_price=_fp)
        # lss(同日決済ショート = 売建 かつ 戦略が *_S でない)は resting 利確を置かない。
        # 日中決済は lss_exit_watcher.py がポーリングで行うため、ここでは触らない。
        if side == "short" and not str(_strat or "").upper().endswith("_S"):
            continue
        _src = "記録(約定値マッチ)"
        if (not _tgt or _tgt <= 0) and _lookup is not None and _fp > 0:
            try:
                _s, _tgt, _strat = _lookup(sym, _fp, side == "short")
                _src = "約定値逆引き(記録なし)"
            except Exception:
                _tgt = None
        if not _tgt or _tgt <= 0:
            print(f"  ⚠ 利確補完できず: {sym} {side} 約定値{_fp:,.0f} の目標特定不可 "
                  f"→ 手動で利確を")
            continue
        try:
            st = _place_target_now(cli, {"symbol": sym, "side": side, "qty": qty,
                                         "target": float(_tgt), "strategy": _strat or "",
                                         "hold_id": hold_id})
        except Exception as e:
            print(f"  ⚠ 利確補完エラー {sym}: {e}")
            _time.sleep(0.6)
            continue
        # 場が引けている(kabu:「場が引けました」/Code100244)なら、残り建玉も同様に
        # 失敗する。30分クールダウンして中断し、429の嵐を止める(市場が開けば再開)。
        if "場が引け" in str(st) or "100244" in str(st):
            _BACKFILL_COOLDOWN_UNTIL = datetime.now(JST) + timedelta(minutes=30)
            print("  ⏸ 場が引けているため利確補完を30分休止します(市場が開いたら自動再開)")
            return
        # 実際に発注できたときのみ表示。失敗(fail/skip)・exists は無駄なので出さない
        # (Code8「決済指定内容に誤り」等で毎回失敗する建玉のログ氾濫を止める)。
        _st_str = str(st)
        _placed = ("placed" in _st_str) or ("exp=" in _st_str)
        if _placed:
            _hl = f" 建玉{hold_id[:8]}" if hold_id else ""
            print(f"    ▸ {sym}{_hl} 目標={float(_tgt):,.0f} 情報源={_src} 約定値={_fp:,.0f}")
            print(f"  🎯 利確補完(接続時) {sym} {side}{_hl} @{float(_tgt):,.0f} x{qty} : {st}")
        _time.sleep(0.6)   # 連続発注のレート制限(429)回避


def _regen_holdings(cli) -> None:
    """実建玉から保有銘柄HTML(holdings_<date>.html)を再生成する(📌保有タブ用)。
    kabu APIが一時失敗(空[])したときは『建玉なし』で上書きせず前回表示を維持する
    (保有があるのに消える誤表示を防ぐ)。"""
    try:
        import close_stop_guard as _csg
        from pathlib import Path
        positions = _csg.load_positions_from_kabu(cli, product=0, verbose=False)
        if not _csg.LAST_KABU_FETCH_OK:
            # 取得失敗: 保有ゼロとは限らない → 既存の保有HTMLを上書きしない
            print("  ⚠ 保有取得が一時失敗 → holdings_latest.html は上書きせず前回表示を維持")
            return
        html = _csg._build_holdings_html(positions, datetime.now(JST),
                                         price_fn=cli.get_current_price)
        out = Path(__file__).resolve().parent / "holdings_latest.html"
        out.write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"  ⚠ 保有HTML更新失敗: {e}")


def _watch_loop():
    """約定待ちを定期チェックし、約定したら利確指値を即発注する。
    併せて保有銘柄HTML(📌保有タブ)を定期更新する。"""
    cli = None
    cycle = 0
    _warned = False
    while True:
        _time.sleep(POLL_SEC)
        cycle += 1
        # 14:50の損切りタスクにkabuを譲る(トークン競合401/429を回避)。窓を抜けたら再接続。
        if GUARD_PAUSE_START <= datetime.now(JST).time() <= GUARD_PAUSE_END:
            if cli is not None:
                print("  ⏸ 14:50損切りタスクにkabuを譲るため監視を一時停止(〜14:53)")
                cli = None   # トークンを解放。窓を抜けた次サイクルで再接続する
            continue
        if cli is None:
            try:
                cli = _watch_build_client()
                print("  ✓ 監視用kabu接続OK → 保有タブ(holdings_latest.html)を生成します")
                _warned = False
                _regen_holdings(cli)   # 接続できたら即 保有タブ生成(起動直後に反映)
                _backfill_targets(cli)  # 接続時に利確の取りこぼしを補完発注(9:00常駐前提にしない)
            except Exception:
                if not _warned:   # 連続スパムを避け、最初の1回だけ警告
                    print("  ⚠ 監視用kabu未接続: kabuステーション(本番18080)を起動・ログインしてください。"
                          "接続できるまで保有タブ更新・約定監視は待機します。")
                    _warned = True
                continue
        # 約定待ちの利確発注
        with _pending_lock:
            items = list(_pending)
        for p in items:
            try:
                if _is_filled(cli, p["symbol"], p["side"]):
                    st = _place_target_now(cli, p)
                    print(f"  🎯 約定検知→利確 {p['symbol']} {p['side']} "
                          f"@{p['target']:,.0f} : {st}")
                    if st in ("placed", "exists"):
                        with _pending_lock:
                            if p in _pending:
                                _pending.remove(p)
                        _regen_holdings(cli)   # 約定したら即 保有タブ更新
            except Exception as e:
                print(f"  ⚠ 監視エラー {p.get('symbol')}: {e}")
        # 保有HTMLを約30秒ごとに更新(現在値・含み損益のリフレッシュ)+利確の取りこぼし点検
        if cycle % 3 == 1:
            _regen_holdings(cli)
            _backfill_targets(cli)   # 監視中に現れた建玉の利確抜けも定期補完(重複は出さない)


def _handoff_to_watcher() -> str:
    """発注サーバを停止し、lss_exit_watcher を新しいウィンドウで起動する(ハンドオフ)。
    kabu はトークン1個なので、watcher を起動したら発注サーバは終了して接続を解放する。
    watcher は holdings_latest.html を更新するので、レポートの📌保有タブは再読込で反映される。"""
    import subprocess, sys as _sys, threading as _th, os as _os
    from pathlib import Path as _P
    base = _P(__file__).resolve().parent
    cmd = [_sys.executable, "-u", str(base / "lss_exit_watcher.py"), "--execute"]
    if PROD:
        cmd.append("--prod")
    try:
        kwargs: dict = {"cwd": str(base)}
        if _os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # 別ウィンドウで表示
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        return f"watcher起動失敗: {e}(発注サーバは継続します)"

    # レスポンスを返し終えてから発注サーバを落とす(kabuトークンを解放)。
    def _bye():
        import time as _t
        _t.sleep(1.2)
        print("🔻 監視(watcher)にハンドオフ → 発注サーバを終了します(kabu接続を解放)。")
        _os._exit(0)
    _th.Thread(target=_bye, daemon=True).start()
    _lbl = "本番(18080)" if PROD else "デモ(18081)"
    return ("watcher(lss_exit_watcher --execute" + (" --prod" if PROD else "")
            + f" / {_lbl})を新しいウィンドウで起動しました。\n"
            "発注サーバは間もなく停止します(kabu接続を解放)。\n"
            "以降の保有・損益は📌保有タブを再読込すれば反映されます。")


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _text(self, msg: str, code: int = 200):
        data = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            arm = "⚠実発注" if EXECUTE else "dry-run"
            env = "本番(18080)" if PROD else "デモ(18081)"
            self._text(f"order_server 稼働中 / {arm} / 接続先 {env} / "
                       f"ロング{'現物' if GENBUTSU else '信用新規'}")
        elif path == "/handoff-watcher":
            # 発注サーバを止めて lss_exit_watcher に切り替える(kabuトークン解放)
            self._text(_handoff_to_watcher())
        else:
            self._text("404", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/order":
            self._text("不明な操作", 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in parse_qs(body).items()}
        try:
            msg = place_order(
                symbol=form.get("symbol", ""),
                entry=_f(form.get("entry")),
                qty=int(_f(form.get("qty")) or 100),
                side=form.get("side", "long"),
                strat=(form.get("strategy") or "").upper(),
                target=_f(form.get("target")),
                stop=_f(form.get("stop")),
                name=form.get("name", ""),
                bt=form.get("bt", ""),
                # H タブのボタンが entry_mode=limit を付けて送る。無ければ従来どおり逆指値。
                entry_mode=(form.get("entry_mode") or "stop"),
                atr=_f(form.get("atr")), sm=_f(form.get("sm")), tm=_f(form.get("tm")),
            )
        except Exception as e:
            msg = f"エラー: {e}"
        self._text(msg)

    def log_message(self, *args):
        pass  # ログ抑制


def main():
    global EXECUTE, PROD, GENBUTSU, PORT
    ap = argparse.ArgumentParser(description="シグナルレポート発注専用サーバ")
    ap.add_argument("--execute", action="store_true",
                    help="kabu に実発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--genbutsu", action="store_true",
                    help="ロングを現物で発注 (未指定なら信用新規)")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"待受ポート (既定 {PORT})")
    args = ap.parse_args()
    EXECUTE, PROD, GENBUTSU, PORT = args.execute, args.prod, args.genbutsu, args.port

    arm = "⚠実発注" if EXECUTE else "dry-run (接続なし・内容のみ)"
    env = "本番(18080)" if PROD else "デモ(18081)"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("=" * 64)
        print(f"✗ ポート {PORT} を確保できません ({e})")
        print(f"  → 別の(古い)order_server が既に {PORT} で動いています。")
        print(f"    そのままだと発注ボタンは『古い版』に繋がります(修正が反映されません)。")
        print(f"    古い order_server / run_signals_holdout_all を全て停止(Ctrl+C・")
        print(f"    ターミナルを閉じる)してから、もう一度起動してください。")
        print("=" * 64)
        return
    print(f"🚀 発注サーバを起動しました → http://{HOST}:{PORT}/order")
    print(f"   モード: {arm} / 接続先 {env} / ロング{'現物' if GENBUTSU else '信用新規'}")
    print(f"   トリガー: 呼値に丸め + 即約定回避(現値以上のショートは現値-1ティック)")
    print(f"   レポートの🚀発注ボタンがここに発注リクエストを送ります")
    if EXECUTE:
        threading.Thread(target=_watch_loop, daemon=True).start()
        print(f"   約定監視: ON ({POLL_SEC}秒間隔。約定したら利確指値を即発注)")
    else:
        print(f"   約定監視: OFF (dry-runのため。--execute で有効)")
    print(f"   停止するには Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.shutdown()


if __name__ == "__main__":
    main()
