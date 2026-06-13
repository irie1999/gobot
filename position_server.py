"""
position_server.py — 保有ポジション管理 Web UI（ボタン操作）
=============================================================

position_tracker.py のロジックを再利用し、ブラウザ上でボタンクリックだけで
ポジションの登録・決済・確認ができるローカル Web サーバー。

使い方:
  python position_server.py
  → ブラウザが自動で開く (http://127.0.0.1:8765)
  → 画面のフォームに入力して「登録」ボタン
  → 各銘柄カードの「目標」「損切」「手動」ボタンで決済

止めるとき: ターミナルで Ctrl+C

依存: 標準ライブラリ http.server のみ（追加インストール不要）
CSV: my_positions.csv（position_tracker.py と共通）
"""

from __future__ import annotations

import html
import os
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd

import position_tracker as pt

HOST = "127.0.0.1"
PORT = 8765
TODAY = date.today()

# 現在値を1回のページ表示中だけキャッシュ（同じ銘柄を何度も叩かない）
_price_cache: dict[str, float | None] = {}


def _f(v) -> float:
    try:
        r = float(v)
        return 0.0 if r != r else r
    except (ValueError, TypeError):
        return 0.0


def _clean(v) -> str:
    s = str(v)
    return "" if s in ("nan", "None", "") else s


def _get_price(symbol: str) -> float | None:
    if symbol not in _price_cache:
        _price_cache[symbol] = pt.fetch_price(symbol)
    return _price_cache[symbol]


