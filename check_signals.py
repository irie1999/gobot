"""
check_signals.py  ―  監視銘柄のシグナルチェック
=================================================
【今日（または指定日）のシグナル確認 → HTML自動表示】
  python check_signals.py
  python check_signals.py --date 2026-03-28        # 指定日のシグナル
  python check_signals.py --preset core            # 元の8銘柄
  python check_signals.py --no-browser             # HTML生成のみ（ブラウザ起動しない）

【過去期間のシグナル発生回数】
  python check_signals.py --history --days 90
  python check_signals.py --history --from 2025-10-01 --to 2026-03-31 --preset core
  python check_signals.py --history --days 180 --verbose

【銘柄一覧】
  python check_signals.py --list
"""

from __future__ import annotations

import argparse
import math
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest_adaptive_mr       as _amr
import backtest_strong_pullback   as _sp
import backtest_donchian_pullback as _don
import backtest_bb_volume         as _bbv
import backtest_limit_oco         as _oco

JST = timezone(timedelta(hours=9))
LOT = 100

# ── 監視リスト ────────────────────────────────────────────────────────
WATCHLIST: list[tuple[str, str, list[str]]] = [
    # ── 強い押し目（1800銘柄バックテスト + 4期間スコア選定）──────────
    ("6146.T", "ディスコ",                        ["strong_pullback"]),
    ("6871.T", "日本マイクロニクス",               ["strong_pullback"]),
    ("7236.T", "ティラド",                        ["strong_pullback"]),
    ("7012.T", "川崎重工業",                      ["strong_pullback"]),
    ("5108.T", "ブリヂストン",                    ["strong_pullback"]),
    ("4025.T", "多木化学",                        ["strong_pullback"]),
    ("8218.T", "コメリ",                          ["strong_pullback"]),
    ("3946.T", "トーモク",                        ["strong_pullback"]),
    ("6183.T", "ベルシステム２４ホールディングス", ["strong_pullback"]),
    ("3636.T", "三菱総合研究所",                  ["strong_pullback"]),
    ("8153.T", "モスフードサービス",               ["strong_pullback"]),
    ("8194.T", "ライフコーポレーション",           ["strong_pullback"]),
    ("9046.T", "神戸電鉄",                        ["strong_pullback"]),
    ("9405.T", "朝日放送グループホールディングス", ["strong_pullback"]),
    ("8081.T", "カナデン",                        ["strong_pullback"]),
    ("6454.T", "マックス",                        ["strong_pullback"]),
    ("6196.T", "ストライク",                      ["strong_pullback"]),
]

STRATEGY_NAMES = {
    "adaptive_mr":    "アダプティブMR",
    "strong_pullback":"強い押し目",
    "donchian":       "ドンチャン押し目",
    "bb_volume":      "BB出来高",
    "limit_oco":      "指値OCO",
}

STRATEGY_COLOR = {
    "adaptive_mr":    "#38bdf8",
    "strong_pullback":"#4ade80",
    "donchian":       "#fb923c",
    "bb_volume":      "#a78bfa",
    "limit_oco":      "#f472b6",
}

PRESETS: dict[str, list[str]] = {
    # 合計損益上位8銘柄（スコア選定17銘柄から）
    "core": ["6146.T", "6871.T", "7236.T", "7012.T",
             "5108.T", "4025.T", "8218.T", "3946.T"],
    "all":  [],  # 空 = WATCHLIST 全体
}


# ─────────────────────────────────────────────────────────────────────
# 内部ユーティリティ
# ─────────────────────────────────────────────────────────────────────

def _rr_float(entry: float, stop: float, target: float) -> float | None:
    risk = entry - stop
    if risk <= 0:
        return None
    return (target - entry) / risk


def _rr(entry: float, stop: float, target: float) -> str:
    v = _rr_float(entry, stop, target)
    return f"{v:.1f}R" if v is not None else "—"


def _pct(a: float, b: float) -> str:
    return f"{(a - b) / b * 100:+.2f}%" if b else "—"


