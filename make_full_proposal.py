"""make_full_proposal.py — 『選定なし』の提案ファイルを作る (スキャン不要・数秒)

何のためか (#8 / CLAUDE.md §18.38):
  いまの銘柄選定は scan_lss_universe.py が **lss式のエントリー**
  (前日終値−1ティックの逆指値 / sm0.1 / delay1) で回した成績から
  (銘柄, 戦略) を選んでいる。ところが実際に評価している K は
  **09:00確認+50bp の指値 / sm0.5 / delay4** で、入り方が違う。

  ではKのルールで選定し直せばよいかというと、それは成立しない。
  K は H の 1/6 しか発火しない(母集団 2,871 vs 17,017)ので、
  「TRAIN に8取引以上」を課すとほぼ何も通らない。2026-07 が150件に
  崩壊したのと同じ失敗になる。1ペア8取引を貯めるには4〜6年かかるが、
  5分足は 2024-07 が最古なので不可能。

  → 先に確かめるべきは **そもそも選定が効いているのか**。
     §18.20 は「選定は何も足していない」、§18.21 はそれを撤回して
     「予算制約下では選定ありが良い」としたまま、as-of で測り直して
     いない。**未決着のまま放置されている。**

  このファイルは「全ペア = 選定なし」を作る。結果次第で:
    * 選定なしが同等以上 → #8 は消滅(lss式で選んでいる不適切さも消える)
    * 選定ありが明確に良い → いまの lss式選定のまま確定
  どちらでも #8 が終わる。

使い方:
  python make_full_proposal.py
  .\\hvar --lss-proposal lss_proposal_full.py

⚠ START_DATES は **出さない**。選定していないのだから
  「いつから使ってよいか」の制限が要らない(選定リークが構造的に無い)。

⛔ 母集団が約3倍(3,054 → 9,000超)になるので、バックテストと as-of BT の
  キャッシュを作り直すぶん、初回のレポート生成は数倍かかる。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from daytrade_data import available_local_symbols

# ⛔ scan_lss_universe を import してはいけない。モジュール先頭で
#    argparse.parse_args() が走るので、このスクリプトの引数を食って落ちる
#    (しかも backtest エンジン一式を読み込むので重い)。同じ定義を持つ。


def _jq_to_yf(code: str) -> str:
    """J-Quants 5桁(pkl名) → yfinance形式。72030 → 7203.T / 既に .T ならそのまま。
    ⚠ scan_lss_universe._jq_to_yf と **同一の定義**。片方だけ直さないこと。"""
    c = code.strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _strategies() -> list[str]:
    """run_signals_holdout_all が集計する6戦略。
    ⚠ scan_lss_universe._strategies と **同一**。"""
    return ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _name_map() -> dict:
    """symbols_listed_prime.py の SYMBOLS から code→name。無ければ空。
    キーの形式(4桁 / .T付き)が版によって違うので両方引けるようにする。"""
    try:
        import symbols_listed_prime as _p
    except Exception:
        return {}
    _m: dict = {}
    for _row in getattr(_p, "SYMBOLS", []):
        try:
            _c, _n = _row[0], _row[1]
        except Exception:
            continue
        _c = str(_c).strip().upper()
        _m[_c] = _n
        _m[_c.removesuffix(".T")] = _n
    return _m

ap = argparse.ArgumentParser(description="選定なし(全ペア)の提案ファイルを作る")
ap.add_argument("--out", type=str, default="lss_proposal_full.py")
ap.add_argument("--base-month", type=str, default="9999-12",
                help="BASE_MONTH。選定していないので意味を持たないが、"
                     "読み込み側が参照するので遠い未来を既定にする")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(デバッグ)")
args = ap.parse_args()

_seen: set = set()
_universe: list = []
for _s in available_local_symbols():
    _yf = _jq_to_yf(_s)
    if _yf not in _seen:
        _seen.add(_yf)
        _universe.append(_yf)
if args.limit > 0:
    _universe = _universe[:args.limit]

if not _universe:
    raise SystemExit("[error] stock_5min に銘柄がありません"
                     "(available_local_symbols が空)。データ場所を確認してください")

_names = _name_map()
_strats = _strategies()

_lines = [
    '"""lss用WATCHLIST — **選定なし(全ペア)**。make_full_proposal.py 生成',
    '',
    '  ⛔ これは選定結果ではない。5分足があるすべての銘柄 × 全戦略を',
    '     そのまま並べただけのファイル。『選定が効いているのか』を',
    '     測るための対照群(#8 / CLAUDE.md §18.38)。',
    '  ⚠ 価格帯(1,000〜6,000円)と空売り可否のフィルタは、読み込む側',
    '     (run_signals_holdout_all)が別途かける。ここでは絞らない。',
    '  ⚠ START_DATES は出さない。選定していないので「いつから使って',
    '     よいか」の制限が構造的に不要。',
    f'  {len(_universe):,}銘柄 × {len(_strats)}戦略 = '
    f'{len(_universe) * len(_strats):,}ペア',
    '"""',
    f'BASE_MONTH = "{args.base_month}"',
    "SELECTED = [",
]
for _sym in _universe:
    _nm = _names.get(_sym.removesuffix(".T"), "") or _names.get(_sym, "") or ""
    for _st in _strats:
        _lines.append(f'    ({_sym!r}, {_nm!r}, {_st!r}),')
_lines.append("]")

_out = Path(args.out)
_out.write_text("\n".join(_lines) + "\n", encoding="utf-8")

print(f"[出力] {_out.resolve()}")
print(f"  {len(_universe):,}銘柄 × {len(_strats)}戦略 = "
      f"{len(_universe) * len(_strats):,}ペア (選定なし)")
print(f"\n次: .\\hvar --lss-proposal {_out.name}")
print("  ⛔ 母集団が約3倍になるので、初回はキャッシュ再構築で数倍かかります。")
print("  ⚠ 読み込みに失敗すると **黙って旧WATCHLIST(265ペア)に落ちます**。")
print("     実行後、ログの『[lss] 新選定で上書き: ... → 価格≤6,000円で Nペア』が")
print("     数千になっているか必ず確認してください"
      "(『提案ファイル読み込み失敗』と出ていたら母集団が別物です)。")
