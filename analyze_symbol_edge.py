"""analyze_symbol_edge.py — 『銘柄ごとに損益の差があるか』を正しく測る。

⛔⛔ **「差があるか」は問いとして間違っている。**

  どんな乱数でも銘柄ごとに集計すれば差は出る。104銘柄あれば、たまたま
  勝った銘柄と負けた銘柄に必ず分かれる。だから見るべきは

      **その差が『次の期間』でも続くか**（= 銘柄が選択軸として使えるか）

  であって、過去の分散そのものではない。ここを取り違えると
  「勝っている銘柄だけ残す」という最悪の過剰適合になる。
  CLAUDE.md 18.12 の BTスコアが、まさにそれで失敗している
  (過去の成績で選ぶ → 先読みを外したら識別力ゼロだった)。

このツールがやること
--------------------
  1. 取引を **日付で TRAIN / TEST に分割**する
  2. TRAIN の 1株あたり損益(bp)で銘柄を5分位に分ける
  3. **TEST でその分位の成績を見る**。TRAIN で良かった銘柄が TEST でも
     良ければ『銘柄は軸として使える』
  4. TRAIN↔TEST の順位相関(Spearman)を出す
  5. **帰無較正**: 銘柄ラベルを『同じ日の中で』シャッフルして同じ計算をし、
     偶然どれくらいの相関/差が出るかの帯を作る

統計の作法 (CLAUDE.md 18.13 で踏んだ罠をそのまま避ける)
--------------------------------------------------------
  ・**同日相関**: lss/J は同日決済なので、下げた日は全銘柄がまとめて勝つ。
    件数で t を計算すると実効サンプルを誤認する → **日クラスタ頑健**で出す。
  ・**帰無はラベルだけ日内シャッフル**。日をまたいでシャッフルすると日効果まで
    壊れて帰無分布が狭くなり、偽陽性を過小評価する(18.13)。
  ・**bp で比べる**。資金均等は『予算 ÷ その日の件数』なので株数が日によって
    変わり、円/件は「その日に何件建てたか」で動く(18.38 の欠落日検算と同じ罠)。
  ・**最低取引数**を両側に課す。3件しかない銘柄の平均は情報ではない。

使い方
------
  python analyze_symbol_edge.py                     # lss_trades_K.csv (J) を自動検出
  python analyze_symbol_edge.py --csv lss_trades.csv
  python analyze_symbol_edge.py --min-trades 8      # 両側での最低取引数
  python analyze_symbol_edge.py --split 2026-05-01  # 分割日を明示(既定は中央値)
  python analyze_symbol_edge.py --by strategy       # 銘柄ではなく戦略で見る
  python analyze_symbol_edge.py --seeds 200         # 帰無較正の本数
"""
from __future__ import annotations

import argparse
import csv as _csv
import random
import statistics as _st
from pathlib import Path

ap = argparse.ArgumentParser(
    description="銘柄別の損益差が『次の期間でも続くか』を測る(過去の分散ではなく持続性)")
ap.add_argument("--csv", type=str, default="",
                help="取引CSV。既定は lss_trades_K.csv → lss_trades.csv の順で自動検出")
ap.add_argument("--by", type=str, default="symbol",
                choices=["symbol", "strategy", "pair"],
                help="層別の単位。pair = 銘柄×戦略")
ap.add_argument("--min-trades", type=int, default=6,
                help="TRAIN/TEST **それぞれ**で必要な最低取引数")
ap.add_argument("--split", type=str, default="",
                help="TRAIN/TEST の分割日 (YYYY-MM-DD)。既定は取引日の中央")
ap.add_argument("--quantiles", type=int, default=5, help="分位の数")
ap.add_argument("--seeds", type=int, default=200, help="帰無較正の本数")
ap.add_argument("--exclude-strat", type=str, default="転換",
                help="除外する戦略(カンマ区切り)。転換は実装不可なので既定で除外")
args = ap.parse_args()


