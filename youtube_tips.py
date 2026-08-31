"""
youtube_tips.py  ―  YouTube 字幕から「株の売買見解 / Tips」を自動収集し gobot と照合
======================================================================================

    [youtube_sources.py]  監視チャンネル (RSS / チャンネルURL / 検索キーワード)
              ↓  新着検知 (公式RSSなら APIキー不要)
    [yt_transcript.py]    字幕取得 manual → yt-dlp → transcript-api の順に試行
              ↓
    [tips_extract.py]     LLM で構造化抽出 (発言と推測を分離 / 信頼度はルーブリック採点)
              ↓  symbol_lookup.py で銘柄コードを裏取り
    youtube_tips_data/youtube_tips.jsonl   全レコード (calls / tips / 取得失敗)
    youtube_tips_data/youtube_tips_log.csv 動画インデックス
              ↓
    [tips_track.py]       30日/90日後の騰落率で答え合わせ → チャンネル別実績
              ↓
    youtube_tips_<date>.html   gobot シグナルとの照合表つき日次レポート

**自動発注はしない。** YouTube の見解は gobot シグナルの補助材料 (参考情報) として扱う。

【字幕取得の既定は「手動取込」のみ】
  yt-dlp / youtube-transcript-api は YouTube 公式の字幕APIではないため、
  既定では **無効** です。使う場合だけ明示的に有効化してください。
    python youtube_tips.py --allow-unofficial
    export YT_CAPTION_PROVIDERS=manual,ytdlp,api
  手動取込 (推奨・第一候補):
    YouTube の「文字起こしを表示」→ コピー → ファイル保存 →
    python yt_transcript.py --import <video_id> --from copied.txt

【セットアップ】
  pip install -U yt-dlp                 # 任意 (--allow-unofficial 時のみ使用)
  pip install anthropic                 # 任意 (ANTHROPIC_API_KEY を使う場合)
  # 上記が無くても Claude Code CLI (`claude`) があれば抽出は動く
  vi youtube_sources.py                 # 監視チャンネルを登録

【使い方】
  python youtube_tips.py                        # 巡回 → 新着だけ処理 → HTML
  python youtube_tips.py --match-signals        # gobot の当日シグナルと突き合わせ
  python youtube_tips.py --url https://youtu.be/xxxx   # 単発の動画を処理
  python youtube_tips.py --source "https://www.youtube.com/@ch/videos"
  python youtube_tips.py --report --days 7      # 収集済みから直近7日のHTML
  python youtube_tips.py --digest --days 7      # ターミナルにテキスト要約
  python youtube_tips.py --failures             # 字幕が取れなかった動画の一覧
  python youtube_tips.py --allow-unofficial     # yt-dlp 等の非公式経路を許可
  python youtube_tips.py --backend heuristic    # LLM を使わない (無料・低精度)
  python youtube_tips.py --cross-check cmd --llm-cmd "codex exec -"
                                                # Claude と codex の 2 エンジン合議
  python youtube_tips.py --force                # 処理済み動画も再抽出
  python youtube_tips.py --dry-run              # 対象動画の一覧だけ表示
  python youtube_tips.py --no-browser           # HTML を開かない

【字幕が取れない動画への対処】
  1. `python youtube_tips.py --failures` で失敗一覧を出す
  2. YouTube 画面の「文字起こしを表示」でテキストをコピー
  3. `python yt_transcript.py --import <video_id> --from copied.txt`
  4. `python youtube_tips.py --url <video_id> --force`
  → 取得経路が将来変わっても、監視・抽出・gobot 連携の部分はそのまま使える。

【毎日自動で回す (朝夕2回)】
  crontab -e:
    30 7  * * 1-5 cd /path/to/gobot && python3 youtube_tips.py --no-browser >> youtube_tips.log 2>&1
    30 21 * * 1-5 cd /path/to/gobot && python3 youtube_tips.py --no-browser --match-signals >> youtube_tips.log 2>&1
    0  6  * * 6   cd /path/to/gobot && python3 tips_track.py --update >> youtube_tips.log 2>&1
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import symbol_lookup
import tips_extract as _ex
import tips_track as _track
import youtube_sources as _src
import yt_transcript as _yt

JST      = timezone(timedelta(hours=9))
BASE     = Path(__file__).parent
DATA_DIR = BASE / "youtube_tips_data"
JSONL    = DATA_DIR / "youtube_tips.jsonl"
CSV_LOG  = DATA_DIR / "youtube_tips_log.csv"

CATEGORIES = ["エントリー", "利確", "損切り", "資金管理", "メンタル",
              "マクロ", "銘柄", "ツール", "その他"]
CAT_COLOR = {
    "エントリー": "#1d4ed8", "利確": "#065f46", "損切り": "#b91c1c",
    "資金管理": "#7c3aed", "メンタル": "#c2410c", "マクロ": "#0369a1",
    "銘柄": "#a16207", "ツール": "#334155", "その他": "#475569",
}
STANCE_COLOR = {"強気": "#16a34a", "弱気": "#dc2626", "中立": "#475569"}
CSV_COLS = ["video_id", "upload_date", "published_at", "channel", "title", "url",
            "duration_min", "lang", "source", "chars", "n_calls", "n_tips", "noise",
            "promo", "backend", "extraction_backend", "llm_attempts",
            "llm_failure_reason", "requires_review", "model", "error", "processed_at"]

MIN_DURATION = 180      # 短すぎる動画は費用対効果が悪いので既定で除外 (秒)
MAX_DURATION = 7200


# ── 蓄積データ ─────────────────────────────────────────────────────────
def load_records() -> dict[str, dict]:
    """JSONL を読み、video_id → レコード (同一 id は後勝ち) の辞書で返す。"""
    recs: dict[str, dict] = {}
    if not JSONL.exists():
        return recs
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("video_id"):
            recs[r["video_id"]] = r
    return recs


def append_record(rec: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_csv(recs: dict[str, dict]) -> None:
    """JSONL から CSV インデックスを作り直す (常に全書き換え)。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(recs.values(),
                  key=lambda r: (r.get("upload_date") or "", r.get("processed_at") or ""),
                  reverse=True)
    with CSV_LOG.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**{k: r.get(k, "") for k in CSV_COLS},
                        "duration_min": round((r.get("duration") or 0) / 60),
                        "n_calls": len(r.get("calls") or []),
                        "n_tips":  len(r.get("tips") or []),
                        "noise": "1" if r.get("noise") else "",
                        "promo": "1" if r.get("promo") else ""})


