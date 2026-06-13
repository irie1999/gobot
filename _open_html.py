"""
_open_html.py  ―  HTML をブラウザで開くヘルパー (逆指値ロングと互換)
==================================================================
"""

import webbrowser
from pathlib import Path


def open_html(path):
    """HTMLファイルをデフォルトブラウザで開く。"""
    p = Path(path).resolve()
    try:
        webbrowser.open(p.as_uri())
        return True
    except Exception as e:
        print(f"[warn] ブラウザ起動失敗: {e}")
        return False
