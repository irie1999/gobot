"""
run_signals_highscore.py  —  スコア上位銘柄 シグナルレポート
==============================================================
既存WATCHLIST + WF + NOLIMIT 全設定の全銘柄をバックテスト採点し、
スコア上位 (デフォルト 80点以上) に絞ったシグナルを確認する。

採点結果は .highscore_cache.json に7日間キャッシュ。
初回または --build 時のみ全銘柄スキャンが走る。

【使い方】
  python run_signals_highscore.py                # キャッシュ使用 or 自動構築
  python run_signals_highscore.py --build        # キャッシュ強制再構築
  python run_signals_highscore.py --min-score 70 # 70点以上に変更 (デフォルト80)
  python run_signals_highscore.py --max-price 5000  # 株価上限フィルター
  python run_signals_highscore.py --days 90      # 直近90日表示
  python run_signals_highscore.py --date 2026-05-01  # 任意日シグナル
  python run_signals_highscore.py --no-browser   # HTML生成のみ
  python run_signals_highscore.py --signal-only  # シグナルのみ
  python run_signals_highscore.py --list         # 採点済み銘柄一覧
  python run_signals_highscore.py --aggressive   # aggressiveモード

【キャッシュについて】
  .highscore_cache.json に保存 (7日有効)
  --build / スコア閾値変更 / モード変更 / キャッシュ期限切れ で再構築

【出力】
  signals_highscore_YYYY-MM-DD.html
  (3タブ: 逆指値B / ブレイクアウト / 採点ランキング)
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──────────────────────────────────────────
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop            as _stop
import check_signals_breakout        as _brk
import check_signals_short           as _short
import check_signals_short_breakout  as _sbrk
from backtest_limit_entry import WORKERS as _DEF_WORKERS, fetch as _fetch_bt
from run_signals import (
    _run_group, _extract_body, _extract_style,
    _auto_update_regime_cache, get_regime_html, build_combined_html,
)
from _signal_funds import filter_items

JST = timezone(timedelta(hours=9))

# ── 定数 ─────────────────────────────────────────────────────────────────────
_CACHE_FILE       = Path(".highscore_cache.json")
_CACHE_VALID_DAYS = 7
_DEFAULT_MIN_SCORE = 80

_STOP_STRATS = frozenset({"MACD", "A7", "RSI2"})
_BRK_STRATS  = frozenset({"DON", "VOL", "MOM"})

# ── 候補銘柄収集 ───────────────────────────────────────────────────────────────
def _collect_candidates() -> list[tuple[str, str, str]]:
    """
    既存WATCHLIST + WF + NOLIMIT + MERGED から
    重複なし (sym, name, strat) を収集する。
    """
    seen: set = set()
    out: list = []

    def _add(wl):
        for entry in wl:
            if len(entry) < 3:
                continue
            sym, name, strat = entry[0], entry[1], entry[2]
            if strat not in _STOP_STRATS | _BRK_STRATS:
                continue          # ショート戦略は除外
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                out.append((sym, name, strat))

    _add(_stop.WATCHLIST)
    _add(_brk.WATCHLIST)

    for mod_name in ("run_signals_wf", "run_signals_nolimit", "run_signals_merged"):
        try:
            m = importlib.import_module(mod_name)
        except Exception:
            continue
        for attr in (
            "_STOP_WATCHLIST_CONSERVATIVE", "_STOP_WATCHLIST_AGGRESSIVE", "STOP_WATCHLIST",
            "_BRK_WATCHLIST_CONSERVATIVE",  "_BRK_WATCHLIST_AGGRESSIVE",  "BRK_WATCHLIST",
            "_WF_STOP", "_WF_BRK",
            "_STOP_WATCHLIST", "_BRK_WATCHLIST",
        ):
            wl = getattr(m, attr, None)
            if wl:
                try:
                    _add(list(wl))
                except Exception:
                    pass

    return out


# ── キャッシュ管理 ─────────────────────────────────────────────────────────────
def _build_cache(min_score: int, max_price: float | None, workers: int) -> list[dict]:
    """
    全候補をバックテスト採点してキャッシュに保存し、全エントリーを返す。
    エントリー形式: {sym, name, strat, score, rank, wr, pf, n_trades, last_price}
    """
    candidates = _collect_candidates()
    mode = _stop.TRADING_MODE

    print(f"  {len(candidates)}銘柄ペアを採点中 (workers={workers})…")

    def _one(sym: str, name: str, strat: str) -> dict | None:
        fn = _stop.backtest_one if strat in _STOP_STRATS else _brk.backtest_one
        try:
            r = fn(sym, name, strat)
        except Exception:
            return None
        if not r:
            return None
        pr = r.get("period_results") or {}
        if not pr:
            return None
        score, rank = _stop.calc_recommend_score(pr)
        max_p = max(pr.keys())
        pr_max = pr[max_p]
        n_trades = pr_max.get("trades", 0)
        wr = pr_max.get("win_rate", 0.0)
        pf = pr_max.get("pf", 0.0)
        # 直近株価 (トレードログ末尾の entry_p か fetch の最終終値)
        tlog = pr_max.get("trade_log") or []
        last_p = tlog[-1]["entry_p"] if tlog else 0.0
        if last_p <= 0:
            df = _fetch_bt(sym, 30)
            last_p = float(df.iloc[-1]["close"]) if df is not None and len(df) > 0 else 0.0
        return dict(sym=sym, name=name, strat=strat,
                    score=score, rank=rank,
                    wr=round(wr, 1), pf=round(pf, 2),
                    n_trades=n_trades, last_p=round(last_p, 0))

    all_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, sym, name, strat): (sym, name, strat)
                for sym, name, strat in candidates}
        done = 0
        for fut in as_completed(futs):
            done += 1
            print(f"\r  {done}/{len(candidates)} ...", end="", flush=True)
            r = fut.result()
            if r:
                all_results.append(r)
    print()

    all_results.sort(key=lambda x: -x["score"])

    # 株価フィルター (last_p が取れた銘柄のみ)
    if max_price is not None:
        n_before = len(all_results)
        all_results = [e for e in all_results
                       if e["last_p"] <= 0 or e["last_p"] <= max_price]
        print(f"  株価フィルター (≤{max_price:,.0f}円): {n_before} → {len(all_results)}銘柄")

    cache = {
        "created":   date.today().isoformat(),
        "mode":      mode,
        "min_score": min_score,
        "max_price": max_price,
        "n_scanned": len(candidates),
        "entries":   all_results,
    }
    _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  キャッシュ保存: {_CACHE_FILE}  ({len(all_results)}銘柄)")
    return all_results


def _load_cache(min_score: int, max_price: float | None) -> list[dict] | None:
    """
    有効なキャッシュがあれば読み込む。なければ None を返す。
    有効条件: 7日以内 / モード一致 / min_score 一致 / max_price 一致
    """
    if not _CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None

    created = date.fromisoformat(cache.get("created", "2000-01-01"))
    if (date.today() - created).days > _CACHE_VALID_DAYS:
        return None
    if cache.get("mode") != _stop.TRADING_MODE:
        return None
    if cache.get("min_score") != min_score:
        return None
    if cache.get("max_price") != max_price:
        return None
    return cache.get("entries") or None


def _filter_entries(entries: list[dict], min_score: int) -> list[dict]:
    return [e for e in entries if e["score"] >= min_score]


# ── HTML 採点ランキングタブ ────────────────────────────────────────────────────
_RANK_COLORS = {"★★★": "#4ade80", "★★": "#60a5fa", "★": "#fbbf24", "△": "#f87171"}

def _ranking_html(all_entries: list[dict], selected: list[dict],
                  cache_date: str, n_scanned: int, min_score: int) -> str:
    """採点ランキング全一覧 HTML (tab3 用)。"""
    selected_keys = {(e["sym"], e["strat"]) for e in selected}

    def _pf_str(pf: float) -> str:
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    rows = ""
    for e in all_entries:
        is_sel = (e["sym"], e["strat"]) in selected_keys
        rank   = e.get("rank", "△")
        rc     = _RANK_COLORS.get(rank, "#94a3b8")
        sel_badge = ('<span style="background:#1d4ed8;color:#bfdbfe;padding:1px 7px;'
                     'border-radius:10px;font-size:0.78rem;margin-left:6px">選定</span>'
                     if is_sel else "")
        row_bg = "background:#0c1a30;" if is_sel else ""
        pf_s = _pf_str(e.get("pf", 0))
        lp   = e.get("last_p", 0)
        lp_s = f"{lp:,.0f}円" if lp > 0 else "—"
        rows += f"""<tr style="{row_bg}">
  <td style="color:#64748b;font-size:0.82rem">{e['sym']}</td>
  <td style="color:#e2e8f0">{e['name']}{sel_badge}</td>
  <td><span style="background:#1e293b;padding:2px 7px;border-radius:4px;
       font-size:0.78rem;color:#94a3b8">{e['strat']}</span></td>
  <td style="color:{rc};font-weight:700;text-align:center">{rank} {e['score']}</td>
  <td style="text-align:right">{e.get('n_trades',0)}</td>
  <td style="text-align:right">{e.get('wr',0):.1f}%</td>
  <td style="text-align:right">{pf_s}</td>
  <td style="text-align:right;color:#64748b">{lp_s}</td>
