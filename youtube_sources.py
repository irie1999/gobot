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
# 検索は yt-dlp を使う (pip install -U yt-dlp)。字幕のダウンロードまで自動化する
# なら実行時に --allow-unofficial を付ける (§16.2)。
SEARCHES: list[dict] = [
    # {"name": "デイトレ手法", "url": "ytsearchdate10:デイトレ 手法 コツ",
    #  "limit": 10, "enabled": True},
]

# ── テーマ別の学習用ソース (ギャップアップ/ギャップダウン) ────────────
# 銘柄の売買見解ではなく「手法・考え方」を集めたいとき用。
# 日本語と英語の両方を回す。英語字幕でも抽出結果は日本語で出る。
#   python youtube_tips.py --theme gap --allow-unofficial --backend cli
#   python youtube_tips.py --report --topic "ギャップ|窓|gap"
THEME_SEARCHES: dict[str, list[dict]] = {
    "gap": [
        # 日本語
        {"name": "ギャップアップ 手法",   "url": "ytsearch12:ギャップアップ 株 手法"},
        {"name": "窓開け 窓埋め",         "url": "ytsearch12:株 窓開け 窓埋め 攻略"},
        {"name": "寄り付き 戦略",         "url": "ytsearch10:寄り付き 戦略 デイトレ 寄り天"},
        {"name": "ギャップダウン 対応",   "url": "ytsearch10:ギャップダウン 株 対応 買い"},
        {"name": "決算 ギャップ",         "url": "ytsearch10:決算 ギャップアップ 翌日 株価"},
        # 英語
        {"name": "gap up strategy",       "url": "ytsearch12:gap up trading strategy stocks"},
        {"name": "gap and go",            "url": "ytsearch12:gap and go strategy day trading"},
        {"name": "gap fill",              "url": "ytsearch10:gap fill trading strategy statistics"},
        {"name": "gap down reversal",     "url": "ytsearch10:gap down reversal trade setup"},
        {"name": "opening range breakout", "url": "ytsearch10:opening range breakout gap stocks"},
        {"name": "overnight gap edge",    "url": "ytsearch8:overnight gap edge backtest"},
    ],
}


def theme_sources(theme: str, limit: int = 0) -> list[dict]:
    """テーマ名 (例 "gap") のソース一覧を返す。未定義なら空。"""
    out = []
    for s in THEME_SEARCHES.get(theme, []):
        out.append({"name": f"[{theme}] {s['name']}", "url": s["url"], "feed": "",
                    "limit": int(limit or s.get("limit", 10))})
    return out


def theme_names() -> list[str]:
    return sorted(THEME_SEARCHES)


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