# ── ユーティリティ ─────────────────────────────────────────────────────
_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def video_id_of(s: str) -> str:
    """URL でも生 ID でも video_id を取り出す。"""
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    s = s.strip()
    return s if re.fullmatch(r"[A-Za-z0-9_-]{11}", s) else ""


def ts_seconds(ts: str) -> int:
    """"12:34" / "1:02:03" → 秒。パースできなければ 0。"""
    parts = [p for p in str(ts).strip().split(":") if p.isdigit()]
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def parse_upload(d: str) -> datetime | None:
    try:
        return datetime.strptime(d, "%Y%m%d").replace(tzinfo=JST)
    except (ValueError, TypeError):
        return None


def watchlist_symbols() -> dict[str, str]:
    """check_signals_* の WATCHLIST から {証券コード: 銘柄名} を作る (失敗しても空)。"""
    out: dict[str, str] = {}
    for mod in ("check_signals_stop", "check_signals_breakout"):
        try:
            m = __import__(mod)
            for sym, name, _strat in getattr(m, "WATCHLIST", []):
                out[str(sym).split(".")[0]] = name
        except Exception:
            continue
    return out


def _in_period(rec: dict, days: int | None) -> bool:
    if not days:
        return True
    cutoff = datetime.now(JST) - timedelta(days=days)
    dt = parse_upload(rec.get("upload_date") or "")
    if dt is None:
        try:
            dt = datetime.fromisoformat(rec.get("processed_at", ""))
        except ValueError:
            return True
    return dt >= cutoff


def load_priced_in() -> dict[tuple, dict]:
    """tips_track の検証結果 (あれば) を (video_id, ticker) で引けるようにする。"""
    f = _track.TRACK_CSV
    if not f.exists():
        return {}
    out = {}
    try:
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            out[(r.get("video_id", ""), r.get("ticker", ""))] = r
    except Exception:
        return {}
    return out


# ── gobot シグナルとの照合 ─────────────────────────────────────────────
def gobot_signals(codes: set[str], workers: int = 4) -> dict[str, list[dict]]:
    """
    指定コードについて gobot (check_signals_stop / breakout) の当日シグナルを調べる。
    WATCHLIST に無い銘柄はそもそも gobot の監視対象外なので空リストになる。
    株価データを引くので数十秒かかることがある (--match-signals 指定時のみ実行)。
    """
    out: dict[str, list[dict]] = {}
    jobs: list[tuple] = []
    for modname, family in (("check_signals_stop", "stop"),
                            ("check_signals_breakout", "breakout")):
        try:
            mod = __import__(modname)
        except Exception as e:
            print(f"  ! {modname} を読み込めません: {str(e)[:120]}", file=sys.stderr)
            continue
        for sym, name, strat in getattr(mod, "WATCHLIST", []):
            if str(sym).split(".")[0] in codes:
                jobs.append((mod, family, sym, name, strat))

    if not jobs:
        return out

    def _one(job):
        mod, family, sym, name, strat = job
        try:
            return sym, name, family, strat, mod.check_signal_on_date(sym, strat, None)
        except Exception:
            return sym, name, family, strat, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_one, j) for j in jobs]):
            sym, name, family, strat, sig = fut.result()
            if not sig:
                continue
            out.setdefault(str(sym).split(".")[0], []).append({
                "family": family, "strategy": strat, "name": name,
                "order_price": sig.get("order_price"), "stop_price": sig.get("stop_price"),
                "target_price": sig.get("target_price"),
                "signal_date": sig.get("signal_date"),
            })
    return out


def judge(call: dict, sigs: list[dict], in_watchlist: bool, priced_in: bool) -> tuple[str, str]:
    """(判定ラベル, 色クラス) を返す。売買指示ではなく「扱い方」の目安。"""
    if priced_in:
        return "織り込み済み警告", "warn"
    if sigs:
        if call["stance"] == "強気":
            return "補強 (gobotロング + 強気)", "ok"
        if call["stance"] == "弱気":
            return "不一致 (gobotロング vs 弱気)", "ng"
        return "gobotシグナルあり", "ok"
    if in_watchlist:
        return "動画のみ (参考)", ""
    return "gobot対象外 (参考)", ""


# ── 1 本処理 ───────────────────────────────────────────────────────────
def _base_rec(vid: str, meta: dict) -> dict:
    return {
        "video_id": vid, "title": meta.get("title", ""),
        "channel": meta.get("channel", ""), "channel_id": meta.get("channel_id", ""),
        "url": meta.get("url", f"https://www.youtube.com/watch?v={vid}"),
        "upload_date": meta.get("upload_date") or "",
        "published_at": meta.get("published_at") or "",
        "duration": meta.get("duration") or 0,
        "view_count": meta.get("view_count") or 0,
        "processed_at": datetime.now(JST).isoformat(timespec="seconds"),
    }


