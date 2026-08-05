"""
show_positions.py — kabu station 保有建玉 + 損切り/利確 表示
=============================================================

kabu station API から実建玉を取得し、signals_latest.json（または
signals_{date}.json）の stop_p / target_p と照合して表示します。

使い方:
  python show_positions.py                    # デモ口座 (18081)
  python show_positions.py --prod             # 本番口座 (18080)
  python show_positions.py --json signals_2026-06-25.json  # 日付指定
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from kabu_api import KabuClient

JST = timezone(timedelta(hours=9))


def _load_signals(json_path: str | None) -> dict[str, list[dict]]:
    """全シグナルJSONを読み込み symbol → [signal, ...] の辞書を返す。
    最新日付のファイルほど優先（同じsymbol+strategyが重複した場合）。"""

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
        # 日付付きファイルを降順（新しい順）+ latest を先頭に
        # ロング版: signals_YYYY-MM-DD.json
        # ショート版: signals_YYYY-MM-DD_short.json
        dated_long  = sorted(
            glob.glob("signals_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"),
            reverse=True
        )
        dated_short = sorted(
            glob.glob("signals_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_short.json"),
            reverse=True
        )
        # 同じ日付のlong/shortをペアで並べる（新しい日付が先）
        dated_pairs: list[str] = []
        all_dates = sorted(
            {f.replace("_short", "") for f in dated_long + dated_short},
            reverse=True
        )
        for d in all_dates:
            base = d  # e.g. "signals_2026-06-25.json"
            short = d.replace(".json", "_short.json")
            if base in dated_long:
                dated_pairs.append(base)
            if short in dated_short:
                dated_pairs.append(short)
        candidates = ["signals_latest.json", "signals_latest_short.json"] + dated_pairs

    # 全ファイルをマージ（新しいファイルが優先）
    result: dict[str, list[dict]] = {}
    loaded_files = []
    seen: set[tuple] = set()  # (symbol, strategy) の重複排除

    for path in candidates:
        sigs = _try_load(path)
        if not sigs:
            continue
        loaded_files.append(f"{Path(path).name}({len(sigs)}件)")
        for s in sigs:
            # yfinance形式 "8544.T" → kabu形式 "8544" に正規化
            sym = str(s["symbol"]).upper().removesuffix(".T")
            strat = s.get("strategy", "")
            key = (sym, strat)
            if key not in seen:
                seen.add(key)
                result.setdefault(sym, []).append(s)

    if result:
        print(f"[シグナル] {', '.join(loaded_files)} を統合 (計{len(seen)}件)")
    else:
        print("[WARN] シグナルJSONが見つかりません。--json でファイルを指定してください。")
    return result


def _pct(price: float, ref: float) -> str:
    if ref and ref > 0:
        return f"{(price - ref) / ref * 100:+.1f}%"
    return "—"


def _bar(pct_val: float, width: int = 20) -> str:
    """簡易バー表示: -20%〜+20% の範囲を可視化"""
    clamped = max(-20.0, min(20.0, pct_val))
    center  = width // 2
    pos     = int(center + clamped / 20.0 * center)
    bar     = [" "] * width
    bar[center] = "|"
    if pos != center:
        step = 1 if pos > center else -1
        for i in range(center + step, pos + step, step):
            bar[i] = "█"
    bar[pos] = "◆"
    return "".join(bar)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod",    action="store_true", help="本番口座 (18080)")
    ap.add_argument("--json",    default=None,        help="使用するシグナルJSONパス")
    ap.add_argument("--product", type=int, default=2, help="0=全 1=現物 2=信用 (default:2)")
    args = ap.parse_args()

    # シグナルデータ読み込み
    sig_map = _load_signals(args.json)

    # kabu station 接続
    cli = KabuClient(prod=args.prod, dry_run=True)
    try:
        cli.connect()
    except Exception as e:
        print(f"[ERROR] kabu station 接続失敗: {e}")
        print("  → kabu station が起動しているか確認してください。")
        sys.exit(1)

    # 建玉取得
    try:
        positions = cli.get_positions(product=args.product)
    except Exception as e:
        print(f"[ERROR] 建玉取得失敗: {e}")
        sys.exit(1)

    if not positions:
        print("現在の保有建玉はありません。")
        return

    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    print()
    print("=" * 72)
    print(f"  kabu station 保有建玉  損切り・利確 一覧   ({now_str})")
    print("=" * 72)

    total_pnl = 0.0
    has_total  = False

    for pos in positions:
        sym       = str(pos.get("Symbol", "")).upper().removesuffix(".T")
        name      = pos.get("SymbolName") or pos.get("Name") or sym
        side_code = pos.get("Side", "")          # "1"=売 "2"=買
        is_long   = side_code == "2"
        side_str  = "ロング ▲" if is_long else "ショート ▼"
        qty       = pos.get("LeavesQty") or pos.get("Qty") or 0
        avg_price = float(pos.get("AveragePrice") or pos.get("Price") or 0)
        profit    = pos.get("Profit")            # kabu計算の評価損益

        # 現在値取得
        current: float | None = pos.get("CurrentPrice")
        if current is None:
            try:
                current = cli.get_current_price(sym)
            except Exception:
                current = None

        # シグナルデータを symbol でマッチ（スコア最高を優先）
        sig_list = sig_map.get(sym, [])
        sig = max(sig_list, key=lambda s: s.get("score", 0)) if sig_list else None

        stop_p   = float(sig["stop_p"])   if sig and sig.get("stop_p")   else None
        target_p = float(sig["target_p"]) if sig and sig.get("target_p") else None
        order_p  = float(sig["order_p"])  if sig and sig.get("order_p")  else avg_price
        strategy = sig["strategy"]        if sig else "—"
        sig_date = sig.get("signal_date", "") if sig else ""

        # 評価損益
        if profit is not None:
            pnl_val = float(profit)
        elif current and avg_price:
            diff    = (current - avg_price) if is_long else (avg_price - current)
            pnl_val = diff * qty
        else:
            pnl_val = None

        pnl_str = ""
        if pnl_val is not None:
            sign     = "+" if pnl_val >= 0 else ""
            pnl_str  = f"  {sign}{pnl_val:>9,.0f} 円"
            total_pnl += pnl_val
            has_total  = True

        print()
        print(f"  ┌─ {sym}  {name}  [{side_str}]  戦略: {strategy}  シグナル日: {sig_date}")
        print(f"  │  約定値   : {avg_price:>9,.1f} 円")
        if current is not None:
            pct_now = _pct(current, avg_price)
            print(f"  │  現在値   : {current:>9,.1f} 円   ({pct_now})")
        else:
            print(f"  │  現在値   : 取得失敗")

        if stop_p is not None:
            d = _pct(stop_p, avg_price)
            remaining_stop = ""
            if current is not None:
                rem = abs(current - stop_p)
                remaining_stop = f"  ←残り {rem:,.0f}円"
            print(f"  │  損切り   : {stop_p:>9,.1f} 円   (約定比 {d}){remaining_stop}")
        else:
            print(f"  │  損切り   : シグナルJSON に見つかりません")

        if target_p is not None:
            d = _pct(target_p, avg_price)
            remaining_tgt = ""
            if current is not None:
                rem = abs(target_p - current)
                remaining_tgt = f"  ←残り {rem:,.0f}円"
            print(f"  │  利確目標 : {target_p:>9,.1f} 円   (約定比 {d}){remaining_tgt}")
        else:
            print(f"  │  利確目標 : シグナルJSON に見つかりません")

        # 進捗バー: 損切り〜現在〜利確 の位置を表示
        if stop_p and target_p and current is not None:
            rng = target_p - stop_p
            if rng > 0:
                pct_pos = (current - stop_p) / rng * 100
                bar_w   = 40
                filled  = int(pct_pos / 100 * bar_w)
                filled  = max(0, min(bar_w, filled))
                bar     = "─" * filled + "◆" + "─" * (bar_w - filled)
                print(f"  │  進捗     : [損切{bar}利確]  {pct_pos:.0f}%")

        if pnl_str:
            marker = "▲" if pnl_val >= 0 else "▼"
            print(f"  └  評価損益 :{pnl_str}  {marker}  (×{qty}株)")
        else:
            print(f"  └  評価損益 : 計算不可  (×{qty}株)")

    print()
    print("=" * 72)
    if has_total:
        sign = "+" if total_pnl >= 0 else ""
        marker = "▲ 含み益" if total_pnl >= 0 else "▼ 含み損"
        print(f"  合計評価損益: {sign}{total_pnl:>10,.0f} 円   {marker}")
    print("=" * 72)
    print()
    print("  ※ 損切り・利確は signals_latest.json の stop_p / target_p を参照")
    print("  ※ 引け前損切りチェック: python close_stop_guard.py")
    print()


if __name__ == "__main__":
    main()
