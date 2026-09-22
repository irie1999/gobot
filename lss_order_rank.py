r"""lss_order_rank.py — lss の『発注順』を1箇所に集約する。

なぜ必要か (2026-08-08 / CLAUDE.md 18.21)
------------------------------------------
予算400万では1日十数件しか建てられないので、**どの順に埋めるか**が損益を左右する。
先読み(期間最大BTの全日適用)を除いて測り直した結果:

| 発注順 | OOS損益 | 判定 |
|---|---:|---|
| ランダム 6シード | +287,865 〜 +421,735 (平均 +334,830 / σ 50,310) | 基準の帯 |
| **流動性順** | +310,105 (z=-0.49) | **帯の中 = ランダムと同等** |
| **BT降順(旧既定)** | +222,938 (**z=-2.22**) | **帯の外(全6本より下) = 有意に悪い** |

結論は2つ:
  1. **BT降順は有意に悪い。使わない。**
  2. 流動性順にバックテスト上の優位は無い(ランダムと同等)。

**それでも流動性順を既定にする理由**: このシミュは `slip=0` なので、流動性の利点が
そもそも数字に映らない。18.17 の実測では決済滑りの99%が6件に集中し、**全部が
無保護窓明けの成行約定**だった。成行の滑りは板の厚さで決まるので、流動性の高い順に
埋めれば -1,100円/件 が構造的に減る。
→ **バックテストが支持しているのではなく、バックテストが盲目な部分で得をするから選ぶ。**
   バックテスト上はランダムと同等(下振れなし)、実運用では執行コストぶん有利。

切り替え
--------
  set LSS_ORDER_RANK=liquidity   (既定) 売買代金の大きい順。同値は銘柄コード順
  set LSS_ORDER_RANK=bt          旧既定。**有意に悪いと実測済み**。比較用にのみ残す

⛔ 2026-08-12 (ユーザー決定): BTはアルゴリズム(フィルタ・並び順・タイブレーク)から
   完全に外した。既定経路では BT を一切読まない。`bt` モードは過去の測定を
   再現するためだけに残してある。
"""
from __future__ import annotations

import os

_VALID = ("liquidity", "bt")


def mode() -> str:
    """現在の発注順モード。不正値は既定(liquidity)に落とす。"""
    v = str(os.environ.get("LSS_ORDER_RANK", "") or "").strip().lower()
    return v if v in _VALID else "liquidity"


def describe() -> str:
    m = mode()
    if m == "bt":
        return ("lss発注順: **BT降順** ⚠ 18.21 でランダム6本すべてを下回ると実測"
                "(z=-2.22)。比較用。既定に戻すには LSS_ORDER_RANK を未設定に")
    return "lss発注順: 流動性(売買代金)降順 / 同値は銘柄コード順 (BTは使わない)"


def sort_key(bt, liquidity, sym=""):
    """発注順のソートキー(小さいほど先)。

    liquidity が取れていない(0/None)銘柄は最後尾に回す。板の薄さが分からない銘柄を
    上位に入れると、狙いである『滑りの小さい順』が崩れるため。
    """
    _bt = float(bt or 0)
    _liq = float(liquidity or 0)
    if mode() == "bt":
        return (-_bt, -_liq)
    # ⛔ 同値のタイブレークも BT を使わない(2026-08-12 ユーザー決定)。
    #    BTは8回測って8回とも識別力ゼロだったので、アルゴリズムから外す。
    #    再現性のために銘柄コードで決定的に並べる(意味は無いが安定する)。
    return (0 if _liq > 0 else 1, -_liq, str(sym))


def sort_signals(signals: list, bt_key="bt", liq_key="liquidity",
                 sym_key="symbol") -> list:
    """シグナル(dict)のリストを発注順に並べ替えて返す(非破壊)。"""
    return sorted(signals,
                  key=lambda s: sort_key(s.get(bt_key), s.get(liq_key),
                                         s.get(sym_key, "")))


def daily_turnover(df, window: int = 120) -> float:
    """日足から平均日次売買代金(出来高×終値)を出す。取れなければ 0.0。

    nikkei_analysis.py:2510 と同じ定義(直近120日平均)に合わせてある。
    """
    if df is None or getattr(df, "empty", True):
        return 0.0
    try:
        cols = {str(c).lower(): c for c in df.columns}
        v, c = cols.get("volume"), cols.get("close")
        if v is None or c is None:
            return 0.0
        val = float((df[v] * df[c]).tail(window).mean())
        return val if val == val and val > 0 else 0.0     # NaN ガード
    except Exception:
        return 0.0
