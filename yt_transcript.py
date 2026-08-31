"""
yt_transcript.py  ―  YouTube 動画の「字幕(caption)」取得ユーティリティ
=====================================================================
youtube_tips.py から呼ばれる下請けモジュール。単体でも動く。

【役割】
  1) チャンネル / 再生リスト URL から最近の動画 ID を列挙   → list_videos()
  2) 動画 ID から字幕テキスト (手動字幕 > 自動生成字幕) を取得 → fetch_transcript()
  3) VTT / SRT を「タイムスタンプ付きプレーンテキスト」に整形

【字幕の取得経路 (プロバイダ) — 差し替え可能・非公式経路は明示的オプトイン】
  既定は **manual のみ**。yt-dlp / youtube-transcript-api は YouTube 公式の
  字幕APIではなく、壊れやすさと利用条件上のリスクがあるため、
  環境変数 YT_CAPTION_PROVIDERS を設定した場合 (または --allow-unofficial を
  付けた場合) だけ有効になる。

    既定                        : manual
    非公式経路を許可する場合     : export YT_CAPTION_PROVIDERS=manual,ytdlp,api
    自分の動画の公式API経路      : 将来 official プロバイダとして追加予定
                                  (captions.download は自分の動画にのみ使える)

  manual : youtube_tips_data/manual/<video_id>.(txt|vtt|srt) を読む。
           YouTube 画面の「文字起こしを表示」からコピペしたものを置くだけ。
           **第三者動画で最も安全な経路** で、YouTube 側の仕様変更にも強い。
  ytdlp  : yt-dlp で字幕をダウンロード (pip install -U yt-dlp)。
  api    : youtube-transcript-api (入っていれば使う)。

  ※ YouTube 公式 Data API の captions.download は「自分が編集権限を持つ動画」しか
    落とせない (https://developers.google.com/youtube/v3/docs/captions/download)。
    第三者動画を継続的に自動処理したい場合は manual 経路を主にし、
    ytdlp/api は補助という位置づけにするのが安全。
    新着検知だけは公式 RSS / Push 通知が使える (feed_videos / PubSubHubbub)。

【依存】
  yt-dlp は実行ファイル (yt-dlp) でも python モジュール (python -m yt_dlp) でも可。

【単体テスト】
  python yt_transcript.py --list "https://www.youtube.com/@example/videos" --limit 5
  python yt_transcript.py --feed UCxxxxxxxxxxxx        # 公式RSSで新着検知 (APIキー不要)
  python yt_transcript.py --video dQw4w9WgXcQ
  python yt_transcript.py --import dQw4w9WgXcQ --from copied.txt   # 手動文字起こし取込
  python yt_transcript.py --parse-vtt sample.vtt      # パーサだけ確認

【キャッシュ】
  取得した字幕は youtube_tips_data/transcripts/<video_id>.json に保存し、
  2 回目以降は再ダウンロードしない (--no-cache で無効化)。
  YouTube 側の字幕は基本的に不変なので、キャッシュは無期限。
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── 設定 ────────────────────────────────────────────────────────────────
DATA_DIR       = Path(__file__).parent / "youtube_tips_data"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
MANUAL_DIR     = DATA_DIR / "manual"        # 手動で置いた文字起こし
SUB_LANGS      = "ja,ja-JP,ja-orig,en,en-US"   # 優先順 (前から順に採用)
YTDLP_TIMEOUT  = 180                            # 1 動画あたりの上限秒
DEFAULT_PROVIDERS   = "manual"                  # 既定は公式に近い手動経路のみ
UNOFFICIAL_PROVIDERS = "manual,ytdlp,api"       # --allow-unofficial 時の順序
FEED_URL       = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


# ── yt-dlp の起動コマンドを解決 ────────────────────────────────────────
def ytdlp_cmd() -> list[str]:
    """yt-dlp を呼ぶためのコマンド配列を返す。無ければ RuntimeError。"""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        raise RuntimeError(
            "yt-dlp が見つかりません。`pip install -U yt-dlp` を実行してください。")


def _run(cmd: list[str], timeout: int = YTDLP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


# ── 1) 動画一覧 ────────────────────────────────────────────────────────
def list_videos(source_url: str, limit: int = 10) -> list[dict]:
    """
    チャンネル / 再生リスト URL から最新 limit 件の動画メタを返す。

    --flat-playlist を使うので 1 リクエストで済み高速。ただし upload_date は
    取れないことが多い (その場合 None)。日付フィルターは fetch_transcript() で
    取れる info.json 側の upload_date を使うこと。

    戻り値: [{"video_id", "title", "url", "duration", "channel", "upload_date"}, ...]
    """
    cmd = ytdlp_cmd() + [
        "--flat-playlist", "--dump-single-json",
        "--playlist-end", str(limit),
        "--no-warnings", source_url,
    ]
    p = _run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"yt-dlp 一覧取得に失敗: {p.stderr.strip()[:300]}")

    data    = json.loads(p.stdout)
    entries = data.get("entries") or ([data] if data.get("id") else [])
    channel = data.get("channel") or data.get("uploader") or data.get("title") or ""

    out: list[dict] = []
    for e in entries[:limit]:
        if not e or not e.get("id"):
            continue
        vid = e["id"]
        # ショート動画 / ライブ予定は tips 化しづらいので duration で弾ける様に返す
        out.append({
            "video_id":    vid,
            "title":       e.get("title") or "",
            "url":         e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "duration":    e.get("duration"),          # 秒 or None
            "channel":     e.get("channel") or e.get("uploader") or channel,
            "upload_date": e.get("upload_date"),       # "YYYYMMDD" or None
            "live":        e.get("live_status") in ("is_upcoming", "is_live"),
        })
    return out


# ── 2) VTT / SRT パーサ ────────────────────────────────────────────────
_TS_RE   = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
                      r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_TAG_RE  = re.compile(r"<[^>]+>")           # <00:00:01.000><c> などの inline タグ
_NUM_RE  = re.compile(r"^\d+$")             # SRT の連番行


def _sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(raw: str) -> list[dict]:
    """
    VTT / SRT 文字列を [{"start": 秒, "text": "..."}] に変換する。

    YouTube の自動生成字幕は「前のキューの文字列 + 新しい単語」というローリング
    表示になっており、素直に連結すると同じ文が何十回も重複する。
    ここで前後関係を見て重複を潰す (prefix 判定)。
    """
    segs: list[dict] = []
    cur_start: float | None = None
    buf: list[str] = []

    def flush():
        nonlocal buf
        if cur_start is None or not buf:
            buf = []
            return
        for line in buf:
            line = _TAG_RE.sub("", line).strip()
            line = re.sub(r"\s+", " ", line)
            if not line or line == "[音楽]" or line == "[Music]":
                continue
            if segs:
                prev = segs[-1]["text"]
                if line == prev or prev.endswith(line):
                    continue
                if line.startswith(prev):        # ローリング: 後勝ちで置換
                    segs[-1]["text"] = line
                    continue
            segs.append({"start": cur_start, "text": line})
        buf = []

    for line in raw.splitlines():
        st = line.strip()
        m = _TS_RE.search(st)
        if m:
            flush()
            cur_start = _sec(*m.groups()[:4])
            continue
        if not st or st.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        if _NUM_RE.match(st):                    # SRT の連番
            continue
        buf.append(st)
    flush()
    return segs


def segments_to_text(segs: list[dict], stamp_every: int = 30) -> str:
    """
    セグメントを「[mm:ss] 本文」形式のプレーンテキストにする。
    stamp_every 秒ごとにだけタイムスタンプを打つ (LLM に引用時刻を答えさせるため)。
    """
    lines: list[str] = []
    next_stamp = 0.0
    for s in segs:
        if s["start"] >= next_stamp:
            mm, ss = divmod(int(s["start"]), 60)
            hh, mm = divmod(mm, 60)
            tag = f"[{hh}:{mm:02d}:{ss:02d}]" if hh else f"[{mm}:{ss:02d}]"
            lines.append(f"\n{tag} {s['text']}")
            next_stamp = s["start"] + stamp_every
        else:
            lines.append(s["text"])
    return re.sub(r"\n{2,}", "\n", " ".join(lines)).strip()


# ── 3) 字幕取得 ────────────────────────────────────────────────────────
def _pick_sub_file(work: Path, video_id: str) -> tuple[Path | None, str]:
    """ダウンロード済み字幕から SUB_LANGS の優先順で 1 本選ぶ。"""
    files = list(work.glob(f"{video_id}*.vtt")) + list(work.glob(f"{video_id}*.srt"))
    if not files:
        return None, ""
    for lang in SUB_LANGS.split(","):
        for f in files:
            # ファイル名は <id>.<lang>.vtt
            parts = f.name.split(".")
            if len(parts) >= 3 and parts[-2].lower().startswith(lang.lower()):
                return f, parts[-2]
    f = files[0]
    return f, f.name.split(".")[-2] if len(f.name.split(".")) >= 3 else "unknown"


def _fetch_via_ytdlp(video_id: str) -> dict | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        cmd = ytdlp_cmd() + [
            "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", SUB_LANGS,
            "--sub-format", "vtt/srt/best",
            "--write-info-json",
            "--no-warnings",
            "-o", str(work / "%(id)s.%(ext)s"),
            url,
        ]
        p = _run(cmd)
        info_f = work / f"{video_id}.info.json"
        if not info_f.exists():
            raise RuntimeError(f"yt-dlp 取得失敗 ({video_id}): {p.stderr.strip()[:300]}")
        info = json.loads(info_f.read_text(encoding="utf-8"))

        sub_f, lang = _pick_sub_file(work, video_id)
        if sub_f is None:
            return {"meta": _meta(info), "segments": [], "lang": "",
                    "source": "yt-dlp", "error": "字幕なし"}
        segs = parse_vtt(sub_f.read_text(encoding="utf-8", errors="replace"))
        return {"meta": _meta(info), "segments": segs, "lang": lang,
                "source": "yt-dlp", "error": ""}


def _fetch_via_api(video_id: str) -> dict | None:
    """youtube-transcript-api によるフォールバック (インストール時のみ)。"""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        langs = [l.strip() for l in SUB_LANGS.split(",")]
        raw   = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
    except Exception as e:
        return {"meta": {"video_id": video_id}, "segments": [], "lang": "",
                "source": "transcript-api", "error": str(e)[:200]}
    segs = [{"start": float(r["start"]), "text": r["text"].replace("\n", " ").strip()}
            for r in raw if r.get("text", "").strip()]
    return {"meta": {"video_id": video_id,
                     "url": f"https://www.youtube.com/watch?v={video_id}"},
            "segments": segs, "lang": "auto", "source": "transcript-api", "error": ""}


# ── プロバイダ: manual (手動で置いた文字起こし) ────────────────────────
_MANUAL_TS_RE = re.compile(r"^\(?(\d{1,2}:\d{2}(?::\d{2})?)\)?\s*")


def parse_manual(raw: str) -> list[dict]:
    """
    YouTube の「文字起こしを表示」からコピペしたテキストを segments に変換する。

    想定する形 (どれでも可):
      "0:35 一番大事なのは…"      … 時刻と本文が同じ行
      "0:35\n一番大事なのは…"     … 時刻の次の行に本文 (画面コピペはこの形が多い)
      "一番大事なのは…"            … 時刻なし (start は 0 から連番で埋める)
    VTT/SRT を貼った場合は parse_vtt に回す。
    """
    if "-->" in raw:
        return parse_vtt(raw)

    segs: list[dict] = []
    pending: float | None = None
    seq = 0.0
    for line in raw.splitlines():
        st = line.strip()
        if not st:
            continue
        m = _MANUAL_TS_RE.match(st)
        if m:
            t    = _hms(m.group(1))
            body = st[m.end():].strip()
            if body:
                segs.append({"start": t, "text": body})
                pending = None
            else:
                pending = t          # 次の行が本文
            continue
        if pending is not None:
            segs.append({"start": pending, "text": st})
            pending = None
        else:
            segs.append({"start": seq, "text": st})
            seq += 10.0
    return segs


def _hms(ts: str) -> float:
    parts = [int(x) for x in ts.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return float(sec)


def manual_path(video_id: str) -> Path | None:
    for ext in (".txt", ".vtt", ".srt", ".md"):
        f = MANUAL_DIR / f"{video_id}{ext}"
        if f.exists():
            return f
    return None


def import_manual(video_id: str, src: Path) -> Path:
    """外部ファイルを manual ディレクトリに取り込む (拡張子は維持)。"""
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)
    dst = MANUAL_DIR / f"{video_id}{src.suffix if src.suffix in ('.txt', '.vtt', '.srt', '.md') else '.txt'}"
    dst.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return dst


def _fetch_via_manual(video_id: str) -> dict | None:
    f = manual_path(video_id)
    if f is None:
        return None
    segs = parse_manual(f.read_text(encoding="utf-8", errors="replace"))
    meta = {"video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}"}
    side = MANUAL_DIR / f"{video_id}.json"       # 任意: タイトル等を補える
    if side.exists():
        try:
            meta.update(json.loads(side.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return {"meta": meta, "segments": segs, "lang": "manual",
            "source": "manual", "error": "" if segs else "手動ファイルが空"}


# ── 新着検知: 公式 RSS フィード (APIキー不要) ─────────────────────────
def feed_videos(channel: str, limit: int = 15) -> list[dict]:
    """
    チャンネルの公式 RSS (feeds/videos.xml) から新着動画を取る。
    channel には UCxxxx 形式のチャンネルID か、その URL を渡す。
    公式フィードなので軽く、Push 通知 (PubSubHubbub) に載せ替えることもできる。
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    m   = re.search(r"(UC[\w-]{20,})", channel)
    cid = m.group(1) if m else channel
    with urllib.request.urlopen(FEED_URL.format(cid=cid), timeout=30) as r:
        xml = r.read().decode("utf-8", "replace")

    ns  = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    root = ET.fromstring(xml)
    ch_name = (root.findtext("a:title", "", ns) or "").strip()
    for e in root.findall("a:entry", ns)[:limit]:
        vid = e.findtext("yt:videoId", "", ns)
        if not vid:
            continue
        pub_iso = (e.findtext("a:published", "", ns) or "")
        out.append({"video_id": vid, "title": e.findtext("a:title", "", ns) or "",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "duration": None, "channel": ch_name,
                    "upload_date": pub_iso[:10].replace("-", ""),
                    "published_at": pub_iso,      # 時刻まで入る (公式RSS の利点)
                    "live": False})
    return out


