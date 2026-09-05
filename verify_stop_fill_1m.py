r"""verify_stop_fill_1m.py — 損切り約定を『1分足』で再現し、5分足モデルの甘さを実測する。

なぜ必要か (2026-08-08)
------------------------
実約定3営業日25件の突合で、決済滑りが **-1,662円/件**(うち6件で99%)だった。
一方 v16 の5分足モデル(`fill = max(stop, そのバーの始値)`)が拾ったコストは
sim 上で **-362円/件**。差が4.6倍あるが、**25件では確定できない**
(6件の外れ値で決まっている / 3日がたまたま悪かった可能性)。

**1分足があれば、同じことを数百件で測れる。**

何が起きているか(ライブの手順)
------------------------------
    09:00  空売り約定 (約定バー = 09:00-09:05)
      ↓    delay1 = このバーの間は損切りを置かない(無保護窓)
    09:05  次の5分グリッドで損切りを武装
             ・すでに損切りを超えている → **即時 成行**で買い戻し
             ・まだ超えていない        → 板に逆指値を置く
    → 前者のとき、約定値は『09:05時点の走った値』。損切りラインではない。

5分足モデルはこれを『09:05バーの始値』で近似する。**近似の精度が本ツールの主題。**
1分足なら (a)無保護窓の中で価格がどこまで走ったか (b)武装した分の値
(c)成行が滑る余地 を分けて測れる。

出すもの
--------
1. 1分足で再現した損切り約定値 vs CSVの5分足モデル値 の差(1件あたり・分布・裾)
2. 無保護窓のMAE(最大逆行) — 5分足では見えない「窓の中でどこまで走ったか」
3. 武装時に『すでに超えていた』割合(=即時成行になる率)
4. 成行の代わりに **指値**(stop×(1+g))を置いた場合の比較 — 執行改善の効果見積り

使い方
------
  python verify_stop_fill_1m.py --workers 8
  python verify_stop_fill_1m.py --stop-delay-bars 1 --limit 200      # デバッグ
  python verify_stop_fill_1m.py --limit-guards 0.3,0.5,1.0           # 指値ガード%スイープ
  python verify_stop_fill_1m.py --csv out.csv                        # 明細を保存

前提:
  * `fetch_1m_all.py` で1分足を取得済 (~/.jquants_cache/minute/<code>_1m.pkl)
  * `.\daily` で lss_trades.csv を出力済 (stop_price / entry_time / reason 列が要る)
  * 1分足のある期間だけが対象。無い取引は自動でスキップし件数を明示する。
"""
from __future__ import annotations

import argparse
import csv as _csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as _time, timedelta

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(description="損切り約定を1分足で再現して5分足モデルを検証")
ap.add_argument("--trades-csv", type=str, default="lss_trades.csv")
ap.add_argument("--stop-delay-bars", type=int, default=1,
                help="無保護窓の5分足本数(live watch.bat = 1)")
ap.add_argument("--limit-guards", type=str, default="0.3,0.5,1.0",
                help="成行の代わりに指値を置いた場合の比較(stop からの%%)")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ(デバッグ)")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--csv", type=str, default="", help="明細をCSV保存")
args = ap.parse_args()

GUARDS = [float(x) / 100.0 for x in args.limit_guards.split(",") if x.strip()]

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
        if "損切" not in str(r.get("reason", "")):
            continue                       # 損切りトレードだけが対象
        try:
            sp = float(r.get("stop_price") or 0)
            xp = float(r.get("exit_p") or 0)
            ep = float(r.get("entry_p") or 0)
        except Exception:
            continue
        et = str(r.get("entry_time") or "").strip()
        ed = str(r.get("entry_date") or "").strip()[:10]
        if not (sp > 0 and xp > 0 and ep > 0 and et and ed):
            continue
        rows.append({"symbol": str(r.get("symbol") or "").strip(),
                     "strategy": str(r.get("strategy") or "").strip(),
                     "date": ed, "entry_time": et,
                     "entry_p": ep, "exit_p_5m": xp, "stop": sp})
