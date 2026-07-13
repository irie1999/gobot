"""
close_stop_guard.py — close 方式の損切りを自動化する「引け前ガード」
=====================================================================

CLAUDE.md §16 の損切り評価モードは既定が **close**（終値が損切り価格を割った
ときだけ引け成行で決済）です。ザラ場に逆指値を置きっぱなしにする intraday と違い、
close は「引けの瞬間の値段で判定」が本質なので、broker に置く 1 注文では再現
できません。そこで毎営業日の引け直前に判定して **引け成行 (MOC) 注文** を出す
このスクリプトで close 方式を自動化します。

【運用の流れ】

  ▼ 通常運用: 毎営業日 14:50〜15:25 JST に実行（市場クローズ前）
    python close_stop_guard.py                      # dry-run
    python close_stop_guard.py --execute            # デモ口座に成行発注
    python close_stop_guard.py --execute --prod     # 本番口座 ※明示必須

  ▼ post-close モード（引け後確認）: 毎営業日 15:30 以降に実行
    python close_stop_guard.py --post-close                  # dry-run
    python close_stop_guard.py --post-close --execute        # デモ口座に成行発注
    python close_stop_guard.py --post-close --execute --prod # 本番口座

【pre-close と post-close の違い】
  pre-close  : kabu 現在値で損切り判定 → 成行 (FrontOrderType=10) を即時発注。
               市場が開いているため即時約定。
  post-close : yfinance 終値で損切り判定 → 成行発注 (翌朝まで受付待ちになる場合あり)。
               15:30 以降は自動的にこのモードへ切替わる。

【ポジション管理の優先順位】
  既定 (--use-csv)   : forward_test_log.csv の filled/holding 行を使用
  --use-kabu-pos     : kabu の実建玉を取得して CSV と照合 (kabu を優先)
                       CSV にある損切り価格を建玉に紐付け。CSV にない建玉は警告。

【ショートポジションの MOC/MOO】
  is_short=True の場合: side="buy" (買い戻し) で発注。
  cash_margin は CSV の値を使うが、ショートは必ず信用建て (CASH_MARGIN_CLOSE=3)
  のはずなので、CSV が 1 (現物) になっている場合は自動で 3 に補正し警告を出す。

【建玉個別管理 (同一銘柄を複数建玉で持つ場合)】
  --kabu / --use-kabu-pos モードでは kabu 実建玉の ExecutionID を各ポジションの
  hold_id として保持し、決済(損切り/利確/タイムカット)を ClosePositions で
  「その建玉だけ」に発注する。これにより同一銘柄を 2 建玉持っても、建玉ごとに
  別々の損切り価格・利確価格で管理できる。
    ※ 前提: 信用建て (kabu_send_signals.py --margin でエントリー) であること。
      現物は同一銘柄が合算され建玉個別指定ができない (hold_id なし → 従来の
      銘柄単位 FIFO / 銘柄単位の利確重複判定にフォールバック)。
    ※ 制約: kabu /orders は返済注文の対象建玉を返さないため、決済時に同一銘柄の
      利確を一括取消する (別建玉の利確も一旦消える)。close_stop_guard は毎回
      利確価格も評価して決済するので取りこぼしはなく、次回 --with-targets で
      生存建玉へ利確が再発注される。

【安全設計】
  - デフォルトは dry-run。--execute を付けたときだけ実発注する。
  - --execute でも接続先は既定でデモ(18081)。本番は --prod を明示しないと使えない。
  - 損切りラインは forward_test_log.csv の stop_price をそのまま使う。

使い方:
  python close_stop_guard.py                          # dry-run (pre-close)
  python close_stop_guard.py --execute                # デモ口座に成行発注
  python close_stop_guard.py --execute --prod         # 本番口座に成行発注
  python close_stop_guard.py --post-close             # dry-run (post-close)
  python close_stop_guard.py --post-close --execute   # デモ口座に MOO 発注
  python close_stop_guard.py --use-kabu-pos --execute # kabu 建玉と照合して発注
  python close_stop_guard.py --log other.csv          # 別のログファイルを使う
  python close_stop_guard.py --aggressive             # aggressive ログを対象にする

  ▼ kabu建玉 + signals JSON モード（CSVなし・推奨）
  python close_stop_guard.py --kabu                   # dry-run
  python close_stop_guard.py --kabu --execute         # デモ口座に発注
  python close_stop_guard.py --kabu --execute --prod  # 本番口座に発注
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import pandas as pd

from kabu_api import KabuClient, CASH_GENBUTSU, CASH_MARGIN_CLOSE

JST = timezone(timedelta(hours=9))


# ────────────────────────────────────────────────────────────
# signals JSON から stop_p / target_p を読み込む
# ────────────────────────────────────────────────────────────
def _load_signals_json(json_path: str | None = None, verbose: bool = True) -> dict[str, list[dict]]:
    """全シグナルJSONをマージして symbol → [signal, ...] の辞書を返す。
    シンボルは ".T" サフィックスを除去してkabu形式に正規化する。"""

    def _try_load(path: str) -> list[dict]:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            sigs = data.get("signals", []) if isinstance(data, dict) else data
            return [s for s in sigs if s.get("symbol")]
        except Exception:
            return []

    if json_path:
        candidates = [json_path]
    else:
        dated_long  = sorted(
            glob.glob("signals_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"),
            reverse=True
        )
        dated_short = sorted(
            glob.glob("signals_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_short.json"),
            reverse=True
        )
        dated_pairs: list[str] = []
        all_dates = sorted(
            {f.replace("_short", "") for f in dated_long + dated_short},
            reverse=True
        )
        for d in all_dates:
            base  = d
            short = d.replace(".json", "_short.json")
            if base  in dated_long:  dated_pairs.append(base)
            if short in dated_short: dated_pairs.append(short)
        candidates = ["signals_latest.json", "signals_latest_short.json"] + dated_pairs

    result: dict[str, list[dict]] = {}
    loaded_files = []
    seen: set[tuple] = set()

    for path in candidates:
        sigs = _try_load(path)
        if not sigs:
            continue
        loaded_files.append(f"{Path(path).name}({len(sigs)}件)")
        for s in sigs:
            sym   = str(s["symbol"]).upper().removesuffix(".T")
            strat = s.get("strategy", "")
            key   = (sym, strat)
            if key not in seen:
                seen.add(key)
                result.setdefault(sym, []).append(s)

    if verbose:
        if result:
            print(f"  [シグナル] {', '.join(loaded_files)} を統合 ({len(seen)}件)")
        else:
            print("  [WARN] シグナルJSONが見つかりません")
    return result


def _lookup_signal(sym: str, sig_map: dict[str, list[dict]],
                   fill_price: float, is_short: bool
                   ) -> tuple[float | None, float | None, str, str]:
    """sig_map からシンボルに対応するstop_p/target_p/signal_dateを返す。
    複数戦略がある場合は fill_price に最も近い order_p を優先。"""
    candidates = sig_map.get(sym, [])
    if not candidates:
        return None, None, "", ""
    best = None
    for s in candidates:
        op = float(s.get("order_p") or 0)
        sp = float(s.get("stop_p")  or 0)
        tp = float(s.get("target_p") or 0)
        if sp <= 0:
            continue
        diff = abs(op - fill_price) / max(op, 1)
        if best is None or diff < best[4]:
            best = (sp, tp, s.get("strategy", ""), str(s.get("signal_date", "")), diff)
    if best:
        return best[0], best[1], best[2], best[3]
    return None, None, "", ""


def _csv_entry_stop_target(sym: str, side: str, fill_price: float | None = None
                           ) -> tuple[float | None, float | None, str, str, str]:
    """エントリー時に記録した固定 (stop, target, strategy, 約定日, bt) を返す。
    ⓪ manual_targets.csv (手動指定=最優先。fill_date列で約定日も指定可)
    ① placed_orders_*.csv (発注ボタンが記録。placed_at から約定日を導出)
    ② my_positions.csv
    のいずれかから取得。どれもレポート取引明細と同値で再計算しないのでズレない。

    ★同一銘柄を複数建玉で持つ場合: fill_price を渡すと、各ソース内で「記録された
      エントリー価格(entry/約定値)が fill_price に最も近い行」を選ぶ。これで建玉ごとに
      別々の stop/target を正しく引き当てる(合算して同じ値になるバグを回避)。
      価格列が無い/1行しか無い場合は従来どおり先頭行。
    見つからなければ (None, None, "", "", "")。"""
    import csv as _csv
    import glob as _glob
    from pathlib import Path as _Path
    base = _Path(__file__).resolve().parent

    def _num(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    def _pick(cands: list):
        """cands: [{'stop','target','strat','date','bt','price'}]。
        fill_price 指定かつ price を持つ候補が複数あれば最も近いものを選ぶ。"""
        if not cands:
            return None
        if fill_price and len(cands) > 1:
            withp = [c for c in cands if c["price"] > 0]
            if withp:
                return min(withp, key=lambda c: abs(c["price"] - fill_price))
        return cands[0]

    # ⓪ manual_targets.csv
    mp = base / "manual_targets.csv"
    cands = []
    if mp.exists():
        try:
            with open(mp, encoding="utf-8-sig") as f:
                for row in _csv.DictReader(f):
                    rsym = str(row.get("symbol", "")).split(".")[0]
                    rside = "short" if str(row.get("side", "")).strip() == "short" else "long"
                    if rsym != sym or rside != side:
                        continue
                    sp = _num(row.get("stop"))
                    tp = _num(row.get("target"))
                    if sp > 0:
                        price = _num(row.get("entry") or row.get("約定値")
                                     or row.get("fill_price") or row.get("price"))
                        cands.append({"stop": sp, "target": tp if tp > 0 else None,
                                      "strat": row.get("strategy", ""),
                                      "date": str(row.get("fill_date", "") or "").strip(),
                                      "bt": str(row.get("bt", "") or "").strip(),
                                      "price": price})
        except Exception:
            pass
    c = _pick(cands)
    if c:
        return (c["stop"], c["target"], c["strat"], c["date"], c["bt"])

    # ① placed_orders_*.csv (発注記録。price=entry=逆指値価格)
    cands = []
    for fp in sorted(_glob.glob(str(base / "placed_orders_*.csv"))):
        try:
            with open(fp, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    rsym = str(row.get("symbol", "")).split(".")[0]
                    rside = "short" if str(row.get("side", "")).strip() == "short" else "long"
                    if rsym != sym or rside != side:
                        continue
                    sp = _num(row.get("stop"))
                    tp = _num(row.get("target"))
                    if sp > 0:
                        cands.append({"stop": sp, "target": tp if tp > 0 else None,
                                      "strat": row.get("strategy", ""),
                                      "date": str(row.get("placed_at", "") or "")[:10],
                                      "bt": str(row.get("bt", "") or "").strip(),
                                      "price": _num(row.get("entry"))})
        except Exception:
            continue
    c = _pick(cands)
    if c:
        return (c["stop"], c["target"], c["strat"], c["date"], c["bt"])

    # ② my_positions.csv (price=fill_price)
    cands = []
    p = base / "my_positions.csv"
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    if str(row.get("status", "")).strip() not in ("holding", "filled"):
                        continue
                    rsym = str(row.get("symbol", "")).split(".")[0]
                    rside = "short" if str(row.get("side", "")).strip() == "short" else "long"
                    if rsym != sym or rside != side:
                        continue
                    sp = _num(row.get("stop_price"))
                    tp = _num(row.get("target_price"))
                    if sp > 0:
                        cands.append({"stop": sp, "target": tp if tp > 0 else None,
                                      "strat": row.get("strategy", ""),
                                      "date": str(row.get("fill_date", "") or "").strip(),
                                      "bt": str(row.get("bt", "") or "").strip(),
                                      "price": _num(row.get("fill_price"))})
        except Exception:
            pass
    c = _pick(cands)
    if c:
        return (c["stop"], c["target"], c["strat"], c["date"], c["bt"])

    return None, None, "", "", ""


def _target_already_set(sym: str, is_short: bool, open_orders: list[dict]) -> bool:
    """指定銘柄に利確指値(未約定)が既に出ているか。ロング=売り(1)/ショート=買戻し(2)。"""
    want_side = "2" if is_short else "1"
    ACTIVE = {1, 2, 3, 4, 5}
    for o in open_orders:
        if str(o.get("Symbol", "")).strip() != str(sym).strip():
            continue
        if str(o.get("Side", "")) != want_side:
            continue
        if int(o.get("OrderState") or o.get("State") or 0) in ACTIVE:
            return True
    return False


def _place_target_order(cli, pos: dict, open_orders: list[dict],
                        placed_holds: set | None = None) -> tuple[str, str]:
    """保有1件に利確指値を発注する。

    建玉個別管理:
      - pos["hold_id"] があれば ClosePositions でその建玉だけに利確を出す。
        同一銘柄を複数建玉で持っても、建玉ごとに別々の利確価格を置ける。
      - placed_holds(このrunで発注済みの HoldID 集合) を渡すと、同じ建玉への
        二重発注を防ぐ。銘柄単位ではなく建玉単位で重複判定する。
      - hold_id が無い(現物/合算)場合は従来どおり銘柄単位で重複判定する。
    返り値: (status, message)  status = placed / exists / skip / fail"""
    sym   = str(pos["symbol"]).split(".")[0]   # kabuは銘柄コードのみ
    tp    = pos.get("target_price")
    qty   = pos.get("qty", 100)
    cm    = pos.get("cash_margin", CASH_GENBUTSU)
    strat = pos.get("strategy", "")
    name  = pos.get("name", "")
    side_label = "ショート" if pos["is_short"] else "ロング"
    hid   = str(pos.get("hold_id") or "").strip()
    if not tp or tp <= 0:
        return "skip", f"  ? {sym} {name}: 目標価格なし → スキップ"
    if hid:
        # 建玉単位の重複判定 (このrunで既に同じ建玉に出していればスキップ)
        if placed_holds is not None and hid in placed_holds:
            return "exists", f"  ↺ {sym} {name} 建玉{hid[:8]}: 既に利確発注済 → スキップ"
    else:
        # hold_id 無し(現物/合算)は従来どおり銘柄単位で重複判定
        if _target_already_set(sym, pos["is_short"], open_orders):
            return "exists", f"  ↺ {sym} {name} [{strat}/{side_label}]: 既に利確注文あり → スキップ"
    # 有効期限 = タイムカット日 (YYYYMMDD)。約定日不明なら当日(0)。
    fd = pos.get("fill_date", "").strip()
    expire_day = 0
    if fd:
        try:
            from backtest_limit_entry import default_max_hold as _dmh
            expire_day = int((pd.Timestamp(fd) + pd.tseries.offsets.BDay(_dmh(strat)))
                             .strftime("%Y%m%d"))
        except Exception:
            expire_day = 0
    cp = _close_list_for(pos)   # 建玉個別: この建玉だけを返済
    hid_lbl = f" 建玉{hid[:8]}" if hid else ""
    try:
        if pos["is_short"]:
            res = cli.send_buy(sym, qty=qty, price=tp, order_type="limit",
                               cash_margin=CASH_MARGIN_CLOSE, expire_day=expire_day,
                               close_positions=cp)
            lbl = f"利確 指値買戻し @{tp:,.0f}"
        else:
            res = cli.send_sell(sym, qty=qty, price=tp, order_type="limit",
                                cash_margin=cm, expire_day=expire_day,
                                close_positions=cp)
            lbl = f"利確 指値売り @{tp:,.0f}"
    except Exception as e:
        return "fail", f"  ⚠ {sym} {name}: 利確発注失敗 ({e})"
    ok = (res.get("Result") == 0) or res.get("_dry_run")
    # 4001005 = 建玉拘束(既に返済注文あり)。二重ではなく「既に利確あり」の無害スキップ。
    if not ok and str(res.get("Code") or res.get("Result")) == "4001005":
        return "exists", f"  ↺ {sym} {name}{hid_lbl}: 建玉拘束(既に利確あり) → スキップ"
    exp_lbl = f"期限{expire_day}" if expire_day else "期限当日"
    if ok:
        if placed_holds is not None and hid:
            placed_holds.add(hid)
        return "placed", f"  🎯発注 {sym} {name} [{strat}/{side_label}]{hid_lbl} {lbl} x{qty} ({exp_lbl})"
    return "fail", f"  ⚠失敗 {sym} {name}: 応答 {res}"


def _build_holdings_html(positions: list[dict], now, price_fn=None) -> str:
    """実際に約定した保有銘柄(kabu建玉)の詳細HTMLを生成する。
    タイムカット日・損切り/利確・含み損益を表示。price_fn(sym)->現在値 を渡すと損益も表示。"""
    import html as _html
    from backtest_limit_entry import default_max_hold as _dmh, MAX_HOLD as _MH
    try:
        from backtest_limit_entry import timecut_enabled as _tc_enabled
        _tc_on = _tc_enabled()
    except Exception:
        _tc_on = True
    today_b = pd.Timestamp(now.date())

    rows_data = []
    for pos in positions:
        sym   = str(pos["symbol"]).split(".")[0]
        name  = pos.get("name", "")
        strat = pos.get("strategy", "")
        is_short = pos["is_short"]
        qty   = pos.get("qty", 100)
        fp    = pos.get("fill_price", 0) or 0
        sp    = pos.get("stop_price")
        tp    = pos.get("target_price")
        fd    = pos.get("fill_date", "").strip()
        mh    = _dmh(strat)
        # タイムカット日と残り
        if not _tc_on:
            # タイムカット無効: 目標/損切りに当たるまで保有。日付は出さない。
            if fd:
                try:
                    fb = pd.Timestamp(fd)
                    hold_days = len(pd.bdate_range(fb, today_b)) - 1
                except Exception:
                    hold_days = "—"
            else:
                hold_days = "—"
            tc_str, rem_str = "タイムカットなし", "—"
            sort_key = pd.Timestamp(fd) if fd else pd.Timestamp.max
        elif fd:
            try:
                fb = pd.Timestamp(fd)
                hold_days = len(pd.bdate_range(fb, today_b)) - 1
                tc = (fb + pd.tseries.offsets.BDay(mh)).normalize()
                tc_str = f"{tc:%Y-%m-%d(%a)}"
                remaining = mh - hold_days
                rem_str = ("本日以前" if remaining <= 0 else
                           "翌営業日" if remaining == 1 else f"あと{remaining}営業日")
                sort_key = tc
            except Exception:
                tc_str, rem_str, hold_days, sort_key = "—", "約定日不明", "—", pd.Timestamp.max
        else:
            tc_str, rem_str, hold_days, sort_key = "—", "約定日不明", "—", pd.Timestamp.max
        # 現在値・含み損益
        cur = price_fn(sym) if price_fn else None
        if cur and fp:
            pnl = (fp - cur) * qty if is_short else (cur - fp) * qty
        else:
            pnl = None
        rows_data.append({
            "sym": sym, "name": name, "strat": strat, "is_short": is_short,
            "qty": qty, "fp": fp, "sp": sp, "tp": tp, "fd": fd,
            "hold_days": hold_days, "mh": mh, "tc_str": tc_str, "rem_str": rem_str,
            "cur": cur, "pnl": pnl, "sort_key": sort_key,
            "bt": pos.get("bt", ""),   # シグナル発注時のBTスコア(記録があれば)
        })
    rows_data.sort(key=lambda r: r["sort_key"])

    def _pct(base, other):
        return f"{(other-base)/base*100:+.1f}%" if (base and other) else ""

    trs = ""
    total_pnl = 0.0
    for r in rows_data:
        side_lbl = "ショート" if r["is_short"] else "ロング"
        side_col = "#f87171" if r["is_short"] else "#38bdf8"
        sp_pct = ""
        tp_pct = ""
        if r["fp"] and r["sp"]:
            sp_pct = f"{(r['sp']-r['fp'])/r['fp']*100:+.1f}%"
        if r["fp"] and r["tp"]:
            tp_pct = f"{(r['tp']-r['fp'])/r['fp']*100:+.1f}%"
        cur_str = f"{r['cur']:,.0f}" if r["cur"] else "—"
        if r["pnl"] is not None:
            total_pnl += r["pnl"]
            pnl_col = "#4ade80" if r["pnl"] >= 0 else "#f87171"
            pnl_str = f"<span style='color:{pnl_col};font-weight:700'>{r['pnl']:+,.0f}円</span>"
        else:
            pnl_str = "—"
        rem_col = ("#94a3b8" if r["rem_str"] in ("—", "") else
                   "#f59e0b" if "あと" in r["rem_str"] or "翌" in r["rem_str"] else "#f87171")
        # シグナル発注時BT: 値があれば帯色付きで表示
        _bt = r.get("bt")
        try:
            _btv = int(float(_bt)) if _bt not in (None, "") else None
        except Exception:
            _btv = None
        if _btv is None:
            bt_cell = '<span style="color:#475569">—</span>'
        else:
            _bc = "#4ade80" if _btv >= 80 else ("#60a5fa" if _btv >= 60 else ("#fbbf24" if _btv >= 40 else "#f87171"))
            bt_cell = f'<span style="color:{_bc};font-weight:700">{_btv}</span>'
        # ── 状況（損切り/利確 の方向・距離）と行の色分け ──
        cur, fp2, sp2, tp2, sh2 = r["cur"], r["fp"], r["sp"], r["tp"], r["is_short"]
        status_cell = '<span style="color:#475569">—</span>'
        row_bg = ""
        if cur and fp2 and r["pnl"] is not None:
            if r["pnl"] >= 0:
                # 含み益（利確方向）
                row_bg = "background:rgba(74,222,128,0.10)"
                reached = ((cur >= tp2) if not sh2 else (cur <= tp2)) if tp2 else False
                if reached:
                    status_cell = ('<span style="background:#052e16;color:#4ade80;font-weight:700;'
                                   'padding:3px 9px;border-radius:5px">🟢 利確到達</span>')
                elif tp2:
                    d = abs(tp2 - cur) / cur * 100
                    status_cell = ('<span style="color:#4ade80;font-weight:700">🟢 含み益</span>'
                                   f'<br><span style="font-size:.72rem;color:#94a3b8">利確まで {d:.1f}%</span>')
                else:
                    status_cell = '<span style="color:#4ade80;font-weight:700">🟢 含み益</span>'
            else:
                # 含み損（損切り方向）
                row_bg = "background:rgba(248,113,113,0.10)"
                stopped = ((cur <= sp2) if not sh2 else (cur >= sp2)) if sp2 else False
                if stopped:
                    status_cell = ('<span style="background:#2d0a0a;color:#f87171;font-weight:700;'
                                   'padding:3px 9px;border-radius:5px">🔴 損切り到達</span>')
                elif sp2:
                    d = abs(cur - sp2) / cur * 100
                    status_cell = ('<span style="color:#f87171;font-weight:700">🔴 含み損</span>'
                                   f'<br><span style="font-size:.72rem;color:#94a3b8">損切りまで {d:.1f}%</span>')
                else:
                    status_cell = '<span style="color:#f87171;font-weight:700">🔴 含み損</span>'
        trs += f"""<tr style="{row_bg}">
  <td style="text-align:left">{_html.escape(r['sym'])}<br><span style="color:#64748b;font-size:.78rem">{_html.escape(r['name'])}</span></td>
  <td style="text-align:center">{_html.escape(r['strat'])}</td>
  <td style="text-align:center">{bt_cell}</td>
  <td style="text-align:center;color:{side_col}">{side_lbl}</td>
  <td style="text-align:right;color:#94a3b8">{_html.escape(r['fd'] or '—')}</td>
  <td style="text-align:right">{r['fp']:,.0f}円</td>
  <td style="text-align:right;color:#f87171">{(f"{r['sp']:,.0f}円<br><span style='font-size:.72rem'>{sp_pct}</span>") if r['sp'] else '—'}</td>
  <td style="text-align:right;color:#4ade80">{(f"{r['tp']:,.0f}円<br><span style='font-size:.72rem'>{tp_pct}</span>") if r['tp'] else '—'}</td>
  <td style="text-align:right;color:#e2e8f0">{cur_str}</td>
  <td style="text-align:right">{pnl_str}</td>
  <td style="text-align:center">{status_cell}</td>
  <td style="text-align:center;color:#f59e0b;font-weight:700">{r['tc_str']}</td>
  <td style="text-align:center;color:{rem_col}">{r['rem_str']}</td>
