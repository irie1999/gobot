r"""fetch_minute_yf.py — yfinance で分足を取り、既存スクリプトが読める形で保存する。

なぜ要るか (2026-08-13)
-----------------------
J-Quants を解約したので、**ETF や先物の分足を J-Quants から取れない**。
だが日中の測定で分かったのは「日本株の日中に 1〜2bp の構造は実在するが、
往復コスト 8bp(呼値4ティック)に負ける」ということだった。構造が無いのではなく
コストに負けている。

  日本株 3,000円(呼値1円)      往復 約13bp
  日経レバETF 1570(3万円・呼値1円) 往復 約1.3bp
  日経225ミニ先物(25,000円)     往復 約0.8bp

ETF・先物なら **コストが1桁小さい**ので、1〜2bp の構造でも取れる可能性がある。
それを測るためのデータを yfinance から調達する。

⛔ yfinance の分足には厳しい制約がある
---------------------------------------
  1分足  : 直近 **7日**  (= 5営業日)
  5分足  : 直近 **60日** (= 約40営業日)
  1時間足: 730日 (ただし1日6本で日中構造は見えない)

40営業日では **日クラスタ t は出ない**(235日で t=2.14 だった升が t≈0.9 に落ちる)。
**bp(構造の大きさ)を見るための道具**と割り切ること。t で判定してはいけない。

毎週これを実行すれば1分足を継ぎ足して貯められる(7日ローリングなので、
週1回で連続データになる)。ただし検証に足る量が貯まるのは1年後。

保存形式
--------
`<code>_1m.pkl` (code = 銘柄コード + "0")。**5分足を取っても _1m.pkl で保存する**
— tenkan_sim._bars_one がこの名前しか見ないため。したがって
map_intraday_signals の N は「分」ではなく **「バー」** の意味になる
(5分足なら N=5 は 25分)。混同しないこと。

使い方
------
  # ETF・先物を5分足60日ぶん
  python fetch_minute_yf.py --symbols 1570,1357,1321,1306 --interval 5m --days 60
  python fetch_minute_yf.py --symbols NKD=F,^N225 --interval 5m --days 60

  # 保存先を既存の J-Quants キャッシュと混ぜないこと(混ぜると出所が分からなくなる)
  $env:MINUTE_1M_DIR = "$HOME\.yf_cache\minute"     # PowerShell(set は効かない)
  python map_intraday_signals.py --only 1570,1357,1321,1306 --days 60 --cost-bp 1.3
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# yfinance が interval ごとに許す最大遡及日数(超えると空で返る)
_MAXDAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730,
            "1h": 730}

ap = argparse.ArgumentParser(description="yfinance で分足を取り既存形式で保存")
ap.add_argument("--symbols", required=True,
                help="カンマ区切り。日本株は 1570 / 1570.T どちらでも可。"
                     "先物は NKD=F、指数は ^N225")
ap.add_argument("--interval", default="5m", choices=list(_MAXDAYS))
ap.add_argument("--days", type=int, default=60, help="遡及日数(interval の上限で切る)")
ap.add_argument("--out-dir", default=None,
                help="保存先。既定 ~/.yf_cache/minute "
                     "(J-Quants のキャッシュと **混ぜない**)")
ap.add_argument("--merge", action="store_true",
                help="既存 pkl があれば結合して保存(週1回実行で継ぎ足す用)")
args = ap.parse_args()

_LIM = _MAXDAYS[args.interval]
_DAYS = min(args.days, _LIM)
if args.days > _LIM:
    print(f"[warn] {args.interval} は yfinance の上限が {_LIM}日なので "
          f"{args.days} → {_DAYS} に切り詰めます")

_OUT = Path(args.out_dir) if args.out_dir else (Path.home() / ".yf_cache" / "minute")
_OUT.mkdir(parents=True, exist_ok=True)


def _code(sym: str) -> str:
    """tenkan_sim._bars_one が探すファイル名の幹。'1570.T' -> '15700'

    _bars_one は symbol.upper().replace('.T','') + '0' で引くので、
    保存側もまったく同じ変換を通す。
    """
    return sym.upper().replace(".T", "") + "0"


def _yf(sym: str) -> str:
    """4桁の数字なら日本株とみなして .T を付ける。先物・指数はそのまま。"""
    s = sym.strip()
    if s.isdigit() and len(s) == 4:
        return s + ".T"
    if len(s) == 5 and s.isdigit() and s[-1] == "0":
        return s[:4] + ".T"
    return s


def main() -> int:
    import pandas as pd
    import yfinance as yf

    syms = [x.strip() for x in str(args.symbols).split(",") if x.strip()]
    print(f"■ yfinance 分足取得  interval={args.interval} / {_DAYS}日 / "
          f"{len(syms)}銘柄")
    print(f"  保存先: {_OUT}")
    if args.interval != "1m":
        print(f"  ⚠ {args.interval} を **_1m.pkl** として保存します(既存スクリプトが"
              f"この名前しか見ないため)。")
        print(f"     → map_intraday_signals の N は「分」ではなく **バー** の意味に"
              f"なります(N=5 は {5 * int(args.interval.rstrip('mh') or 1)}分)。")
    print()

    ok = fail = 0
    for s in syms:
        y = _yf(s)
        try:
            df = yf.Ticker(y).history(period=f"{_DAYS}d", interval=args.interval,
                                      auto_adjust=False, actions=False)
        except Exception as e:
            print(f"  {s:<8} → エラー: {type(e).__name__}: {e}")
            fail += 1
            continue
        if df is None or df.empty:
            print(f"  {s:<8} → 空。yfinance がこの銘柄/期間を返しません")
            fail += 1
            continue
        df.columns = [c.lower() for c in df.columns]
        keep = [c for c in ("open", "high", "low", "close", "volume")
                if c in df.columns]
        df = df[keep].dropna(subset=["close"])
        try:
            df.index = (df.index.tz_localize("Asia/Tokyo")
                        if df.index.tzinfo is None
                        else df.index.tz_convert("Asia/Tokyo"))
        except Exception:
            pass
        p = _OUT / f"{_code(y)}_1m.pkl"
        if args.merge and p.exists():
            try:
                with open(p, "rb") as f:
                    old = pickle.load(f)
                df = pd.concat([old, df])
                df = df[~df.index.duplicated(keep="last")].sort_index()
            except Exception:
                pass
        with open(p, "wb") as f:
            pickle.dump(df, f)
        d0, d1 = str(df.index[0].date()), str(df.index[-1].date())
        nd = len({ts.date() for ts in df.index})
        print(f"  {s:<8} → {len(df):>7,}本 / {nd:>3}営業日 / {d0}〜{d1} → {p.name}")
        ok += 1

    print()
    print(f"  成功 {ok} / 失敗 {fail}")
    if ok:
        print()
        print("  次に測るには、保存先を明示してから実行してください:")
        # ⛔ PowerShell の set は環境変数を設定しない(Set-Variable のエイリアス)。
        #    CLAUDE.md 18.33 で実際に踏んでいる。$env: を出すこと。
        print(f'    $env:MINUTE_1M_DIR = "{_OUT}"       # PowerShell')
        print(f'    set MINUTE_1M_DIR={_OUT}            # cmd.exe')
        print(f"    python map_intraday_signals.py --only {args.symbols} "
              f"--days {_DAYS} --quantile 0.05 --cost-bp 1.3 --qty 10")
        print()
        print("  ⚠ 営業日数が少ないので **t で判定しないこと**。見るのは bp だけ。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
