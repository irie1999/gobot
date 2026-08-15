"""audit_proposals.py — lss_proposal_YYYY-MM.py の件数が月ごとに揃っているか監査する。

なぜ要るか (2026-08-15):
  `lss_proposal_2026-07.py` が **150件**しかない。他の月は 621〜1,012件。
  8月は「初出が7月以前のペア」で動くので、7月の選定が欠けていると
  **いま唯一の純OOS月(8月)の評価が痩せる**。#4〜#9 の測定はすべてこの母集団の
  上で行うので、先に直さないと測り直しになる。

何を見るか:
  scan_lss_universe.py の TRAIN は **「約定日 <= 基準月末」の全取引**なので、
  基準月が進むほど TRAIN は単調に増える。したがって同じ実行(--base-months で
  一括)なら、**後の月ほど合格ペアが増えるのが正常**。減っていたら

    (a) その月だけ別実行(ユニバースが小さい / --limit 付き / 中断)
    (b) --select-top / --max-per-symbol が付いた実行
    (c) 5分足の在庫不足

  のいずれか。生成ファイルの先頭に実行時の引数が刻まれているので、
  ヘッダを並べれば (a)(b) はその場で分かる。

使い方:
  python audit_proposals.py
  python audit_proposals.py --dir "C:\\...\\swingtrade"
"""
from __future__ import annotations

import argparse
import glob
import os
import re
from pathlib import Path

ap = argparse.ArgumentParser(description="lss_proposal の件数・生成条件を監査")
ap.add_argument("--dir", type=str, default=".", help="lss_proposal_*.py があるフォルダ")
args = ap.parse_args()

_files = sorted(glob.glob(str(Path(args.dir) / "lss_proposal_????-??.py")))
if not _files:
    raise SystemExit(f"[error] {Path(args.dir).resolve()} に lss_proposal_YYYY-MM.py がありません")


def _read(path: str) -> dict:
    """ヘッダの実行条件と SELECTED のペア集合を読む(import せず本文を舐める)。"""
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    base = re.search(r"(\d{4}-\d{2})", Path(path).name).group(1)
    # ヘッダ(docstring)は最初の """ ... """
    head = txt.split('"""')[1] if txt.count('"""') >= 2 else ""
    # ⛔ 実際の形式は **3要素・シングルクォート**:
    #     ('7203.T', '銘柄名', 'MACDTF'),  # TRAIN ...
    #    銘柄名にカンマやクォートが入りうるので、行ごとに
    #    「# より前の引用符付きトークン」を拾って 先頭=コード / 末尾=戦略 とする。
    pairs = set()
    for _ln in txt.splitlines():
        _s = _ln.strip()
        if not _s.startswith("("):
            continue
        _s = _s.split("#")[0]
        _tok = re.findall(r"'([^']*)'|\"([^\"]*)\"", _s)
        _tok = [a or b for a, b in _tok]
        if len(_tok) >= 2:
            pairs.add((_tok[0], _tok[-1]))
    # ★ ヘッダの「合格 Nペア」を突き合わせる。パースが壊れたときに
    #   「0件」を本物の異常と読み違えないための保険(2026-08-15 に実際やった)。
    _m = re.search(r"合格\s*([\d,]+)\s*ペア", head)
    n_head = int(_m.group(1).replace(",", "")) if _m else None
    # ★ 効くパラメータだけを名前で抜く。文言の揺れ(「TEST は選定に未使用」等)を
    #   条件差として誤検出しないため、比較はこの辞書で行う。
    par = {}
    for _k2, _re in (("sm", r"sm=([\d.]+)"), ("tm", r"tm=([\d.]+)"),
                     ("slip", r"slip=([\d.]+)"), ("fee", r"fee=([\d.]+)"),
                     ("delay", r"stop_delay_bars=(\d+)"),
                     ("PF", r"PF>=([\d.]+)"), ("取引", r"取引>=(\d+)")):
        _mm = re.search(_re, head)
        par[_k2] = _mm.group(1) if _mm else "—"
    # ⛔ ヘッダには基準月・件数・TRAIN終端日が入るので、そのまま比べると
    #    全ファイルが「別条件」になる。**実行パラメータだけ**を残して正規化する。
    _cond = []
    for _l in head.splitlines():
        _l = _l.strip()
        if not _l or _l.startswith("lss専用"):
            continue
        # ⛔ sm/tm/slip/fee は「基準月(TRAIN終端): YYYY-MM / sm=... 」と
        #    **同じ行**にある。行ごと捨てると比較の意味が無くなるので、
        #    月の部分だけを落として残りは残す。
        _l = re.sub(r"基準月\(TRAIN終端\):\s*\d{4}-\d{2}\s*/?\s*", "", _l)
        _l = re.sub(r"合格\s*[\d,]+\s*ペア。?", "", _l)
        _l = re.sub(r"TRAIN\(\d{4}-\d{2}-\d{2}以前\)", "TRAIN(基準月以前)", _l)
        _l = _l.strip(" 。/")
        if _l:
            _cond.append(_l)
    return {
        "base": base,
        "path": path,
        "n": len(pairs),
        "n_head": n_head,
        "par": par,
        "pairs": pairs,
        "syms": {p[0] for p in pairs},
        "head": " / ".join(_cond),
        "mtime": os.path.getmtime(path),
    }