</tr>"""

    n_sel = len(selected)
    return f"""
<div style="background:#0f172a;color:#e2e8f0;padding:20px 24px">

<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px">
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;
       padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">スキャン銘柄数</div>
    <div style="font-size:1.3rem;font-weight:700;color:#f8fafc">{n_scanned}</div>
  </div>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;
       padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">選定数 (スコア{min_score}+)</div>
    <div style="font-size:1.3rem;font-weight:700;color:#3b82f6">{n_sel}</div>
  </div>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;
       padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">採点日</div>
    <div style="font-size:1.1rem;font-weight:600;color:#94a3b8">{cache_date}</div>
  </div>
</div>

<div style="background:#1e2a1a;border:1px solid #2d4a24;border-radius:8px;
     padding:12px 16px;margin-bottom:20px;font-size:0.85rem;color:#86efac">
  <strong>🎯 選定基準</strong>: 全設定(既存/WF/NOLIMIT)から全ユニーク銘柄を採点し、
  スコア{min_score}点以上を自動選定。<br>
  <span style="color:#94a3b8">⚠️ スコアは同期間バックテスト成績から計算 (in-sample bias あり)。
  高スコア = 過去に良かった銘柄であり、将来の保証ではありません。</span>
</div>

<table style="width:100%;border-collapse:collapse;font-size:0.87rem">
<thead>
<tr style="background:#1e293b">
  <th style="padding:9px 10px;text-align:left;color:#94a3b8;border-bottom:2px solid #334155">コード</th>
  <th style="padding:9px 10px;text-align:left;color:#94a3b8;border-bottom:2px solid #334155">銘柄</th>
  <th style="padding:9px 10px;text-align:left;color:#94a3b8;border-bottom:2px solid #334155">戦略</th>
  <th style="padding:9px 10px;text-align:center;color:#94a3b8;border-bottom:2px solid #334155">スコア</th>
  <th style="padding:9px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">取引数</th>
  <th style="padding:9px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">勝率</th>
  <th style="padding:9px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">PF</th>
  <th style="padding:9px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">直近株価</th>