</tr>"""

    if not rows_data:
        trs = ('<tr><td colspan="13" style="text-align:center;color:#94a3b8;padding:24px">'
               '実際に約定した保有銘柄はありません（kabu建玉なし）</td></tr>')

    total_row = ""
    if any(r["pnl"] is not None for r in rows_data):
        tcol = "#4ade80" if total_pnl >= 0 else "#f87171"
        total_row = (f'<p style="margin-top:10px;font-size:1.05rem">含み損益合計: '
                     f'<span style="color:{tcol};font-weight:700">{total_pnl:+,.0f}円</span></p>')

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>保有銘柄 詳細 {now:%Y-%m-%d}</title>
<style>
  body {{ font-family:-apple-system,"Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; margin:0; padding:16px; }}
  h2 {{ font-size:1.15rem; border-left:4px solid #2d6cdf; padding-left:8px; }}
  .sub {{ color:#94a3b8; font-size:.85rem; margin-bottom:14px; }}
  table {{ width:100%; border-collapse:collapse; background:#1e293b; border-radius:10px; overflow:hidden; }}
  th {{ background:#0f172a; color:#94a3b8; padding:9px; font-size:.74rem; }}
  td {{ padding:8px 10px; border-bottom:1px solid #334155; font-size:.82rem; }}
  tr:hover td {{ background:#243045; }}
</style></head><body>
<h2>📌 保有銘柄 詳細（実際に約定した建玉）</h2>
<p class="sub">基準日 {now:%Y-%m-%d %H:%M} JST ／ {'保有期限(タイムカット) = 約定日 + 7営業日（全戦略7日に統一）' if _tc_on else '<span style="color:#fbbf24">タイムカット無効（目標/損切りに当たるまで保有）</span>'}<br>
  ※ kabuの実建玉のみ表示（実際に約定した銘柄）。損切り/利確はシグナル(発注時)の値。</p>
<table>
  <thead><tr>
    <th style="text-align:left">銘柄/名前</th><th>戦略</th><th>シグナル時<br>BT</th><th>区分</th><th>約定日</th><th>約定値</th>
    <th>損切り</th><th>利確目標</th><th>現在値</th><th>含み損益</th><th>状況</th>
    <th>タイムカット日</th><th>残り</th>
  </tr></thead>
  <tbody>{trs}</tbody>
</table>
{total_row}
</body></html>"""


