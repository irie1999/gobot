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

# BTスコアキャッシュ（バックグラウンドで算出）
_bt_cache: dict[str, int] = {}  # "symbol::strategy" → score
_bt_cache_lock = threading.Lock()


def _find_signal_and_bt(symbol: str, fill_price: float, fill_date: str,
                         strategy: str = "") -> tuple[int | None, str, float, float]:
    """fill_price に一致するシグナルを全戦略で探し (BT, strat, stop_p, target_p) を返す。
    シグナル日 = fill_date の直前3営業日を検索。order_price と fill_price が ±5% 以内で一致。"""
    try:
        import check_signals_stop as _stop
        import check_signals_breakout as _brk
        from datetime import datetime as _dt, timedelta as _td

        sym_t = symbol + ".T"
        fd = _dt.strptime(fill_date, "%Y-%m-%d").date()

        # fill_date の直前 3 営業日を候補シグナル日とする
        sig_dates = []
        d = fd
        for _ in range(6):  # 最大6暦日さかのぼって3営業日を探す
            d -= _td(days=1)
            if d.weekday() < 5:
                sig_dates.append(d)
            if len(sig_dates) >= 3:
                break

        # 試す戦略
        strat_upper = (strategy or "").upper()
        candidates: list[tuple] = []
        if strat_upper in _stop.STRATEGY_PARAMS:
            candidates = [(_stop, strat_upper)]
        elif strat_upper in _brk.STRATEGY_PARAMS:
            candidates = [(_brk, strat_upper)]
        else:
            for s in _stop.STRATEGY_PARAMS:
                candidates.append((_stop, s))
            for s in _brk.STRATEGY_PARAMS:
                candidates.append((_brk, s))

        for sig_d in sig_dates:
            for mod, strat in candidates:
                try:
                    sig = mod.check_signal_on_date(sym_t, strat, sig_d)
                    if not sig:
                        continue
                    order_p = float(sig.get("order_p", 0) or 0)
                    if order_p <= 0:
                        continue
                    # fill_price が order_p の ±5% 以内ならシグナル一致
                    if abs(order_p - fill_price) / order_p > 0.05:
                        continue
                    # 一致 → BTスコア取得
                    key = f"{symbol}::{strat}"
                    with _bt_cache_lock:
                        if key in _bt_cache:
                            return (_bt_cache[key], strat,
                                    float(sig.get("stop_p", 0) or 0),
                                    float(sig.get("target_p", 0) or 0))
                    item = mod.backtest_one(sym_t, sig.get("name", ""), strat)
                    if item:
                        score, _ = mod.calc_recommend_score(item["period_results"])
                        with _bt_cache_lock:
                            _bt_cache[key] = score
                        return (score, strat,
                                float(sig.get("stop_p", 0) or 0),
                                float(sig.get("target_p", 0) or 0))
                except Exception:
                    continue
    except Exception:
        pass
    return None, "", 0.0, 0.0


def _bg_fill_bt_scores() -> None:
    """保有中ポジションのBTスコア・戦略・損切・目標をバックグラウンドで補完してCSVに保存する。"""
    try:
        df = pt.load()
        holding = df[df["status"] == "holding"]
        changed = False
        for idx, r in holding.iterrows():
            has_bt = _clean(r.get("bt_score", ""))
            has_stop = _f(r.get("stop_price", 0))
            has_tgt = _f(r.get("target_price", 0))
            if has_bt and has_stop and has_tgt:
                continue  # 全部あればスキップ
            sym = str(r["symbol"]).split(".")[0]
            strat = _clean(r.get("strategy", ""))
            fill_p = _f(r.get("fill_price", 0))
            fill_d = _clean(r.get("fill_date", ""))
            if not fill_p or not fill_d:
                continue
            score, matched_strat, stop_p, tgt_p = _find_signal_and_bt(sym, fill_p, fill_d, strat)
            if score is not None and not has_bt:
                df.at[idx, "bt_score"] = str(score)
                changed = True
            if matched_strat and not _clean(r.get("strategy", "")):
                df.at[idx, "strategy"] = matched_strat
                changed = True
            if stop_p and not has_stop:
                df.at[idx, "stop_price"] = str(stop_p)
                changed = True
            if tgt_p and not has_tgt:
                df.at[idx, "target_price"] = str(tgt_p)
                changed = True
        if changed:
            pt.save(df)
    except Exception:
        pass


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
        v = pt.fetch_price(symbol)
        # NaN ガード: float('nan') は None 扱いにする
        _price_cache[symbol] = None if (v is None or v != v) else v
    return _price_cache[symbol]