except FileNotFoundError:
    sys.exit(f"[error] {args.trades_csv} がありません。先に .\\daily を実行してください")

if args.limit > 0:
    rows = rows[:args.limit]
if not rows:
    sys.exit("[error] 損切りトレードが見つかりません(reason に『損切』を含む行)")
print(f"[入力] 損切りトレード {len(rows):,}件 / {args.trades_csv}")


def _arm_time(entry_time: str, k: int):
    """(約定バー開始時刻, 損切りを武装する時刻) を datetime.time で返す。

    live(lss_exit_watcher --stop-delay-bars K)と同じ: 約定を検知した5分足から
    K本ぶんは置かず、その次の5分グリッドから有効化する。

    ※ 1分足キャッシュは tz付き DatetimeIndex(Asia/Tokyo)なので、日付つきの
      Timestamp で比較すると tz 不一致で落ちる。時刻(time)だけで比較する。
    """
    try:
        h, m = (int(x) for x in entry_time.split(":")[:2])
    except Exception:
        return None, None
    base = datetime(2000, 1, 1, h, m) - timedelta(minutes=m % 5)   # 5分グリッドへ丸め
    return base.time(), (base + timedelta(minutes=5 * max(0, k))).time()


def _one(rec: dict) -> dict | None:
    df = _bars_one(rec["symbol"], 1)
    if df is None or getattr(df, "empty", True):
        return None
    try:
        _d = datetime.strptime(rec["date"], "%Y-%m-%d").date()
        day = df[df.index.date == _d]        # tz付きindexでも .date は素直に効く
    except Exception:
        return None
    if len(day) < 10:
        return None
    ent_t, arm_t = _arm_time(rec["entry_time"], args.stop_delay_bars)
    if arm_t is None:
        return None

    stop = rec["stop"]
    o = day["open"].to_numpy(dtype=float)
    h = day["high"].to_numpy(dtype=float)
    c = day["close"].to_numpy(dtype=float)
    tt = np.array([ts.time() for ts in day.index])      # 時刻だけで比較(tz非依存)

    # ── 無保護窓 (約定〜武装) の最大逆行 ─────────────────────────
    # ショートなので『高値がどこまで上がったか』が逆行。5分足では見えない情報。
    win = (tt >= ent_t) & (tt < arm_t)
    mae = float(h[win].max()) if win.any() else float("nan")

    # ── 武装以降 ────────────────────────────────────────────────
    post = tt >= arm_t
    if not post.any():
        return None
    i0 = int(np.argmax(post))                       # 武装した最初の1分バー

    if o[i0] >= stop:
        # すでに損切りを超えている → 即時 成行。約定はその分の始値近辺。
        kind = "即時成行"
        fill_1m = o[i0]
        fill_1m_worst = max(o[i0], h[i0])           # その1分の中で最悪まで滑った場合
    else:
        # 板に逆指値。以降で高値が stop に触れたバーで発火。
        hit = np.where(post & (h >= stop))[0]
        if len(hit) == 0:
            return None                             # 武装後に触れず(=CSVと不整合)。除外
        j = int(hit[0])
        kind = "板逆指値"
        fill_1m = max(stop, o[j])                   # 窓開けなら始値
        fill_1m_worst = fill_1m

    out = {**rec, "arm": arm_t.strftime("%H:%M"), "kind": kind,
           "mae_window": mae,
           "fill_1m": fill_1m, "fill_1m_worst": fill_1m_worst,
           # ショートなので (エントリー - 決済) × 株数
           "pnl_5m": (rec["entry_p"] - rec["exit_p_5m"]) * args.qty,
           "pnl_1m": (rec["entry_p"] - fill_1m) * args.qty,
           "pnl_1m_worst": (rec["entry_p"] - fill_1m_worst) * args.qty}

    # ── 成行の代わりに指値を置いた場合 ──────────────────────────
    # 「stop×(1+g) の指値で買い戻す。届かなければ引けまで持つ」= 執行改善案の見積り。
    for g in GUARDS:
        lim = stop * (1.0 + g)
        if kind == "即時成行" and o[i0] > lim:
            # 既に指値より上 → その日のうちに指値まで戻れば約定、戻らなければ引け
            back = np.where(post & (day["low"].to_numpy(dtype=float) <= lim))[0]
            fill_g = lim if len(back) else float(c[-1])
        else:
            fill_g = min(fill_1m, lim) if fill_1m > lim else fill_1m
        out[f"pnl_lim{int(g * 1000)}"] = (rec["entry_p"] - fill_g) * args.qty
    return out


