r"""diff_proposals.py — 2つの lss proposal (SELECTED) の差分を出す。

用途: 「7月までのマージ」と「6月までのマージ」でレポートの数字が同じになった、
      というときに **本当に中身が違うのか** を確かめる。

  ・件数が同じ / 差分ゼロ → マージ対象を変えても候補集合が変わっていない
    (その月の proposal が既存の和集合に吸収されている)
  ・差分がある                → 候補は違うので、数字が同じなら別の理由
    (--lss-proposal の指定が効いていない / 差分ペアにトレードが無い 等)

使い方:
  python diff_proposals.py lss_proposal_cumul.py lss_proposal_cumul_to202606.py
  python diff_proposals.py A.py B.py --show 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ap = argparse.ArgumentParser(description="2つの proposal の SELECTED を比較する")
ap.add_argument("a")
ap.add_argument("b")
ap.add_argument("--show", type=int, default=20, help="差分を何件まで表示するか")
args = ap.parse_args()


def _load(path: str) -> list:
    p = Path(path)
    if not p.exists():
        sys.exit(f"[error] {p} が見つかりません")
    ns: dict = {}
    exec(p.read_text(encoding="utf-8"), ns)
    sel = ns.get("SELECTED")
    if sel is None:
        sys.exit(f"[error] {p} に SELECTED がありません")
    return list(sel)


A, B = _load(args.a), _load(args.b)


def _key(t):
    # (コード, 戦略) で比較。名前の表記ゆれを無視する。
    if isinstance(t, (list, tuple)):
        code = str(t[0])
        strat = str(t[2]) if len(t) > 2 else ""
        return (code, strat)
    return (str(t), "")


KA, KB = {_key(t) for t in A}, {_key(t) for t in B}
print(f"  {Path(args.a).name:<40}{len(A):>6}件 (ユニーク {len(KA):,})")
print(f"  {Path(args.b).name:<40}{len(B):>6}件 (ユニーク {len(KB):,})")
only_a, only_b = KA - KB, KB - KA
print(f"\n  共通            {len(KA & KB):>6,}")
print(f"  A のみ          {len(only_a):>6,}")
print(f"  B のみ          {len(only_b):>6,}")

if not only_a and not only_b:
    print("\n  → **中身は完全に同一**。マージ対象を変えても候補集合が変わっていない。")
    print("     追加した月の proposal が既存の和集合にすべて含まれている、ということ。")
    print("     レポートの数字が同じなのは当然で、比較になっていない。")
else:
    for lbl, s in (("A のみ", only_a), ("B のみ", only_b)):
        if not s:
            continue
        print(f"\n  ── {lbl} ({len(s):,}件) 先頭{min(args.show, len(s))}件 ──")
        for code, strat in sorted(s)[:args.show]:
            print(f"    {code:<10}{strat}")
    print("\n  → 候補集合は違う。それでもレポートの数字が同じなら:")
    print("     ① --lss-proposal の指定が効いていない"
          "(実行ログの『[lss] 新選定で上書き: <ファイル名>』を確認)")
    print("     ② 差分ペアが表示窓の中でトレードを出していない")
    print("     ③ 差分ペアが価格フィルタ(1,000〜6,000円)や空売り不可で落ちている")