# ── シグナルページ ────────────────────────────────────────────────────────────

import json

SIGNAL_FILES = ["signals_latest.json", "signals_latest_short.json"]


def _load_signal_json() -> tuple[list[dict], str]:
    """run_signals_holdout_all.py が書き出したシグナルJSONを読む。
    ロング・ショート両方をマージして返す。(signals, generated_at) のタプル。"""
    merged: list[dict] = []
    generated = ""
    for fname in SIGNAL_FILES:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
        if not os.path.exists(p):
            continue
        try:
            data = json.loads(open(p, encoding="utf-8").read())
        except Exception:
            continue
        generated = data.get("generated_at", generated) or generated
        for s in data.get("signals", []):
            merged.append(s)
    return merged, generated


def _fetch_signals(date_str: str) -> tuple[list[dict], str]:
    """シグナルJSONを読み込んで返す。
    date_str が指定されていれば signal_date で絞り込む。
    戻り値: (signals, generated_at)"""
    signals, generated = _load_signal_json()
    if date_str:
        signals = [s for s in signals if str(s.get("signal_date", "")).startswith(date_str)]
    # スコア降順
    signals.sort(key=lambda s: s.get("score", 0), reverse=True)
    return signals, generated


def render_signals_page(date_str: str = "", message: str = "") -> str:
    # date_str 未指定 = JSONの全シグナルを表示 (絞り込みなし)
    signals, generated = _fetch_signals(date_str)
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""

    # JSONが1つも無い場合は案内を出す
    json_exists = any(
        os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), f))
        for f in SIGNAL_FILES
    )

    rows = ""
    for _ri, s in enumerate(signals):
        sym_code = s["symbol"].split(".")[0]
        side     = "short" if str(s["strategy"]).upper().endswith("_S") else "long"
        side_badge = "🔻" if side == "short" else "🔼"
        stop_pct = (s["order_p"] - s["stop_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        tgt_pct  = (s["target_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        strat_lower = s["strategy"].lower().rstrip("_s")
        qty_val = s.get("qty", 100)
        sig_score = s.get("score", s.get("bt_score", ""))
        sig_score_str = str(int(sig_score)) if sig_score != "" and sig_score == sig_score else ""
        rows += f"""<tr>
  <td class="sym">{html.escape(s['symbol'])}<br>
    <small>{html.escape(s['name'])}</small></td>
  <td style="text-align:center">
    <span class="tag tag-{strat_lower}">{html.escape(s['strategy'])}</span> {side_badge}</td>
  <td style="text-align:right;color:#94a3b8;font-size:12px">{html.escape(s['signal_date'])}</td>
  <td style="text-align:right;color:#38bdf8;font-weight:bold">{s['order_p']:,.0f}</td>
  <td style="text-align:right;color:#f87171">-{stop_pct:.1f}%<br><small>{s['stop_p']:,.0f}</small></td>
  <td style="text-align:right;color:#4ade80">+{tgt_pct:.1f}%<br><small>{s['target_p']:,.0f}</small></td>
  <td>
    <form method="POST" action="/add" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <input type="hidden" name="symbol"   value="{html.escape(sym_code)}">
      <input type="hidden" name="stop"     value="{s['stop_p']:.0f}">
      <input type="hidden" name="target"   value="{s['target_p']:.0f}">
      <input type="hidden" name="strategy" value="{html.escape(s['strategy'])}">
      <input type="hidden" name="qty"      id="qty_{_ri}" value="{qty_val}">
      <input type="hidden" name="side"     value="{side}">
      <input type="hidden" name="margin"   value="3">
      <input type="hidden" name="bt_score" value="{html.escape(sig_score_str)}">
      <input type="hidden" name="return_to" value="/signals?date={html.escape(date_str)}">
      <div class="qty-ctrl">
        <button type="button" class="qty-btn" onclick="adjQty({_ri},-100)">－</button>
        <span id="qty_d_{_ri}" class="qty-disp">{qty_val}株</span>
        <button type="button" class="qty-btn" onclick="adjQty({_ri},+100)">＋</button>
      </div>
      <input name="entry" type="number" step="any" value="{s['order_p']:.0f}"
             style="width:82px;padding:6px;border:1px solid #334155;border-radius:4px;font-size:13px;background:#0f172a;color:#e2e8f0"
             title="実際の約定値に修正してから登録">
      <button class="btn btn-add" type="submit">📥 登録</button>
    </form>
  </td>
</tr>"""

    if not rows:
        if not json_exists:
            empty = ('シグナルJSONがありません。先に '
                     '<code style="color:#fbbf24">python run_signals_holdout_all.py --force</code> '
                     '（ショートは <code style="color:#fbbf24">--short</code>）を実行してください。')
        elif date_str:
            empty = f'{html.escape(date_str)} に一致するシグナルなし（日付を空にすると全件表示）'
        else:
            empty = 'シグナルなし（最新レポートにエントリーシグナルがありません）'
        rows = (f'<tr><td colspan="7" style="text-align:center;color:#999;padding:24px">'
                f'{empty}</td></tr>')

    gen_note = f"（{html.escape(generated)} 生成）" if generated else ""
    count_note = (f"{len(signals)}件のシグナル{gen_note}" if signals
                  else f"シグナルなし{gen_note}")

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>シグナル確認 {html.escape(date_str)}</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,"Hiragino Sans",sans-serif; margin:0;
         background:#0f172a; color:#e2e8f0; }}
  .wrap {{ max-width:980px; margin:0 auto; padding:16px; }}
  h1 {{ font-size:20px; }}
  .msg {{ background:#1e3a2a; border:1px solid #4ade80; color:#4ade80;
          padding:10px 14px; border-radius:8px; margin-bottom:14px; }}
  .toolbar {{ display:flex; gap:12px; align-items:center; background:#1e293b;
              padding:12px 16px; border-radius:10px; margin-bottom:16px; }}
  .toolbar input[type=date] {{ padding:8px; border:1px solid #334155; border-radius:6px;
                               font-size:14px; background:#0f172a; color:#e2e8f0; }}
  .btn {{ border:none; border-radius:6px; padding:9px 16px; font-size:14px;
          cursor:pointer; color:#fff; font-weight:bold; }}
  .btn-primary {{ background:#2d6cdf; }}
  .btn-add {{ background:#16a34a; }}
  .back {{ color:#60a5fa; text-decoration:none; font-size:14px; }}
  .back:hover {{ text-decoration:underline; }}
  table {{ width:100%; border-collapse:collapse; background:#1e293b;
           border-radius:10px; overflow:hidden; }}
  th {{ background:#0f172a; color:#94a3b8; padding:10px; font-size:12px; text-align:center; }}
  td {{ padding:8px 10px; border-bottom:1px solid #334155; font-size:13px; vertical-align:middle; }}
  tr:hover td {{ background:#243045; }}
  .sym {{ min-width:100px; font-weight:600; }}
  .sym small {{ color:#94a3b8; font-weight:normal; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-rsi2  {{ background:#7c3aed; color:#ddd6fe; }}
  .tag-macd  {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7    {{ background:#065f46; color:#a7f3d0; }}
  .tag-don   {{ background:#0e7490; color:#cffafe; }}
  .tag-vol   {{ background:#92400e; color:#fef3c7; }}
  .tag-mom   {{ background:#4d7c0f; color:#d9f99d; }}
  .tag-rsi2_s{{ background:#6d28d9; color:#ddd6fe; }}
  .tag-a7_s  {{ background:#064e3b; color:#a7f3d0; }}
  .tag-macd_s{{ background:#1e3a8a; color:#bfdbfe; }}
  .qty-ctrl {{ display:flex; align-items:center; gap:2px; background:#0f172a;
               border:1px solid #334155; border-radius:6px; padding:2px 4px; }}
  .qty-btn {{ background:#334155; border:none; color:#e2e8f0; border-radius:4px;
              width:24px; height:24px; cursor:pointer; font-size:15px; line-height:1;
              display:flex; align-items:center; justify-content:center; }}
  .qty-btn:hover {{ background:#475569; }}
  .qty-disp {{ min-width:42px; text-align:center; font-size:13px; color:#e2e8f0; }}
</style>
<script>
function adjQty(ri, delta) {{
  var inp = document.getElementById('qty_' + ri);
  var v = Math.max(100, parseInt(inp.value || '100') + delta);
  inp.value = v;
  document.getElementById('qty_d_' + ri).textContent = v + '株';
}}
</script></head>
<body><div class="wrap">
  <h1>📋 シグナル確認</h1>
  {msg_html}
  <div class="toolbar">
    <a href="/" class="back">← ポジション管理</a>
    <form method="GET" action="/signals" style="display:flex;gap:8px;align-items:center">
      <label style="color:#64748b;font-size:12px">日付で絞込</label>
      <input type="date" name="date" value="{html.escape(date_str)}">
      <button class="btn btn-primary" type="submit">絞込</button>
    </form>
    <a href="/signals" class="back" style="font-size:12px">全件</a>
    <span style="color:#64748b;font-size:13px">{count_note}</span>
  </div>
  <table>
    <thead><tr>
      <th style="text-align:left">銘柄</th><th>戦略</th><th>シグナル日</th>
      <th>逆指値</th><th>損切り</th><th>目標</th>
      <th>株数 ／ 約定値 → 登録</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#475569;font-size:11px;margin-top:12px">
    ※ 約定値欄に実際の約定価格を入力してから「📥 登録」／逆指値ちょうどの場合はそのまま</p>
</div></body></html>"""


# ── HTML 生成 ────────────────────────────────────────────────────────────────
def render_page(message: str = "", prefill: dict | None = None) -> str:
    prefill = prefill or {}
    df = pt.load()
    holding = df[df["status"] == "holding"]
    closed = df[df["status"].isin(["target", "stop", "timeout", "manual", "expired"])]

    # BTスコア・戦略・損切・目標が未設定のポジションをバックグラウンドで補完
    needs_bt = holding[
        holding["bt_score"].fillna("").str.strip().eq("") |
        holding["stop_price"].fillna("").str.strip().eq("") |
        holding["target_price"].fillna("").str.strip().eq("")
    ]
    if not needs_bt.empty:
        threading.Thread(target=_bg_fill_bt_scores, daemon=True).start()

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
         background: #0f172a; color: #e2e8f0; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 16px; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 28px; border-left: 4px solid #2d6cdf; padding-left: 8px; }}
  .msg {{ background: #1e3a2a; border: 1px solid #4ade80; color: #4ade80;
          padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; }}
  .addbox {{ background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 20px;
             box-shadow: 0 1px 4px rgba(0,0,0,.3); }}
  .addbox-prefill {{ border: 2px solid #2d6cdf; }}
  .addbox label {{ font-size: 12px; color: #94a3b8; display: block; margin-bottom: 2px; }}
  .addbox .row {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }}
  .addbox .fld {{ flex: 1; min-width: 90px; }}
  .addbox input, .addbox select {{ width: 100%; padding: 8px; border: 1px solid #334155;
             border-radius: 6px; font-size: 14px; background: #0f172a; color: #e2e8f0; }}
  .btn {{ border: none; border-radius: 6px; padding: 9px 16px; font-size: 14px;
          cursor: pointer; color: #fff; font-weight: bold; }}
  .btn-add {{ background: #2d6cdf; }}
  .btn-tgt {{ background: #29a35a; }}
  .btn-stop {{ background: #d9534f; }}
  .btn-man {{ background: #888; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 14px 16px; margin-bottom: 12px;
           box-shadow: 0 1px 4px rgba(0,0,0,.3); }}
  .card .head {{ display: flex; justify-content: space-between; align-items: center; }}
  .card .name {{ font-size: 17px; font-weight: bold; }}
  .card .meta {{ font-size: 12px; color: #94a3b8; margin: 4px 0; }}
  .card .prices {{ font-size: 13px; margin: 6px 0; }}
  .card .actions {{ display: flex; gap: 8px; margin-top: 10px; align-items: center; }}
  .card .actions input {{ width: 90px; padding: 7px; border: 1px solid #334155;
           border-radius: 6px; background: #0f172a; color: #e2e8f0; }}
  .pos {{ color: #4ade80; font-weight: bold; }}
  .neg {{ color: #f87171; font-weight: bold; }}
  .big {{ font-size: 18px; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px;
           font-weight: bold; }}
  .pill-ok {{ background: #1e3a2a; color: #4ade80; }}
  .pill-warn {{ background: #3a3320; color: #fbbf24; }}
  .pill-danger {{ background: #3a2020; color: #f87171; }}
  table.closed {{ width: 100%; border-collapse: collapse; font-size: 13px; background:#1e293b; }}
  table.closed th, table.closed td {{ padding: 6px 8px; border-bottom: 1px solid #334155; text-align: left; }}
  .empty {{ color: #64748b; }}
</style></head>
<body><div class="wrap">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
    <h1 style="margin:0">📊 ポジション管理 <span style="font-size:13px;color:#999">{TODAY}</span></h1>
    <a href="/signals" style="display:inline-block;padding:8px 16px;background:#1e293b;
       color:#60a5fa;border-radius:8px;text-decoration:none;font-size:14px;font-weight:bold;
       border:1px solid #334155">📋 前日シグナル</a>
  </div>
  {msg_html}

  <div class="addbox{"" if not prefill else " addbox-prefill"}">
    {"<p style='color:#2d6cdf;font-size:13px;margin:0 0 8px'>📥 シグナルから自動入力しました。約定値を実際の約定価格に修正してから「＋ 登録」を押してください。</p>" if prefill else ""}
    <form method="POST" action="/add">
      <div class="row">
        <div class="fld"><label>証券コード</label><input name="symbol" required placeholder="4631" value="{html.escape(prefill.get('symbol', ''))}"></div>
        <div class="fld"><label>約定値</label><input name="entry" type="number" step="any" required placeholder="4806" value="{html.escape(prefill.get('entry', ''))}"></div>
        <div class="fld"><label>損切値</label><input name="stop" type="number" step="any" placeholder="4486" value="{html.escape(prefill.get('stop', ''))}"></div>
        <div class="fld"><label>目標値</label><input name="target" type="number" step="any" placeholder="5069" value="{html.escape(prefill.get('target', ''))}"></div>
        <div class="fld"><label>戦略</label>
          <select name="strategy">
            <option value="">-</option>
            {"".join(f'<option {"selected" if prefill.get("strategy","").upper()==s else ""}>{s}</option>' for s in ["MACD","A7","RSI2","DON","VOL","MOM"])}
          </select></div>
        <div class="fld"><label>株数</label><input name="qty" type="number" value="{html.escape(prefill.get('qty','100'))}"></div>
        <div class="fld"><label>方向</label>
          <select name="side">
            <option value="long"{" selected" if prefill.get("side","long")!="short" else ""}>ロング</option>
            <option value="short"{" selected" if prefill.get("side")=="short" else ""}>ショート</option>
          </select></div>
        <div class="fld"><label>区分</label>
          <select name="margin"><option value="3">信用</option><option value="1">現物</option></select></div>
        <div class="fld"><label>BTスコア</label><input name="bt_score" type="number" min="0" max="100" placeholder="0-100" value="{html.escape(prefill.get('bt_score', ''))}"></div>
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
    side = _clean(r.get("side", "long")) or "long"
    side_badge = ("<span class='pill pill-danger'>🔻 ショート</span>" if side == "short"
                  else "<span class='pill pill-ok'>🔼 ロング</span>")
    mh = pt.max_hold(strat)

    try:
        fd = datetime.strptime(fill_d, "%Y-%m-%d").date()
    except Exception:
        fd = TODAY
    hold_d = pt._business_days(fd, TODAY)
    remain = max(0, mh - hold_d)

    # 売却期限 = 約定日 + MAX保有(営業日)。土日をスキップして mh 営業日先を求める。
    _d = fd
    _added = 0
    while _added < mh:
        _d += timedelta(days=1)
        if _d.weekday() < 5:
            _added += 1
    deadline = _d
    _wd = "月火水木金土日"[deadline.weekday()]
    deadline_str = deadline.strftime("%Y-%m-%d") + f"（{_wd}）"

    # 残り日数ピル
    if remain == 0:
        rem_pill = "<span class='pill pill-danger'>🔴 タイムカット超過</span>"
    elif remain <= 2:
        rem_pill = f"<span class='pill pill-warn'>⚠️ 残り{remain}日</span>"
    else:
        rem_pill = f"<span class='pill pill-ok'>残り{remain}日</span>"

    cur_p = _get_price(symbol)
    if cur_p:
        # ショートは下がると利益
        if side == "short":
            unreal = (fill_p - cur_p) * qty
            unreal_pct = (1 - cur_p / fill_p) * 100 if fill_p else 0
        else:
            unreal = (cur_p - fill_p) * qty
            unreal_pct = (cur_p / fill_p - 1) * 100 if fill_p else 0
        cls = "pos" if unreal >= 0 else "neg"
        price_line = (f"現在値 <b>{cur_p:,.0f}円</b> "
                      f"<span class='{cls}'>含み {'+' if unreal>=0 else ''}{unreal:,.0f}円 "
                      f"({'+' if unreal_pct>=0 else ''}{unreal_pct:.1f}%)</span>")
        alert = ""
        if stop_p:
            # ショートは損切りが現在値より上、ロングは下
            to_stop = (stop_p / cur_p - 1) * 100 if side == "short" else (cur_p / stop_p - 1) * 100
            if to_stop < 2:
                alert += " <span class='pill pill-danger'>🔴 損切り近接</span>"
            elif to_stop < 5:
                alert += f" <span class='pill pill-warn'>損切りまで{to_stop:.1f}%</span>"
        if tgt_p:
            to_tgt = (cur_p / tgt_p - 1) * 100 if side == "short" else (tgt_p / cur_p - 1) * 100
            if to_tgt < 3:
                alert += " <span class='pill pill-ok'>🎯 目標間近</span>"
        price_line += alert
        cur_val = f"{cur_p:.0f}"
    else:
        price_line = "現在値 取得中… (再読込で表示)"
        cur_val = ""

    stop_str = f"{stop_p:,.0f}円" if stop_p else "未設定"
    tgt_str = f"{tgt_p:,.0f}円" if tgt_p else "未設定"

    bt_raw = _clean(r.get("bt_score", ""))
    # キャッシュから取得済みであればそちらを使う
    if not bt_raw:
        strat_key = _clean(r.get("strategy", ""))
        _cached = _bt_cache.get(f"{symbol}::{strat_key}", _bt_cache.get(f"{symbol}::"))
        if _cached is not None:
            bt_raw = str(_cached)
    if bt_raw and str(bt_raw).strip().isdigit():
        bt_val = int(bt_raw)
        if bt_val >= 80:
            bt_rank, bt_cls = "★★★", "pill-ok"
        elif bt_val >= 60:
            bt_rank, bt_cls = "★★", "pill-ok"
        elif bt_val >= 40:
            bt_rank, bt_cls = "★", "pill-warn"
        else:
            bt_rank, bt_cls = "△", "pill-danger"
        bt_pill = f"<span class='pill {bt_cls}'>BT:{bt_val} {bt_rank}</span>"
    else:
        bt_pill = "<span class='pill' style='background:#1e293b;color:#64748b;font-size:11px'>BT計算中…</span>"

    return f"""
    <div class="card">
      <div class="head">
        <span class="name">[{html.escape(symbol)}] {html.escape(name)}</span>
        <span>{side_badge} {bt_pill} {rem_pill}</span>
      </div>
      <div class="meta">戦略 {html.escape(strat or '-')} ／ {margin} {qty}株 ／
        約定 {html.escape(fill_d)} ／ 保有 {hold_d}日 / MAX{mh}日</div>
      <div class="meta">📅 売却期限 <b style="color:#fbbf24">{deadline_str}</b>（残り{remain}営業日）</div>
      <div class="prices">約定 {fill_p:,.0f}円 ／ 損切 {stop_str} ／ 目標 {tgt_str}</div>
      <div class="prices">{price_line}</div>
      <div class="actions">
        <form method="POST" action="/update" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
          <input type="hidden" name="symbol"    value="{html.escape(symbol)}">
          <input type="hidden" name="fill_date" value="{html.escape(fill_d)}">
          <input name="stop_price"   type="number" step="any" placeholder="損切値"
                 value="{stop_p if stop_p else ''}"
                 style="width:82px;padding:6px;border:1px solid {'#ef4444' if not stop_p else '#334155'};border-radius:4px;font-size:13px;background:#0f172a;color:#e2e8f0"
                 title="損切り価格（必須）">
          <input name="target_price" type="number" step="any" placeholder="目標値"
                 value="{tgt_p if tgt_p else ''}"
                 style="width:82px;padding:6px;border:1px solid #334155;border-radius:4px;font-size:13px;background:#0f172a;color:#e2e8f0">
          <button class="btn" style="background:#475569;padding:6px 10px;font-size:12px" type="submit">💾 更新</button>
        </form>
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

    def _redirect(self, msg: str = "", to: str = "/"):
        from urllib.parse import quote
        sep = "&" if "?" in to else "?"
        self.send_response(303)
        self.send_header("Location", to + sep + "msg=" + quote(msg))
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        msg = qs.get("msg", [""])[0]

        if parsed.path == "/signals":
            date_str = qs.get("date", [""])[0]
            self._send_html(render_signals_page(date_str, msg))
            return

        if parsed.path != "/":
            self._send_html("<h1>404</h1>", 404)
            return

        prefill: dict = {}
        if qs.get("prefill"):
            for key in ("symbol", "entry", "stop", "target", "strategy", "qty", "side"):
                v = qs.get(key, [""])[0]
                if v:
                    prefill[key] = v
        _price_cache.clear()  # 再読込で最新値を取り直す
        self._send_html(render_page(msg, prefill))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in parse_qs(body).items()}
        path = urlparse(self.path).path

        try:
            if path == "/add":
                msg, return_to = self._handle_add(form)
            elif path == "/close":
                msg, return_to = self._handle_close(form)
            elif path == "/update":
                msg, return_to = self._handle_update(form)
            else:
                msg, return_to = "不明な操作", "/"
        except Exception as e:
            msg, return_to = f"エラー: {e}", "/"
        self._redirect(msg, to=return_to)

    def _handle_add(self, form) -> tuple[str, str]:
        return_to = form.get("return_to", "/")
        df = pt.load()
        symbol = form["symbol"].split(".")[0].strip()
        entry = _f(form.get("entry"))
        stop = _f(form.get("stop"))
        target = _f(form.get("target"))
        strat = (form.get("strategy") or "").upper()
        qty = int(_f(form.get("qty")) or 100)
        margin = form.get("margin", "3")
        side = "short" if (form.get("side", "long") == "short") else "long"
        fill_date = str(TODAY)

        dup = df[(df["symbol"] == symbol) & (df["fill_date"] == fill_date) & (df["status"] == "holding")]
        if not dup.empty:
            return f"{symbol} は本日分が既に登録済みです", return_to

        name = ""
        try:
            info = pt.yf.Ticker(f"{symbol}.T").info
            name = (info.get("shortName") or info.get("longName") or "")[:10]
        except Exception:
            pass

        bt_score_raw = form.get("bt_score", "").strip()
        row = {c: "" for c in pt.COLS}
        row.update({
            "record_date": str(TODAY), "symbol": symbol, "name": name,
            "strategy": strat, "family": "stop", "signal_date": fill_date,
            "signal_price": entry, "order_price": entry, "stop_price": stop,
            "target_price": target, "status": "holding", "fill_date": fill_date,
            "fill_price": entry, "updated_date": str(TODAY), "side": side,
            "qty": qty, "cash_margin": margin, "bt_score": bt_score_raw,
        })
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        pt.save(df)
        _side_lbl = "ショート" if side == "short" else "ロング"
        return (f"✅ {symbol}({name}) を登録しました（{_side_lbl} / MAX{pt.max_hold(strat)}日）",
                return_to)

    def _handle_update(self, form) -> tuple[str, str]:
        """損切り価格・目標価格を更新する。"""
        df = pt.load()
        symbol = form["symbol"].split(".")[0].strip()
        fill_date = form.get("fill_date", "")
        stop_raw = form.get("stop_price", "").strip()
        tgt_raw  = form.get("target_price", "").strip()

        mask = (df["symbol"] == symbol) & (df["status"] == "holding")
        if fill_date:
            mask &= (df["fill_date"] == fill_date)
        if mask.sum() == 0:
            return f"{symbol} の保有が見つかりません", "/"
        idx = df[mask].index[0]

        updated = []
        if stop_raw:
            df.at[idx, "stop_price"] = stop_raw
            updated.append(f"損切={float(stop_raw):,.0f}円")
        if tgt_raw:
            df.at[idx, "target_price"] = tgt_raw
            updated.append(f"目標={float(tgt_raw):,.0f}円")

        if not updated:
            return "変更なし", "/"
        df.at[idx, "updated_date"] = str(TODAY)
        pt.save(df)
        return f"✅ {symbol} を更新しました（{'／'.join(updated)}）", "/"

    def _handle_close(self, form) -> tuple[str, str]:
        return_to = form.get("return_to", "/")
        df = pt.load()
        symbol = form["symbol"].split(".")[0].strip()
        fill_date = form.get("fill_date", "")
        exit_p = _f(form.get("exit_price"))
        reason = form.get("reason", "manual")

        if exit_p <= 0:
            return "決済値を入力してください", return_to

        mask = (df["symbol"] == symbol) & (df["status"] == "holding")
        if fill_date:
            mask &= (df["fill_date"] == fill_date)
        if mask.sum() == 0:
            return f"{symbol} の保有が見つかりません", return_to
        idx = df[mask].index[0]
        entry_p = _f(df.at[idx, "fill_price"])
        qty = int(_f(df.at[idx, "qty"]) or 100)
        side = _clean(df.at[idx, "side"]) or "long"
        # ショートは (約定値 - 決済値)、ロングは (決済値 - 約定値)
        pnl = (entry_p - exit_p) * qty if side == "short" else (exit_p - entry_p) * qty

        df.at[idx, "status"] = reason
        df.at[idx, "exit_date"] = str(TODAY)
        df.at[idx, "exit_price"] = str(exit_p)
        df.at[idx, "pnl"] = str(round(pnl))
        df.at[idx, "updated_date"] = str(TODAY)
        pt.save(df)
        rmap = {"target": "目標達成", "stop": "損切り", "timeout": "タイムカット", "manual": "手動決済"}
        return (f"✅ {symbol} を決済（{rmap.get(reason, reason)}）損益 {'+' if pnl>=0 else ''}{pnl:,.0f}円",
                return_to)

    def log_message(self, *args):
        pass  # ログ抑制


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"📊 ポジション管理 Web UI を起動しました → {url}")
    print("   停止するには Ctrl+C")
    from _open_html import open_html
    threading.Timer(0.8, lambda: open_html(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.shutdown()


if __name__ == "__main__":
    main()
