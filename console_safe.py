"""console_safe.py — Windows でリダイレクトしても落ちないようにするだけ

★ 何のためか (2026-09-08)
────────────────────────────────────────────────────────────────────
    python kabu_ws.py ... > ws_20260908.txt 2>&1

これで **スクリプトごと落ちた**:

    UnicodeEncodeError: 'cp932' codec can't encode character
                        '\\u26d4' in position 7

画面に出すぶんには通るのに、**ファイルに残そうとした瞬間だけ**落ちる。
理由は Python の stdout のエンコーディングが行き先で変わるから:

  * コンソール    … utf-8(Windows でも Python 3.6 以降)
  * リダイレクト  … **cp932**(ロケール)

日本語は cp932 にあるので通る。落ちるのは cp932 に **無い** 記号:

    ⛔ U+26D4   ⚠ U+26A0   ✅ U+2705   ❌ U+274C   ▶ U+25B6

このリポジトリの出力はこの記号だらけなので、リダイレクトすると
ほぼ確実に落ちる。しかも **落ちる場所は print の位置次第**なので、
「途中まで動いて終わった」ように見える。

⛔ encoding を utf-8 に変えてはいけない
────────────────────────────────────────────────────────────────────
PowerShell は native コマンドの出力を `[Console]::OutputEncoding`
(日本語 Windows では cp932)として読み直してからファイルに書く。
Python 側が UTF-8 で書くと、**日本語が全部 化ける**(`type` で読めない)。
落ちなくなる代わりに中身が読めなくなるので、直したことにならない。

→ **encoding は触らず、errors だけ "replace" にする。**
   cp932 に無い記号が `?` になるだけで、日本語もリダイレクトも通る。

  ⚠ `backtest_*.py` は `encoding="utf-8"` を指定している。あちらは
    画面で読む前提なので問題にならないが、**同じ形を真似しないこと**。

使い方
────────────────────────────────────────────────────────────────────
    import console_safe   # noqa: F401  (import しただけで効く)

⛔ 出力の中身は1文字も変えない。文字化けもしない。
   cp932 に無い記号がリダイレクト先で `?` になるだけ。
"""
from __future__ import annotations

import sys


def install() -> bool:
    """stdout / stderr を「落ちない」設定にする。1つでも変えたら True。"""
    _done = False
    for _fh in (sys.stdout, sys.stderr):
        # ⛔ reconfigure は TextIOWrapper にしか無い。pytest の capture や
        #   StringIO に差し替えられている場合は素通りする。
        if not hasattr(_fh, "reconfigure"):
            continue
        try:
            # encoding を渡さない = 今のエンコーディングを維持する
            _fh.reconfigure(errors="replace")
            _done = True
        except Exception:
            # 差し替え済みストリームなどで失敗しても **絶対に落とさない**。
            # ここで例外を出したら、本来の目的(落ちないこと)と正反対になる。
            pass
    return _done


install()
