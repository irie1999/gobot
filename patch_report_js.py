"""patch_report_js.py — 既存レポートHTMLのUI JS(月開閉トグル・日別詳細トグル)を
最新版に差し替える。バックテストを回さず、JSだけ数秒で更新する。

--force での全再生成(バックテスト再計算)を待たずに、表示(JS)の修正だけを既存の
HTMLへ即反映して検証したいとき用のデバッグツール。差し替える JS は
nikkei_analysis.py の現行定義をそのまま使う(=二重管理しない・常に最新)。

使い方:
  python patch_report_js.py                         # signals_holdout_all*.html を全部パッチ
  python patch_report_js.py signals_holdout_all_lss_2026-07-17.html
  python patch_report_js.py *.html
→ 実行後、ブラウザで Ctrl+Shift+R すると反映されます。
"""
from __future__ import annotations

import glob
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent

# nikkei_analysis.py 内の差し替え対象ブロックの先頭/末尾アンカー(f-string の {{ }} のまま)
_SRC_START = "function toggleMG(hdr) {{"
_SRC_END = "function showEntryDateY(btn, dk) {{ _showEntryDateGrid(btn, dk); }}"

# 生成済み HTML 内の旧ブロック: toggleMG( 〜 showEntryDateY(...) {...} までを丸ごと置換。
# 途中の関数(旧/新問わず)は非貪欲でまとめて掴み、末尾は showEntryDateY のラッパー本体。
_HTML_PATTERN = re.compile(
    r"function toggleMG\([\s\S]*?function showEntryDateY\([^)]*\)\s*\{[^{}]*\}"
)


def _extract_current_js() -> str:
    """nikkei_analysis.py から現行の UI JS ブロックを取り出し、f-string の {{}} を解除。"""
    src = (BASE / "nikkei_analysis.py").read_text(encoding="utf-8")
    i = src.find(_SRC_START)
    j = src.find(_SRC_END)
    if i < 0 or j < 0:
        raise RuntimeError("nikkei_analysis.py から UI JS ブロックを特定できません"
                           "(アンカーが変わった可能性)。")
    block = src[i:j + len(_SRC_END)]
    return block.replace("{{", "{").replace("}}", "}")


def patch_file(p: Path, new_js: str) -> str:
    html = p.read_text(encoding="utf-8")
    if "function toggleMG(" not in html:
        return "スキップ(トグルJSなし=統合ページ等)"
    new_html, n = _HTML_PATTERN.subn(lambda _m: new_js, html)
    if n == 0:
        return "対象なし(HTML構造が想定外)"
    if new_html == html:
        return "既に最新"
    p.write_text(new_html, encoding="utf-8")
    return f"パッチ済み({n}箇所)"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--all"]
    patch_all = "--all" in sys.argv[1:]
    if args:
        files: list[Path] = []
        for a in args:
            files += [Path(x) for x in glob.glob(a)] or [Path(a)]
    elif patch_all:
        # 過去分も含めて全部(既定では今日のみ。ノイズ回避)
        files = [Path(f) for f in sorted(glob.glob(str(BASE / "signals_holdout_all*.html")))]
    else:
        # 既定: 今日の日付を含むレポートだけ(あなたが今見るのはこれ)
        _today = datetime.now().strftime("%Y-%m-%d")
        files = [Path(f) for f in sorted(glob.glob(str(BASE / f"signals_holdout_all*{_today}*.html")))]
        if not files:
            print(f"今日({_today})のレポートが見つかりません。ファイル名を直接指定するか "
                  "--all で全期間を対象にできます。")
            return 1
    try:
        new_js = _extract_current_js()
    except Exception as e:
        print(f"[X] JS抽出失敗: {e}")
        return 1
    print(f"最新UI JS({len(new_js)}字)を {len(files)}ファイルに適用します。")
    print("-" * 60)
    _npatched = 0
    for p in files:
        if not p.exists():
            print(f"{p.name}: ファイルなし")
            continue
        _r = patch_file(p, new_js)
        if _r.startswith("パッチ済み"):
            _npatched += 1
        print(f"{p.name}: {_r}")
    print("-" * 60)
    print(f"パッチ済み {_npatched} ファイル。→ ブラウザで Ctrl+Shift+R で反映(再生成・バックテスト不要)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
