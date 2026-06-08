"""共通ユーティリティ: HTML ファイルをブラウザで開く"""
from __future__ import annotations
import os
import sys
import webbrowser
from pathlib import Path


def open_html(path: str | Path) -> None:
    """HTML ファイルをブラウザで開く。ブラウザが見つからない場合はパスを表示する。"""
    p = Path(path).resolve()
    try:
        if sys.platform == "win32":
            os.startfile(str(p))
        else:
            opened = webbrowser.open(p.as_uri())
            if not opened:
                raise webbrowser.Error("open() returned False")
    except Exception:
        print(f"\n⚠️  ブラウザを自動で開けませんでした。")
        print(f"   以下のファイルをブラウザで開いてください:")
        print(f"   {p}")