def _published_at(info: dict) -> str:
    """
    公開時刻を ISO8601 (JST) で返す。yt-dlp の timestamp があれば時刻まで取れる。
    tips_track が「公開後に実際に買える最初の価格」を決めるのに使う。
    """
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        try:
            from datetime import datetime, timedelta, timezone
            jst = timezone(timedelta(hours=9))
            return datetime.fromtimestamp(int(ts), jst).isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            pass
    return ""


def _meta(info: dict) -> dict:
    return {
        "video_id":    info.get("id", ""),
        "title":       info.get("title", ""),
        "channel":     info.get("channel") or info.get("uploader") or "",
        "channel_id":  info.get("channel_id", ""),
        "upload_date": info.get("upload_date", ""),          # YYYYMMDD
        "published_at": _published_at(info),                  # ISO8601 (JST) 取れれば
        "duration":    info.get("duration") or 0,            # 秒
        "view_count":  info.get("view_count") or 0,
        "url":         info.get("webpage_url")
                       or f"https://www.youtube.com/watch?v={info.get('id','')}",
        "description": (info.get("description") or "")[:2000],
    }


def providers() -> list[str]:
    """
    字幕取得の優先順。既定は manual のみ。
    非公式経路 (yt-dlp / youtube-transcript-api) は環境変数
    YT_CAPTION_PROVIDERS を明示設定した場合だけ有効になる。
    """
    raw = os.environ.get("YT_CAPTION_PROVIDERS", DEFAULT_PROVIDERS)
    return [p.strip() for p in raw.split(",") if p.strip()]


