"""
set_stop_orders.py — 保有銘柄の損切り逆指値を毎日自動設定
===========================================================

my_positions.csv の保有銘柄に対して kabuステーション に
損切り逆指値注文（FOT_STOP）を設定するスクリプト。

【運用フロー】
  ① 前日引け後 or 当日寄り前に実行
    python set_stop_orders.py              # dry-run（確認のみ）
    python set_stop_orders.py --execute    # デモ口座に発注
    python set_stop_orders.py --execute --prod  # 本番口座

  ② 翌日ザラ場中に逆指値が自動発動 → 損切り成行

【重複防止】
  - 既に同銘柄の有効な逆指値売り注文がある場合はスキップ
  - stop_price が 0.5% 以上変わった場合は取消→再発注
  - --refresh で全て強制取消→再設定

【データソース（優先順）】
  ① my_positions.csv の stop_price（手動 or 自動補完済み）
  ② signals_YYYY-MM-DD.json の stop_p（その日の推定値）

【安全設計】
  - デフォルト dry-run。--execute で実発注
  - --execute のみではデモ(18081)。本番は --prod 必須

使い方:
  python set_stop_orders.py                          # dry-run
  python set_stop_orders.py --execute                # デモ口座
  python set_stop_orders.py --execute --prod         # 本番口座
  python set_stop_orders.py --refresh --execute      # 全て再設定
  python set_stop_orders.py --log other.csv          # 別のCSV
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from kabu_api import KabuClient, CASH_GENBUTSU, CASH_MARGIN_CLOSE

TODAY = date.today()


# ── ポジション読み込み ─────────────────────────────────────────────────────────

def _signal_candidates() -> list[Path]:
    """直近3営業日の日付付きシグナルJSONを返す。"""
    files: list[Path] = [
        Path("signals_latest.json"),
        Path("signals_latest_short.json"),
    ]
    d = TODAY
    for _ in range(6):
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y-%m-%d")
        files.append(Path(f"signals_{ds}.json"))
        files.append(Path(f"signals_{ds}_short.json"))
        if len(files) >= 10:
            break
    return files


def _load_signal_map() -> dict[str, dict]:
    """シグナルJSONから symbol → signal の辞書を作る。"""
    sig_map: dict[str, dict] = {}
    for p in _signal_candidates():
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for s in data.get("signals", []):
                sym = str(s.get("symbol", "")).split(".")[0]
                if sym and sym not in sig_map:
                    sig_map[sym] = s
        except Exception:
            pass
    return sig_map


def load_positions(csv_path: str) -> list[dict]:
    """保有中ポジションを CSV から読む。stop_price が未設定の場合シグナルで補完。"""
    sig_map = _load_signal_map()
    positions = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "holding":
                continue

            sym = str(row.get("symbol", "")).strip()
            name = str(row.get("name", "")).strip()
            side = str(row.get("side", "long")).lower()

            def _fv(k):
                try:
                    v = float(row.get(k) or 0)
                    return 0.0 if v != v else v
                except Exception:
                    return 0.0

            stop_p = _fv("stop_price")
            qty    = int(_fv("qty") or 100)
            cm     = int(_fv("cash_margin") or 1)

            # stop_price 未設定 → シグナルから補完
            if stop_p <= 0 and sym in sig_map:
                s = sig_map[sym]
                try:
                    stop_p = float(s.get("stop_p") or 0)
                except Exception:
                    pass

            if stop_p <= 0:
                fill_p = _fv("fill_price")
                if fill_p > 0:
                    strat = str(row.get("strategy", "")).upper()
                    pct   = 0.060 if strat == "RSI2" else 0.045
                    stop_p = round(fill_p * (1 + pct) if side == "short"
                                   else fill_p * (1 - pct), 0)
                    print(f"  ⚠ {sym} {name}: stop_price 未設定 → ATR推定 {stop_p:,.0f}円 (-{pct*100:.1f}%)")
                else:
                    print(f"  ✗ {sym} {name}: stop_price も fill_price も未設定 → スキップ")
                    continue

            positions.append({
                "symbol":      sym,
                "name":        name,
                "stop_price":  stop_p,
                "qty":         qty,
                "side":        side,
                "cash_margin": cm,
            })

    return positions


# ── 既存注文チェック ──────────────────────────────────────────────────────────

def get_active_stop_orders(orders: list[dict]) -> dict[str, dict]:
    """有効な逆指値注文（売り/買い）を {symbol: {order_id, trigger_price, side}} で返す。

    OrderState: 1=待機, 2=処理中, 4=訂正取消中 → 有効
    FrontOrderType: 30=逆指値
    """
    result: dict[str, dict] = {}
    for o in orders:
        state = o.get("OrderState", 0)
        if state not in (1, 2, 4):
            continue
        if o.get("FrontOrderType") != 30:  # 逆指値以外はスキップ
            continue
        sym = str(o.get("Symbol", ""))
        rl  = o.get("ReverseLimitOrder") or {}
        result[sym] = {
            "order_id":     o.get("ID", ""),
            "trigger_price": float(rl.get("TriggerPrice", 0)),
            "side":         o.get("Side", ""),   # "1"=売, "2"=買
        }
    return result


# ── メイン ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="保有銘柄の損切り逆指値を自動設定")
    ap.add_argument("--execute",  action="store_true", help="実際に発注する（デフォルト: dry-run）")
    ap.add_argument("--prod",     action="store_true", help="本番口座(18080)を使う")
    ap.add_argument("--refresh",  action="store_true", help="既存の逆指値を取消して再設定")
    ap.add_argument("--log",      default="my_positions.csv", help="CSVパス")
    args = ap.parse_args()

    csv_path = Path(args.log)
    if not csv_path.exists():
        print(f"✗ ファイルが見つかりません: {csv_path}")
        sys.exit(1)

    positions = load_positions(str(csv_path))
    if not positions:
        print("保有中ポジションなし（または全銘柄 stop_price 未設定）")
        return

    dry_run = not args.execute
    cli = KabuClient(prod=args.prod, dry_run=dry_run)
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}kabu接続中 ({cli.env_label})…")

    try:
        cli.connect()
    except Exception as e:
        print(f"✗ 接続失敗: {e}")
        sys.exit(1)

    # 既存の有効な逆指値注文を取得
    active_stops: dict[str, dict] = {}
    try:
        orders = cli.get_orders()
        active_stops = get_active_stop_orders(orders)
        print(f"既存の有効逆指値注文: {len(active_stops)}件\n")
    except Exception as e:
        print(f"⚠ 注文一覧取得失敗: {e}（重複チェックなしで進みます）\n")

    set_count = 0
    skip_count = 0

    for pos in positions:
        sym    = pos["symbol"]
        name   = pos["name"]
        stop_p = pos["stop_price"]
        qty    = pos["qty"]
        side   = pos["side"]
        cm     = pos["cash_margin"]

        # 信用返済の場合は CASH_MARGIN_CLOSE を使う
        send_cm = CASH_MARGIN_CLOSE if cm == 3 else CASH_GENBUTSU

        existing = active_stops.get(sym)

        if existing and not args.refresh:
            diff = abs(existing["trigger_price"] - stop_p) / max(stop_p, 1)
            if diff < 0.005:  # 0.5% 以内は同じとみなしスキップ
                print(f"  ✓ {sym} {name}: 既存の逆指値 @{existing['trigger_price']:,.0f}円 → スキップ")
                skip_count += 1
                continue
            else:
                print(f"  ↻ {sym} {name}: 価格変更 @{existing['trigger_price']:,.0f} → @{stop_p:,.0f}円 → 取消して再発注")
                cli.cancel_order(existing["order_id"])

        elif existing and args.refresh:
            print(f"  ↻ {sym} {name}: --refresh → 既存取消して再設定")
            cli.cancel_order(existing["order_id"])

        # 発注: ロングは逆指値売り、ショートは逆指値買い（踏み上げ損切り）
        if side == "short":
            print(f"  → {sym} {name}: 損切り逆指値買い @≥{stop_p:,.0f}円 x{qty}株")
            cli.send_stop_buy(sym, qty, stop_p, cash_margin=send_cm)
        else:
            print(f"  → {sym} {name}: 損切り逆指値売り @≤{stop_p:,.0f}円 x{qty}株")
            cli.send_stop_sell(sym, qty, stop_p, cash_margin=send_cm)

        set_count += 1

    print(f"\n{'─'*40}")
    print(f"設定: {set_count}件 / スキップ: {skip_count}件 / 合計: {len(positions)}件")
    if dry_run:
        print("※ dry-run です。実際の発注には --execute を追加してください。")


if __name__ == "__main__":
    main()
