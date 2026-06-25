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
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

TEST_CSV = "test_positions.csv"
TEST_QTY = 100
CASH_MARGIN = 3   # 3=信用返済（テストポジションは信用想定）
CTX_FILE  = ".test_ctx.json"   # S1 の結果を保存してプロセス間で共有
_PROD     = False               # --prod で True になる（本番口座 18080 を使用）


# ── ctx の保存・読み込み ──────────────────────────────────────────────────────

def _save_ctx(ctx: dict) -> None:
    Path(CTX_FILE).write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")


def _load_ctx(symbol: str) -> dict | None:
    p = Path(CTX_FILE)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("symbol") == symbol:
            return data
    except Exception:
        pass
    return None


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


def _make_cli(prod: bool | None = None, dry_run: bool = True):
    from kabu_api import KabuClient
    if prod is None:
        prod = _PROD
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

    cli = _make_cli(dry_run=False)
    env = "本番(18080)" if _PROD else "デモ(18081)"
    _ok(f"kabu 接続成功 ({env})")

    # 保有建玉一覧を表示
    try:
        positions = cli.get_positions(product=0)
        if positions:
            print(f"\n  保有建玉 ({len(positions)}件):")
            for p in positions:
                sym_p  = p.get("Symbol", "?")
                name_p = p.get("SymbolName", "")[:10]
                side_p = "売建(ショート)" if str(p.get("Side","")) == "1" else "買建(ロング)"
                qty_p  = p.get("LeavesQty") or p.get("Qty") or "?"
                price_p = p.get("CurrentPrice") or p.get("Price") or "?"
                pnl_p  = p.get("Profit") or p.get("ProfitLoss") or ""
                pnl_str = f"  損益={pnl_p:+,.0f}円" if isinstance(pnl_p, (int, float)) else ""
                print(f"    {sym_p} {name_p} [{side_p}] {qty_p}株 @{price_p}{pnl_str}")
        else:
            _warn("kabu に保有建玉がありません")
    except Exception as e:
        _warn(f"保有建玉取得失敗: {e}")

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

    ctx = {
        "symbol": symbol,
        "price": price,
        "order_p": order_p,
        "stop_p": stop_p,
        "target_p": target_p,
    }
    _save_ctx(ctx)
    _ok(f"ctx を {CTX_FILE} に保存しました（次回 S2〜S9 実行時に自動読み込み）")
    return ctx


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
        cli = _make_cli(dry_run=False)
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


def s3_sync(ctx: dict, execute: bool, force_holding: bool = False) -> None:
    """S3: 約定確認 → test_positions.csv に反映"""
    _header("S3: 約定確認 → test_positions.csv 反映")

    sym = ctx["symbol"]

    if execute and not force_holding:
        import subprocess
        cmd = [sys.executable, "kabu_position_sync.py", "--log", TEST_CSV, "--execute"]
        if _PROD:
            cmd.append("--prod")
        subprocess.run(cmd)
        rows = _read_test_csv()
        holding = [r for r in rows if r.get("status") == "holding"]
        if holding:
            _ok(f"holding に更新された銘柄: {[r['symbol'] for r in holding]}")
        else:
            _warn("まだ pending のまま（約定していないか照合できなかった）")
            print("  → テストを続けるには --force-holding を付けて S3 を再実行してください:")
            print(f"     python run_scenario_test.py --scenario 3 --execute --force-holding --symbol {sym}")
    elif force_holding:
        # kabu照合なしで強制的にholding化（テスト継続用）
        today = str(date.today())
        yesterday = str(date.today() - timedelta(days=1))
        rows = _read_test_csv()
        updated = 0
        for r in rows:
            if r.get("symbol") == sym and r.get("status") == "pending":
                r["status"]       = "holding"
                r["fill_date"]    = yesterday
                r["fill_price"]   = str(ctx["price"])
                r["updated_date"] = today
                updated += 1
        _write_test_csv(rows)
        mode = "" if execute else "[dry-run] "
        _ok(f"{mode}{sym} を pending → holding に強制更新 "
            f"(fill_date={yesterday}, fill_price={ctx['price']:.1f}) {updated}件")
        if execute:
            _warn("注意: kabu の実際の約定価格ではなく S1 の価格を使っています")
    else:
        print(f"\n  dry-run のため S3 では手動で holding に更新してシミュレートします")


