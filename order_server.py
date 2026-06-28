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
from datetime import datetime, timezone, timedelta
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

    try:
        if side == "short":
            res = cli.send_stop_sell(symbol, qty=qty, trigger_price=entry,
                                     cash_margin=cash_margin)
            dir_label = f"逆指値売り(信用新規) @≤{entry:,.0f}{adj_note}"
        else:
            res = cli.send_stop_buy(symbol, qty=qty, trigger_price=entry,
                                    cash_margin=cash_margin)
            kind = "信用新規" if cash_margin == 2 else "現物"
            dir_label = f"逆指値買い({kind}) @≥{entry:,.0f}{adj_note}"
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


def _place_target_now(cli, p: dict) -> str:
    """約定後の利確指値を発注。重複は出さない。"""
    symbol, side, qty, target, strat = (p["symbol"], p["side"], p["qty"],
                                        p["target"], p["strategy"])
    if _has_active_close_order(cli, symbol, side):
        return "exists"
    import pandas as pd
    from backtest_limit_entry import default_max_hold
    mh = default_max_hold(strat)
    today = pd.Timestamp(datetime.now(JST).date())
    try:
        expire = int((today + pd.tseries.offsets.BDay(mh)).strftime("%Y%m%d"))
    except Exception:
        expire = 0
    try:
        if side == "short":
            res = cli.send_buy(symbol, qty=qty, price=target, order_type="limit",
                               cash_margin=3, expire_day=expire)   # 信用返済(買戻)
        else:
            cm = 1 if GENBUTSU else 3   # 現物売(1) / 信用返済売(3)
            res = cli.send_sell(symbol, qty=qty, price=target, order_type="limit",
                                cash_margin=cm, expire_day=expire)
    except Exception as e:
        return f"fail({e})"
    return "placed" if ((res.get("Result") == 0) or res.get("_dry_run")) else f"fail({res})"


def _regen_holdings(cli) -> None:
    """実建玉から保有銘柄HTML(holdings_<date>.html)を再生成する(📌保有タブ用)。"""
    try:
        from close_stop_guard import load_positions_from_kabu, _build_holdings_html
        from pathlib import Path
        positions = load_positions_from_kabu(cli, product=0, verbose=False)
        html = _build_holdings_html(positions, datetime.now(JST),
                                    price_fn=cli.get_current_price)
        d = datetime.now(JST).strftime("%Y-%m-%d")
        out = Path(__file__).resolve().parent / f"holdings_{d}.html"
        out.write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"  ⚠ 保有HTML更新失敗: {e}")


def _watch_loop():
    """約定待ちを定期チェックし、約定したら利確指値を即発注する。
    併せて保有銘柄HTML(📌保有タブ)を定期更新する。"""
    cli = None
    cycle = 0
    while True:
        _time.sleep(POLL_SEC)
        cycle += 1
        if cli is None:
            try:
                cli = _watch_build_client()
            except Exception as e:
                print(f"  ⚠ 監視用kabu接続失敗(次回再試行): {e}")
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
        # 保有HTMLを約30秒ごとに更新(現在値・含み損益のリフレッシュ)
        if cycle % 3 == 1:
            _regen_holdings(cli)


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