def process_one(v: dict, args, verbose: bool = True) -> dict | None:
    vid  = v["video_id"]
    tr   = _yt.fetch_transcript(vid, use_cache=not args.no_cache)
    meta = {**v, **{k: val for k, val in (tr.get("meta") or {}).items() if val}}

    if not tr.get("segments"):
        if verbose:
            print(f"  - {vid} 字幕なし: {meta.get('title','')[:40]} "
                  f"({(tr.get('error') or '')[:60]})")
        return {**_base_rec(vid, meta), "lang": "", "source": tr.get("source", ""),
                "chars": 0, "backend": "none", "model": "", "noise": True, "promo": False,
                "market_view": "", "summary": [], "calls": [], "tips": [],
                "error": tr.get("error") or "字幕なし"}

    # 発信者の実績は「この動画の公開時刻より前に確定していた判定」だけを使う。
    # (後から判明した成績を過去動画に逆適用すると未来情報が混ざる)
    # 公開時刻が分かればそれを、日付しか無ければその日の 00:00 JST を使う (保守的)。
    pub_iso = (meta.get("published_at") or "").strip()
    if not pub_iso:
        ud = meta.get("upload_date") or ""
        pub_iso = (f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}T00:00:00+09:00" if len(ud) == 8 else "")
    src_rel = _track.source_reliability_asof(meta.get("channel", ""), pub_iso or None)
    res = _ex.extract_tips(tr["text"], meta, backend=args.backend, model=args.model,
                           verbose=verbose, source_reliability=src_rel,
                           cross_check=args.cross_check)
    rec = {**_base_rec(vid, meta),
           "lang": tr.get("lang", ""), "source": tr.get("source", ""),
           "chars": len(tr["text"]), "source_reliability": src_rel,
           "injection_suspected": res.get("injection_suspected", False),
           "injection_hits": res.get("injection_hits", []),
           "backend": res.get("backend", ""), "model": res.get("model", ""),
           "extraction_backend": res.get("extraction_backend", ""),
           "llm_attempts": res.get("llm_attempts", 0),
           "llm_failure_reason": res.get("llm_failure_reason", ""),
           "requires_review": res.get("requires_review", False),
           "noise": res.get("noise", False), "promo": res.get("promo", False),
           "market_view": res.get("market_view", ""), "summary": res.get("summary", []),
           "calls": res.get("calls", []), "tips": res.get("tips", [])}
    for k in ("error", "cross_check_error"):
        if res.get(k):
            rec[k] = res[k]
    if verbose:
        mark = " (ノイズ)" if rec["noise"] else ""
        if rec.get("requires_review"):
            mark += f" ⚠要確認({rec.get('llm_failure_reason') or 'heuristic/注入検出'})"
        print(f"  ✓ {vid} calls={len(rec['calls'])} tips={len(rec['tips'])}{mark} "
              f"[{rec['backend']}/{rec['source']}] {rec['title'][:36]}")
    return rec


# ── 収集 ───────────────────────────────────────────────────────────────
def gather_targets(args, done: set[str]) -> list[dict]:
    """処理対象の動画リストを作る (重複・尺・日付・ライブを除外)。"""
    targets: list[dict] = []
    seen: set[str] = set()

    for u in (args.url or []):
        vid = video_id_of(u)
        if not vid:
            print(f"  ! 動画IDを取り出せません: {u}", file=sys.stderr)
            continue
        if vid not in seen:
            seen.add(vid)
            targets.append({"video_id": vid, "title": "", "url": u,
                            "duration": None, "channel": "", "upload_date": None})

    sources = [{"name": s, "url": s, "limit": args.limit or 5, "feed": ""}
               for s in (args.source or [])]
    if not args.url and not sources:
        sources = _src.active_sources()
        if args.limit:
            for s in sources:
                s["limit"] = args.limit

    for s in sources:
        try:
            if s.get("feed"):        # 公式 RSS (APIキー不要・軽い)
                vids = _yt.feed_videos(s["feed"], s["limit"])
            else:
                vids = _yt.list_videos(s["url"], s["limit"])
        except Exception as e:
            print(f"  ! ソース取得失敗 [{s['name']}]: {str(e)[:160]}", file=sys.stderr)
            continue
        print(f"  · {s['name']}: {len(vids)} 本")
        for v in vids:
            if v["video_id"] in seen:
                continue
            seen.add(v["video_id"])
            targets.append(v)

    out = []
    for v in targets:
        if v["video_id"] in done and not args.force:
            continue
        if v.get("live"):
            continue
        dur = v.get("duration")
        if dur and not args.url and (dur < args.min_duration or dur > args.max_duration):
            continue
        ud = parse_upload(v.get("upload_date") or "")
        if ud and args.since_days and ud < datetime.now(JST) - timedelta(days=args.since_days):
            continue
        out.append(v)
        if args.max_videos and len(out) >= args.max_videos:
            break
    return out


