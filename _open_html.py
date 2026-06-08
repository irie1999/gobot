"""共通ユーティリティ: HTML ファイルをブラウザで開く"""
from __future__ import annotations
import subprocess
import sys
import webbrowser
from pathlib import Path


def open_html(path: str | Path) -> None:
    """HTML ファイルを Edge (Windows) またはデフォルトブラウザで開く。"""
    p = Path(path).resolve()
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "start", "msedge", str(p)], check=True)
        else:
            opened = webbrowser.open(p.as_uri())
            if not opened:
                raise webbrowser.Error("open() returned False")
    except Exception:
        print(f"\n⚠️  ブラウザを自動で開けませんでした。")
        print(f"   以下のファイルをブラウザで開いてください:")
        print(f"   {p}")
