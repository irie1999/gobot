"""measure_entry_decay.py — 09:00 に何秒で発注できれば、いくら取れるか

⛔ 照会のみ。発注しない。1分足キャッシュを読むだけ。

■ 何を答えるツールか (2026-08-16 ユーザー質問「9:00に注文を出せればいいのですよね?」)

K(09:00確認方式) は「始値を見てギャップ判定 → その場で成行売り」。
ところがバックテストは 5分足しか持っていないので、約定を
**最初の5分足の終値(09:05)** で近似している。実際は 08:5x に板を暖めて
おけば 09:00:10 頃には判定が終わるので、**5分ではなく十数秒**のはず。

その差は小さくない。同じ銘柄・同じ株数・同じ決済で、売る値段だけが違う:

    旧K(寄り値で売れたとしたら)  月平均 +562,258   ← 上限
    現K(09:05 で売る)            月平均 +271,391   ← 下限

**この 29万円/月 が『5分』の値段。** 本当の値はこの間のどこかで、
それを決めるのは執行速度だけ。1分足があれば 09:01 / 09:02 / 09:03 まで
刻めるので、**帯を [寄り, 09:05] から [寄り, 09:01] に狭められる。**

■ 使い方

    .\\hvar                      # lss_trades_K.csv が出る(推奨Kの明細)
    python measure_entry_decay.py

    python measure_entry_decay.py --trades-csv lss_trades_K.csv
    python measure_entry_decay.py --minutes 1,2,3,5 --workers 8
    python measure_entry_decay.py --csv decay_detail.csv

■ ⚠ これは **価格の測定**であって損益の再シミュではない

ショートの損益は決済理由で E(建値) への依存が違う:

    利確  pnl = (E - (E - tm*ATR)) * q =  tm*ATR*q   ← E に依存しない
    損切  pnl = (E - (E + sm*ATR)) * q = -sm*ATR*q   ← E に依存しない
    引け  pnl = (E - C) * q                          ← E に**比例**

つまり建値が上がって直接効くのは **引け決済のトレードだけ**。
利確/損切りのトレードでは損切り・利確のライン自体も一緒に上へずれるので、
効くのは金額ではなく **どちらに当たるかの確率**(高く売るほど損切りが遠い)。

→ 本ツールは
   ① 全件の価格減衰(bp) …「5分でいくら逃げるか」の実測
   ② 引け決済ぶんの損益差 …**確実に取れる下限**
   ③ 参考: 全件を線形換算した額 …**上限**(確率の改善を無視した粗い上界)
   を出す。②〜③の間が本当の値。②だけでも判断に足りることが多い。

厳密にやるなら 5分足の決済判定を新しい建値で回し直す必要があるが、
まず①で「そもそも速さに価値があるのか」を見てからでよい。
"""
from __future__ import annotations

import argparse
import csv as _csv
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

ap = argparse.ArgumentParser(
    description="09:00 の発注速度がいくらの価値になるかを1分足で測る(照会のみ)")
ap.add_argument("--trades-csv", type=str, default="lss_trades_K.csv",
                help="推奨K の明細CSV(.\\hvar が出力)")
ap.add_argument("--minutes", type=str, default="1,2,3,4,5",
                help="測る経過分。1 = 09:00-09:01 の足の終値で売った場合")
ap.add_argument("--qty-col", type=str, default="qty")
ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ(デバッグ)")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--csv", type=str, default="", help="明細をCSV保存")
args = ap.parse_args()

MINS = [int(x) for x in str(args.minutes).split(",")
        if str(x).strip().isdigit() and int(x) > 0]
if not MINS:
    sys.exit("[error] --minutes が空です")

try:
    from tenkan_sim import _bars_one, find_minute_dirs
except Exception as e:
    sys.exit(f"[error] tenkan_sim を読めません: {e}")

_d5, _d1 = find_minute_dirs()
if _d1 is None or not _d1.exists():
    sys.exit("[error] 1分足ディレクトリが見つかりません。fetch_1m_all.py を実行するか "
             "MINUTE_1M_DIR を設定してください")
print(f"[1分足] {_d1}")

