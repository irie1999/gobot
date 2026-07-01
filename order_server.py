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
import threading
import time as _time
from datetime import datetime, timezone, timedelta, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _log_placed_order(rec: dict) -> None:
    """発注内容を placed_orders_<date>.csv に追記する(保有銘柄タブのソース)。"""
    import csv
    from pathlib import Path
    d = datetime.now(JST).strftime("%Y-%m-%d")
    path = Path(__file__).resolve().parent / f"placed_orders_{d}.csv"
    cols = ["placed_at", "symbol", "name", "strategy", "side", "qty",
            "entry", "stop", "target", "env"]
    new = not path.exists()
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerow({k: rec.get(k, "") for k in cols})
    except Exception as e:
        print(f"  ⚠ 発注ログ書き込み失敗: {e}")


def place_order(symbol: str, entry: float, qty: int, side: str,
                strat: str = "", target: float = 0.0,
                stop: float = 0.0, name: str = "") -> str:
    """逆指値エントリーを発注し、結果メッセージを返す。
    target>0 かつ実発注なら、約定監視に登録して約定後に利確指値を自動発注する。"""
    symbol = (symbol or "").split(".")[0].strip()
    side = "short" if side == "short" else "long"
    if not symbol or entry <= 0 or qty <= 0:
        return "発注失敗: 銘柄・逆指値・株数が不正です"

    # 既定は信用新規(2)。ロングで --genbutsu 指定時のみ現物(1)。ショートは常に信用。
    cash_margin = 1 if (side == "long" and GENBUTSU) else 2

    try:
        from kabu_api import KabuClient
        cli = KabuClient(prod=PROD, dry_run=not EXECUTE)
        if EXECUTE:                 # dry-run は接続不要 (内容プレビューのみ)
            cli.connect()
    except Exception as e:
        return f"発注失敗: kabu 接続エラー ({e})"

    # ── トリガー価格の自動調整 (kabu Code 100217 回避) ──
    # 逆指値買いはトリガーが現在値より上、逆指値売りは現在値より下でないと
    # 「即約定になる」と弾かれる。現値を取得し、必要なら現値±1ティックに調整する。
    adj_note = ""
    if EXECUTE:
        cur = cli.get_current_price(symbol)
        if cur and cur > 0:
            from backtest_limit_entry import tick_size, round_to_tick
            tick = tick_size(cur)
            if side == "long" and entry <= cur:
                new = round_to_tick(cur + tick)
                adj_note = f" ※現値{cur:,.0f}≧逆指値→{new:,.0f}に引上げ"
                entry = new
            elif side == "short" and entry >= cur:
                new = round_to_tick(cur - tick)
                adj_note = f" ※現値{cur:,.0f}≦逆指値→{new:,.0f}に引下げ"
                entry = new

    # 逆指値付き指値: 発火後は指値で約定（ロング:+3%上限 / ショート:-3%下限）。
    # ギャップが大きすぎる時は約定せず見送る＝バックテストの「+3%超キャンセル」と一致。
    try:
        from backtest_limit_entry import round_to_tick as _r2t
        limit_p = _r2t(entry * (0.97 if side == "short" else 1.03))
    except Exception:
        limit_p = round(entry * (0.97 if side == "short" else 1.03))

    try:
        if side == "short":
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
        if EXECUTE and target and target > 0:
            with _pending_lock:
                _pending.append({"symbol": symbol, "side": side, "qty": qty,
                                 "target": float(target), "strategy": strat})
            watch_note = f" / 約定したら利確@{float(target):,.0f}を自動発注(監視中)"
        if EXECUTE:
            _log_placed_order({
                "placed_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "name": name, "strategy": strat, "side": side,
                "qty": qty, "entry": f"{entry:.0f}", "stop": f"{stop:.0f}" if stop else "",
                "target": f"{target:.0f}" if target else "", "env": env,
            })
        return (f"🚀 発注完了: {symbol} {strat} {dir_label} x{qty}株 "
                f"({env}口座) OrderId={res.get('OrderId','')}{watch_note}")
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
    try:
        orders = cli.get_orders()
    except Exception:
        return False
    want = "1" if side == "long" else "2"
    ACTIVE = {1, 2, 3, 4, 5}
    for o in orders:
        if str(o.get("Symbol", "")).split(".")[0] != symbol:
            continue
        if str(o.get("Side", "")) == want and \
           int(o.get("OrderState") or o.get("State") or 0) in ACTIVE:
            return True
    return False


