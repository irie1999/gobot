"""fetch_1m_all.py — J-Quants 1分足を「取れるだけ」全銘柄取得(2年ローリング上限狙い)。

- ユニバース: stock_5min にある全銘柄(既定=local) / prime / all
- 銘柄ごとにキャッシュ(~/.jquants_cache/minute/<code>_1m.pkl)。
  中断→再実行で「キャッシュ内容が十分な履歴を持つ銘柄」はスキップ(日をまたいでも再開可)。
- レート制限/一時エラーは指数バックオフでリトライ。銘柄間に小休止。
- 不十分なキャッシュ(履歴が短い)は削除して full 取り直し。

使い方(あなたの機械・分足アドオン有効・.env設定済み):
  python fetch_1m_all.py                 # 全stock_5min銘柄・約2年(760日)
  python fetch_1m_all.py --days 760
  python fetch_1m_all.py --universe prime   # プライム全銘柄
  python fetch_1m_all.py --limit 30         # 先頭30(接続確認/デバッグ)
  python fetch_1m_all.py --refetch          # 既存キャッシュも取り直す

注意:
  - 1分足は5分足の約5倍のデータ量。全銘柄×2年は数時間〜。途中で止めても再開可。
  - fetch_intraday はチャンク単位のエラーを内部で握るため、稀に一部欠損しうる。
    データが短い銘柄は後日 --refetch で埋め直せる。
"""
from __future__ import annotations
import argparse
import sys
import time
from datetime import datetime, timedelta

import pandas as pd

import jquants_fetch as jf
from daytrade_data import available_local_symbols


def _jq_to_yf(code: str) -> str:
    c = code.strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


ap = argparse.ArgumentParser(description="J-Quants 1分足を取れるだけ全銘柄取得")
ap.add_argument("--days", type=int, default=760, help="遡及日数(既定760≒2年ローリング上限)")
ap.add_argument("--interval", default="1m", choices=["1m", "5m", "15m"])
ap.add_argument("--universe", default="local", choices=["local", "prime", "all"])
ap.add_argument("--only", default=None,
                help="銘柄を明示指定(カンマ区切り)。ETF など stock_5min に無いものを"
                     "直接取りに行く用。指定時は --universe を無視する")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部)")
ap.add_argument("--sleep", type=float, default=0.2, help="銘柄間の小休止秒")
ap.add_argument("--retry", type=int, default=4, help="レート制限/エラー時のリトライ回数")
ap.add_argument("--refetch", action="store_true", help="既存キャッシュも無視して取り直す")
args = ap.parse_args()


def universe() -> list[str]:
    if args.only:
        # stock_5min に無い銘柄(ETF など)を直接指定して取りに行く。
        return [_jq_to_yf(x) for x in str(args.only).split(",") if x.strip()]
    if args.universe == "local":
        return [_jq_to_yf(s) for s in available_local_symbols()]
    if args.universe == "prime":
        import symbols_listed_prime as p
        return [_jq_to_yf(str(s)) for s in getattr(p, "SYMBOLS", [])]
    import symbols_listed_all as a
    return [_jq_to_yf(str(s)) for s in getattr(a, "SYMBOLS", [])]


def _cache_path(sym: str):
    jq = jf.yf_to_jquants(sym)
    return jf._cache_path(jf.MINUTE_CACHE_DIR, f"{jq}_{args.interval}")


def already_have(sym: str) -> bool:
    """1分足キャッシュが十分な履歴(days-40日より前まで)を持てばTrue=スキップ。"""
    if args.refetch:
        return False
    df = jf._load_pkl(_cache_path(sym))
    if df is None or getattr(df, "empty", True):
        return False
    # ★2026-08-01: 旧バグ(時刻消失)で作られた「日付のみ(全バー同一時刻)」の壊れキャッシュは
    #   不十分扱いで取り直す(手動削除不要・再開可)。正しい1分足は時刻が多数=distinct>1。
    try:
        _times = set(df.index.time)
        if len(_times) <= 1:
            return False
    except Exception:
        pass
    try:
        earliest = pd.Timestamp(df.index.min())
        if earliest.tzinfo is not None:
            earliest = earliest.tz_localize(None)
        target = pd.Timestamp(datetime.now()) - timedelta(days=args.days - 40)
        return earliest <= target
    except Exception:
        return True  # 判定不能でもデータがあれば取り直さない


def main():
    seen = set()
    uni = []
    for s in universe():
        if s and s not in seen:
            seen.add(s)
            uni.append(s)
    if args.limit > 0:
        uni = uni[:args.limit]
    print(f"[info] 1分足取得: {len(uni)}銘柄 / 遡及{args.days}日 / interval={args.interval} "
          f"/ universe={args.universe}", flush=True)
    done = skip = fail = 0
    for i, sym in enumerate(uni, 1):
        if already_have(sym):
            skip += 1
            if i % 50 == 0:
                print(f"  [{i}/{len(uni)}] ...(done{done}/skip{skip}/fail{fail})", flush=True)
            continue
        # 不十分キャッシュは消して full 取り直し(fetch_intraday の当日フレッシュ返却で
        # 短いキャッシュが返るのを防ぐ)
        cp = _cache_path(sym)
        if cp.exists():
            try:
                cp.unlink()
            except Exception:
                pass
        ok = False
        df = None
        for attempt in range(args.retry + 1):
            try:
                df = jf.fetch_intraday(sym, days=args.days,
                                       interval=args.interval, use_cache=True)
                ok = df is not None and not df.empty
                break
            except Exception as e:
                wait = 5 * (2 ** attempt)
                print(f"  [{i}/{len(uni)}] {sym} エラー({e}) → {wait}s後リトライ"
                      f"({attempt + 1}/{args.retry})", file=sys.stderr, flush=True)
                time.sleep(wait)
        if ok:
            done += 1
            print(f"  [{i}/{len(uni)}] {sym}: {len(df)}本 "
                  f"(done{done}/skip{skip}/fail{fail})", flush=True)
        else:
            fail += 1
            print(f"  [{i}/{len(uni)}] {sym}: 取得失敗/データなし "
                  f"(done{done}/skip{skip}/fail{fail})", flush=True)
        time.sleep(args.sleep)
    print(f"\n[完了] 取得{done} / スキップ{skip} / 失敗{fail} / 合計{len(uni)}", flush=True)
    print(f"キャッシュ: {jf.MINUTE_CACHE_DIR}")
    print("※ 途中で止めても、再実行すれば十分な履歴のある銘柄はスキップされます(再開可)。")


if __name__ == "__main__":
    main()
