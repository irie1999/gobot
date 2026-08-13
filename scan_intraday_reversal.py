r"""scan_intraday_reversal.py — 日中の短期リバーサルに **地力があるか** だけを測る。

何を確かめるためのものか
------------------------
H は1日1往復で +806円/件(建値の約29bp)。これを「1日に何往復も」に増やしたい。
だが **同じ +29.4bp を2回に分けて取ると必ず負ける**(ドリフトは1日ぶんしか無いのに
往復コストだけ回数ぶん増える / 呼値2〜4ティック = 7.2〜14.3bp)。

したがって回数が意味を持つのは、**往復ごとに新しいエッジがある場合だけ**。
このスクリプトが測るのはただ1つ:

    日中の短期リバーサル(数分で上げたら戻る)が、日足ドリフトとは独立に存在するか

⛔ ここでプラスが出なければ、そこで終わり。選定やパラメータで救おうとしないこと。
   §18.18 で LDT を、§18.26b で転換を、どちらも「地力がマイナスなものを選定で
   黒字に見せていただけ」と確定させている。

測り方 (§18.19 → §18.20 と同じ順序)
------------------------------------
  1. **対照群**(信号が出ていない銘柄)で同じルール      → **ゼロのはず**
  2. **信号群**(6信号が点灯した銘柄)で同じルール        → 1との差 = 寄与
  3. 2 の合計が H の +806円/件 を超えるか              → 超えなければ H のまま

**1がプラスに出たら測り方が間違っている**(約定判定が楽観になっている)。
これは検算として機能するので、必ず対照群も一緒に見ること。

固定しておくもの (動かすと必ず勝つ結果が出る)
----------------------------------------------
  * 約定判定は既定 strict = **高値が指値を「超えた」ときだけ**約定。
    指値に触れただけで約定させると、1日N往復では誤差がN倍積み上がる(§18.8)。
  * 同一1分足内の決済は禁止。**1分足の中で高値と安値のどちらが先かは分からない**。
  * 摩擦はまず 0 で地力を見る。通ってから --cost-bp 7.2 / 14.3 を引く(§18.18 の
    `--aggregate` と同じ形)。
  * 1銘柄1ポジション。再エントリーは §18.8 で PF≈1.0 と出ている。

使い方
------
  python scan_intraday_reversal.py --trades lss_trades_H.csv --days 180
  python scan_intraday_reversal.py --window 5 --trigger 1.0 --target 0.5 --stop 0.5
  python scan_intraday_reversal.py --cost-bp 14.3          # 保守側の往復コストを引く
  python scan_intraday_reversal.py --split 2026-04-01      # TRAIN/TEST に割る
  python scan_intraday_reversal.py --control 0             # 対照群を作らない(速い)

前提: 1分足が ~/.jquants_cache/minute/<code>_1m.pkl にあること(fetch_1m_all.py)。
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))
_BASE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser(description="日中の短期リバーサルの地力を測る(選定なし)")
ap.add_argument("--trades", default="lss_trades_H.csv",
                help="信号群の出所。entry_date/symbol 列を読む")
ap.add_argument("--days", type=int, default=180, help="遡及日数")
ap.add_argument("--split", default=None, help="TRAIN/TEST の境界日 YYYY-MM-DD")
# --- ルール(1つだけ。増やさない) ---
ap.add_argument("--window", type=int, default=5, help="直近W分の上昇率を見る")
ap.add_argument("--trigger", type=float, default=1.0, help="W分で+X%%以上上げたら売る")
ap.add_argument("--target", type=float, default=0.5, help="利確 -Y%%")
ap.add_argument("--stop", type=float, default=0.5, help="損切り +Z%%")
ap.add_argument("--max-hold", type=int, default=30, help="T分で時間決済")
ap.add_argument("--start-time", default="09:05", help="この時刻以降だけ(寄りは H が使う)")
ap.add_argument("--end-time", default="14:50", help="この時刻までに手仕舞う")
ap.add_argument("--fill-mode", choices=["strict", "loose"], default="strict",
                help="strict=高値が指値を超えたときだけ約定(既定) / loose=触れたら約定")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--cost-bp", type=float, default=0.0,
                help="往復コスト(bp)。地力測定は0、現実確認は 7.2 / 14.3")
ap.add_argument("--control", type=int, default=30,
                help="対照群の1日あたり銘柄数(0=作らない)")
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄日だけ(デバッグ)")
ap.add_argument("--csv", default=None, help="明細を保存")
args = ap.parse_args()

try:
    from tenkan_sim import _bars_one, drop_symbol, find_minute_dirs
except Exception as e:                                            # pragma: no cover
    sys.exit(f"[error] tenkan_sim を import できません: {e}")

_d5, _d1 = find_minute_dirs()
if _d1 is None:
    sys.exit("[error] 1分足ディレクトリが見つかりません。fetch_1m_all.py を実行するか "
             "MINUTE_1M_DIR を設定してください "
             "(既定 ~/.jquants_cache/minute/<コード>_1m.pkl)")


def _hhmm(s: str):
    h, m = (int(x) for x in s.split(":")[:2])
    return h * 60 + m


_T0, _T1 = _hhmm(args.start_time), _hhmm(args.end_time)


# ──────────────────────────────────────────────────────────────────────
# 母集団
# ──────────────────────────────────────────────────────────────────────
def load_signal_days() -> dict:
    """{日付(str): set(銘柄)} — 6信号が点灯した銘柄。"""
    p = Path(args.trades)
    if not p.exists():
        sys.exit(f"[error] {p} がありません。.\\daily を1回流してください。")
    cut = (datetime.now(JST).date() - timedelta(days=args.days)).isoformat()
    out: dict = defaultdict(set)
    with open(p, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            d = str(r.get("entry_date") or r.get("date") or "")[:10]
            sym = str(r.get("symbol") or "").strip()
            if not d or not sym or d < cut:
                continue
            if str(r.get("strategy") or r.get("strat") or "").strip() == "転換":
                continue          # §18.26b: 転換は lss ではない。必ず除く
            out[d].add(sym)
    return dict(out)


def load_control(sig_days: dict) -> dict:
    """{日付: set(銘柄)} — 同じ日に1分足があり、信号が出ていない銘柄からランダム。"""
    if args.control <= 0:
        return {}
    try:
        from daytrade_data import available_local_symbols
        pool = [s if s.endswith(".T") else f"{s}.T"
                for s in available_local_symbols()]
    except Exception:
        pool = sorted({s for v in sig_days.values() for s in v})
    rng = random.Random(args.seed)
    out: dict = {}
    for d, sig in sig_days.items():
        cand = [s for s in pool if s not in sig]
        rng.shuffle(cand)
        out[d] = set(cand[:args.control])
    return out


# ──────────────────────────────────────────────────────────────────────
# 1銘柄日のシミュレーション
# ──────────────────────────────────────────────────────────────────────
def sim_one(symbol: str, day: str) -> list[dict]:
    """その日の1分足で「W分で+X%上げたら指値ショート → OCO or 時間決済」を回す。

    ⛔ 同一1分足内でエントリーと決済を同時に起こさない。1分足は OHLC しか無く、
       その1分の中で高値と安値のどちらが先かが分からないため。
    """
    df = _bars_one(symbol, 1)
    if df is None or getattr(df, "empty", True):
        return []
    try:
        _d = datetime.strptime(day, "%Y-%m-%d").date()
        bar = df[df.index.date == _d]
    except Exception:
        return []
    if len(bar) < args.window + 5:
        return []

    o = bar["open"].to_numpy(dtype=float)
    h = bar["high"].to_numpy(dtype=float)
    lo = bar["low"].to_numpy(dtype=float)
    c = bar["close"].to_numpy(dtype=float)
    mins = np.array([ts.hour * 60 + ts.minute for ts in bar.index])
    n = len(bar)

    trig = args.trigger / 100.0
    tgt = args.target / 100.0
    stp = args.stop / 100.0

    out: list[dict] = []
    i = args.window
    while i < n - 1:
        if mins[i] < _T0 or mins[i] > _T1:
            i += 1
            continue
        base = c[i - args.window]
        if base <= 0:
            i += 1
            continue
        # ── トリガー: 直近W分で +X% 以上 ────────────────────────────
        if c[i] / base - 1.0 < trig:
            i += 1
            continue

        # ── 指値ショートを「次の分から」置く。約定は指値を **超えた** ときだけ ──
        limit = c[i]
        ei = -1
        for j in range(i + 1, n):
            if mins[j] > _T1:
                break
            hit = (h[j] > limit) if args.fill_mode == "strict" else (h[j] >= limit)
            if hit:
                ei = j
                break
        if ei < 0:
            i += 1
            continue

        ep = max(limit, o[ei])          # 窓開けなら始値(ショートに有利側は取らない)
        sp = ep * (1.0 + stp)
        tp = ep * (1.0 - tgt)

        # ── 決済は **次の分足から**。同一足内の決済は禁止 ──────────
        xp, why, xi = c[min(ei + 1, n - 1)], "打切", min(ei + 1, n - 1)
        for j in range(ei + 1, n):
            if mins[j] > _T1 or (j - ei) > args.max_hold:
                xp, why, xi = o[j], "時間", j
                break
            if h[j] >= sp and lo[j] <= tp:
                xp, why, xi = sp, "損切", j      # 両方触れたら損切り優先(保守)
                break
            if h[j] >= sp:
                xp, why, xi = max(sp, o[j]), "損切", j
                break
            if lo[j] <= tp:
                xp, why, xi = min(tp, o[j]), "利確", j
                break
        else:
            xp, why, xi = c[n - 1], "引け", n - 1

        gross = (ep - xp) * args.qty
        cost = (ep + xp) / 2.0 * args.qty * (args.cost_bp / 10000.0)
        out.append({"date": day, "symbol": symbol,
                    "entry_min": int(mins[ei]), "exit_min": int(mins[xi]),
                    "entry_p": ep, "exit_p": xp, "why": why,
                    "hold": int(xi - ei), "pnl": gross - cost,
                    "bp": (ep - xp) / ep * 10000.0})
        i = xi + 1                       # 1銘柄1ポジション(§18.8: 再エントリーはPF≈1.0)
    return out


# ──────────────────────────────────────────────────────────────────────
# 集計 (同日相関があるので **日クラスタ** で t を出す / §18.13)
# ──────────────────────────────────────────────────────────────────────
def summarize(name: str, rows: list[dict]) -> dict:
    if not rows:
        print(f"  {name:<12} 取引0件")
        return {}
    pnl = np.array([r["pnl"] for r in rows], dtype=float)
    bp = np.array([r["bp"] for r in rows], dtype=float)
    byday: dict = defaultdict(float)
    for r in rows:
        byday[r["date"]] += r["pnl"]
    dv = np.array(list(byday.values()), dtype=float)
    nd = len(dv)
    t = (dv.mean() / (dv.std(ddof=1) / math.sqrt(nd))) if nd > 1 and dv.std(ddof=1) > 0 else 0.0
    win = float((pnl > 0).mean() * 100.0)
    gp = float(pnl[pnl > 0].sum())
    gl = float(-pnl[pnl < 0].sum())
    pf = (gp / gl) if gl > 0 else float("inf")
    print(f"  {name:<12}{len(rows):>7}件{win:>7.1f}%{pf:>8.2f}"
          f"{pnl.sum():>+14,.0f}{pnl.mean():>+9,.0f}円{bp.mean():>+8.1f}bp"
          f"{nd:>6}日{t:>+8.2f}")
    return {"n": len(rows), "pnl": pnl.sum(), "per": pnl.mean(),
            "bp": bp.mean(), "days": nd, "t": t}


def run(days_map: dict, label: str) -> list[dict]:
    rows: list[dict] = []
    pairs = [(d, s) for d, ss in sorted(days_map.items()) for s in sorted(ss)]
    if args.limit:
        pairs = pairs[:args.limit]
    bysym: dict = defaultdict(list)
    for d, s in pairs:
        bysym[s].append(d)
    done = 0
    for s, ds in bysym.items():
        for d in ds:
            rows.extend(sim_one(s, d))
        drop_symbol(s)                    # 分足は1銘柄数十MB。都度解放する
        done += 1
        if done % 100 == 0:
            print(f"    [{label}] {done}/{len(bysym)}銘柄 … {len(rows)}取引",
                  flush=True)
    return rows


def main() -> int:
    sig_days = load_signal_days()
    if not sig_days:
        sys.exit(f"[error] {args.trades} に対象期間の行がありません。")
    ctl_days = load_control(sig_days)

    print("■ 日中の短期リバーサル — 地力測定(選定なし)")
    print("=" * 92)
    print(f"  ルール : 直近{args.window}分で +{args.trigger}% 上げたら指値ショート")
    print(f"           → 利確 -{args.target}% / 損切り +{args.stop}% / "
          f"{args.max_hold}分で時間決済")
    print(f"  約定   : {args.fill_mode}"
          f"{'(高値が指値を超えたときだけ = 保守)' if args.fill_mode == 'strict' else '(触れたら約定 = 楽観)'}")
    print(f"  時間帯 : {args.start_time}〜{args.end_time} / 1銘柄1ポジション / "
          f"{args.qty}株 / 往復コスト {args.cost_bp}bp")
    print(f"  母集団 : 信号 {sum(len(v) for v in sig_days.values()):,}銘柄日 "
          f"({len(sig_days)}日) / 対照 {sum(len(v) for v in ctl_days.values()):,}銘柄日")
    print()

    sig_rows = run(sig_days, "信号")
    ctl_rows = run(ctl_days, "対照") if ctl_days else []

    print()
    print(f"  {'群':<12}{'取引':>9}{'勝率':>8}{'PF':>8}{'合計':>14}"
          f"{'円/件':>10}{'bp/件':>10}{'日数':>6}{'日t':>8}")
    print("-" * 92)
    s = summarize("信号あり", sig_rows)
    c = summarize("対照", ctl_rows) if ctl_rows else {}

    if args.split:
        print()
        print(f"  【TRAIN / TEST 分割  境界 {args.split}】")
        for nm, rr in (("信号あり", sig_rows), ("対照", ctl_rows)):
            if not rr:
                continue
            summarize(f"{nm}:TRAIN", [r for r in rr if r["date"] < args.split])
            summarize(f"{nm}:TEST", [r for r in rr if r["date"] >= args.split])

    print()
    print("  【判定】")
    if c and abs(c.get("per", 0)) > 0 and c.get("t", 0) > 2.0:
        print("  ⛔ **対照群がプラスで有意**。約定判定が楽観になっている疑いが濃い。")
        print("     --fill-mode strict になっているか、同一足内で決済していないかを確認。")
        print("     §18.19 で無条件の日中ドリフトはゼロと確定しているので、")
        print("     対照がプラスに出るのは測り方の問題である可能性が高い。")
    if not s:
        print("  取引が発生していません。--trigger を下げるか --window を変えてください。")
        return 1
    if s["per"] <= 0:
        print("  ⛔ **信号群の地力がマイナス**。ここで終わり。")
        print("     選定・パラメータで救おうとしないこと(§18.18 / §18.26b)。")
        return 0
    edge = s["per"] - (c.get("per", 0.0) if c else 0.0)
    print(f"  信号の寄与 = {edge:+,.0f}円/件 (信号 {s['per']:+,.0f} - 対照 "
          f"{c.get('per', 0.0):+,.0f})")
    per_day = s["pnl"] / max(1, s["days"])
    print(f"  1日あたり {per_day:+,.0f}円 / 1日 {s['n'] / max(1, s['days']):.1f}往復")
    print()
    print("  ⚠ ここがプラスでも、まだ地力(摩擦なし)。次にやること:")
    print("     1) --cost-bp 7.2 と 14.3 を引いて残るか(呼値2/4ティック)")
    print("     2) --split で TRAIN/TEST に割って両方プラスか")
    print("     3) H の +806円/件 と『1日あたり』で比較する")
    print("     4) それを通って初めてノイズ帯・walk-forward(§18.24 / §18.36)")

    if args.csv:
        allr = [{**r, "grp": "信号"} for r in sig_rows] + \
               [{**r, "grp": "対照"} for r in ctl_rows]
        if allr:
            with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(allr[0].keys()))
                w.writeheader()
                w.writerows(allr)
            print(f"\n  明細を {args.csv} に保存({len(allr)}件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