def _fill_date_from_signal(signal_date: str) -> str:
    """シグナル日(終値判定)の翌営業日 = 逆指値約定日 を返す。空なら空文字。"""
    if not signal_date:
        return ""
    try:
        return (pd.Timestamp(signal_date)
                + pd.tseries.offsets.BDay(1)).strftime("%Y-%m-%d")
    except Exception:
        return ""


# ────────────────────────────────────────────────────────────
# kabu建玉 + signals JSON からポジション一覧を構築（--kabu モード）
# ────────────────────────────────────────────────────────────
LAST_KABU_FETCH_OK = True   # 直近の get_positions が成功したか。
# 一時的なAPIエラー(空[])を「建玉なし」と誤表示・誤上書きしないため、呼び出し側が参照する。


def load_positions_from_kabu(cli: KabuClient, product: int = 2,
                             verbose: bool = True) -> list[dict]:
    """kabu の実建玉を取得し、signals JSON で stop/target を補完して返す。
    verbose=False で進捗printを抑制(保有HTMLの定期更新などで使う)。
    取得の成否は module 変数 LAST_KABU_FETCH_OK に記録(失敗時 False)。"""
    global LAST_KABU_FETCH_OK
    def _p(*a):
        if verbose:
            print(*a)
    try:
        raw = cli.get_positions(product=product)
        LAST_KABU_FETCH_OK = True
    except Exception as e:
        LAST_KABU_FETCH_OK = False
        _p(f"  ✗ kabu 建玉取得失敗: {e}")
        return []

    sig_map = _load_signals_json(verbose=verbose)
    positions: list[dict] = []

    for kp in raw:
        sym  = str(kp.get("Symbol", "")).upper().removesuffix(".T")
        name = (kp.get("SymbolName") or kp.get("Name") or sym)[:16]
        side_str = str(kp.get("Side", ""))
        is_short = (side_str == "1")
        leaves   = int(kp.get("LeavesQty") or kp.get("Qty") or 0)
        if leaves == 0:
            continue
        fill_p = float(kp.get("AveragePrice") or kp.get("Price") or 0)
        margin_type = int(kp.get("MarginTradeType") or 1)
        cm = CASH_MARGIN_CLOSE if margin_type >= 1 else CASH_GENBUTSU
        # 建玉個別決済用の HoldID。kabu /positions の実フィールド名を確定できて
        # いないため ExecutionID / HoldID の両方を試す(空なら建玉個別ができず
        # FIFO にフォールバック)。信用建てなのに空なら警告する。
        hold_id = str(kp.get("ExecutionID") or kp.get("HoldID") or "").strip()
        if cm == CASH_MARGIN_CLOSE and not hold_id:
            _p(f"  ⚠ {sym} {name}: 建玉ID(ExecutionID)が空 → 建玉個別決済ができず"
               f"FIFO/銘柄単位にフォールバックします(同一銘柄複数建玉なら利確が1本のみ)")

        # 【第一情報源】my_positions.csv のエントリー記録(=レポート取引明細と同値の
        # 固定 stop/target)。check_signal_on_date 再計算はモード(con/agg)/データ調整で
        # ズレる(例: 4088 記録3,111 → 再計算3,235)ため、記録があればそれを最優先。
        _side_lbl = "short" if is_short else "long"
        # fill_p を渡して、同一銘柄の複数建玉でも約定値に最も近い記録行を引き当てる
        stop_p, tgt_p, strat, _csv_fill_date, _csv_bt = _csv_entry_stop_target(
            sym, _side_lbl, fill_price=fill_p)
        sig_date = ""
        _fixed_fill_date = ""   # CSV/手動指定が持つ約定日(あれば直接採用)
        _entry_bt = _csv_bt     # シグナル発注時のBT(記録があれば)
        if stop_p is not None:
            src = "my_positions.csv"
            _fixed_fill_date = _csv_fill_date
            _p(f"  ✓ {sym} {name}: stop={stop_p:.0f} target={tgt_p or 0:.0f} [{strat}] (記録/手動)")
        else:
            # フォールバック1: 約定値からエントリーシグナルを逆引き(CSV記録なしの保有)
            if fill_p > 0:
                try:
                    stop_p, tgt_p, strat = lookup_stop_from_signal(sym, fill_p, is_short)
                except Exception:
                    stop_p = None
            if stop_p is not None:
                src = "signal_backtrack"
                _p(f"  ✓ {sym} {name}: stop={stop_p:.0f} target={tgt_p:.0f} [{strat}] (約定値逆引き)")
            else:
                # フォールバック2: 当日シグナルJSON(約定値マッチ)。約定日もここで取得。
                stop_p, tgt_p, strat, sig_date = _lookup_signal(sym, sig_map, fill_p, is_short)
                if stop_p is not None:
                    src = "signals_json"
                    _p(f"  ✓ {sym} {name}: stop={stop_p:.0f} target={tgt_p:.0f} [{strat}] (当日JSON照合)")
                elif fill_p > 0:
                    # フォールバック3: ATR推定
                    stop_p = calc_atr_stop(sym, fill_p, is_short, strat or "")
                    src = "atr_estimate"
                    if stop_p:
                        _p(f"  ⚠ {sym} {name}: ATR推定 stop={stop_p:.0f}")
                    else:
                        _p(f"  ✗ {sym} {name}: 損切り価格取得失敗 → 判定スキップ")
                else:
                    src = "missing"
        # 約定日: 手動指定/記録の fill_date を最優先、無ければシグナル日から導出
        fill_date = _fixed_fill_date or _fill_date_from_signal(sig_date)

        positions.append({
            "symbol":       sym,
            "name":         name,
            "strategy":     strat or "?",
            "stop_price":   stop_p,
            "target_price": tgt_p,
            "is_short":     is_short,
            "qty":          leaves,
            "fill_price":   fill_p,
            "fill_date":    fill_date,
            "cash_margin":  cm,
            "source":       src,
            "bt":           _entry_bt,   # シグナル発注時のBT(記録があれば表示)
            # 建玉個別決済用の HoldID。同一銘柄を複数建玉で持つとき、この ID で
            # 建玉ごとに別々の損切り/利確を発注する。
            "hold_id":      hold_id,
        })

    return positions


