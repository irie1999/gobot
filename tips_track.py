"""
tips_track.py  ―  YouTube 発言の「事後検証」とチャンネル別実績 (時点情報のみ)
==============================================================================
youtube_tips.py が貯めた calls (銘柄への売買見解) を、実際の株価で答え合わせする。
**この仕組みの一番の価値はここ**。要約そのものより、
「誰の発言がどれだけ当たっているか」を数字にする方が売買判断に効く。

【未来情報を混ぜないための 2 つの決まり】

  1. **基準価格は「公開後に現実に買える最初の価格」**。公開時刻で場合分けする:

       寄り付き前 (< 09:00 JST) に公開 → その日の始値
       ザラ場中   (09:00-15:00)  に公開 → その日の終値      ※日足の制約 (下記)
       引け後     (>= 15:00)     に公開 → 翌営業日の始値
       公開時刻が不明 (日付のみ)        → 翌営業日の始値 (最も保守的)

     日足しか持っていないため、ザラ場中公開の「公開直後の約定価格」は取れない。
     その日の終値を代用しており、実際の値より有利にも不利にもなりうる。
     どのルールを使ったかは CSV の entry_rule 列に残す。

  2. **チャンネル実績はその時点で確定していた結果だけで計算する**
     (`source_reliability_asof`)。動画 X を採点するときに使ってよいのは、
     X の公開日より前に評価期間 (30日) が満了していた見解だけ。
     後から判明した成績を過去動画に逆適用すると、バックテストに未来情報が混ざる。

【株価データ】
  yfinance から **auto_adjust=True (株式分割・配当調整後)** で取得する。
  backtest_limit_entry.fetch は auto_adjust=False (無調整) なので、
  数ヶ月にまたがる騰落率の計算にはこちらを使う。取得結果は
  .youtube_tips_price_cache/ に 1 日キャッシュ。

【出力】
  youtube_tips_data/call_tracking.csv   … 1 call = 1 行の検証結果
  youtube_tips_data/channel_stats.json  … チャンネル別の集計 + 判定履歴 (時点計算用)

【使い方】
  python tips_track.py --update            # 株価を取得して検証 (週1回程度)
  python tips_track.py --update --days 180 # 直近180日に公開された動画だけ
  python tips_track.py --report            # チャンネル別ランキング
  python tips_track.py --report --by-symbol # 銘柄別
  python tips_track.py --asof 2026-06-01 --channel "○○ch"   # 時点実績の確認
"""

from __future__ import annotations

import argparse
import csv
import json
import pickle
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

JST      = timezone(timedelta(hours=9))
BASE     = Path(__file__).parent
DATA_DIR = BASE / "youtube_tips_data"
JSONL    = DATA_DIR / "youtube_tips.jsonl"
TRACK_CSV     = DATA_DIR / "call_tracking.csv"
CHANNEL_STATS = DATA_DIR / "channel_stats.json"
PRICE_CACHE   = BASE / ".youtube_tips_price_cache"

HORIZONS            = (30, 90)      # 評価する日数 (暦日)
MIN_CALLS_FOR_SCORE = 5             # これ未満は実績不明 (50 = 中立) 扱い
FULL_WEIGHT_N       = 20            # この件数で実績を満額反映 (それ未満は 50 に縮める)
THRESHOLD_PRICED_IN = 8.0           # 公開後にこれ以上動いていたら「織り込み済み」
NEUTRAL_SCORE       = 50.0          # 実績不明のときの source_reliability

MARKET_OPEN  = time(9, 0)
MARKET_CLOSE = time(15, 0)

TRACK_COLS = ["video_id", "channel", "upload_date", "published_at", "ticker", "company",
              "stance", "extraction_confidence", "entry_rule", "entry_date", "entry_price",
              "ret_30", "ret_90", "ret_now", "hit_30", "hit_90", "judged_30_at",
              "judged_90_at", "priced_in", "status", "url"]


# ── 価格取得 (調整後終値) ──────────────────────────────────────────────
def _fetch_adjusted(symbol: str):
    """
    株式分割・配当調整後の日足を取得する。失敗したら None。
    キャッシュは 1 日単位 (.youtube_tips_price_cache/<code>.pkl)。
    """
    PRICE_CACHE.mkdir(exist_ok=True)
    f = PRICE_CACHE / f"{symbol}.pkl"
    if f.exists():
        try:
            stamp, df = pickle.loads(f.read_bytes())
            if stamp == date.today():
                return df
        except Exception:
            pass
    try:
        import yfinance as yf
        raw = yf.Ticker(f"{symbol}.T").history(period="3y", interval="1d",
                                               auto_adjust=True, actions=False)
    except Exception:
        return None
    if raw is None or len(raw) == 0:
        return None
    if raw.index.tz is not None:
        raw.index = raw.index.tz_localize(None)
    raw.columns = [str(c).lower() for c in raw.columns]
    raw = raw[[c for c in ("open", "high", "low", "close") if c in raw.columns]].dropna()
    try:
        f.write_bytes(pickle.dumps((date.today(), raw)))
    except Exception:
        pass
    return raw


