#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**1分足**キャッシュの在庫確認（読むだけ・発注しない）。

⛔ `check_minute_data.py` は **5分足の `data/minute_5m` を見る古いツール**で、
   1分足の在庫は分からない（2026-09-15 レビュー指摘①）。こちらを使うこと。

1分足の置き場は 5分足と **解決ロジックも場所も別**（CLAUDE.md §18.6）:
    環境変数 MINUTE_1M_DIR → 無ければ ~/.jquants_cache/minute
    ファイル名は  <4桁コード>0_1m.pkl   (例 7203 → 72030_1m.pkl)

使い方:
    python check_1m_data.py
    python check_1m_data.py --symbols n_signals_20260915.csv   # 今日の候補と突合
    python check_1m_data.py --symbols symbols_listed_prime.py
    python check_1m_data.py --sample 30      # 期間を調べる本数(既定20)
"""
from __future__ import annotations

# ⛔ Windows で `> out.txt` にリダイレクトすると stdout が cp932 になり、
#   ⛔ ⚠ ✅ のような記号で UnicodeEncodeError を出して落ちる。
import console_safe  # noqa: F401

import argparse
import pickle
import re
import sys
from collections import Counter
from pathlib import Path

ap = argparse.ArgumentParser(description="1分足キャッシュの在庫を確認する")
ap.add_argument("--symbols", default="",
                help="この銘柄リストと突合する（.py / .csv / カンマ区切り）")
ap.add_argument("--sample", type=int, default=20,
                help="収録期間を調べる本数（全部開くと重いので既定20）")
ap.add_argument("--list-missing", type=int, default=20,
                help="欠けている銘柄を何件まで並べるか")
a = ap.parse_args()


def _dir1() -> Path | None:
    """1分足のフォルダ。tenkan_sim と同じ解決順。"""
    try:
        from tenkan_sim import find_minute_dirs
        return find_minute_dirs()[1]
    except Exception:
        import os
        env = os.environ.get("MINUTE_1M_DIR", "").strip()
        if env:
            p = Path(env)
            return p if p.exists() else None
        p = Path.home() / ".jquants_cache" / "minute"
        return p if p.exists() else None


D1 = _dir1()
if D1 is None or not D1.exists():
    sys.exit("[error] 1分足のフォルダが見つかりません。\n"
             "  MINUTE_1M_DIR を設定するか、~/.jquants_cache/minute を確認。\n"
             "  取得は  python fetch_1m_all.py")

_files = sorted(D1.glob("*_1m.pkl"))
print(f"■ 1分足キャッシュ  {D1}")
print(f"  ファイル **{len(_files):,}件**")
if not _files:
    sys.exit("[error] *_1m.pkl が1つもありません。python fetch_1m_all.py")


def _code_of(p: Path) -> str:
    """72030_1m.pkl → 7203。J-Quants の5桁(末尾0)を4桁に戻す。"""
    s = p.name.replace("_1m.pkl", "")
    return s[:-1] if (len(s) == 5 and s.endswith("0")) else s


_have = {_code_of(p): p for p in _files}

# ── 収録期間をサンプルで調べる ────────────────────────────────────
#   ⚠ 全ファイルを開くと数分かかるので既定20本。**平均ではなく最古/最新の
#     ばらつき**を見たいので、先頭・中間・末尾から散らして取る。
import random as _rnd                                        # noqa: E402
_rnd.seed(0)
_pick = (_files if len(_files) <= a.sample
         else _rnd.sample(_files, a.sample))
_spans, _rows, _bad = [], [], []
for p in _pick:
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
        if df is None or len(df) == 0:
            _bad.append((p.name, "空"))
            continue
        _rows.append(len(df))
        _spans.append((str(df.index.min())[:10], str(df.index.max())[:10],
                       len(df)))
    except Exception as e:                                    # noqa: BLE001
        _bad.append((p.name, type(e).__name__))

if _spans:
    _lo = sorted(s[0] for s in _spans)
    _hi = sorted(s[1] for s in _spans)
    print(f"\n  収録期間（{len(_spans)}本のサンプル）")
    print(f"    最古   中央 {_lo[len(_lo) // 2]}  / 全体 {_lo[0]} 〜 {_lo[-1]}")
    print(f"    最新   中央 {_hi[len(_hi) // 2]}  / 全体 {_hi[0]} 〜 {_hi[-1]}")
    print(f"    行数   中央 {sorted(_rows)[len(_rows) // 2]:,}行")
    print(f"\n  ⚠ J-Quants の分足アドオンは **全プラン2年ローリング**"
          f"（§18.6）。古い側はこの上限で切れます")
    # ★ 最新日がばらついていたら「取り切れていない銘柄がある」合図
    if _hi[0] != _hi[-1]:
        print(f"  ⚠ **最新日が銘柄でばらついています**（{_hi[0]} 〜 {_hi[-1]}）。"
              f"取得が途中で止まった可能性 → python fetch_1m_all.py")
if _bad:
    print(f"\n  ⛔ 読めないファイル {len(_bad)}件: "
          + " / ".join(f"{n}({w})" for n, w in _bad[:5]))

# ── 銘柄リストと突合 ──────────────────────────────────────────────
if a.symbols:
    _want: list[str] = []
    _src = a.symbols
    if "," in _src and not Path(_src).exists():
        _want = [x.strip() for x in _src.split(",") if x.strip()]
    else:
        _p = Path(_src)
        if not _p.exists():
            sys.exit(f"[error] {_src} がありません")
        _txt = _p.read_text(encoding="utf-8-sig", errors="replace")
        # .py の SELECTED / WATCHLIST / .csv の symbol 列 — どれでも拾えるよう
        # **4桁数字(+.T)** を総なめする。⚠ 日付や金額を拾わないよう境界を厳しく。
        _want = re.findall(r"\b([0-9]{4}[0-9A-Z]?)\.T\b", _txt)
        if not _want:
            _want = re.findall(r"^\s*([0-9]{4})\s*(?:,|$)", _txt, re.M)
    _want = [w.upper().replace(".T", "") for w in _want]
    _want = [w[:-1] if (len(w) == 5 and w.endswith("0")) else w for w in _want]
    _uniq = sorted(set(_want))
    _ok = [w for w in _uniq if w in _have]
    _ng = [w for w in _uniq if w not in _have]
    print(f"\n■ 突合  {_src}")
    print(f"  リスト {len(_uniq):,}銘柄 → **ある {len(_ok):,} / 無い {len(_ng):,}**"
          f"  （{len(_ok) / max(1, len(_uniq)) * 100:.1f}%）")
    if _ng:
        print(f"  ⛔ 無い: " + " / ".join(_ng[:a.list_missing])
              + (f" ほか{len(_ng) - a.list_missing}件"
                 if len(_ng) > a.list_missing else ""))
        print(f"     → python fetch_1m_all.py  で取り直せます")
    else:
        print(f"  ✅ 全部あります")

print(f"\n⚠ ここで分かるのは **ファイルの有無と期間**だけ。"
      f"その日のバーが揃っているかは analyze_fill_1m.py が銘柄日ごとに見ます")