# ────────────────────────────────────────────────────────────
# 保有ポジションの読み込み
# ────────────────────────────────────────────────────────────
_MY_POS_CSV = "my_positions.csv"


def _default_log_path(aggressive: bool) -> str:
    # my_positions.csv が存在すればそちらを優先（kabu station 運用）
    if not aggressive and Path(_MY_POS_CSV).exists():
        return _MY_POS_CSV
    return "forward_test_log_aggressive.csv" if aggressive else "forward_test_log.csv"


def load_open_positions(log_path: str) -> list[dict]:
    """my_positions.csv または forward_test_log.csv から保有中ポジションを返す。"""
    p = Path(log_path)
    if not p.exists():
        print(f"⚠ ログが見つかりません: {log_path}")
        return []

    open_pos: list[dict] = []
    no_stop: list[str] = []
    with p.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status") not in ("filled", "holding"):
                continue
            sym = row.get("symbol", "").strip()
            name = row.get("name", "").strip()
            strat = row.get("strategy", "").strip()
            sp_raw = row.get("stop_price", "").strip()
            try:
                stop_price = float(sp_raw) if sp_raw else None
            except ValueError:
                stop_price = None

            tp_raw = row.get("target_price", "").strip()
            try:
                target_price: float | None = float(tp_raw) if tp_raw else None
            except ValueError:
                target_price = None

            # stop_price 未設定 → ATR から自動算出
            if stop_price is None:
                side_tmp = (row.get("side") or "long").strip().lower()
                is_short_tmp = side_tmp in ("short", "sell", "s")
                fp_raw = row.get("fill_price", "").strip()
                try:
                    fp = float(fp_raw) if fp_raw else 0.0
                except ValueError:
                    fp = 0.0
                if fp > 0:
                    stop_price = calc_atr_stop(sym, fp, is_short_tmp, strat)
                if stop_price is not None:
                    no_stop.append(f"{sym} {name}(ATR推定={stop_price:.0f})")
                else:
                    no_stop.append(f"{sym} {name}(推定不可→スキップ)")
                    continue
            side = (row.get("side") or "long").strip().lower()
            is_short = side in ("short", "sell", "s")
            try:
                cm = int(row.get("cash_margin") or 1)
            except ValueError:
                cm = CASH_GENBUTSU
            # ショートは必ず信用建て。CSV が現物(1)になっていたら補正する。
            if is_short and cm == CASH_GENBUTSU:
                print(f"  ⚠ {sym}: ショートなのに cash_margin=1 (現物) → 3 (信用返済) に補正")
                cm = CASH_MARGIN_CLOSE
            open_pos.append({
                "symbol": sym,
                "name": name,
                "strategy": row.get("strategy", "").strip(),
                "stop_price": stop_price,
                "target_price": target_price,
                "is_short": is_short,
                "qty": int(float(row.get("qty") or 100)),
                "fill_date": row.get("fill_date", "").strip(),
                "cash_margin": cm,
                "source": "csv",
            })
    if no_stop:
        print(f"  ⚠ 損切り価格が未設定だったポジション ({len(no_stop)}件):")
        for s in no_stop:
            print(f"     {s}")
        print(f"     ※ ATR推定値で判定継続。正確な値は position_server で登録してください")
    return open_pos