res = []
with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
    futs = {ex.submit(_one, r): r for r in rows}
    done = 0
    for f in as_completed(futs):
        done += 1
        if done % 500 == 0:
            print(f"  ...{done}/{len(rows)}件", flush=True)
        v = f.result()
        if v:
            res.append(v)

if not res:
    sys.exit("[error] 1分足で再現できた損切りが0件。fetch_1m_all.py の対象期間を確認してください")

d = pd.DataFrame(res)
d["date"] = pd.to_datetime(d["date"])
print(f"[再現] {len(d):,}件 / {len(rows):,}件中 "
      f"({len(d) / len(rows) * 100:.0f}% / 1分足の無い期間・銘柄はスキップ)")
print(f"       {d['date'].min().date()} 〜 {d['date'].max().date()}")
if args.csv:
    d.to_csv(args.csv, index=False, encoding="utf-8-sig")
    print(f"[保存] {args.csv}")

# ── ① 5分足モデル vs 1分足再現 ──────────────────────────────────
print(f"\n{'=' * 92}")
print("■ ① 5分足モデル(v16)は甘いのか — 損切り約定値の比較")
print(f"{'=' * 92}")
diff = d["pnl_1m"] - d["pnl_5m"]
diff_w = d["pnl_1m_worst"] - d["pnl_5m"]
print(f"  {'':<26}{'合計':>16}{'1件あたり':>13}")
print("  " + "-" * 56)
print(f"  {'5分足モデル(現行v16)':<26}{d['pnl_5m'].sum():>+16,.0f}{d['pnl_5m'].mean():>+13,.0f}")
print(f"  {'1分足で再現':<26}{d['pnl_1m'].sum():>+16,.0f}{d['pnl_1m'].mean():>+13,.0f}")
print(f"  {'1分足(その分の最悪値)':<26}{d['pnl_1m_worst'].sum():>+16,.0f}{d['pnl_1m_worst'].mean():>+13,.0f}")
print("  " + "-" * 56)
print(f"  {'差 (1分 − 5分)':<26}{diff.sum():>+16,.0f}{diff.mean():>+13,.0f}")
print(f"  {'差 (最悪 − 5分)':<26}{diff_w.sum():>+16,.0f}{diff_w.mean():>+13,.0f}")
print()
print(f"  実測(実約定3営業日25件)の決済滑り = **-1,662円/件**")
_m = float(diff.mean())
_REAL = -1662.0          # 実測(実約定3営業日25件)の決済滑り 1件あたり
if _m > -100:
    print(f"  → 1分足で再現しても差は {_m:+,.0f}円/件。**5分足モデルは十分正確。**")
    print(f"     実測の {_REAL:+,.0f}円/件 は 25件の外れ値(6件で99%)による可能性が高い。")
elif _m > _REAL * 0.5:
    print(f"  → 5分足モデルは {_m:+,.0f}円/件 甘い。実測({_REAL:+,.0f})ほどではないが無視できない。")
elif _m > _REAL * 1.5:
    print(f"  → 5分足モデルは {_m:+,.0f}円/件 甘く、**実測({_REAL:+,.0f}円/件)と同水準**。")
    print(f"     3営業日25件が外れ値だったのではなく、これが常態ということ。")
    print(f"     **バックテストの金額を下方修正する必要がある。**")