_rows = [_read(f) for f in _files]

# ⛔ パースが壊れると全月0件になり、本物の異常と見分けがつかない。
#    ヘッダの「合格 Nペア」と突き合わせて、食い違ったらそこで止める。
_mis = [r for r in _rows if r["n_head"] is not None and r["n"] != r["n_head"]]
if _mis:
    print("⛔ 本文のパース結果とヘッダの『合格 Nペア』が食い違っています。")
    print("   SELECTED の書式が変わった可能性があります(このツール側のバグ)。")
    for r in _mis[:5]:
        print(f"   {r['base']}: 本文 {r['n']}件 / ヘッダ {r['n_head']}件  {r['path']}")
    raise SystemExit(1)

_ns = [r["n"] for r in _rows]
_med = sorted(_ns)[len(_ns) // 2]
if _med <= 0:
    raise SystemExit("⛔ 全ファイルが0件です。選定そのものが失敗しています。")

print("=" * 100)
print(f"■ lss_proposal 監査 — {len(_rows)}ファイル / 件数の中央値 {_med}")
print("=" * 100)
print(f"{'基準月':<9}{'ペア':>7}{'銘柄':>7}{'中央比':>8}  {'前月からの増減':>14}  生成日時")
print("-" * 100)

_prev = None
_bad: list[dict] = []
for r in _rows:
    _ratio = r["n"] / max(1, _med)
    _dn = "" if _prev is None else f"{r['n'] - _prev['n']:+,}"
    # ⛔ TRAIN は基準月が進むほど増えるので、**前月より減っていたら異常**。
    _flag = ""
    if _ratio < 0.5:
        _flag = " ⛔ 中央値の半分未満"
        _bad.append(r)
    elif _prev is not None and r["n"] < _prev["n"] * 0.8:
        _flag = " ⚠ 前月から2割以上 減少"
        _bad.append(r)
    import datetime as _dt
    _ts = _dt.datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
    print(f"{r['base']:<9}{r['n']:>7,}{len(r['syms']):>7,}{_ratio:>7.0%}  {_dn:>14}  {_ts}{_flag}")
    _prev = r          # ⛔ これを忘れて「増減」列が全行 空だった(2026-08-15)

print("-" * 100)

# ── 生成条件。★ 文言ではなく **効くパラメータだけ**を突き合わせる ─────────────
#    (「TEST は選定に未使用」のような注記の揺れを条件差と誤検出しないため)
_KEYS = ["sm", "tm", "slip", "fee", "delay", "PF", "取引"]
print(f"\n■ 生成条件(効くパラメータだけ抜粋)")
print(f"  {'基準月':<9}" + "".join(f"{_k2:>9}" for _k2 in _KEYS))
print("  " + "-" * (9 + 9 * len(_KEYS)))
for r in _rows:
    print(f"  {r['base']:<9}" + "".join(f"{r['par'][_k2]:>9}" for _k2 in _KEYS))

_sigs: dict = {}
for r in _rows:
    _sigs.setdefault(tuple(r["par"][_k2] for _k2 in _KEYS), []).append(r["base"])
if len(_sigs) > 1:
    _maxn = max(len(v) for v in _sigs.values())
    print(f"\n  ⛔ 条件が {len(_sigs)}通りに割れています。少数派は **別実行**:")
    for _sg, _bs in sorted(_sigs.items(), key=lambda kv: -len(kv[1])):
        _mark = "★ 多数派" if len(_bs) == _maxn else "⛔ 少数派"
        print(f"     [{_mark}] "
              + " ".join(f"{_k2}={_v}" for _k2, _v in zip(_KEYS, _sg))
              + f"  ← {', '.join(_bs)}")
else:
    print("\n  ✅ 全月おなじ条件で生成されています。")
    print("     → 件数が落ちているなら、原因は引数ではなく **実行そのもの**")
    print("        (中断 / ユニバースが小さい / 5分足の在庫不足)。")

# ── ★ 実機の設定と合っているか。合っていないと『別の戦略で選んだ銘柄』になる ──
_LIVE = {"fee": ("0", "18.14 実口座は信用大口優遇プランで手数料無料"),
         "delay": (os.environ.get("LSS_STOP_DELAY_BARS", "1"),
                   "daily.bat / watch.bat と揃える(18.9 の鉄則)")}
_ng = []
for _k2, (_want, _why) in _LIVE.items():
    _got = {r["par"][_k2] for r in _rows}
    _bad2 = {g for g in _got if g != "—" and float(g or 0) != float(_want)}
    if _bad2:
        _ng.append((_k2, _want, sorted(_got), _why))
if _ng:
    print("\n  ⛔ 実機の設定と食い違っています(選定そのものが別条件で行われている):")
    for _k2, _want, _got, _why in _ng:
        print(f"     {_k2}: 提案ファイル {_got} / 実機 {_want}  — {_why}")
    print("     ⚠ 選定は『どの銘柄×戦略を使うか』を決める工程なので、ここが違うと")
    print("        母集団そのものが別物になります。#4〜#8 を測る前に揃えること。")

# ── 包含関係。TRAIN は累積なので、後の月は前の月をほぼ含むはず ────────────────
print("\n■ 前月ペアの引き継ぎ率 (TRAIN は累積なので、正常なら高い)")
for _i in range(1, len(_rows)):
    _a, _b = _rows[_i - 1], _rows[_i]
    _keep = len(_a["pairs"] & _b["pairs"]) / max(1, len(_a["pairs"]))
    _mk = " ⛔" if _keep < 0.5 else ("  ⚠" if _keep < 0.75 else "")
    print(f"  {_a['base']} → {_b['base']}: {_keep:>6.0%} "
          f"({len(_a['pairs'] & _b['pairs']):,}/{len(_a['pairs']):,}){_mk}")

if _bad:
    _bl = ",".join(r["base"] for r in _bad)
    print("\n" + "=" * 100)
    print(f"⛔ 作り直しが要る基準月: {_bl}")
    print("=" * 100)
    print("  多数派と同じ引数で、その月だけ作り直します(--base-months はカンマ区切りで一括可):")
    print(f"\n    python scan_lss_universe.py --base-months {_bl} --workers 8\n")
    print("  ⚠ 上の『生成条件』の多数派と同じ sm/tm/slip/しきい値になるよう、")
    print("     足りない引数は多数派の行から写して付けること。")
    print("  ⚠ 作り直したら merge_lss_proposals が読み直すので、次の .\\daily は")
    print("     BTキャッシュ再構築で遅くなります。")
else:
    print("\n✅ 件数・生成条件とも揃っています。作り直しは不要です。")
