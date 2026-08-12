r"""check_5m_coverage.py — 5分足がある銘柄と無い銘柄を、流動性で層別して数える。

なぜ要るか
----------
lss / H は同日決済なので、決済判定に **5分足が必須** (`[5分足集計] 5分足の無い
取引は除外されます`)。5分足が無い銘柄日は、日足でシグナルが出て約定していても
損益タブ・予算タブ・ローリングOOS の母集団から **丸ごと消える**。

2026-08-12 実測: 選定1,343ペアに対し E/H の母集団は **573銘柄**。
しかもその日の流動性上位3件(オリエンタルランド151億 / オリンパス95億 /
東洋エンジ74億)が3つとも損益タブに出なかった。

もし欠損が **高流動性側に偏っている** なら:
  * 18.21 で流動性順を選んだ狙い(執行コストを下げる)を検証できていない
  * ローリングOOSの数字は「5分足のある銘柄」だけの話で、実運用の母集団と違う
  * 実発注はシグナルタブに従うので、**検証できていない銘柄に注文が出る**

使い方
------
  python check_5m_coverage.py                        # holdout_selected_symbols.py を見る
  python check_5m_coverage.py --symbols 4661,7733    # 個別に確認
  python check_5m_coverage.py --date 2026-08-12      # その日のバーの有無・先頭時刻も見る
  python check_5m_coverage.py --list-missing 30      # 欠損の流動性上位30件を列挙
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--symbols-file", default="holdout_selected_symbols.py",
                help="SELECTED を持つ .py (既定=レポートが毎回書き出すもの)")
ap.add_argument("--symbols", default="", help="カンマ区切りで直接指定")
ap.add_argument("--date", default="", help="この日のバーの有無と先頭時刻も見る YYYY-MM-DD")
ap.add_argument("--list-missing", type=int, default=20, help="欠損の流動性上位N件を列挙")
ap.add_argument("--first-bar-hist", type=int, default=0,
                help="指定銘柄について直近N営業日の『先頭バー時刻』を数える。"
                     "『いつも09:05』なのか『最近だけ』なのかを切り分ける"
                     "(--symbols と併用)")
ap.add_argument("--dump", type=int, default=0,
                help="--symbols --date と併用。その日の5分足を先頭N本そのまま出す。"
                     "09:00のバーが本当に無いのかを目で確認する用")
ap.add_argument("--raw-dump", type=int, default=0,
                help="--symbols --date と併用。**正規化前の pickle そのもの** を先頭N行出す。"
                     "09:00の行が元データに無いのか、読み込みで消えているのかを切り分ける")
ap.add_argument("--workers", type=int, default=8)
args = ap.parse_args()

try:
    import pandas as pd
    from daytrade_data import load_intraday as _li, split_by_day as _sbd
    from backtest_limit_entry import fetch as _fetch
    import lss_order_rank as _lor
except Exception as e:
    sys.exit(f"[error] 依存モジュールを読めません: {e}")


def _norm(x) -> str:
    return str(x).upper().removesuffix(".T").split(".")[0]


syms: list[str] = []
if args.symbols:
    syms = [_norm(x) for x in args.symbols.split(",") if x.strip()]
else:
    import importlib.util
    from pathlib import Path
    p = Path(args.symbols_file)
    if not p.exists():
        sys.exit(f"[error] {p} がありません。.\\daily を1回流すと作られます")
    spec = importlib.util.spec_from_file_location("_sel", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                      # type: ignore
    sel = getattr(mod, "SELECTED", None) or getattr(mod, "WATCHLIST", None) or []
    seen: set = set()
    for row in sel:
        c = _norm(row[0] if isinstance(row, (list, tuple)) else row)
        if c and c not in seen:
            seen.add(c)
            syms.append(c)
    print(f"[入力] {p.name} から {len(syms):,}銘柄 (ペアではなく銘柄の重複除去後)")

tgt = None
if args.date:
    tgt = pd.to_datetime(args.date).date()


def _one(s: str):
    """(銘柄, 売買代金, 5分足あり, 最新日, 対象日のバー有無, 先頭時刻)"""
    liq = 0.0
    try:
        d = _fetch(f"{s}.T", 200)
        if d is not None and not d.empty:
            liq = float(_lor.daily_turnover(d))
    except Exception:
        pass
    try:
        m = _li(s, 400, source="local")
    except Exception:
        m = None
    if m is None or m.empty:
        return (s, liq, False, "", None, "")
    days = _sbd(m)
    if not days:
        return (s, liq, False, "", None, "")
    last = max(days)
    has_t, first = None, ""
    if tgt is not None:
        if tgt in days:
            has_t = True
            try:
                first = days[tgt].index[0].strftime("%H:%M")
            except Exception:
                first = "?"
        else:
            has_t = False
    return (s, liq, True, str(last), has_t, first)


rows = []
with ThreadPoolExecutor(max_workers=args.workers) as ex:
    futs = [ex.submit(_one, s) for s in syms]
    for i, f in enumerate(as_completed(futs), 1):
        rows.append(f.result())
        if i % 200 == 0:
            print(f"  {i:,}/{len(syms):,} ...", flush=True)

have = [r for r in rows if r[2]]
miss = [r for r in rows if not r[2]]
print(f"\n5分足あり {len(have):,} / なし {len(miss):,} "
      f"(全{len(rows):,}銘柄 / カバー率 {len(have) / max(len(rows), 1) * 100:.1f}%)")

# ── 流動性5分位ごとのカバー率。ここが本題 ────────────────────────────
liq_known = sorted([r for r in rows if r[1] > 0], key=lambda r: r[1])
n = len(liq_known)
if n >= 10:
    print(f"\n■ 流動性5分位ごとの 5分足カバー率 (売買代金の分かる {n:,}銘柄)")
    print(f"  {'分位':<10}{'中央売買代金':>14}{'銘柄':>7}{'5分足あり':>11}{'カバー率':>10}")
    print("  " + "-" * 54)
    for q in range(5):
        g = liq_known[q * n // 5:(q + 1) * n // 5]
        if not g:
            continue
        med = g[len(g) // 2][1]
        ok = sum(1 for r in g if r[2])
        print(f"  {q + 1}/5{'(薄)' if q == 0 else '(厚)' if q == 4 else '':<7}"
              f"{med / 1e8:>12,.1f}億{len(g):>7}{ok:>11}{ok / len(g) * 100:>9.1f}%")
    print("  " + "-" * 54)
    print("  [!] 厚い側のカバー率が低いなら、検証は板の薄い銘柄に偏っています。")
    print("      実発注はシグナルタブに従うので、検証していない銘柄に注文が出ます。")

if miss and args.list_missing > 0:
    ms = sorted(miss, key=lambda r: -r[1])[:args.list_missing]
    print(f"\n■ 5分足が無い銘柄 — 流動性の高い順 上位{len(ms)}件")
    for s, liq, *_ in ms:
        print(f"  {s}  {liq / 1e8:>8,.1f}億")

if tgt is not None:
    ng = [r for r in have if r[4] is False]
    late = [r for r in have if r[4] and r[5] and r[5] != "09:00"]
    print(f"\n■ {tgt} のバー: あり {sum(1 for r in have if r[4]):,} / "
          f"無し {len(ng):,} / 先頭が09:00でない {len(late):,}")
    if late:
        print("  先頭が09:00でない銘柄(寄り直後の逆行が判定から抜けるので E/H は落とす):")
        for s, liq, _h, _l, _t, first in sorted(late, key=lambda r: -r[1])[:15]:
            print(f"    {s}  {liq / 1e8:>7,.1f}億  先頭 {first}")


# ── 先頭バー時刻の履歴。systematic か 最近だけか を切り分ける ────────────
# require_open_bar は「先頭バーが09:00でない銘柄日」を E/H の母集団から落とす
# (18.32: 寄り直後の逆行が判定から抜けて損切りが過小に出るため)。
# それが **いつもそう** なら、その銘柄は構造的に検証対象外ということになる。
# **最近だけ** なら、単なるデータ取得の問題で埋められる。
if args.first_bar_hist > 0 and args.symbols:
    print(f"\n■ 先頭バー時刻の履歴 (直近{args.first_bar_hist}営業日)")
    for s_ in syms:
        try:
            m = _li(s_, 400, source="local")
            days = _sbd(m) if m is not None and not m.empty else {}
        except Exception as e:
            print(f"  {s_}: 読めません ({e})")
            continue
        if not days:
            print(f"  {s_}: 5分足なし")
            continue
        ks = sorted(days)[-args.first_bar_hist:]
        cnt: dict = {}
        for k in ks:
            try:
                t_ = days[k].index[0].strftime("%H:%M")
            except Exception:
                t_ = "?"
            cnt[t_] = cnt.get(t_, 0) + 1
        line = " / ".join(f"{t_}:{c}日" for t_, c in sorted(cnt.items()))
        print(f"  {s_}  {line}")
        print(f"       直近5日: " + ", ".join(
            f"{k}({days[k].index[0].strftime('%H:%M')})" for k in ks[-5:]))


# ── その日の5分足をそのまま出す。推測せず現物を見る ────────────────────
if args.dump > 0 and args.symbols and tgt is not None:
    for s_ in syms:
        print(f"\n■ {s_} の {tgt} 5分足 (先頭{args.dump}本)")
        try:
            m = _li(s_, 400, source="local")
        except Exception as e:
            print(f"  読めません ({e})"); continue
        if m is None or m.empty:
            print("  5分足なし"); continue
        day = m[m.index.normalize() == pd.Timestamp(tgt)]
        if day.empty:
            print(f"  {tgt} のバーが1本もありません")
            _near = sorted({d.date() for d in m.index})[-5:]
            print(f"  この銘柄の直近5営業日: {', '.join(str(d) for d in _near)}")
            continue
        cols = [c for c in ("open", "high", "low", "close", "volume")
                if c in {str(x).lower(): x for x in day.columns}]
        cmap = {str(x).lower(): x for x in day.columns}
        print(f"  バー数 {len(day)}本 / 先頭 {day.index[0].strftime('%H:%M')} "
              f"/ 末尾 {day.index[-1].strftime('%H:%M')}")
        print(f"  {'時刻':<7}{'始値':>9}{'高値':>9}{'安値':>9}{'終値':>9}{'出来高':>12}")
        for ts, r in day.head(args.dump).iterrows():
            print(f"  {ts.strftime('%H:%M'):<7}"
                  + "".join(f"{float(r[cmap[c]]):>9,.1f}" for c in cols if c != "volume")
                  + (f"{float(r[cmap['volume']]):>12,.0f}" if "volume" in cmap else ""))
        # 日足の始値と突き合わせる。5分足の先頭が寄り値と一致するかで
        # 「09:00のバーが欠けている」のか「単にラベルが09:05始まり」なのかが分かる。
        try:
            d = _fetch(f"{s_}.T", 200)
            d.index = pd.to_datetime(d.index).normalize()
            if pd.Timestamp(tgt) in d.index:
                dc = {str(x).lower(): x for x in d.columns}
                o = float(d.loc[pd.Timestamp(tgt), dc["open"]])
                f0 = float(day.iloc[0][cmap["open"]])
                print(f"  日足の始値 {o:,.1f} vs 5分足の先頭始値 {f0:,.1f} → "
                      + ("一致(=寄りは入っている。ラベルが09:05なだけ)" if abs(o - f0) < 0.51
                         else "不一致(=寄りのバーが欠けている)"))
        except Exception:
            pass


# ── 正規化前の pickle をそのまま見る。ここが最後の切り分け ──────────────
# ローダ(normalize_minute_df / resample_to_5m)は先頭バーを落とさない
# (既に5分足なら素通し)。それでも09:00が無いなら **元データに無い**。
if args.raw_dump > 0 and args.symbols and tgt is not None:
    import pickle as _pk
    from daytrade_data import DATA_DIR as _DD, yf_to_jquants as _y2j
    for s_ in syms:
        print(f"\n■ {s_} の pickle 生データ ({tgt} 先頭{args.raw_dump}行)")
        fp = _DD / f"{_y2j(s_)}.pkl"
        if not fp.exists():
            print(f"  {fp} がありません"); continue
        try:
            raw = _pk.loads(fp.read_bytes())
        except Exception as e:
            print(f"  読めません ({e})"); continue
        print(f"  ファイル: {fp.name} / 行数 {len(raw):,} / 列 {list(raw.columns)[:10]}")
        # 日付列を探して当日ぶんに絞る
        sub = None
        for c in ("Date", "date"):
            if c in raw.columns:
                sub = raw[raw[c].astype(str).str[:10] == str(tgt)]
                break
        if sub is None:
            for c in ("DateTime", "datetime"):
                if c in raw.columns:
                    sub = raw[raw[c].astype(str).str[:10] == str(tgt)]
                    break
        if sub is None and isinstance(raw.index, pd.DatetimeIndex):
            sub = raw[raw.index.normalize() == pd.Timestamp(tgt)]
        if sub is None or len(sub) == 0:
            print(f"  {tgt} の行が見つかりません"); continue
        print(f"  {tgt} の行数: {len(sub):,}")
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(sub.head(args.raw_dump).to_string())
            # ⚠ 引けも見る。東証は2024/11から15:30引けなので、末尾が15:20だと
            #    **引けの10分が無い**ことになる。lss/H は引け成行決済なので、
            #    そこが欠けていると決済価格が実際とズレる。
            print("  ... 末尾3行 ...")
            print(sub.tail(3).to_string())
