"""
analyze_fill_timing.py ― 逆指値シグナルの約定率・約定タイミング分析 (スイング)
================================================================================
シグナルが出た逆指値注文(ENTRY_EXPIRE=3営業日有効)が、
  ・全体で何%約定するか (fill率)
  ・約定したうち『翌日(T+1)/2日目(T+2)/3日目(T+3)』の内訳
を、WATCHLIST(または5分足がある全銘柄)×全戦略のバックテストから集計する。

日足バックテストのみ使用(5分足不要)。

使い方:
  python analyze_fill_timing.py                 # WATCHLIST・conservative
  python analyze_fill_timing.py --aggressive
  python analyze_fill_timing.py --all-minute --workers 8   # 5分足がある全銘柄
  python analyze_fill_timing.py --all-minute --limit 500 --workers 8
  python analyze_fill_timing.py --days 60       # 直近60日のシグナルに限定
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import run_limit_backtest, fetch, ENTRY_EXPIRE

JST = timezone(timedelta(hours=9))


def _pairs(universe):
    if universe is None:
        out = []
        for mod in (_stop, _brk):
            for sym, name, strat in getattr(mod, "WATCHLIST", []):
                out.append((mod, sym, name, strat))
        return out
    out = []
    for sym in universe:
        for strat in getattr(_stop, "STRATEGY_PARAMS", {}):
            out.append((_stop, sym, sym, strat))
        for strat in getattr(_brk, "STRATEGY_PARAMS", {}):
            out.append((_brk, sym, sym, strat))
    return out


def _one(mod, sym, name, strat, cutoff):
    """1(銘柄,戦略): (rec_score, signals, 約定明細[(days_to_fill,strategy),...]) を返す。
    rec_score(BTスコア)はBTフィルタ用。約定挙動は em=0 で con/agg 共通。"""
    try:
        bt = mod.backtest_one(sym, name, strat)
    except Exception:
        return 0, 0, []
    if not bt:
        return 0, 0, []
    pr = bt.get("period_results") or {}
    if not pr:
        return 0, 0, []
    try:
        rec, _ = mod.calc_recommend_score(pr)
    except Exception:
        rec = 0
    maxp = max(pr.keys())
    prm = pr[maxp]
    signals = prm.get("signals", 0)
    fills = []
    for t in prm.get("trade_log", []):
        if t.get("reason") == "発注中":       # 未約定(まだ期限内)は約定にカウントしない
            continue
        sdt = t.get("signal_dt")
        sd = sdt.date() if hasattr(sdt, "date") else sdt
        if cutoff and sd and sd < cutoff:
            continue
        fills.append((t.get("days_to_fill", 0), strat))
    return rec, signals, fills


def _aggregate(pairs, cutoff, workers):
    """(mod,sym,name,strat) を並列集計し records=[(rec_score,signals,fills,strat),...] を返す。"""
    records = []
    if workers and workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_one, m, s, n, st, cutoff): st for (m, s, n, st) in pairs}
            for fu in as_completed(futs):
                rec, sig, fills = fu.result()
                records.append((rec, sig, fills, futs[fu]))
    else:
        for (m, s, n, st) in pairs:
            rec, sig, fills = _one(m, s, n, st, cutoff)
            records.append((rec, sig, fills, st))
    return records


def build_html(is_short: bool = False, workers: int = 4) -> str:
    """レポート詳細タブ用: 逆指値の約定率・約定タイミング・失効(未約定)の集計HTML。
    BTフィルタ(全部/BT60/BT70)付き。em=0(トリガ=前日終値)で約定挙動は con/agg 共通。"""
    if is_short:
        try:
            import check_signals_short as m1
            import check_signals_short_breakout as m2
        except Exception:
            return '<p style="color:#94a3b8;padding:16px">ショート戦略モジュール未検出</p>'
        mods = [m1, m2]
        side_label = "ショート(逆指値売り: 前日終値割れで発火)"
    else:
        mods = [_stop, _brk]
        side_label = "ロング(逆指値買い: 前日終値超えで発火)"

    pairs = []
    for mod in mods:
        for sym, name, strat in getattr(mod, "WATCHLIST", []):
            pairs.append((mod, sym, name, strat))
    if not pairs:
        return '<p style="color:#94a3b8;padding:16px">WATCHLISTが空です</p>'

    records = _aggregate(pairs, None, workers)  # (rec_score, signals, fills, strat)

    def _c(v, base):
        return "#4ade80" if v >= base else "#f87171"

    def _render_block(recs, key, active):
        from collections import defaultdict as _dd
        total_signals = sum(r[1] for r in recs)
        dtf_all = _dd(int)
        by_sig, by_fill, by_t1 = _dd(int), _dd(int), _dd(int)
        for rec, sig, fills, strat in recs:
            by_sig[strat] += sig
            for dtf, s2 in fills:
                dtf_all[dtf] += 1
                by_fill[s2] += 1
                if dtf == 1:
                    by_t1[s2] += 1
        total_filled = sum(dtf_all.values())
        expired = max(0, total_signals - total_filled)
        fill_pct = total_filled / total_signals * 100 if total_signals else 0
        exp_pct = expired / total_signals * 100 if total_signals else 0
        t1 = dtf_all.get(1, 0)
        t1_pct = t1 / total_signals * 100 if total_signals else 0

        def _row(label, n, note=""):
            pct = n / total_signals * 100 if total_signals else 0
            return (f'<tr><td style="padding:3px 12px">{label}</td>'
                    f'<td style="padding:3px 12px;text-align:right">{n:,}件</td>'
                    f'<td style="padding:3px 12px;text-align:right">{pct:.1f}%</td>'
                    f'<td style="padding:3px 12px;color:#94a3b8">{note}</td></tr>')

        rows = ""
        for d in sorted(dtf_all):
            lbl = {1: "翌日(T+1)約定", 2: "2日目(T+2)約定", 3: "3日目(T+3)約定"}.get(d, f"{d}日目約定")
            rows += _row(lbl, dtf_all[d])
        rows += _row("失効(未約定)", expired, "3営業日以内にトリガ未達=ブレイク不発/逆行")

        strat_rows = ""
        for st in sorted(by_fill, key=lambda s: -by_fill.get(s, 0)):
            sig = by_sig.get(st, 0)
            fil = by_fill.get(st, 0)
            exp = max(0, sig - fil)
            fr = fil / sig * 100 if sig else 0
            er = exp / sig * 100 if sig else 0
            t1r = by_t1.get(st, 0) / fil * 100 if fil else 0
            strat_rows += (
                f'<tr><td style="padding:3px 12px">{st}</td>'
                f'<td style="padding:3px 12px;text-align:right">{sig:,}</td>'
                f'<td style="padding:3px 12px;text-align:right">{fil:,}</td>'
                f'<td style="padding:3px 12px;text-align:right">{exp:,}</td>'
                f'<td style="padding:3px 12px;text-align:right;color:{_c(fr,50)}">{fr:.1f}%</td>'
                f'<td style="padding:3px 12px;text-align:right;color:{_c(50,er)}">{er:.1f}%</td>'
                f'<td style="padding:3px 12px;text-align:right;color:#94a3b8">{t1r:.1f}%</td></tr>'
            )
        disp = "block" if active else "none"
        return f"""<div id="ftblk_{key}" style="display:{disp}">