# ── 入力 ────────────────────────────────────────────────────────────
rows = []
try:
    for r in _csv.DictReader(open(args.trades_csv, encoding="utf-8-sig")):
        if str(r.get("reason", "")).strip() in ("約定せず", ""):
            continue
        try:
            ep = float(r.get("entry_p") or 0)
            xp = float(r.get("exit_p") or 0)
            q = int(float(r.get(args.qty_col) or 0))
        except Exception:
            continue
        ed = str(r.get("entry_date") or "").strip()[:10]
        if not (ep > 0 and xp > 0 and q > 0 and ed):
            continue
        rows.append({"symbol": str(r.get("symbol") or "").strip(),
                     "strategy": str(r.get("strategy") or "").strip(),
                     "date": ed, "entry_p": ep, "exit_p": xp, "qty": q,
                     "reason": str(r.get("reason") or "").strip()})
except FileNotFoundError:
    sys.exit(f"[error] {args.trades_csv} がありません。先に .\\hvar を実行してください"
             "(推奨Kの明細を出力します)")

if args.limit > 0:
    rows = rows[:args.limit]
if not rows:
    sys.exit(f"[error] {args.trades_csv} に約定トレードがありません")

_months = sorted({r["date"][:7] for r in rows})
print(f"[入力] {len(rows):,}件 / {args.trades_csv} / "
      f"{_months[0]}〜{_months[-1]} ({len(_months)}ヶ月)")


def _one(rec: dict):
    """その日の 1分足から 09:00 の始値と経過分ごとの終値を拾う。"""
    df = _bars_one(rec["symbol"], 1)
    if df is None or getattr(df, "empty", True):
        return None
    try:
        day = df[df.index.strftime("%Y-%m-%d") == rec["date"]]
    except Exception:
        return None
    if day is None or len(day) == 0:
        return None
    # 09:00 台から始まる並びだけを対象にする。寄りが遅れた銘柄(特別気配)は
    # 『09:00 に発注できない』ケースそのものなので、件数として別に数える。
    _first = day.index[0]
    _hm = f"{_first.hour:02d}:{_first.minute:02d}"
    out = {"symbol": rec["symbol"], "strategy": rec["strategy"],
           "date": rec["date"], "qty": rec["qty"], "reason": rec["reason"],
           "exit_p": rec["exit_p"], "entry_5m": rec["entry_p"],
           "first_hm": _hm, "late_open": (_hm != "09:00")}
    try:
        out["open"] = float(day["open"].iloc[0])
    except Exception:
        return None
    if not (out["open"] > 0):
        return None
    for m in MINS:
        # m 分後 = インデックス m-1 の足の終値(09:00足の終値 = 09:01)
        out[f"m{m}"] = (float(day["close"].iloc[m - 1])
                        if len(day) >= m else None)
    return out


print(f"[実行] 1分足を読み込み中 (workers={args.workers}) ...", flush=True)
with ThreadPoolExecutor(max_workers=args.workers) as ex:
    res = [x for x in ex.map(_one, rows) if x]

if not res:
    sys.exit("[error] 1分足と突合できた行がありません。銘柄コードの形式や "
             "fetch_1m_all.py の在庫を確認してください(check_minute_data.py)")

_late = sum(1 for r in res if r["late_open"])
print(f"[突合] {len(res):,}/{len(rows):,}件 "
      f"({len(res) / len(rows) * 100:.0f}%)"
      + (f" / うち 09:00 に寄っていない {_late:,}件 "
         f"({_late / len(res) * 100:.1f}%)" if _late else ""))

_n_mo = max(1, len({r["date"][:7] for r in res}))

# ── ① 価格の減衰 ────────────────────────────────────────────────────
print("\n" + "=" * 74)
print("① 価格の減衰 — 寄り値からどれだけ逃げるか (ショートなので下がると不利)")
print("=" * 74)
print(f"{'経過':>6} {'件数':>7} {'中央bp':>9} {'平均bp':>9} "
      f"{'寄りより不利':>12} {'寄り比 円/件':>13}")


def _q(xs, p):
    if not xs:
        return 0.0
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(len(ys) * p))]


_decay = {}
for m in MINS:
    bps, yens = [], []
    for r in res:
        v = r.get(f"m{m}")
        if v is None or not (v > 0):
            continue
        # ショート: 高く売れるほど良い。寄り値との差(マイナス=不利)
        bps.append((v - r["open"]) / r["open"] * 1e4)
        yens.append((v - r["open"]) * r["qty"])
    if not bps:
        continue
    _decay[m] = (bps, yens)
    _worse = sum(1 for b in bps if b < 0) / len(bps) * 100
    print(f"{m:>4}分 {len(bps):>7,} {_q(bps, 0.5):>9.1f} "
          f"{sum(bps) / len(bps):>9.1f} {_worse:>11.0f}% "
          f"{sum(yens) / len(yens):>13,.0f}")