# ────────────────────────────────────────────────────────────
# シグナルから損切り価格を逆引き
# ────────────────────────────────────────────────────────────
def lookup_stop_from_signal(symbol: str, fill_price: float, is_short: bool
                             ) -> tuple[float | None, float | None, str]:
    """check_signal_on_date を使って損切り価格を逆引きする。

    直近 30 営業日を全戦略で検索し、order_price ≈ fill_price のシグナルを照合。
    ロングなら stop_price < fill_price、ショートなら stop_price > fill_price を確認。
    戻り値: (stop_price, target_price, strategy_name) — 見つからなければ (None, None, "")
    """
    try:
        import check_signals_stop as _stop
        import check_signals_breakout as _brk
        from datetime import timedelta as _td

        sym_t = symbol + ".T"
        today = date.today()

        # 直近 30 営業日を生成
        sig_dates: list[date] = []
        d = today
        for _ in range(50):
            d -= _td(days=1)
            if d.weekday() < 5:
                sig_dates.append(d)
            if len(sig_dates) >= 30:
                break

        # 試す戦略一覧
        candidates: list[tuple] = []
        for s in _stop.STRATEGY_PARAMS:
            candidates.append((_stop, s))
        for s in _brk.STRATEGY_PARAMS:
            candidates.append((_brk, s))

        best: tuple | None = None  # (stop_p, target_p, strategy, diff_pct)
        for sig_d in sig_dates:
            for mod, strat in candidates:
                try:
                    sig = mod.check_signal_on_date(sym_t, strat, sig_d)
                    if not sig:
                        continue
                    order_p = float(sig.get("order_price", 0) or 0)
                    if order_p <= 0:
                        continue
                    diff_pct = abs(order_p - fill_price) / order_p
                    if diff_pct > 0.08:   # ±8% 以内
                        continue
                    stop_p = float(sig.get("stop_price", 0) or 0)
                    tgt_p  = float(sig.get("target_price", 0) or 0)
                    if stop_p <= 0:
                        continue
                    # 方向チェック: ロングは stop < fill, ショートは stop > fill
                    if not is_short and stop_p >= fill_price:
                        continue
                    if is_short and stop_p <= fill_price:
                        continue
                    if best is None or diff_pct < best[3]:
                        best = (stop_p, tgt_p, strat, diff_pct)
                except Exception:
                    continue

        if best is not None:
            return best[0], best[1], best[2]
    except Exception as e:
        print(f"  ⚠ {symbol}: シグナル逆引き失敗 ({e})")
    return None, None, ""


# ────────────────────────────────────────────────────────────
# kabu 建玉との照合（--use-kabu-pos）
# ────────────────────────────────────────────────────────────
def reconcile_with_kabu(csv_positions: list[dict], cli: KabuClient) -> list[dict]:
    """kabu の実建玉を取得して CSV ポジションと照合する。

    照合ルール:
      - CSV にあって kabu にもある  → kabu の qty を採用（CSV の stop_price を維持）
      - CSV にあって kabu にない    → 警告してスキップ（kabu が正なので）
      - kabu にあって CSV にない    → シグナル逆引きで stop_price を取得して追加。
                                       逆引き失敗時は ATR 推定。両方失敗なら損切り判定スキップ。
    csv_positions が空でも kabu 建玉のみで動作する。
    """
    try:
        kabu_pos_raw = cli.get_positions(product=0)
    except Exception as e:
        print(f"  ⚠ kabu 建玉取得失敗 ({e}) — CSV ポジションをそのまま使います")
        return csv_positions

    # kabu 建玉を symbol でインデックス化
    # Side: "1"=売建(ショート) "2"=買建(ロング)
    kabu_map: dict[str, list[dict]] = {}
    for kp in kabu_pos_raw:
        sym = str(kp.get("Symbol", "")).strip()
        if sym:
            kabu_map.setdefault(sym, []).append(kp)

    print(f"  kabu 実建玉: {len(kabu_pos_raw)} 件, CSV ポジション: {len(csv_positions)} 件")

    reconciled: list[dict] = []
    csv_symbols = set()

    for pos in csv_positions:
        sym = pos["symbol"]
        csv_symbols.add(sym)
        if sym not in kabu_map:
            print(f"  ⚠ {sym} {pos['name']}: CSV にあるが kabu に建玉なし → スキップ")
            continue
        # kabu の建玉から同サイドのものを探す
        target_side_str = "1" if pos["is_short"] else "2"
        matching = [kp for kp in kabu_map[sym]
                    if str(kp.get("Side", "")) == target_side_str]
        if not matching:
            side_label = "売建" if pos["is_short"] else "買建"
            print(f"  ⚠ {sym}: CSV は {side_label} だが kabu に該当建玉なし → スキップ")
            continue
        # 残数量を合算
        total_leaves = sum(int(kp.get("LeavesQty") or 0) for kp in matching)
        if total_leaves == 0:
            print(f"  ⚠ {sym}: kabu 建玉の残数量=0 → スキップ")
            continue
        merged = dict(pos)
        merged["qty"] = total_leaves
        merged["source"] = "kabu+csv"
        reconciled.append(merged)

    # kabu にあって CSV にない建玉
    for sym, kps in kabu_map.items():
        if sym in csv_symbols:
            continue
        for kp in kps:
            side_str = str(kp.get("Side", ""))
            is_short = (side_str == "1")
            leaves = int(kp.get("LeavesQty") or 0)
            if leaves == 0:
                continue
            side_label = "売建(ショート)" if is_short else "買建(ロング)"
            name = (kp.get("SymbolName") or "")[:12]
            # kabu 建玉の取得価格 (信用: Price / 現物: Price)
            fill_p = float(kp.get("AveragePrice") or kp.get("Price") or 0)
            margin_type = int(kp.get("MarginTradeType") or 0)
            cm = CASH_MARGIN_CLOSE if margin_type >= 1 else CASH_GENBUTSU

            # シグナル逆引きで損切り価格を取得
            stop_p = tgt_p = strat = None
            if fill_p > 0:
                print(f"  🔍 {sym} {name}: シグナル逆引き中 (fill_price={fill_p:.0f})...")
                stop_p, tgt_p, strat = lookup_stop_from_signal(sym, fill_p, is_short)

            if stop_p is not None:
                print(f"  ✓ {sym} {name}: {side_label} {leaves}株 "
                      f"[{strat}] stop={stop_p:.0f} target={tgt_p:.0f} (シグナル逆引き)")
            else:
                # フォールバック: ATR 推定
                if fill_p > 0:
                    stop_p = calc_atr_stop(sym, fill_p, is_short)
                if stop_p is not None:
                    print(f"  ⚠ {sym} {name}: {side_label} {leaves}株 "
                          f"stop={stop_p:.0f} (ATR推定) ← シグナル未一致")
                else:
                    print(f"  ✗ {sym} {name}: {side_label} {leaves}株 "
                          f"損切り価格取得失敗 → 損切り判定スキップ")

            reconciled.append({
                "symbol": sym,
                "name": name,
                "strategy": strat or "?",
                "stop_price": stop_p,
                "target_price": tgt_p,
                "is_short": is_short,
                "qty": leaves,
                "fill_price": fill_p,
                "fill_date": "",
                "cash_margin": cm,
                "source": "kabu_only",
                "hold_id": str(kp.get("ExecutionID") or kp.get("HoldID") or "").strip(),
            })

    return reconciled


# ────────────────────────────────────────────────────────────
# 現在値の取得
# ────────────────────────────────────────────────────────────
def get_current_price_fallback(symbol: str) -> float | None:
    """kabu が使えない (dry-run / post-close) ときの最新終値フォールバック。"""
    try:
        from backtest_limit_entry import fetch
        df = fetch(f"{symbol}.T")
        if df is None or df.empty:
            return None
        return float(df.iloc[-1]["close"])
    except Exception as e:
        print(f"  ⚠ {symbol}: 終値フォールバック失敗 ({e})")
        return None


def calc_atr_stop(symbol: str, fill_price: float, is_short: bool,
                  strategy: str = "") -> float | None:
    """stop_price 未設定時のフォールバック: ATR × sm から損切り価格を推定する。
    戦略が RSI2 なら sm=2.0、それ以外は sm=1.5 を使用。"""
    try:
        from backtest_limit_entry import fetch
        df = fetch(f"{symbol}.T", 30)
        if df is None or df.empty:
            return None
        atr = float(df.iloc[-1].get("atr", 0))
        if atr <= 0:
            return None
        sm = 2.0 if (strategy or "").upper() == "RSI2" else 1.5
        stop = fill_price + atr * sm if is_short else fill_price - atr * sm
        return round(stop, 1)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────
