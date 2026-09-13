r"""signal_history.py — 発注リスト(シグナルタブ)を日付ごとに保存し、後から見る。

■ なぜ必要か (2026-08-17 ユーザー依頼)

  「昨日のシグナルの順番を確認したい」ことが頻繁にある。ところが
  **過去日を再生成して確認してはいけない**(CLAUDE.md 18.11):

    ③ ユニバースの価格フィルタが **最新終値** で切るので、当時は対象だった
       銘柄が行ごと消え、下位が繰り上がって順位がズレる。
       2026-08-08 の実測では、BTの値は168件すべて当時と一致したのに、
       当時3位の銘柄が画面から消えて4位が繰り上がっていた。
    + 凍結キャッシュへの書き込みは1回きりなので、汚したら後から直せない。

  → **その日に見えていたものを、その日に保存しておく**しかない。これがそれ。

■ 保存のルール (ここが本質)

  * ファイル名は **シグナル日**。1日1ファイル
  * **書き込みは1回だけ**。すでにファイルがあれば **絶対に上書きしない**
    (過去日を再生成しても記録は壊れない)
  * `saved_on`(実行日) を各行に記録する。シグナル日と離れていたら
    『当日に取った記録ではない』と分かるので、表示側で警告できる

■ 使い方

    # 保存はレポート生成時に自動(nikkei_analysis から呼ばれる)
    python signal_history.py --list                 # 保存済みの日付を一覧
    python signal_history.py --date 2026-08-14      # その日の発注リストを表示
    python signal_history.py --date 2026-08-14 --top 20
"""
from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path

DIR = Path("signal_history")
# ★ 「全部は保存しなくていい。前日ぐらいでいい」(2026-08-17 ユーザー指示)。
#   土日・祝日を挟むので 3 にしてある(金曜ぶんが月曜まで残る)。
KEEP_DAYS = int(__import__("os").environ.get("LSS_SIGNAL_HISTORY_KEEP", "3") or 3)
COLS = ["rank", "symbol", "name", "strategy", "liquidity", "order_price",
        "stop", "target", "qty", "wf_score", "signal_date", "saved_on",
        "src_base"]


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def path_for(signal_date: str) -> Path:
    return DIR / f"signal_list_{str(signal_date)[:10]}.csv"


def prune(keep: int = KEEP_DAYS, quiet: bool = False) -> int:
    """古い記録を消す。**前日ぶんが見られれば十分**(2026-08-17 ユーザー指示)。

    土日・祝日を挟むので keep=3 にしてある(金曜ぶんが月曜まで残る)。
    """
    _ds = dates()
    _old = _ds[keep:]
    for _d in _old:
        try:
            path_for(_d).unlink()
        except Exception:
            pass
    if _old and not quiet:
        print(f"  [発注リスト履歴] 古い {len(_old)}日ぶんを削除"
              f"(直近{keep}日だけ残します): {', '.join(_old)}", flush=True)
    return len(_old)