# kabu が受け付けた最長の ExpireDay 営業日オフセットをキャッシュ (毎回の探索を避ける)
_EXPIRE_OK_BDAYS: int | None = None


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


def _place_target_now(cli, p: dict) -> str:
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
    if _has_active_close_order(cli, symbol, side):
        return "exists"
    # 利確指値は値幅制限(ストップ高/安)超だと kabu の値段チェックで弾かれるので
    # 上限にキャップ。取得失敗時(429等)は弾かれる値を送らずスキップ(ストーム防止)。
    price = _cap_to_price_limit(cli, symbol, side, target)
    if price is None:
        return "skip(値幅取得失敗→次回再試行)"

    from backtest_limit_entry import default_max_hold
    mh = default_max_hold(strat)
    if _EXPIRE_OK_BDAYS is not None:
        # 既知の最長のみ試す (だめなら当日)
        cand = [_EXPIRE_OK_BDAYS] + ([0] if _EXPIRE_OK_BDAYS != 0 else [])
    else:
        # 長い順に候補。kabuが受け付ける最長を探す
        cand = [n for n in (mh, 10, 5, 4, 3, 2, 1, 0)]
        _seen: set = set()
        cand = [n for n in cand if not (n in _seen or _seen.add(n))]

    def _send(expire):
        if side == "short":
            return cli.send_buy(symbol, qty=qty, price=price, order_type="limit",
                                cash_margin=3, expire_day=expire)   # 信用返済(買戻)
        cm = 1 if GENBUTSU else 3   # 現物売(1) / 信用返済売(3)
        return cli.send_sell(symbol, qty=qty, price=price, order_type="limit",
                             cash_margin=cm, expire_day=expire)

    res = None
    for n in cand:
        try:
            res = _send(_expire_int(n))
        except Exception as e:
            return f"fail({e})"
        if (res.get("Result") == 0) or res.get("_dry_run"):
            _EXPIRE_OK_BDAYS = n   # 通った最長をキャッシュ
            return f"placed(exp={n}営業日)"
        # 有効期限エラー(Code5)なら短い候補へ。それ以外は即中断(スパム防止)
        if res.get("Code") == 5 or "有効期限" in str(res.get("Message", "")):
            continue
        return f"fail({res})"
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
    if side == "short":
        if target < lower:
            print(f"  ↘ {symbol} 利確{target:,.0f}が下限{lower:,.0f}割れ "
                  f"(前日終値{base:,.0f}-値幅{width:,.0f}) → 下限値で発注")
            return lower
    else:
        if target > upper:
            print(f"  ↗ {symbol} 利確{target:,.0f}が上限{upper:,.0f}超 "
                  f"(前日終値{base:,.0f}+値幅{width:,.0f}) → 上限値で発注")
            return upper
    return target


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


def _load_signal_targets() -> dict:
    """(symbol, side) -> {"target":利確価格, "strategy":戦略} を作る。
    当日シグナル(signals_latest.json)に加え、my_positions.csv(実保有の記録)も読み、
    古い保有(当日シグナルに無い銘柄)の利確目標もカバーする。my_positions.csvを優先。"""
    import json, csv
    from pathlib import Path
    out: dict = {}
    base = Path(__file__).resolve().parent
    # 1) 当日シグナル
    for fn, side in (("signals_latest.json", "long"),
                     ("signals_latest_short.json", "short")):
        p = base / fn
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for s in data.get("signals", []):
            sym = str(s.get("symbol", "")).split(".")[0]
            tp = _f(s.get("target_p"))
            if sym and tp > 0:
                out[(sym, side)] = {"target": tp, "strategy": s.get("strategy", "")}
    # 2) my_positions.csv(保有記録)で上書き＝古い保有もカバー
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