<div style="background:#111827;border:1px solid #1e293b;border-radius:6px;padding:8px 14px;margin-bottom:12px;display:inline-block">
  シグナル <b>{total_signals:,}</b> 件 &nbsp;/&nbsp;
  約定 <b style="color:#4ade80">{total_filled:,}件 ({fill_pct:.1f}%)</b> &nbsp;/&nbsp;
  失効 <b style="color:#f87171">{expired:,}件 ({exp_pct:.1f}%)</b> &nbsp;/&nbsp;
  うち翌日(T+1)約定 <b>{t1:,}件 ({t1_pct:.1f}%)</b>
</div>
<h3 style="margin:10px 0 4px;font-size:0.95rem">シグナル→約定 内訳 (全シグナル比)</h3>
<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">
<thead><tr style="color:#94a3b8"><th style="padding:3px 12px;text-align:left">区分</th>
<th style="padding:3px 12px">件数</th><th style="padding:3px 12px">全シグナル比</th>
<th style="padding:3px 12px;text-align:left">備考</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h3 style="margin:14px 0 4px;font-size:0.95rem">戦略別</h3>
<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">
<thead><tr style="color:#94a3b8"><th style="padding:3px 12px;text-align:left">戦略</th>
<th style="padding:3px 12px">シグナル</th><th style="padding:3px 12px">約定</th>
<th style="padding:3px 12px">失効</th><th style="padding:3px 12px">fill率</th>
<th style="padding:3px 12px">失効率</th><th style="padding:3px 12px">翌日/約定</th></tr></thead>
<tbody>{strat_rows}</tbody></table></div>
</div>"""

    bt_filters = [("all", "全部", lambda sc: True),
                  ("bt60", "BT60以上", lambda sc: (sc or 0) >= 60),
                  ("bt70", "BT70以上", lambda sc: (sc or 0) >= 70)]
    btns = "".join(
        f'<button class="ovbt-btn{" active" if k=="all" else ""}" id="ftbtn_{k}" '
        f'onclick="switchFtBt(\'{k}\')">{lbl} '
        f'<span style="font-size:0.72rem;color:#94a3b8">'
        f'({sum(1 for r in records if f(r[0]))}銘柄戦略)</span></button>'
        for k, lbl, f in bt_filters
    )
    blocks = "".join(
        _render_block([r for r in records if f(r[0])], k, k == "all")
        for k, lbl, f in bt_filters
    )

    return f"""<h2 style="margin-top:8px">⑮ 逆指値の約定率・約定タイミング</h2>
