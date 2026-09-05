r"""show_frozen_signals.py — 『その日、実際に見えていた発注順』を凍結キャッシュから復元する。

なぜ必要か
-----------
過去日を後から再生成しても、シグナルタブの並びは当時と一致するとは限らない。
理由は3つあり、どれも『順位』を動かす:

  ① BTが凍結済みなら当時の値が使われる (`nikkei_analysis.py:2326` `_lookup_frozen_bt`)。
     ただし凍結辞書は (銘柄,戦略) 単位で **最新シグナル日** の値を持つ
     (`run_signals_holdout_all.py:1652-1667`)。表示日より後の値が出うる。
  ② 凍結が無い銘柄は `calc_recommend_score` = **実行日までの365日** で再計算される
     (`check_signals_stop.py:334`)。= その日以降の決済結果込み = 先読み。
  ③ ユニバースの価格フィルタが **最新終値** で切る (CLAUDE.md 18.16)。
     当時は対象だった銘柄が **行ごと消え**、下位が繰り上がって順位がズレる。

一方 `signal_score_cache_lss.json` には、当日の `.\daily` が書いた
**そのときのBT**が (銘柄::戦略::シグナル日) → bt_score で凍結されている。
これが『当時、実際に見えていた順番』の唯一の記録。

本ツールはそれを復元して並べる。再生成したレポートと突き合わせれば、
「この銘柄は当時ほんとうに上位だったのか」がその場で確認できる。
**実務上いちばん効くのは ③ の検出**(当時あったのに今の画面に無い銘柄)。

使い方
------
  python show_frozen_signals.py --date 2026-08-06          # その日の凍結BT順
  python show_frozen_signals.py --date 2026-08-06 --symbol 4553   # 1銘柄だけ
  python show_frozen_signals.py --date 2026-08-06 --today-html signals_holdout_all_both_2026-08-06.html
        # 再生成HTMLのBTを抜き出して『当時 vs 今』を並べて差分を出す

読み方
------
* 『当時BT』が再生成レポートのBTと違う = その銘柄の順位は後から動いている。
* 順位が上がった銘柄 = その後に勝ったのでBTが上がった = **先読み**。
  再生成レポートで上位に見えても、当日は下位で予算に入らなかった可能性がある。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ap = argparse.ArgumentParser(description="凍結キャッシュから当時の発注順を復元する")
ap.add_argument("--date", type=str, required=True, help="シグナル日 YYYY-MM-DD")
ap.add_argument("--json", type=str, default="signal_score_cache_lss.json")
ap.add_argument("--symbol", type=str, default="", help="銘柄コードで絞る(例 4553)")
ap.add_argument("--today-html", type=str, default="",
                help="再生成した signals_*.html。今のBTを抜いて当時と比較する")
ap.add_argument("--limit", type=int, default=60)
args = ap.parse_args()

p = Path(args.json)
if not p.exists():
    sys.exit(f"[error] {p} がありません")
cache: dict = json.loads(p.read_text(encoding="utf-8"))

want = str(args.date).strip()
rows = []
for k, v in cache.items():
    parts = k.split("::")
    if len(parts) != 3 or parts[2] != want:
        continue
    sym, strat, _ = parts
    if args.symbol and args.symbol.strip().upper().removesuffix(".T") \
            not in sym.upper():
        continue
    rows.append({"sym": sym, "strat": strat,
                 "bt": float(v.get("bt_score", 0) or 0),
                 "seen": str(v.get("first_seen", "")),
                 "ts": str(v.get("first_seen_ts", "") or "")})

if not rows:
    print(f"[結果] {want} のシグナルは凍結キャッシュにありません。")
    print("       = その日は .\\daily を走らせていない(または lss シグナルが0件)。")
    print("       この場合『当時の順番』の記録は存在せず、再生成しても復元できません。")
    sys.exit(0)

rows.sort(key=lambda r: -r["bt"])
for i, r in enumerate(rows, 1):
    r["rank"] = i

print(f"[凍結] シグナル日 {want} / {len(rows)}件 / 凍結日 "
      f"{sorted({r['seen'] for r in rows})}")

# ── 再生成HTMLから『今のBT』を抜く ──────────────────────────────────
today_bt: dict[str, float] = {}
if args.today_html:
    hp = Path(args.today_html)
    if not hp.exists():
        sys.exit(f"[error] {hp} がありません")
    html = hp.read_text(encoding="utf-8", errors="ignore")
    # 行(<tr>)ごとに切って、その行の中の銘柄コードと BT: を対応づける。
    # 旧実装は「コードから4000字以内の BT:」を探していたが、実レポートは1行が
    # 長く(インラインstyleが多い)0件になった。行単位なら取りこぼさず誤対応もしない。
    for _row in re.split(r"<tr[\s>]", html):
        _codes = re.findall(r"(\d{4})\.T", _row)
        _bts = re.findall(r"BT:\s*(\d+)", _row)
        if _codes and _bts:
            today_bt.setdefault(f"{_codes[0]}.T", float(_bts[0]))
    print(f"[再生成] {hp.name} から {len(today_bt)}銘柄のBTを抽出")
    if not today_bt:
        print("         ⚠ 0件でした。レポートのHTML構造が想定と違います。"
              "比較はスキップし、当時の並びだけを表示します")
    print("         ※ HTML抽出は <tr> 行ごとに最初の銘柄コードとBTを対応づける近似。"
          "凍結側(当時BT)が正で、こちらは目視確認の補助です")

hdr = f"  {'当時順位':>7}{'銘柄':>10}{'戦略':>9}{'当時BT':>8}"
if today_bt:
    hdr += f"{'今のBT':>8}{'差':>7}"
print(f"\n{'=' * (len(hdr) + 12)}")
print(hdr)
print("  " + "-" * (len(hdr) + 4))

_moved = []
for r in rows[:args.limit]:
    line = f"  {r['rank']:>7}{r['sym']:>10}{r['strat']:>9}{r['bt']:>8.0f}"
    if today_bt:
        tb = today_bt.get(r["sym"])
        if tb is None:
            line += f"{'—':>8}{'':>7}"
        else:
            d = tb - r["bt"]
            line += f"{tb:>8.0f}{d:>+7.0f}"
            if abs(d) >= 1:
                _moved.append((r["sym"], r["strat"], r["bt"], tb, d))
    print(line)

if today_bt:
    _frozen_syms = {r["sym"] for r in rows}
    _new = sorted(set(today_bt) - _frozen_syms)
    # ★ こちらが本命: 当時あったのに再生成レポートから消えた銘柄。
    #   シグナルタブは凍結BTを使うので値は一致しやすいが、ユニバースの価格フィルタが
    #   **最新終値**で切るため(18.16)、当時は対象だった銘柄が行ごと落ちる。
    #   すると下位の銘柄が繰り上がり、順位だけが実際と食い違う。
    _gone = [r for r in rows if r["sym"] not in today_bt]
    print(f"\n{'─' * 78}")
    if _moved:
        print(f"⚠ BTが変わった銘柄 {len(_moved)}件 "
              f"(= その後の決済結果でスコアが動いた = 先読み)")
        for sym, strat, ob, tb, d in sorted(_moved, key=lambda x: -abs(x[4]))[:20]:
            print(f"    {sym:>9} {strat:<8} 当時 {ob:>3.0f} → 今 {tb:>3.0f}  ({d:+.0f})")
    else:
        print("✅ BTの値は当時と一致（シグナルタブは凍結BTを使うため / "
              "nikkei_analysis.py:2326）")
    if _gone:
        print(f"\n⛔ **当時あったのに再生成レポートから消えた銘柄 {len(_gone)}件**")
        for r in _gone[:20]:
            print(f"    当時{r['rank']:>3}位  {r['sym']:>9} {r['strat']:<8} BT {r['bt']:>3.0f}")
        print("    → ユニバースの価格フィルタが**最新終値**で切るため(18.16)、当時は")
        print("      対象だった銘柄が丸ごと落ちる。**下位が繰り上がって順位がズレる**ので、")
        print("      再生成レポートの『順位』は当時のものではない。値が同じでも順位は違う。")
    if _new:
        print(f"\n⚠ 再生成レポートにあるが当時の凍結に無い銘柄 {len(_new)}件:")
        print(f"    {', '.join(_new[:30])}")
        print("    → 当日のリストに載っていなかった(または凍結の取りこぼし)。")

print(f"\n{'─' * 78}")
print("★ 当時の実発注は `.\\fills --date <yyyyMMdd> --save` が出す orders_<日付>.csv が真実。")
print("  上の『当時BT順』と実注文が一致していれば、その日の運用は正しく行われている。")
print("  再生成レポートの順位と食い違っていても、それは再生成側が先読みしているだけ。")