def collect(args) -> dict[str, dict]:
    recs = load_records()
    print("■ 対象動画を収集中…")
    targets = gather_targets(args, set(recs.keys()))
    if not targets:
        print("  新着なし (処理済み or ソース未登録)。"
              " youtube_sources.py にチャンネルを追加してください。")
        return recs

    print(f"■ 新規 {len(targets)} 本を処理 "
          f"(backend={_ex.pick_backend(args.backend)}"
          f"{'+' + args.cross_check if args.cross_check else ''}, model={args.model})")
    if args.dry_run:
        for v in targets:
            dur = f"{(v.get('duration') or 0)//60}分" if v.get("duration") else "?"
            print(f"  · {v['video_id']} {dur:>5} {v.get('title','')[:60]}")
        return recs

    new: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, v, args): v for v in targets}
        for fut in as_completed(futs):
            v = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                print(f"  ! {v['video_id']} 処理失敗: {str(e)[:200]}", file=sys.stderr)
                continue
            if r:
                new.append(r)

    for r in sorted(new, key=lambda r: r.get("upload_date") or ""):
        append_record(r)
        recs[r["video_id"]] = r
    write_csv(recs)
    print(f"■ 完了: 動画 {len(new)} 本 / "
          f"見解 {sum(len(r.get('calls') or []) for r in new)} 件 / "
          f"Tips {sum(len(r.get('tips') or []) for r in new)} 件 → {JSONL.name}")
    return recs


# ── 集計 ───────────────────────────────────────────────────────────────
def flatten(recs: list[dict], key: str) -> list[dict]:
    """全レコードの calls / tips を「動画情報付き」の 1 次元リストにする。"""
    out = []
    for r in recs:
        for x in r.get(key) or []:
            out.append({**x, "video_id": r["video_id"], "title": r.get("title", ""),
                        "channel": r.get("channel", ""), "url": r.get("url", ""),
                        "upload_date": r.get("upload_date", "")})
    if key == "calls":
        out.sort(key=lambda c: (c.get("reference_score") or 0), reverse=True)
    else:
        out.sort(key=lambda t: (t.get("upload_date") or "", t.get("confidence") or 0),
                 reverse=True)
    return out


# ── HTML ───────────────────────────────────────────────────────────────
CSS = """
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a;
         color:#e2e8f0; padding:20px; }
  h1 { color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }
  .subtitle { color:#94a3b8; margin-bottom:20px; font-size:0.9rem; }
  h2 { color:#60a5fa; margin:28px 0 12px; font-size:1.2rem;
       border-left:3px solid #60a5fa; padding-left:10px; }
  table { width:100%; border-collapse:collapse; margin-bottom:10px; font-size:0.85rem; }
  th { background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:left;
       border:1px solid #334155; white-space:nowrap; }
  td { padding:6px 8px; border:1px solid #1e293b; vertical-align:top; }
  tr:hover td { background:#1e293b; }
  .tag { display:inline-block; padding:1px 8px; border-radius:99px;
         font-size:0.75rem; font-weight:600; color:#fff; white-space:nowrap; }
  .num { text-align:right; white-space:nowrap; }
  .muted { color:#94a3b8; font-size:0.78rem; }
  .claim { color:#e2e8f0; }
  .ai { color:#a5b4fc; font-size:0.78rem; margin-top:3px; }
  .ai::before { content:"AI補足: "; color:#6366f1; }
  a { color:#38bdf8; text-decoration:none; }
  a:hover { text-decoration:underline; }
  .ok   { color:#4ade80; font-weight:600; }
  .ng   { color:#f87171; font-weight:600; }
  .warn { color:#fbbf24; font-weight:600; }
  .bar { display:inline-block; height:8px; border-radius:4px; background:#334155;
         width:60px; vertical-align:middle; overflow:hidden; }
  .bar i { display:block; height:100%; background:#38bdf8; }
  .filters { margin:10px 0 12px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  .filters button { background:#1e293b; color:#e2e8f0; border:1px solid #334155;
                    border-radius:99px; padding:4px 12px; cursor:pointer; font-size:0.8rem; }
  .filters button.on { background:#2563eb; border-color:#3b82f6; }
  .filters input { background:#1e293b; color:#e2e8f0; border:1px solid #334155;
                   border-radius:6px; padding:5px 10px; font-size:0.8rem; min-width:220px; }
  .card { background:#111c34; border:1px solid #1e293b; border-radius:8px;
          padding:12px 14px; margin-bottom:10px; }
  .card h3 { font-size:0.95rem; margin-bottom:4px; }
  .card ul { margin:6px 0 0 18px; font-size:0.83rem; color:#cbd5e1; }
  .watch { background:#fbbf24; color:#000; padding:1px 6px; border-radius:4px;
           font-size:0.72rem; font-weight:700; }
  .unverified { background:#7f1d1d; color:#fecaca; padding:1px 6px; border-radius:4px;
                font-size:0.72rem; }
  .noise { opacity:0.55; }
  .stat { display:inline-block; background:#1e293b; border:1px solid #334155;
          border-radius:6px; padding:6px 12px; margin:0 8px 8px 0; font-size:0.82rem; }
  .stat b { color:#60a5fa; font-size:1.05rem; }
  .note { color:#94a3b8; font-size:0.78rem; margin:6px 0 18px; }
"""