</tr>
</thead>
<tbody style="border:1px solid #1e293b">
{rows}
</tbody>
</table>
</div>"""


def _build_hs_html(stop_html: str, brk_html: str,
                   ranking_html_body: str,
                   cache_date: str, n_scanned: int,
                   min_score: int, n_selected: int,
                   mode: str) -> str:
    """3タブ統合HTML (逆指値B / ブレイクアウト / 採点ランキング)。"""
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    stop_css  = _extract_style(stop_html)
    brk_css   = _extract_style(brk_html)
    stop_body = _extract_body(stop_html)
    brk_body  = _extract_body(brk_html)

    extra_css = "\n".join(
        line for line in brk_css.splitlines()
        if any(k in line for k in ("tag-don", "tag-vol", "tag-mom"))
    )

    mode_badge = (f'<span style="background:#7c3aed;color:#fff;padding:2px 9px;'
                  f'border-radius:4px;font-size:0.83rem;margin-left:8px">{mode}</span>')
    info_banner = f"""
<div style="background:#0f1e3a;border-bottom:1px solid #1e3a5f;padding:8px 20px;
     font-size:0.84rem;color:#64748b;display:flex;gap:24px;flex-wrap:wrap">
  <span>📅 採点日: <strong style="color:#94a3b8">{cache_date}</strong></span>
  <span>🔍 スキャン: <strong style="color:#94a3b8">{n_scanned}銘柄ペア</strong></span>
  <span>✅ 選定 (スコア{min_score}+): <strong style="color:#3b82f6">{n_selected}銘柄ペア</strong></span>
  <span style="color:#fbbf24">⚠ 再構築: <code>python run_signals_highscore.py --build</code></span>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>高スコアシグナル — {today_str}</title>
