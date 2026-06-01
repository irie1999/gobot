"""
run_signals_highscore.py  —  スコア上位銘柄 シグナルレポート
==============================================================
プライム市場全銘柄 (~1800) × 6戦略をバックテスト採点し、
スコア上位に絞ったシグナルを確認する。

構成銘柄はハードコードせず、採点結果だけで決まる。
採点結果は .highscore_cache.json に14日間キャッシュ。

【使い方】
  python run_signals_highscore.py                   # キャッシュ使用 or 自動構築
  python run_signals_highscore.py --build           # 全銘柄再採点 (初回30分〜)
  python run_signals_highscore.py --per-strategy 10 # 戦略あたり上位10銘柄 (デフォルト)
  python run_signals_highscore.py --min-score 80    # 代替: スコア閾値指定
  python run_signals_highscore.py --max-price 5000  # 株価5,000円以下のみ
  python run_signals_highscore.py --limit 200       # デバッグ: 先頭200銘柄のみ
  python run_signals_highscore.py --list            # 採点済み銘柄一覧
  python run_signals_highscore.py --no-browser      # HTML生成のみ
  python run_signals_highscore.py --signal-only     # シグナルのみ
  python run_signals_highscore.py --aggressive      # aggressiveモード
  python run_signals_highscore.py --workers 8       # 並列数
  python run_signals_highscore.py --symbols symbols_listed_all.py  # ユニバース指定

【キャッシュ仕様】
  .highscore_cache.json に保存 (14日有効)
  --build / モード変更 / 期限切れ で再構築
  全エントリーをキャッシュし、--per-strategy / --min-score は再構築なしで変更可能

【出力】
  signals_highscore[_aggressive]_YYYY-MM-DD.html
  (3タブ: 逆指値B / ブレイクアウト / 採点ランキング)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import webbrowser
from collections import defaultdict
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
from backtest_limit_entry import (
    WORKERS as _DEF_WORKERS, fetch as _fetch_bt, FIXED_QTY,
)
from check_signals_stop import calc_recommend_score
from run_signals import (
    _run_group, _extract_body, _extract_style,
    _auto_update_regime_cache, get_regime_html,
)
from _signal_funds import filter_items

JST = timezone(timedelta(hours=9))

# ── 定数 ─────────────────────────────────────────────────────────────────────
_CACHE_FILE        = Path(".highscore_cache.json")
_CACHE_VALID_DAYS  = 14
_DEFAULT_PER_STRAT = 10      # デフォルト: 戦略あたり上位10銘柄

_STOP_STRATS = frozenset({"MACD", "A7", "RSI2"})
_BRK_STRATS  = frozenset({"DON", "VOL", "MOM"})
_ALL_STRATS  = list(_STOP_STRATS) + list(_BRK_STRATS)   # 6戦略

_UNIVERSE_CANDIDATES = [
    "symbols_listed_prime.py",
    "symbols_listed_all.py",
    "symbols_listed_standard.py",
    "symbols_all.py",
]


# ── ユニバース読み込み ─────────────────────────────────────────────────────────
def _load_universe(explicit: str | None = None) -> tuple[list[tuple[str, str]], str]:
    """(symbol, name) リストとソースファイル名を返す。"""
    candidates = ([explicit] if explicit else []) + _UNIVERSE_CANDIDATES
    for cand in candidates:
        p = Path(cand)
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            syms = [(str(t[0]), str(t[1])) for t in mod.SYMBOLS]
            return syms, p.name
        except Exception as e:
            print(f"[WARN] {cand}: {e}", file=sys.stderr)

    raise RuntimeError(
        "ユニバースファイルが見つかりません。先に実行してください:\n"
        "  python fetch_listed_symbols.py --market prime   # プライム ~1800銘柄\n"
        "  python fetch_listed_symbols.py --market all     # 全市場 ~4000銘柄"
    )


# ── 1銘柄 × 全6戦略 採点 ─────────────────────────────────────────────────────
def _score_stock(sym: str, name: str,
                 max_price: float | None) -> list[dict]:
    """
    1銘柄に対して全6戦略を採点し、有効なエントリーをリストで返す。
    yfinanceデータは1回だけfetchしてキャッシュを効かせる。
    """
    results: list[dict] = []

    # 株価フィルター: fetch で最新終値を確認
    df = _fetch_bt(sym, 365)
    if df is None or len(df) < 60:
        return results
    latest_p = float(df.iloc[-1]["close"])
    if max_price is not None and latest_p > max_price:
        return results

    for strat in _ALL_STRATS:
        fn = _stop.backtest_one if strat in _STOP_STRATS else _brk.backtest_one
        try:
            r = fn(sym, name, strat)
        except Exception:
            continue
        if not r:
            continue
        pr = r.get("period_results") or {}
        if not pr:
            continue
        score, rank = calc_recommend_score(pr)
        if score == 0:
            continue                          # 取引ゼロは除外
        max_p   = max(pr.keys())
        pr_max  = pr[max_p]
        results.append(dict(
            sym      = sym,
            name     = name,
            strat    = strat,
            score    = score,
            rank     = rank,
            wr       = round(pr_max.get("win_rate", 0.0), 1),
            pf       = round(pr_max.get("pf", 0.0), 2),
            n_trades = pr_max.get("trades", 0),
            last_p   = round(latest_p, 0),
        ))

    return results


# ── キャッシュ管理 ─────────────────────────────────────────────────────────────
def _build_cache(max_price: float | None, workers: int,
                 limit: int | None, symbols_path: str | None) -> list[dict]:
    """
    全銘柄 × 6戦略をスキャンして採点。
    スコア > 0 の全エントリーをキャッシュに保存して返す。
    """
    syms, src = _load_universe(symbols_path)
    if limit:
        syms = syms[:limit]
    mode = _stop.TRADING_MODE

    print(f"  ユニバース: {src}  ({len(syms)}銘柄)  モード: {mode}  workers: {workers}")
    if max_price:
        print(f"  株価フィルター: {max_price:,.0f}円以下")
    print(f"  6戦略 (MACD/A7/RSI2/DON/VOL/MOM) × {len(syms)}銘柄 = 最大{len(syms)*6:,}ペアを採点…")

    all_entries: list[dict] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_score_stock, sym, name, max_price): (sym, name)
                for sym, name in syms}
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0 or done == len(futs):
                print(f"\r  {done}/{len(syms)} 銘柄処理済み …", end="", flush=True)
            try:
                results = fut.result()
                all_entries.extend(results)
            except Exception:
                pass

    print(f"\n  → {len(all_entries)}エントリー (スコア>0のみ保存)")
    all_entries.sort(key=lambda x: -x["score"])

    cache = dict(
        created    = date.today().isoformat(),
        mode       = mode,
        universe   = src,
        n_scanned  = len(syms),
        max_price  = max_price,
        entries    = all_entries,
    )
    _CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  キャッシュ保存: {_CACHE_FILE}")
    return all_entries


def _load_cache() -> dict | None:
    """
    有効なキャッシュを返す。
    有効条件: 14日以内 / モード一致。
    max_price / per_strategy / min_score はキャッシュと独立に変更可能。
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
    return cache


