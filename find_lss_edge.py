"""find_lss_edge.py — lss の取引を『発注時点で分かる属性』で層別し、エッジを探す。

なぜ必要か
-----------
BTスコアには識別力が無いことが確定した(CLAUDE.md 18.12)。先読みを除いたら
勝率が全BT帯で 40.8〜42.5% とフラットになり、合計損益も非単調だった。
つまり **今の銘柄選択軸は機能していない**。代わりになる軸を探す必要がある。

このツールがやること
--------------------
compare_lss_rules.py --audit が出す全トレードCSVを読み、各トレードに
**発注時点で入手可能な属性だけ**を付けて層別する。

  ⛔ MAE/MFE/損益のような『結果を見ないと分からない値』は絶対に使わない。
     それで層別すると必ず綺麗な表が出るが、実運用では再現できない(今日の先読みと同じ)。

そして **必ず TRAIN と TEST を並べて出す**。TRAIN だけ良い仕切りは過剰適合なので、
片方だけの数字を見て判断できないようにしてある。

属性の『使えるタイミング』
--------------------------
  前夜  … 発注を出す時点で分かる。そのまま発注ルールにできる
  寄り  … 当日09:00の寄り値が要る。cancel_gap_orders で寄り後に取消すなら使える
どちらかを表に出す。「寄り」の仕切りを採用するなら取消の実装が要る。

使い方
------
  # 1) 監査CSVを作る (compare_lss_rules 側。ルール名は実運用に合わせる)
  python compare_lss_rules.py --days 240 --min-price 1000 --max-price 6000 \
      --bt-min 0 --audit delay1
  # → lss_audit_delay1_<date>.csv

  # 2) エッジ探索
  python find_lss_edge.py                       # 最新の lss_audit_*.csv を自動検出
  python find_lss_edge.py --audit lss_audit_delay1_2026-08-07.csv
  python find_lss_edge.py --split 2026-04-01    # TRAIN/TEST の境目を明示
  python find_lss_edge.py --bins 4 --min-n 40   # 分位数と最小サンプル
  python find_lss_edge.py --no-market           # 日経の取得を省く(オフライン時)

読み方
------
  ・TRAIN と TEST で **符号が揃っている** 帯だけが候補。
  ・片方だけ大きい/符号が反転する属性はノイズ。採用しない。
  ・最後の【候補】に、両方で生き残った仕切りだけが出る。ここが空なら
    「発注時点で分かる属性では選別できない」= 全部取るのが正解、という結論。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(description="lss の発注時点属性でエッジを探す(TRAIN/TEST同時)")
ap.add_argument("--audit", type=str, default=None,
                help="compare_lss_rules --audit が出したCSV。省略時は最新の lss_audit_*.csv")
ap.add_argument("--pnl-col", type=str, default="pnl_現実",
                help="評価に使う損益列 (pnl_現実 / pnl_保守 / pnl_report_楽観)")
ap.add_argument("--split", type=str, default="0.5",
                help="TRAIN/TEST の境目。日付(2026-04-01)か比率(0.5=前半半分がTRAIN)")
ap.add_argument("--bins", type=int, default=5, help="分位数の数(既定5=五分位)")
ap.add_argument("--min-n", type=int, default=30, help="この件数未満の帯は『サンプル少』印を付ける")
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--no-market", action="store_true", help="日経(^N225)を取らない")
ap.add_argument("--out", type=str, default="", help="属性付きの全トレードCSVを書き出す")
args = ap.parse_args()

# ── 監査CSVの読込 ──────────────────────────────────────────────────────
_path = Path(args.audit) if args.audit else None
if _path is None:
    cands = sorted(Path(".").glob("lss_audit_*.csv"))
    if not cands:
        sys.exit("[error] lss_audit_*.csv がありません。先に compare_lss_rules を "
                 "--audit 付きで実行してください:\n"
                 "  python compare_lss_rules.py --days 240 --min-price 1000 "
                 "--max-price 6000 --bt-min 0 --audit delay1")
    _path = cands[-1]
    print(f"[監査CSV] 自動検出: {_path.name}")
if not _path.exists():
    sys.exit(f"[error] {_path} がありません")

df_t = pd.read_csv(_path, encoding="utf-8-sig")
if args.pnl_col not in df_t.columns:
    sys.exit(f"[error] 損益列 {args.pnl_col} がありません。候補: "
             f"{[c for c in df_t.columns if str(c).startswith('pnl')]}")
df_t["date"] = pd.to_datetime(df_t["date"], errors="coerce")
df_t = df_t.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
df_t["pnl"] = pd.to_numeric(df_t[args.pnl_col], errors="coerce").fillna(0.0)
print(f"[監査CSV] {len(df_t):,}トレード / {df_t['date'].min().date()} 〜 "
      f"{df_t['date'].max().date()} / 損益列={args.pnl_col} 合計 {df_t['pnl'].sum():+,.0f}円")

# ── 日足データを取得(属性計算用) ──────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from backtest_limit_entry import fetch as _fetch
except Exception as e:                                    # pragma: no cover
    sys.exit(f"[error] backtest_limit_entry を読めません: {e}")

_syms = sorted({str(s) for s in df_t["sym"].astype(str)})
_need_days = int((df_t["date"].max() - df_t["date"].min()).days) + 120
print(f"[日足] {len(_syms)}銘柄 × 約{_need_days}日 を取得中 (キャッシュがあれば即座)...")

_DFS: dict = {}


def _load(sym: str):
    try:
        d = _fetch(sym if "." in sym else f"{sym}.T", _need_days)
        if d is None or len(d) < 40:
            return sym, None
        return sym, d
    except Exception:
        return sym, None


with ThreadPoolExecutor(max_workers=args.workers) as ex:
    for f in as_completed([ex.submit(_load, s) for s in _syms]):
        s, d = f.result()
        if d is not None:
            _DFS[s] = d
print(f"[日足] 取得できたのは {len(_DFS)}/{len(_syms)}銘柄")
if not _DFS:
    sys.exit("[error] 日足が1銘柄も取れませんでした。キャッシュ(.rsi2_cache)を確認してください")

# ── 日経 (相場環境) ───────────────────────────────────────────────────
_N = None
if not args.no_market:
    try:
        _N = _fetch("^N225", _need_days)
        if _N is not None and len(_N) > 30:
            print(f"[日経] {len(_N)}本 取得")
        else:
            _N = None
    except Exception:
        _N = None
    if _N is None:
        print("[日経] 取得できませんでした。相場環境の属性はスキップします")


def _atr_pct(h, l, c, i, n=14) -> float:
    """i-1 までのバーで ATR(n) を出し、終値比%で返す(発注時点で計算できる)。"""
    if i - n - 1 < 0:
        return np.nan
    tr = np.maximum(h[i - n:i] - l[i - n:i],
                    np.maximum(np.abs(h[i - n:i] - c[i - n - 1:i - 1]),
                               np.abs(l[i - n:i] - c[i - n - 1:i - 1])))
    return float(tr.mean() / c[i - 1] * 100) if c[i - 1] else np.nan


# ── トレードごとの属性 ────────────────────────────────────────────────
# ★ 使ってよいのは「エントリー日の前日終値まで」の情報だけ。
#   例外: 当日の寄り値(gap)は09:00に分かるので、寄り後に取消す運用なら使える(=区分「寄り」)。
_ATTRS: list[dict] = []
_miss = 0
for r in df_t.itertuples(index=False):
    d = _DFS.get(str(r.sym))
    if d is None:
        _miss += 1
        _ATTRS.append({})
        continue
    idx = d.index.searchsorted(pd.Timestamp(r.date))
    if idx >= len(d) or idx < 25 or d.index[idx].date() != pd.Timestamp(r.date).date():
        _miss += 1
        _ATTRS.append({})
        continue
    c = d["close"].to_numpy(dtype=float)
    h = d["high"].to_numpy(dtype=float)
    lo = d["low"].to_numpy(dtype=float)
    o = d["open"].to_numpy(dtype=float)
    v = d["volume"].to_numpy(dtype=float)
    cp = c[idx - 1]
    a = {
        "株価": float(cp),
        "ATR%": _atr_pct(h, lo, c, idx),
        "前日リターン%": float(cp / c[idx - 2] - 1) * 100 if c[idx - 2] else np.nan,
        "5日リターン%": float(cp / c[idx - 6] - 1) * 100 if c[idx - 6] else np.nan,
        "20日リターン%": float(cp / c[idx - 21] - 1) * 100 if c[idx - 21] else np.nan,
        "出来高比": float(v[idx - 1] / v[idx - 21:idx - 1].mean())
        if v[idx - 21:idx - 1].mean() > 0 else np.nan,
        "売買代金_億": float(cp * v[idx - 21:idx - 1].mean() / 1e8),
        "曜日": int(pd.Timestamp(r.date).weekday()),
    }
    # 20日レンジ内の位置 (1.0=20日高値圏 / 0.0=安値圏)
    _hi, _lo = h[idx - 20:idx].max(), lo[idx - 20:idx].min()
    a["20日レンジ位置"] = float((cp - _lo) / (_hi - _lo)) if _hi > _lo else np.nan
    # 連続上昇日数(前日まで)
    _st = 0
    for j in range(idx - 1, max(idx - 12, 1), -1):
        if c[j] > c[j - 1]:
            _st += 1
        else:
            break
    a["連続上昇日数"] = _st
    # 当日の寄りギャップ (区分「寄り」= 09:00に分かる)
    a["寄りギャップ%"] = float(o[idx] / cp - 1) * 100 if cp else np.nan
    _ATTRS.append(a)

_att = pd.DataFrame(_ATTRS)
df = pd.concat([df_t.reset_index(drop=True), _att], axis=1)

# 相場環境 (日付単位で結合)
if _N is not None:
    nc = _N["close"].to_numpy(dtype=float)
    no = _N["open"].to_numpy(dtype=float)
    nmap_r, nmap_g = {}, {}
    for i in range(1, len(_N)):
        dd = _N.index[i].date()
        nmap_r[dd] = float(nc[i - 1] / nc[i - 2] - 1) * 100 if i >= 2 and nc[i - 2] else np.nan
        nmap_g[dd] = float(no[i] / nc[i - 1] - 1) * 100 if nc[i - 1] else np.nan
    _dts = df["date"].dt.date
    df["日経_前日リターン%"] = _dts.map(nmap_r)
    df["日経_寄りギャップ%"] = _dts.map(nmap_g)

if _miss:
    print(f"[!] 属性を計算できなかったトレード {_miss}件 (日足が無い/日付が合わない)。集計から除外")

# ── TRAIN / TEST 分割 ─────────────────────────────────────────────────
try:
    _ratio = float(args.split)
    _cut = df["date"].quantile(_ratio)
except ValueError:
    _cut = pd.Timestamp(args.split)
tr = df[df["date"] < _cut].copy()
te = df[df["date"] >= _cut].copy()
print(f"\n[分割] TRAIN {len(tr):,}件 ({df['date'].min().date()}〜{_cut.date()}) "
      f"{tr['pnl'].sum():+,.0f}円  |  "
      f"TEST {len(te):,}件 ({_cut.date()}〜{df['date'].max().date()}) {te['pnl'].sum():+,.0f}円")
if len(tr) < 100 or len(te) < 100:
    print("[!] どちらかが100件未満です。--split を調整するか期間を延ばしてください")

# ── 属性ごとの層別 ────────────────────────────────────────────────────
# 「前夜」= 発注時点で確定。そのまま発注ルールにできる。
# 「寄り」= 当日09:00の値が要る。寄り後に取消す運用(cancel_gap_orders)なら使える。
_TIMING = {
    "株価": "前夜", "ATR%": "前夜", "前日リターン%": "前夜", "5日リターン%": "前夜",
    "20日リターン%": "前夜", "出来高比": "前夜", "売買代金_億": "前夜",
    "20日レンジ位置": "前夜", "連続上昇日数": "前夜", "曜日": "前夜",
    "日経_前日リターン%": "前夜",
    "寄りギャップ%": "寄り", "日経_寄りギャップ%": "寄り",
}
_COLS = [c for c in _TIMING if c in df.columns]

_DOW = {0: "月", 1: "火", 2: "水", 3: "木", 4: "金", 5: "土", 6: "日"}


def _stat(s: pd.DataFrame) -> tuple:
    n = len(s)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    w = int((s["pnl"] > 0).sum())
    p = float(s["pnl"].sum())
    return n, w / n * 100, p, p / n


def _welch_t(a: np.ndarray, b: np.ndarray) -> float:
    """帯(a) と それ以外(b) の1件あたり損益の差の t値(Welch)。
    『帯の平均がプラス』ではなく『他と違う』を測る。全体がプラスなら
    どの帯も平均プラスになるので、そのままでは選別の根拠にならない。"""
    if len(a) < 5 or len(b) < 5:
        return 0.0
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = np.sqrt(va / len(a) + vb / len(b))
    return float((a.mean() - b.mean()) / se) if se > 0 else 0.0


_T_MIN = 2.0            # TEST でこの t値を超えないと候補にしない
_NULL_REPS = 300        # 帰無分布の較正回数

_cands: list[dict] = []
_bin_masks: list[tuple] = []   # (col, lbl, mask_tr, mask_te) — 帰無較正で使い回す
for col in _COLS:
    if col == "曜日":
        edges = None
        keys = sorted(set(tr[col].dropna().astype(int)) | set(te[col].dropna().astype(int)))
        bands = [(k, _DOW.get(k, str(k))) for k in keys]
    else:
        v = tr[col].replace([np.inf, -np.inf], np.nan).dropna()
        if len(v) < args.bins * 10:
            continue
        # TRAIN の分位点で境界を決め、その境界を TEST にもそのまま当てる
        # (TEST 側で切り直すと『TESTを見て決めた』ことになり検証にならない)
        edges = list(np.unique(np.quantile(v, np.linspace(0, 1, args.bins + 1))))
        if len(edges) < 3:
            continue
        edges[0], edges[-1] = -np.inf, np.inf
        bands = [(i, f"{edges[i]:,.2f}〜{edges[i + 1]:,.2f}") for i in range(len(edges) - 1)]

    print("\n" + "=" * 104)
    print(f"■ {col}   [{_TIMING.get(col, '?')}に確定]")
    print(f"  {'帯':<22}{'TRAIN件':>8}{'1件':>9}{'t':>6}"
          f"{'TEST件':>8}{'勝率':>7}{'合計':>12}{'1件':>9}{'t':>6}  判定")
    print("  " + "-" * 100)
    for bi, lbl in bands:
        if col == "曜日":
            m_tr, m_te = tr[col] == bi, te[col] == bi
        else:
            m_tr = (tr[col] >= edges[bi]) & (tr[col] < edges[bi + 1])
            m_te = (te[col] >= edges[bi]) & (te[col] < edges[bi + 1])
        n1, w1, p1, e1 = _stat(tr[m_tr])
        n2, w2, p2, e2 = _stat(te[m_te])
        if n1 == 0 and n2 == 0:
            continue
        _a1 = tr.loc[m_tr, "pnl"].to_numpy(); _b1 = tr.loc[~m_tr, "pnl"].to_numpy()
        _a2 = te.loc[m_te, "pnl"].to_numpy(); _b2 = te.loc[~m_te, "pnl"].to_numpy()
        t1, t2 = _welch_t(_a1, _b1), _welch_t(_a2, _b2)
        _bin_masks.append((col, lbl, m_tr.to_numpy(), m_te.to_numpy()))
        # 判定: 「他の帯と違う」が TRAIN/TEST で同じ向きに出ていて、
        #        かつ TEST 側で t>=2 (偶然では説明しにくい水準) のときだけ候補。
        #        平均がプラスかどうかでは判定しない(全体がプラスなら全部プラスになる)。
        if n1 < args.min_n or n2 < args.min_n:
            mark = "サンプル少"
        elif t1 * t2 <= 0:
            mark = "— 向き不一致"
        elif abs(t2) < _T_MIN:
            mark = f"— TEST弱い(t={t2:+.1f})"
        else:
            mark = "◎ 他より良い" if t2 > 0 else "× 他より悪い"
        print(f"  {lbl:<22}{n1:>8}{e1:>+9,.0f}{t1:>+6.1f}"
              f"{n2:>8}{w2:>6.1f}%{p2:>+12,.0f}{e2:>+9,.0f}{t2:>+6.1f}  {mark}")
        if mark.startswith(("◎", "×")):
            _cands.append({"属性": col, "帯": lbl, "timing": _TIMING.get(col, "?"),
                           "向き": "採用" if t2 > 0 else "除外",
                           "TRAIN_n": n1, "TRAIN_1件": e1, "TRAIN_t": t1,
                           "TEST_n": n2, "TEST_1件": e2, "TEST_t": t2, "TEST_合計": p2})

# ── 帰無分布の較正 (多重検定の補正) ──────────────────────────────────
# 属性13本 × 5帯 = 約65回の検定をしている。何もエッジが無くても、偶然いくつかは
# 「TRAIN/TEST で向きが揃い、TEST の t>=2」を満たす。その『偶然の個数』を、
# 損益ラベルをシャッフルして実測する。候補数がこれを上回らなければ、
# 見つけたものは全部ノイズ。
#   ⛔ 2026-08-07: この較正を入れる前は、完全な乱数データでも「◎両方プラス」が
#      20個以上出た。BTスコアで踏んだのと同じ罠なので、必ずこれを見ること。
_null_counts: list[int] = []
if _bin_masks:
    _rng = np.random.default_rng(12345)
    _p_tr = tr["pnl"].to_numpy()
    _p_te = te["pnl"].to_numpy()
    print(f"\n[帰無較正] 損益をシャッフルして {_NULL_REPS}回 判定し直します"
          f"(検定数 {len(_bin_masks)})...", flush=True)
    for _ in range(_NULL_REPS):
        s_tr = _rng.permutation(_p_tr)
        s_te = _rng.permutation(_p_te)
        c = 0
        for _col, _lbl, m1, m2 in _bin_masks:
            if m1.sum() < args.min_n or m2.sum() < args.min_n:
                continue
            t1 = _welch_t(s_tr[m1], s_tr[~m1])
            t2 = _welch_t(s_te[m2], s_te[~m2])
            if t1 * t2 > 0 and abs(t2) >= _T_MIN:
                c += 1
        _null_counts.append(c)

# ── 候補のまとめ ──────────────────────────────────────────────────────
print("\n" + "=" * 104)
print("【候補】TRAIN と TEST で向きが一致し、TEST で他の帯と有意に違う帯だけ")
print("=" * 104)
_null_mean = float(np.mean(_null_counts)) if _null_counts else 0.0
_null_p95 = float(np.percentile(_null_counts, 95)) if _null_counts else 0.0
if _null_counts:
    print(f"  偶然でも出る個数(損益シャッフル {_NULL_REPS}回): 平均 {_null_mean:.1f}個 / "
          f"95パーセンタイル {_null_p95:.0f}個")
    print(f"  実際に出た個数: {len(_cands)}個")
    if len(_cands) <= _null_p95:
        print(f"  → **偶然の範囲内**。下の表は本物のエッジではない可能性が高い。")
    else:
        print(f"  → 偶然の水準を超えている。ただし『どれが本物か』は個別には決まらない。")
    print()

if not _cands:
    print("  該当なし。")
    print("  → 発注時点で分かる属性では選別できない、という結論。")
    print("     BTスコアと同じく、この軸で絞っても期待値は上がらない。")
    print("     『全部取って予算で制限する』のが正しい可能性が高い。")
else:
    _cands.sort(key=lambda x: -abs(x["TEST_t"]))
    print(f"  {'属性':<18}{'帯':<20}{'確定':<5}{'向き':<5}"
          f"{'TRAIN t':>9}{'TEST t':>8}{'TEST 1件':>11}{'TEST件':>7}{'TEST合計':>12}")
    print("  " + "-" * 100)
    for c in _cands:
        print(f"  {c['属性']:<18}{c['帯']:<20}{c['timing']:<5}{c['向き']:<5}"
              f"{c['TRAIN_t']:>+9.1f}{c['TEST_t']:>+8.1f}{c['TEST_1件']:>+11,.0f}"
              f"{c['TEST_n']:>7}{c['TEST_合計']:>+12,.0f}")
    print()
    print("  ⚠ ここに出ても確定ではありません。採用する前に必ず:")
    print("    1. --split を変えて(別の切り方でも)同じ帯が残るか")
    print("    2. sim_portfolio_lss.py(予算・上限キャンセル込み)で検証する")
    print("       単体の期待値がプラスでも、予算が有限だと機会費用でマイナスになる"
          "(CLAUDE.md 18.10)")

if args.out:
    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"\n[出力] 属性付き全トレード → {args.out}")
