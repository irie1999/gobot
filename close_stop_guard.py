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
def _load_signals_json(json_path: str | None = None) -> dict[str, list[dict]]:
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

    if result:
        print(f"  [シグナル] {', '.join(loaded_files)} を統合 ({len(seen)}件)")
    else:
        print("  [WARN] シグナルJSONが見つかりません")
    return result


def _lookup_signal(sym: str, sig_map: dict[str, list[dict]],
                   fill_price: float, is_short: bool
                   ) -> tuple[float | None, float | None, str]:
    """sig_map からシンボルに対応するstop_p/target_pを返す。
    複数戦略がある場合は fill_price に最も近い order_p を優先。"""
    candidates = sig_map.get(sym, [])
    if not candidates:
        return None, None, ""
    best = None
    for s in candidates:
        op = float(s.get("order_p") or 0)
        sp = float(s.get("stop_p")  or 0)
        tp = float(s.get("target_p") or 0)
        if sp <= 0:
            continue
        diff = abs(op - fill_price) / max(op, 1)
        if best is None or diff < best[3]:
            best = (sp, tp, s.get("strategy", ""), diff)
    if best:
        return best[0], best[1], best[2]
    return None, None, ""


# ────────────────────────────────────────────────────────────
# kabu建玉 + signals JSON からポジション一覧を構築（--kabu モード）
# ────────────────────────────────────────────────────────────
def load_positions_from_kabu(cli: KabuClient, product: int = 2) -> list[dict]:
    """kabu の実建玉を取得し、signals JSON で stop/target を補完して返す。"""
    try:
        raw = cli.get_positions(product=product)
    except Exception as e:
        print(f"  ✗ kabu 建玉取得失敗: {e}")
        return []

    sig_map = _load_signals_json()
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

        stop_p, tgt_p, strat = _lookup_signal(sym, sig_map, fill_p, is_short)

        if stop_p is not None:
            src = "signals_json"
            print(f"  ✓ {sym} {name}: stop={stop_p:.0f} target={tgt_p:.0f} [{strat}]")
        else:
            # フォールバック: 既存のシグナル逆引き
            if fill_p > 0:
                print(f"  🔍 {sym} {name}: signals JSONに未登録 → シグナル逆引き中...")
                stop_p, tgt_p, strat = lookup_stop_from_signal(sym, fill_p, is_short)
            if stop_p is None and fill_p > 0:
                stop_p = calc_atr_stop(sym, fill_p, is_short, strat or "")
                src = "atr_estimate"
                if stop_p:
                    print(f"  ⚠ {sym} {name}: ATR推定 stop={stop_p:.0f}")
                else:
                    print(f"  ✗ {sym} {name}: 損切り価格取得失敗 → 判定スキップ")
            else:
                src = "signal_lookup" if stop_p else "missing"

        positions.append({
            "symbol":       sym,
            "name":         name,
            "strategy":     strat or "?",
            "stop_price":   stop_p,
            "target_price": tgt_p,
            "is_short":     is_short,
            "qty":          leaves,
            "fill_price":   fill_p,
            "fill_date":    "",
            "cash_margin":  cm,
            "source":       src,
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
    if pos["is_short"]:
        ok = cancel_open_buy_orders(pos["symbol"], cli)
    else:
        ok = cancel_open_sell_orders(pos["symbol"], cli)
    if not ok:
        print(f"    ✗ {pos['symbol']}: 既存注文のキャンセルに失敗したため損切り発注をスキップします。手動で対応してください。")
        return False

    side = "buy" if pos["is_short"] else "sell"
    cm = pos.get("cash_margin", CASH_GENBUTSU)
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"
    side_label = "買い戻し" if pos["is_short"] else "売り決済"
    print(f"    → {label} 引け成行({side_label}) side={side} cash_margin={cm}")
    res = cli.send_moc(pos["symbol"], qty=pos["qty"], side=side, cash_margin=cm)
    return res.get("Result") == 0


def send_moo_order(pos: dict, cli: KabuClient) -> bool:
    """保有ポジションを成行で決済する。post-close モードで使用。

    信用返済ロング/ショートともに成行 (FrontOrderType=10) を使用。
    MOO (FrontOrderType=13) は 信用返済で 4001005 になるため使わない。
    """
    from kabu_api import CASH_GENBUTSU, CASH_MARGIN_CLOSE
    if pos["is_short"]:
        ok = cancel_open_buy_orders(pos["symbol"], cli)
    else:
        ok = cancel_open_sell_orders(pos["symbol"], cli)
    if not ok:
        print(f"    ✗ {pos['symbol']}: 既存注文のキャンセルに失敗したため損切り発注をスキップします。手動で対応してください。")
        return False

    cm = pos.get("cash_margin", CASH_GENBUTSU)
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"

    if pos["is_short"]:  # ショート → 成行買い戻し
        print(f"    → {label} 成行(買い戻し) cash_margin={cm}")
        res = cli.send_buy(pos["symbol"], qty=pos["qty"],
                           cash_margin=cm, order_type="market")
    else:  # ロング → 成行売り決済
        print(f"    → {label} 成行(売り決済) cash_margin={cm}")
        res = cli.send_sell(pos["symbol"], qty=pos["qty"],
                            cash_margin=cm, order_type="market")
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

    # 価格取得の方針
    # post-close: yfinance 終値を使う (kabu 不要)
    # pre-close:  kabu 接続済みなら /board、未接続なら yfinance フォールバック
    def _get_price(symbol: str) -> float | None:
        if args.post_close:
            return get_current_price_fallback(symbol)
        if cli is not None and args.execute:
            price = cli.get_current_price(symbol)
            if price is None:
                price = get_current_price_fallback(symbol)
            return price
        return get_current_price_fallback(symbol)

    # MAX_HOLD は戦略別 (RSI2=7, MOM=20, other=15)
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
            print(f"  ? {pos['symbol']} {pos['name']}: 現在値取得不可 → スキップ")
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

    order_label = "成行(翌朝)" if args.post_close else "引け成行(MOC)"
    print(f"{order_label} を {env_label} に発注します...")
    print(f"  ※ タイムカットは分析結果(引け有利)に基づき、実行時刻に関わらず常に引け成行(MOC)で決済します")
    ok = 0
    for pos in breached:
        # タイムカットは「引けで売る」方が有利(寄付vs引け比較で確認済み)。
        # post-closeで実行されても翌引けMOCを使い、翌朝の寄り(MOO)は避ける。
        is_timecut = pos.get("exit_reason") == "タイムカット"
        use_moc = is_timecut or (not args.post_close)
        if use_moc:
            if send_moc_order(pos, cli):
                ok += 1
        else:
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
