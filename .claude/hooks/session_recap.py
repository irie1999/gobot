#!/usr/bin/env python3
"""
session_recap.py — SessionStart フックで「前回までに何を話したか」を表示する
=============================================================================

毎セッション開始時に、直近の会話（実際のユーザー発言）と最近の git コミットを
抽出して additionalContext として注入する。コンテキスト圧縮(auto-compact)で
チャット履歴が要約されても、「直近で何を話したか」を毎回確認できる。

安全設計:
  - 何が起きても session 開始を止めない (全例外を握りつぶして exit 0)
  - 失敗時は additionalContext を出さずに静かに終了
"""

import json
import os
import re
import sys
import glob
import subprocess
from pathlib import Path

N_MESSAGES = 12   # 表示する直近ユーザー発言の数
N_COMMITS  = 5    # 表示する直近コミット数

# system-reminder / ローカルコマンド / コマンドタグを除去するための正規表現
_RE_SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_RE_LOCAL  = re.compile(r"<local-command-[^>]*>.*?</local-command-[^>]*>", re.S)
_RE_CMD    = re.compile(r"<command-[^>]*>.*?</command-[^>]*>", re.S)

# これで始まる発言はシステム由来のノイズなので除外
_SKIP_PREFIX = (
    "This session is being continued",
    "[Request interrupted",
    "Base directory for this skill",
    "Caveat:",
    "<command-name>",
)


def _clean(text: str) -> str:
    text = _RE_SYSREM.sub("", text)
    text = _RE_LOCAL.sub("", text)
    text = _RE_CMD.sub("", text)
    return text.strip()


def _extract_user_msgs(jsonl_path: Path) -> list[tuple[str, str]]:
    """1つの transcript から (timestamp, テキスト) のユーザー発言を抽出。"""
    out: list[tuple[str, str]] = []
    try:
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "user":
                    continue
                m = o.get("message", {})
                if m.get("role") != "user":
                    continue
                c = m.get("content")
                if isinstance(c, list):
                    # ツール結果だけのメッセージは会話ではないのでスキップ
                    if all(isinstance(b, dict) and b.get("type") == "tool_result"
                           for b in c):
                        continue
                    text = " ".join(b.get("text", "") for b in c
                                    if isinstance(b, dict) and b.get("type") == "text")
                elif isinstance(c, str):
                    text = c
                else:
                    text = ""
                text = _clean(text)
                if not text or any(text.startswith(s) for s in _SKIP_PREFIX):
                    continue
                ts = (o.get("timestamp") or "")[:16].replace("T", " ")
                out.append((ts, " ".join(text.split())))
    except Exception:
        pass
    return out


def _recent_messages(transcript_dir: Path, current_path: str) -> list[tuple[str, str]]:
    """最近変更された transcript 群から直近のユーザー発言を集める。"""
    files = sorted(transcript_dir.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:4]
    all_msgs: list[tuple[str, str]] = []
    for f in files:
        all_msgs.extend(_extract_user_msgs(f))
    # timestamp 昇順に並べて末尾 N 件
    all_msgs.sort(key=lambda x: x[0])
    # 連続重複を除去
    deduped: list[tuple[str, str]] = []
    for ts, t in all_msgs:
        if deduped and deduped[-1][1] == t:
            continue
        deduped.append((ts, t))
    return deduped[-N_MESSAGES:]


def _recent_commits(project_dir: str) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", project_dir, "log", f"-{N_COMMITS}",
             "--pretty=format:%h %s"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        pass
    return []


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    transcript_path = data.get("transcript_path", "")
    cwd = data.get("cwd") or os.getcwd()

    # transcript ディレクトリを決定 (stdin 優先、無ければ HOME から算出)
    if transcript_path:
        transcript_dir = Path(transcript_path).parent
    else:
        home = os.path.expanduser("~")
        transcript_dir = Path(home) / ".claude" / "projects" / cwd.replace("/", "-")

    msgs = _recent_messages(transcript_dir, transcript_path) \
        if transcript_dir.exists() else []
    commits = _recent_commits(cwd)

    if not msgs and not commits:
        return 0  # 何も無ければ静かに終了

    lines = ["## 📝 前回までの会話メモ（自動表示）",
             "",
             "セッション開始時に、ユーザーへこの要点を簡潔に提示してから本題に入ること。",
             ""]
    if msgs:
        lines.append("### 直近のユーザー発言")
        for ts, t in msgs:
            if len(t) > 100:
                t = t[:100] + "…"
            lines.append(f"- `{ts}` {t}")
        lines.append("")
    if commits:
        lines.append("### 直近のコミット")
        for c in commits:
            lines.append(f"- {c}")
        lines.append("")

    context = "\n".join(lines)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # 絶対にセッション開始を妨げない