# ── 日足 → 純 Python のバー列 (テストしやすくするため) ────────────────
def to_bars(df) -> list[tuple[date, float, float]]:
    """DataFrame を [(日付, 始値, 終値), ...] に変換する。"""
    return [(idx.date() if hasattr(idx, "date") else idx,
             float(row["open"]), float(row["close"]))
            for idx, row in df.iterrows()]


# ── 基準価格 (公開後に現実に買える最初の価格) ─────────────────────────
def entry_reference(bars: list[tuple[date, float, float]], published: datetime,
                    has_time: bool) -> tuple[str, float, str]:
    """
    (entry_date, entry_price, entry_rule) を返す。取れなければ ("", 0.0, 理由)。
    ルールの根拠はモジュール docstring を参照。
    """
    pub_d = published.date()
    same  = [b for b in bars if b[0] == pub_d]
    after = [b for b in bars if b[0] > pub_d]

    if has_time and published.time() < MARKET_OPEN and same:
        return same[0][0].strftime("%Y-%m-%d"), same[0][1], "当日始値(寄付前公開)"
    if has_time and MARKET_OPEN <= published.time() < MARKET_CLOSE and same:
        return same[0][0].strftime("%Y-%m-%d"), same[0][2], "当日終値(ザラ場公開/日足代用)"
    if after:
        return after[0][0].strftime("%Y-%m-%d"), after[0][1], "翌営業日始値"
    return "", 0.0, "評価可能な足がまだ無い"


def _parse_published(rec: dict) -> tuple[datetime | None, bool]:
    """(公開日時, 時刻が判明しているか) を返す。"""
    iso = (rec.get("published_at") or "").strip()
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return (dt.astimezone(JST) if dt.tzinfo else dt.replace(tzinfo=JST)), True
        except ValueError:
            pass
    try:
        return datetime.strptime(rec.get("upload_date", ""), "%Y%m%d").replace(tzinfo=JST), False
    except ValueError:
        return None, False


# ── 1 件の評価 ─────────────────────────────────────────────────────────
def _eval_call(bars: list[tuple[date, float, float]], published: datetime,
               has_time: bool, stance: str) -> dict:
    edate, entry, rule = entry_reference(bars, published, has_time)
    out = {"entry_rule": rule, "entry_date": edate,
           "entry_price": round(entry, 1) if entry else "", "status": "ok"}
    if not edate or entry <= 0:
        out["status"] = "pending"
        return out

    entry_dt   = datetime.strptime(edate, "%Y-%m-%d").replace(tzinfo=JST)
    last_close = bars[-1][2]
    out["ret_now"]   = round((last_close / entry - 1) * 100, 2)
    out["priced_in"] = abs(out["ret_now"]) >= THRESHOLD_PRICED_IN

    now = datetime.now(JST)
    for h in HORIZONS:
        end = entry_dt + timedelta(days=h)
        out[f"judged_{h}_at"] = end.strftime("%Y-%m-%d")
        if end > now:
            out[f"ret_{h}"], out[f"hit_{h}"] = None, None
            out["status"] = "pending"
            continue
        window = [b for b in bars if b[0] <= end.date()]
        if not window:
            out[f"ret_{h}"], out[f"hit_{h}"] = None, None
            continue
        ret = (window[-1][2] / entry - 1) * 100
        out[f"ret_{h}"] = round(ret, 2)
        out[f"hit_{h}"] = (ret > 0) if stance == "強気" else \
                          ((ret < 0) if stance == "弱気" else None)
    return out


# ── 検証 ───────────────────────────────────────────────────────────────
def load_calls(days: int = 0) -> list[dict]:
    """JSONL から calls をフラットに取り出す (ticker 確定済みのみ)。"""
    if not JSONL.exists():
        return []
    cutoff = datetime.now(JST) - timedelta(days=days) if days else None
    seen: dict[tuple, dict] = {}
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        pub, has_time = _parse_published(r)
        if pub is None:
            continue
        if cutoff and pub < cutoff:
            continue
        for c in r.get("calls") or []:
            if not c.get("ticker") or not c.get("code_verified"):
                continue
            seen[(r["video_id"], c["ticker"])] = {          # 同じ動画×銘柄は後勝ち
                "video_id": r["video_id"], "channel": r.get("channel", ""),
                "published": pub, "has_time": has_time,
                "upload_date": r.get("upload_date", ""),
                "published_at": r.get("published_at", ""),
                "url": r.get("url", ""), "ticker": c["ticker"],
                "company": c.get("company", ""), "stance": c.get("stance", "中立"),
                "extraction_confidence": c.get("extraction_confidence",
                                               c.get("reliability", "")),
            }
    return list(seen.values())