def _select_entries(all_entries: list[dict],
                    per_strategy: int | None,
                    min_score: int | None,
                    max_price: float | None) -> list[dict]:
    """
    採点済み全エントリーから選定基準を適用してリストを返す。
    per_strategy と min_score を同時指定した場合は両方の条件を AND 適用。
    """
    entries = list(all_entries)

    # 株価フィルター (キャッシュ構築時と異なる値を後から適用したい場合)
    if max_price is not None:
        entries = [e for e in entries
                   if e.get("last_p", 0) <= 0 or e.get("last_p", 0) <= max_price]

    # スコア閾値
    if min_score is not None:
        entries = [e for e in entries if e["score"] >= min_score]

    # 戦略あたりトップN
    if per_strategy is not None:
        by_strat: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            by_strat[e["strat"]].append(e)
        selected: list[dict] = []
        for strat, es in by_strat.items():
            es_sorted = sorted(es, key=lambda x: -x["score"])
            selected.extend(es_sorted[:per_strategy])
        return selected

    return entries


# ── HTML 採点ランキングタブ ────────────────────────────────────────────────────
_RANK_COLORS = {"★★★": "#4ade80", "★★": "#60a5fa", "★": "#fbbf24", "△": "#f87171"}


def _ranking_html(all_entries: list[dict], selected: list[dict],
                  cache: dict, per_strategy: int | None,
                  min_score: int | None, max_price: float | None) -> str:
    selected_keys = {(e["sym"], e["strat"]) for e in selected}
    cache_date = cache.get("created", "—")
    n_scanned  = cache.get("n_scanned", 0)
    universe   = cache.get("universe", "—")
    mode       = cache.get("mode", "—")

    sel_note = []
    if per_strategy:
        sel_note.append(f"戦略あたり上位{per_strategy}銘柄")
    if min_score:
        sel_note.append(f"スコア{min_score}以上")
    if max_price:
        sel_note.append(f"株価{max_price:,.0f}円以下")
    sel_note_str = " / ".join(sel_note) if sel_note else "全エントリー"

    def _pf_str(pf):
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    rows = ""
    for e in all_entries[:300]:            # 表示上限 300件
        is_sel = (e["sym"], e["strat"]) in selected_keys
        rank   = e.get("rank", "△")
        rc     = _RANK_COLORS.get(rank, "#94a3b8")
        badge  = ('<span style="background:#1d4ed8;color:#bfdbfe;padding:1px 7px;'
                  'border-radius:10px;font-size:0.75rem;margin-left:4px">選定</span>'
                  if is_sel else "")
        bg = "background:#0c1a30;" if is_sel else ""
        lp_s = f"{e.get('last_p',0):,.0f}" if e.get("last_p", 0) > 0 else "—"
        rows += f"""<tr style="{bg}">
  <td style="color:#64748b;font-size:0.82rem">{e['sym']}</td>
  <td style="color:#e2e8f0">{e['name']}{badge}</td>
  <td><span style="background:#1e293b;padding:2px 6px;border-radius:4px;
       font-size:0.78rem;color:#94a3b8">{e['strat']}</span></td>
  <td style="color:{rc};font-weight:700;text-align:center">{rank} {e['score']}</td>
  <td style="text-align:right;color:#94a3b8">{e.get('n_trades',0)}</td>
  <td style="text-align:right">{e.get('wr',0):.1f}%</td>
  <td style="text-align:right">{_pf_str(e.get('pf',0))}</td>
  <td style="text-align:right;color:#64748b">{lp_s}円</td>
</tr>"""

    more = f'<p style="color:#64748b;padding:12px">... 上位300件を表示 (全{len(all_entries)}エントリー)</p>' \
           if len(all_entries) > 300 else ""

    return f"""
<div style="background:#0f172a;color:#e2e8f0;padding:20px 24px">
<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px">
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">スキャン銘柄</div>
    <div style="font-size:1.3rem;font-weight:700;color:#f8fafc">{n_scanned}</div>
    <div style="font-size:0.75rem;color:#475569">{universe}</div>
  </div>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">有効エントリー</div>
    <div style="font-size:1.3rem;font-weight:700;color:#94a3b8">{len(all_entries)}</div>
    <div style="font-size:0.75rem;color:#475569">スコア>0</div>
  </div>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">選定数</div>
    <div style="font-size:1.3rem;font-weight:700;color:#3b82f6">{len(selected)}</div>
    <div style="font-size:0.75rem;color:#475569">{sel_note_str}</div>
  </div>
  <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 18px;min-width:130px">
    <div style="font-size:0.78rem;color:#64748b;margin-bottom:4px">採点日 / モード</div>
    <div style="font-size:1rem;font-weight:600;color:#94a3b8">{cache_date}</div>
    <div style="font-size:0.75rem;color:#475569">{mode}</div>
  </div>
</div>
<div style="background:#1a2a1a;border:1px solid #2d4a24;border-radius:8px;
     padding:12px 16px;margin-bottom:16px;font-size:0.84rem;color:#86efac">
  <strong>🎯 選定基準</strong>: {sel_note_str}<br>
  <span style="color:#94a3b8">⚠ スコアは直近365日バックテスト成績から計算 (in-sample bias あり)。
  高スコア = 過去に良かった銘柄。Walk-forward検証には <code>scan_walkforward.py</code> を使用してください。</span>
</div>
<table style="width:100%;border-collapse:collapse;font-size:0.86rem">
<thead>
<tr style="background:#1e293b">
  <th style="padding:8px 10px;text-align:left;color:#94a3b8;border-bottom:2px solid #334155">コード</th>
  <th style="padding:8px 10px;text-align:left;color:#94a3b8;border-bottom:2px solid #334155">銘柄</th>
  <th style="padding:8px 10px;text-align:left;color:#94a3b8;border-bottom:2px solid #334155">戦略</th>
  <th style="padding:8px 10px;text-align:center;color:#94a3b8;border-bottom:2px solid #334155">スコア</th>
  <th style="padding:8px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">取引数</th>
  <th style="padding:8px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">勝率</th>
  <th style="padding:8px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">PF</th>
  <th style="padding:8px 10px;text-align:right;color:#94a3b8;border-bottom:2px solid #334155">直近株価</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
{more}
</div>"""


