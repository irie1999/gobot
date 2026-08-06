"""analyze_today_delay.py — 「今日の損切りは delay を伸ばせば回避できたか」を実約定ベースで答える。

背景:
  lss の損失の大半は「約定直後の寄り付近で、タイトな損切りが一瞬のヒゲに刈られる」もの。
  対策として delay1(約定した5分足の間は損切りを効かせない)を入れているが、
  2026-08-06 は 09:00約定の6件が 09:05〜09:14 に損切りされ、delay1(5分)では
  足りていない疑いが出た。

  compare_lss_rules.py は240日ぶんの母集団で delay を比較するツールで、
  「今日のこの銘柄が助かったのか」は答えない。このスクリプトはその逆で、
  **実際に約定した銘柄だけ**を、**実際の約定値・約定時刻**を起点に、
  delay 0〜N で振り直して1件ずつ結果を出す。

  ⚠ これは今日1日のサンプル。ここで delay2 が良く見えても、運用を変える根拠は
  compare_lss_rules.py の母集団比較(§18.9)。本スクリプトは「今日 何が起きたか」の
  解像度を上げるためのもの。

前提データ:
  fills_<yyyyMMdd>.csv      … .\\fills が残す実約定(往復)。実売値・約定時刻の出どころ。
  発注記録                    … 損切り/利確価格。lss_exit_watcher._load_lss_orders を再利用し、
                              ordered_signals_lss.csv (kabu_send_lss経由) と
                              placed_orders_<日付>.csv (レポートの発注ボタン=order_server経由)
                              の両方から読む。実運用は後者なので前者だけでは取れない。
  5分足(stock_5min)          … その日の値動き。

使い方(swingtrade フォルダで):
  python analyze_today_delay.py                    # 当日の fills_<今日>.csv を自動検出
  python analyze_today_delay.py --date 2026-08-06
  python analyze_today_delay.py --max-delay 6      # 0〜6本(=0〜30分)まで振る
  python analyze_today_delay.py --bars 6247        # 指定銘柄の5分足を1本ずつ表示
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick
from daytrade_data import load_intraday, split_by_day
from sameday5m_firsttouch import short_pnl

QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY

ap = argparse.ArgumentParser(
    description="今日の実約定を delay 別に振り直して『損切りは回避できたか』を出す")
ap.add_argument("--date", type=str, default=None,
                help="対象日 YYYY-MM-DD または yyyyMMdd (既定=fills_*.csv の最新)")
ap.add_argument("--fills-csv", type=str, default=None, help="fills_<日付>.csv を明示指定")
# 損切り/利確価格は lss_exit_watcher._load_lss_orders が読む発注記録から取る
# (ordered_signals_lss.csv と placed_orders_<日付>.csv の両方)。個別指定は不要。
ap.add_argument("--max-delay", type=int, default=6,
                help="振る delay の上限(5分足の本数)。既定6=30分まで")
ap.add_argument("--source", type=str, default=None, help="load_intraday の source")
ap.add_argument("--bars", type=str, default=None,
                help="この銘柄の5分足を1本ずつ表示(損切りラインとの位置関係を目視する)")
args = ap.parse_args()


def _resolve_fills() -> tuple[Path, str]:
    """(fillsのパス, yyyyMMdd) を返す。"""
    if args.fills_csv:
        p = Path(args.fills_csv)
        if not p.exists():
            sys.exit(f"[error] {p} がありません")
        return p, "".join(ch for ch in p.stem if ch.isdigit())[-8:]
    if args.date:
        d = "".join(ch for ch in args.date if ch.isdigit())
        p = Path(f"fills_{d}.csv")
        if not p.exists():
            sys.exit(f"[error] {p} がありません。先に `.\\fills --date {d}` を実行してください")
        return p, d
    cands = sorted(Path(".").glob("fills_*.csv"))
    if not cands:
        sys.exit("[error] fills_*.csv がありません。引け後に `.\\fills` を実行してください")
    p = cands[-1]
    return p, "".join(ch for ch in p.stem if ch.isdigit())[-8:]


fills_path, date_dig = _resolve_fills()
try:
    the_day = datetime.strptime(date_dig, "%Y%m%d").date()
except Exception:
    sys.exit(f"[error] 日付を特定できません: {fills_path}")

print(f"[入力] {fills_path.name} / 対象日 {the_day}")


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


# ── 実約定(往復)を読む ────────────────────────────────────────
actual: list[dict] = []
with open(fills_path, encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        sym = str(r.get("symbol", "")).strip()
        if not sym:
            continue
        actual.append({
            "sym": sym, "name": str(r.get("name", "")),
            "qty": int(_num(r.get("qty")) or QTY),
            "entry": _num(r.get("entry(売)")), "exit": _num(r.get("exit(買戻)")),
            "entry_t": str(r.get("entry_t", "")), "exit_t": str(r.get("exit_t", "")),
            "pnl": _num(r.get("pnl")),
        })
if not actual:
    sys.exit(f"[error] {fills_path} に往復完了した取引がありません")

# ── 発注記録から損切り/利確価格を拾う ─────────────────────────
# ★ 発注経路は2つある。watcher の _load_lss_orders が唯一の正しい読み手なので、
#   自前でCSVを読まずにそれを再利用する:
#     A) ordered_signals_lss.csv  … kabu_send_lss.py 経由の発注
#     B) placed_orders_<date>.csv … レポートの「発注」ボタン(order_server)経由 ← 実運用はこちら
#   自前で A) だけを読んでいたため 2026-08-06 は全銘柄が「損切り価格なし」になった。
try:
    from lss_exit_watcher import _load_lss_orders, _match_lss
except Exception as e:
    sys.exit(f"[error] lss_exit_watcher の読込に失敗しました: {e}")

lss_map = _load_lss_orders(str(the_day), all_dates=True)
if not lss_map:
    print("[!] lss の発注記録が1件も読めませんでした。以下を確認してください:")
    print("      ・ordered_signals_lss.csv (kabu_send_lss 経由)")
    print("      ・placed_orders_<日付>.csv (レポートの発注ボタン経由)")
    sys.exit(1)
_n_src = sum(len(v) for v in lss_map.values())
print(f"[発注記録] {len(lss_map)}銘柄 / {_n_src}件 を読込")


def _lookup(sym: str, entry: float, qty: int) -> dict | None:
    """実約定(銘柄・平均売値・数量)に対応する発注記録を引く。
    watcher と同じ _match_lss(数量一致 → 発注日が新しい → 価格が近い)で選ぶ。
    価格許容を段階的に広げる(実約定は寄りギャップで注文値から離れることがある)。"""
    for tol in (0.05, 0.10, 0.20, 1.00):
        rec = _match_lss(sym, entry, qty, lss_map, tol)
        if rec and _num(rec.get("stop")) > 0 and _num(rec.get("target")) > 0:
            return rec
    # 価格を無視して、対象日の記録 → 最新の記録 の順で拾う最終手段
    cands = [c for c in lss_map.get(sym, [])
             if _num(c.get("stop")) > 0 and _num(c.get("target")) > 0]
    if not cands:
        return None
    same = [c for c in cands if str(c.get("date", ""))[:10] == str(the_day)]
    return (same or sorted(cands, key=lambda c: str(c.get("date", ""))))[-1]


def _simulate(bars, ei: int, entry: float, stop_p: float, target_p: float,
              delay: int, day_close: float):
    """約定バー ei から利確、ei+delay からタイト損切りを first-touch(損切り優先)。

    損切り約定価格は compare_lss_rules の『現実(real)』モデルに合わせる:
      損切り注文を新規に板へ置いたバー(=ei+delay, ただし delay>0 のときだけ)の始値が
      既にラインを超えていたら即時成行 max(line, open)。それ以外は板の逆指値がラインで約定。
    """
    o = bars["open"].to_numpy(dtype=float)
    h = bars["high"].to_numpy(dtype=float)
    lo = bars["low"].to_numpy(dtype=float)
    c = bars["close"].to_numpy(dtype=float)
    n = len(h)
    stop_from = ei + delay
    for j in range(ei, n):
        if j >= stop_from and h[j] >= stop_p:
            px = stop_p
            if j == stop_from and stop_from > ei:
                px = max(stop_p, float(o[j]))
            return "損切り", float(px), j
        if lo[j] <= target_p:
            return "利確", float(target_p), j
    cx = day_close if day_close > 0 else float(c[-1])
    return "引け", float(cx), n - 1


rows: list[dict] = []
delays = list(range(0, max(1, args.max_delay) + 1))
missing: list[str] = []

for a in actual:
    sym = a["sym"]
    px = _lookup(sym, a["entry"], a["qty"])
    if not px:
        _have = len(lss_map.get(sym, []))
        missing.append(f"{sym} {a['name']}(発注記録に損切り価格なし / 記録{_have}件)")
        continue
    try:
        m5 = load_intraday(sym, days=10, source=args.source)
    except Exception as e:
        missing.append(f"{sym} {a['name']}(5分足読込失敗: {e})")
        continue
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    bars = by_day.get(the_day)
    if bars is None or len(bars) < 2:
        missing.append(f"{sym} {a['name']}(対象日の5分足なし)")
        continue

    # 損切り=上 / 利確=下 (空売り)。compare_lss_rules._prices と同じ呼値丸め。
    stop_p = float(ceil_to_tick(max(px["stop"], px["target"])))
    target_p = float(ceil_to_tick(min(px["stop"], px["target"])))
    entry = a["entry"]

    # 実際の約定時刻に対応する5分足バーを探す(HH:MM)。見つからなければ最初のバー。
    ei = 0
    if ":" in a["entry_t"]:
        try:
            hh, mm = (int(x) for x in a["entry_t"].split(":")[:2])
            emin = hh * 60 + mm
            ei = None
            for j, ts in enumerate(bars.index):
                if int(ts.hour) * 60 + int(ts.minute) <= emin:
                    ei = j
                else:
                    break
            if ei is None:
                ei = 0
        except Exception:
            ei = 0

    day_close = float(bars["close"].iloc[-1])
    res = {}
    for k in delays:
        reason, ex_p, xi = _simulate(bars, ei, entry, stop_p, target_p, k, day_close)
        pnl = short_pnl(entry, ex_p, "stop" if reason == "損切り" else reason,
                        a["qty"], FEE_ONE_WAY, 0.0)
        res[k] = {"reason": reason, "exit": ex_p, "pnl": pnl,
                  "t": f"{bars.index[xi].hour:02d}:{bars.index[xi].minute:02d}"}
    rows.append({"a": a, "stop": stop_p, "target": target_p, "ei": ei,
                 "ei_t": f"{bars.index[ei].hour:02d}:{bars.index[ei].minute:02d}",
                 "res": res, "bars": bars, "strategy": px.get("strategy", "")})

if not rows:
    print("[error] 計算できた銘柄がありません:")
    for m in missing:
        print("   ", m)
    sys.exit(1)

# ── 銘柄別 ───────────────────────────────────────────────────
hdr_d = "".join(f"{('d' + str(k)):>17}" for k in delays)
print()
print("=" * (46 + 17 * len(delays)))
print(f"銘柄別 delay スイープ  ({the_day} / 実約定 {len(rows)}銘柄)")
print("  実 = .\\fills の実績。d0=即損切り(base) / d1=5分後から / d2=10分後から …")
print("=" * (46 + 17 * len(delays)))
print(f"{'コード':>6} {'銘柄':<12}{'実売値':>9}{'損切ライン':>10}{'約定':>6}{'実損益':>10}" + hdr_d)
for r in rows:
    a = r["a"]
    line = (f"{a['sym']:>6} {a['name'][:12]:<12}{a['entry']:>9,.1f}"
            f"{r['stop']:>10,.1f}{r['ei_t']:>6}{a['pnl']:>+10,.0f}")
    for k in delays:
        v = r["res"][k]
        mark = {"損切り": "損", "利確": "利", "引け": "引"}.get(v["reason"], "?")
        line += f"{mark + v['t']:>9}{v['pnl']:>+8,.0f}"
    print(line)

# ── 合計 ─────────────────────────────────────────────────────
print("-" * (46 + 17 * len(delays)))
tot_actual = sum(r["a"]["pnl"] for r in rows)
line = f"{'合計':>6} {'':<12}{'':>9}{'':>10}{'':>6}{tot_actual:>+10,.0f}"
for k in delays:
    s = sum(r["res"][k]["pnl"] for r in rows)
    line += f"{'':>9}{s:>+8,.0f}"
print(line)

print()
print("=" * 78)
print("delay 別サマリー")
print("=" * 78)
print(f"{'delay':<10}{'損切り':>8}{'利確':>7}{'引け':>7}{'勝ち':>7}{'損益':>13}{'d0比':>13}")
base_sum = sum(r["res"][0]["pnl"] for r in rows)
for k in delays:
    vs = [r["res"][k] for r in rows]
    n_stop = sum(1 for v in vs if v["reason"] == "損切り")
    n_tgt = sum(1 for v in vs if v["reason"] == "利確")
    n_cls = sum(1 for v in vs if v["reason"] == "引け")
    n_win = sum(1 for v in vs if v["pnl"] > 0)
    s = sum(v["pnl"] for v in vs)
    lbl = f"d{k}({k * 5}分)"
    print(f"{lbl:<10}{n_stop:>8}{n_tgt:>7}{n_cls:>7}{n_win:>7}{s:>+13,.0f}{s - base_sum:>+13,.0f}")
print(f"{'実績':<10}{'':>8}{'':>7}{'':>7}"
      f"{sum(1 for r in rows if r['a']['pnl'] > 0):>7}{tot_actual:>+13,.0f}")

# ── 回避できた損切りの明細 ────────────────────────────────────
print()
print("=" * 78)
print("『delay を伸ばせば助かった』銘柄 (d0 で損切り → その delay では損切りにならない)")
print("=" * 78)
any_saved = False
for k in delays[1:]:
    saved = [r for r in rows
             if r["res"][0]["reason"] == "損切り" and r["res"][k]["reason"] != "損切り"]
    if not saved:
        continue
    any_saved = True
    gain = sum(r["res"][k]["pnl"] - r["res"][0]["pnl"] for r in saved)
    print(f"\n▼ d{k}({k * 5}分待つ): {len(saved)}銘柄 が損切りを回避 → 差 {gain:+,.0f}円")
    for r in saved:
        v0, vk = r["res"][0], r["res"][k]
        print(f"   {r['a']['sym']:>6} {r['a']['name'][:12]:<12} "
              f"d0:損切り{v0['t']} {v0['pnl']:+,.0f}円 → "
              f"d{k}:{vk['reason']}{vk['t']} {vk['pnl']:+,.0f}円 "
              f"({vk['pnl'] - v0['pnl']:+,.0f})")
if not any_saved:
    print("  なし (どの delay でも損切りは損切りのまま)")

# ── 逆に delay で悪化した銘柄 ─────────────────────────────────
print()
print("=" * 78)
print("『delay で悪化した』銘柄 (待った結果、損切り価格が不利になった/利確を逃した)")
print("=" * 78)
any_worse = False
for k in delays[1:]:
    worse = [r for r in rows if r["res"][k]["pnl"] < r["res"][0]["pnl"] - 1]
    if not worse:
        continue
    any_worse = True
    loss = sum(r["res"][k]["pnl"] - r["res"][0]["pnl"] for r in worse)
    print(f"\n▼ d{k}({k * 5}分待つ): {len(worse)}銘柄 が悪化 → 差 {loss:+,.0f}円")
    for r in worse:
        v0, vk = r["res"][0], r["res"][k]
        print(f"   {r['a']['sym']:>6} {r['a']['name'][:12]:<12} "
              f"d0:{v0['reason']}{v0['t']} {v0['pnl']:+,.0f}円 → "
              f"d{k}:{vk['reason']}{vk['t']} {vk['pnl']:+,.0f}円 "
              f"({vk['pnl'] - v0['pnl']:+,.0f})")
if not any_worse:
    print("  なし")

if missing:
    print("\n[計算できなかった銘柄]")
    for m in missing:
        print("   ", m)

# ── 指定銘柄の5分足を1本ずつ ─────────────────────────────────
if args.bars:
    tgt = str(args.bars).strip()
    r = next((x for x in rows if x["a"]["sym"] == tgt), None)
    if r is None:
        print(f"\n[!] {tgt} は対象に含まれていません")
    else:
        print()
        print("=" * 78)
        print(f"{tgt} {r['a']['name']} の5分足 (損切り {r['stop']:,.1f} / 利確 {r['target']:,.1f} "
              f"/ 実売値 {r['a']['entry']:,.1f} / 約定バー {r['ei_t']})")
        print("=" * 78)
        print(f"{'時刻':>7}{'始値':>10}{'高値':>10}{'安値':>10}{'終値':>10}   判定")
        for j, (ts, row) in enumerate(r["bars"].iterrows()):
            flag = ""
            if j == r["ei"]:
                flag += " ←約定"
            if float(row["high"]) >= r["stop"]:
                flag += " ★損切りライン超"
            if float(row["low"]) <= r["target"]:
                flag += " ◎利確ライン到達"
            print(f"{ts.hour:>4d}:{ts.minute:02d}{row['open']:>10,.1f}{row['high']:>10,.1f}"
                  f"{row['low']:>10,.1f}{row['close']:>10,.1f}  {flag}")

print()
print("⚠ これは1日ぶんのサンプルです。運用の delay を変える判断は "
      "compare_lss_rules.py の母集団比較(240日)で行ってください:")
print("   python compare_lss_rules.py --bt-min 40 --days 240 --min-price 1000 --max-price 6000")