def _fetch_and_calc(symbol: str, key: str, fetch_days: int,
                    target_date: date | None = None) -> pd.DataFrame | None:
    """fetch + calc し、target_date が指定されればその日までスライスして返す。"""
    try:
        if key == "adaptive_mr":
            df = _amr.fetch(symbol, fetch_days)
            df = _amr.calc(df) if df is not None else None
        elif key == "strong_pullback":
            df = _sp.fetch(symbol, fetch_days)
            df = _sp.calc(df) if df is not None else None
        elif key == "donchian":
            df = _don.fetch(symbol, fetch_days)
            df = _don.calc(df) if df is not None else None
        elif key == "bb_volume":
            df = _bbv.fetch(symbol, fetch_days)
            df = _bbv.calc(df) if df is not None else None
        elif key == "limit_oco":
            df = _oco.fetch(symbol, fetch_days)
            df = _oco.calc(df) if df is not None else None
        else:
            return None

        if df is None or len(df) == 0:
            return None

        if target_date is not None:
            df = df.loc[df.index <= pd.Timestamp(target_date)]
            if len(df) == 0:
                return None

        return df
    except Exception as e:
        print(f"  ※ {symbol} [{key}] fetch/calc エラー: {e}", file=sys.stderr)
    return None


def _check_signal(df: pd.DataFrame, key: str) -> dict | None:
    try:
        if key == "adaptive_mr":
            return _amr._has_signal_today(df)
        elif key == "strong_pullback":
            return _sp._has_signal_today(df)
        elif key == "donchian":
            return _don._has_signal_today(df)
        elif key == "bb_volume":
            return _bbv._has_signal_today(df)
        elif key == "limit_oco":
            return _oco._has_signal_today(df, _oco.DEFAULT_PARAMS)
    except Exception:
        pass
    return None


def _normalize(key: str, sig: dict) -> dict:
    entry  = sig.get("limit_price", 0.0)
    stop   = sig.get("stop_loss") or sig.get("stop", 0.0)
    target = sig.get("profit_target") or sig.get("target", 0.0)
    atr    = sig.get("atr", 0.0)
    if target == 0.0 and atr > 0:
        target = entry + atr * 3.0
    expire = sig.get("expire_days", 3)
    if isinstance(expire, list):
        expire = max(expire)

    if key == "adaptive_mr":
        note = f"RSI2={sig.get('rsi2',0):.1f}  IBS={sig.get('ibs',float('nan')):.2f}  レジーム={sig.get('regime','—')}"
    elif key == "strong_pullback":
        note = f"RSI5={sig.get('rsi5',0):.1f}"
    elif key == "donchian":
        vr = sig.get("vol_ratio", float("nan"))
        note = f"DC高値={sig.get('don_high',0):.0f}" + (f"  出来高比={vr:.1f}x" if not math.isnan(vr) else "")
    elif key == "bb_volume":
        vr = sig.get("vol_ratio", float("nan"))
        note = f"BB下限={sig.get('bb_lower',0):.0f}  BB中心={sig.get('bb_mid',0):.0f}" + (f"  出来高比={vr:.1f}x" if not math.isnan(vr) else "")
    elif key == "limit_oco":
        note = f"RSI2={sig.get('rsi2',0):.1f}  IBS={sig.get('ibs',0):.2f}"
    else:
        note = ""

    return dict(key=key, close=sig["close"], entry=entry, stop=stop,
                target=target, atr=atr, note=note, expire=expire)


def _collect_signals(wl: list, target_date: date | None) -> tuple[list, list]:
    """シグナルあり / なし を収集して返す。"""
    today = datetime.now(JST).date()
    ref   = target_date or today
    fetch_days = (today - ref).days + 365 + 60  # 過去日の場合は多めに確保

    active:  list[tuple[str, str, dict]] = []
    waiting: list[tuple[str, str]]       = []

    for symbol, name, strategies in wl:
        hits = []
        for key in strategies:
            df  = _fetch_and_calc(symbol, key, fetch_days, target_date)
            if df is None:
                continue
            sig = _check_signal(df, key)
            if sig:
                hits.append(_normalize(key, sig))
        if hits:
            for h in hits:
                active.append((symbol, name, h))
        else:
            waiting.append((symbol, name))

    return active, waiting