JS = """
function applyFilter() {
  var on = document.querySelector('.filters button.on');
  var cat = on ? on.dataset.cat : 'ALL';
  var q = document.getElementById('q').value.trim().toLowerCase();
  var n = 0;
  document.querySelectorAll('#tips-body tr').forEach(function (tr) {
    var okCat = (cat === 'ALL' || tr.dataset.cat === cat);
    var okQ = (q === '' || tr.innerText.toLowerCase().indexOf(q) >= 0);
    tr.style.display = (okCat && okQ) ? '' : 'none';
    if (okCat && okQ) n++;
  });
  document.getElementById('hit').textContent = n + ' 件';
}
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.filters button').forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll('.filters button').forEach(function (x) {
        x.classList.remove('on');
      });
      b.classList.add('on');
      applyFilter();
    };
  });
  document.getElementById('q').oninput = applyFilter;
  applyFilter();
});
"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _yt_link(url: str, sec: int) -> str:
    if not sec:
        return url
    return url + ("&" if "?" in url else "?") + f"t={sec}s"


def build_html(recs: list[dict], days: int | None, sigs: dict[str, list[dict]] | None,
               matched: bool) -> str:
    now     = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    calls   = flatten(recs, "calls")
    tips    = flatten(recs, "tips")
    wl      = watchlist_symbols()
    sigs    = sigs or {}
    tracked = load_priced_in()
    stats   = _track.load_channel_stats()
    period  = f"直近 {days} 日" if days else "全期間"
    fails   = [r for r in recs if r.get("error")]

    backends: dict[str, int] = {}
    for r in recs:
        backends[r.get("backend") or "?"] = backends.get(r.get("backend") or "?", 0) + 1
    backend_str = " / ".join(f"{k}:{v}" for k, v in sorted(backends.items()))

    # 1) gobot 照合表
    match_rows = ""
    for c in calls:
        code = c.get("ticker", "")
        s    = sigs.get(code, [])
        tr   = tracked.get((c["video_id"], code)) or {}
        priced = str(tr.get("priced_in", "")).lower() == "true"
        label, cls = judge(c, s, code in wl, priced)
        gobot = "<br>".join(f"{x['family']}/{x['strategy']} {x['signal_date']}" for x in s) \
                or ('<span class="muted">シグナルなし</span>' if code in wl
                    else '<span class="muted">監視対象外</span>')
        badge = ' <span class="watch">WATCH</span>' if code in wl else ""
        if not c.get("code_verified"):
            badge += ' <span class="unverified">コード未確認</span>'
        basis = " / ".join(c.get("evidence_type") or []) or "-"
        if c.get("catalysts"):
            basis += "<br><span class='muted'>" + _esc("・".join(c["catalysts"][:3])) + "</span>"
        ext  = int(c.get("extraction_confidence") or 0)
        agr  = c.get("agreement_score")
        srel = c.get("source_reliability")
        ref  = int(c.get("reference_score") or 0)
        breakdown = (f"抽出{ext} / 一致{'-' if agr is None else int(agr)} / "
                     f"発信者{'不明' if srel in (None, '') else int(float(srel))}")
        match_rows += f"""
        <tr>
          <td><b>{_esc(code or '-')}</b>{badge}<br><span class="muted">{_esc(c.get('company',''))}</span></td>
          <td>{gobot}</td>
          <td><span class="tag" style="background:{STANCE_COLOR.get(c['stance'], '#475569')}">
              {_esc(c['stance'])}</span><br><span class="muted">{_esc(c.get('action',''))}</span></td>
          <td class="muted">{_esc(c.get('upload_date',''))}<br>{_esc(c.get('time_horizon',''))}</td>
          <td>{basis}</td>
          <td class="num"><span class="bar"><i style="width:{ref}%"></i></span> {ref}
              <br><span class="muted">{_esc(breakdown)}</span></td>
          <td class="{cls}">{_esc(label)}</td>
        </tr>"""
    if not match_rows:
        match_rows = ('<tr><td colspan="7" style="text-align:center;color:#94a3b8">'
                      '銘柄見解なし</td></tr>')

    # 2) 見解の詳細 (発言と推測を分離)
    call_rows = ""
    for c in calls:
        link = _yt_link(c["url"], int(c.get("timestamp_seconds") or 0))
        ts   = c.get("timestamp_seconds") or 0
        ts_s = f'<a href="{_esc(link)}" target="_blank">{ts // 60}:{ts % 60:02d}</a>' if ts else ""
        risks = "・".join(c.get("risks") or []) or "-"
        agree = (f'<br><span class="muted">{_esc(c["agreement"])}</span>'
                 if c.get("agreement") and c["agreement"] != "未実施" else "")
        if c.get("requires_review"):
            agree += '<br><span class="unverified">要確認</span>' 
        ext   = int(c.get("extraction_confidence") or 0)
        agr   = c.get("agreement_score")
        srel  = c.get("source_reliability")
        call_rows += f"""
        <tr>
          <td><b>{_esc(c.get('ticker') or '-')}</b><br><span class="muted">{_esc(c.get('company',''))}</span></td>
          <td><span class="tag" style="background:{STANCE_COLOR.get(c['stance'], '#475569')}">
              {_esc(c['stance'])}</span>{agree}</td>
          <td><div class="claim">{_esc(c.get('speaker_claim',''))}</div>
              <div class="ai">{_esc(c.get('ai_note',''))}</div></td>
          <td class="muted">入: {_esc(c.get('entry_condition') or '-')}<br>
              標: {_esc(c.get('target_price') or '-')}<br>
              退: {_esc(c.get('stop_condition') or '-')}</td>
          <td class="muted">{_esc(risks)}</td>
          <td class="num">{ext}</td>
          <td class="num">{'-' if agr is None else int(agr)}</td>
          <td class="num">{'不明' if srel in (None, '') else int(float(srel))}</td>
          <td class="muted">{ts_s}<br><a href="{_esc(c['url'])}" target="_blank">{_esc(c['title'][:28])}</a>
              <br>{_esc(c['channel'][:20])}</td>
        </tr>"""
    if not call_rows:
        call_rows = ('<tr><td colspan="9" style="text-align:center;color:#94a3b8">'
                     '銘柄見解なし</td></tr>')

    # 3) Tips
    cat_counts: dict[str, int] = {}
    for t in tips:
        cat_counts[t["category"]] = cat_counts.get(t["category"], 0) + 1
    buttons = [f'<button class="on" data-cat="ALL">すべて ({len(tips)})</button>']
    for c in CATEGORIES:
        if cat_counts.get(c):
            buttons.append(f'<button data-cat="{_esc(c)}" '
                           f'style="border-color:{CAT_COLOR.get(c, "#334155")}">'
                           f'{_esc(c)} ({cat_counts[c]})</button>')

    tip_rows = ""
    for t in tips:
        color = CAT_COLOR.get(t["category"], "#475569")
        conf  = float(t.get("confidence") or 0)
        link  = _yt_link(t["url"], ts_seconds(t.get("timestamp", "")))
        ts_txt = (f'<a href="{_esc(link)}" target="_blank">{_esc(t["timestamp"])}</a>'
                  if t.get("timestamp") else "")
        detail = f'<div class="muted">{_esc(t["detail"])}</div>' if t.get("detail") else ""
        act    = "" if t.get("actionable", True) else '<span class="muted"> (参考)</span>'
        tip_rows += f"""
        <tr data-cat="{_esc(t['category'])}">
          <td><span class="tag" style="background:{color}">{_esc(t['category'])}</span></td>
          <td>{_esc(t['tip'])}{act}{detail}</td>
          <td class="num"><span class="bar"><i style="width:{int(conf*100)}%"></i></span></td>
          <td class="muted">{ts_txt}</td>
          <td class="muted"><a href="{_esc(t['url'])}" target="_blank">{_esc(t['title'][:40])}</a>
              <br>{_esc(t['channel'][:22])} / {_esc(t['upload_date'])}</td>
        </tr>"""
    if not tip_rows:
        tip_rows = ('<tr><td colspan="5" style="text-align:center;color:#94a3b8">'
                    'Tips がありません</td></tr>')

    # 4) チャンネル実績
    stat_rows = ""
    for ch, s in sorted(stats.items(), key=lambda x: -(x[1].get("hit_rate") or 0)):
        stat_rows += f"""
        <tr><td>{_esc(ch)}</td><td class="num">{s.get('calls', 0)}</td>
            <td class="num">{s.get('judged', 0)}</td>
            <td class="num">{'-' if s.get('hit_rate') is None else f"{s['hit_rate']:.1f}%"}</td>
            <td class="num">{'-' if s.get('avg_ret_30') is None else f"{s['avg_ret_30']:+.2f}%"}</td>
            <td class="num">{'-' if s.get('avg_ret_90') is None else f"{s['avg_ret_90']:+.2f}%"}</td>
            <td class="num">{'不明' if s.get('source_reliability') is None
                                 else f"{s['source_reliability']:.1f}"}</td></tr>"""
    stat_section = f"""
  <h2>チャンネル別 実績 (事後検証)</h2>
  <table><thead><tr><th>チャンネル</th><th>見解数</th><th>判定済</th><th>的中率(30日)</th>
    <th>平均30日</th><th>平均90日</th><th>実績スコア</th></tr></thead>
    <tbody>{stat_rows}</tbody></table>
  <div class="note">tips_track.py --update で更新 (0-100, 50=中立)。判定済
    {_track.MIN_CALLS_FOR_SCORE} 件未満は「不明」。抽出時に使うのは
    <b>その動画の公開日より前に確定していた判定だけ</b>で、後から判明した成績を
    過去の動画へ逆適用しません。</div>
