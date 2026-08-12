r"""probe_jq_5m.py — J-Quants の5分足を **1日ぶんだけ直接** 取ってきて、
ローカル(stock_5min = yfinance製)と並べて比べる。書き込みは一切しない。

なぜ要るか (2026-08-12)
-----------------------
stock_5min は `daily_fetch_minute.py` の既定 source="yfinance" で作られている。
その yfinance 5分足には2つの欠陥がある:

  * **09:00 のバーが無い**(or 出来高0・OHLC同値の合成ダミー)
    → 寄り(09:00-09:05)の値動きが見えない。4208 の 2026-08-12 は
      日足安値 3,546 が5分足に存在しない = 欠損窓に実際の値動きがある
  * **15:20 で終わる**(東証は2024/11から15:30引け)
    → 引け10分が見えない。決済価格は日足終値で救済済み(v12)だが、
      その間の損切り/利確は全銘柄で見えていない

`eh_trades.require_open_bar` は「先頭バーが09:00の日だけ使う」ので、実際には
**ダミー行がある銘柄だけ**を通していた(2026-08-12 実測で 633銘柄中 355件を除外)。
しかも delay1 の起点がダミーの有無で1本ずれる。

J-Quants の5分足に 09:00 と 15:25 があるなら、取り直すだけで全部直る。
`daily_fetch_minute.py --source jquants` は「既存より後ろのバーだけ追記」なので
**既にある日は取りに行かない**(=この確認には使えない)。だからこれを用意する。

使い方
------
  python probe_jq_5m.py --symbols 4661,4208 --date 2026-08-12
  python probe_jq_5m.py --symbols 4661 --date 2026-08-12 --all-bars
"""
from __future__ import annotations

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--symbols", required=True, help="カンマ区切りの4桁コード")
ap.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
ap.add_argument("--all-bars", action="store_true", help="全バーを出す(既定は前後3本)")
args = ap.parse_args()

try:
    import pandas as pd
    from daily_fetch_minute import get_client, yf_to_jq  # type: ignore
except Exception:
    try:
        import pandas as pd
        from daily_fetch_minute import get_client        # type: ignore
        yf_to_jq = None                                  # type: ignore
    except Exception as e:
        sys.exit(f"[error] daily_fetch_minute を読めません: {e}")

try:
    from daytrade_data import (normalize_minute_df as _norm_df,
                               load_intraday as _li,
                               yf_to_jquants as _y2j)
    from backtest_limit_entry import fetch as _fetch
except Exception as e:
    sys.exit(f"[error] 依存モジュールを読めません: {e}")

cli = get_client()
d = args.date.replace("-", "")
tgt = pd.to_datetime(args.date).normalize()


def _show(tag: str, df):
    if df is None or len(df) == 0:
        print(f"    {tag}: バーなし")
        return None
    cm = {str(x).lower(): x for x in df.columns}
    print(f"    {tag}: {len(df)}本 / 先頭 {df.index[0].strftime('%H:%M')} "
          f"/ 末尾 {df.index[-1].strftime('%H:%M')}")
    rows = df if args.all_bars else pd.concat([df.head(3), df.tail(2)])
    for ts, r in rows.iterrows():
        print(f"      {ts.strftime('%H:%M')} O{float(r[cm['open']]):>9,.1f} "
              f"H{float(r[cm['high']]):>9,.1f} L{float(r[cm['low']]):>9,.1f} "
              f"C{float(r[cm['close']]):>9,.1f} V{float(r[cm['volume']]):>10,.0f}")
    return (float(df[cm["high"]].max()), float(df[cm["low"]].min()))


for s in [x.strip().upper().removesuffix(".T").split(".")[0]
          for x in args.symbols.split(",") if x.strip()]:
    code = _y2j(f"{s}.T")            # 4桁 → J-Quants の5桁
    print(f"\n{'=' * 70}\n■ {s} (J-Quants code {code})  {args.date}\n{'=' * 70}")

    # ── 日足(正解の基準) ──────────────────────────────────────────
    dref = None
    try:
        dd = _fetch(f"{s}.T", 200)
        dd.index = pd.to_datetime(dd.index).normalize()
        if tgt in dd.index:
            c = {str(x).lower(): x for x in dd.columns}
            r = dd.loc[tgt]
            dref = (float(r[c["open"]]), float(r[c["high"]]),
                    float(r[c["low"]]), float(r[c["close"]]))
            print(f"  日足: 始{dref[0]:,.1f} 高{dref[1]:,.1f} "
                  f"安{dref[2]:,.1f} 終{dref[3]:,.1f}")
    except Exception as e:
        print(f"  日足を取れません: {e}")

    # ── ローカル(yfinance製) ─────────────────────────────────────
    try:
        loc = _li(f"{s}.T", 400, source="local")
        loc = loc[loc.index.normalize() == tgt] if loc is not None else None
    except Exception:
        loc = None
    hl_loc = _show("ローカル(yfinance製)", loc)

    # ── J-Quants を直接 ─────────────────────────────────────────
    try:
        raw = cli.get_eq_bars_5minute(code=code, from_yyyymmdd=d, to_yyyymmdd=d)
    except Exception as e:
        print(f"    J-Quants: 取得失敗 ({e})")
        continue
    if raw is None or len(raw) == 0:
        print("    J-Quants: 空の応答")
        continue
    if "Code" in raw.columns:
        raw = raw[raw["Code"].astype(str) == code].copy()
    jq = _norm_df(raw)
    jq = jq[jq.index.normalize() == tgt] if jq is not None and len(jq) else jq
    hl_jq = _show("J-Quants(直接)", jq)

    # ── 判定 ────────────────────────────────────────────────────
    if dref and hl_jq:
        ok_h = abs(dref[1] - hl_jq[0]) < 0.51
        ok_l = abs(dref[2] - hl_jq[1]) < 0.51
        print(f"  → J-Quants の高値/安値 vs 日足: "
              f"高値{'一致' if ok_h else '不一致'} / 安値{'一致' if ok_l else '不一致'}")
        if ok_h and ok_l and hl_loc and (abs(dref[2] - hl_loc[1]) >= 0.51
                                         or abs(dref[1] - hl_loc[0]) >= 0.51):
            print("     ★ J-Quants は日足と整合、ローカルは不整合 = "
                  "**取り直せば欠損が直る**")