# ─────────────────────────────────────────────────────────────────────
# HTML 生成
# ─────────────────────────────────────────────────────────────────────

_CSS = """
body{font-family:'Meiryo',sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:20px}
h1{font-size:1.2em;color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:8px;margin-bottom:16px}
h2{font-size:1.0em;color:#8b949e;margin:24px 0 8px}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:.75em;font-weight:bold;border:1px solid}
table{border-collapse:collapse;width:100%;font-size:.82em;margin-top:4px}
th{background:#161b22;color:#8b949e;padding:7px 12px;border:1px solid #30363d;text-align:right;white-space:nowrap}
th:first-child,th:nth-child(2),th:nth-child(3){text-align:left}
td{padding:6px 12px;border:1px solid #21262d;text-align:right;vertical-align:top}
td:first-child,td:nth-child(2),td:nth-child(3){text-align:left}
tr:nth-child(even){background:#0d1117}
tr:nth-child(odd){background:#161b22}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.note{font-size:.78em;color:#8b949e;margin-top:2px}
.waiting td{color:#4b5563}
"""


def _rr_color(v: float | None) -> str:
    if v is None:
        return "neu"
    return "up" if v >= 2.0 else ("dn" if v < 1.0 else "neu")


def build_signal_html(active: list, waiting: list,
                      target_date: date | None, preset_name: str) -> str:
    ref_label = str(target_date) if target_date else datetime.now(JST).strftime("%Y-%m-%d")
    now_str   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    # ── シグナルあり テーブル ────────────────────────────────────
    if active:
        rows = ""
        for sym, nm, r in active:
            strat  = STRATEGY_NAMES.get(r["key"], r["key"])
            color  = STRATEGY_COLOR.get(r["key"], "#94a3b8")
            risk   = r["entry"] - r["stop"]
            rr_val = _rr_float(r["entry"], r["stop"], r["target"])
            rr_cls = _rr_color(rr_val)
            rr_str = f"{rr_val:.1f}R" if rr_val is not None else "—"
            rows += f"""
<tr>
  <td>{sym}</td>
  <td>{nm}</td>
  <td><span class="badge" style="color:{color};border-color:{color}">{strat}</span></td>
  <td>¥{r['close']:,.0f}</td>
  <td>¥{r['entry']:,.0f}<div class="note">{_pct(r['entry'], r['close'])}</div></td>
  <td class="dn">¥{r['stop']:,.0f}<div class="note">リスク ¥{risk:,.0f} / ¥{risk*LOT:,.0f}</div></td>
  <td class="up">¥{r['target']:,.0f}</td>
  <td class="{rr_cls}">{rr_str}</td>
  <td>{r['expire']}営業日</td>
  <td class="note" style="text-align:left">{r['note']}</td>
</tr>"""

        signal_block = f"""
<h2>▶ シグナル発生 {len(active)}件</h2>
<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>戦略</th><th>現在値</th>
  <th>指値（エントリー）</th><th>損切</th><th>目標</th><th>RR</th>
  <th>有効期限</th><th>指標メモ</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
    else:
        signal_block = "<h2>▶ シグナル発生なし</h2><p style='color:#4b5563;padding:8px 0'>条件を満たす銘柄はありませんでした。</p>"

    # ── 待機中テーブル ───────────────────────────────────────────
    wait_rows = "".join(
        f"<tr class='waiting'><td>{sym}</td><td>{nm}</td></tr>"
        for sym, nm in waiting
    )
    wait_block = f"""
<h2>▷ 待機中（シグナルなし） {len(waiting)}銘柄</h2>
<table style="width:auto">
<thead><tr><th>コード</th><th>銘柄名</th></tr></thead>
<tbody>{wait_rows}</tbody>
</table>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>シグナルチェック {ref_label}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>監視銘柄シグナルチェック
  <span style="font-size:.8em;color:#8b949e">
    判定日: {ref_label} &nbsp;|&nbsp; プリセット: {preset_name} &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>
