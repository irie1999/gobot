"""quiet_log.py — ターミナル出力だけを絞る

★ 何のためか (2026-08-26)
────────────────────────────────────────────────────────────────────
`.\\daily` / `.\\dailyfast` の出力が長すぎて、チャットに貼れない。
**出力を減らすだけ**で、計算にも HTML にも一切触らない仕組み。

  ⛔ **print をフィルタするだけ。** 値も分岐も1つも変えない。
     LSS_QUIET=0(既定) なら import しても素通りする。

  set LSS_QUIET=1        … 静かにする(貼れる量になる)
  set LSS_QUIET=0        … 全部出す(既定)

残すもの(どれか1つでも当たれば通す):
  * ⛔ ⚠ ❌ ✅ を含む行           … 判定・警告・エラー
  * [error] [warn] 失敗 Traceback  … 例外まわり
  * 出力した HTML のファイル名
  * `_KEEP_TAGS` の診断タグ         … 毎回 確認したい行
  * 空行(見やすさのため。連続する空行は1つに畳む)

捨てるもの:
  * 進捗(`… 200/1540銘柄` など)
  * `[info]` の説明ブロック
  * その他

最後に「N行 省略しました」と出すので、**隠れたことは必ず分かる**。
"""
from __future__ import annotations

import atexit
import builtins
import os
import re
import sys

_ON = os.environ.get("LSS_QUIET", "").strip().lower() not in ("", "0", "false", "no")

# ── 必ず残すタグ。ここに足せば静音時も出る ────────────────────────
_KEEP_TAGS = (
    "[error]", "[warn]", "[新方式N]", "[BT出所]", "[検算]", "[lss]",
    "[START_DATES]", "[filter]", "[較正]", "[import]", "[export]",
    "[pairs]", "[⏱", "[試行記録]",
)
_KEEP_RE = re.compile(r"⛔|⚠|❌|✅|失敗|Traceback|エラー|\.html\b")
# 進捗行: 先頭が「…」や「  … 200/1540」
_DROP_RE = re.compile(r"^\s*(…|\.\.\.)")

_orig_print = builtins.print
_n_hidden = 0
_last_blank = False


def _keep(line: str) -> bool:
    s = line.strip()
    if not s:
        return True                      # 空行は残す(後で畳む)
    if _DROP_RE.match(line):
        return False
    if _KEEP_RE.search(s):
        return True
    for t in _KEEP_TAGS:
        if t in s:
            return True
    return False


def _quiet_print(*args, **kwargs):
    global _n_hidden, _last_blank
    # ⛔ **画面(stdout)以外は絶対に触らない。**
    #    `print(..., file=fh)` はファイルへの書き出しなので素通しする。
    #    ここを filter すると **データが欠ける**(2026-08-26 の実装で実際に踏んだ)。
    _f = kwargs.get("file")
    if _f is not None and _f is not sys.stdout:
        return _orig_print(*args, **kwargs)
    try:
        _sep = kwargs.get("sep", " ")
        _txt = _sep.join(str(a) for a in args)
    except Exception:
        return _orig_print(*args, **kwargs)
    # 複数行を1回で print することがあるので行ごとに判定する
    _out = []
    for _ln in _txt.split("\n"):
        if _keep(_ln):
            if not _ln.strip():
                if _last_blank:
                    continue             # 連続する空行は1つに畳む
                _last_blank = True
            else:
                _last_blank = False
            _out.append(_ln)
        else:
            _n_hidden += 1
    if not _out:
        return
    return _orig_print("\n".join(_out), **{k: v for k, v in kwargs.items()
                                           if k != "sep"})


def _footer():
    if _n_hidden:
        _orig_print(f"\n[quiet] {_n_hidden:,}行を省略しました"
                    f"（全部見るには set LSS_QUIET=0）", flush=True)


def install() -> bool:
    """静音モードを入れる。入れたら True。**表示以外は何も変えない。**"""
    if not _ON or builtins.print is _quiet_print:
        return False
    builtins.print = _quiet_print
    atexit.register(_footer)
    _orig_print("[quiet] LSS_QUIET=1 — 判定・警告・エラー・出力ファイル名だけ出します"
                "（全部見るには set LSS_QUIET=0）", flush=True)
    return True


# import しただけで効く(既定 OFF なので素通り)
install()