def save(signals, signal_date: str, saved_on: str = "", quiet: bool = False,
         keep: int = KEEP_DAYS):
    """発注リストを1回だけ保存する。既にあれば何もしない。

    signals: シグナルタブの並び順(=発注順)に並んだ dict のリスト。
             rank は **リストの順番** から振る(表示と同じ番号になる)。
    返り値: (保存したか, Path)
    """
    _sd = str(signal_date or "")[:10]
    if not _sd or not signals:
        return (False, None)
    _p = path_for(_sd)
    # ⛔⛔ 上書きしない。ここが記録を守っている唯一の仕組み。
    if _p.exists():
        if not quiet:
            print(f"  [発注リスト履歴] {_p.name} は既にあります(上書きしません)",
                  flush=True)
        return (False, _p)
    _so = str(saved_on or _dt.date.today())[:10]
    DIR.mkdir(exist_ok=True)
    _rows = []
    for _i, _s in enumerate(signals, 1):
        _g = _s.get if hasattr(_s, "get") else (lambda k, d=None: d)
        _rows.append({
            "rank": _i,
            "symbol": _g("symbol", ""),
            "name": _g("name", ""),
            "strategy": _g("strategy", "") or _g("strat", ""),
            "liquidity": round(_f(_g("liquidity", 0)), 0),
            # H(指値)なら h_order_price、無ければ order_p / order_price
            "order_price": round(_f(_g("h_order_price", 0))
                                 or _f(_g("order_p", 0))
                                 or _f(_g("order_price", 0)), 1),
            "stop": round(_f(_g("lss_stop", 0)) or _f(_g("stop_p", 0))
                          or _f(_g("stop_price", 0)), 1),
            "target": round(_f(_g("lss_target", 0)) or _f(_g("target_p", 0))
                            or _f(_g("target_price", 0)), 1),
            "qty": int(_f(_g("qty", 0))) or "",
            "wf_score": _g("wf_score", "") if _g("wf_score", None) is not None else "",
            "signal_date": _sd,
            "saved_on": _so,
            "src_base": _g("src_base", "") or "",
        })
    with open(_p, "w", newline="", encoding="utf-8-sig") as _f2:
        _w = csv.DictWriter(_f2, fieldnames=COLS)
        _w.writeheader()
        _w.writerows(_rows)
    if not quiet:
        _late = " ⚠ シグナル日と実行日が離れています(当日の記録ではありません)" \
            if _so > _sd and (_dt.date.fromisoformat(_so)
                              - _dt.date.fromisoformat(_sd)).days > 4 else ""
        print(f"  [発注リスト履歴] {_p.name} に {len(_rows)}件を保存"
              f"(実行日 {_so}){_late}", flush=True)
    prune(keep, quiet=quiet)
    return (True, _p)


def load(signal_date: str):
    _p = path_for(signal_date)
    if not _p.exists():
        return []
    with open(_p, encoding="utf-8-sig") as _f2:
        return list(csv.DictReader(_f2))


def dates(limit: int = 0):
    """保存済みのシグナル日を新しい順に返す。"""
    if not DIR.exists():
        return []
    _ds = sorted((p.stem.replace("signal_list_", "")
                  for p in DIR.glob("signal_list_*.csv")), reverse=True)
    return _ds[:limit] if limit > 0 else _ds