<p style="font-size:.8em;color:#4b5563;margin-bottom:16px">
  ※ 指値注文は翌営業日の寄り付き前に設定 &nbsp;/&nbsp;
  損切は同時に逆指値注文（OCO推奨）&nbsp;/&nbsp;
  有効期限内に未約定はキャンセル
</p>
{signal_block}
{wait_block}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# 今日（または指定日）のシグナル確認
# ─────────────────────────────────────────────────────────────────────

def check_today(target_date: date | None, verbose: bool,
                watchlist: list | None, preset_name: str,
                no_browser: bool) -> None:
    wl  = watchlist if watchlist is not None else WATCHLIST
    ref = target_date or datetime.now(JST).date()

    print(f"\n{'='*72}")
    print(f"  シグナルチェック  判定日: {ref}  ({len(wl)}銘柄)")
    print(f"{'='*72}\n")

    active, waiting = _collect_signals(wl, target_date)

    # ── ターミナル出力 ───────────────────────────────────────────
    if active:
        print(f"【シグナル発生】 {len(active)}件")
        print("─" * 72)
        for sym, nm, r in active:
            strat = STRATEGY_NAMES.get(r["key"], r["key"])
            risk  = r["entry"] - r["stop"]
            print(f"  ★ {sym}  {nm}")
            print(f"     戦略   : {strat}")
            print(f"     現在値 : ¥{r['close']:,.0f}")
            print(f"     指値   : ¥{r['entry']:,.0f}  ({_pct(r['entry'], r['close'])})")
            print(f"     損切   : ¥{r['stop']:,.0f}  (リスク ¥{risk:,.0f} / ロット ¥{risk*LOT:,.0f})")
            print(f"     目標   : ¥{r['target']:,.0f}  RR={_rr(r['entry'], r['stop'], r['target'])}")
            print(f"     期限   : {r['expire']}営業日")
            if verbose:
                print(f"     指標   : {r['note']}")
            print()
    else:
        print("【シグナル発生なし】\n")

    if waiting:
        print("【待機中】 " + "  ".join(f"{s}" for s, _ in waiting))
        print()

    # ── HTML出力 ────────────────────────────────────────────────
    html   = build_signal_html(active, waiting, target_date, preset_name)
    ref_str = str(ref)
    outpath = Path(f"signals_{ref_str}.html")
    outpath.write_text(html, encoding="utf-8")
    print(f"HTML: {outpath.resolve()}")

    if not no_browser:
        webbrowser.open(f"file://{outpath.resolve()}")

    print(f"{'='*72}\n")


# ─────────────────────────────────────────────────────────────────────
# 過去期間のシグナル発生回数
# ─────────────────────────────────────────────────────────────────────

