"""
apply_watchlist.py  ―  提案 WATCHLIST を自動適用
==================================================
build_watchlist.py が生成した watchlist_proposal_*.py の
STOP_WATCHLIST / BRK_WATCHLIST を
check_signals_stop.py / check_signals_breakout.py の
WATCHLIST に自動で差し替える。

バックアップは自動作成 (*.py.bak)。

【使い方】
  python apply_watchlist.py                         # 最新提案を自動検出 (conservative)
  python apply_watchlist.py --aggressive            # aggressive 版
  python apply_watchlist.py --proposal watchlist_proposal_2026-06-09.py
  python apply_watchlist.py --dry-run               # 変更内容を表示するだけ (書き込みなし)
  python apply_watchlist.py --no-backup             # バックアップなし
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()


# ─── 提案ファイルからブロックを抽出 ──────────────────────────────────────────
def _extract_block(text: str, var_name: str) -> str | None:
    """テキストから `var_name = [...]` ブロックを抽出する。"""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(var_name)}\s*[:=]", line):
            start = i
            break
    if start is None:
        return None

    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip() == "]":
            end = i
            break
    if end is None:
        return None

    return "".join(lines[start : end + 1])


# ─── ターゲットファイルの WATCHLIST を置き換え ─────────────────────────────
def _replace_watchlist(file_path: Path, new_block: str,
                       dry_run: bool, backup: bool) -> bool:
    """file_path の WATCHLIST = [...] を new_block に置き換える。"""
    if not file_path.exists():
        print(f"  [SKIP] {file_path} が見つかりません")
        return False

    text  = file_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    start = None
    for i, line in enumerate(lines):
        if re.match(r"^WATCHLIST\s*[:=]", line):
            start = i
            break
    if start is None:
        print(f"  [ERROR] {file_path}: WATCHLIST 定義が見つかりません")
        return False

    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip() == "]":
            end = i
            break
    if end is None:
        print(f"  [ERROR] {file_path}: WATCHLIST の閉じ括弧 ] が見つかりません")
        return False

    # 新しいブロックの先頭の変数名を WATCHLIST に統一
    new_block = re.sub(
        r"^(STOP_WATCHLIST|BRK_WATCHLIST|SHORT_WATCHLIST|SHORT_BRK_WATCHLIST)\s*:",
        "WATCHLIST:",
        new_block,
    )
    if not new_block.endswith("\n"):
        new_block += "\n"

    old_entries = end - start
    new_entries = new_block.count('("')

    print(f"  {file_path.name}")
    print(f"    旧: {old_entries} 行 → 新: {new_block.count(chr(10))} 行  "
          f"({new_entries} 銘柄)")

    if new_entries == 0:
        print(f"    [SKIP] 新 WATCHLIST が空のため既存リストを保持します")
        return False

    if dry_run:
        print("    [DRY-RUN] 実際の書き込みはスキップ")
        return True

    if backup:
        bak = file_path.with_suffix(".py.bak")
        shutil.copy2(file_path, bak)
        print(f"    バックアップ → {bak.name}")

    new_lines = lines[:start] + [new_block] + lines[end + 1:]
    file_path.write_text("".join(new_lines), encoding="utf-8")
    print(f"    ✓ 差し替え完了")
    return True


# ─── 自動検出 ────────────────────────────────────────────────────────────────
def _auto_detect(aggressive: bool) -> Path | None:
    date_pat = re.compile(r"(\d{4}-\d{2}-\d{2})\.py$")
    dated = []
    for p in Path(".").glob("watchlist_proposal_*.py"):
        m = date_pat.search(p.name)
        if m:
            dated.append((m.group(1), p))
    if not dated:
        return None
    dated.sort(key=lambda x: x[0], reverse=True)
    latest_date = dated[0][0]
    same_day = [p for d, p in dated if d == latest_date]
    if aggressive:
        agg = [p for p in same_day if "aggressive" in p.name]
        if agg:
            return agg[0]
    else:
        non_agg = [p for p in same_day if "aggressive" not in p.name]
        if non_agg:
            return non_agg[0]
    return same_day[0]


# ─── メイン ──────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="提案 WATCHLIST を check_signals_*.py に自動適用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python apply_watchlist.py                         # conservative 最新提案を自動検出
  python apply_watchlist.py --aggressive            # aggressive 版
  python apply_watchlist.py --proposal watchlist_proposal_2026-06-09.py
  python apply_watchlist.py --dry-run               # 確認のみ (書き込みなし)
        """,
    )
    parser.add_argument("--proposal",   type=Path, default=None,
                        help="watchlist_proposal_*.py のパス (省略時は最新を自動検出)")
    parser.add_argument("--dry-run",    action="store_true",
                        help="変更内容を表示するだけで実際には書き込まない")
    parser.add_argument("--no-backup",  action="store_true",
                        help="バックアップファイルを作成しない")
    mode_grp = parser.add_mutually_exclusive_group()
    mode_grp.add_argument("--aggressive",   action="store_true")
    mode_grp.add_argument("--conservative", action="store_true")
    args = parser.parse_args()

    # 提案ファイル決定
    if args.proposal is None:
        proposal_path = _auto_detect(args.aggressive)
        if proposal_path is None:
            print("[ERROR] watchlist_proposal_*.py が見つかりません。"
                  " build_watchlist.py を先に実行してください。", file=sys.stderr)
            sys.exit(1)
        print(f"自動検出: {proposal_path}")
    else:
        proposal_path = args.proposal
        if not proposal_path.exists():
            print(f"[ERROR] {proposal_path} が見つかりません", file=sys.stderr)
            sys.exit(1)

    proposal_text = proposal_path.read_text(encoding="utf-8")

    # 適用マッピング: (提案変数名, ターゲットファイル)
    targets = [
        ("STOP_WATCHLIST",      Path("check_signals_stop.py")),
        ("BRK_WATCHLIST",       Path("check_signals_breakout.py")),
        ("SHORT_WATCHLIST",     Path("check_signals_short.py")),
        ("SHORT_BRK_WATCHLIST", Path("check_signals_short_breakout.py")),
    ]

    print("=" * 60)
    print(f"WATCHLIST 自動適用  提案: {proposal_path.name}")
    if args.dry_run:
        print("  ※ DRY-RUN モード: 実際の書き込みは行いません")
    print("=" * 60)

    success = 0
    for var_name, target_file in targets:
        block = _extract_block(proposal_text, var_name)
        if block is None:
            print(f"  [SKIP] {var_name} が提案ファイルに見つかりません "
                  f"({target_file.name} はスキップ)")
            continue
        ok = _replace_watchlist(
            target_file, block,
            dry_run=args.dry_run,
            backup=not args.no_backup,
        )
        if ok:
            success += 1

    print("=" * 60)
    if args.dry_run:
        print(f"DRY-RUN 完了。{success} ファイルが更新対象です。")
        print("実際に適用するには --dry-run を外して再実行してください。")
    else:
        print(f"完了。{success} ファイルを更新しました。")
        print("\n次ステップ:")
        print("  python run_signals.py --days 365   # 新 WATCHLIST で確認")


if __name__ == "__main__":
    main()