""" if stat_rows else ""

    # 5) 取得失敗
    fail_rows = ""
    for r in fails:
        fail_rows += f"""
        <tr><td><a href="{_esc(r['url'])}" target="_blank">{_esc(r.get('title') or r['video_id'])}</a></td>
            <td class="muted">{_esc(r.get('channel',''))}</td>
            <td class="muted">{_esc(r.get('upload_date') or '-')}</td>
            <td class="muted">{_esc((r.get('error') or '')[:120])}</td>
            <td class="muted">python yt_transcript.py --import {_esc(r['video_id'])} --from copied.txt</td>
        </tr>"""
    fail_section = f"""
  <h2>字幕を取得できなかった動画 ({len(fails)})</h2>
  <table><thead><tr><th>動画</th><th>チャンネル</th><th>公開日</th><th>理由</th>
    <th>手動取込コマンド</th></tr></thead><tbody>{fail_rows}</tbody></table>
  <div class="note">YouTube 画面の「文字起こしを表示」からコピーしたテキストを取り込めば、
    同じパイプラインで処理できます。</div>
""" if fail_rows else ""

    # 6) 動画カード
    cards = ""
    for r in sorted(recs, key=lambda r: (r.get("upload_date") or ""), reverse=True):
        if r.get("error"):
            continue
        cls  = " noise" if r.get("noise") else ""
        summ = "".join(f"<li>{_esc(x)}</li>" for x in (r.get("summary") or []))
        mv   = (f'<div class="muted">相場観: {_esc(r["market_view"])}</div>'
                if r.get("market_view") else "")
        promo = ' <span class="warn">宣伝多め</span>' if r.get("promo") else ""
        inj   = (' <span class="unverified">字幕に指示文を検出</span>'
                 if r.get("injection_suspected") else "")
        if r.get("llm_failure_reason"):
            inj += (f' <span class="unverified">LLM失敗→heuristic '
                    f'({_esc(r["llm_failure_reason"])}, {r.get("llm_attempts", 0)}回試行)</span>')
        cards += f"""
      <div class="card{cls}">
        <h3><a href="{_esc(r['url'])}" target="_blank">{_esc(r['title'] or r['video_id'])}</a>{promo}{inj}</h3>
        <div class="muted">{_esc(r.get('channel',''))} / {_esc(r.get('upload_date',''))}
             / {round((r.get('duration') or 0)/60)}分 / 見解{len(r.get('calls') or [])}件
             / Tips{len(r.get('tips') or [])}件 / {_esc(r.get('backend',''))}
             / 字幕:{_esc(r.get('source',''))}</div>
        {mv}<ul>{summ}</ul>
      </div>"""

    injected = [r for r in recs if r.get("injection_suspected")]
    injection_banner = (
        f'<div class="note warn">⚠ {len(injected)} 本の字幕に「指示文」らしき記述を検出しました'
        '(データとして扱い、指示には従っていません)。該当動画は下の一覧で確認してください。</div>'
        if injected else "")

    match_note = ("gobot の当日シグナルと突き合わせ済み。"
                  if matched else
                  "※ gobot 照合は未実行 (--match-signals を付けると当日シグナルと突き合わせます)。")
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>YouTube 株Tips — {now[:10]}</title>
<style>{CSS}</style>
<script>{JS}</script>
</head>
<body>
  <h1>YouTube 株Tips レポート</h1>
  <div class="subtitle">生成 {now} JST / {period} / 抽出: {_esc(backend_str)}</div>

  <div>
    <span class="stat">動画 <b>{len(recs)}</b> 本</span>
    <span class="stat">銘柄見解 <b>{len(calls)}</b> 件</span>
    <span class="stat">Tips <b>{len(tips)}</b> 件</span>
    <span class="stat">字幕取得失敗 <b>{len(fails)}</b> 本</span>
    <span class="stat">要確認 <b>{sum(1 for r in recs if r.get('requires_review'))}</b> 本</span>
  </div>
  {injection_banner}

  <h2>gobot シグナルとの照合</h2>
  <table>
    <thead><tr>
      <th style="width:150px">銘柄</th><th style="width:150px">gobot</th>
      <th style="width:120px">動画見解</th><th style="width:90px">投稿日</th>
      <th>根拠</th><th style="width:150px">参考値<br><span class="muted">(抽出/一致/発信者)</span></th>
      <th style="width:160px">扱い</th>
    </tr></thead>
    <tbody>{match_rows}</tbody>
  </table>
  <div class="note">{match_note}
    <b>スコアは 3 種類を分けて保存しています</b>: 抽出確度 (字幕から正しく読み取れたか) /
    一致度 (2エンジンの独立抽出が一致したか) / 発信者実績 (公開時点で確定していた過去成績)。
    表示している参考値はこの 3 つの加重平均 (重み {int(_ex.WEIGHT_EXTRACTION*100)}/
    {int(_ex.WEIGHT_AGREEMENT*100)}/{int(_ex.WEIGHT_SOURCE*100)}) にすぎず、
    <b>売買シグナルではありません。2エンジンが一致していても、両方が同じ誤読をしている
    可能性は残ります。この表から自動発注は行いません。</b></div>

  <h2>銘柄見解の詳細 (発言 / AI補足を分離)</h2>
  <table>
    <thead><tr>
      <th style="width:110px">銘柄</th><th style="width:110px">スタンス</th>
      <th>発言内容</th><th style="width:180px">条件</th><th style="width:130px">リスク</th>
      <th style="width:56px">抽出<br>確度</th><th style="width:56px">一致<br>度</th>
      <th style="width:64px">発信者<br>実績</th><th style="width:170px">出典</th>
    </tr></thead>
    <tbody>{call_rows}</tbody>
  </table>

  <h2>Tips 一覧 (手法・ルール)</h2>
  <div class="filters">
    {" ".join(buttons)}
    <input id="q" placeholder="キーワードで絞り込み (例: 損切り 5%)">
    <span class="muted" id="hit"></span>
  </div>
  <table>
    <thead><tr>
      <th style="width:90px">分類</th><th>Tips</th><th style="width:80px">確度</th>
      <th style="width:60px">時刻</th><th style="width:250px">出典</th>
    </tr></thead>
    <tbody id="tips-body">{tip_rows}</tbody>
  </table>
{stat_section}{fail_section}
  <h2>動画別サマリー</h2>
  {cards}
</body>
</html>"""


