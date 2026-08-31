"""
tips_track.py  ―  YouTube 発言の「事後検証」とチャンネル別実績ランキング
==========================================================================
youtube_tips.py が貯めた calls (銘柄への売買見解) を、実際の株価で答え合わせする。
**この仕組みの一番の価値はここ**。要約そのものより、
「誰の発言がどれだけ当たっているか」を数字にする方が売買判断に効く。

【評価の考え方】
  基準日   : 動画の公開日
  基準価格 : 公開日の翌営業日の始値 (= 動画を見てから現実に買える最初の価格)
  評価     : 基準価格に対する +30日 / +90日 後の終値の騰落率
  的中判定 : stance=強気 → リターン > 0
             stance=弱気 → リターン < 0
             stance=中立 → 判定対象外
  織り込み : 公開日から現在までに |騰落率| が THRESHOLD_PRICED_IN% を超えていたら
             「織り込み済み」として警告 (今から同じ行動を取っても遅い)

  ※ 未来日付 (まだ 30/90 日経っていない) は "pending" として集計から除外する。
    公開時点より後の情報で評価しないための処理。

【出力】
  youtube_tips_data/call_tracking.csv   … 1 call = 1 行の検証結果
  youtube_tips_data/channel_stats.json  … チャンネル別の集計 + 信頼度ボーナス
                                          (youtube_tips.py が読み込んで採点に反映)

【使い方】
  python tips_track.py --update            # 株価を取得して検証 (毎週〜毎月でよい)
  python tips_track.py --update --days 180 # 直近180日に公開された動画だけ
  python tips_track.py --report            # チャンネル別ランキングを表示
  python tips_track.py --report --by-symbol # 銘柄別に見る

【依存】
  backtest_limit_entry.fetch (yfinance + pandas) をそのまま使う。
  .rsi2_cache/ の永続キャッシュを共有するので、run_signals.py を回した後なら速い。
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST      = timezone(timedelta(hours=9))
BASE     = Path(__file__).parent
DATA_DIR = BASE / "youtube_tips_data"
JSONL    = DATA_DIR / "youtube_tips.jsonl"
TRACK_CSV     = DATA_DIR / "call_tracking.csv"
CHANNEL_STATS = DATA_DIR / "channel_stats.json"

HORIZONS            = (30, 90)      # 評価する日数 (暦日)
MIN_CALLS_FOR_BONUS = 5             # これ未満のチャンネルはボーナス 0 (標本不足)
MAX_BONUS           = 10.0          # 信頼度への加減点の上限 (±)
THRESHOLD_PRICED_IN = 8.0           # 公開後にこれ以上動いていたら「織り込み済み」

TRACK_COLS = ["video_id", "channel", "upload_date", "ticker", "company", "stance",
              "reliability", "entry_date", "entry_price", "ret_30", "ret_90",
              "ret_now", "hit_30", "hit_90", "priced_in", "status", "url"]


# ── 価格取得 ───────────────────────────────────────────────────────────
def _fetch(symbol: str, days: int):
    from backtest_limit_entry import fetch
    return fetch(f"{symbol}.T", max(days + 200, 400))


def _eval_call(df, upload: datetime, stance: str) -> dict:
    """1 件の call を株価で評価。df は backtest_limit_entry.fetch の DataFrame。"""
    import pandas as pd  # noqa: F401  (df 操作で必要)

    after = df[df.index > upload]
    if len(after) == 0:
        return {"status": "pending", "entry_date": "", "entry_price": 0.0}

    row0  = after.iloc[0]
    entry = float(row0["open"] if "open" in after.columns else row0["close"])
    if entry <= 0:
        return {"status": "no_price", "entry_date": "", "entry_price": 0.0}

    out = {"status": "ok",
           "entry_date":  after.index[0].strftime("%Y-%m-%d"),
           "entry_price": round(entry, 1)}

    last_close = float(df.iloc[-1]["close"])
    out["ret_now"] = round((last_close / entry - 1) * 100, 2)
    out["priced_in"] = abs(out["ret_now"]) >= THRESHOLD_PRICED_IN

    now = datetime.now(JST)
    for h in HORIZONS:
        end = upload + timedelta(days=h)
        if end > now:
            out[f"ret_{h}"] = None          # まだ評価期間が終わっていない
            out[f"hit_{h}"] = None
            out["status"] = "pending"
            continue
        window = df[df.index <= end]
        if len(window) == 0:
            out[f"ret_{h}"] = None
            out[f"hit_{h}"] = None
            continue
        ret = (float(window.iloc[-1]["close"]) / entry - 1) * 100
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
        try:
            up = datetime.strptime(r.get("upload_date", ""), "%Y%m%d").replace(tzinfo=JST)
        except ValueError:
            continue
        if cutoff and up < cutoff:
            continue
        for c in r.get("calls") or []:
            if not c.get("ticker") or not c.get("code_verified"):
                continue
            # 同じ動画×銘柄は後勝ち (--force 再抽出に対応)
            seen[(r["video_id"], c["ticker"])] = {
                "video_id": r["video_id"], "channel": r.get("channel", ""),
                "upload": up, "upload_date": r.get("upload_date", ""),
                "url": r.get("url", ""), "ticker": c["ticker"],
                "company": c.get("company", ""), "stance": c.get("stance", "中立"),
                "reliability": c.get("reliability", 0),
            }
    return list(seen.values())


def update(days: int = 0, verbose: bool = True) -> list[dict]:
    calls = load_calls(days)
    if not calls:
        print("検証対象の calls がありません (ticker 確定済みの見解が必要)。")
        return []

    rows: list[dict] = []
    cache: dict[str, object] = {}
    for c in calls:
        sym = c["ticker"]
        if sym not in cache:
            try:
                cache[sym] = _fetch(sym, 400)
            except Exception as e:
                cache[sym] = None
                if verbose:
                    print(f"  ! {sym} 株価取得失敗: {str(e)[:120]}")
        df = cache[sym]
        if df is None or len(df) == 0:
            rows.append({**{k: "" for k in TRACK_COLS}, "video_id": c["video_id"],
                         "channel": c["channel"], "upload_date": c["upload_date"],
                         "ticker": sym, "company": c["company"], "stance": c["stance"],
                         "reliability": c["reliability"], "status": "no_data",
                         "url": c["url"]})
            continue
        ev = _eval_call(df, c["upload"], c["stance"])
        rows.append({
            "video_id": c["video_id"], "channel": c["channel"],
            "upload_date": c["upload_date"], "ticker": sym, "company": c["company"],
            "stance": c["stance"], "reliability": c["reliability"], "url": c["url"],
            "entry_date": ev.get("entry_date", ""), "entry_price": ev.get("entry_price", ""),
            "ret_30": ev.get("ret_30"), "ret_90": ev.get("ret_90"),
            "ret_now": ev.get("ret_now"), "hit_30": ev.get("hit_30"),
            "hit_90": ev.get("hit_90"), "priced_in": ev.get("priced_in", ""),
            "status": ev.get("status", ""),
        })
        if verbose:
            print(f"  · {sym} {c['company'][:10]:<10} {c['stance']} "
                  f"30d={ev.get('ret_30')} 90d={ev.get('ret_90')} [{ev.get('status')}]")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TRACK_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRACK_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k, "")) for k in TRACK_COLS})
    stats = aggregate(rows)
    CHANNEL_STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"■ 検証 {len(rows)} 件 → {TRACK_CSV.name} / {CHANNEL_STATS.name}")
    return rows


# ── 集計 ───────────────────────────────────────────────────────────────
def aggregate(rows: list[dict]) -> dict:
    """チャンネル別に的中率・平均リターンを集計し、信頼度ボーナスを決める。"""
    agg: dict[str, dict] = {}
    for r in rows:
        ch = r.get("channel") or "(不明)"
        a  = agg.setdefault(ch, {"n": 0, "judged": 0, "hits": 0,
                                 "ret30": [], "ret90": []})
        a["n"] += 1
        for h, key in ((30, "ret30"), (90, "ret90")):
            v = r.get(f"ret_{h}")
            if isinstance(v, (int, float)):
                a[key].append(v)
        hit = r.get("hit_30")
        if hit is not None and hit != "":
            a["judged"] += 1
            a["hits"]   += 1 if hit is True else 0

    out: dict[str, dict] = {}
    for ch, a in agg.items():
        hr = (a["hits"] / a["judged"] * 100) if a["judged"] else None
        if a["judged"] >= MIN_CALLS_FOR_BONUS and hr is not None:
            bonus = max(-MAX_BONUS, min(MAX_BONUS, (hr - 50) / 5))
        else:
            bonus = 0.0
        out[ch] = {
            "calls": a["n"], "judged": a["judged"],
            "hit_rate": round(hr, 1) if hr is not None else None,
            "avg_ret_30": round(sum(a["ret30"]) / len(a["ret30"]), 2) if a["ret30"] else None,
            "avg_ret_90": round(sum(a["ret90"]) / len(a["ret90"]), 2) if a["ret90"] else None,
            "bonus": round(bonus, 1),
            "updated": datetime.now(JST).strftime("%Y-%m-%d"),
        }
    return out


def load_channel_stats() -> dict:
    """youtube_tips.py から呼ばれる。無ければ空 dict。"""
    if not CHANNEL_STATS.exists():
        return {}
    try:
        return json.loads(CHANNEL_STATS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def channel_bonus(channel: str) -> float:
    return float(load_channel_stats().get(channel, {}).get("bonus", 0.0) or 0.0)


# ── レポート ───────────────────────────────────────────────────────────
def report(by_symbol: bool = False) -> None:
    if not TRACK_CSV.exists():
        print("call_tracking.csv がありません。まず --update を実行してください。")
        return
    rows = list(csv.DictReader(TRACK_CSV.open(encoding="utf-8-sig")))
    for r in rows:
        for k in ("ret_30", "ret_90", "ret_now"):
            try:
                r[k] = float(r[k]) if r[k] != "" else None
            except ValueError:
                r[k] = None
        r["hit_30"] = {"True": True, "False": False}.get(r.get("hit_30"), None)

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

    stats = aggregate(rows)
    print(f"{'チャンネル':<24}{'件数':>5}{'判定済':>6}{'的中率':>8}"
          f"{'平均30d':>9}{'平均90d':>9}{'補正':>6}")
    print("─" * 70)
    for ch, s in sorted(stats.items(), key=lambda x: -(x[1]["hit_rate"] or 0)):
        hr  = f"{s['hit_rate']:.1f}%" if s["hit_rate"] is not None else "-"
        a30 = f"{s['avg_ret_30']:+.2f}%" if s["avg_ret_30"] is not None else "-"
        a90 = f"{s['avg_ret_90']:+.2f}%" if s["avg_ret_90"] is not None else "-"
        print(f"{ch[:23]:<24}{s['calls']:>5}{s['judged']:>6}{hr:>8}{a30:>9}{a90:>9}"
              f"{s['bonus']:>+6.1f}")
    pend = sum(1 for r in rows if r.get("status") == "pending")
    print(f"\n判定待ち (評価期間が未経過): {pend} 件")
    print("※ 補正は信頼度スコアへの加減点 (±10)。判定済 "
          f"{MIN_CALLS_FOR_BONUS} 件未満のチャンネルは 0。")


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube 発言の事後検証・チャンネル実績")
    ap.add_argument("--update", action="store_true", help="株価を取得して検証を更新")
    ap.add_argument("--report", action="store_true", help="集計結果を表示")
    ap.add_argument("--by-symbol", action="store_true", help="銘柄別に集計")
    ap.add_argument("--days", type=int, default=0, help="直近N日の動画のみ検証")
    a = ap.parse_args()
    if a.update:
        update(a.days)
    if a.report or not a.update:
        report(a.by_symbol)


if __name__ == "__main__":
    main()
