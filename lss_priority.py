"""lss_priority.py — lss の『発注優先順位』と『戦略別BT下限』の唯一の定義。

なぜ必要か
-----------
現行の発注は **BTスコアの降順** で予算を埋める。しかし BTスコアは銘柄の質の目安に
過ぎず、**同じBT帯でも戦略によって1件あたり期待値が桁違い**であることが実測で判明した
(compare_lss_rules --bt-min 40 --bt-min-per-strategy, delay2, 240日, BT40+/戦略別):

    RSI2  BT20-39   +1,876円/件   ← 発注していなかった
    DON   BT60-79     +627円/件   ← BT降順だと先に発注される
    MOM   BT20-39  -14,529円/件   ← 絶対に入れてはいけない

つまり一律のBT閾値は「MOMの破滅層を避けるためにRSI2の優良層を道連れにする」
トレードオフになっている。戦略ごとに下限を変え、期待値の降順で発注すれば両方取れる。

このモジュールが持つもの
------------------------
1. BT_MIN_BY_STRATEGY : 戦略別のBT下限(未定義の戦略は既定値)
2. PRIORITY           : (戦略, BT下限, BT上限, 1件あたり期待値) の優先順位表
3. bt_floor()         : ある戦略のBT下限
4. is_orderable()     : (戦略, BT) が発注対象か
5. priority_key()     : sorted() に渡すキー。小さいほど優先

★ 既定は「無効」。環境変数で opt-in する(既存の挙動を変えないため):
    LSS_PRIORITY=1                  … 期待値順の発注 + 戦略別BT下限を有効化
    LSS_BT_MIN_PER_STRATEGY="A7=20,RSI2=20,VOLTF=20,MACDTF=20"  … 下限を上書き
    LSS_PRIORITY_TABLE="RSI2:20-99=1731,..."                     … 優先順位表を上書き

⛔ 検証結果: 不採用 (2026-08-06)。既定OFFのまま使わないこと。CLAUDE.md §18.10
------------------------------------------------------------------------
順位表を直近120日を除外して作り(--holdout-days 120)、2026-05以降で検証したところ、
**5倍率すべてで従来(一律BT40・BT降順)に負けた**:

    倍率      A従来        B①のみ      C①+②
    x1.0   +1,691,277   +1,680,675   +1,466,040
    x2.0   +2,180,127   +2,111,519   +2,009,717   ← 運用点
    x3.0   +2,294,579   +2,166,114   +2,158,808

TRAIN では B/C が A を上回るのに OOS で逆転する = 過剰適合。

★ なぜ compare_lss_rules では +65.5% に見えたのか
   あちらは**資金制約なしで全トレードを数える**。BT20-39 の層は単体ではプラスだが、
   予算が有限だと**それを追加することでより良いトレードが押し出される**。
   単体の期待値がプラスでも機会費用を含めるとマイナスだった。
   → 発注ルールの変更は必ず sim_portfolio_lss.py(予算・上限キャンセル込み)で検証すること。

このモジュールは再検証用に残してあるだけで、LSS_PRIORITY を設定しない限り
一切影響しない。下の期待値も棄却された表なので、そのまま信用しないこと。
"""
from __future__ import annotations

import os

# ─────────────────────────────────────────────────────────────
# 戦略別 BT下限
#   実測(delay2, 240日, BT帯×戦略の1件あたり期待値):
#     BT20-39: RSI2 +1,876 / VOLTF +1,518 / A7 +1,002 / MACDTF +678
#              DON -1 / MOM -14,529
#   → A7/RSI2/VOLTF/MACDTF は BT20 まで下げてよい。MOM/DON は BT40 を死守。
#   MOM の BT20-39 が -14,529円/件 なのは「低品質シグナルに損切り遅延(delay2)を
#   掛けると、だまし下げ→持続反発で無保護窓に走り上がられる」ため(CLAUDE.md §18.9)。
#   delay2 を使う限り、この2戦略の閾値を下げてはいけない。
# ─────────────────────────────────────────────────────────────
DEFAULT_BT_MIN = 40.0

BT_MIN_BY_STRATEGY: dict[str, float] = {
    "A7": 20.0,
    "RSI2": 20.0,
    "VOLTF": 20.0,
    "MACDTF": 20.0,
    "MOM": 40.0,
    "DON": 40.0,
}

# ─────────────────────────────────────────────────────────────
# 優先順位表 (戦略, BT下限, BT上限, 1件あたり期待値[円])
#
# BT帯を分けるか統合するかは「帯ごとの差が単調か」で決めた:
#   A7     +2,201 / +1,829 / +1,002  → 単調 → 分ける
#   VOLTF  +2,115 / +1,518           → 単調 → 分ける
#   MACDTF +1,599 /   +858 /   +678  → 単調 → 分ける
#   DON      +627 /   +429           → 単調 → 分ける
#   RSI2   +1,886 / +1,401 / +1,876  → 非単調(BTに識別力なし) → BT20以上を統合
#   MOM    +1,119 / +1,134           → ほぼ同値              → BT40以上を統合
# 統合したほうがサンプルが増えて順位が安定する。
# ─────────────────────────────────────────────────────────────
PRIORITY: list[tuple[str, float, float, float]] = [
    ("A7",     60, 100, 2201),
    ("VOLTF",  40,  60, 2115),
    ("A7",     40,  60, 1829),
    ("RSI2",   20, 100, 1731),   # BT20以上を統合(帯で差がつかない)
    ("MACDTF", 60, 100, 1599),
    ("VOLTF",  20,  40, 1518),
    ("MOM",    40, 100, 1131),   # BT40以上を統合(帯で差がつかない)
    ("A7",     20,  40, 1002),
    ("MACDTF", 40,  60,  858),
    ("MACDTF", 20,  40,  678),
    ("DON",    60, 100,  611),
    ("DON",    40,  60,  429),
]


