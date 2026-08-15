"""audit_proposals.py — lss_proposal_YYYY-MM.py の件数が月ごとに揃っているか監査する。

なぜ要るか (2026-08-15):
  `lss_proposal_2026-07.py` が **150件**しかない。他の月は 621〜1,012件。
  8月は「初出が7月以前のペア」で動くので、7月の選定が欠けていると
  **いま唯一の純OOS月(8月)の評価が痩せる**。#4〜#9 の測定はすべてこの母集団の
  上で行うので、先に直さないと測り直しになる。

何を見るか:
  scan_lss_universe.py の TRAIN は **「約定日 <= 基準月末」の全取引**なので、
  基準月が進むほど TRAIN は単調に増える。したがって同じ実行(--base-months で
  一括)なら、**後の月ほど合格ペアが増えるのが正常**。減っていたら

    (a) その月だけ別実行(ユニバースが小さい / --limit 付き / 中断)
    (b) --select-top / --max-per-symbol が付いた実行
    (c) 5分足の在庫不足

  のいずれか。生成ファイルの先頭に実行時の引数が刻まれているので、
  ヘッダを並べれば (a)(b) はその場で分かる。

使い方:
  python audit_proposals.py
  python audit_proposals.py --dir "C:\\...\\swingtrade"
"""
from __future__ import annotations

import argparse
import glob
import os
import re
from pathlib import Path

ap = argparse.ArgumentParser(description="lss_proposal の件数・生成条件を監査")
ap.add_argument("--dir", type=str, default=".", help="lss_proposal_*.py があるフォルダ")
args = ap.parse_args()

_files = sorted(glob.glob(str(Path(args.dir) / "lss_proposal_????-??.py")))
if not _files:
    raise SystemExit(f"[error] {Path(args.dir).resolve()} に lss_proposal_YYYY-MM.py がありません")


def _read(path: str) -> dict:
    """ヘッダの実行条件と SELECTED のペア集合を読む(import せず本文を舐める)。"""
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    base = re.search(r"(\d{4}-\d{2})", Path(path).name).group(1)
    # ヘッダ(docstring)は最初の """ ... """
    head = txt.split('"""')[1] if txt.count('"""') >= 2 else ""
    # ("7203", "MACDTF") 形式のペアを拾う
    pairs = set(re.findall(r'\(\s*"([0-9A-Za-z.]+)"\s*,\s*"([A-Za-z0-9]+)"\s*\)', txt))
    # ⛔ ヘッダには基準月・件数・TRAIN終端日が入るので、そのまま比べると
    #    全ファイルが「別条件」になる。**実行パラメータだけ**を残して正規化する。
    _cond = []
    for _l in head.splitlines():
        _l = _l.strip()
        if not _l or _l.startswith("lss専用"):
            continue
        # ⛔ sm/tm/slip/fee は「基準月(TRAIN終端): YYYY-MM / sm=... 」と
        #    **同じ行**にある。行ごと捨てると比較の意味が無くなるので、
        #    月の部分だけを落として残りは残す。
        _l = re.sub(r"基準月\(TRAIN終端\):\s*\d{4}-\d{2}\s*/?\s*", "", _l)
        _l = re.sub(r"合格\s*[\d,]+\s*ペア。?", "", _l)
        _l = re.sub(r"TRAIN\(\d{4}-\d{2}-\d{2}以前\)", "TRAIN(基準月以前)", _l)
        _l = _l.strip(" 。/")
        if _l:
            _cond.append(_l)
    return {
        "base": base,
        "path": path,
        "n": len(pairs),
        "pairs": pairs,
        "syms": {p[0] for p in pairs},
        "head": " / ".join(_cond),
        "mtime": os.path.getmtime(path),
    }


