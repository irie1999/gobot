r"""k_morning_archive.py — その朝の J(09:00確認)を丸ごと保存し、後から見る。

■ なぜ必要か (2026-08-18 ユーザー依頼「今日のシグナルは保存しておいて」)

  朝の記録は3つのファイルに散っていて、しかも **上書きされうる**:

    k_signals_<日付>.csv   今日のシグナル全部(--collect の出力)
    k_paper_<日付>.csv     09:00 に読んだ板・合格判定・株数・発注
    ordered_signals_lss.csv  実発注(追記式・全日ぶん)

  日付は入っているが、同じ日に `--collect` や `--poll` をもう一度回すと
  **その日のぶんが消える**。朝の記録は二度と取り直せない(板・気配の履歴は
  どのデータにも無い / §18.35)ので、失うと復元できない。

  → **書き込みは1回だけ**。すでにあれば絶対に上書きしない。
    signal_history.py と同じ方針(§18.11 の「その日に見えていたものを
    その日に保存する」)。

■ 何が残るか

    k_morning/2026-08-18_signals.csv   k_signals のコピー(全シグナル)
    k_morning/2026-08-18_board.csv     k_paper のコピー(読んだ銘柄・合格・発注)
    k_morning/2026-08-18.md            ★人が読むサマリー(合格銘柄の表)
    k_morning_log.csv                  1日1行の累積(日ごとの件数と投入額)

  ⛔ 剪定しない。signal_history は「前日ぶんが見られれば十分」という指示で
    3日で消すが、こちらは **実約定と突き合わせる素材**(§18.7)なので全部残す。
    1日あたり数十KBしかない。

■ 使い方

    # 保存は k_open_confirm の最後に自動。手で打つ必要はない
    python k_morning_archive.py --list              # 保存済みの日付を一覧
    python k_morning_archive.py --date 2026-08-18   # その朝のサマリーを表示
    python k_morning_archive.py --backfill          # 手元のCSVから作り直す
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import re
import shutil
from pathlib import Path

_BASE = Path(__file__).resolve().parent
DIR = _BASE / "k_morning"
LOG = _BASE / "k_morning_log.csv"

LOG_COLS = ["date", "saved_on", "signals", "signal_symbols", "read",
            "late", "pass", "ordered", "yen", "guard_ng", "groups"]


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return d


def _rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    try:
        return list(csv.DictReader(open(p, encoding="utf-8-sig")))
    except Exception:
        return []


def _sym(v) -> str:
    return str(v or "").upper().removesuffix(".T").split(".")[0]


def dates(limit: int = 0) -> list[str]:
    """保存済みの日付(新しい順)。"""
    if not DIR.exists():
        return []
    _ds = sorted({m.group(1) for p in DIR.glob("*.md")
                  if (m := re.fullmatch(r"(\d{4}-\d{2}-\d{2})", p.stem))},
                 reverse=True)
    return _ds[:limit] if limit else _ds


def load(day: str) -> tuple[list[dict], dict]:
    """(板の行, 銘柄->{name,strategies}) を返す。"""
    _b = _rows(DIR / f"{day}_board.csv")
    _meta: dict = {}
    for r in _rows(DIR / f"{day}_signals.csv"):
        _s = _sym(r.get("symbol"))
        if not _s:
            continue
        _m = _meta.setdefault(_s, {"name": "", "strategies": []})
        if not _m["name"]:
            _m["name"] = str(r.get("name") or "")
        _st = str(r.get("strategy") or "").strip()
        if _st and _st not in _m["strategies"]:
            _m["strategies"].append(_st)
    return _b, _meta


def _upsert_log(row: dict) -> None:
    """1日1行。同じ日を作り直したらその行だけ差し替える(重複しない)。"""
    _cur = {r.get("date"): r for r in _rows(LOG)}
    _cur[row["date"]] = row
    try:
        with open(LOG, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLS)
            w.writeheader()
            for _d in sorted(_cur):
                w.writerow({c: _cur[_d].get(c, "") for c in LOG_COLS})
    except Exception as e:
        print(f"  ⚠ {LOG.name} を書けません: {e}")


def _md(day: str, board: list[dict], meta: dict) -> str:
    _pass = [r for r in board if str(r.get("pass_gap")) == "1"]
    _pass.sort(key=lambda r: -_f(r.get("gap_bp")))
    _ord = [r for r in board if str(r.get("ordered")) == "1"]
    _guard = [r for r in board if str(r.get("guard_ng")) == "1"]
    _late = [r for r in board if str(r.get("late")) == "1"]
    _grp: dict = {}
    for r in board:
        _grp.setdefault(str(r.get("grp")), []).append(r)

    _o = [f"# {day} の朝 — J(09:00確認方式)", "",
          f"- 読んだ銘柄: **{len(board)}件**"
          f"（09:00に未寄 {len(_late)}件 = {len(_late) / max(1, len(board)) * 100:.1f}%）",
          f"- 合格（始値が前日終値 +50bp 以上）: **{len(_pass)}件**",
          f"- 実発注: **{len(_ord)}件** / 投入 "
          f"{sum(_f(r.get('yen_k')) for r in board) / 1e4:,.1f}万円",
          f"- ガード超過で見送り: {len(_guard)}件"
          + (f"（{', '.join(_sym(r.get('symbol')) for r in _guard)}）" if _guard else ""),
          ""]
    if len(_grp) > 1:
        _o += ["## 寄った順（グループ）", "",
               "| # | 時刻 | 銘柄数 |", "|---|---|---|"]
        for _g in sorted(_grp, key=lambda x: int(x or 0)):
            _rs = _grp[_g]
            _o.append(f"| {int(_g) + 1} | {_rs[0].get('seen_ts', '')} "
                      f"| {len(_rs)} |")
        _o.append("")

    _o += ["## 合格銘柄", "",
           "| 銘柄 | 名前 | 戦略 | ギャップ | 前日終値 | 始値 | 損切 | 利確 "
           "| 株数 | 投入額 | 寄り | 発注 |",
           "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for r in _pass:
        _s = _sym(r.get("symbol"))
        _m = meta.get(_s, {})
        _mt = re.search(r"(\d{2}:\d{2}:\d{2})", str(r.get("open_time") or ""))
        _o.append(
            f"| {_s} | {_m.get('name', '')} "
            f"| {'/'.join(_m.get('strategies', []))} "
            f"| {_f(r.get('gap_bp')):+.0f}bp "
            f"| {_f(r.get('prev_close')):,.1f} | {_f(r.get('open_p')):,.1f} "
            f"| {_f(r.get('stop_k')):,.1f} | {_f(r.get('target_k')):,.1f} "
            f"| {int(_f(r.get('lots_k'))) * 100:,} | {_f(r.get('yen_k')):,.0f} "
            f"| {_mt.group(1) if _mt else '-'} "
            f"| {'✓ ' + str(r.get('order_limit')) if str(r.get('ordered')) == '1' else ''} |")
    _o += ["",
           "※ 損切 = 始値 + 0.5ATR / 利確 = 始値 − 1.0ATR（ショートなので損切が上）。",
           "※ 発注列の数字は **保護指値**（始値 −50bp）。実約定はこれ以上の値段になる。",
           "※ 株数0 = 合格したが予算を使い切って建てられなかった銘柄。", ""]
    return "\n".join(_o)


def archive(day: str = "", quiet: bool = False) -> bool:
    """その日の朝を保存する。**既にあれば何もしない**(上書き禁止)。"""
    day = str(day or _dt.date.today())[:10]
    _cmp = day.replace("-", "")
    _sig = _BASE / f"k_signals_{_cmp}.csv"
    _brd = _BASE / f"k_paper_{_cmp}.csv"
    if not _brd.exists():
        if not quiet:
            print(f"  [k_morning] {_brd.name} が無いので保存しません")
        return False
    DIR.mkdir(exist_ok=True)
    _md_p = DIR / f"{day}.md"
    if _md_p.exists():
        if not quiet:
            print(f"  [k_morning] {day} は保存済み（上書きしません）")
        return False
    for _src, _dst in ((_sig, DIR / f"{day}_signals.csv"),
                       (_brd, DIR / f"{day}_board.csv")):
        if _src.exists() and not _dst.exists():
            try:
                shutil.copy2(_src, _dst)
            except Exception as e:
                print(f"  ⚠ {_src.name} をコピーできません: {e}")
    _board, _meta = load(day)
    try:
        _md_p.write_text(_md(day, _board, _meta), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠ {_md_p.name} を書けません: {e}")
        return False
    _grp = {str(r.get("grp")) for r in _board}
    _upsert_log({
        "date": day, "saved_on": f"{_dt.date.today()}",
        "signals": len(_rows(DIR / f"{day}_signals.csv")),
        "signal_symbols": len({_sym(r.get("symbol"))
                               for r in _rows(DIR / f"{day}_signals.csv")}),
        "read": len(_board),
        "late": sum(1 for r in _board if str(r.get("late")) == "1"),
        "pass": sum(1 for r in _board if str(r.get("pass_gap")) == "1"),
        "ordered": sum(1 for r in _board if str(r.get("ordered")) == "1"),
        "yen": round(sum(_f(r.get("yen_k")) for r in _board)),
        "guard_ng": sum(1 for r in _board if str(r.get("guard_ng")) == "1"),
        "groups": len(_grp),
    })
    if not quiet:
        print(f"  [k_morning] {day} を保存しました → {_md_p.relative_to(_BASE)}"
              f"（合格 {sum(1 for r in _board if str(r.get('pass_gap')) == '1')}件 / "
              f"発注 {sum(1 for r in _board if str(r.get('ordered')) == '1')}件）")
    return True


def backfill(quiet: bool = True) -> int:
    """手元に残っている k_paper_*.csv から、未保存のぶんを作る。

    ⛔ 元CSVがまだ消えていない日だけ。上書きはしない。
    """
    _n = 0
    for p in sorted(_BASE.glob("k_paper_*.csv")):
        m = re.fullmatch(r"k_paper_(\d{8})", p.stem)
        if not m:
            continue
        _d = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        if archive(_d, quiet=True):
            _n += 1
            if not quiet:
                print(f"  [k_morning] {_d} を作り直しました")
    return _n


def render_html(limit: int = 3) -> str:
    """保存済みの朝を折りたたみで並べる(レポートのシグナルタブ用)。"""
    _ds = dates(limit)
    if not _ds:
        return ""
    _o = ['<details style="margin:14px 0;border:1px solid #334155;'
          'border-radius:8px;padding:10px 12px;background:#0f172a">',
          '<summary style="cursor:pointer;font-weight:700;color:#e2e8f0">'
          f'🌅 朝の J 記録（{" / ".join(_ds)}）</summary>',
          '<p style="color:#94a3b8;font-size:0.8rem;margin:8px 0">'
          '★ <b>09:00 に板で見た始値そのもの</b>です。板・気配の履歴はどの'
          'データにも無いので、この記録は取り直せません（18.35）。<br>'
          '株数0 = 合格したが予算を使い切って建てられなかった銘柄。'
          '発注列は<b>保護指値</b>（始値 −50bp）で、実約定はこれ以上になります。</p>']
    for _d in _ds:
        _board, _meta = load(_d)
        _pass = sorted([r for r in _board if str(r.get("pass_gap")) == "1"],
                       key=lambda r: -_f(r.get("gap_bp")))
        if not _pass:
            continue
        _n_ord = sum(1 for r in _board if str(r.get("ordered")) == "1")
        _o.append(
            f'<h4 style="color:#e2e8f0;margin:12px 0 6px">{_d}'
            f'<span style="color:#94a3b8;font-weight:400;font-size:0.85rem">'
            f'　読み {len(_board)}件 / 合格 {len(_pass)}件 / 発注 {_n_ord}件 / '
            f'投入 {sum(_f(r.get("yen_k")) for r in _board) / 1e4:,.1f}万</span></h4>'
            '<table style="width:100%;border-collapse:collapse;font-size:0.82rem">'
            '<tr style="color:#94a3b8;text-align:right">'
            '<th style="text-align:left">銘柄</th>'
            '<th style="text-align:left">名前</th>'
            '<th style="text-align:left">戦略</th>'
            '<th>ギャップ</th><th>前日終値</th><th>始値</th>'
            '<th>損切</th><th>利確</th><th>株数</th><th>寄り</th>'
            '<th style="text-align:left">発注</th></tr>')
        for r in _pass:
            _s = _sym(r.get("symbol"))
            _m = _meta.get(_s, {})
            _mt = re.search(r"(\d{2}:\d{2}:\d{2})",
                            str(r.get("open_time") or ""))
            _on = str(r.get("ordered")) == "1"
            _c = "#e2e8f0" if _on else "#64748b"
            _o.append(
                f'<tr style="color:{_c};text-align:right;'
                f'border-top:1px solid #1e293b">'
                f'<td style="text-align:left">{_s}</td>'
                f'<td style="text-align:left">{_m.get("name", "")}</td>'
                f'<td style="text-align:left">{"/".join(_m.get("strategies", []))}</td>'
                f'<td>{_f(r.get("gap_bp")):+.0f}bp</td>'
                f'<td>{_f(r.get("prev_close")):,.1f}</td>'
                f'<td>{_f(r.get("open_p")):,.1f}</td>'
                f'<td>{_f(r.get("stop_k")):,.1f}</td>'
                f'<td>{_f(r.get("target_k")):,.1f}</td>'
                f'<td>{int(_f(r.get("lots_k"))) * 100:,}</td>'
                f'<td>{_mt.group(1) if _mt else "-"}</td>'
                f'<td style="text-align:left">'
                f'{"✓ " + str(r.get("order_limit")) if _on else ""}</td></tr>')
        _o.append("</table>")
    _o.append("</details>")
    return "".join(_o)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="朝の J 記録を保存/表示する")
    ap.add_argument("--date", type=str, default="", help="表示する日付")
    ap.add_argument("--list", action="store_true", help="保存済みの日付を一覧")
    ap.add_argument("--backfill", action="store_true",
                    help="手元の k_paper_*.csv から未保存ぶんを作る")
    ap.add_argument("--save", type=str, default="",
                    help="この日付を保存する(既定は今日)")
    a = ap.parse_args()
    if a.backfill:
        print(f"[k_morning] {backfill(quiet=False)}件 作りました")
    elif a.save or (not a.list and not a.date):
        archive(a.save)
    if a.list or (not a.date and not a.backfill):
        _ds = dates()
        print(f"[k_morning] 保存済み {len(_ds)}日: {', '.join(_ds[:15])}"
              + (" …" if len(_ds) > 15 else ""))
        if LOG.exists():
            print(f"  累積ログ: {LOG.name}")
    if a.date:
        p = DIR / f"{a.date}.md"
        if p.exists():
            print(p.read_text(encoding="utf-8"))
        else:
            print(f"[k_morning] {a.date} の記録はありません")