# 未約定の売り注文（利確逆指値など）をキャンセル
# ────────────────────────────────────────────────────────────
def cancel_open_sell_orders(symbol: str, cli: KabuClient) -> bool:
    """指定銘柄の未約定売り注文をすべてキャンセルする。
    ロング損切り時に利確逆指値が残っていると二重決済になるため呼ぶ。
    返り値: True=全件キャンセル成功（または対象なし）/ False=1件でも失敗
    """
    try:
        orders = cli.get_orders()
    except Exception as e:
        print(f"    ✗ 注文一覧取得失敗 ({e}) — 損切り発注を中断します")
        return False

    # 未約定 (OrderState 1〜4) の同銘柄売り注文を対象にする
    # Side: "1"=売 / "2"=買
    ACTIVE_STATES = {1, 2, 3, 4}
    targets = [
        o for o in orders
        if str(o.get("Symbol", "")).strip() == str(symbol).strip()
        and str(o.get("Side", "")) == "1"  # 売り注文
        and int(o.get("OrderState") or o.get("State") or 0) in ACTIVE_STATES
    ]
    if not targets:
        return True

    all_ok = True
    for o in targets:
        oid = o.get("ID", "")
        detail = f"注文ID={oid} 価格={o.get('Price', '?')} 数量={o.get('OrderQty', '?')}"
        print(f"    → 売り注文キャンセル: {detail}")
        res = cli.cancel_order(oid)
        if res.get("Result") == 0:
            print(f"      ✓ キャンセル完了")
        else:
            print(f"      ✗ キャンセル失敗: {res} — 損切り発注を中断します")
            all_ok = False
    return all_ok


def cancel_open_buy_orders(symbol: str, cli: KabuClient) -> bool:
    """指定銘柄の未約定買い注文をすべてキャンセルする。
    ショート損切り（買い戻し）時に、既存の買い戻し注文が残っている場合に呼ぶ。
    返り値: True=全件キャンセル成功（または対象なし）/ False=1件でも失敗
    """
    try:
        orders = cli.get_orders()
    except Exception as e:
        print(f"    ✗ 注文一覧取得失敗 ({e}) — 損切り発注を中断します")
        return False

    ACTIVE_STATES = {1, 2, 3, 4}
    targets = [
        o for o in orders
        if str(o.get("Symbol", "")).strip() == str(symbol).strip()
        and str(o.get("Side", "")) == "2"  # 買い注文
        and int(o.get("OrderState") or o.get("State") or 0) in ACTIVE_STATES
    ]
    if not targets:
        return True

    all_ok = True
    for o in targets:
        oid = o.get("ID", "")
        detail = f"注文ID={oid} 価格={o.get('Price', '?')} 数量={o.get('OrderQty', '?')}"
        print(f"    → 買い注文キャンセル: {detail}")
        res = cli.cancel_order(oid)
        if res.get("Result") == 0:
            print(f"      ✓ キャンセル完了")
        else:
            print(f"      ✗ キャンセル失敗: {res} — 損切り発注を中断します")
            all_ok = False
    return all_ok


# ────────────────────────────────────────────────────────────
# 建玉個別決済用の ClosePositions
# ────────────────────────────────────────────────────────────
def _close_list_for(pos: dict) -> list[dict] | None:
    """信用返済を「この建玉だけ」に限定する ClosePositions を返す。

    pos["hold_id"] (positions API の ExecutionID) があり、かつ信用返済
    (cash_margin=3) のときだけ [{"HoldID": ..., "Qty": ...}] を返す。
    - 同一銘柄を複数建玉で保有していても、指定した建玉だけを決済できる。
    - hold_id が無い / 現物 のときは None を返し、kabu_api 側の
      FIFO 自動割当て(従来動作)に委ねる。
    """
    if pos.get("cash_margin") != CASH_MARGIN_CLOSE:
        return None
    hid = str(pos.get("hold_id") or "").strip()
    if not hid:
        return None
    return [{"HoldID": hid, "Qty": int(pos.get("qty", 100))}]


def _resolve_actual_holding(pos: dict, cli: KabuClient) -> dict | None:
    """実際のkabu口座の保有を確認し、決済に使う cash_margin/hold_id/qty を
    実態に合わせて返す。CSVと口座がズレていても『実際に持っている建玉』だけを
    決済するための安全機構(2026-07 8086/DIC の決済スキップ対策)。

    返り値: {"cash_margin":.., "hold_id":.., "qty":..}
            None = 口座に該当保有なし(=既に決済済み等、売る対象がない)
    dry_run / API照合失敗時は CSV の指定をそのまま返す(従来動作)。
    """
    sym = str(pos["symbol"]).upper().removesuffix(".T")
    is_short = pos["is_short"]
    csv_cm = pos.get("cash_margin", CASH_GENBUTSU)
    fallback = {"cash_margin": csv_cm,
                "hold_id": str(pos.get("hold_id") or "").strip(),
                "qty": int(pos.get("qty", 100))}
    if cli.dry_run:
        return fallback
    try:
        genbutsu = cli.get_positions(product=1)   # 現物
        shinyo   = cli.get_positions(product=2)   # 信用
    except Exception as e:
        print(f"    ⚠ {sym}: 建玉照合に失敗 ({e}) → CSV指定のまま発注")
        return fallback

    def _m(p): return str(p.get("Symbol", "")).upper().removesuffix(".T") == sym
    want_side = "1" if is_short else "2"   # 信用 売建=1 / 買建=2

    def _leaves(p): return int(p.get("LeavesQty") or p.get("Qty") or 0)
    gen = [p for p in genbutsu if _m(p) and _leaves(p) > 0]
    mar = [p for p in shinyo if _m(p) and str(p.get("Side", "")) == want_side and _leaves(p) > 0]
    gen_qty = sum(_leaves(p) for p in gen)
    mar_qty = sum(_leaves(p) for p in mar)

    want_qty = int(pos.get("qty", 100))
    # 意図した建て方(CSV)が実口座にあればそれを、無ければ実在するもう一方を使う。
    if csv_cm == CASH_MARGIN_CLOSE and mar_qty > 0:
        hid = str(mar[0].get("ExecutionID") or mar[0].get("HoldID") or "").strip()
        return {"cash_margin": CASH_MARGIN_CLOSE, "hold_id": hid, "qty": min(want_qty, mar_qty)}
    if not is_short and gen_qty > 0:
        print(f"    ℹ {sym}: 信用建玉なし・現物保有あり → 現物売りで決済 (CSVは信用指定だった)")
        return {"cash_margin": CASH_GENBUTSU, "hold_id": "", "qty": min(want_qty, gen_qty)}
    if mar_qty > 0:
        hid = str(mar[0].get("ExecutionID") or mar[0].get("HoldID") or "").strip()
        print(f"    ℹ {sym}: 現物なし・信用建玉あり → 信用返済で決済")
        return {"cash_margin": CASH_MARGIN_CLOSE, "hold_id": hid, "qty": min(want_qty, mar_qty)}
    return None   # 口座に該当保有なし


# ────────────────────────────────────────────────────────────
# 引け成行 (MOC) または翌日寄成 (MOO) 発注
# ────────────────────────────────────────────────────────────
def send_moc_order(pos: dict, cli: KabuClient) -> bool:
    """保有ポジションを決済する MOC (pre-close) 注文を発注する。

    ロング → 売り決済 (side="sell")
    ショート → 買い戻し (side="buy")
    cash_margin: 1=現物 / 3=信用返済
    """
    # 損切り前に残っている反対側の注文をキャンセルする
    # キャンセル失敗時は空売りになるため発注しない
    # 【建玉個別管理の注意】kabu /orders は返済注文の対象建玉(HoldID)を返さないため、
    # 「この建玉の利確だけ」を狙ってキャンセルできない。同一銘柄の利確を一括取消して
    # 建玉の拘束(4001005)を解いてから決済する。→ 継続保有する別建玉の利確も一旦消えるが、
    # close_stop_guard は毎回 利確価格も評価して決済する(取りこぼしなし)ので実害はなく、
    # 次回 --with-targets 実行時に生存建玉へ利確が再発注される。
    # 実口座の保有を照合し、現物/信用と建玉IDを実態に合わせる(CSVズレ対策)
    actual = _resolve_actual_holding(pos, cli)
    if actual is None:
        print(f"    ℹ {pos['symbol']}: 口座に該当保有なし(既に決済済み?) → 発注不要")
        return True

    if pos["is_short"]:
        ok = cancel_open_buy_orders(pos["symbol"], cli)
    else:
        ok = cancel_open_sell_orders(pos["symbol"], cli)
    if not ok:
        print(f"    ✗ {pos['symbol']}: 既存注文のキャンセルに失敗したため損切り発注をスキップします。手動で対応してください。")
        return False

    side = "buy" if pos["is_short"] else "sell"
    cm = actual["cash_margin"]
    qty = actual["qty"]
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"
    side_label = "買い戻し" if pos["is_short"] else "売り決済"
    cp = ([{"HoldID": actual["hold_id"], "Qty": qty}]
          if cm == CASH_MARGIN_CLOSE and actual["hold_id"] else None)
    hid_lbl = f" 建玉={cp[0]['HoldID'][:8]}" if cp else ""
    print(f"    → {label} 引け成行({side_label}) side={side} cash_margin={cm} qty={qty}{hid_lbl}")
    res = cli.send_moc(pos["symbol"], qty=qty, side=side, cash_margin=cm,
                       close_positions=cp)
    return res.get("Result") == 0


