"""test_budget_cap.py — 予算上限キャンセル(_budget_sweep)のオフライン検証。

kabu 接続なしで、偽の KabuClient を使い「約定累計が予算到達→残りの未発動lss新規売り
逆指値だけを取り消す」ロジックが正しく効くかを検証する。実発注ゼロで安全。

  python test_budget_cap.py
"""
from cancel_gap_orders import _budget_sweep, ACTIVE_STATES
from kabu_api import SIDE_SELL, SIDE_BUY, CASH_MARGIN_OPEN


class FakeClient:
    """get_orders / cancel_order だけ持つ偽クライアント。"""
    def __init__(self, orders):
        self._orders = orders
        self.cancelled = []          # cancel_order を呼ばれた OrderId

    def get_orders(self):
        return self._orders

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        # 取消成功を模擬。実際は次ループの get_orders で State=5 になる想定。
        return {"Result": 0}


def _order(oid, *, side=SIDE_SELL, cm=CASH_MARGIN_OPEN, state=3, cum=0, sym="7203"):
    return {"ID": oid, "Side": side, "CashMargin": cm,
            "OrderState": state, "CumQty": cum, "Symbol": sym}


# lss として認識される銘柄(placed_orders/ordered_signals 由来を模擬)
LSS_MAP = {"7203": [{"name": "トヨタ"}], "6758": [{"name": "ソニー"}]}


def test_cancels_only_untriggered_lss_sells():
    orders = [
        _order("1", sym="7203"),                       # ○ 未発動lss売り → 取消
        _order("2", sym="9999"),                       # × lss_mapに無い(メインショート/手動) → 保護
        _order("3", sym="6758", cum=100, state=4),     # × 一部約定(CumQty>0) → 残す
        _order("4", side=SIDE_BUY, sym="7203"),        # × 買い注文 → 対象外
        _order("5", sym="7203", state=5),              # × 終了(State5=非active) → 対象外
        _order("6", sym="7203", cm=1),                 # × 現物(信用新規でない) → 対象外
        _order("7", sym="6758", state=1),              # ○ 未発動lss売り(受付) → 取消
    ]
    cli = FakeClient(orders)
    done = {}
    n_target, n_sent = _budget_sweep(cli, LSS_MAP, done, dry=False)
    assert n_target == 2, f"取消対象は2件のはず: {n_target}"
    assert n_sent == 2, f"送信も2件のはず: {n_sent}"
    assert set(cli.cancelled) == {"1", "7"}, f"取消したのは1,7だけのはず: {cli.cancelled}"
    print("PASS: 未発動lss売りのみ取消(メインshort/一部約定/買い/終了/現物は保護)")


def test_dry_run_sends_nothing():
    orders = [_order("1", sym="7203"), _order("7", sym="6758", state=1)]
    cli = FakeClient(orders)
    n_target, n_sent = _budget_sweep(cli, LSS_MAP, {}, dry=True)
    assert n_target == 2, f"対象は数える: {n_target}"
    assert n_sent == 0, f"dry-runは送信0: {n_sent}"
    assert cli.cancelled == [], f"dry-runはcancel_orderを呼ばない: {cli.cancelled}"
    print("PASS: dry-run は対象を表示するが実取消しない")


def test_idempotent_after_gone():
    """一度取り消して次ループで消えた注文は done='gone' で二重取消しない。"""
    orders = [_order("1", sym="7203")]
    cli = FakeClient(orders)
    done = {}
    _budget_sweep(cli, LSS_MAP, done, dry=False)   # 1回目: 取消送信
    assert cli.cancelled == ["1"]
    # 次ループ: 板から消えた(active に無い)状態を模擬
    cli2 = FakeClient([])
    n_target, n_sent = _budget_sweep(cli2, LSS_MAP, done, dry=False)
    assert n_target == 0 and n_sent == 0
    assert cli2.cancelled == [], "消えた注文を再取消しない"
    print("PASS: 取消済みは二重送信しない(done='gone')")


def test_filled_notional_threshold():
    """watcher 側の『約定累計 >= budget-cap で発火』の計算を再現して境界を確認。"""
    def _num(x):
        try:
            return float(x)
        except Exception:
            return 0.0
    def filled_notional(shorts):
        return sum(_num(p.get("avg", 0)) * int(p.get("qty", 0))
                   for p in shorts if _num(p.get("avg", 0)) > 0)
    cap = 4_000_000
    below = [{"avg": 3000, "qty": 100}, {"avg": 3000, "qty": 100}]   # 60万
    assert filled_notional(below) < cap and not (filled_notional(below) >= cap)
    at = [{"avg": 2000, "qty": 100} for _ in range(20)]              # 400万 ちょうど
    assert filled_notional(at) >= cap, "400万ちょうどで発火するはず"
    over = at + [{"avg": 5000, "qty": 100}]                          # 450万
    assert filled_notional(over) >= cap
    print("PASS: 約定累計 >= 予算 で発火(境界=ちょうどで発火)")


if __name__ == "__main__":
    test_cancels_only_untriggered_lss_sells()
    test_dry_run_sends_nothing()
    test_idempotent_after_gone()
    test_filled_notional_threshold()
    print("\n=== すべて成功: 予算上限キャンセルのロジックは設計通り ===")
