"""
run_signals.py  ―  逆指値シグナル統合レポート
==================================================
check_signals_stop.py（MACD / A7 / RSI2 逆指値B） +
check_signals_breakout.py（DON / VOL / MOM ブレイクアウト） +
check_signals_short.py（MACD_S / A7_S / RSI2_S ショート逆指値）
を1コマンドで実行し、タブ付きHTMLに統合する。

【使い方】
  python run_signals.py                    # 全期間(365日) HTMLレポート (conservative)
  python run_signals.py --days 90          # 直近90日
  python run_signals.py --date 2026-04-08  # 任意日シグナル確認
  python run_signals.py --signal-only      # シグナルのみ表示
  python run_signals.py --no-browser       # HTML生成のみ（ブラウザ起動しない）
  python run_signals.py --aggressive       # 積極利確モード (tm=1.5 目標+4.5%)

※ デフォルトは conservative モード (tm=3.0 目標+9%)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# ── 相場環境フィルター用マッピング ────────────────────────────────────────────
_REGIME_STRATEGY_MAP: dict[tuple, set] = {
    ("up",       "high"):     {"DON", "VOL", "MACD"},
    ("up",       "mid"):      {"MACD", "DON", "VOL", "MOM", "A7"},
    ("up",       "low"):      {"MACD", "MOM", "A7"},
    ("sideways", "high"):     {"RSI2", "A7"},
    ("sideways", "mid"):      {"MACD", "A7", "RSI2"},
    ("sideways", "low"):      {"MACD", "A7", "MOM"},
    ("down",     "high"):     {"RSI2"},
    ("down",     "mid"):      {"RSI2", "A7"},
    ("down",     "low"):      {"RSI2", "A7"},
}

def _detect_and_filter_by_regime() -> str:
    """相場環境を検出してWATCHLISTを適合戦略のみに絞り込む。説明文字列を返す。"""
    try:
        import pandas as pd
        import yfinance as yf
        df = yf.download("^N225", period="2mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 25:
            return "相場データ取得失敗 (フィルターなし)"
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        vol   = float(close.pct_change().tail(14).std() * 100)
        ma10  = float(close.rolling(10).mean().iloc[-1])
        ma25  = float(close.rolling(25).mean().iloc[-1])
        cur   = float(close.iloc[-1])
        vol_level = "high" if vol > 1.5 else ("mid" if vol > 0.8 else "low")
        vol_label = {"high": "高ボラ", "mid": "中ボラ", "low": "低ボラ"}[vol_level]
        if cur > ma10 and ma10 > ma25:
            trend, trend_label = "up", "上昇"
        elif cur < ma10 and ma10 < ma25:
            trend, trend_label = "down", "下落"
        else:
            trend, trend_label = "sideways", "レンジ"
    except Exception as e:
        return f"相場データ取得失敗: {e}"

    allowed = _REGIME_STRATEGY_MAP.get((trend, vol_level), set())
    if not allowed:
        return f"相場環境: {vol_label}/{trend_label} (対応マッピングなし)"

    before_stop = len(_stop.WATCHLIST)
    before_brk  = len(_brk.WATCHLIST)
    before_srt  = len(_short.WATCHLIST)
    before_sbrk = len(_sbrk.WATCHLIST)
    _stop.WATCHLIST  = [(s, n, st) for s, n, st in _stop.WATCHLIST  if st in allowed]
    _brk.WATCHLIST   = [(s, n, st) for s, n, st in _brk.WATCHLIST   if st in allowed]
    _short.WATCHLIST = [(s, n, st) for s, n, st in _short.WATCHLIST if st in allowed]
    _sbrk.WATCHLIST  = [(s, n, st) for s, n, st in _sbrk.WATCHLIST  if st in allowed]
    after = len(_stop.WATCHLIST) + len(_brk.WATCHLIST) + len(_short.WATCHLIST) + len(_sbrk.WATCHLIST)
    total_before = before_stop + before_brk + before_srt + before_sbrk
    strats_str = " / ".join(sorted(allowed))
    return (f"相場環境: {vol_label}/{trend_label} → 適合戦略: {strats_str} "
            f"({total_before}→{after}銘柄)")
from pathlib import Path

# ── TRADING_MODE を import 前に設定 (check_signals_* が読み取る) ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop            as _stop
import check_signals_breakout        as _brk
import check_signals_short           as _short
import check_signals_short_breakout  as _sbrk
from _signal_funds import collect_fund_rows, fund_html as _fund_html, filter_items

JST = timezone(timedelta(hours=9))


# ── グループ単位でバックテスト + シグナル確認 ────────────────────────────
def _run_group(mod, sig_date, workers: int) -> list[dict]:
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in mod.WATCHLIST}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass

    order = {(s, st): i for i, (s, _, st) in enumerate(mod.WATCHLIST)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)

        # 今日シグナルなし かつ 本日モード のとき:
        # ENTRY_EXPIRE 日分遡って未約定/保有中シグナルを探す（取引詳細の記録表示用）
        if not item["today_sig"] and sig_date is None:
            import pandas as _pd
            from backtest_limit_entry import fetch as _fetch_bt
            today_d = datetime.now(JST).date()
            bdays = _pd.bdate_range(
                end=_pd.Timestamp(today_d), periods=_stop.ENTRY_EXPIRE + 1
            )[:-1]  # 今日を除く直近 ENTRY_EXPIRE 営業日
            for bday in reversed(bdays):
                past_sig = mod.check_signal_on_date(
                    item["symbol"], item["strategy"], bday.date())
                if not past_sig:
                    continue
                # 逆指値が約定済みか判定: シグナル日以降の高値/安値を確認
                is_short = item["strategy"].endswith("_S")
                order_p  = past_sig["order_price"]
                sig_ts   = _pd.Timestamp(past_sig["signal_date"])
                df_recent = _fetch_bt(item["symbol"], 30)
                filled = False
                if df_recent is not None:
                    future_bars = df_recent[df_recent.index > sig_ts]
                    if len(future_bars) > 0:
                        filled = (future_bars["low"].min()  <= order_p if is_short
                                  else future_bars["high"].max() >= order_p)
                    # 最新終値を current_price に更新
                    past_sig["current_price"] = float(df_recent.iloc[-1]["close"])
                if filled:
                    # フィル日・エントリー価格・含み損益を計算して渡す
                    from backtest_limit_entry import (
                        SLIPPAGE_STOP_PCT as _slip, FIXED_QTY as _qty)
                    entry_p = round(order_p * (1 + _slip if not is_short else 1 - _slip), 0)
                    current_latest = float(df_recent.iloc[-1]["close"]) if df_recent is not None else order_p
                    unreal_pnl = ((entry_p - current_latest) if is_short
                                  else (current_latest - entry_p)) * _qty
                    unreal_pct = (((entry_p - current_latest) / entry_p)
                                  if is_short
                                  else ((current_latest - entry_p) / entry_p)) * 100
                    fill_idx = None
                    if df_recent is not None:
                        for idx, row in future_bars.iterrows():
                            if (row["low"] <= order_p if is_short
                                    else row["high"] >= order_p):
                                fill_idx = idx
                                break
                    fill_date_str = fill_idx.strftime("%Y-%m-%d") if fill_idx is not None else "-"
                    today_ts = _pd.Timestamp(today_d)
                    hold_days = (len(_pd.bdate_range(start=fill_idx, end=today_ts)) - 1
                                 if fill_idx is not None else 0)
                    fill_days = (len(_pd.bdate_range(start=sig_ts,   end=fill_idx)) - 1
                                 if fill_idx is not None else 0)
                    past_sig.update({
                        "_filled_holding":      True,
                        "_fill_date":           fill_date_str,
                        "_entry_price":         entry_p,
                        "_current_latest":      current_latest,
                        "_unreal_pnl":          unreal_pnl,
                        "_unreal_pct":          unreal_pct,
                        "_hold_days":           hold_days,
                        "_fill_days":           fill_days,
                        "_qty":                 _qty,
                    })
                else:
                    past_sig["_pending_lookback"] = True  # 未約定・継続中
                item["today_sig"] = past_sig
                break

    return all_items


# ── 統合タブHTML生成 ─────────────────────────────────────────────────────
def _extract_body(html: str) -> str:
    m = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else html


def _auto_update_regime_cache(workers: int = 4) -> None:
    """regime_cache.json が今日付でなければ regime_verify.py を自動実行する。"""
    import json
    import subprocess
    cache_path = Path(__file__).with_name("regime_cache.json")
    today_str  = datetime.now(JST).strftime("%Y-%m-%d")
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("updated") == today_str:
            return   # 今日付のキャッシュがある → スキップ
    except Exception:
        pass

    print(f"[相場環境] キャッシュが古いため regime_verify.py を実行します...", flush=True)
    rv_path = Path(__file__).with_name("regime_verify.py")
    result  = subprocess.run(
        [sys.executable, str(rv_path), "--no-browser", f"--workers={workers}"],
        text=True,
    )
    if result.returncode != 0:
        print("[WARN] regime_verify.py の実行に失敗しました（バナーは理論値のみ表示）", flush=True)
    print(flush=True)


def get_regime_html() -> str:
    """現在の相場環境と適合戦略をHTMLバナーとして返す（シグナルHTML先頭に挿入）"""
    import json
    _STRATS  = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    _THEORY  = {
        "up_high":       {"DON", "VOL", "MACD"},
        "up_mid":        {"MACD", "DON", "VOL", "MOM", "A7"},
        "up_low":        {"MACD", "MOM", "A7"},
        "sideways_high": {"RSI2", "A7"},
        "sideways_mid":  {"MACD", "A7", "RSI2"},
        "sideways_low":  {"MACD", "A7", "MOM"},
        "down_high":     {"RSI2"},
        "down_mid":      {"RSI2", "A7"},
        "down_low":      {"RSI2", "A7"},
    }
    try:
        import yfinance as yf
        df = yf.download("^N225", period="2mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 25:
            return ""
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        vol   = float(close.pct_change().tail(14).std() * 100)
        ma10  = float(close.rolling(10).mean().iloc[-1])
        ma25  = float(close.rolling(25).mean().iloc[-1])
        cur   = float(close.iloc[-1])
        mom5  = (cur / float(close.iloc[-6]) - 1) * 100
        vol_lv = "high" if vol > 1.5 else ("mid" if vol > 0.8 else "low")
        if cur > ma10 and ma10 > ma25:
            trend, t_lbl = "up",       "上昇"
        elif cur < ma10 and ma10 < ma25:
            trend, t_lbl = "down",     "下落"
        else:
            trend, t_lbl = "sideways", "レンジ"
    except Exception:
        return ""

    t_col  = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[trend]
    v_col  = {"high": "#f87171", "mid": "#fbbf24", "low": "#4ade80"}[vol_lv]
    v_lbl  = {"high": "高ボラ",  "mid": "中ボラ",  "low": "低ボラ"}[vol_lv]
    m_col  = "#4ade80" if mom5 >= 0 else "#f87171"
    rkey   = f"{trend}_{vol_lv}"
    good   = _THEORY.get(rkey, set())

    # キャッシュから実績PF取得
    strat_html = ""
    cache_note = ""
    try:
        cache = json.loads(Path(__file__).with_name("regime_cache.json").read_text(encoding="utf-8"))
        rd    = cache.get("regimes", {}).get(rkey, {})
        parts = []
        for s in _STRATS:
            d = rd.get(s)
            if not d or d["n"] < 3:
                continue
            pf_v   = d["pf"]
            pf_s   = f"{pf_v:.2f}" if pf_v < 99 else "∞"
            is_good = s in good
            if is_good and pf_v >= 1.2:
                ic, badge = "#4ade80", "✅"
            elif is_good and pf_v < 1.0:
                ic, badge = "#f87171", "⚠️"
            elif not is_good and pf_v < 0.8:
                ic, badge = "#f87171", "❌"
            else:
                ic, badge = "#94a3b8", "　"
            parts.append(
                f'<span style="margin-right:14px;white-space:nowrap">'
                f'{badge} <b style="color:{ic}">{s}</b> '
                f'<span style="color:#64748b;font-size:0.78em">'
                f'勝率{d["wr"]:.0f}% PF{pf_s}</span></span>')
        strat_html = f'<div style="margin-top:7px">{"".join(parts)}</div>'
        cache_note = (f'<div style="font-size:0.7rem;color:#334155;margin-top:4px">'
                      f'実績: regime_cache.json ({cache.get("updated","")}) — '
                      f'更新: <code style="color:#38bdf8">python regime_verify.py</code></div>')
    except Exception:
        if good:
            g_s = " / ".join(f'<b style="color:#4ade80">{s}</b>' for s in _STRATS if s in good)
            b_s = " / ".join(f'<span style="color:#64748b">{s}</span>' for s in _STRATS if s not in good)
            strat_html = (f'<div style="margin-top:7px">'
                          f'✅ 推奨: {g_s} &nbsp;&nbsp; ⚠️ 注意: {b_s}</div>')
            cache_note = (f'<div style="font-size:0.7rem;color:#334155;margin-top:4px">'
                          f'実績データなし — '
                          f'<code style="color:#38bdf8">python regime_verify.py</code> を実行してください</div>')

    return (
        f'<div style="background:#0d1424;border:1px solid #1e3a5f;'
        f'border-left:4px solid #38bdf8;border-radius:8px;'
        f'padding:12px 16px;margin:12px 16px 0;font-family:inherit">'
        f'<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">'
        f'<span style="font-weight:700;color:#38bdf8">📊 現在の相場環境</span>'
        f'<span><b style="color:{v_col}">{v_lbl}</b>'
        f' <span style="color:#334155">/</span> '
        f'<b style="color:{t_col}">{t_lbl}</b></span>'
        f'<span style="font-size:0.82rem;color:#475569">'
        f'日経225: {cur:,.0f}円 &nbsp;'
        f'5日騰落: <span style="color:{m_col}">{mom5:+.2f}%</span></span>'
        f'</div>'
        f'{strat_html}'
        f'{cache_note}'
        f'</div>'
    )


def _extract_style(html: str) -> str:
    m = re.search(r'<style[^>]*>(.*?)</style>', html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def build_combined_html(stop_html: str, brk_html: str, srt_html: str = "",
                        sbrk_html: str = "", fund_html_block: str = "") -> str:
    today_str  = datetime.now(JST).strftime("%Y-%m-%d")
    stop_css   = _extract_style(stop_html)
    brk_css    = _extract_style(brk_html)
    srt_css    = _extract_style(srt_html)  if srt_html  else ""
    sbrk_css   = _extract_style(sbrk_html) if sbrk_html else ""
    stop_body  = _extract_body(stop_html)
    brk_body   = _extract_body(brk_html)
    srt_body   = _extract_body(srt_html)   if srt_html  else ""
    sbrk_body  = _extract_body(sbrk_html)  if sbrk_html else ""

    # 各モジュール固有のタグ色を追加（stop_css には含まれないもの）
    extra_css = "\n".join(
        line for line in (brk_css + "\n" + srt_css + "\n" + sbrk_css).splitlines()
        if any(k in line for k in ("tag-don", "tag-vol", "tag-mom",
                                   "tag-macd_s", "tag-a7_s", "tag-rsi2_s",
                                   "tag-don_s", "tag-mom_s", "tag-gap_s"))
    )

    srt_tab_btn   = '<button class="tab-btn" onclick="switchTab(2)">ショート逆指値（A7_S）</button>'  if srt_html  else ""
    srt_tab_pane  = f'<div id="tc2" class="tab-pane" style="display:none">\n{srt_body}\n</div>'       if srt_html  else ""
    sbrk_tab_btn  = '<button class="tab-btn" onclick="switchTab(3)">ショートBRK（DON_S/MOM_S/GAP_S）</button>' if sbrk_html else ""
    sbrk_tab_pane = f'<div id="tc3" class="tab-pane" style="display:none">\n{sbrk_body}\n</div>'      if sbrk_html else ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>逆指値シグナル統合レポート — {today_str}</title>
<style>
{stop_css}
{extra_css}
.tab-nav{{display:flex;gap:0;background:#090b14;padding:10px 16px 0;border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;flex-wrap:wrap}}
.tab-btn{{background:#16192a;color:#94a3b8;border:1px solid #252840;border-bottom:none;padding:9px 22px;margin-right:4px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px;font-family:inherit;transition:background .15s,color .15s}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{background:#0f1117;color:#38bdf8;border-color:#38bdf8;border-bottom-color:#0f1117}}
.tab-pane{{min-height:60vh}}
</style>
</head>
<body>
{get_regime_html()}
{fund_html_block}
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">ブレイクアウト（DON / VOL / MOM）</button>
  {srt_tab_btn}
  {sbrk_tab_btn}
</div>
<div id="tc0" class="tab-pane">
{stop_body}
</div>
<div id="tc1" class="tab-pane" style="display:none">
{brk_body}
</div>
{srt_tab_pane}
{sbrk_tab_pane}
<script>
function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{b.classList.toggle('active',i===n);}});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{t.style.display=i===n?'block':'none';}});
}}
</script>
</body>
</html>"""


