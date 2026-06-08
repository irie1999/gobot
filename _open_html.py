"""共通ユーティリティ: HTML ファイルをブラウザで開く"""
from __future__ import annotations
import subprocess
import sys
import webbrowser
from pathlib import Path


def open_html(path: str | Path) -> None:
    """HTML ファイルを Edge (Windows) またはデフォルトブラウザで開く。"""
    s = str(path)
    # file:// URI はそのまま使う。それ以外は Path → URI に変換。
    uri = s if s.startswith("file://") else Path(path).resolve().as_uri()
    try:
        if sys.platform == "win32":
            # URI をそのまま Edge に渡す（Path 変換すると日本語・スペースが二重エンコード）
            subprocess.run(["cmd", "/c", "start", "msedge", uri], check=True)
        else:
            opened = webbrowser.open(uri)
            if not opened:
                raise webbrowser.Error("open() returned False")
    except Exception:
        p = Path(path) if not s.startswith("file://") else Path(s[8:].lstrip("/"))
        print(f"\n⚠️  ブラウザを自動で開けませんでした。")
        print(f"   以下のファイルをブラウザで開いてください:")
        print(f"   {p}")