def _active_close_set(cli) -> set:
    """利確(決済)注文が既に出ている (symbol, side) の集合。get_ordersは1回だけ(429回避)。
    long利確=売(1) / short利確=買戻(2)。"""
    out: set = set()
    try:
        orders = _kabu_get(cli.get_orders)
    except Exception as e:
        print(f"  ⚠ 利確補完: 注文一覧取得失敗 ({e}) → 重複チェックなしで続行")
        return out
    ACTIVE = {1, 2, 3, 4, 5}
    for o in orders or []:
        if int(o.get("OrderState") or o.get("State") or 0) not in ACTIVE:
            continue
        sym = str(o.get("Symbol", "")).split(".")[0]
        s = str(o.get("Side", ""))
        if s == "1":
            out.add((sym, "long"))
        elif s == "2":
            out.add((sym, "short"))
    return out


def _backfill_targets(cli) -> None:
    """実建玉を調べ、利確(決済)注文が無いポジションに利確指値を補完発注する。

    order_server が約定の瞬間に起動していなくても(9:00常駐不要)、接続時に保有を点検し
    取りこぼしを埋める。目標価格は signals_latest.json + my_positions.csv で照合。
    get_orders/get_positions は1回だけ叩き、429時はバックオフ再試行する。"""
    if not EXECUTE:
        return
    try:
        positions = _kabu_get(cli.get_positions, product=0)
    except Exception as e:
        print(f"  ⚠ 利確補完: 建玉取得失敗のためスキップ ({e})")
        return
    agg: dict = {}
    for p in positions or []:
        sym = str(p.get("Symbol", "")).split(".")[0]
        qty = int(p.get("LeavesQty") or p.get("HoldQty") or 0)
        if not sym or qty <= 0:
            continue
        side = "long" if str(p.get("Side", "")) == "2" else "short"
        agg[(sym, side)] = agg.get((sym, side), 0) + qty
    if not agg:
        return
    targets = _load_signal_targets()
    have_close = _active_close_set(cli)   # 既存利確を一括取得(get_ordersは1回)
    for (sym, side), qty in agg.items():
        if (sym, side) in have_close:
            continue   # 既に利確あり
        info = targets.get((sym, side))
        if not info or info["target"] <= 0:
            print(f"  ⚠ 利確補完できず: {sym} {side} の目標価格が signals/my_positions.csv に無し "
                  f"→ 手動で利確を入れてください")
            continue
        try:
            st = _place_target_now(cli, {"symbol": sym, "side": side, "qty": qty,
                                         "target": info["target"], "strategy": info["strategy"]})
            print(f"  🎯 利確補完(接続時) {sym} {side} @{info['target']:,.0f} x{qty} : {st}")
        except Exception as e:
            print(f"  ⚠ 利確補完エラー {sym}: {e}")
        _time.sleep(0.6)   # 連続発注のレート制限(429)回避


def _regen_holdings(cli) -> None:
    """実建玉から保有銘柄HTML(holdings_<date>.html)を再生成する(📌保有タブ用)。"""
    try:
        from close_stop_guard import load_positions_from_kabu, _build_holdings_html
        from pathlib import Path
        positions = load_positions_from_kabu(cli, product=0, verbose=False)
        html = _build_holdings_html(positions, datetime.now(JST),
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"🚀 発注サーバを起動しました → http://{HOST}:{PORT}/order")
    print(f"   モード: {arm} / 接続先 {env} / ロング{'現物' if GENBUTSU else '信用新規'}")
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