def _env_flag(name: str) -> bool:
    v = str(os.environ.get(name, "") or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


def enabled() -> bool:
    """戦略別BT下限を使うか。既定 False(=従来の一律閾値)。"""
    return _env_flag("LSS_PRIORITY")


def order_enabled() -> bool:
    """発注順を『期待値降順』にするか。

    LSS_PRIORITY=floors(または floor / bt) のときは **下限だけ有効・並びは従来のBT降順**。
    ①戦略別BT下限 と ②期待値順 を切り分けて検証するために分けてある:
      ① は240日/120日/base/delay2 のどの切り口でも一貫していて根拠が強い
      ② は期間で順位が動くので、単独で効果を測る必要がある
    """
    if not enabled():
        return False
    v = str(os.environ.get("LSS_PRIORITY", "") or "").strip().lower()
    return v not in ("floors", "floor", "bt", "btdesc")


def _parse_bt_min_env() -> dict[str, float]:
    out = dict(BT_MIN_BY_STRATEGY)
    raw = str(os.environ.get("LSS_BT_MIN_PER_STRATEGY", "") or "").strip()
    if not raw:
        return out
    for kv in raw.split(","):
        kv = kv.strip()
        if not kv or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        try:
            out[k.strip().upper()] = float(v)
        except ValueError:
            continue
    return out


_BT_MIN_RESOLVED: dict[str, float] | None = None


def bt_floor(strategy: str, default: float | None = None) -> float:
    """その戦略のBT下限。enabled() が False なら default(=従来の一律閾値)を返す。"""
    d = DEFAULT_BT_MIN if default is None else float(default)
    if not enabled():
        return d
    global _BT_MIN_RESOLVED
    if _BT_MIN_RESOLVED is None:
        _BT_MIN_RESOLVED = _parse_bt_min_env()
    return _BT_MIN_RESOLVED.get(str(strategy).upper(), d)


def is_orderable(strategy: str, bt: float, default_min: float | None = None) -> bool:
    """(戦略, BTスコア) が発注対象か。"""
    return float(bt or 0) >= bt_floor(strategy, default_min)


def _parse_priority_env() -> list[tuple[str, float, float, float]] | None:
    """LSS_PRIORITY_TABLE="RSI2:20-99=1731,A7:60-100=2201" 形式で上書き。"""
    raw = str(os.environ.get("LSS_PRIORITY_TABLE", "") or "").strip()
    if not raw:
        return None
    out: list[tuple[str, float, float, float]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item or "=" not in item:
            continue
        try:
            head, exp = item.split("=", 1)
            st, band = head.split(":", 1)
            lo, hi = band.split("-", 1)
            out.append((st.strip().upper(), float(lo), float(hi), float(exp)))
        except ValueError:
            continue
    return out or None


_PRIORITY_RESOLVED: list[tuple[str, float, float, float]] | None = None


def _table() -> list[tuple[str, float, float, float]]:
    global _PRIORITY_RESOLVED
    if _PRIORITY_RESOLVED is None:
        _PRIORITY_RESOLVED = _parse_priority_env() or PRIORITY
    return _PRIORITY_RESOLVED


def expectancy(strategy: str, bt: float) -> float:
    """(戦略, BT) の1件あたり期待値[円]。表に無ければ 0。"""
    st = str(strategy).upper()
    b = float(bt or 0)
    for s, lo, hi, exp in _table():
        if s == st and lo <= b < hi:
            return exp
    return 0.0


def priority_key(strategy: str, bt: float):
    """sorted() 用のキー。**小さいほど先に発注**。

    第1キー = -期待値(高いほど先)、第2キー = -BT(同じ層の中では高BT優先)。
    enabled() が False のときは従来どおり BT降順のみ。
    表に無い(戦略,BT)は期待値0扱いで最後尾に回る(切るのではなく後回し)。
    """
    b = float(bt or 0)
    if not order_enabled():
        return (0.0, -b)
    return (-expectancy(strategy, b), -b)


def describe() -> str:
    """現在の設定を1行で(ログ用)。"""
    if not enabled():
        return ("lss発注順: BT降順(従来) / 戦略別BT下限: 無効 "
                "[LSS_PRIORITY=1 で両方有効 / LSS_PRIORITY=floors で下限だけ有効]")
    bm = _BT_MIN_RESOLVED if _BT_MIN_RESOLVED is not None else _parse_bt_min_env()
    _ord = "期待値降順(戦略×BT帯)" if order_enabled() else "BT降順(従来・下限のみ変更)"
    return (f"lss発注順: {_ord} / 戦略別BT下限: "
            + ", ".join(f"{k}={v:.0f}" for k, v in sorted(bm.items())))
