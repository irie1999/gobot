"""
close_stop_guard.py — close 方式の損切りを自動化する「引け前ガード」
=====================================================================

CLAUDE.md §16 の損切り評価モードは既定が **close**（終値が損切り価格を割った
ときだけ引け成行で決済）です。ザラ場に逆指値を置きっぱなしにする intraday と違い、
close は「引けの瞬間の値段で判定」が本質なので、broker に置く 1 注文では再現
できません。そこで毎営業日の引け直前に判定して **引け成行 (MOC) 注文** を出す
このスクリプトで close 方式を自動化します。

【運用の流れ】

  ▼ pre-close モード（既定）: 毎営業日 14:50〜14:55 JST に実行
    python close_stop_guard.py                      # dry-run
    python close_stop_guard.py --execute            # デモ口座に引け成行を発注
    python close_stop_guard.py --execute --prod     # 本番口座 ※明示必須

  ▼ post-close モード（引け後検証）: 毎営業日 15:30 以降に実行
    python close_stop_guard.py --post-close                  # dry-run
    python close_stop_guard.py --post-close --execute        # デモ口座に翌日寄成を発注
    python close_stop_guard.py --post-close --execute --prod # 本番口座

【pre-close と post-close の違い】
  pre-close  : 14:50 の現在値で判定 → MOC (引け成行) を今日のうちに発注
               バックテストの close 損切りに最も近い運用。
  post-close : 15:30 以降に yfinance 終値で判定 → MOO (翌日寄成) を発注
               実際の終値で判定できる。当日は決済できないが判定が正確。

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
  python close_stop_guard.py --execute                # デモ口座に MOC 発注
  python close_stop_guard.py --execute --prod         # 本番口座に MOC 発注
  python close_stop_guard.py --post-close             # dry-run (post-close)
  python close_stop_guard.py --post-close --execute   # デモ口座に MOO 発注
  python close_stop_guard.py --use-kabu-pos --execute # kabu 建玉と照合して発注
  python close_stop_guard.py --log other.csv          # 別のログファイルを使う
  python close_stop_guard.py --aggressive             # aggressive ログを対象にする
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from kabu_api import KabuClient, CASH_GENBUTSU, CASH_MARGIN_CLOSE

JST = timezone(timedelta(hours=9))


# ────────────────────────────────────────────────────────────
# 保有ポジションの読み込み
# ────────────────────────────────────────────────────────────
def _default_log_path(aggressive: bool) -> str:
    return "forward_test_log_aggressive.csv" if aggressive else "forward_test_log.csv"


def load_open_positions(log_path: str) -> list[dict]:
    """forward_test_log.csv から保有中 (filled / holding) のポジションを返す。"""
    p = Path(log_path)
    if not p.exists():
        print(f"⚠ ログが見つかりません: {log_path}")
        return []

    open_pos: list[dict] = []
    with p.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status") not in ("filled", "holding"):
                continue
            try:
                stop_price = float(row["stop_price"])
            except (KeyError, ValueError):
                continue
            side = (row.get("side") or "long").strip().lower()
            is_short = side in ("short", "sell", "s")
            try:
                cm = int(row.get("cash_margin") or 1)
            except ValueError:
                cm = CASH_GENBUTSU
            # ショートは必ず信用建て。CSV が現物(1)になっていたら補正する。
            if is_short and cm == CASH_GENBUTSU:
                print(f"  ⚠ {row['symbol'].strip()}: ショートなのに cash_margin=1 (現物) → 3 (信用返済) に補正")
                cm = CASH_MARGIN_CLOSE
            open_pos.append({
                "symbol": row["symbol"].strip(),
                "name": row.get("name", "").strip(),
                "strategy": row.get("strategy", "").strip(),
                "stop_price": stop_price,
                "is_short": is_short,
                "qty": int(float(row.get("qty") or 100)),
                "fill_date": row.get("fill_date", "").strip(),
                "cash_margin": cm,
                "source": "csv",
            })
    return open_pos


# ────────────────────────────────────────────────────────────
# kabu 建玉との照合（--use-kabu-pos）
# ────────────────────────────────────────────────────────────
def reconcile_with_kabu(csv_positions: list[dict], cli: KabuClient) -> list[dict]:
    """kabu の実建玉を取得して CSV ポジションと照合する。

    照合ルール:
      - CSV にあって kabu にもある  → kabu の qty を採用（CSV の stop_price を維持）
      - CSV にあって kabu にない    → 警告してスキップ（kabu が正なので）
      - kabu にあって CSV にない    → 警告して含める（stop_price は不明 → None）
                                       stop_price が None のものは損切り判定をスキップ
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
            print(f"  ⚠ {sym}: kabu に {side_label} {leaves}株 の建玉があるが CSV にない → 損切り価格不明のため損切り判定スキップ")
            # stop_price=None → 後でスキップ処理
            reconciled.append({
                "symbol": sym,
                "name": "",
                "strategy": "?",
                "stop_price": None,
                "is_short": is_short,
                "qty": leaves,
                "fill_date": "",
                "cash_margin": CASH_MARGIN_CLOSE if is_short else CASH_GENBUTSU,
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


# ────────────────────────────────────────────────────────────
# 引け成行 (MOC) または翌日寄成 (MOO) 発注
# ────────────────────────────────────────────────────────────
def send_moc_order(pos: dict, cli: KabuClient) -> bool:
    """保有ポジションを決済する MOC (pre-close) 注文を発注する。

    ロング → 売り決済 (side="sell")
    ショート → 買い戻し (side="buy")
    cash_margin: 1=現物 / 3=信用返済
    """
    side = "buy" if pos["is_short"] else "sell"
    cm = pos.get("cash_margin", CASH_GENBUTSU)
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"
    side_label = "買い戻し" if pos["is_short"] else "売り決済"
    print(f"    → {label} 引け成行({side_label}) side={side} cash_margin={cm}")
    res = cli.send_moc(pos["symbol"], qty=pos["qty"], side=side, cash_margin=cm)
    return res.get("Result") == 0


def send_moo_order(pos: dict, cli: KabuClient) -> bool:
    """保有ポジションを翌日寄成 (MOO) で決済する。post-close モードで使用。

    ロング → 売り決済 (send_sell order_type="moo")
    ショート → 買い戻し (send_buy order_type="moo")
    """
    from kabu_api import CASH_GENBUTSU, CASH_MARGIN_CLOSE
    side = pos["is_short"]
    cm = pos.get("cash_margin", CASH_GENBUTSU)
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"
    if side:  # ショート → 買い戻し
        side_label = "買い戻し"
        print(f"    → {label} 翌日寄成({side_label}) cash_margin={cm}")
        res = cli.send_buy(pos["symbol"], qty=pos["qty"],
                           cash_margin=cm, order_type="moo")
    else:  # ロング → 売り決済
        side_label = "売り決済"
        print(f"    → {label} 翌日寄成({side_label}) cash_margin={cm}")
        res = cli.send_sell(pos["symbol"], qty=pos["qty"],
                            cash_margin=cm, order_type="moo")
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
    args = ap.parse_args()

    log_path = args.log or _default_log_path(args.aggressive)
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"
    timing_label = "post-close (yfinance終値→翌日MOO)" if args.post_close else "pre-close (現在値→当日MOC)"

    now = datetime.now(JST)
    print("=" * 65)
    print(f"close 損切りガード  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード  : {mode_label}")
    print(f"タイミング: {timing_label}")
    print(f"接続先  : {env_label}  /  ログ: {log_path}")
    if args.use_kabu_pos:
        print("建玉照合: kabu 実建玉と CSV を照合します")
    print("=" * 65)

    positions = load_open_positions(log_path)
    if not positions:
        print("保有中ポジションなし。終了します。")
        return 0
    print(f"CSV 保有中ポジション: {len(positions)} 件\n")

    # kabu クライアント
    cli: KabuClient | None = None
    need_kabu = args.execute or args.use_kabu_pos
    if need_kabu:
        cli = KabuClient(prod=args.prod, dry_run=not args.execute)
        try:
            cli.connect()
            print(f"kabu 接続成功 ({cli.env_label})\n")
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            if args.execute:
                return 1
            # use-kabu-pos だが execute でない場合は続行 (照合スキップ)
            cli = None

    # kabu 建玉との照合
    if args.use_kabu_pos and cli is not None:
        positions = reconcile_with_kabu(positions, cli)
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

    breached: list[dict] = []
    for pos in positions:
        sp = pos.get("stop_price")
        if sp is None:
            print(f"  ? {pos['symbol']} {pos['name']}: 損切り価格不明 → スキップ")
            continue

        price = _get_price(pos["symbol"])
        if price is None:
            print(f"  ? {pos['symbol']} {pos['name']}: 現在値取得不可 → スキップ")
            continue

        hit = (price >= sp) if pos["is_short"] else (price <= sp)
        side_label = "ショート" if pos["is_short"] else "ロング"
        src_label = f"[{pos['source']}]" if pos.get("source") != "csv" else ""
        mark = "🔴損切り" if hit else "  保有継続"
        price_label = "終値" if args.post_close else "現在値"
        print(f"  {mark}  {pos['symbol']} {pos['name']} "
              f"[{pos['strategy']}/{side_label}]{src_label} "
              f"{price_label}={price:.1f} 損切り={sp:.1f}")
        if hit:
            breached.append(pos)

    print()
    if not breached:
        print("損切りライン抵触なし。発注なし。")
        return 0

    print(f"損切り抵触: {len(breached)} 件")
    if not args.execute:
        order_type = "MOO (翌日寄成)" if args.post_close else "MOC (引け成行)"
        print(f"dry-run のため発注しません ({order_type})。実発注するには --execute を付けてください。")
        return 0

    if args.post_close:
        print(f"翌日寄成 (MOO) を {env_label} に発注します...")
        ok = 0
        for pos in breached:
            if send_moo_order(pos, cli):
                ok += 1
        print(f"\n発注完了: {ok}/{len(breached)} 件成功 (翌日寄成)")
    else:
        print(f"引け成行 (MOC) を {env_label} に発注します...")
        ok = 0
        for pos in breached:
            if send_moc_order(pos, cli):
                ok += 1
        print(f"\n発注完了: {ok}/{len(breached)} 件成功 (引け成行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