def send_moo_order(pos: dict, cli: KabuClient) -> bool:
    """保有ポジションを成行で決済する。post-close モードで使用。

    信用返済ロング/ショートともに成行 (FrontOrderType=10) を使用。
    MOO (FrontOrderType=13) は 信用返済で 4001005 になるため使わない。
    """
    from kabu_api import CASH_GENBUTSU, CASH_MARGIN_CLOSE
    # 実口座の保有を照合し、現物/信用と建玉IDを実態に合わせる(CSVズレ対策)
    actual = _resolve_actual_holding(pos, cli)
    if actual is None:
        print(f"    ℹ {pos['symbol']}: 口座に該当保有なし(既に決済済み?) → 発注不要")
        return True

    if pos["is_short"]:
        ok = cancel_open_buy_orders(pos["symbol"], cli)
    else:
        ok = cancel_open_sell_orders(pos["symbol"], cli)
    if not ok:
        print(f"    ✗ {pos['symbol']}: 既存注文のキャンセルに失敗したため損切り発注をスキップします。手動で対応してください。")
        return False

    cm = actual["cash_margin"]
    qty = actual["qty"]
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"
    cp = ([{"HoldID": actual["hold_id"], "Qty": qty}]
          if cm == CASH_MARGIN_CLOSE and actual["hold_id"] else None)
    hid_lbl = f" 建玉={cp[0]['HoldID'][:8]}" if cp else ""

    if pos["is_short"]:  # ショート → 成行買い戻し
        print(f"    → {label} 成行(買い戻し) cash_margin={cm} qty={qty}{hid_lbl}")
        res = cli.send_buy(pos["symbol"], qty=qty,
                           cash_margin=cm, order_type="market",
                           close_positions=cp)
    else:  # ロング → 成行売り決済
        print(f"    → {label} 成行(売り決済) cash_margin={cm} qty={qty}{hid_lbl}")
        res = cli.send_sell(pos["symbol"], qty=qty,
                            cash_margin=cm, order_type="market",
                            close_positions=cp)
    return res.get("Result") == 0