def check_history(from_date: date, to_date: date, verbose: bool,
                  watchlist: list | None, no_browser: bool = False) -> None:
    wl         = watchlist if watchlist is not None else WATCHLIST
    today      = datetime.now(JST).date()
    fetch_days = (today - from_date).days + 365 + 60
    period_days = (to_date - from_date).days + 1

    print(f"\n{'='*80}")
    print(f"  過去シグナル発生回数")
    print(f"  集計期間: {from_date} 〜 {to_date}  ({period_days}日間)")
    print(f"  対象: {len(wl)}銘柄")
    print(f"{'='*80}\n")

    W = (10, 22, 13, 6)
    hdr = f"{'コード':<{W[0]}}  {'銘柄名':<{W[1]}}  {'戦略':<{W[2]}}  {'回数':>{W[3]}}  発生日（直近5件）"
    print("  " + hdr)
    print("  " + "─" * len(hdr))

    # 集計
    rows_data: list[dict] = []
    total = 0
    for symbol, name, strategies in wl:
        for key in strategies:
            df = _fetch_and_calc(symbol, key, fetch_days)
            strat = STRATEGY_NAMES.get(key, key)
            if df is None:
                print(f"  {symbol:<{W[0]}}  {name:<{W[1]}}  {strat:<{W[2]}}  {'—':>{W[3]}}  データ取得失敗")
                rows_data.append(dict(symbol=symbol, name=name, strat=strat,
                                      count=0, signal_dates=[], error=True))
                continue

            idx_dates = [(i, (d.date() if hasattr(d, "date") else d))
                         for i, d in enumerate(df.index)]
            signal_dates: list[str] = []
            for i, d in idx_dates:
                if d < from_date:
                    continue
                if d > to_date:
                    break
                if i < 50:
                    continue
                if _check_signal(df.iloc[:i + 1], key):
                    signal_dates.append(str(d))

            count = len(signal_dates)
            total += count
            recent_str = "  ".join(signal_dates[-5:]) if signal_dates else "—"
            print(f"  {symbol:<{W[0]}}  {name:<{W[1]}}  {strat:<{W[2]}}  {count:>{W[3]}}回  {recent_str}")

            if verbose and len(signal_dates) > 5:
                print(f"  {'':>{W[0]}}  {'':>{W[1]}}  全発生日: {'  '.join(signal_dates)}")

            rows_data.append(dict(symbol=symbol, name=name, strat=strat,
                                  count=count, signal_dates=signal_dates, error=False))
        print()

    print("─" * 80)
    avg_per_month = total / (period_days / 30) if period_days > 0 else 0
    print(f"  合計: {total}回  月平均: {avg_per_month:.1f}回")
    print(f"{'='*80}\n")

    # ── HTML 生成 ────────────────────────────────────────────────
    now_str  = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    rows_data_sorted = sorted(rows_data, key=lambda r: -r["count"])
    WD = ["月","火","水","木","金","土","日"]

    # 銘柄別ブロック（<details> で展開）
    stock_blocks = ""
    for r in rows_data_sorted:
        color   = STRATEGY_COLOR.get(
            next((k for k, v in STRATEGY_NAMES.items() if v == r["strat"]), ""), "#94a3b8"
        )
        cnt_cls = "up" if r["count"] >= 3 else ("neu" if r["count"] >= 1 else "dn")

        # 詳細テーブル（発生日 × 曜日）
        detail_rows = ""
        for i, ds in enumerate(r["signal_dates"], 1):
            d_obj = date.fromisoformat(ds)
            wd    = WD[d_obj.weekday()]
            detail_rows += (
                f"<tr>"
                f"<td style='color:#8b949e;width:2em'>{i}</td>"
                f"<td>{ds}</td>"
                f"<td style='color:#8b949e'>{wd}曜日</td>"
                f"</tr>"
            )
        if not detail_rows:
            detail_rows = "<tr><td colspan='3' style='color:#4b5563'>発生なし</td></tr>"

        stock_blocks += f"""
<details class="stock-row">
  <summary>
    <span class="s-sym">{r['symbol']}</span>
    <span class="s-name">{r['name']}</span>
    <span class="badge" style="color:{color};border-color:{color}">{r['strat']}</span>
    <span class="s-count {cnt_cls}">{r['count']}回</span>
    <span class="s-hint">▶ クリックで全発生日</span>
  </summary>
  <div class="s-detail">
    <table class="s-dtable">
      <thead><tr><th>#</th><th>発生日</th><th>曜日</th></tr></thead>
      <tbody>{detail_rows}</tbody>
    </table>
  </div>
</details>"""

    # 月次カレンダー（全銘柄合算）
    all_dates_agg: dict[str, int] = {}
    for r in rows_data:
        for d in r["signal_dates"]:
            all_dates_agg[d] = all_dates_agg.get(d, 0) + 1

    cal_html = _build_calendar_heatmap(all_dates_agg, from_date, to_date)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>シグナル発生頻度 {from_date}〜{to_date}</title>
