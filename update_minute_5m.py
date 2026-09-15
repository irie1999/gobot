"""
update_minute_5m.py ― 5分足(minute_5m)を最新まで更新するラッパー (スイング側から実行)
================================================================================
デイトレ側の daily_fetch_minute_yf.py を自動で見つけて実行し、
data/minute_5m/*.pkl を直近(yfinanceの上限=約60日)まで更新する。
⑭③フェア版などが最新の5分足を使えるようにするため。

スイング(swingtrade)とデイトレ(daytrading)が別フォルダでも、姉妹フォルダを探索して
daily_fetch_minute_yf.py の場所を自動特定する(場所を聞かない)。

使い方:
  python update_minute_5m.py              # minute_5m の既存全銘柄を直近60日で更新
  python update_minute_5m.py --symbols 6141.T 1982.T
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _find_fetcher() -> Path | None:
    """daily_fetch_minute_yf.py を近傍(祖先の子=姉妹フォルダ含む)から探す。"""
    seen = set()

    def _ok(p):
        return p.is_file()

    # 1) スクリプト位置/CWD の直下
    for base in (_HERE, Path.cwd()):
        c = base / "daily_fetch_minute_yf.py"
        if _ok(c):
            return c
    # 2) 祖先をたどり、その子フォルダ(姉妹)も探索
    ancestors = []
    for b in (_HERE, Path.cwd()):
        p = b
        for _ in range(6):
            if p not in ancestors:
                ancestors.append(p)
            if p.parent == p:
                break
            p = p.parent
    try:
        ancestors.append(Path.home())
    except Exception:
        pass
    for a in ancestors:
        c = a / "daily_fetch_minute_yf.py"
        if _ok(c):
            return c
        try:
            for c in a.glob("*/daily_fetch_minute_yf.py"):
                if _ok(c):
                    return c
        except Exception:
            pass
    return None


def main():
    fetcher = _find_fetcher()
    if fetcher is None:
        print("✗ daily_fetch_minute_yf.py が見つかりません。")
        print("  デイトレ側フォルダ(daytrading 等)で直接実行してください:")
        print("    python daily_fetch_minute_yf.py --all")
        sys.exit(1)

    # 引数: 明示指定が無ければ --all (minute_5m の全既存銘柄を更新)
    passthrough = sys.argv[1:]
    if not passthrough:
        passthrough = ["--all"]
    cmd = [sys.executable, str(fetcher)] + passthrough
    print("=" * 66)
    print("5分足(minute_5m)を最新まで更新します")
    print(f"  実行: {' '.join(cmd)}")
    print(f"  作業フォルダ: {fetcher.parent}  (data/minute_5m はここ配下)")
    print("  ※ yfinanceの5分足は約60日が上限。それ以前は遡れません。")
    print("=" * 66)
    # daily_fetch_minute_yf.py は自身の位置基準で data/minute_5m を使うため、
    # そのフォルダを作業ディレクトリにして実行する。
    rc = subprocess.run(cmd, cwd=str(fetcher.parent)).returncode
    if rc == 0:
        print("\n✓ 更新完了。⑭③フェア版などが最新5分足を使えます。")
        print("  (⑭のキャッシュを更新するには run_signals_holdout_all を --recalc-analysis で再実行)")
    else:
        print(f"\n✗ 更新に失敗しました (rc={rc})。")
    sys.exit(rc)


if __name__ == "__main__":
    main()