def allow_unofficial() -> None:
    """非公式プロバイダを有効化する (--allow-unofficial から呼ぶ)。"""
    os.environ.setdefault("YT_CAPTION_PROVIDERS", UNOFFICIAL_PROVIDERS)
    if "ytdlp" not in providers():
        os.environ["YT_CAPTION_PROVIDERS"] = UNOFFICIAL_PROVIDERS


_PROVIDER_FN = {
    "manual": _fetch_via_manual,
    "ytdlp":  _fetch_via_ytdlp,
    "api":    _fetch_via_api,
}


def fetch_transcript(video_id: str, use_cache: bool = True) -> dict:
    """
    動画 1 本の字幕 + メタ情報を返す。プロバイダを順に試し、最初に成功したものを使う。

    戻り値:
      {"meta": {...}, "segments": [{"start","text"}...], "text": "タイムスタンプ付き本文",
       "lang": "ja", "source": "manual|yt-dlp|transcript-api|cache", "error": ""}

    字幕が取れない動画は segments=[] / error にメッセージを入れて返す (例外にしない)。
    日次バッチを 1 本の失敗で止めないため。
    """
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = TRANSCRIPT_DIR / f"{video_id}.json"
    if use_cache and cache_f.exists():
        d = json.loads(cache_f.read_text(encoding="utf-8"))
        if d.get("segments"):
            d["source"] = "cache"
            d["text"]   = segments_to_text(d.get("segments", []))
            return d

    best: dict | None = None
    errors: list[str] = []
    for name in providers():
        fn = _PROVIDER_FN.get(name)
        if fn is None:
            continue
        try:
            d = fn(video_id)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:150]}")
            continue
        if not d:
            continue
        if d.get("segments"):
            # メタ情報は他プロバイダの方が充実していることがあるのでマージ
            if best and best.get("meta"):
                d["meta"] = {**best["meta"], **{k: v for k, v in d["meta"].items() if v}}
            d["text"] = segments_to_text(d["segments"])
            cache_f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            return d
        best = best or d
        if d.get("error"):
            errors.append(f"{name}: {d['error'][:150]}")

    out = best or {"meta": {"video_id": video_id,
                            "url": f"https://www.youtube.com/watch?v={video_id}"},
                   "segments": [], "lang": "", "source": "none"}
    if "ytdlp" not in providers() and "api" not in providers():
        errors.append("非公式の字幕取得は既定で無効 "
                      "(--allow-unofficial か YT_CAPTION_PROVIDERS で明示的に許可)")
    out["error"] = "; ".join(errors) or out.get("error") or "字幕なし"
    out["text"]  = ""
    cache_f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