# ── HTML 生成 ────────────────────────────────────────────────────────────────
def render_page(message: str = "") -> str:
    df = pt.load()
    holding = df[df["status"] == "holding"]
    closed = df[df["status"].isin(["target", "stop", "timeout", "manual", "expired"])]

    # 現在値をまとめて取得（並列）
    from concurrent.futures import ThreadPoolExecutor
    syms = [str(r["symbol"]) for _, r in holding.iterrows()]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_get_price, [s for s in syms if s not in _price_cache]))

    cards = []
    for _, r in holding.iterrows():
        cards.append(_render_card(r))

    cards_html = "\n".join(cards) if cards else "<p class='empty'>保有中のポジションはありません</p>"

    # 決済済みサマリー
    closed_html = ""
    if not closed.empty:
        total = sum(_f(x) for x in closed["pnl"])
        wins = sum(1 for x in closed["pnl"] if _f(x) > 0)
        rate = wins / len(closed) * 100 if len(closed) else 0
        rows = []
        for _, r in closed.sort_values("exit_date", ascending=False).head(30).iterrows():
            pnl = _f(r["pnl"])
            cls = "pos" if pnl >= 0 else "neg"
            rows.append(
                f"<tr><td>{html.escape(_clean(r['symbol']))}</td>"
                f"<td>{html.escape(_clean(r['name']))}</td>"
                f"<td>{html.escape(_clean(r['exit_date']))}</td>"
                f"<td class='{cls}'>{'+' if pnl>=0 else ''}{pnl:,.0f}円</td>"
                f"<td>{html.escape(_clean(r['status']))}</td></tr>"
            )
        tcls = "pos" if total >= 0 else "neg"
        closed_html = f"""
        <h2>📁 決済済み（直近30件）</h2>
        <p>合計損益 <span class="{tcls} big">{'+' if total>=0 else ''}{total:,.0f}円</span>
           ／ 勝率 {rate:.0f}%（{wins}/{len(closed)}）</p>
        <table class="closed">
          <tr><th>コード</th><th>銘柄</th><th>決済日</th><th>損益</th><th>理由</th></tr>
          {''.join(rows)}
        </table>
        """

    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ポジション管理</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Hiragino Sans", sans-serif; margin: 0;
         background: #f4f6f9; color: #222; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 16px; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 28px; border-left: 4px solid #2d6cdf; padding-left: 8px; }}
  .msg {{ background: #e3f5e8; border: 1px solid #6ac285; color: #1c7a3c;
          padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; }}
  .addbox {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 20px;
             box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .addbox label {{ font-size: 12px; color: #666; display: block; margin-bottom: 2px; }}
  .addbox .row {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }}
  .addbox .fld {{ flex: 1; min-width: 90px; }}
  .addbox input, .addbox select {{ width: 100%; padding: 8px; border: 1px solid #ccc;
             border-radius: 6px; font-size: 14px; }}
  .btn {{ border: none; border-radius: 6px; padding: 9px 16px; font-size: 14px;
          cursor: pointer; color: #fff; font-weight: bold; }}
  .btn-add {{ background: #2d6cdf; }}
  .btn-tgt {{ background: #29a35a; }}
  .btn-stop {{ background: #d9534f; }}
  .btn-man {{ background: #888; }}
  .card {{ background: #fff; border-radius: 12px; padding: 14px 16px; margin-bottom: 12px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .card .head {{ display: flex; justify-content: space-between; align-items: center; }}
  .card .name {{ font-size: 17px; font-weight: bold; }}
  .card .meta {{ font-size: 12px; color: #777; margin: 4px 0; }}
  .card .prices {{ font-size: 13px; margin: 6px 0; }}
  .card .actions {{ display: flex; gap: 8px; margin-top: 10px; align-items: center; }}
  .card .actions input {{ width: 90px; padding: 7px; border: 1px solid #ccc; border-radius: 6px; }}
  .pos {{ color: #1c7a3c; font-weight: bold; }}
  .neg {{ color: #c1392b; font-weight: bold; }}
  .big {{ font-size: 18px; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px;
           font-weight: bold; }}
  .pill-ok {{ background: #e3f5e8; color: #1c7a3c; }}
  .pill-warn {{ background: #fff3cd; color: #9a6b00; }}
  .pill-danger {{ background: #fde2e0; color: #c1392b; }}
  table.closed {{ width: 100%; border-collapse: collapse; font-size: 13px; background:#fff; }}
  table.closed th, table.closed td {{ padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; }}
  .empty {{ color: #999; }}
</style></head>
<body><div class="wrap">
  <h1>📊 ポジション管理 <span style="font-size:13px;color:#999">{TODAY}</span></h1>
  {msg_html}

  <div class="addbox">
    <form method="POST" action="/add">
      <div class="row">
        <div class="fld"><label>証券コード</label><input name="symbol" required placeholder="4631"></div>
        <div class="fld"><label>約定値</label><input name="entry" type="number" step="any" required placeholder="4806"></div>
        <div class="fld"><label>損切値</label><input name="stop" type="number" step="any" placeholder="4486"></div>
        <div class="fld"><label>目標値</label><input name="target" type="number" step="any" placeholder="5069"></div>
        <div class="fld"><label>戦略</label>
          <select name="strategy">
            <option value="">-</option>
            <option>MACD</option><option>A7</option><option>RSI2</option>
            <option>DON</option><option>VOL</option><option>MOM</option>
          </select></div>
        <div class="fld"><label>株数</label><input name="qty" type="number" value="100"></div>
        <div class="fld"><label>区分</label>
          <select name="margin"><option value="3">信用</option><option value="1">現物</option></select></div>
        <div><button class="btn btn-add" type="submit">＋ 登録</button></div>
      </div>
    </form>
  </div>

  <h2>🟢 保有中（{len(holding)}件）</h2>
  {cards_html}

  {closed_html}
  <p style="color:#bbb;font-size:11px;margin-top:30px">
    停止: ターミナルで Ctrl+C ／ データ: my_positions.csv</p>
</div></body></html>"""


def _render_card(r) -> str:
    symbol = str(r["symbol"])
    name = _clean(r["name"]) or symbol
    strat = _clean(r["strategy"])
    fill_d = _clean(r["fill_date"])
    fill_p = _f(r["fill_price"])
    stop_p = _f(r["stop_price"])
    tgt_p = _f(r["target_price"])
    qty = int(_f(r["qty"]) or 100)
    margin = "信用" if _clean(r.get("cash_margin", "1")) == "3" else "現物"
    mh = pt.max_hold(strat)

    try:
        fd = datetime.strptime(fill_d, "%Y-%m-%d").date()
    except Exception:
        fd = TODAY
    hold_d = pt._business_days(fd, TODAY)
    remain = max(0, mh - hold_d)

    # 残り日数ピル
    if remain == 0:
        rem_pill = "<span class='pill pill-danger'>🔴 タイムカット超過</span>"
    elif remain <= 2:
        rem_pill = f"<span class='pill pill-warn'>⚠️ 残り{remain}日</span>"
    else:
        rem_pill = f"<span class='pill pill-ok'>残り{remain}日</span>"

    cur_p = _get_price(symbol)
    if cur_p:
        unreal = (cur_p - fill_p) * qty
        unreal_pct = (cur_p / fill_p - 1) * 100 if fill_p else 0
        cls = "pos" if unreal >= 0 else "neg"
        price_line = (f"現在値 <b>{cur_p:,.0f}円</b> "
                      f"<span class='{cls}'>含み {'+' if unreal>=0 else ''}{unreal:,.0f}円 "
                      f"({'+' if unreal_pct>=0 else ''}{unreal_pct:.1f}%)</span>")
        alert = ""
        if stop_p:
            to_stop = (cur_p / stop_p - 1) * 100
            if to_stop < 2:
                alert += " <span class='pill pill-danger'>🔴 損切り近接</span>"
            elif to_stop < 5:
                alert += f" <span class='pill pill-warn'>損切りまで{to_stop:.1f}%</span>"
        if tgt_p:
            to_tgt = (tgt_p / cur_p - 1) * 100
            if to_tgt < 3:
                alert += " <span class='pill pill-ok'>🎯 目標間近</span>"
        price_line += alert
        cur_val = f"{cur_p:.0f}"
    else:
        price_line = "現在値 取得中… (再読込で表示)"
        cur_val = ""

    stop_str = f"{stop_p:,.0f}円" if stop_p else "未設定"
    tgt_str = f"{tgt_p:,.0f}円" if tgt_p else "未設定"

    return f"""
    <div class="card">
      <div class="head">
        <span class="name">[{html.escape(symbol)}] {html.escape(name)}</span>
        {rem_pill}
      </div>
      <div class="meta">戦略 {html.escape(strat or '-')} ／ {margin} {qty}株 ／
        約定 {html.escape(fill_d)} ／ 保有 {hold_d}日 / MAX{mh}日</div>
      <div class="prices">約定 {fill_p:,.0f}円 ／ 損切 {stop_str} ／ 目標 {tgt_str}</div>
      <div class="prices">{price_line}</div>
      <div class="actions">
        <form method="POST" action="/close" style="display:flex;gap:6px;align-items:center">
          <input type="hidden" name="symbol" value="{html.escape(symbol)}">
          <input type="hidden" name="fill_date" value="{html.escape(fill_d)}">
          <input name="exit_price" type="number" step="any" placeholder="決済値" value="{cur_val}">
          <button class="btn btn-tgt"  name="reason" value="target">目標</button>
          <button class="btn btn-stop" name="reason" value="stop">損切</button>
          <button class="btn btn-man"  name="reason" value="manual">手動</button>
        </form>
      </div>
    </div>"""


# ── サーバー処理 ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send_html(self, body: str, code: int = 200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, msg: str = ""):
        from urllib.parse import quote
        self.send_response(303)
        self.send_header("Location", "/?msg=" + quote(msg))
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self._send_html("<h1>404</h1>", 404)
            return
        qs = parse_qs(parsed.query)
        msg = qs.get("msg", [""])[0]
        _price_cache.clear()  # 再読込で最新値を取り直す
        self._send_html(render_page(msg))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in parse_qs(body).items()}
        path = urlparse(self.path).path

        try:
            if path == "/add":
                msg = self._handle_add(form)
            elif path == "/close":
                msg = self._handle_close(form)
            else:
                msg = "不明な操作"
        except Exception as e:
            msg = f"エラー: {e}"
        self._redirect(msg)

    def _handle_add(self, form) -> str:
        df = pt.load()
        symbol = form["symbol"].split(".")[0].strip()
        entry = _f(form.get("entry"))
        stop = _f(form.get("stop"))
        target = _f(form.get("target"))
        strat = (form.get("strategy") or "").upper()
        qty = int(_f(form.get("qty")) or 100)
        margin = form.get("margin", "3")
        fill_date = str(TODAY)

        dup = df[(df["symbol"] == symbol) & (df["fill_date"] == fill_date) & (df["status"] == "holding")]
        if not dup.empty:
            return f"{symbol} は本日分が既に登録済みです"

        name = ""
        try:
            info = pt.yf.Ticker(f"{symbol}.T").info
            name = (info.get("shortName") or info.get("longName") or "")[:10]
        except Exception:
            pass

        row = {c: "" for c in pt.COLS}
        row.update({
            "record_date": str(TODAY), "symbol": symbol, "name": name,
            "strategy": strat, "family": "stop", "signal_date": fill_date,
            "signal_price": entry, "order_price": entry, "stop_price": stop,
            "target_price": target, "status": "holding", "fill_date": fill_date,
            "fill_price": entry, "updated_date": str(TODAY), "side": "long",
            "qty": qty, "cash_margin": margin,
        })
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        pt.save(df)
        return f"✅ {symbol}({name}) を登録しました（MAX{pt.max_hold(strat)}日）"

    def _handle_close(self, form) -> str:
        df = pt.load()
        symbol = form["symbol"].split(".")[0].strip()
        fill_date = form.get("fill_date", "")
        exit_p = _f(form.get("exit_price"))
        reason = form.get("reason", "manual")

        if exit_p <= 0:
            return "決済値を入力してください"

        mask = (df["symbol"] == symbol) & (df["status"] == "holding")
        if fill_date:
            mask &= (df["fill_date"] == fill_date)
        if mask.sum() == 0:
            return f"{symbol} の保有が見つかりません"
        idx = df[mask].index[0]
        entry_p = _f(df.at[idx, "fill_price"])
        qty = int(_f(df.at[idx, "qty"]) or 100)
        pnl = (exit_p - entry_p) * qty

        df.at[idx, "status"] = reason
        df.at[idx, "exit_date"] = str(TODAY)
        df.at[idx, "exit_price"] = str(exit_p)
        df.at[idx, "pnl"] = str(round(pnl))
        df.at[idx, "updated_date"] = str(TODAY)
        pt.save(df)
        rmap = {"target": "目標達成", "stop": "損切り", "timeout": "タイムカット", "manual": "手動決済"}
        return f"✅ {symbol} を決済（{rmap.get(reason, reason)}）損益 {'+' if pnl>=0 else ''}{pnl:,.0f}円"

    def log_message(self, *args):
        pass  # ログ抑制


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"📊 ポジション管理 Web UI を起動しました → {url}")
    print("   停止するには Ctrl+C")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.shutdown()


if __name__ == "__main__":
    main()