_rows = [_read(f) for f in _files]
_ns = [r["n"] for r in _rows]
_med = sorted(_ns)[len(_ns) // 2]

print("=" * 100)
print(f"■ lss_proposal 監査 — {len(_rows)}ファイル / 件数の中央値 {_med}")
print("=" * 100)
print(f"{'基準月':<9}{'ペア':>7}{'銘柄':>7}{'中央比':>8}  {'前月からの増減':>14}  生成日時")
print("-" * 100)

_prev = None
_bad: list[dict] = []
for r in _rows:
    _ratio = r["n"] / max(1, _med)
    _dn = "" if _prev is None else f"{r['n'] - _prev['n']:+,}"
    # ⛔ TRAIN は基準月が進むほど増えるので、**前月より減っていたら異常**。
    _flag = ""
    if _ratio < 0.5:
        _flag = " ⛔ 中央値の半分未満"
        _bad.append(r)
    elif _prev is not None and r["n"] < _prev["n"] * 0.8:
        _flag = " ⚠ 前月から2割以上 減少"
        _bad.append(r)
    import datetime as _dt
    _ts = _dt.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
    print(f"{r['base']:<9}{r['n']:>7,}{len(r['syms']):>7,}{_ratio:>7.0%}  {_dn:>14}  {_ts}{_flag}")

print("-" * 100)

# ── 生成条件(ヘッダ)を突き合わせる。ここが違えば「別実行」が確定 ──────────────
_heads: dict = {}
for r in _rows:
    _heads.setdefault(r["head"], []).append(r["base"])
_maxn = max(len(v) for v in _heads.values())
print(f"\n■ 生成条件(ファイル先頭の引数。基準月・件数は除いて比較) — {len(_heads)}通り")
for _h, _bs in sorted(_heads.items(), key=lambda kv: -len(kv[1])):
    _mark = "★ 多数派" if len(_bs) == _maxn else "⛔ 少数派"
    print(f"\n  [{_mark}] 基準月 {', '.join(_bs)}")
    for _line in _h.split(" / "):
        print(f"      {_line}")

if len(_heads) > 1:
    print("\n  ⛔ 生成条件が揃っていません。少数派の月は **別実行** で作られています。")
    print("     sm/tm/slip/fee/しきい値 が違えば母集団の意味が変わるので、")
    print("     多数派と同じ引数で作り直してください。")
else:
    print("\n  ✅ 全月おなじ条件で生成されています。")
    print("     → 件数が落ちているなら、原因は引数ではなく **実行そのもの**")
    print("        (中断 / ユニバースが小さい / 5分足の在庫不足)。")

# ── 包含関係。TRAIN は累積なので、後の月は前の月をほぼ含むはず ────────────────
print("\n■ 前月ペアの引き継ぎ率 (TRAIN は累積なので、正常なら高い)")
for _i in range(1, len(_rows)):
    _a, _b = _rows[_i - 1], _rows[_i]
    _keep = len(_a["pairs"] & _b["pairs"]) / max(1, len(_a["pairs"]))
    _mk = " ⛔" if _keep < 0.5 else ("  ⚠" if _keep < 0.75 else "")
    print(f"  {_a['base']} → {_b['base']}: {_keep:>6.0%} "
          f"({len(_a['pairs'] & _b['pairs']):,}/{len(_a['pairs']):,}){_mk}")

if _bad:
    _bl = ",".join(r["base"] for r in _bad)
    print("\n" + "=" * 100)
    print(f"⛔ 作り直しが要る基準月: {_bl}")
    print("=" * 100)
    print("  多数派と同じ引数で、その月だけ作り直します(--base-months はカンマ区切りで一括可):")
    print(f"\n    python scan_lss_universe.py --base-months {_bl} --workers 8\n")
    print("  ⚠ 上の『生成条件』の多数派と同じ sm/tm/slip/しきい値になるよう、")
    print("     足りない引数は多数派の行から写して付けること。")
    print("  ⚠ 作り直したら merge_lss_proposals が読み直すので、次の .\\daily は")
    print("     BTキャッシュ再構築で遅くなります。")
else:
    print("\n✅ 件数・生成条件とも揃っています。作り直しは不要です。")
