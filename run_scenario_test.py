"""
run_scenario_test.py — kabu デモ環境でのシナリオテスト
======================================================

デモ口座(18081)でエンドツーエンドのシナリオを順番に確認する。
実際の my_positions.csv を変更せず test_positions.csv を使う。

【事前準備】
  1. kabuステーションを起動してデモ口座にログイン
  2. 環境変数を設定: export KABU_API_PASSWORD_DEMO=<デモAPIパスワード>
  3. このスクリプトを実行

【シナリオ一覧】
  S1  : 接続・銘柄価格確認
  S2  : エントリー逆指値発注（翌日約定待ち）
  S3  : 約定確認 → test_positions.csv に反映
  S4  : 損切り逆指値注文を設定
  S5  : 利確指値注文を設定
  S6  : 損切りライン抵触 → 引け成行（MOC）確認
  S7  : 利確ライン到達 → 引け成行（MOC）確認
  S8  : MAX_HOLD 超過 → タイムカット引け成行確認
  S9  : --refresh で注文を全て再設定

使い方:
  python run_scenario_test.py --scenario 1          # S1 のみ実行
  python run_scenario_test.py --scenario 1 2 3      # 複数
  python run_scenario_test.py --all                 # S1〜S9 全部
  python run_scenario_test.py --scenario 4 --execute # 実発注
  python run_scenario_test.py --symbol 7203         # テスト銘柄を指定
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

TEST_CSV = "test_positions.csv"
TEST_QTY = 100
CASH_MARGIN = 3   # 3=信用返済（テストポジションは信用想定）


# ── ユーティリティ ────────────────────────────────────────────────────────────

def _header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _step(msg: str) -> None:
    print(f"  → {msg}")


def _pause(msg: str = "確認したら Enter を押して次へ進みます…") -> None:
    input(f"\n  [{msg}] ")


def _make_cli(prod: bool = False, dry_run: bool = True):
    from kabu_api import KabuClient
    cli = KabuClient(prod=prod, dry_run=dry_run)
    cli.connect()
    return cli


# ── テスト CSV 操作 ────────────────────────────────────────────────────────────

def _write_test_csv(rows: list[dict]) -> None:
    fieldnames = [
        "record_date", "symbol", "name", "strategy", "family",
        "signal_date", "signal_price", "order_price",
        "stop_price", "target_price", "status",
        "fill_date", "fill_price", "exit_date", "exit_price",
        "pnl", "updated_date", "side", "qty", "cash_margin", "bt_score",
    ]
    with open(TEST_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = {k: "" for k in fieldnames}
            row.update(r)
            w.writerow(row)


def _read_test_csv() -> list[dict]:
    p = Path(TEST_CSV)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── シナリオ ──────────────────────────────────────────────────────────────────

def s1_connect(symbol: str, execute: bool) -> dict:
    """S1: 接続・銘柄価格確認"""
    _header("S1: 接続・銘柄価格確認")

    cli = _make_cli(prod=False, dry_run=False)
    _ok(f"kabu デモ接続成功 ({cli.env_label})")

    price = cli.get_current_price(symbol)
    if price is None:
        board = cli.get_board(symbol)
        print(f"  ボード情報: {board}")
        _warn(f"{symbol}: 現在値が取得できませんでした（市場が閉じている可能性）")
        price = float(input("  手動で現在値を入力してください: "))
    else:
        _ok(f"{symbol}: 現在値 = {price:,.1f} 円")

    # 注文価格の計算（現在値の+1% を逆指値買い = ブレイクアウト想定）
    order_p  = round(price * 1.01)
    stop_p   = round(price * 0.94)    # 約-6%損切り
    target_p = round(price * 1.09)    # 約+9%利確

    print(f"\n  テスト注文価格:")
    print(f"    逆指値買い（エントリー）: @≥ {order_p:,} 円")
    print(f"    損切り逆指値（損切り）:   @≤ {stop_p:,} 円  (現在値の-{(1-stop_p/price)*100:.1f}%)")
    print(f"    利確指値（目標）:         @ {target_p:,} 円  (現在値の+{(target_p/price-1)*100:.1f}%)")

    return {
        "symbol": symbol,
        "price": price,
        "order_p": order_p,
        "stop_p": stop_p,
        "target_p": target_p,
    }


def s2_entry(ctx: dict, execute: bool) -> None:
    """S2: エントリー逆指値発注"""
    _header("S2: エントリー逆指値発注")

    sym      = ctx["symbol"]
    order_p  = ctx["order_p"]
    stop_p   = ctx["stop_p"]
    target_p = ctx["target_p"]

    today = str(date.today())
    order_id = "DRYRUN"

    if execute:
        from kabu_api import KabuClient, CASH_MARGIN_OPEN
        cli = _make_cli(prod=False, dry_run=False)
        _step(f"{sym} 逆指値買い @≥{order_p:,}円 x{TEST_QTY}株 (信用新規)")
        res = cli.send_stop_buy(sym, qty=TEST_QTY, trigger_price=order_p,
                                cash_margin=CASH_MARGIN_OPEN)
        if res.get("Result") != 0:
            _warn(f"発注失敗: {res}")
            return
        order_id = res.get("OrderId", "DRYRUN")
        _ok(f"発注成功 OrderId={order_id}")
    else:
        print(f"  [dry-run] 逆指値買い {sym} x{TEST_QTY} @≥{order_p:,}円 (信用新規)")
        _ok("dry-run のため実発注しません")

    # テスト CSV に pending 行を作成（dry-run でも作成する）
    rows = _read_test_csv()
    rows = [r for r in rows if r.get("symbol") != sym]
    rows.append({
        "record_date":  today,
        "symbol":       sym,
        "name":         sym,
        "strategy":     "TEST",
        "signal_date":  today,
        "order_price":  order_p,
        "stop_price":   stop_p,
        "target_price": target_p,
        "status":       "pending",
        "updated_date": today,
        "side":         "long",
        "qty":          TEST_QTY,
        "cash_margin":  CASH_MARGIN,
    })
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} に pending 行を追加しました（OrderId={order_id}）")

    if execute:
        print(f"\n  次のステップ:")
        print(f"    注文 (OrderId={order_id}) が約定したら S3 を実行してください")
        print(f"    デモ環境では翌営業日の寄り付きに約定することが多いです")
    else:
        print(f"\n  dry-run のため S3 では手動で holding に更新してシミュレートします")


def s3_sync(ctx: dict, execute: bool) -> None:
    """S3: 約定確認 → test_positions.csv に反映"""
    _header("S3: 約定確認 → test_positions.csv 反映")

    sym = ctx["symbol"]

    if execute:
        import subprocess
        cmd = [sys.executable, "kabu_position_sync.py", "--log", TEST_CSV, "--execute"]
        subprocess.run(cmd)
        rows = _read_test_csv()
        holding = [r for r in rows if r.get("status") == "holding"]
        if holding:
            _ok(f"holding に更新された銘柄: {[r['symbol'] for r in holding]}")
        else:
            _warn("まだ pending のまま（約定していないか照合できなかった）")
            print("  → kabu 注文一覧で状態を確認してください")
    else:
        # dry-run: pending → holding に手動でシミュレート
        today = str(date.today())
        yesterday = str(date.today() - timedelta(days=1))
        rows = _read_test_csv()
        for r in rows:
            if r.get("symbol") == sym and r.get("status") == "pending":
                r["status"]     = "holding"
                r["fill_date"]  = yesterday
                r["fill_price"] = str(ctx["price"])
                r["updated_date"] = today
        _write_test_csv(rows)
        _ok(f"[dry-run] {sym} を pending → holding にシミュレート "
            f"(fill_date={yesterday}, fill_price={ctx['price']:.1f})")


def s4_stop_orders(ctx: dict, execute: bool) -> None:
    """S4: 損切り逆指値注文を設定"""
    _header("S4: 損切り逆指値注文の設定")

    import subprocess
    cmd = [sys.executable, "set_stop_orders.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)


def s5_profit_orders(ctx: dict, execute: bool) -> None:
    """S5: 利確指値注文を設定"""
    _header("S5: 利確指値注文の設定")

    import subprocess
    cmd = [sys.executable, "set_profit_orders.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)


def s6_stop_loss_moc(ctx: dict, execute: bool) -> None:
    """S6: 損切りライン抵触 → 引け成行（MOC）確認
    テスト用に stop_price を現在値より高く設定してシミュレート
    """
    _header("S6: 損切りライン抵触 → 引け成行（MOC）確認")

    sym   = ctx["symbol"]
    price = ctx["price"]

    # stop_price を現在値より高く設定（わざと損切りラインを突破させる）
    fake_stop = round(price * 1.10)  # 現在値+10% → 必ず「割った」状態
    print(f"  テスト: {sym} の stop_price を {fake_stop:,}円（現在値+10%）に設定")
    print(f"  （現在値={price:.1f}円 < stop_price={fake_stop:,}円 → 損切り抵触）")

    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["stop_price"] = fake_stop
            r["status"] = "holding"
            r["fill_date"] = str(date.today() - timedelta(days=1))
            r["fill_price"] = str(price)
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} の stop_price を {fake_stop:,} 円に更新")

    import subprocess
    cmd = [sys.executable, "close_stop_guard.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)

    # テスト後に元の stop_price に戻す
    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["stop_price"] = ctx["stop_p"]
    _write_test_csv(rows)
    _ok("stop_price を元の値に戻しました")


def s7_profit_moc(ctx: dict, execute: bool) -> None:
    """S7: 利確ライン到達 → 引け成行（MOC）確認
    テスト用に target_price を現在値より低く設定
    """
    _header("S7: 利確ライン到達 → 引け成行（MOC）確認")

    sym   = ctx["symbol"]
    price = ctx["price"]

    # target_price を現在値より低く設定（「超えた」状態にする）
    fake_target = round(price * 0.90)  # 現在値-10% → 必ず「到達」
    print(f"  テスト: {sym} の target_price を {fake_target:,}円（現在値-10%）に設定")
    print(f"  （現在値={price:.1f}円 > target_price={fake_target:,}円 → 利確到達）")

    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["target_price"] = fake_target
            r["stop_price"] = ctx["stop_p"]
            r["status"] = "holding"
            r["fill_date"] = str(date.today() - timedelta(days=1))
            r["fill_price"] = str(price)
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} の target_price を {fake_target:,} 円に更新")

    import subprocess
    cmd = [sys.executable, "close_stop_guard.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)

    # テスト後に元の target_price に戻す
    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["target_price"] = ctx["target_p"]
    _write_test_csv(rows)
    _ok("target_price を元の値に戻しました")


def s8_timecut_moc(ctx: dict, execute: bool) -> None:
    """S8: MAX_HOLD 超過 → タイムカット引け成行"""
    _header("S8: MAX_HOLD 超過 → タイムカット引け成行")

    from backtest_limit_entry import default_max_hold
    sym      = ctx["symbol"]
    strat    = "TEST"
    max_hold = default_max_hold(strat)  # 15日

    # fill_date を MAX_HOLD + 10 営業日 (≈ 14 暦日) 前に設定して確実にタイムカット対象にする
    old_date = str(date.today() - timedelta(days=max_hold + 14))
    print(f"  テスト: {sym} の fill_date を {old_date} に設定")
    print(f"  （MAX_HOLD={max_hold}日 → 確実にタイムカット対象）")

    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["fill_date"]    = old_date
            r["fill_price"]   = str(ctx["price"])
            r["status"]       = "holding"
            r["stop_price"]   = ctx["stop_p"]
            r["target_price"] = ctx["target_p"]
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} の fill_date を {old_date} に更新")

    import subprocess
    cmd = [sys.executable, "close_stop_guard.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)

    # テスト後に fill_date を今日に戻す
    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["fill_date"] = str(date.today() - timedelta(days=1))
    _write_test_csv(rows)
    _ok("fill_date を元の値に戻しました")


def s9_refresh(ctx: dict, execute: bool) -> None:
    """S9: --refresh で既存注文を全て取消して再設定"""
    _header("S9: --refresh で注文を全て再設定")

    import subprocess
    print("  [損切り逆指値 --refresh]")
    cmd = [sys.executable, "set_stop_orders.py", "--log", TEST_CSV, "--refresh"]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)

    print("\n  [利確指値 --refresh]")
    cmd = [sys.executable, "set_profit_orders.py", "--log", TEST_CSV, "--refresh"]
    if execute:
        cmd.append("--execute")
    subprocess.run(cmd)


# ── メイン ───────────────────────────────────────────────────────────────────

SCENARIOS = {
    1: ("接続・銘柄価格確認",             s1_connect,      False),
    2: ("エントリー逆指値発注",            s2_entry,        True),
    3: ("約定確認 → CSV 反映",            s3_sync,         True),
    4: ("損切り逆指値注文を設定",           s4_stop_orders,  True),
    5: ("利確指値注文を設定",              s5_profit_orders, True),
    6: ("損切りライン抵触 → MOC",         s6_stop_loss_moc, True),
    7: ("利確ライン到達 → MOC",           s7_profit_moc,   True),
    8: ("MAX_HOLD 超過 → タイムカット",    s8_timecut_moc,  True),
    9: ("--refresh で注文を全て再設定",    s9_refresh,      True),
}


def main() -> None:
    ap = argparse.ArgumentParser(description="kabu デモ環境でのシナリオテスト")
    ap.add_argument("--scenario", "-s", type=int, nargs="+",
                    help="実行するシナリオ番号（例: --scenario 1 2 3）")
    ap.add_argument("--all", action="store_true", help="全シナリオを順番に実行")
    ap.add_argument("--execute", action="store_true",
                    help="実際に kabu へ発注/照会する（省略時は dry-run）")
    ap.add_argument("--symbol", default="7203",
                    help="テスト銘柄コード（デフォルト: 7203 トヨタ）")
    ap.add_argument("--list", action="store_true", help="シナリオ一覧を表示して終了")
    args = ap.parse_args()

    if args.list or not (args.scenario or args.all):
        print("\nシナリオ一覧:")
        for n, (title, _, needs_kabu) in SCENARIOS.items():
            needs = "（kabu接続必須）" if needs_kabu else ""
            print(f"  S{n}: {title} {needs}")
        print(f"\n使い方:")
        print(f"  python {Path(__file__).name} --scenario 1          # S1 だけ")
        print(f"  python {Path(__file__).name} --all                  # S1〜S9 全部")
        print(f"  python {Path(__file__).name} --all --execute        # 実発注あり")
        print(f"  python {Path(__file__).name} --scenario 6 7 8      # 決済シナリオのみ")
        return

    targets = list(range(1, 10)) if args.all else sorted(set(args.scenario))
    invalid = [n for n in targets if n not in SCENARIOS]
    if invalid:
        print(f"✗ 無効なシナリオ番号: {invalid}")
        sys.exit(1)

    if not args.execute:
        print("\n[DRY-RUN モード] kabu への実発注・CSV更新はしません")
        print("実際に動かすには --execute を付けてください\n")

    # S1 は必ず最初に実行してコンテキストを作る
    # S1 が targets に含まれない場合は dry-run のダミー値で初期化する
    dummy_price = 3000.0
    ctx: dict = {
        "symbol":   args.symbol,
        "price":    dummy_price,
        "order_p":  round(dummy_price * 1.01),
        "stop_p":   round(dummy_price * 0.94),
        "target_p": round(dummy_price * 1.09),
    }

    if 1 not in targets:
        if not args.execute:
            print(f"\n[DRY-RUN] S1 をスキップして仮の価格を使います")
            print(f"  仮価格: {ctx['price']:,.0f} → 逆指値={ctx['order_p']:,} "
                  f"損切={ctx['stop_p']:,} 利確={ctx['target_p']:,}")

    for n in targets:
        title, fn, needs_kabu = SCENARIOS[n]

        if n == 1:
            try:
                if args.execute:
                    ctx = fn(args.symbol, args.execute)
                else:
                    # dry-run: S1 は接続なしでダミー値を設定
                    print(f"\n[DRY-RUN] S1 をスキップして仮の価格を使います")
                    ctx = {
                        "symbol":   args.symbol,
                        "price":    dummy_price,
                        "order_p":  round(dummy_price * 1.01),
                        "stop_p":   round(dummy_price * 0.94),
                        "target_p": round(dummy_price * 1.09),
                    }
                    print(f"  仮価格: {ctx['price']:,} → 逆指値={ctx['order_p']:,} "
                          f"損切={ctx['stop_p']:,} 利確={ctx['target_p']:,}")
            except Exception as e:
                print(f"  ✗ S1 失敗: {e}")
                sys.exit(1)
        else:
            try:
                fn(ctx, args.execute)
            except Exception as e:
                print(f"\n  ✗ S{n} 失敗: {e}")
                import traceback
                traceback.print_exc()

        if args.all and n < max(targets):
            _pause(f"S{n} 完了。次は S{n+1} ({SCENARIOS.get(n+1, ('',))[0]})")

    print(f"\n{'='*60}")
    print("テスト完了")
    print(f"テスト CSV: {TEST_CSV}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