<p class="footnote">{side_label}。シグナルが出た逆指値注文(有効期限3営業日)が、いつ約定するか/
約定しないか(失効)を集計。em=0(トリガ=前日終値)のため con/agg 共通。WATCHLIST基準・直近365日。</p>
<div class="detail-tab-nav" style="margin:10px 0">{btns}</div>
{blocks}
<p class="footnote" style="margin-top:10px">
読み方: fill率=約定した割合。失効率=約定せず期限切れ(=ブレイク不発/逆行)。
翌日/約定=約定のうちT+1に約定した割合(逆指値は寄り付きに集中しやすい)。<br>
失効率が高い戦略はシグナルが空振りしやすい(発注コストは小さいが機会損失)。</p>
<script>
function switchFtBt(key){{
  ['all','bt60','bt70'].forEach(function(k){{
    var b=document.getElementById('ftblk_'+k); if(b) b.style.display=(k===key)?'block':'none';
    var t=document.getElementById('ftbtn_'+k); if(t) t.classList.toggle('active', k===key);
  }});
}}
</script>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-minute", action="store_true",
                    help="WATCHLISTでなく5分足がある全銘柄を対象")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--days", type=int, default=0,
                    help="直近N日のシグナルに限定(0=全期間365日)")
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    universe = None
    if args.all_minute:
        from analyze_open_confirm_entry import universe_from_pkl
        universe = universe_from_pkl()
        if args.limit and args.limit > 0:
            universe = universe[:args.limit]

    cutoff = (datetime.now(JST).date() - timedelta(days=args.days)) if args.days else None
    pairs = _pairs(universe)
    mode = os.environ.get("TRADING_MODE", "conservative")

    print("=" * 70)
    print(f"逆指値 約定率・約定タイミング分析  mode={mode}  "
          f"有効期限={ENTRY_EXPIRE}営業日")
    scope = f"5分足がある全銘柄 {len(universe)}銘柄" if universe else "WATCHLIST"
    print(f"対象: {scope} × 全戦略"
          + (f" / 直近{args.days}日のシグナル" if cutoff else " / 直近365日"))
    print("=" * 70)

    total_signals = 0
    dtf_all: dict[int, int] = defaultdict(int)
    by_strat_sig: dict[str, int] = defaultdict(int)
    by_strat_fill: dict[str, int] = defaultdict(int)
    by_strat_t1: dict[str, int] = defaultdict(int)

    def _acc(sig, fills):
        nonlocal total_signals
        total_signals += sig
        for dtf, strat in fills:
            dtf_all[dtf] += 1
            by_strat_fill[strat] += 1
            if dtf == 1:
                by_strat_t1[strat] += 1

    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, m, s, n, st, cutoff): st
                    for (m, s, n, st) in pairs}
            done = 0
            for fu in as_completed(futs):
                _rec, sig, fills = fu.result()
                _acc(sig, fills)
                by_strat_sig[futs[fu]] += sig
                done += 1
                if done % 200 == 0:
                    print(f"  ...{done}/{len(pairs)}", flush=True)
    else:
        for (m, s, n, st) in pairs:
            _rec, sig, fills = _one(m, s, n, st, cutoff)
            _acc(sig, fills)
            by_strat_sig[st] += sig

    total_filled = sum(dtf_all.values())
    # cutoff指定時は signals をシグナル日で絞れないため fill率は概算になる旨を注記
    print(f"\nシグナル総数: {total_signals}"
          + ("  (※--days指定時のsignalsは全期間ベースの概算)" if cutoff else ""))
    print(f"約定総数    : {total_filled}")
    if total_signals > 0 and not cutoff:
        print(f"約定率(fill率): {total_filled / total_signals * 100:.1f}%  "
              f"(= 約定 {total_filled} / シグナル {total_signals})")
        print(f"失効(期限{ENTRY_EXPIRE}日内に未約定): "
              f"{total_signals - total_filled} 件 "
              f"({(total_signals - total_filled) / total_signals * 100:.1f}%)")

    expired = (total_signals - total_filled) if (total_signals and not cutoff) else None

    print("\n── シグナル→約定 内訳 (シグナル全体を100%として) ──────────")
    for d in sorted(dtf_all):
        label = {1: "翌日(T+1)約定", 2: "2日目(T+2)約定", 3: "3日目(T+3)約定"}.get(d, f"{d}日目約定")
        n = dtf_all[d]
        pct_fill = n / total_filled * 100 if total_filled else 0
        pct_sig = n / total_signals * 100 if total_signals and not cutoff else None
        line = f"  {label:<14}: {n:>6}件  (約定内{pct_fill:5.1f}%"
        line += f" / 全シグナルの{pct_sig:5.1f}%)" if pct_sig is not None else ")"
        print(line)
    if expired is not None:
        print(f"  {'失効(未約定)':<14}: {expired:>6}件  "
              f"(全シグナルの{expired / total_signals * 100:5.1f}%)  "
              f"← 3営業日以内に前日終値超えに届かず=ブレイク不発/下落")

    _t1 = dtf_all.get(1, 0)
    if total_signals and not cutoff:
        print(f"\n★ 全シグナルのうち: 翌日約定 {_t1 / total_signals * 100:.1f}% / "
              f"約定計 {total_filled / total_signals * 100:.1f}% / "
              f"失効 {expired / total_signals * 100:.1f}%")

    print("\n── 戦略別 (fill率 / 失効率 / 翌日約定) ────────────────────")
    print(f"  {'戦略':<8}{'シグナル':>8}{'約定':>7}{'失効':>7}{'fill率':>8}{'失効率':>8}{'翌日/約定':>9}")
    for st in sorted(by_strat_fill, key=lambda s: -by_strat_fill[s]):
        sig = by_strat_sig.get(st, 0)
        fil = by_strat_fill.get(st, 0)
        t1 = by_strat_t1.get(st, 0)
        exp = (sig - fil) if (sig and not cutoff) else None
        fr = f"{fil / sig * 100:.1f}%" if sig and not cutoff else "-"
        er = f"{exp / sig * 100:.1f}%" if exp is not None and sig else "-"
        t1r = f"{t1 / fil * 100:.1f}%" if fil else "-"
        exp_s = str(exp) if exp is not None else "-"
        print(f"  {st:<8}{sig:>8}{fil:>7}{exp_s:>7}{fr:>8}{er:>8}{t1r:>9}")

    print("\n読み方:")
    print("  ・fill率=シグナルのうち約定した割合。失効率=約定せず期限切れの割合(=不発/下落)。")
    print("  ・翌日約定=約定の中でT+1に約定した割合。逆指値は前日終値超えで発火するため")
    print("    寄り(T+1朝)に集中しやすい。")
    print("  ・失効が多い戦略=シグナルが出ても伸びずに終わることが多い(空振りコスト小だが機会損失)。")


if __name__ == "__main__":
    main()