# ── レポート用 HTML ─────────────────────────────────────────────────────
def render_html(limit: int = 2, top: int = 60, watch_cap: int = 50) -> str:
    """保存済みの発注リストを折りたたみで並べる。

    watch_cap: これを超える順位は『09:00に板を読めない』のでグレー表示にする。
    """
    _ds = dates(limit)
    if not _ds:
        return ""
    _out = [
        '<details style="margin:14px 0;border:1px solid #334155;'
        'border-radius:8px;padding:10px 12px;background:#0f172a">',
        '<summary style="cursor:pointer;font-weight:700;color:#e2e8f0">'
        f'📋 前日までの発注リスト（{" / ".join(_ds)}）'
        '</summary>',
        '<p style="color:#94a3b8;font-size:0.8rem;margin:8px 0">'
        '★ <b>その日に見えていた発注リストそのもの</b>です。'
        '過去日を再生成すると価格フィルタが最新終値で切るため順位がズレるので'
        '（CLAUDE.md 18.11）、当日に保存したこちらが正本です。<br>'
        f'グレーの行 = 順位が watch{watch_cap} を超えており、'
        '09:00 に板を読めない（ライブでは登録すらしない）銘柄。</p>',
    ]
    for _d in _ds:
        _rows = load(_d)
        if not _rows:
            continue
        _so = (_rows[0].get("saved_on") or "")[:10]
        _warn = ""
        try:
            if _so and (_dt.date.fromisoformat(_so)
                        - _dt.date.fromisoformat(_d)).days > 4:
                _warn = ('<span style="color:#fbbf24">　⚠ 実行日が離れています'
                         f'（{_so} に保存）= 当日の記録ではありません</span>')
        except Exception:
            pass
        _in = sum(1 for r in _rows if int(_f(r.get("rank"), 0)) <= watch_cap)
        _out.append(
            f'<details style="margin:8px 0;border-left:3px solid #475569;'
            f'padding-left:10px">'
            f'<summary style="cursor:pointer;color:#cbd5e1">'
            f'<b>{_d}</b>　{len(_rows)}件'
            f'（watch{watch_cap} 圏内 <b>{_in}</b>件 / 圏外 {len(_rows) - _in}件）'
            f'{_warn}</summary>'
            f'<div style="overflow-x:auto"><table style="font-size:0.78rem;'
            f'border-collapse:collapse;margin:6px 0">'
            f'<tr style="color:#94a3b8"><th style="padding:2px 6px">順位</th>'
            f'<th style="padding:2px 6px">銘柄</th>'
            f'<th style="padding:2px 6px">戦略</th>'
            f'<th style="padding:2px 6px;text-align:right">売買代金/日</th>'
            f'<th style="padding:2px 6px;text-align:right">注文値</th>'
            f'<th style="padding:2px 6px;text-align:right">損切</th>'
            f'<th style="padding:2px 6px;text-align:right">目標</th></tr>')
        for _r in _rows[:top]:
            _rk = int(_f(_r.get("rank"), 0))
            _dim = ' style="opacity:0.45"' if _rk > watch_cap else ''
            _lq = _f(_r.get("liquidity"))
            _out.append(
                f'<tr{_dim}>'
                f'<td style="padding:2px 6px;text-align:right">#{_rk}</td>'
                f'<td style="padding:2px 6px">{_r.get("symbol","")} '
                f'<span style="color:#64748b">{_r.get("name","")}</span></td>'
                f'<td style="padding:2px 6px">{_r.get("strategy","")}</td>'
                f'<td style="padding:2px 6px;text-align:right">'
                f'{_lq / 1e8:,.0f}億</td>'
                f'<td style="padding:2px 6px;text-align:right">'
                f'{_f(_r.get("order_price")):,.0f}</td>'
                f'<td style="padding:2px 6px;text-align:right;color:#f87171">'
                f'{_f(_r.get("stop")):,.0f}</td>'
                f'<td style="padding:2px 6px;text-align:right;color:#4ade80">'
                f'{_f(_r.get("target")):,.0f}</td></tr>')
        if len(_rows) > top:
            _out.append(f'<tr><td colspan="7" style="padding:4px 6px;'
                        f'color:#64748b">… 以下 {len(_rows) - top}件は省略'
                        f'（全件は signal_history/signal_list_{_d}.csv）</td></tr>')
        _out.append('</table></div></details>')
    _out.append('</details>')
    return "\n".join(_out)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="保存済みの発注リストを見る(照会のみ)")
    ap.add_argument("--date", type=str, default="", help="yyyy-mm-dd")
    ap.add_argument("--list", action="store_true", help="保存済みの日付を一覧")
    ap.add_argument("--top", type=int, default=60)
    a = ap.parse_args()
    if a.list or not a.date:
        _ds = dates()
        if not _ds:
            raise SystemExit(f"[error] {DIR}/ に記録がありません。"
                             f"`.\\daily` を1回走らせると作られます。")
        print(f"■ 保存済みの発注リスト {len(_ds)}日")
        for _d in _ds:
            _r = load(_d)
            _so = (_r[0].get("saved_on") or "") if _r else ""
            print(f"  {_d}  {len(_r):>4}件  (保存 {_so})")
        raise SystemExit(0)
    _rows = load(a.date)
    if not _rows:
        raise SystemExit(f"[error] {path_for(a.date)} がありません。"
                         f"--list で保存済みの日付を確認してください。")
    print(f"■ {a.date} の発注リスト（当日に保存された正本 / "
          f"保存 {_rows[0].get('saved_on','')}）")
    print(f"  {'順位':>4} {'銘柄':<9} {'戦略':<8} {'売買代金':>9} "
          f"{'注文値':>9} {'損切':>9} {'目標':>9}")
    for _r in _rows[:a.top]:
        print(f"  #{int(_f(_r.get('rank'))):>3} {_r.get('symbol',''):<9} "
              f"{_r.get('strategy',''):<8} "
              f"{_f(_r.get('liquidity')) / 1e8:>8,.0f}億 "
              f"{_f(_r.get('order_price')):>9,.0f} "
              f"{_f(_r.get('stop')):>9,.0f} {_f(_r.get('target')):>9,.0f}"
              f"  {_r.get('name','')}")
    if len(_rows) > a.top:
        print(f"  … 以下 {len(_rows) - a.top}件（--top で増やせます）")