def _pick_csv() -> Path:
    if args.csv:
        p = Path(args.csv)
        if not p.exists():
            raise SystemExit(f"[error] {p} がありません")
        return p
    for _n in ("lss_trades_K.csv", "lss_trades.csv"):
        if Path(_n).exists():
            return Path(_n)
    raise SystemExit("[error] lss_trades_K.csv も lss_trades.csv もありません。\n"
                     "  先に .\\dailyfast --no-serve を流してください")


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return d


# ══════════════════════════════════════════════════════════════════════
#  読み込み
# ══════════════════════════════════════════════════════════════════════
_path = _pick_csv()
_skip = {s.strip() for s in args.exclude_strat.split(",") if s.strip()}
_rows: list[dict] = []
for r in _csv.DictReader(open(_path, encoding="utf-8-sig")):
    if str(r.get("reason") or "") in ("約定せず", "発注中", "保有中"):
        continue
    if str(r.get("strategy") or "") in _skip:
        continue
    _d = str(r.get("entry_date") or "")[:10]
    _ep, _q = _f(r.get("entry_p")), _f(r.get("qty"))
    _pnl = _f(r.get("pnl"))
    if len(_d) != 10 or _ep <= 0 or _q <= 0:
        continue
    # ★ **bp で持つ**。円/件は資金均等で株数が変わるので比較できない。
    _rows.append({
        "d": _d,
        "key": (str(r.get("symbol") or "") if args.by == "symbol"
                else str(r.get("strategy") or "") if args.by == "strategy"
                else f"{r.get('symbol')}/{r.get('strategy')}"),
        "name": str(r.get("name") or ""),
        "bp": _pnl / (_ep * _q) * 1e4,
        "pnl": _pnl,
    })
if not _rows:
    raise SystemExit(f"[error] {_path} に決済済みの取引がありません")