else:
    print(f"  → 5分足モデルは {_m:+,.0f}円/件 甘く、**実測({_REAL:+,.0f}円/件)より更に悪い**。")
    print(f"     母数が少ないか、CSVの exit_p が楽観側(v16未適用)の可能性がある。")
    print(f"     lss_trades.csv が v16 適用後に出力されたものか確認すること。")

# エッジとの比較 = 採否の本体
_EDGE = 391.0            # 流動性順・7ヶ月の1件あたり (18.21)
print(f"\n  ※ 損切りは全取引の一部。全取引ベースの影響は "
      f"{_m:+,.0f}円/件 × 損切り比率 で効く。")
print(f"     エッジは +{_EDGE:,.0f}円/件(全取引)なので、"
      f"損切り比率を r とすると {_m:+,.0f}×r が {-_EDGE:,.0f} を下回ると赤字。")

# ── ② 無保護窓で何が起きているか ────────────────────────────────
print(f"\n{'=' * 92}")
print("■ ② 無保護窓(約定〜武装)の中身 — 5分足では見えない部分")
print(f"{'=' * 92}")
n_imm = int((d["kind"] == "即時成行").sum())
print(f"  武装時にすでに損切りを超えていた(=即時成行) : {n_imm:,}件 "
      f"({n_imm / len(d) * 100:.1f}%)")
print(f"  板に逆指値を置けた                          : {len(d) - n_imm:,}件 "
      f"({(len(d) - n_imm) / len(d) * 100:.1f}%)")
_ov = (d["mae_window"] - d["stop"]) / d["stop"] * 100
_ov = _ov[_ov == _ov]
if len(_ov):
    print(f"\n  無保護窓の最大逆行が損切りを超えた幅(%):")
    for q in (50, 75, 90, 95, 99):
        print(f"    {q}パーセンタイル: {np.percentile(_ov, q):+6.2f}%")
    print(f"    最大: {_ov.max():+.2f}%")
print(f"\n  ※ ここが『5分足では見えない』部分。窓の中で走った幅が大きいほど、")
print(f"     武装した瞬間の成行が不利になる。")

# ── ③ 裾 ────────────────────────────────────────────────────────
print(f"\n{'=' * 92}")
print("■ ③ 損の集中 (実測では25件中6件で99%だった。母数を増やすと?)")
print(f"{'=' * 92}")
w = diff.sort_values()
for k in (5, 10, 20):
    n = max(1, int(len(w) * k / 100))
    print(f"  下位{k:>2}% ({n:,}件) が差分合計の {w.head(n).sum() / diff.sum() * 100:5.1f}% を占める"
          if diff.sum() != 0 else "")
print(f"  最悪の5件: {', '.join(f'{v:+,.0f}円' for v in w.head(5))}")

# ── ④ 成行 → 指値 にしたら ──────────────────────────────────────
print(f"\n{'=' * 92}")
print("■ ④ 執行改善: 武装時の成行を『指値』に変えたら (届かなければ引けまで持つ)")
print(f"{'=' * 92}")
print(f"  {'ルール':<26}{'合計':>16}{'1件あたり':>13}{'1分足比':>13}")
print("  " + "-" * 69)
print(f"  {'成行(現行)':<26}{d['pnl_1m'].sum():>+16,.0f}{d['pnl_1m'].mean():>+13,.0f}{'—':>13}")
for g in GUARDS:
    col = f"pnl_lim{int(g * 1000)}"
    print(f"  {f'指値 stop+{g:.1%}':<26}{d[col].sum():>+16,.0f}"
          f"{d[col].mean():>+13,.0f}{d[col].mean() - d['pnl_1m'].mean():>+13,.0f}")
print("\n  ※ 指値は『届かなければ引けまで持つ』= 損が伸びる可能性も含めた計算。")
print("     プラスなら執行改善の余地あり。マイナスなら成行のままが正しい。")

print(f"\n{'─' * 92}")
print("⚠ 1分足のある期間だけの結果。母数と期間を必ず確認すること。")
print("  実約定(.\\fills の slip_daily_log.csv)とは母集団が違う(あちらは実発注のみ)。")
print("  両方が同じ向きを指したときに初めて結論とすること。")
