"""
Telegram ボタン動作テスト
────────────────────────────────────────────────────────────
swing_notify_top10.py のボタン通知・コールバック処理を
yfinance / Telegram API に接続せず単体で検証します。

■ テスト内容:
  1. 買いシグナル通知 → ボタン生成 → BUY_DONE コールバック → ポジション登録
  2. 売りシグナル通知 → ボタン生成 → SELL_DONE コールバック → ポジションクリア
  3. ストップ到達通知 → ボタン生成 → STOP_DONE コールバック → ポジションクリア
  4. コールバックデータ不正時のエラー処理

■ 実行方法:
  python test_notify_buttons.py
  python test_notify_buttons.py -v   # 詳細ログ付き
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np
import pandas as pd

# ── テスト対象モジュールのインポート ──────────────────────────────
import swing_notify_top10 as bot


# ── テスト用ヘルパー ──────────────────────────────────────────────
def make_mock_row(
    close: float = 850.0,
    low: float   = 840.0,
    rsi: float   = 52.0,
    atr: float   = 20.0,
    ema_slow: float  = 820.0,
    ema_fast: float  = 848.0,
    ema_mid: float   = 840.0,
    bb_upper: float  = 920.0,
    bb_lower: float  = 780.0,
    bb_band: float   = 70.0,
    ema_cross_up: bool = False,
    entry_sig: bool    = True,
    exit_sig: bool     = False,
) -> pd.Series:
    """テスト用のローソク足行（pd.Series）を生成する。"""
    return pd.Series({
        "close":        close,
        "low":          low,
        "rsi":          rsi,
        "atr":          atr,
        "ema_slow":     ema_slow,
        "ema_fast":     ema_fast,
        "ema_mid":      ema_mid,
        "bb_upper":     bb_upper,
        "bb_lower":     bb_lower,
        "bb_band":      bb_band,
        "ema_cross_up": ema_cross_up,
        "entry_sig":    entry_sig,
        "exit_sig":     exit_sig,
    })


def make_buy_position(
    symbol: str    = "8604.T",
    price: float   = 850.0,
    qty: int       = 100,
    stop: float    = 810.0,
    entry_dt: str  = "2025-03-01",
) -> dict:
    """テスト用の保有中ポジションを生成する。"""
    return {
        "in_pos":      True,
        "entry_price": price,
        "stop_price":  stop,
        "qty":         qty,
        "entry_dt":    entry_dt,
    }


# ── テストクラス ──────────────────────────────────────────────────
class TestMessageBuilding(unittest.TestCase):
    """メッセージとボタンデータの生成テスト"""

    SYMBOL = "8604.T"
    NAME   = "野村HD"
    TODAY  = "2025-03-21(Fri)"

    def test_buy_message_contains_symbol(self):
        row = make_mock_row(close=850.0, atr=20.0)
        msg, buttons = bot.build_buy_message(
            self.SYMBOL, self.NAME, row, self.TODAY)
        self.assertIn(self.SYMBOL, msg)
        self.assertIn(self.NAME,   msg)
        self.assertIn("850",       msg)

    def test_buy_button_callback_format(self):
        row = make_mock_row(close=850.0, atr=20.0)
        _, buttons = bot.build_buy_message(
            self.SYMBOL, self.NAME, row, self.TODAY)
        self.assertEqual(len(buttons), 1)
        cb_data = buttons[0][0]["callback_data"]
        parts   = cb_data.split(":")
        self.assertEqual(parts[0], "BUY_DONE")
        self.assertEqual(parts[1], self.SYMBOL)
        # close=850, stop=850-20*1.5=820
        self.assertEqual(float(parts[2]), 850.0)
        self.assertEqual(float(parts[4]), 820.0)

    def test_buy_qty_is_multiple_of_lot_size(self):
        row = make_mock_row(close=850.0, atr=20.0)
        _, buttons = bot.build_buy_message(
            self.SYMBOL, self.NAME, row, self.TODAY)
        qty = int(buttons[0][0]["callback_data"].split(":")[3])
        self.assertEqual(qty % bot.LOT_SIZE, 0, "推奨株数はLOT_SIZEの倍数であるべき")

    def test_sell_message_contains_pnl(self):
        row = make_mock_row(close=900.0, exit_sig=True)
        pos = make_buy_position(price=850.0, qty=100)
        msg, buttons = bot.build_sell_message(
            self.SYMBOL, self.NAME, row, pos, self.TODAY)
        self.assertIn("5,000", msg)   # (900-850)*100 = 5000円
        cb_data = buttons[0][0]["callback_data"]
        self.assertTrue(cb_data.startswith("SELL_DONE:"))
        self.assertIn(self.SYMBOL, cb_data)

    def test_stop_message_contains_stop_price(self):
        row = make_mock_row(close=805.0, low=805.0)
        pos = make_buy_position(price=850.0, qty=100, stop=810.0)
        msg, buttons = bot.build_stop_message(
            self.SYMBOL, self.NAME, row, pos, self.TODAY)
        self.assertIn("810", msg)   # ストップ価格
        cb_data = buttons[0][0]["callback_data"]
        self.assertTrue(cb_data.startswith("STOP_DONE:"))

    def test_no_signal_message_waiting(self):
        row = make_mock_row(close=860.0, entry_sig=False, exit_sig=False)
        pos = {"in_pos": False, "entry_price": 0.0, "stop_price": 0.0,
               "qty": 0, "entry_dt": None}
        msg = bot.build_no_signal_message(
            self.SYMBOL, self.NAME, row, pos, self.TODAY)
        self.assertIn("待機中", msg)

    def test_no_signal_message_holding(self):
        row = make_mock_row(close=900.0, entry_sig=False, exit_sig=False)
        pos = make_buy_position(price=850.0, qty=100)
        msg = bot.build_no_signal_message(
            self.SYMBOL, self.NAME, row, pos, self.TODAY)
        self.assertIn("ポジション保持", msg)
        self.assertIn("5,000", msg)   # 含み益


class TestPositionManagement(unittest.TestCase):
    """ポジションファイルの保存・読み込みテスト"""

    def setUp(self):
        self.tmpdir  = tempfile.TemporaryDirectory()
        self.orig_dir = bot.POSITION_DIR
        bot.POSITION_DIR = Path(self.tmpdir.name)

    def tearDown(self):
        bot.POSITION_DIR = self.orig_dir
        self.tmpdir.cleanup()

    def test_load_returns_default_when_no_file(self):
        pos = bot.load_position("9999.T")
        self.assertFalse(pos["in_pos"])
        self.assertEqual(pos["qty"], 0)

    def test_save_and_load_roundtrip(self):
        symbol = "8604.T"
        data = {
            "in_pos":      True,
            "entry_price": 850.0,
            "stop_price":  810.0,
            "qty":         100,
            "entry_dt":    "2025-03-21",
        }
        bot.save_position(symbol, data)
        loaded = bot.load_position(symbol)
        self.assertTrue(loaded["in_pos"])
        self.assertEqual(loaded["entry_price"], 850.0)
        self.assertEqual(loaded["qty"],         100)

    def test_clear_position(self):
        symbol = "8604.T"
        bot.save_position(symbol, make_buy_position())
        bot.clear_position(symbol)
        pos = bot.load_position(symbol)
        self.assertFalse(pos["in_pos"])
        self.assertEqual(pos["qty"], 0)

    def test_position_file_path(self):
        f = bot._pos_file("8604.T")
        self.assertIn("8604_T", f.name)


class TestCallbackHandler(unittest.TestCase):
    """Telegram コールバック処理テスト"""

    def setUp(self):
        self.tmpdir  = tempfile.TemporaryDirectory()
        self.orig_dir = bot.POSITION_DIR
        bot.POSITION_DIR = Path(self.tmpdir.name)

    def tearDown(self):
        bot.POSITION_DIR = self.orig_dir
        self.tmpdir.cleanup()

    def _make_callback(self, data: str) -> dict:
        return {"id": "mock_cq_id_123", "data": data}

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.answer_callback_query")
    def test_buy_done_registers_position(self, mock_answer, mock_send):
        """BUY_DONE コールバックでポジションが正しく登録される"""
        cb   = self._make_callback("BUY_DONE:8604.T:850:100:810")
        bot.handle_callback(cb)

        pos = bot.load_position("8604.T")
        self.assertTrue(pos["in_pos"],            "in_pos が True になるべき")
        self.assertEqual(pos["entry_price"], 850.0, "entry_price が 850 になるべき")
        self.assertEqual(pos["qty"],         100,   "qty が 100 になるべき")
        self.assertEqual(pos["stop_price"],  810.0, "stop_price が 810 になるべき")
        mock_answer.assert_called_once_with("mock_cq_id_123", "✅ ポジション登録完了")
        mock_send.assert_called_once()
        self.assertIn("8604.T", mock_send.call_args[0][0])

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.answer_callback_query")
    def test_sell_done_clears_position(self, mock_answer, mock_send):
        """SELL_DONE コールバックでポジションがクリアされる"""
        bot.save_position("8604.T", make_buy_position())
        cb = self._make_callback("SELL_DONE:8604.T")
        bot.handle_callback(cb)

        pos = bot.load_position("8604.T")
        self.assertFalse(pos["in_pos"], "ポジションがクリアされるべき")
        self.assertEqual(pos["qty"], 0)
        mock_answer.assert_called_once_with("mock_cq_id_123", "✅ ポジションクリア完了")
        mock_send.assert_called_once()
        self.assertIn("売り注文済み", mock_send.call_args[0][0])

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.answer_callback_query")
    def test_stop_done_clears_position(self, mock_answer, mock_send):
        """STOP_DONE コールバックでポジションがクリアされる"""
        bot.save_position("8604.T", make_buy_position())
        cb = self._make_callback("STOP_DONE:8604.T")
        bot.handle_callback(cb)

        pos = bot.load_position("8604.T")
        self.assertFalse(pos["in_pos"], "ポジションがクリアされるべき")
        mock_answer.assert_called_once_with("mock_cq_id_123", "✅ ポジションクリア完了")
        mock_send.assert_called_once()
        self.assertIn("ストップロス", mock_send.call_args[0][0])

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.answer_callback_query")
    def test_unknown_action_does_not_crash(self, mock_answer, mock_send):
        """不正なコールバックデータでクラッシュしない"""
        cb = self._make_callback("UNKNOWN_ACTION:9999.T")
        try:
            bot.handle_callback(cb)
        except Exception as e:
            self.fail(f"不正データで例外が発生: {e}")
        mock_answer.assert_called_once()

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.answer_callback_query")
    def test_buy_done_with_invalid_price_does_not_crash(self, mock_answer, mock_send):
        """BUY_DONE で価格が数値でない場合もクラッシュしない"""
        cb = self._make_callback("BUY_DONE:8604.T:not_a_price:100:810")
        try:
            bot.handle_callback(cb)
        except Exception as e:
            self.fail(f"不正価格で例外が発生: {e}")

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.answer_callback_query")
    def test_buy_done_with_missing_parts_does_not_crash(self, mock_answer, mock_send):
        """BUY_DONE でフィールドが不足している場合もクラッシュしない"""
        cb = self._make_callback("BUY_DONE:8604.T")
        try:
            bot.handle_callback(cb)
        except Exception as e:
            self.fail(f"不足フィールドで例外が発生: {e}")


class TestCheckOne(unittest.TestCase):
    """check_one() の統合テスト（yfinance/Telegram をモック化）"""

    def setUp(self):
        self.tmpdir  = tempfile.TemporaryDirectory()
        self.orig_dir = bot.POSITION_DIR
        bot.POSITION_DIR = Path(self.tmpdir.name)

    def tearDown(self):
        bot.POSITION_DIR = self.orig_dir
        self.tmpdir.cleanup()

    def _make_df(self, rows: list[dict]) -> pd.DataFrame:
        """テスト用 DataFrame を生成（インジケーター計算済みを想定）。"""
        df = pd.DataFrame(rows)
        df.index = pd.date_range("2025-01-01", periods=len(rows), freq="B")
        return df

    def _minimal_row(self, **kwargs) -> dict:
        base = {
            "open": 850, "high": 860, "low": 840, "close": 850,
            "volume": 1000000,
            "ema_fast": 848, "ema_mid": 840, "ema_slow": 820,
            "ema_cross_up": False, "rsi": 52.0,
            "bb_upper": 920, "bb_lower": 780, "bb_band": 70, "atr": 20.0,
            "entry_sig": False, "exit_sig": False,
        }
        base.update(kwargs)
        return base

    @patch("swing_notify_top10.send_telegram_keyboard")
    @patch("swing_notify_top10.fetch_data")
    def test_buy_signal_sends_keyboard(self, mock_fetch, mock_send_kb):
        """エントリーシグナル発生時にキーボード付きメッセージが送信される"""
        row = self._minimal_row(entry_sig=True)
        df  = self._make_df([row] * 60)
        # add_indicators / add_signals が既に計算済みの列を使うようモック
        with patch("swing_notify_top10.add_indicators", return_value=df), \
             patch("swing_notify_top10.add_signals",    return_value=df):
            mock_fetch.return_value = df
            bot.check_one("8604.T", "野村HD")

        mock_send_kb.assert_called_once()
        _, buttons = mock_send_kb.call_args[0]
        self.assertTrue(
            buttons[0][0]["callback_data"].startswith("BUY_DONE:"),
            "買いシグナルは BUY_DONE コールバックを持つべき"
        )

    @patch("swing_notify_top10.send_telegram_keyboard")
    @patch("swing_notify_top10.fetch_data")
    def test_exit_signal_sends_keyboard(self, mock_fetch, mock_send_kb):
        """エグジットシグナル発生時にキーボード付きメッセージが送信される"""
        # ポジション保有中に設定
        bot.save_position("8604.T", make_buy_position(stop=810.0))
        row = self._minimal_row(exit_sig=True, low=850)  # ストップ未到達
        df  = self._make_df([row] * 60)
        with patch("swing_notify_top10.add_indicators", return_value=df), \
             patch("swing_notify_top10.add_signals",    return_value=df):
            mock_fetch.return_value = df
            bot.check_one("8604.T", "野村HD")

        mock_send_kb.assert_called_once()
        _, buttons = mock_send_kb.call_args[0]
        self.assertTrue(
            buttons[0][0]["callback_data"].startswith("SELL_DONE:"),
            "売りシグナルは SELL_DONE コールバックを持つべき"
        )

    @patch("swing_notify_top10.send_telegram_keyboard")
    @patch("swing_notify_top10.fetch_data")
    def test_stop_signal_sends_keyboard(self, mock_fetch, mock_send_kb):
        """ストップ到達時にキーボード付きメッセージが送信される"""
        bot.save_position("8604.T", make_buy_position(stop=810.0))
        row = self._minimal_row(low=800)  # ストップ(810)到達
        df  = self._make_df([row] * 60)
        with patch("swing_notify_top10.add_indicators", return_value=df), \
             patch("swing_notify_top10.add_signals",    return_value=df):
            mock_fetch.return_value = df
            bot.check_one("8604.T", "野村HD")

        mock_send_kb.assert_called_once()
        _, buttons = mock_send_kb.call_args[0]
        self.assertTrue(
            buttons[0][0]["callback_data"].startswith("STOP_DONE:"),
            "ストップ到達は STOP_DONE コールバックを持つべき"
        )

    @patch("swing_notify_top10.send_telegram_keyboard")
    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.fetch_data")
    def test_no_signal_no_message(self, mock_fetch, mock_send, mock_send_kb):
        """シグナルなし + daily_report=False の場合は何も送信しない"""
        row = self._minimal_row(entry_sig=False, exit_sig=False)
        df  = self._make_df([row] * 60)
        with patch("swing_notify_top10.add_indicators", return_value=df), \
             patch("swing_notify_top10.add_signals",    return_value=df):
            mock_fetch.return_value = df
            bot.check_one("8604.T", "野村HD", daily_report=False)

        mock_send_kb.assert_not_called()
        mock_send.assert_not_called()

    @patch("swing_notify_top10.send_telegram")
    @patch("swing_notify_top10.fetch_data")
    def test_daily_report_sends_simple_message(self, mock_fetch, mock_send):
        """daily_report=True ならシグナルなしでも日次レポートを送信する"""
        row = self._minimal_row(entry_sig=False, exit_sig=False)
        df  = self._make_df([row] * 60)
        with patch("swing_notify_top10.add_indicators", return_value=df), \
             patch("swing_notify_top10.add_signals",    return_value=df):
            mock_fetch.return_value = df
            bot.check_one("8604.T", "野村HD", daily_report=True)

        mock_send.assert_called_once()
        self.assertIn("日次レポート", mock_send.call_args[0][0])


# ── シナリオ表示（ビジュアルテスト） ─────────────────────────────
def run_visual_scenario():
    """
    実際のメッセージ・ボタン内容をターミナルに出力するシナリオテスト。
    Telegram に接続せずに通知内容を確認できる。
    """
    import tempfile
    tmpdir = tempfile.TemporaryDirectory()
    orig   = bot.POSITION_DIR
    bot.POSITION_DIR = Path(tmpdir.name)

    SEP  = "=" * 60
    STEP = "─" * 60

    print(f"\n{SEP}")
    print("  ビジュアル シナリオテスト")
    print(f"{SEP}\n")

    # ── シナリオ 1: 買いシグナル ──────────────────────────────────
    print("【シナリオ 1】買いシグナル発生 → ボタン表示 → BUY_DONE コールバック")
    print(STEP)
    row = make_mock_row(close=850.0, atr=20.0, entry_sig=True)
    msg, buttons = bot.build_buy_message("8604.T", "野村HD", row, "2025-03-21(Fri)")
    print(msg)
    print()
    for btn_row in buttons:
        for btn in btn_row:
            print(f"  [ {btn['text']} ]")
            print(f"  → callback_data: {btn['callback_data']}")
    print()
    print("  ▶ ボタンタップをシミュレート (BUY_DONE:8604.T:850:100:820)")
    with patch("swing_notify_top10.answer_callback_query"), \
         patch("swing_notify_top10.send_telegram") as mock_send:
        bot.handle_callback({"id": "cq1", "data": buttons[0][0]["callback_data"]})
        if mock_send.called:
            print(mock_send.call_args[0][0])
    pos = bot.load_position("8604.T")
    print(f"\n  ✅ ポジション確認: in_pos={pos['in_pos']}  "
          f"entry_price={pos['entry_price']}  qty={pos['qty']}  "
          f"stop={pos['stop_price']}")

    # ── シナリオ 2: 売りシグナル ──────────────────────────────────
    print(f"\n{STEP}")
    print("【シナリオ 2】売りシグナル発生 → ボタン表示 → SELL_DONE コールバック")
    print(STEP)
    row  = make_mock_row(close=920.0, exit_sig=True, entry_sig=False)
    pos2 = make_buy_position(price=850.0, qty=100)
    msg, buttons = bot.build_sell_message("8604.T", "野村HD", row, pos2, "2025-04-10(Thu)")
    print(msg)
    print()
    for btn_row in buttons:
        for btn in btn_row:
            print(f"  [ {btn['text']} ]")
            print(f"  → callback_data: {btn['callback_data']}")
    print()
    print("  ▶ ボタンタップをシミュレート (SELL_DONE:8604.T)")
    with patch("swing_notify_top10.answer_callback_query"), \
         patch("swing_notify_top10.send_telegram") as mock_send:
        bot.handle_callback({"id": "cq2", "data": buttons[0][0]["callback_data"]})
        if mock_send.called:
            print(mock_send.call_args[0][0])
    pos = bot.load_position("8604.T")
    print(f"\n  ✅ ポジション確認: in_pos={pos['in_pos']}  qty={pos['qty']}")

    # ── シナリオ 3: ストップ到達 ──────────────────────────────────
    print(f"\n{STEP}")
    print("【シナリオ 3】ストップ到達 → ボタン表示 → STOP_DONE コールバック")
    print(STEP)
    bot.save_position("8604.T", make_buy_position(price=850.0, qty=100, stop=810.0))
    row  = make_mock_row(close=805.0, low=805.0)
    pos3 = bot.load_position("8604.T")
    msg, buttons = bot.build_stop_message("8604.T", "野村HD", row, pos3, "2025-04-15(Tue)")
    print(msg)
    print()
    for btn_row in buttons:
        for btn in btn_row:
            print(f"  [ {btn['text']} ]")
            print(f"  → callback_data: {btn['callback_data']}")
    print()
    print("  ▶ ボタンタップをシミュレート (STOP_DONE:8604.T)")
    with patch("swing_notify_top10.answer_callback_query"), \
         patch("swing_notify_top10.send_telegram") as mock_send:
        bot.handle_callback({"id": "cq3", "data": buttons[0][0]["callback_data"]})
        if mock_send.called:
            print(mock_send.call_args[0][0])
    pos = bot.load_position("8604.T")
    print(f"\n  ✅ ポジション確認: in_pos={pos['in_pos']}  qty={pos['qty']}")

    bot.POSITION_DIR = orig
    tmpdir.cleanup()
    print(f"\n{SEP}")
    print("  ビジュアルシナリオテスト 完了")
    print(SEP)


# ── エントリーポイント ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Telegram ボタン動作テスト")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="unittest の詳細ログを表示")
    parser.add_argument("--visual", action="store_true",
                        help="ビジュアルシナリオテストのみ実行")
    args = parser.parse_args()

    if args.visual:
        run_visual_scenario()
        sys.exit(0)

    # ── ユニットテスト実行 ────────────────────────────────────────
    verbosity = 2 if args.verbose else 1
    loader    = unittest.TestLoader()
    suite     = unittest.TestSuite()
    for cls in [
        TestMessageBuilding,
        TestPositionManagement,
        TestCallbackHandler,
        TestCheckOne,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # ── ビジュアルシナリオ ────────────────────────────────────────
    if result.wasSuccessful():
        run_visual_scenario()

    sys.exit(0 if result.wasSuccessful() else 1)