# ────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="close 方式の損切りを引け成行で自動化する引け前ガード")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run で判定のみ)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続する (未指定ならデモ18081)")
    ap.add_argument("--log", default=None,
                    help="保有ポジションを読む CSV (既定: forward_test_log.csv)")
    ap.add_argument("--aggressive", action="store_true",
                    help="aggressive ログ (forward_test_log_aggressive.csv) を対象にする")
    ap.add_argument("--post-close", action="store_true",
                    help="引け後モード: yfinance 終値で判定し翌日寄成(MOO)で発注する")
    ap.add_argument("--use-kabu-pos", action="store_true",
                    help="kabu の実建玉を取得して CSV ポジションと照合する")
    ap.add_argument("--kabu", action="store_true",
                    help="kabu 実建玉 + signals JSON を使用 (CSV 不要・推奨)")
    ap.add_argument("--product", type=int, default=2,
                    help="--kabu 時の取得建玉種別: 0=全 1=現物 2=信用 (default:2)")
    ap.add_argument("--schedule", action="store_true",
                    help="各保有のタイムカット売却予定日を一覧表示して終了 (価格取得・発注なし)")
    ap.add_argument("--targets", action="store_true",
                    help="各保有に利確指値を発注 (ロング=指値売り/ショート=指値買戻し)。"
                         "リレー未対応のためエントリー約定後に日次で置く運用")
    ap.add_argument("--with-targets", action="store_true",
                    help="損切り/タイムカット判定の際に、決済しない保有の利確指値が"
                         "未設定なら自動で発注する")
    ap.add_argument("--holdings-html", action="store_true",
                    help="実際に約定した保有銘柄の詳細(タイムカット日・損切り/利確)を"
                         "HTMLに出力して終了 (発注なし)")
    args = ap.parse_args()

    log_path = args.log or _default_log_path(args.aggressive)
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"

    now = datetime.now(JST)

    # 15:30 以降は市場外のため post-close フラグを自動設定
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30
    if not args.post_close:
        after_close = (now.hour, now.minute) >= (MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN)
        if after_close:
            print(f"  ⚠ {now:%H:%M} JST: 市場終了後のため post-close モードに自動切替")
            args.post_close = True

    timing_label = "post-close (yfinance終値)" if args.post_close else "pre-close (現在値)"
    src_label    = "kabu建玉+signals JSON" if args.kabu else \
                   ("kabu建玉+CSV" if args.use_kabu_pos else f"CSV: {log_path}")
    print("=" * 65)
    print(f"close 損切りガード  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード    : {mode_label}")
    print(f"タイミング: {timing_label}")
    print(f"接続先    : {env_label}  /  ソース: {src_label}")
    print("=" * 65)

    # kabu クライアント
    cli: KabuClient | None = None
    need_kabu = args.execute or args.use_kabu_pos or args.kabu
    if need_kabu:
        cli = KabuClient(prod=args.prod, dry_run=not args.execute)
        try:
            cli.connect()
            print(f"kabu 接続成功 ({cli.env_label})\n")
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            return 1

    # ── ポジション取得 ────────────────────────────────────────────────────
    if args.kabu:
        # kabu建玉 + signals JSON モード（CSV不要）
        if cli is None:
            cli = KabuClient(prod=args.prod, dry_run=True)
            cli.connect()
        positions = load_positions_from_kabu(cli, product=args.product)
        if not positions:
            print("kabu 建玉なし。終了します。")
            return 0
        print(f"\nkabu 建玉: {len(positions)} 件\n")
    else:
        # 従来モード: CSV ベース
        positions = load_open_positions(log_path)
        if not positions and not args.use_kabu_pos:
            print("保有中ポジションなし。終了します。")
            return 0
        if positions:
            print(f"CSV 保有中ポジション: {len(positions)} 件\n")
        else:
            print("CSV にポジションなし — kabu 実建玉から取得します\n")

        # kabu 建玉との照合
        if args.use_kabu_pos and cli is not None:
            positions = reconcile_with_kabu(positions, cli)
            if not positions:
                print("kabu 実建玉なし。終了します。")
                return 0
            print(f"照合後ポジション: {len(positions)} 件\n")

    # ── --schedule: タイムカット売却予定日の一覧 (価格取得・発注なし) ──
    if args.schedule:
        from backtest_limit_entry import default_max_hold as _dmh
        today_b = pd.Timestamp(now.date())
        rows = []
        for pos in positions:
            strat = pos.get("strategy", "")
            side_label = "ショート" if pos["is_short"] else "ロング"
            fd = pos.get("fill_date", "").strip()
            mh = _dmh(strat)
            if not fd:
                rows.append((pd.Timestamp.max, pos, "—", "—", mh, "約定日不明"))
                continue
            fb = pd.Timestamp(fd)
            hold_days = len(pd.bdate_range(fb, today_b)) - 1
            tc_date = (fb + pd.tseries.offsets.BDay(mh)).normalize()
            remaining = mh - hold_days
            if remaining <= 0:
                note = "★本日以前に売却対象"
            elif remaining == 1:
                note = "翌営業日に売却"
            else:
                note = f"あと{remaining}営業日"
            rows.append((tc_date, pos, fd, hold_days, mh, note))
        rows.sort(key=lambda r: r[0])
        print(f"タイムカット売却予定 (基準日 {now:%Y-%m-%d})")
        print(f"※ 「売却日」の引け成行(MOC)で自動決済します\n")
        print(f"  {'売却日(期限)':<14} {'銘柄':<8} {'名前':<16} {'戦略':<6} {'区分':<5} "
              f"{'約定日':<11} 状況")
        print("  " + "-" * 82)
        for tc_date, pos, fd, hold_days, mh, note in rows:
            tcs = "約定日不明" if fd == "—" else f"{tc_date:%Y-%m-%d(%a)}"
            side_label = "ショート" if pos["is_short"] else "ロング"
            nm = (pos.get("name", "") or "")[:14]
            print(f"  {tcs:<14} {pos['symbol']:<8} {nm:<16} {pos.get('strategy',''):<6} "
                  f"{side_label:<5} {fd:<11} {note}")
        print()
        return 0

    # ── --holdings-html: 実際に約定した保有銘柄の詳細HTMLを出力 ──
    if args.holdings_html:
        def _price(sym):
            try:
                return get_current_price_fallback(sym)
            except Exception:
                return None
        html_str = _build_holdings_html(positions, now, price_fn=_price)
        out = Path(__file__).resolve().parent / "holdings_latest.html"
        out.write_text(html_str, encoding="utf-8")
        print(f"保有銘柄HTML: {out}  ({len(positions)}件)")
        try:
            from _open_html import open_html
            open_html(out.resolve().as_uri())
        except Exception:
            pass
        return 0

    # ── --targets: 各保有に利確指値を発注 (リレー未対応のため約定後に置く) ──
    if args.targets:
        # dry-run プレビュー用に未接続なら dry-run クライアントを用意 (接続なし)
        if cli is None:
            cli = KabuClient(prod=args.prod, dry_run=True)
        # 既存の未約定注文（重複発注防止）
        open_orders = []
        if args.execute:
            try:
                open_orders = cli.get_orders()
            except Exception as e:
                print(f"  ⚠ 注文一覧取得失敗 ({e}) — 重複チェックなしで続行")

        print(f"利確指値の発注 ({'★実発注★' if args.execute else 'dry-run'} / {env_label})")
        print(f"※ ロング=指値売り / ショート=指値買戻し。期限=タイムカット日まで")
        print(f"※ 同一銘柄でも建玉ごとに別々の利確を発注(HoldID個別)\n")
        placed = skipped = 0
        placed_holds: set = set()   # このrunで利確済みの建玉ID(建玉単位の重複防止)
        for pos in positions:
            status, msg = _place_target_order(cli, pos, open_orders, placed_holds)
            print(msg)
            if status == "placed":
                placed += 1
            else:
                skipped += 1
        print(f"\n利確発注: {placed}件 / スキップ: {skipped}件")
        if not args.execute:
            print("※ dry-run のため実発注していません。--execute で発注します。")
        return 0

    # 価格取得の方針
    # post-close: yfinance 終値を使う (kabu 不要)
    # pre-close:  kabu 接続済みなら /board、未接続なら yfinance フォールバック
    def _get_price(symbol: str) -> float | None:
        if args.post_close:
            return get_current_price_fallback(symbol)
        if cli is not None and args.execute:
            # ★重要: 損切り判定のリアルタイム価格。429等で取れないと古い前日終値
            # (yfinanceフォールバック)で「セーフ」と誤判定し損切りが効かなくなる。
            # kabu現在値をバックオフ再試行で確実に取りにいく。
            import time as _t
            for _i in range(5):
                try:
                    price = cli.get_current_price(symbol)
                except Exception:
                    price = None
                if price is not None and price > 0:
                    return price
                _t.sleep(1.5 * (_i + 1))   # 1.5/3.0/4.5/6.0/7.5s
            # kabu が全滅 → yfinanceフォールバック(前日終値=遅延の可能性)。警告。
            fb = get_current_price_fallback(symbol)
            print(f"  ⚠ {symbol}: kabu現在値が5回取得失敗 → yfinance終値{('%.0f'%fb) if fb else 'なし'}"
                  f"(遅延/前日値の可能性)で判定。損切り取りこぼしに注意")
            return fb
        return get_current_price_fallback(symbol)

    # MAX_HOLD は全戦略7日に統一 (default_max_hold)
    from backtest_limit_entry import default_max_hold

    today_bdate = pd.Timestamp(now.date())

    breached: list[dict] = []
    for pos in positions:
        sp = pos.get("stop_price")
        side_label = "ショート" if pos["is_short"] else "ロング"
        src_label = f"[{pos['source']}]" if pos.get("source") != "csv" else ""
        strat = pos.get("strategy", "")

        # ── MAX_HOLD タイムカット判定 ──
        timecut = False
        fill_date_str = pos.get("fill_date", "").strip()
        if fill_date_str:
            try:
                fill_bdate = pd.Timestamp(fill_date_str)
                hold_days = len(pd.bdate_range(fill_bdate, today_bdate)) - 1
                max_hold = default_max_hold(strat)
                if hold_days >= max_hold:
                    timecut = True
                    print(f"  ⏰タイムカット {pos['symbol']} {pos['name']} "
                          f"[{strat}/{side_label}]{src_label} "
                          f"保有{hold_days}日 ≥ MAX_HOLD={max_hold}日")
                    pos = dict(pos, exit_reason="タイムカット")
                    breached.append(pos)
                    continue
            except Exception:
                pass

        price = _get_price(pos["symbol"])
        if price is None:
            print(f"  ⚠⚠ {pos['symbol']} {pos['name']}: 現在値取得不可(429等) "
                  f"→ 損切り判定できず【無防備】。手動で価格・損切りを確認してください")
            continue

        # ── 利確判定 (target_price) ──
        tp = pos.get("target_price")
        if tp and tp > 0:
            profit_hit = (price <= tp) if pos["is_short"] else (price >= tp)
            if profit_hit:
                price_label = "終値" if args.post_close else "現在値"
                print(f"  💰利確  {pos['symbol']} {pos['name']} "
                      f"[{strat}/{side_label}]{src_label} "
                      f"{price_label}={price:.1f} 目標={tp:.1f}")
                breached.append(dict(pos, exit_reason="利確"))
                continue

        if sp is None:
            print(f"  ? {pos['symbol']} {pos['name']}: 損切り価格不明 → 保有継続")
            price_label = "終値" if args.post_close else "現在値"
            print(f"     {price_label}={price:.1f}")
            continue

        hit = (price >= sp) if pos["is_short"] else (price <= sp)
        mark = "🔴損切り" if hit else "  保有継続"
        price_label = "終値" if args.post_close else "現在値"
        tp_str = f" 目標={tp:.1f}" if tp else ""
        print(f"  {mark}  {pos['symbol']} {pos['name']} "
              f"[{strat}/{side_label}]{src_label} "
              f"{price_label}={price:.1f} 損切り={sp:.1f}{tp_str}")
        if hit:
            pos = dict(pos, exit_reason="損切り")
            breached.append(pos)

    print()

    # ── --with-targets: 決済しない保有に利確指値が無ければ発注 ──
    if args.with_targets:
        if cli is None:
            cli = KabuClient(prod=args.prod, dry_run=True)
        # 決済対象の建玉を除外して「継続保有」だけに利確を出す。
        # 建玉個別管理: HoldID がある建玉は HoldID 単位で除外し、同一銘柄でも
        # 決済しない建玉には利確を出す。HoldID が無い(現物/合算)場合のみ
        # 従来どおり (銘柄, 売買方向) 単位で除外する。
        _breached_holds = {str(p.get("hold_id") or "").strip()
                           for p in breached if str(p.get("hold_id") or "").strip()}
        _breached_keys = {(str(p["symbol"]).split(".")[0], p["is_short"])
                          for p in breached if not str(p.get("hold_id") or "").strip()}

        def _is_breached(p: dict) -> bool:
            hid = str(p.get("hold_id") or "").strip()
            if hid:
                return hid in _breached_holds
            return (str(p["symbol"]).split(".")[0], p["is_short"]) in _breached_keys

        _held = [p for p in positions if not _is_breached(p)]
        _open_orders = []
        if args.execute:
            try:
                _open_orders = cli.get_orders()
            except Exception as e:
                print(f"  ⚠ 注文一覧取得失敗 ({e}) — 利確の重複チェックなしで続行")
        print(f"[利確指値チェック] 決済しない保有 {len(_held)}件 "
              f"({'★実発注★' if args.execute else 'dry-run'})")
        _tp_placed = 0
        _placed_holds: set = set()   # このrunで利確済みの建玉ID(建玉単位の重複防止)
        for _pos in _held:
            _status, _msg = _place_target_order(cli, _pos, _open_orders, _placed_holds)
            print(_msg)
            if _status == "placed":
                _tp_placed += 1
        print(f"利確指値: {_tp_placed}件 新規発注\n")

    if not breached:
        print("損切り・タイムカット抵触なし。発注なし。")
        return 0

    stop_count   = sum(1 for p in breached if p.get("exit_reason") == "損切り")
    tc_count     = sum(1 for p in breached if p.get("exit_reason") == "タイムカット")
    profit_count = sum(1 for p in breached if p.get("exit_reason") == "利確")
    print(f"損切り: {stop_count}件 / 利確: {profit_count}件 / タイムカット: {tc_count}件 (合計: {len(breached)}件)")
    if not args.execute:
        print(f"dry-run のため発注しません (成行)。実発注するには --execute を付けてください。")
        return 0

    order_label = "成行"
    print(f"{order_label} を {env_label} に発注します...")
    ok = 0
    for pos in breached:
        # 損切り・利確・タイムカットとも通常成行(FrontOrderType=10)で即時決済する。
        # ガードは引け前(14:50頃)に走るため通常成行でもほぼ引け値で約定し、
        # 引け成行(MOC)特有の失効/エラーを避けられる。
        if send_moo_order(pos, cli):
            ok += 1
    print(f"\n発注完了: {ok}/{len(breached)} 件成功 ({order_label})")
    return 0


if __name__ == "__main__":
    import io as _io

    # タスクスケジューラ経由の実行ではコンソールが見えないため、
    # 標準出力をログファイルにも同時書き出しする
    _log_dir  = Path(__file__).parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _log_file = _log_dir / f"close_stop_guard_{datetime.now(JST).strftime('%Y-%m-%d')}.log"

    class _Tee:
        def __init__(self, *streams):
            self._s = streams
        def write(self, data):
            for s in self._s:
                try: s.write(data)
                except Exception: pass
        def flush(self):
            for s in self._s:
                try: s.flush()
                except Exception: pass

    _log_fh = open(_log_file, "a", encoding="utf-8")
    _log_fh.write(f"\n{'='*60}\n{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}\n{'='*60}\n")
    sys.stdout = _Tee(sys.__stdout__, _log_fh)
    sys.stderr = _Tee(sys.__stderr__, _log_fh)

    try:
        _rc = main()
    finally:
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        _log_fh.close()

    sys.exit(_rc)