<style>
{stop_css}
{extra_css}
.tab-nav{{display:flex;gap:0;background:#090b14;padding:10px 16px 0;
          border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;flex-wrap:wrap}}
.tab-btn{{background:#16192a;color:#94a3b8;border:1px solid #252840;border-bottom:none;
          padding:9px 22px;margin-right:4px;border-radius:6px 6px 0 0;cursor:pointer;
          font-size:13px;font-family:inherit;transition:background .15s,color .15s}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{background:#0f1117;color:#38bdf8;border-color:#38bdf8;border-bottom-color:#0f1117}}
.tab-pane{{min-height:60vh}}
</style>
</head>
<body>
{get_regime_html()}
{info_banner}
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">
    ★高スコア 逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">
    ★高スコア ブレイクアウト（DON / VOL / MOM）</button>
  <button class="tab-btn"        onclick="switchTab(2)">
    採点ランキング（全{n_scanned}銘柄ペア）</button>
</div>
<div id="tc0" class="tab-pane">
{stop_body}
</div>
<div id="tc1" class="tab-pane" style="display:none">
{brk_body}
</div>
<div id="tc2" class="tab-pane" style="display:none">
{ranking_html_body}
</div>
<script>
function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{b.classList.toggle('active',i===n);}});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{t.style.display=i===n?'block':'none';}});
}}
</script>
</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="スコア上位銘柄 シグナルレポート")
    p.add_argument("--days",        type=int, default=365,
                   help="バックテスト表示期間 (日数)")
    p.add_argument("--date",        type=str, default=None,
                   help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    p.add_argument("--min-score",   type=int, default=_DEFAULT_MIN_SCORE,
                   help=f"スコア選定閾値 (デフォルト: {_DEFAULT_MIN_SCORE})")
    p.add_argument("--max-price",   type=float, default=None,
                   help="株価上限フィルター (円)")
    p.add_argument("--build",       action="store_true",
                   help="キャッシュを強制再構築して採点しなおす")
    p.add_argument("--list",        action="store_true",
                   help="採点済み銘柄一覧を標準出力して終了")
    p.add_argument("--no-browser",  action="store_true")
    p.add_argument("--signal-only", action="store_true")
    p.add_argument("--workers",     type=int, default=_DEF_WORKERS)
    p.add_argument("--aggressive",  action="store_true")
    args = p.parse_args()

    _auto_update_regime_cache(args.workers)

    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── WATCHLIST 取得 (キャッシュ or 構築) ──────────────────────────────────
    all_entries: list[dict] = []
    if not args.build:
        all_entries = _load_cache(args.min_score, args.max_price) or []

    if not all_entries:
        print(f"[highscore] キャッシュなし / 期限切れ / 設定変更 → 全銘柄採点します…")
        all_entries = _build_cache(args.min_score, args.max_price, args.workers)

    selected = _filter_entries(all_entries, args.min_score)
    cache_date  = (json.loads(_CACHE_FILE.read_text(encoding="utf-8")).get("created", "—")
                   if _CACHE_FILE.exists() else "—")
    n_scanned = (json.loads(_CACHE_FILE.read_text(encoding="utf-8")).get("n_scanned", len(all_entries))
                 if _CACHE_FILE.exists() else len(all_entries))

    # ── --list モード ─────────────────────────────────────────────────────────
    if args.list:
        print(f"\n採点ランキング (スコア降順 / {len(all_entries)}銘柄ペア)")
        print(f"{'コード':<10} {'銘柄':<24} {'戦略':<6} {'スコア':>6} {'勝率':>7} {'PF':>6} {'取引数':>5}")
        print("-" * 80)
        for e in all_entries:
            sel = "◆" if e["score"] >= args.min_score else "  "
            pf_s = "∞" if e.get("pf", 0) == float("inf") else f"{e.get('pf',0):.2f}"
            print(f"{sel} {e['sym']:<10} {e['name']:<24} {e['strat']:<6} "
                  f"{e['rank']}{e['score']:>3}点  {e.get('wr',0):>6.1f}%  {pf_s:>6}  "
                  f"{e.get('n_trades',0):>5}回")
        print(f"\n◆ = スコア{args.min_score}以上 (選定済み): {len(selected)}銘柄ペア")
        return

    # ── WATCHLIST 差し替え (monkey-patch) ────────────────────────────────────
    hs_stop = [(e["sym"], e["name"], e["strat"]) for e in selected
               if e["strat"] in _STOP_STRATS]
    hs_brk  = [(e["sym"], e["name"], e["strat"]) for e in selected
               if e["strat"] in _BRK_STRATS]

    _stop.WATCHLIST = hs_stop
    _brk.WATCHLIST  = hs_brk

    mode = _stop.TRADING_MODE
    print(f"[highscore] スコア{args.min_score}+選定: 逆指値B={len(hs_stop)}銘柄 / BRK={len(hs_brk)}銘柄  "
          f"モード: {mode}  確認日: {args.date or '本日'}", flush=True)

    if not hs_stop and not hs_brk:
        print(f"[highscore] スコア{args.min_score}以上の銘柄がありません。"
              f"--min-score を下げるか --build で再採点してください。", file=sys.stderr)
        return

    # ── シグナル実行 ──────────────────────────────────────────────────────────
    sig_date = None
    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    date_label = args.date if args.date else "本日"

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_stop = pool.submit(_run_group, _stop, sig_date, args.workers)
        fut_brk  = pool.submit(_run_group, _brk,  sig_date, args.workers)

    stop_items = filter_items(fut_stop.result())
    brk_items  = filter_items(fut_brk.result())

    # ── シグナル一覧 (コンソール) ─────────────────────────────────────────────
    all_sigs: list[tuple] = []
    for it in stop_items:
        sig = it.get("today_sig")
        if sig and not sig.get("_pending_lookback") and not sig.get("_filled_holding"):
            score, rank = _stop.calc_recommend_score(it["period_results"])
            all_sigs.append((it, sig, score, rank, "逆指値B"))
    for it in brk_items:
        sig = it.get("today_sig")
        if sig and not sig.get("_pending_lookback") and not sig.get("_filled_holding"):
            score, rank = _brk.calc_recommend_score(it["period_results"])
            all_sigs.append((it, sig, score, rank, "BRK"))
    all_sigs.sort(key=lambda x: x[2], reverse=True)

    print(f"\n【高スコアシグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'コード':<12} {'銘柄':<24} {'戦略':<6} {'スコア':>8} "
              f"{'逆指値':>8} {'損切り':>8} {'目標':>8}")
        print("  " + "-" * 80)
        for it, sig, score, rank, kind in all_sigs:
            print(f"  {it['symbol']:<12} {it['name']:<24} {it['strategy']:<6}"
                  f" {rank}{score:>3}点  "
                  f"{sig['order_price']:>8,.0f} {sig['stop_price']:>8,.0f}"
                  f" {sig['target_price']:>8,.0f}")
    else:
        print("  (本日シグナルなし)")

    if args.signal_only:
        return

    # ── HTML 生成 ─────────────────────────────────────────────────────────────
    _cmd = f"python run_signals_highscore.py --min-score {args.min_score}"
    stop_html = _stop.build_html(stop_items, args.days, date_label, run_cmd=_cmd)
    brk_html  = _brk.build_html(brk_items,  args.days, date_label, run_cmd=_cmd)

    rank_body = _ranking_html(
        all_entries, selected,
        cache_date, n_scanned, args.min_score,
    )

    combined = _build_hs_html(
        stop_html, brk_html, rank_body,
        cache_date, n_scanned, args.min_score, len(selected), mode,
    )

    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    out = Path(f"signals_highscore{mode_suffix}_{today_str}.html")
    out.write_text(combined, encoding="utf-8")
    print(f"\nHTML: {out}", flush=True)

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