print("\n  ⚠ 『寄り比 円/件』は **建値の差そのもの**。損益への効き方は決済理由で"
      "違うので、②③で分けて出します。")

# ── ② 引け決済ぶん = 確実に取れる下限 ────────────────────────────────
print("\n" + "=" * 74)
print("② 引け決済のトレードだけ — ここは建値の差が **そのまま損益** になる")
print("=" * 74)
_close_kw = ("引け", "タイムカット", "close")
_cl = [r for r in res
       if any(k in r["reason"] for k in _close_kw)]
print(f"  対象 {len(_cl):,}件 / 全{len(res):,}件 "
      f"({len(_cl) / len(res) * 100:.0f}%)")
if _cl:
    _base = MINS[-1]
    print(f"\n  基準 = {_base}分後に売る(いまのバックテストの近似)\n")
    print(f"{'経過':>6} {'合計差':>14} {'月平均':>12} {'円/件':>10}")
    for m in MINS:
        tot = 0.0
        n = 0
        for r in _cl:
            a, b = r.get(f"m{m}"), r.get(f"m{_base}")
            if a is None or b is None:
                continue
            tot += (a - b) * r["qty"]      # 高く売れたぶんだけ有利
            n += 1
        if n:
            print(f"{m:>4}分 {tot:>+14,.0f} {tot / _n_mo:>+12,.0f} "
                  f"{tot / n:>+10,.0f}")

# ── ③ 全件を線形換算した上界 ─────────────────────────────────────────
print("\n" + "=" * 74)
print("③ 参考: 全件を線形換算した **上界** (利確/損切りの確率改善を無視)")
print("=" * 74)
_base = MINS[-1]
print(f"{'経過':>6} {'合計差':>14} {'月平均':>12} {'円/件':>10}")
for m in MINS:
    tot = 0.0
    n = 0
    for r in res:
        a, b = r.get(f"m{m}"), r.get(f"m{_base}")
        if a is None or b is None:
            continue
        tot += (a - b) * r["qty"]
        n += 1
    if n:
        print(f"{m:>4}分 {tot:>+14,.0f} {tot / _n_mo:>+12,.0f} "
              f"{tot / n:>+10,.0f}")
# 寄り値で売れた場合(=執行が瞬時)
_tot0 = 0.0
_n0 = 0
for r in res:
    b = r.get(f"m{_base}")
    if b is None:
        continue
    _tot0 += (r["open"] - b) * r["qty"]
    _n0 += 1
if _n0:
    print(f"{'寄り':>5} {_tot0:>+14,.0f} {_tot0 / _n_mo:>+12,.0f} "
          f"{_tot0 / _n0:>+10,.0f}   ← 執行が瞬時なら")

# ── ④ 読み方 ────────────────────────────────────────────────────────
print("\n" + "=" * 74)
print("④ 読み方")
print("=" * 74)
print("""  ・②が **確実に取れるぶん**(引け決済は建値の差がそのまま損益)。
  ・③は上界。利確/損切りのトレードでは建値と一緒にラインも動くので、
    金額はそのままは効かない(効くのは『どちらに当たるか』の確率)。
  ・**1分後と5分後の差が小さいなら、速さに価値は無い**。
    その場合 09:05 近似のままで K を評価してよく、実装も楽になる。
  ・逆に1分後がはっきり良いなら、**08:5x に板を暖めて 09:00 直後に
    発注する**価値がある(登録直後の初回 /board は48秒かかるので、
    暖めていないと間に合わない = check_board_limits.py の実測)。
  ・『09:00 に寄っていない』件数も見ること。そこは速さ以前に
    始値が出ていないので、待つか見送るかのルールが要る。""")

if args.csv:
    _cols = (["symbol", "strategy", "date", "qty", "reason", "first_hm",
              "late_open", "open", "entry_5m", "exit_p"]
             + [f"m{m}" for m in MINS])
    with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=_cols, extrasaction="ignore")
        w.writeheader()
        for r in res:
            w.writerow(r)
    print(f"\n[CSV] {args.csv} に {len(res):,}件を出力")