def s4_stop_orders(ctx: dict, execute: bool) -> None:
    """S4: 損切り逆指値注文を設定"""
    _header("S4: 損切り逆指値注文の設定")

    import subprocess
    cmd = [sys.executable, "set_stop_orders.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    if _PROD:
        cmd.append("--prod")
    subprocess.run(cmd)


def s5_profit_orders(ctx: dict, execute: bool) -> None:
    """S5: 利確指値注文を設定"""
    _header("S5: 利確指値注文の設定")

    import subprocess
    cmd = [sys.executable, "set_profit_orders.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    if _PROD:
        cmd.append("--prod")
    subprocess.run(cmd)


def _get_real_price(sym: str, fallback: float, execute: bool) -> float:
    """execute モードなら kabu board → yfinance の順で現在値を取得。失敗したら fallback を返す。
    close_stop_guard._get_price() と同じフォールバック順序にすることで
    fake_stop / fake_target を実際の判定価格に対して正しく設定できる。
    """
    if not execute:
        return fallback
    # 1. kabu board API
    try:
        cli = _make_cli(prod=False, dry_run=False)
        p = cli.get_current_price(sym)
        if p and p > 0:
            return p
    except Exception:
        pass
    # 2. yfinance フォールバック（close_stop_guard と同じ経路）
    try:
        from backtest_limit_entry import fetch
        df = fetch(f"{sym}.T")
        if df is not None and not df.empty:
            p = float(df.iloc[-1]["close"])
            if p > 0:
                _warn(f"{sym}: kabu API 未応答のため yfinance 終値 {p:.1f} を使います")
                return p
    except Exception:
        pass
    _warn(f"{sym}: 現在値取得に失敗。S1 の価格 {fallback:.1f} を使います")
    return fallback


def s6_stop_loss_moc(ctx: dict, execute: bool) -> None:
    """S6: 損切りライン抵触 → 引け成行（MOC）確認
    テスト用に stop_price を現在値より高く設定してシミュレート
    """
    _header("S6: 損切りライン抵触 → 引け成行（MOC）確認")

    sym   = ctx["symbol"]
    price = _get_real_price(sym, ctx["price"], execute)

    # stop_price を現在値より高く設定（わざと損切りラインを突破させる）
    # target_price は現在値の2倍以上に設定して利確が先に発火しないようにする
    fake_stop   = round(price * 1.10)  # 現在値+10% → 必ず「割った」状態
    fake_target = round(price * 2.00)  # 現在値×2 → 到達不可
    print(f"  テスト: {sym} の stop_price を {fake_stop:,}円（現在値+10%）に設定")
    print(f"  （現在値={price:.1f}円 < stop_price={fake_stop:,}円 → 損切り抵触）")

    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["stop_price"]   = fake_stop
            r["target_price"] = fake_target
            r["status"]       = "holding"
            r["fill_date"]    = str(date.today() - timedelta(days=1))
            r["fill_price"]   = str(price)
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} の stop_price を {fake_stop:,} 円・target_price を {fake_target:,} 円に更新")

    import subprocess
    cmd = [sys.executable, "close_stop_guard.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    if _PROD:
        cmd.append("--prod")
    subprocess.run(cmd)

    # テスト後に元の値に戻す
    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["stop_price"]   = ctx["stop_p"]
            r["target_price"] = ctx["target_p"]
    _write_test_csv(rows)
    _ok("stop_price / target_price を元の値に戻しました")


def s7_profit_moc(ctx: dict, execute: bool) -> None:
    """S7: 利確ライン到達 → 引け成行（MOC）確認
    テスト用に target_price を現在値より低く設定
    """
    _header("S7: 利確ライン到達 → 引け成行（MOC）確認")

    sym   = ctx["symbol"]
    price = _get_real_price(sym, ctx["price"], execute)

    # target_price を現在値より低く設定（「超えた」状態にする）
    # ただし stop_price より高く設定して損切りより利確が先に判定されるようにする
    fake_stop   = round(price * 0.50)  # 損切りは現在値の半値以下（絶対に発火しない）
    fake_target = round(price * 0.90)  # 利確は現在値の-10%（必ず到達済み）
    print(f"  テスト: {sym} の target_price を {fake_target:,}円（現在値-10%）に設定")
    print(f"  （現在値={price:.1f}円 >= target_price={fake_target:,}円 → 利確到達）")

    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["target_price"] = fake_target
            r["stop_price"]   = fake_stop
            r["status"]       = "holding"
            r["fill_date"]    = str(date.today() - timedelta(days=1))
            r["fill_price"]   = str(price)
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} の target_price を {fake_target:,} 円・stop_price を {fake_stop:,} 円に更新")

    import subprocess
    cmd = [sys.executable, "close_stop_guard.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    if _PROD:
        cmd.append("--prod")
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
    price    = _get_real_price(sym, ctx["price"], execute)
    strat    = "TEST"
    max_hold = default_max_hold(strat)  # 15日

    # fill_date を MAX_HOLD + 10 営業日 (≈ 14 暦日) 前に設定して確実にタイムカット対象にする
    old_date = str(date.today() - timedelta(days=max_hold + 14))
    print(f"  テスト: {sym} の fill_date を {old_date} に設定")
    print(f"  （MAX_HOLD={max_hold}日 → 確実にタイムカット対象）")

    # 損切り・利確を現在値から外れた場所に設定して、タイムカットより先に発火しないようにする
    safe_stop   = round(price * 0.50)   # 現在値の半値以下（絶対に発火しない）
    safe_target = round(price * 2.00)   # 現在値の2倍以上（絶対に発火しない）

    rows = _read_test_csv()
    for r in rows:
        if r.get("symbol") == sym:
            r["fill_date"]    = old_date
            r["fill_price"]   = str(price)
            r["status"]       = "holding"
            r["stop_price"]   = safe_stop
            r["target_price"] = safe_target
    _write_test_csv(rows)
    _ok(f"{TEST_CSV} の fill_date を {old_date} に更新")

    import subprocess
    cmd = [sys.executable, "close_stop_guard.py", "--log", TEST_CSV]
    if execute:
        cmd.append("--execute")
    if _PROD:
        cmd.append("--prod")
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
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)を使う（省略時はデモ(18081)）")
    ap.add_argument("--symbol", default="7203",
                    help="テスト銘柄コード（デフォルト: 7203 トヨタ）")
    ap.add_argument("--force-holding", action="store_true",
                    help="S3 で kabu 照合なしに強制 holding 更新（注文が約定前でもテスト続行）")
    ap.add_argument("--reset-ctx", action="store_true",
                    help=f"{CTX_FILE} を削除してコンテキストをリセット")
    ap.add_argument("--list", action="store_true", help="シナリオ一覧を表示して終了")
    ap.add_argument("--positions", action="store_true",
                    help="kabu の保有建玉一覧を表示して終了")
    args = ap.parse_args()

    global _PROD
    _PROD = args.prod

    if args.positions:
        env = "本番(18080)" if _PROD else "デモ(18081)"
        print(f"\nkabu 保有建玉を取得中 ({env})…")
        try:
            cli = _make_cli(dry_run=False)
            positions = cli.get_positions(product=0)
        except Exception as e:
            print(f"✗ 接続失敗: {e}")
            sys.exit(1)
        if not positions:
            print("  保有建玉なし")
        else:
            print(f"  保有建玉 {len(positions)}件:\n")
            for p in positions:
                sym_p   = p.get("Symbol", "?")
                name_p  = (p.get("SymbolName") or "")[:12]
                side_p  = "売建(ショート)" if str(p.get("Side","")) == "1" else "買建(ロング)"
                qty_p   = p.get("LeavesQty") or p.get("Qty") or "?"
                price_p = p.get("CurrentPrice") or p.get("Price") or "?"
                avg_p   = p.get("AveragePrice") or ""
                pnl_p   = p.get("Profit") or p.get("ProfitLoss") or ""
                avg_str = f"  平均={avg_p:,.1f}円" if isinstance(avg_p, float) else ""
                pnl_str = f"  損益={pnl_p:+,.0f}円" if isinstance(pnl_p, (int, float)) else ""
                print(f"  {sym_p} {name_p} [{side_p}] {qty_p}株 現在値={price_p}{avg_str}{pnl_str}")
        return

    if args.list or not (args.scenario or args.all or args.reset_ctx):
        print("\nシナリオ一覧:")
        for n, (title, _, needs_kabu) in SCENARIOS.items():
            needs = "（kabu接続必須）" if needs_kabu else ""
            print(f"  S{n}: {title} {needs}")
        print(f"\n使い方:")
        print(f"  python {Path(__file__).name} --scenario 1 --execute    # S1 だけ（kabu接続）")
        print(f"  python {Path(__file__).name} --scenario 3 --execute --force-holding  # 強制holding")
        print(f"  python {Path(__file__).name} --all --execute            # S1〜S9 全部")
        print(f"  python {Path(__file__).name} --scenario 6 7 8 --execute # 決済シナリオのみ")
        print(f"  python {Path(__file__).name} --reset-ctx                # コンテキストをリセット")
        return

    if args.reset_ctx:
        p = Path(CTX_FILE)
        if p.exists():
            p.unlink()
            print(f"✓ {CTX_FILE} を削除しました")
        else:
            print(f"ℹ {CTX_FILE} は存在しません")
        if not (args.scenario or args.all):
            return

    targets = list(range(1, 10)) if args.all else sorted(set(args.scenario))
    invalid = [n for n in targets if n not in SCENARIOS]
    if invalid:
        print(f"✗ 無効なシナリオ番号: {invalid}")
        sys.exit(1)

    if not args.execute:
        print("\n[DRY-RUN モード] kabu への実発注・CSV更新はしません")
        print("実際に動かすには --execute を付けてください\n")

    # ctx の初期化: ファイルから読む → なければダミー値
    dummy_price = 3000.0
    saved_ctx = _load_ctx(args.symbol)
    if saved_ctx:
        ctx = saved_ctx
        if 1 not in targets:
            print(f"  前回の S1 価格を読み込みました: {ctx['price']:,.1f}円 ({CTX_FILE})")
    else:
        ctx = {
            "symbol":   args.symbol,
            "price":    dummy_price,
            "order_p":  round(dummy_price * 1.01),
            "stop_p":   round(dummy_price * 0.94),
            "target_p": round(dummy_price * 1.09),
        }
        if 1 not in targets:
            print(f"  [注意] {CTX_FILE} がありません。先に --scenario 1 --execute を実行してください")
            print(f"  仮価格 {dummy_price:,.0f}円 を使います（S6/S7/S8 は kabu から実価格を取得）")

    for n in targets:
        title, fn, needs_kabu = SCENARIOS[n]

        if n == 1:
            try:
                if args.execute:
                    ctx = fn(args.symbol, args.execute)
                else:
                    print(f"\n[dry-run] S1: kabu 未接続のためダミー価格を使います")
                    print(f"  仮価格: {ctx['price']:,} → 逆指値={ctx['order_p']:,} "
                          f"損切={ctx['stop_p']:,} 利確={ctx['target_p']:,}")
            except Exception as e:
                print(f"  ✗ S1 失敗: {e}")
                sys.exit(1)
        elif n == 3:
            try:
                fn(ctx, args.execute, force_holding=args.force_holding)
            except Exception as e:
                print(f"\n  ✗ S{n} 失敗: {e}")
                import traceback
                traceback.print_exc()
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