# ── テキスト digest ────────────────────────────────────────────────────
def print_digest(recs: list[dict], category: str | None) -> None:
    calls = flatten(recs, "calls")
    if calls:
        wl = watchlist_symbols()
        print("■ 銘柄見解 (信頼度順)")
        for c in calls[:20]:
            mark = " ★WATCHLIST" if c.get("ticker") in wl else ""
            ver  = "" if c.get("code_verified") else " [コード未確認]"
            if c.get("requires_review"):
                ver += " [要確認]"
            agr  = c.get("agreement_score")
            srel = c.get("source_reliability")
            print(f"  {c.get('ticker') or '----'} {c.get('company','')[:12]:<12} "
                  f"{c['stance']} 参考{int(c.get('reference_score') or 0):>3} "
                  f"(抽出{int(c.get('extraction_confidence') or 0)}"
                  f"/一致{'-' if agr is None else int(agr)}"
                  f"/発信者{'不明' if srel in (None, '') else int(float(srel))})"
                  f"{mark}{ver}")
            if c.get("speaker_claim"):
                print(f"      発言: {c['speaker_claim'][:80]}")
            if c.get("entry_condition") or c.get("stop_condition"):
                print(f"      条件: 入={c.get('entry_condition','-')} / "
                      f"退={c.get('stop_condition','-')}")

    tips = flatten(recs, "tips")
    if category:
        tips = [t for t in tips if t["category"] == category]
    by_cat: dict[str, list[dict]] = {}
    for t in tips:
        by_cat.setdefault(t["category"], []).append(t)
    for c in CATEGORIES:
        if c not in by_cat:
            continue
        print(f"\n■ {c} ({len(by_cat[c])}件)")
        for t in by_cat[c]:
            ts = f" [{t['timestamp']}]" if t.get("timestamp") else ""
            print(f"  ・{t['tip']}")
            if t.get("detail"):
                print(f"      {t['detail']}")
            print(f"      ↳ {t['channel']} / {t['title'][:40]}{ts}")