def _build_combined_html(stop_html: str, brk_html: str,
                         ranking_body: str,
                         cache: dict, selected: list[dict],
                         per_strategy: int | None, min_score: int | None,
                         mode: str) -> str:
    today_str  = datetime.now(JST).strftime("%Y-%m-%d")
    stop_css   = _extract_style(stop_html)
    brk_css    = _extract_style(brk_html)
    stop_body  = _extract_body(stop_html)
    brk_body   = _extract_body(brk_html)
    extra_css  = "\n".join(
        l for l in brk_css.splitlines()
        if any(k in l for k in ("tag-don", "tag-vol", "tag-mom"))
    )
    cache_date = cache.get("created", "—")
    n_scanned  = cache.get("n_scanned", 0)
    n_selected = len(selected)
    sel_note = (f"戦略あたり上位{per_strategy}銘柄" if per_strategy
                else f"スコア{min_score}以上" if min_score else "全エントリー")

    mode_badge = (f'<span style="background:#7c3aed;color:#fff;padding:2px 9px;'
                  f'border-radius:4px;font-size:0.83rem;margin-left:8px">{mode}</span>')
    info_banner = f"""
<div style="background:#0f1e3a;border-bottom:1px solid #1e3a5f;padding:8px 20px;
     font-size:0.84rem;color:#64748b;display:flex;gap:24px;flex-wrap:wrap">
  <span>📅 採点日: <strong style="color:#94a3b8">{cache_date}</strong></span>
  <span>🔍 スキャン: <strong style="color:#94a3b8">{n_scanned}銘柄 × 6戦略</strong></span>
  <span>✅ 選定 ({sel_note}): <strong style="color:#3b82f6">{n_selected}ペア</strong></span>
  <span style="color:#fbbf24">⚠ 再構築: <code>python run_signals_highscore.py --build</code></span>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>高スコアシグナル{mode_badge} — {today_str}</title>
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
    ★ 逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">
    ★ ブレイクアウト（DON / VOL / MOM）</button>
  <button class="tab-btn"        onclick="switchTab(2)">
    採点ランキング（{n_scanned}銘柄スキャン）</button>
</div>
<div id="tc0" class="tab-pane">{stop_body}</div>
<div id="tc1" class="tab-pane" style="display:none">{brk_body}</div>
<div id="tc2" class="tab-pane" style="display:none">{ranking_body}</div>
<script>
function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{b.classList.toggle('active',i===n);}});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{t.style.display=i===n?'block':'none';}});
}}
</script>
</body>
</html>"""


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="スコア上位銘柄 シグナルレポート")
    p.add_argument("--days",         type=int,   default=365,
                   help="バックテスト表示期間 (日数)")
    p.add_argument("--date",         type=str,   default=None,
                   help="シグナル確認日 YYYY-MM-DD (省略時=本日)")
    p.add_argument("--per-strategy", type=int,   default=_DEFAULT_PER_STRAT,
                   help=f"戦略あたり選定上位N銘柄 (デフォルト:{_DEFAULT_PER_STRAT})")
    p.add_argument("--min-score",    type=int,   default=None,
                   help="スコア閾値 (--per-strategy と併用可)")
    p.add_argument("--max-price",    type=float, default=None,
                   help="株価上限フィルター (円)")
    p.add_argument("--build",        action="store_true",
                   help="キャッシュを強制再構築 (全銘柄スキャン)")
    p.add_argument("--list",         action="store_true",
                   help="採点済み銘柄一覧を表示して終了")
    p.add_argument("--limit",        type=int,   default=None,
                   help="デバッグ: ユニバース先頭N銘柄のみスキャン")
    p.add_argument("--symbols",      type=str,   default=None,
                   help="ユニバースファイル指定 (デフォルト: symbols_listed_prime.py)")
    p.add_argument("--no-browser",   action="store_true")
    p.add_argument("--signal-only",  action="store_true")
    p.add_argument("--workers",      type=int,   default=_DEF_WORKERS)
    p.add_argument("--aggressive",   action="store_true")
    args = p.parse_args()

    _auto_update_regime_cache(args.workers)

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    mode      = _stop.TRADING_MODE

    # ── キャッシュ取得 or 構築 ────────────────────────────────────────────────
    cache: dict | None = None
    if not args.build:
        cache = _load_cache()
        if cache:
            age = (date.today() - date.fromisoformat(cache["created"])).days
            print(f"[highscore] キャッシュ使用 (採点日:{cache['created']}, "
                  f"{age}日前, {cache['n_scanned']}銘柄スキャン済み)")

    if cache is None:
        print(f"[highscore] 全銘柄採点開始 … (初回は時間がかかります)")
        all_entries = _build_cache(
            args.max_price, args.workers, args.limit, args.symbols)
        cache = _load_cache()
        if cache is None:                   # 構築直後のリロード保険
            cache = {"created": today_str, "mode": mode,
                     "universe": "—", "n_scanned": 0,
                     "max_price": args.max_price, "entries": all_entries}

    all_entries: list[dict] = cache.get("entries", [])

    # ── 選定 ─────────────────────────────────────────────────────────────────
    selected = _select_entries(
        all_entries, args.per_strategy, args.min_score, args.max_price)

    # ── --list モード ─────────────────────────────────────────────────────────
    if args.list:
        print(f"\n採点ランキング ({len(all_entries)}エントリー / スコア降順)")
        print(f"{'  '} {'コード':<10} {'銘柄':<22} {'戦略':<6} "
              f"{'スコア':>7} {'勝率':>7} {'PF':>6} {'取引':>5}")
        print("  " + "-" * 74)
        for e in all_entries[:200]:
            sel = "◆" if (e["sym"], e["strat"]) in {(s["sym"],s["strat"]) for s in selected} else "  "
            pf_s = "∞" if e.get("pf", 0) == float("inf") else f"{e.get('pf',0):.2f}"
            print(f"  {sel} {e['sym']:<10} {e['name']:<22} {e['strat']:<6} "
                  f"{e['rank']}{e['score']:>3}点  {e.get('wr',0):>6.1f}%  "
                  f"{pf_s:>6}  {e.get('n_trades',0):>4}回")
        print(f"\n◆ = 選定済み: {len(selected)}ペア (選定基準: "
              + (f"戦略あたり上位{args.per_strategy}" if args.per_strategy else "")
              + (f" / スコア{args.min_score}+" if args.min_score else "") + ")")
        return

    # ── WATCHLIST 差し替え (monkey-patch) ────────────────────────────────────
    hs_stop = [(e["sym"], e["name"], e["strat"]) for e in selected
               if e["strat"] in _STOP_STRATS]
    hs_brk  = [(e["sym"], e["name"], e["strat"]) for e in selected
               if e["strat"] in _BRK_STRATS]

    if not hs_stop and not hs_brk:
        print("[highscore] 選定銘柄がありません。"
              "--per-strategy を増やすか --build で再採点してください。",
              file=sys.stderr)
        return

    _stop.WATCHLIST = hs_stop
    _brk.WATCHLIST  = hs_brk

    print(f"[highscore] 選定: 逆指値B={len(hs_stop)}銘柄 / BRK={len(hs_brk)}銘柄  "
          f"モード:{mode}  確認日:{args.date or '本日'}", flush=True)

    # ── シグナル確認 ──────────────────────────────────────────────────────────
    sig_date = None
    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    date_label = args.date or "本日"

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_stop = pool.submit(_run_group, _stop, sig_date, args.workers)
        fut_brk  = pool.submit(_run_group, _brk,  sig_date, args.workers)

    stop_items = filter_items(fut_stop.result())
    brk_items  = filter_items(fut_brk.result())

    # ── シグナル一覧 ──────────────────────────────────────────────────────────
    all_sigs: list[tuple] = []
    for it in stop_items:
        sig = it.get("today_sig")
        if sig and not sig.get("_pending_lookback") and not sig.get("_filled_holding"):
            score, rank = _stop.calc_recommend_score(it["period_results"])
            all_sigs.append((it, sig, score, rank))
    for it in brk_items:
        sig = it.get("today_sig")
        if sig and not sig.get("_pending_lookback") and not sig.get("_filled_holding"):
            score, rank = _brk.calc_recommend_score(it["period_results"])
            all_sigs.append((it, sig, score, rank))
    all_sigs.sort(key=lambda x: x[2], reverse=True)

    print(f"\n【高スコアシグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'コード':<12} {'銘柄':<22} {'戦略':<6} {'スコア':>8}"
              f" {'逆指値':>8} {'損切り':>8} {'目標':>8}")
        print("  " + "-" * 80)
        for it, sig, score, rank in all_sigs:
            print(f"  {it['symbol']:<12} {it['name']:<22} {it['strategy']:<6}"
                  f" {rank}{score:>3}点"
                  f" {sig['order_price']:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f}"
                  f" {sig['target_price']:>8,.0f}")
    else:
        print("  (本日シグナルなし)")

    if args.signal_only:
        return

    # ── HTML 生成 ─────────────────────────────────────────────────────────────
    _cmd = (f"python run_signals_highscore.py"
            + (f" --per-strategy {args.per_strategy}" if args.per_strategy else "")
            + (f" --min-score {args.min_score}"       if args.min_score    else ""))
    stop_html = _stop.build_html(stop_items, args.days, date_label, run_cmd=_cmd)
    brk_html  = _brk.build_html(brk_items,  args.days, date_label, run_cmd=_cmd)
    rank_body = _ranking_html(all_entries, selected, cache,
                              args.per_strategy, args.min_score, args.max_price)
    combined  = _build_combined_html(stop_html, brk_html, rank_body,
                                     cache, selected,
                                     args.per_strategy, args.min_score, mode)

    mode_sfx = f"_{mode}" if mode != "conservative" else ""
    out = Path(f"signals_highscore{mode_sfx}_{today_str}.html")
    out.write_text(combined, encoding="utf-8")
    print(f"\nHTML: {out}", flush=True)

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