def update(days: int = 0, verbose: bool = True) -> list[dict]:
    calls = load_calls(days)
    if not calls:
        print("検証対象の calls がありません (ticker 確定済みの見解が必要)。")
        return []

    rows: list[dict] = []
    cache: dict[str, list] = {}
    for c in calls:
        sym = c["ticker"]
        if sym not in cache:
            df = _fetch_adjusted(sym)
            cache[sym] = to_bars(df) if df is not None and len(df) else []
            if not cache[sym] and verbose:
                print(f"  ! {sym} 株価取得失敗 (yfinance)")
        bars = cache[sym]
        base = {k: c.get(k, "") for k in
                ("video_id", "channel", "upload_date", "published_at", "ticker",
                 "company", "stance", "extraction_confidence", "url")}
        if not bars:
            rows.append({**base, "status": "no_data"})
            continue
        rows.append({**base, **_eval_call(bars, c["published"], c["has_time"], c["stance"])})
        if verbose:
            r = rows[-1]
            print(f"  · {sym} {c['company'][:10]:<10} {c['stance']} "
                  f"[{r.get('entry_rule','')}] 30d={r.get('ret_30')} "
                  f"90d={r.get('ret_90')} ({r.get('status')})")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TRACK_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRACK_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k, "")) for k in TRACK_COLS})
    CHANNEL_STATS.write_text(json.dumps(build_stats(rows), ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"■ 検証 {len(rows)} 件 → {TRACK_CSV.name} / {CHANNEL_STATS.name}")
    return rows


# ── 集計 (履歴つき: 時点計算のため) ───────────────────────────────────
def build_stats(rows: list[dict]) -> dict:
    """
    チャンネル別に「いつ判定が確定したか」を含む履歴を残す。
    source_reliability_asof がこの履歴を使って、指定時点までの成績だけを集計する。
    """
    out: dict[str, dict] = {}
    for r in rows:
        ch = r.get("channel") or "(不明)"
        a  = out.setdefault(ch, {"calls": 0, "history": [], "ret30": [], "ret90": []})
        a["calls"] += 1
        for h, key in ((30, "ret30"), (90, "ret90")):
            v = r.get(f"ret_{h}")
            if isinstance(v, (int, float)):
                a[key].append(v)
        hit = r.get("hit_30")
        if isinstance(hit, bool) and r.get("judged_30_at"):
            a["history"].append({"judged_at": r["judged_30_at"], "hit": hit})

    for ch, a in out.items():
        a["history"].sort(key=lambda x: x["judged_at"])
        judged = len(a["history"])
        hits   = sum(1 for h in a["history"] if h["hit"])
        a["judged"]     = judged
        a["hit_rate"]   = round(hits / judged * 100, 1) if judged else None
        a["avg_ret_30"] = round(sum(a["ret30"]) / len(a["ret30"]), 2) if a["ret30"] else None
        a["avg_ret_90"] = round(sum(a["ret90"]) / len(a["ret90"]), 2) if a["ret90"] else None
        a["source_reliability"] = _score(hits, judged)
        a["updated"] = datetime.now(JST).strftime("%Y-%m-%d")
        del a["ret30"], a["ret90"]
    return out


def _score(hits: int, judged: int) -> float | None:
    """
    的中率を 0-100 の source_reliability に変換する。
    標本が少ないうちは 50 (中立) に縮める: 50 + (hit_rate - 50) * min(n/20, 1)。
    """
    if judged < MIN_CALLS_FOR_SCORE:
        return None
    hr = hits / judged * 100
    shrunk = NEUTRAL_SCORE + (hr - NEUTRAL_SCORE) * min(judged / FULL_WEIGHT_N, 1.0)
    return round(max(0.0, min(100.0, shrunk)), 1)


def load_channel_stats() -> dict:
    if not CHANNEL_STATS.exists():
        return {}
    try:
        return json.loads(CHANNEL_STATS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def source_reliability_asof(channel: str, asof: str | datetime | None = None,
                            stats: dict | None = None) -> float | None:
    """
    **asof 時点で既に確定していた** 判定だけを使って発信者の実績を算出する。

    asof には「これから採点する動画の公開日」を渡すこと。
    None を返したら実績不明 (呼び出し側は 50 = 中立として扱う)。
    """
    st = (stats if stats is not None else load_channel_stats()).get(channel)
    if not st:
        return None
    hist = st.get("history") or []
    if asof is None:
        return st.get("source_reliability")
    if isinstance(asof, datetime):
        asof = asof.strftime("%Y-%m-%d")
    asof = str(asof)[:10]
    past = [h for h in hist if str(h.get("judged_at", ""))[:10] < asof]
    return _score(sum(1 for h in past if h.get("hit")), len(past))


# ── レポート ───────────────────────────────────────────────────────────
def _read_rows() -> list[dict]:
    rows = list(csv.DictReader(TRACK_CSV.open(encoding="utf-8-sig")))
    for r in rows:
        for k in ("ret_30", "ret_90", "ret_now"):
            try:
                r[k] = float(r[k]) if r.get(k) not in ("", None) else None
            except ValueError:
                r[k] = None
        for k in ("hit_30", "hit_90"):
            r[k] = {"True": True, "False": False}.get(r.get(k), None)
    return rows


def report(by_symbol: bool = False) -> None:
    if not TRACK_CSV.exists():
        print("call_tracking.csv がありません。まず --update を実行してください。")
        return
    rows = _read_rows()

    if by_symbol:
        agg: dict[str, list] = {}
        for r in rows:
            agg.setdefault(f"{r['ticker']} {r['company'][:10]}", []).append(r)
        print(f"{'銘柄':<18}{'言及':>4}{'平均30d':>9}{'平均90d':>9}")
        for k, rs in sorted(agg.items(), key=lambda x: -len(x[1])):
            r30 = [r["ret_30"] for r in rs if r["ret_30"] is not None]
            r90 = [r["ret_90"] for r in rs if r["ret_90"] is not None]
            print(f"{k:<18}{len(rs):>4}"
                  f"{(sum(r30)/len(r30) if r30 else 0):>+8.2f}%"
                  f"{(sum(r90)/len(r90) if r90 else 0):>+8.2f}%")
        return

    stats = build_stats(rows)
    print(f"{'チャンネル':<24}{'件数':>5}{'判定済':>6}{'的中率':>8}"
          f"{'平均30d':>9}{'平均90d':>9}{'実績スコア':>10}")
    print("─" * 74)
    for ch, s in sorted(stats.items(), key=lambda x: -(x[1]["hit_rate"] or 0)):
        hr  = f"{s['hit_rate']:.1f}%" if s["hit_rate"] is not None else "-"
        a30 = f"{s['avg_ret_30']:+.2f}%" if s["avg_ret_30"] is not None else "-"
        a90 = f"{s['avg_ret_90']:+.2f}%" if s["avg_ret_90"] is not None else "-"
        sc  = f"{s['source_reliability']:.1f}" if s["source_reliability"] is not None else "不明"
        print(f"{ch[:23]:<24}{s['calls']:>5}{s['judged']:>6}{hr:>8}{a30:>9}{a90:>9}{sc:>10}")
    pend = sum(1 for r in rows if r.get("status") == "pending")
    print(f"\n判定待ち (評価期間が未経過): {pend} 件")
    print(f"※ 実績スコアは 0-100 (50=中立)。判定済 {MIN_CALLS_FOR_SCORE} 件未満は『不明』、"
          f"{FULL_WEIGHT_N} 件で満額反映。")
    print("※ 抽出時に使うのは『その動画の公開日より前に確定していた分だけ』"
          "(source_reliability_asof)。未来情報は入れない。")


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube 発言の事後検証・チャンネル実績")
    ap.add_argument("--update", action="store_true", help="株価を取得して検証を更新")
    ap.add_argument("--report", action="store_true", help="集計結果を表示")
    ap.add_argument("--by-symbol", action="store_true", help="銘柄別に集計")
    ap.add_argument("--days", type=int, default=0, help="直近N日の動画のみ検証")
    ap.add_argument("--asof", help="この日付時点の実績を表示 (YYYY-MM-DD)")
    ap.add_argument("--channel", help="--asof で見るチャンネル名")
    a = ap.parse_args()

    if a.asof:
        stats = load_channel_stats()
        chans = [a.channel] if a.channel else list(stats)
        for ch in chans:
            v = source_reliability_asof(ch, a.asof, stats)
            print(f"{ch[:30]:<32} {a.asof} 時点の実績スコア: "
                  f"{'不明 (判定済不足)' if v is None else v}")
        return
    if a.update:
        update(a.days)
    if a.report or not a.update:
        report(a.by_symbol)


if __name__ == "__main__":
    main()
