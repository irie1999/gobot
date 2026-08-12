r"""check_fill_today.py - シグナルが実際に約定したかを、当日の実データで確かめる。

なぜ要るか
----------
レポートの損益タブは **約定して決済したものだけ** を並べる。だから
「シグナルには出ていたのに損益に無い」銘柄が、

  (a) 本当に約定しなかったのか
  (b) 何かの不具合で落ちたのか

を画面からは区別できない。このスクリプトは日足の実 OHLC を取ってきて、
その日の始値・高値・安値から **約定条件をそのまま当てはめる**。

判定ルール (エンジンと同じ)
---------------------------
  前営業日終値 pc から
    現行 lss : トリガー = pc - 1ティック の **逆指値売り**
               約定条件 = 安値 <= トリガー
               約定値   = min(トリガー, 始値)      <- 寄りが割って始まれば始値(18.8)
               ガード   = 始値 < トリガー*0.97 なら約定不可(深ギャップ)
    H        : 指値 = pc - 5ティック(LSS_H_LIMIT_TICKS) の **指値売り**
               板寄せ   = 始値 >= 指値 なら 始値で約定(指値以上=有利)
               ザラ場   = 高値 >= 指値 なら 指値で約定
               ガード   = 始値 > pc*1.03 なら約定不可(ギャップアップ)

  決済は同日引け(概算)。損切・利確は日足では判定できないので、
  「引けまで持った場合」の損益を参考値として出す。**5分足の OCO は見ていない**ので
  レポートの損益とは一致しない。ここで確かめたいのは **約定したかどうか** だけ。

使い方
------
  python check_fill_today.py --date 2026-08-12 --symbols 4661,7733,6330,6140,3994,4208
  python check_fill_today.py --date 2026-08-12 --from-html signals_holdout_all_both_2026-08-11.html
  python check_fill_today.py --date 2026-08-12 --from-html <レポート> --limit 25

  --from-html はレポートHTMLから 4桁コード(NNNN.T)を出現順に拾う。
  シグナル表は流動性降順に並んでいるので、--limit N で上位N件だけ見られる。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--date", required=True, help="約定日(エントリー日) YYYY-MM-DD")
ap.add_argument("--symbols", default="", help="カンマ区切りの4桁コード 例 4661,7733")
ap.add_argument("--from-html", default="", help="レポートHTMLから銘柄を抽出する")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部)")
ap.add_argument("--h-ticks", type=int, default=-5, help="H の指値位置(ティック)。既定-5")
ap.add_argument("--gap-guard", type=float, default=0.03, help="ギャップガード。既定0.03")
args = ap.parse_args()

try:
    import pandas as pd
    from backtest_limit_entry import fetch as _fetch, round_to_tick as _r2t, tick_size as _tsz
except Exception as e:
    print(f"[error] 依存モジュールを読めません: {e}")
    sys.exit(1)

syms: list[str] = []
if args.from_html:
    p = Path(args.from_html)
    if not p.exists():
        print(f"[error] {p} がありません")
        sys.exit(1)
    seen = set()
    for m in re.finditer(r"\b(\d{4})\.T\b", p.read_text(encoding="utf-8", errors="ignore")):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            syms.append(m.group(1))
    print(f"[info] {p.name} から {len(syms)}銘柄を抽出")
syms += [s.strip().upper().removesuffix(".T")
         for s in args.symbols.split(",") if s.strip()]
# 重複除去(順序は保つ)
_seen: set[str] = set()
syms = [s for s in syms if not (s in _seen or _seen.add(s))]
if args.limit > 0:
    syms = syms[:args.limit]
if not syms:
    print("[error] 銘柄がありません。--symbols か --from-html を指定してください")
    sys.exit(1)

target = pd.to_datetime(args.date).normalize()
print(f"\n約定日 {target.date()} / {len(syms)}銘柄 / H指値 = 前日終値 {args.h_ticks:+d}ティック")
print("=" * 118)
print(f"{'銘柄':<7}{'前日終値':>9}{'始値':>9}{'高値':>9}{'安値':>9}{'終値':>9}"
      f"{'逆指値':>9}{'現行':>16}{'H指値':>9}{'H':>18}")
print("-" * 118)

n_lss = n_h = n_h_auc = 0
rows = []
for s in syms:
    try:
        df = _fetch(f"{s}.T", 400)
    except Exception as e:
        print(f"{s:<7} 取得失敗 ({e})")
        continue
    if df is None or df.empty:
        print(f"{s:<7} データなし")
        continue
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()
    c = {str(x).lower(): x for x in df.columns}
    if target not in df.index:
        print(f"{s:<7} {target.date()} のバーがありません(休場 or 未確定)")
        continue
    i = df.index.get_loc(target)
    if i == 0:
        print(f"{s:<7} 前営業日がありません")
        continue
    pc = float(df.iloc[i - 1][c["close"]])
    o = float(df.iloc[i][c["open"]])
    h = float(df.iloc[i][c["high"]])
    lo = float(df.iloc[i][c["low"]])
    cl = float(df.iloc[i][c["close"]])

    trig = float(_r2t(pc - _tsz(pc)))                       # 現行 = 前日終値-1tick
    hlim = float(_r2t(pc + args.h_ticks * _tsz(pc)))        # H    = 前日終値-5tick

    # 現行(逆指値売り)
    if o < trig * (1.0 - args.gap_guard):
        lss = "x 深ギャップ"
    elif lo <= trig:
        lss = f"O {min(trig, o):,.0f}"
        n_lss += 1
    else:
        lss = "x 未達"
    # H(指値売り)
    if o > pc * (1.0 + args.gap_guard):
        hs = "x ギャップUP"
    elif o >= hlim:
        hs = f"O 板寄 {o:,.0f}"
        n_h += 1
        n_h_auc += 1
    elif h >= hlim:
        hs = f"O ザラ場 {hlim:,.0f}"
        n_h += 1
    else:
        hs = "x 未達"
    print(f"{s:<7}{pc:>9,.0f}{o:>9,.0f}{h:>9,.0f}{lo:>9,.0f}{cl:>9,.0f}"
          f"{trig:>9,.0f}{lss:>16}{hlim:>9,.0f}{hs:>18}")
    rows.append((s, pc, o, h, lo, cl, trig, lss, hlim, hs))

n = len(rows)
if n:
    print("-" * 118)
    print(f"約定率  現行(逆指値) {n_lss}/{n} = {n_lss / n * 100:.0f}%   "
          f"H(指値) {n_h}/{n} = {n_h / n * 100:.0f}% "
          f"(うち板寄せ {n_h_auc}件 / ザラ場到達 {n_h - n_h_auc}件)")
    print("\n[!] ここで見ているのは **約定したかどうか** だけです。")
    print("    損益はレポート(5分足のOCO)と一致しません。日足では損切り0.1ATR/")
    print("    利確1.0ATR のどちらが先に来たか判定できないためです。")
    print("[!] 日足は分割調整済みですが5分足は未調整です(18.27)。分割銘柄の")
    print("    過去日を見るときは注意してください。当日の確認には影響しません。")