# ── メイン ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="逆指値シグナル統合レポート")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true",
                        help="シグナルのみ表示（HTML生成をスキップ）")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
    # モード選択 (実際の切替は import 前に sys.argv で行う)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, 目標+4.5%%)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, 目標+9%%, デフォルト)")
    parser.add_argument("--funds", action="store_true",
                        help="指定期間のシグナル銘柄・必要資金集計をHTMLに表示")
    parser.add_argument("--regime-filter", action="store_true",
                        help="今の相場環境に適した戦略のみ表示 (N225自動検出)")
    args = parser.parse_args()

    _auto_update_regime_cache(args.workers)

    if args.regime_filter:
        msg = _detect_and_filter_by_regime()
        print(f"[相場フィルター] {msg}", flush=True)

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    n_total = len(_stop.WATCHLIST) + len(_brk.WATCHLIST) + len(_short.WATCHLIST) + len(_sbrk.WATCHLIST)
    print(f"逆指値シグナル統合 開始 ({n_total}銘柄) シグナル確認日: {date_label}  モード: {_stop.TRADING_MODE}", flush=True)
    print(f"  逆指値B: {len(_stop.WATCHLIST)}銘柄  /  ブレイクアウト: {len(_brk.WATCHLIST)}銘柄"
          f"  /  ショート: {len(_short.WATCHLIST)}銘柄  /  ショートBRK: {len(_sbrk.WATCHLIST)}銘柄", flush=True)

    # 4グループを並列実行
    with ThreadPoolExecutor(max_workers=4) as outer:
        fut_stop  = outer.submit(_run_group, _stop,  sig_date, args.workers)
        fut_brk   = outer.submit(_run_group, _brk,   sig_date, args.workers)
        fut_short = outer.submit(_run_group, _short, sig_date, args.workers)
        fut_sbrk  = outer.submit(_run_group, _sbrk,  sig_date, args.workers)
    stop_items  = filter_items(fut_stop.result())
    brk_items   = filter_items(fut_brk.result())
    short_items = filter_items(fut_short.result())
    sbrk_items  = filter_items(fut_sbrk.result())

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 92)
    print(f"  逆指値シグナル統合  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 92)

    def _is_new_signal(item: dict) -> bool:
        """ルックバック（継続中/保有中）を除いた当日新規シグナルのみ True"""
        sig = item.get("today_sig")
        return bool(sig
                    and not sig.get("_pending_lookback")
                    and not sig.get("_filled_holding"))

    # 全シグナルをスコア降順でまとめて表示（当日新規のみ）
    all_sigs: list[tuple] = []
    for item in stop_items:
        if _is_new_signal(item):
            score, rank = _stop.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "逆指値B"))
    for item in brk_items:
        if _is_new_signal(item):
            score, rank = _brk.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "BRK"))
    for item in short_items:
        if _is_new_signal(item):
            score, rank = _short.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "ショート"))
    for item in sbrk_items:
        if _is_new_signal(item):
            score, rank = _sbrk.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "ショートBRK"))
    all_sigs.sort(key=lambda x: x[1], reverse=True)

    print(f"\n【シグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'銘柄':<12} {'名前':<24} {'戦略':<6} {'種別':<8} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値':>8} {'指値上限':>8} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 134)
        for item, score, rank, kind in all_sigs:
            sig = item["today_sig"]
            limit_entry = sig.get("limit_entry_price", sig["order_price"])
            print(f"  {item['symbol']:<12} {item['name']:<24} {item['strategy']:<6} {kind:<8}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>8,.0f}"
                  f" {limit_entry:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    show_days = args.days
    fund_rows: list[dict] = []
    if args.funds:
        fund_rows = collect_fund_rows([stop_items, brk_items, short_items, sbrk_items], show_days)

    print(f"\nHTMLレポート生成中...", flush=True)
    _cmd = "python run_signals.py"
    stop_html  = _stop.build_html(stop_items,   show_days, date_label, run_cmd=_cmd)
    brk_html   = _brk.build_html(brk_items,     show_days, date_label, run_cmd=_cmd)
    short_html = _short.build_html(short_items,  show_days, date_label, run_cmd=_cmd)
    sbrk_html  = _sbrk.build_html(sbrk_items,   show_days, date_label, run_cmd=_cmd)

    funds_block = _fund_html(fund_rows, show_days) if fund_rows else ""
    date_suffix = args.date if args.date else today
    # conservative (デフォルト) は suffix なし、aggressive は "_aggressive"
    mode_suffix = f"_{_stop.TRADING_MODE}" if _stop.TRADING_MODE != "conservative" else ""
    out_path    = Path(f"signals_combined{mode_suffix}_{date_suffix}.html")
    out_path.write_text(
        build_combined_html(stop_html, brk_html, short_html, sbrk_html, fund_html_block=funds_block),
        encoding="utf-8"
    )
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