def print_failures(recs: dict[str, dict]) -> None:
    fails = [r for r in recs.values() if r.get("error")]
    if not fails:
        print("字幕取得に失敗した動画はありません。")
        return
    print(f"■ 字幕を取得できなかった動画 {len(fails)} 本")
    for r in sorted(fails, key=lambda r: r.get("upload_date") or "", reverse=True):
        print(f"  {r['video_id']}  {r.get('upload_date') or '-':<8}  {(r.get('title') or '')[:50]}")
        print(f"      理由: {(r.get('error') or '')[:120]}")
        print(f"      対処: 文字起こしをコピーして "
              f"python yt_transcript.py --import {r['video_id']} --from copied.txt")
    print("\n取り込んだ後: python youtube_tips.py --url <video_id> --force")


# ── CLI ────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="YouTube 字幕から株の見解/Tips を収集し gobot と照合する",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append", metavar="URL|ID", help="単発の動画 (複数可)")
    ap.add_argument("--source", action="append", metavar="URL",
                    help="チャンネル/再生リスト (youtube_sources.py の代わり)")
    ap.add_argument("--limit", type=int, default=0, help="1ソースあたりの取得本数")
    ap.add_argument("--max-videos", type=int, default=30, help="1回で処理する最大本数")
    ap.add_argument("--since-days", type=int, default=0,
                    help="公開からN日以内の動画だけ処理 (0=無制限)")
    ap.add_argument("--min-duration", type=int, default=MIN_DURATION,
                    help=f"これより短い動画は無視 (秒, 既定 {MIN_DURATION})")
    ap.add_argument("--max-duration", type=int, default=MAX_DURATION,
                    help=f"これより長い動画は無視 (秒, 既定 {MAX_DURATION})")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "api", "cli", "cmd", "heuristic"], help="抽出エンジン")
    ap.add_argument("--cross-check", default="",
                    choices=["", "api", "cli", "cmd", "heuristic"],
                    help="2つ目のエンジンで相互チェック (例: codex を cmd で)")
    ap.add_argument("--llm-cmd", default="",
                    help='外部エンジンのコマンド 例: "codex exec -"')
    ap.add_argument("--model", default=_ex.DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=3, help="並列数")
    ap.add_argument("--match-signals", action="store_true",
                    help="gobot の当日シグナルと照合する (株価取得あり)")
    ap.add_argument("--allow-unofficial", action="store_true",
                    help="yt-dlp / youtube-transcript-api を有効化 (既定は手動取込のみ)")
    ap.add_argument("--force", action="store_true", help="処理済み動画も再抽出")
    ap.add_argument("--no-cache", action="store_true", help="字幕キャッシュを使わない")
    ap.add_argument("--dry-run", action="store_true", help="対象一覧だけ表示")
    ap.add_argument("--report", action="store_true", help="収集せず HTML だけ再生成")
    ap.add_argument("--digest", action="store_true", help="HTML でなくテキストで出力")
    ap.add_argument("--failures", action="store_true", help="字幕取得失敗の一覧を表示")
    ap.add_argument("--category", choices=CATEGORIES, help="digest のカテゴリ絞り込み")
    ap.add_argument("--days", type=int, default=0, help="レポート対象期間 (0=全期間)")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    args = ap.parse_args()

    if args.llm_cmd:
        os.environ["TIPS_LLM_CMD"] = args.llm_cmd
    if args.allow_unofficial:
        _yt.allow_unofficial()

    if args.failures:
        print_failures(load_records())
        return

    recs = load_records() if args.report else collect(args)
    if args.dry_run:
        return

    target = [r for r in recs.values() if _in_period(r, args.days)]
    target.sort(key=lambda r: (r.get("upload_date") or ""), reverse=True)

    if args.digest:
        print_digest(target, args.category)
        return
    if not target:
        print("レポート対象のデータがありません。まず収集を実行してください。")
        return

    sigs = None
    if args.match_signals:
        codes = {c["ticker"] for c in flatten(target, "calls") if c.get("ticker")}
        print(f"■ gobot シグナル照合中… ({len(codes)} 銘柄)")
        sigs = gobot_signals(codes, args.workers)
        print(f"  シグナルあり: {len(sigs)} 銘柄")

    out = BASE / f"youtube_tips_{datetime.now(JST).strftime('%Y-%m-%d')}.html"
    out.write_text(build_html(target, args.days or None, sigs, args.match_signals),
                   encoding="utf-8")
    print(f"■ HTML: {out}")
    if not args.no_browser:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