# ── CLI (単体デバッグ用) ───────────────────────────────────────────────
def _safe_console() -> None:
    """
    Windows の cp932 コンソールで「✓」等が UnicodeEncodeError にならないようにする。
    (エンコードできない文字は ? に置き換えて出力を続ける)
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _safe_console()
    ap = argparse.ArgumentParser(description="YouTube 字幕取得 (デバッグ用)")
    ap.add_argument("--list",  metavar="URL", help="チャンネル/再生リストの動画一覧")
    ap.add_argument("--feed",  metavar="UCID", help="公式RSSで新着検知 (チャンネルID)")
    ap.add_argument("--import", dest="imp", metavar="VIDEO_ID",
                    help="手動の文字起こしを取り込む (--from と併用)")
    ap.add_argument("--from", dest="src", metavar="FILE", help="取り込むテキスト/VTT ファイル")
    ap.add_argument("--video", metavar="ID",  help="動画IDの字幕を取得して表示")
    ap.add_argument("--parse-vtt", metavar="FILE", help="ローカル VTT をパースして表示")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--allow-unofficial", action="store_true",
                    help="yt-dlp / youtube-transcript-api を有効化 (既定は manual のみ)")
    a = ap.parse_args()
    if a.allow_unofficial:
        allow_unofficial()

    if a.parse_vtt:
        segs = parse_vtt(Path(a.parse_vtt).read_text(encoding="utf-8"))
        print(segments_to_text(segs))
        print(f"\n--- {len(segs)} セグメント ---")
        return
    if a.imp:
        if not a.src:
            ap.error("--import には --from FILE が必要です")
        dst = import_manual(a.imp, Path(a.src))
        (TRANSCRIPT_DIR / f"{a.imp}.json").unlink(missing_ok=True)   # キャッシュ無効化
        segs = parse_manual(dst.read_text(encoding="utf-8"))
        print(f"取込: {dst}  ({len(segs)} セグメント)")
        return
    if a.feed:
        for v in feed_videos(a.feed, a.limit):
            print(f"{v['video_id']}  {v['upload_date']}  {v['title'][:60]}")
        return
    if a.list:
        for v in list_videos(a.list, a.limit):
            dur = f"{(v['duration'] or 0)//60}分" if v.get("duration") else "?"
            print(f"{v['video_id']}  {dur:>6}  {v['title'][:60]}")
        return
    if a.video:
        d = fetch_transcript(a.video, use_cache=not a.no_cache)
        m = d["meta"]
        print(f"# {m.get('title','')} / {m.get('channel','')} "
              f"/ {m.get('upload_date','')} / {d['source']} / lang={d['lang']}")
        if d.get("error"):
            print(f"! {d['error']}")
        print(d["text"][:3000])
        print(f"\n--- {len(d['segments'])} セグメント, {len(d['text'])} 文字 ---")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
