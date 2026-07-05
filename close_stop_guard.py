"""
close_stop_guard.py — close 方式の損切りを自動化する「引け前ガード」
=====================================================================

CLAUDE.md §16 の損切り評価モードは既定が **close**（終値が損切り価格を割った
ときだけ引け成行で決済）です。ザラ場に逆指値を置きっぱなしにする intraday と違い、
close は「引けの瞬間の値段で判定」が本質なので、broker に置く 1 注文では再現
できません。そこで毎営業日の引け直前に判定して **引け成行 (MOC) 注文** を出す
このスクリプトで close 方式を自動化します。

【運用の流れ】

  毎営業日 14:50〜14:55 JST に実行 (cron / タスクスケジューラ):
    python close_stop_guard.py            # dry-run: 判定だけ表示 (発注しない)
    python close_stop_guard.py --execute  # デモ口座(18081)に引け成行を発注
    python close_stop_guard.py --execute --prod  # 本番口座(18080) ※明示必須

  1. forward_test_log.csv から保有中ポジション (status=filled/holding) を読む
  2. 各銘柄の現在値を取得 (kabu /board、取得失敗時は最新終値でフォールバック)
  3. 現在値 <= 損切り価格 (ロング) / >= 損切り価格 (ショート) なら
     引け成行 (FrontOrderType=16 引成) 注文を発注
  4. 引け (15:00) で実際の終値で約定 → バックテストの close 方式とほぼ一致

【安全設計】
  - デフォルトは dry-run。--execute を付けたときだけ実発注する。
  - --execute でも接続先は既定でデモ(18081)。本番は --prod を明示しないと使えない。
  - 損切りラインは forward_test_log.csv の stop_price をそのまま使う
    (エントリー時に記録済みの値。再計算しない)。

【前提】
  - kabuステーションが起動し、API パスワードが環境変数 KABU_API_PASSWORD に
    入っていること (--execute 時のみ必須)。
  - 現物の買い建て (ロング) を売り決済する想定。信用の返済は §注意 を参照。

使い方:
  python close_stop_guard.py                 # dry-run
  python close_stop_guard.py --execute       # デモ口座に発注
  python close_stop_guard.py --execute --prod  # 本番口座に発注 (要明示)
  python close_stop_guard.py --log other.csv # 別のログファイルを使う
  python close_stop_guard.py --aggressive    # aggressive ログを対象にする
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# kabuステーション連携は共通クライアントに集約
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
            # side 列があれば使う。無ければロング (逆指値買い) とみなす
            side = (row.get("side") or "long").strip().lower()
            is_short = side in ("short", "sell", "s")
            # cash_margin 列: 1=現物, 3=信用返済。無ければ現物とみなす
            try:
                cm = int(row.get("cash_margin") or 1)
            except ValueError:
                cm = CASH_GENBUTSU
            open_pos.append({
                "symbol": row["symbol"].strip(),
                "name": row.get("name", "").strip(),
                "strategy": row.get("strategy", "").strip(),
                "stop_price": stop_price,
                "is_short": is_short,
                "qty": int(float(row.get("qty") or 100)),
                "fill_date": row.get("fill_date", "").strip(),
                "cash_margin": cm,
            })
    return open_pos


# ────────────────────────────────────────────────────────────
# 現在値の取得
# ────────────────────────────────────────────────────────────
def get_current_price_fallback(symbol: str) -> float | None:
    """kabu が使えない (dry-run 等) ときの最新終値フォールバック。"""
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
# 引け成行 (MOC) 発注
# ────────────────────────────────────────────────────────────
def send_moc_order(pos: dict, cli: KabuClient) -> bool:
    """保有ポジションを決済する引け成行 (MOC) 注文を発注する。

    ロング (is_short=False) → 売り決済、ショート → 買い戻し。
    CSV の cash_margin 列: 1=現物(既定) / 3=信用返済。
    信用返済 (cash_margin=3) の場合、建玉 ID を API から自動取得して ClosePositions に設定する。
    """
    side = "buy" if pos["is_short"] else "sell"
    cm = pos.get("cash_margin", CASH_GENBUTSU)
    label = "信用返済" if cm == CASH_MARGIN_CLOSE else "現物"
    print(f"    → {label} 引け成行({side}) cash_margin={cm}")
    res = cli.send_moc(pos["symbol"], qty=pos["qty"], side=side, cash_margin=cm)
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
    args = ap.parse_args()

    log_path = args.log or _default_log_path(args.aggressive)
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"

    now = datetime.now(JST)
    print("=" * 60)
    print(f"close 損切りガード  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label}  /  接続先: {env_label}  /  ログ: {log_path}")
    print("=" * 60)

    positions = load_open_positions(log_path)
    if not positions:
        print("保有中ポジションなし。終了します。")
        return 0
    print(f"保有中ポジション: {len(positions)} 件\n")

    # kabu クライアント: --execute のときだけ接続して発注する
    cli: KabuClient | None = None
    if args.execute:
        cli = KabuClient(prod=args.prod, dry_run=False)
        try:
            cli.connect()
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            return 1

    breached: list[dict] = []
    for pos in positions:
        # 現在値: 接続済みなら kabu /board、未接続(dry-run)は終値フォールバック
        if cli is not None:
            price = cli.get_current_price(pos["symbol"])
            if price is None:
                price = get_current_price_fallback(pos["symbol"])
        else:
            price = get_current_price_fallback(pos["symbol"])

        if price is None:
            print(f"  ? {pos['symbol']} {pos['name']}: 現在値取得不可 → スキップ")
            continue

        sp = pos["stop_price"]
        hit = (price >= sp) if pos["is_short"] else (price <= sp)
        side_label = "ショート" if pos["is_short"] else "ロング"
        mark = "🔴損切り" if hit else "  保有継続"
        print(f"  {mark}  {pos['symbol']} {pos['name']} [{pos['strategy']}/{side_label}] "
              f"現在値={price:.1f} 損切り={sp:.1f}")
        if hit:
            breached.append(pos)

    print()
    if not breached:
        print("損切りライン抵触なし。発注なし。")
        return 0

    print(f"損切り抵触: {len(breached)} 件")
    if not args.execute:
        print("dry-run のため発注しません。実発注するには --execute を付けてください。")
        return 0

    print(f"引け成行 (MOC) を {env_label} に発注します...")
    ok = 0
    for pos in breached:
        if send_moc_order(pos, cli):
            ok += 1
    print(f"\n発注完了: {ok}/{len(breached)} 件成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