_dates = sorted({r["d"] for r in _rows})
_split = args.split or _dates[len(_dates) // 2]
_tr = [r for r in _rows if r["d"] < _split]
_te = [r for r in _rows if r["d"] >= _split]

_LBL = {"symbol": "銘柄", "strategy": "戦略", "pair": "銘柄×戦略"}[args.by]
print(f"[入力] {_path.name} — 決済 {len(_rows):,}件 / {len(_dates)}営業日 "
      f"({_dates[0]} 〜 {_dates[-1]}) / 単位={_LBL}")
if _skip:
    print(f"  除外した戦略: {', '.join(sorted(_skip))}")
print(f"[分割] TRAIN {_dates[0]}〜(〜{_split}) {len(_tr):,}件 / "
      f"TEST {_split}〜 {len(_te):,}件")
if not _tr or not _te:
    raise SystemExit("[error] TRAIN か TEST が空です。--split を見直してください")


# ══════════════════════════════════════════════════════════════════════
#  ① 過去の分散そのもの (これは『差がある』の答えだが、意味は薄い)
# ══════════════════════════════════════════════════════════════════════
def _agg(rows: list[dict]) -> dict:
    _m: dict = {}
    for r in rows:
        _e = _m.setdefault(r["key"], {"n": 0, "bp": 0.0, "pnl": 0.0,
                                      "name": r["name"]})
        _e["n"] += 1
        _e["bp"] += r["bp"]
        _e["pnl"] += r["pnl"]
    for _e in _m.values():
        _e["avg"] = _e["bp"] / max(1, _e["n"])
    return _m


_A = _agg(_rows)
_all = sorted(_A.items(), key=lambda kv: -kv[1]["avg"])
_enough = [(k, v) for k, v in _all if v["n"] >= args.min_trades]
print(f"\n{'=' * 74}\n① 全期間の {_LBL}別ばらつき — "
      f"{len(_A):,}{_LBL} (うち{args.min_trades}件以上 {len(_enough):,})\n{'=' * 74}")
if _enough:
    _av = [v["avg"] for _, v in _enough]
    print(f"  1株あたり(bp)  平均 {_st.mean(_av):+.1f} / "
          f"σ {(_st.stdev(_av) if len(_av) > 1 else 0):.1f} / "
          f"最良 {max(_av):+.1f} / 最悪 {min(_av):+.1f}")
    print(f"  ⛔ **これは『差がある』の答えだが、ほぼ意味がない。**"
          f"乱数でも同じ形の表は必ず出る。\n"
          f"     使えるかどうかは ② の『TRAIN で良かったものが TEST でも"
          f"良いか』で決まる。")
    print(f"\n  上位5 / 下位5 ({args.min_trades}件以上):")
    for _k, _v in _enough[:5] + [("…", None)] + _enough[-5:]:
        if _v is None:
            print("      …")
            continue
        print(f"    {_k:<14}{_v['name'][:12]:<13}{_v['n']:>4}件 "
              f"{_v['avg']:>+8.1f}bp  ({_v['pnl']:>+10,.0f}円)")


# ══════════════════════════════════════════════════════════════════════
#  ② 持続性: TRAIN の順位が TEST で再現するか  ← **これが本題**
# ══════════════════════════════════════════════════════════════════════
_TR, _TE = _agg(_tr), _agg(_te)
_both = sorted(k for k in _TR if k in _TE
               and _TR[k]["n"] >= args.min_trades
               and _TE[k]["n"] >= args.min_trades)
print(f"\n{'=' * 74}\n② ★ 持続性 — TRAIN で良かった {_LBL} は TEST でも良いか"
      f"\n{'=' * 74}")
print(f"  両側で {args.min_trades}件以上ある {_LBL}: **{len(_both)}**"
      f"  (TRAIN {len(_TR)} / TEST {len(_TE)})")
if len(_both) < 8:
    # ★★ **「足りない」で終わらせない**。どれだけ足りないのか、そもそも
    #   届くのかまで出す。届かないなら「銘柄では選べない」が結論になる。
    _per = len(_rows) / max(1, len(_A))
    _mon_now = len(_dates) / 21.0
    print(f"  ⛔ {len(_both)}しかないので **判定できません**。")
    print(f"\n  ★ どれだけ足りないか:")
    print(f"    現状 {len(_rows):,}件 / {len(_A):,}{_LBL} = "
          f"**{_per:.2f}件/{_LBL}** ({_mon_now:.1f}ヶ月)")
    for _need in (4, args.min_trades, 8):
        _tot = _need * 2
        print(f"    両側{_need}件(計{_tot}件) … 現状の {_tot / _per:.1f}倍 = "
              f"約 **{_tot / _per * _mon_now:.0f}ヶ月**ぶん")
    # 5分足の上限。J は5分足が無いと1件も作れないので、ここが天井。
    _AVAIL_MON = 25.0     # 2024-07 が最古 / 2年ローリング (CLAUDE.md 18.6)
    _max_per = _per * _AVAIL_MON / _mon_now
    print(f"\n  ⛔ **5分足は 2024-07 が最古**(2年ローリング / 18.6)。"
          f"使える最大の窓は約 {_AVAIL_MON:.0f}ヶ月")
    print(f"     → 全部使っても 1{_LBL}あたり {_max_per:.1f}件 = "
          f"**片側 {_max_per / 2:.1f}件**")
    if _max_per / 2 < args.min_trades:
        print(f"\n  ★★ つまり **{_LBL}別の選別はデータ量の面から測れません**"
              f"(全データを使っても片側 {_max_per / 2:.1f}件 < {args.min_trades}件)。\n"
              f"     窓を伸ばしても届かないので、これは『まだ分からない』ではなく\n"
              f"     **『この戦略では{_LBL}を選択軸にできない』**という結論です。\n"
              f"     ⛔ ①の表で上位/下位に見える{_LBL}は数件の平均です。"
              f"そこから銘柄を選ぶのは 18.12 の BTスコアと同じ失敗になります。")
    else:
        print(f"\n  → 窓を伸ばせば届きます: **.\\dailyfast --no-serve --days 730**\n"
              f"     そのあと python analyze_symbol_edge.py --min-trades 4\n"
              f"     ⚠ ただし片側4件では検出力がほぼ無く、"
              f"本当に力があっても ❌ になりがちです。")
    print(f"\n  ★ いま測れるのは **戦略単位**です(6戦略 × {len(_rows):,}件):")
    print(f"     python analyze_symbol_edge.py --by strategy --min-trades 30")
    raise SystemExit(0)


def _spearman(a: list[float], b: list[float]) -> float:
    def _rk(v):
        _s = sorted(range(len(v)), key=lambda i: v[i])
        _r = [0.0] * len(v)
        for _i, _j in enumerate(_s):
            _r[_j] = float(_i)
        return _r
    _ra, _rb = _rk(a), _rk(b)
    _n = len(a)
    _ma, _mb = sum(_ra) / _n, sum(_rb) / _n
    _num = sum((_ra[i] - _ma) * (_rb[i] - _mb) for i in range(_n))
    _da = sum((x - _ma) ** 2 for x in _ra) ** 0.5
    _db = sum((x - _mb) ** 2 for x in _rb) ** 0.5
    return _num / (_da * _db) if _da > 0 and _db > 0 else 0.0


_x = [_TR[k]["avg"] for k in _both]
_y = [_TE[k]["avg"] for k in _both]
_rho = _spearman(_x, _y)

# ── TRAIN分位ごとの TEST 成績 ────────────────────────────────────────
_q = max(2, args.quantiles)
_ord = sorted(_both, key=lambda k: _TR[k]["avg"])
_bins: list[list[str]] = [[] for _ in range(_q)]
for _i, _k in enumerate(_ord):
    _bins[min(_q - 1, _i * _q // len(_ord))].append(_k)
print(f"\n  TRAIN の成績で {_q}分位に分け、**TEST の成績**を見る:")
print(f"  {'分位':<12}{_LBL:>6}{'TRAIN bp':>11}{'TEST 件':>9}{'TEST bp':>10}")
_qte = []
for _i, _bk in enumerate(_bins):
    if not _bk:
        continue
    _trbp = sum(_TR[k]["bp"] for k in _bk) / sum(_TR[k]["n"] for k in _bk)
    _n_te = sum(_TE[k]["n"] for k in _bk)
    _tebp = sum(_TE[k]["bp"] for k in _bk) / max(1, _n_te)
    _qte.append(_tebp)
    _tag = ("最悪" if _i == 0 else "最良" if _i == len(_bins) - 1 else f"Q{_i + 1}")
    print(f"  {_tag:<12}{len(_bk):>6}{_trbp:>+11.1f}{_n_te:>9,}{_tebp:>+10.1f}")
_spread = (_qte[-1] - _qte[0]) if len(_qte) >= 2 else 0.0
print(f"\n  最良分位 − 最悪分位 (TEST) = **{_spread:+.1f}bp**")
print(f"  TRAIN↔TEST の順位相関 (Spearman) = **{_rho:+.3f}**")

# ══════════════════════════════════════════════════════════════════════
#  ③ 帰無較正 — 銘柄ラベルを『同じ日の中で』入れ替えて同じ計算をする
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 74}\n③ 帰無較正 — {_LBL}に力が無くても偶然どこまで出るか"
      f"\n{'=' * 74}")
print(f"  ラベルを **同じ日の中だけ** で入れ替えます(日効果を壊さないため / 18.13)。"
      f"  {args.seeds}本")

_by_day: dict = {}
for r in _rows:
    _by_day.setdefault(r["d"], []).append(r)

_n_rho, _n_spr = [], []
for _s in range(args.seeds):
    _rnd = random.Random(1000 + _s)
    _sh: list[dict] = []
    for _d, _rs in _by_day.items():
        _keys = [r["key"] for r in _rs]
        _rnd.shuffle(_keys)
        for _r, _k in zip(_rs, _keys):
            _sh.append({"d": _d, "key": _k, "name": "", "bp": _r["bp"],
                        "pnl": _r["pnl"]})
    _str = _agg([r for r in _sh if r["d"] < _split])
    _ste = _agg([r for r in _sh if r["d"] >= _split])
    _sb = [k for k in _str if k in _ste
           and _str[k]["n"] >= args.min_trades
           and _ste[k]["n"] >= args.min_trades]
    if len(_sb) < 8:
        continue
    _n_rho.append(_spearman([_str[k]["avg"] for k in _sb],
                            [_ste[k]["avg"] for k in _sb]))
    _so = sorted(_sb, key=lambda k: _str[k]["avg"])
    _lo = _so[:max(1, len(_so) // _q)]
    _hi = _so[-max(1, len(_so) // _q):]
    _lb = sum(_ste[k]["bp"] for k in _lo) / max(1, sum(_ste[k]["n"] for k in _lo))
    _hb = sum(_ste[k]["bp"] for k in _hi) / max(1, sum(_ste[k]["n"] for k in _hi))
    _n_spr.append(_hb - _lb)

if len(_n_rho) < 20:
    print("  ⛔ 帰無を十分に作れませんでした(条件を満たす層が少なすぎます)")
    raise SystemExit(0)


def _z(v: float, null: list[float]) -> tuple[float, float]:
    _m = _st.mean(null)
    _s = _st.stdev(null) if len(null) > 1 else 0.0
    _zz = (v - _m) / _s if _s > 0 else 0.0
    _p = sum(1 for x in null if abs(x - _m) >= abs(v - _m)) / len(null)
    return _zz, _p


_zr, _pr = _z(_rho, _n_rho)
_zs, _ps = _z(_spread, _n_spr)
print(f"  順位相関   実測 {_rho:+.3f}  / 帰無 平均 {_st.mean(_n_rho):+.3f} "
      f"σ {_st.stdev(_n_rho):.3f}  → **z={_zr:+.2f}  両側p={_pr:.3f}**")
print(f"  分位スプレッド 実測 {_spread:+.1f}bp / 帰無 平均 "
      f"{_st.mean(_n_spr):+.1f} σ {_st.stdev(_n_spr):.1f}  "
      f"→ **z={_zs:+.2f}  両側p={_ps:.3f}**")

# ══════════════════════════════════════════════════════════════════════
#  判定
# ══════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 74}\n判定\n{'=' * 74}")
_ok = (_pr < 0.05 and _ps < 0.05 and _rho > 0 and _spread > 0)
if _ok:
    print(f"  ✅ **{_LBL}に持続性がある**(順位相関 p={_pr:.3f} / "
          f"スプレッド p={_ps:.3f} が両方とも帰無の外)。")
    print(f"     ⚠ ただし即採用しないこと。次は必ず **予算シミュ**で確かめる"
          f"(18.10: 『全部買えるなら得』と『予算内でどれを買うか』は別問題)。")
else:
    print(f"  ❌ **{_LBL}は選択軸として使えません。**")
    _why = []
    if _pr >= 0.05:
        _why.append(f"順位相関が帰無の中 (p={_pr:.3f})")
    if _ps >= 0.05:
        _why.append(f"分位スプレッドが帰無の中 (p={_ps:.3f})")
    if _rho <= 0:
        _why.append("順位相関が負(TRAINで良かったものがTESTで悪い)")
    if _spread <= 0:
        _why.append("最良分位がTESTで最悪分位に負けている")
    for _w in _why:
        print(f"     ・{_w}")
    print(f"\n  ★ ①の表で見えた『銘柄ごとの差』は **過去のばらつき**であって、"
          f"未来の予測力ではありません。\n"
          f"     勝っている銘柄だけ残す運用は 18.12 の BTスコアと同じ失敗になります"
          f"(過去成績で選ぶ → 先読みを外したら識別力ゼロ)。")
print(f"\n  ※ {_LBL} {len(_both)} / TRAIN {len(_tr):,}件・TEST {len(_te):,}件 "
      f"/ 帰無 {len(_n_rho)}本。窓が短いと何も出ません(.\\daily --days 730 で伸ばせます)")