<style>
{_CSS}
.up{{color:#3fb950}}.dn{{color:#f85149}}.neu{{color:#8b949e}}
/* カレンダー */
.cal-wrap{{display:flex;flex-wrap:wrap;gap:24px;margin-bottom:24px}}
.cal-month{{min-width:220px}}
.cal-month h3{{font-size:.9em;color:#8b949e;margin:0 0 6px;font-weight:600}}
.cal-grid{{display:grid;grid-template-columns:repeat(7,34px);gap:2px}}
.cal-wh{{height:22px;display:flex;align-items:center;justify-content:center;
         font-size:.72em;color:#4b5563;font-weight:600}}
.cal-cell{{height:38px;border-radius:5px;display:flex;flex-direction:column;
           align-items:center;justify-content:center;cursor:default;position:relative}}
.cal-cell.empty{{background:transparent!important}}
.cal-dnum{{font-size:.65em;color:rgba(255,255,255,.45);line-height:1.1}}
.cal-cnt{{font-size:.85em;font-weight:700;line-height:1.1}}
/* 銘柄ブロック */
.stock-row{{margin-bottom:4px;border:1px solid #21262d;border-radius:6px;overflow:hidden}}
.stock-row summary{{
  display:flex;align-items:center;gap:12px;padding:10px 14px;
  cursor:pointer;list-style:none;background:#161b22;user-select:none}}
.stock-row summary::-webkit-details-marker{{display:none}}
.stock-row[open] summary{{background:#1c2128;border-bottom:1px solid #21262d}}
.stock-row:hover summary{{background:#1c2128}}
.s-sym{{font-family:monospace;font-size:.88em;color:#8b949e;width:70px;flex-shrink:0}}
.s-name{{flex:1;font-weight:600}}
.s-count{{font-size:1.15em;font-weight:700;width:50px;text-align:right}}
.s-hint{{font-size:.75em;color:#4b5563;margin-left:4px}}
.s-detail{{padding:12px 16px;background:#0d1117}}
.s-dtable{{border-collapse:collapse;width:auto;min-width:280px}}
.s-dtable th{{background:#161b22;color:#8b949e;padding:5px 14px;text-align:left;
              border-bottom:1px solid #21262d;font-size:.82em}}
.s-dtable td{{padding:4px 14px;border-bottom:1px solid #161b22;font-size:.88em}}
.s-dtable tr:last-child td{{border-bottom:none}}
</style>
</head>
<body>
<h1>シグナル発生頻度
  <span style="font-size:.75em;color:#8b949e">
    {from_date} 〜 {to_date}（{period_days}日間） &nbsp;|&nbsp;
    {len(wl)}銘柄 &nbsp;|&nbsp; 合計{total}回 / 月平均{avg_per_month:.1f}回 &nbsp;|&nbsp;
    生成: {now_str}
  </span>
</h1>

<h2>▶ 発生カレンダー（色が濃いほど多い / 数字 = 銘柄数）</h2>
{cal_html}

<h2>▶ 銘柄別 発生回数（多い順 / クリックで発生日を展開）</h2>
{stock_blocks}
</body>
</html>"""

    out_path = Path(f"signal_history_{from_date}_{to_date}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"HTML: {out_path.resolve()}")
    if not no_browser:
        webbrowser.open(f"file://{out_path.resolve()}")


def _build_calendar_heatmap(all_dates: dict[str, int],
                             from_date: date, to_date: date) -> str:
    """日付ごとのシグナル数をカレンダーヒートマップで返す（週グリッド形式）。"""
    import calendar as cal_mod

    max_count = max(all_dates.values()) if all_dates else 1

    def bg_color(n: int) -> str:
        if n == 0:
            return "#161b22"
        intensity = min(n / max(max_count, 1), 1.0)
        g = int(80 + intensity * 175)
        return f"rgb(0,{g},60)"

    WD_LABELS = ["月","火","水","木","金","土","日"]

    # 月ごとにグループ化（全日 = 範囲外も含む）
    months: dict[tuple, list[date]] = {}
    cur = from_date.replace(day=1)
    while cur <= to_date.replace(day=1):
        key = (cur.year, cur.month)
        _, last_day = cal_mod.monthrange(cur.year, cur.month)
        months[key] = [date(cur.year, cur.month, d) for d in range(1, last_day + 1)]
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)

    MN = ["1月","2月","3月","4月","5月","6月",
          "7月","8月","9月","10月","11月","12月"]

    html = "<div class='cal-wrap'>"
    for (year, month), all_days in months.items():
        mn = MN[month - 1]
        # 曜日ヘッダー
        grid = "".join(f"<div class='cal-wh'>{w}</div>" for w in WD_LABELS)
        # 月の1日の曜日オフセット
        first_dow = all_days[0].weekday()
        grid += "<div class='cal-cell empty'></div>" * first_dow
        for d in all_days:
            in_range = (from_date <= d <= to_date)
            n   = all_dates.get(str(d), 0) if in_range else 0
            bg  = bg_color(n) if in_range else "#0d1117"
            tip = f"{d}: {n}銘柄" if (in_range and n > 0) else str(d)
            cnt_html = (f"<span class='cal-cnt' style='color:#fff'>{n}</span>"
                        if n > 0 else "")
            opacity = "1" if in_range else "0.3"
            grid += (
                f"<div class='cal-cell' style='background:{bg};opacity:{opacity}'"
                f" title='{tip}'>"
                f"<span class='cal-dnum'>{d.day}</span>"
                f"{cnt_html}"
                f"</div>"
            )
        html += f"<div class='cal-month'><h3>{year}年 {mn}</h3><div class='cal-grid'>{grid}</div></div>"
    html += "</div>"
    return html


# ─────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="監視銘柄シグナルチェック",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python check_signals.py                                  # 今日のシグナル（HTML自動表示）
  python check_signals.py --date 2026-03-28                # 指定日のシグナル
  python check_signals.py --preset core                    # 元の8銘柄
  python check_signals.py --preset core --date 2026-03-28  # 8銘柄×指定日
  python check_signals.py --no-browser                     # HTML生成のみ
  python check_signals.py --history --days 90              # 過去90日の発生回数
  python check_signals.py --history --from 2025-10-01 --to 2026-03-31
  python check_signals.py --list                           # 登録銘柄一覧
""")
    preset_choices = list(PRESETS.keys())
    parser.add_argument("--preset",     "-p", choices=preset_choices, default="all",
                        help=f"銘柄グループ: {' / '.join(preset_choices)}（デフォルト: all）")
    parser.add_argument("--date",             metavar="YYYY-MM-DD",
                        help="シグナル判定日（省略時=今日）")
    parser.add_argument("--no-browser",       action="store_true",
                        help="HTMLを生成するがブラウザは起動しない")
    parser.add_argument("--verbose",    "-v", action="store_true", help="詳細表示")
    parser.add_argument("--history",          action="store_true", help="過去期間集計モード")
    parser.add_argument("--days",       type=int, default=90,
                        help="過去N日を集計（--history時）")
    parser.add_argument("--from",  dest="from_date", metavar="YYYY-MM-DD")
    parser.add_argument("--to",    dest="to_date",   metavar="YYYY-MM-DD")
    parser.add_argument("--list",             action="store_true",
                        help="登録銘柄一覧を表示して終了")
    args = parser.parse_args()

    if args.list:
        for pname, pcodes in PRESETS.items():
            wl_p = [r for r in WATCHLIST if r[0] in pcodes] if pcodes else WATCHLIST
            print(f"\n【{pname}】 {len(wl_p)}銘柄")
            print("─" * 60)
            for sym, nm, strats in wl_p:
                print(f"  {sym:<10}  {nm:<24}  "
                      f"{'  +  '.join(STRATEGY_NAMES.get(s,s) for s in strats)}")
        print()
        return

    codes = PRESETS.get(args.preset, [])
    wl    = [r for r in WATCHLIST if r[0] in codes] if codes else None

    if args.history:
        today = datetime.now(JST).date()
        to_dt = date.fromisoformat(args.to_date) if args.to_date else today
        from_dt = (date.fromisoformat(args.from_date) if args.from_date
                   else to_dt - timedelta(days=args.days))
        check_history(from_dt, to_dt, args.verbose, wl, args.no_browser)
    else:
        target = date.fromisoformat(args.date) if args.date else None
        check_today(target, args.verbose, wl, args.preset, args.no_browser)


if __name__ == "__main__":
    main()
