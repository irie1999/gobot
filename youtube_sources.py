"""
youtube_sources.py  ―  監視する YouTube チャンネル / 検索キーワードの定義
=========================================================================
`youtube_tips.py` はここに書いたソースを毎回巡回する。
**このファイルだけ編集すれば収集対象を増減できる。**

【推奨: 公式RSSで新着検知 (APIキー不要・軽い・仕様変更に強い)】
  {"name": "○○チャンネル", "feed": "UCxxxxxxxxxxxxxxxxxxxxxx", "limit": 5}
  チャンネルIDは チャンネルページのソースか、
  `python yt_transcript.py --list "https://www.youtube.com/@ハンドル/videos"` の
  出力元 (yt-dlp の channel_id) で確認できます。
  YouTube は Push 通知 (PubSubHubbub) も提供しているので、
  将来リアルタイム化する場合もこの feed の URL がそのまま使えます。
  https://developers.google.com/youtube/v3/guides/push_notifications

【SOURCES に書ける URL の形】
  チャンネル(ハンドル)  https://www.youtube.com/@ハンドル名/videos
  チャンネル(ID)        https://www.youtube.com/channel/UCxxxxxxxx/videos
  再生リスト            https://www.youtube.com/playlist?list=PLxxxxxxxx
  1本だけ               https://www.youtube.com/watch?v=VIDEOID

  ※ /videos を付けると「動画」タブ (新しい順)。付けないとショートやライブも混ざる。
  ※ ハンドルは YouTube でチャンネルを開いて URL をコピーすればよい。

【SEARCHES に書ける形】
  yt-dlp の検索記法をそのまま使う。
    "ytsearchdate20:デイトレ 手法"   → 「新しい順」で 20 件
    "ytsearch10:日本株 決算 攻略"    → 「関連度順」で 10 件
  チャンネルを知らないテーマを拾いたいとき用。ノイズが多いので limit は小さめに。

【limit】 1 回の巡回で見る本数。処理済みの動画は video_id で自動スキップされるので、
        毎日回すなら 3〜10 で十分 (多すぎると LLM コストが嵩む)。

【enabled】 False にすると一時停止 (行を消さずに止められる)。
"""

from __future__ import annotations

# ── 定期巡回するチャンネル / 再生リスト ────────────────────────────────
# 初期状態は空。自分がよく見る株チャンネルを追加してください。
# (例をそのまま有効化せず、実在する URL を貼ること)
SOURCES: list[dict] = [
    # {"name": "○○投資チャンネル", "feed": "UCxxxxxxxxxxxxxxxxxxxxxx",
    #  "limit": 5, "enabled": True},                       # ← 公式RSS (推奨)
    # {"name": "○○投資チャンネル", "url": "https://www.youtube.com/@example/videos",
    #  "limit": 5, "enabled": True},                       # ← yt-dlp 経由
    # {"name": "決算まとめ再生リスト",
    #  "url": "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx",
    #  "limit": 10, "enabled": True},
]

# ── キーワード検索で拾うソース (チャンネル横断) ───────────────────────
SEARCHES: list[dict] = [
    # {"name": "デイトレ手法", "url": "ytsearchdate10:デイトレ 手法 コツ",
    #  "limit": 10, "enabled": True},
]


def active_sources() -> list[dict]:
    """enabled な SOURCES + SEARCHES をまとめて返す。"""
    out = []
    for s in (*SOURCES, *SEARCHES):
        if not s.get("enabled", True):
            continue
        if not (s.get("url") or s.get("feed")):
            continue
        out.append({"name": s.get("name") or s.get("url") or s.get("feed"),
                    "url": s.get("url", ""),
                    "feed": s.get("feed", ""),
                    "limit": int(s.get("limit", 5))})
    return out
