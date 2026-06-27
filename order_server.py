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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8765

EXECUTE = False   # True なら実発注。False なら dry-run (接続なし・内容のみ)
PROD    = False   # True なら本番(18080)。False ならデモ(18081)
MARGIN  = False   # True ならロングも信用新規。False ならロングは現物


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def place_order(symbol: str, entry: float, qty: int, side: str,
                strat: str = "") -> str:
    """逆指値エントリーを発注し、結果メッセージを返す。"""
    symbol = (symbol or "").split(".")[0].strip()
    side = "short" if side == "short" else "long"
    if not symbol or entry <= 0 or qty <= 0:
        return "発注失敗: 銘柄・逆指値・株数が不正です"

    cash_margin = 2 if (side == "short" or MARGIN) else 1  # 現物1 / 信用新規2

    try:
        from kabu_api import KabuClient
        cli = KabuClient(prod=PROD, dry_run=not EXECUTE)
        if EXECUTE:                 # dry-run は接続不要 (内容プレビューのみ)
            cli.connect()
    except Exception as e:
        return f"発注失敗: kabu 接続エラー ({e})"

    try:
        if side == "short":
            res = cli.send_stop_sell(symbol, qty=qty, trigger_price=entry,
                                     cash_margin=cash_margin)
            dir_label = f"逆指値売り(信用新規) @≤{entry:,.0f}"
        else:
            res = cli.send_stop_buy(symbol, qty=qty, trigger_price=entry,
                                    cash_margin=cash_margin)
            kind = "信用新規" if cash_margin == 2 else "現物"
            dir_label = f"逆指値買い({kind}) @≥{entry:,.0f}"
    except Exception as e:
        return f"発注失敗: {symbol} ({e})"

    env = "本番" if PROD else "デモ"
    if not EXECUTE:
        return (f"🧪 dry-run: {symbol} {strat} {dir_label} x{qty}株 "
                f"({env}) — 実発注は --execute で起動")
    ok = (res.get("Result") == 0) or res.get("_dry_run")
    if ok:
        return (f"🚀 発注完了: {symbol} {strat} {dir_label} x{qty}株 "
                f"({env}口座) OrderId={res.get('OrderId','')}")
    return f"⚠ 発注応答エラー: {symbol} {res}"


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
                       f"ロング{'信用' if MARGIN else '現物'}")
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
            )
        except Exception as e:
            msg = f"エラー: {e}"
        self._text(msg)

    def log_message(self, *args):
        pass  # ログ抑制


def main():
    global EXECUTE, PROD, MARGIN, PORT
    ap = argparse.ArgumentParser(description="シグナルレポート発注専用サーバ")
    ap.add_argument("--execute", action="store_true",
                    help="kabu に実発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--margin", action="store_true",
                    help="ロングも信用新規で発注 (未指定なら現物)")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"待受ポート (既定 {PORT})")
    args = ap.parse_args()
    EXECUTE, PROD, MARGIN, PORT = args.execute, args.prod, args.margin, args.port

    arm = "⚠実発注" if EXECUTE else "dry-run (接続なし・内容のみ)"
    env = "本番(18080)" if PROD else "デモ(18081)"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"🚀 発注サーバを起動しました → http://{HOST}:{PORT}/order")
    print(f"   モード: {arm} / 接続先 {env} / ロング{'信用新規' if MARGIN else '現物'}")
    print(f"   レポートの🚀発注ボタンがここに発注リクエストを送ります")
    print(f"   停止するには Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.shutdown()


if __name__ == "__main__":
    main()
