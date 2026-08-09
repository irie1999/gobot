"""
verify_bt_score_shift.py — BTスコアが急変した原因を検証する

使い方:
  python verify_bt_score_shift.py                   # DIC(4631.T) RSI2
  python verify_bt_score_shift.py --symbol 4631.T --strategy RSI2
  python verify_bt_score_shift.py --symbol 8154.T --strategy RSI2 --days 3

何を調べるか:
  1. 365日ウィンドウの境界(今日-365日 前後)にどんなトレードがあるか
  2. 「昨日の計算」と「今日の計算」でBTスコアがどう変わるか
  3. スコアの構成要素(勝率/PF/安定性/取引数)の変化
  4. 結論: 何が原因でスコアが変動したか
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "conservative")

parser = argparse.ArgumentParser()
parser.add_argument("--symbol",   default="4631.T", help="銘柄コード (デフォルト: DIC 4631.T)")
parser.add_argument("--strategy", default="RSI2",   help="戦略 (MACD/A7/RSI2/DON/VOL/MOM)")
parser.add_argument("--days",     type=int, default=3,
                    help="境界前後N日のトレードを強調 (デフォルト: 3)")
parser.add_argument("--mode",     default=None, choices=["conservative", "aggressive"],
                    help="トレードモード (デフォルト: 環境変数 TRADING_MODE または conservative)")
args = parser.parse_args()

if args.mode:
    os.environ["TRADING_MODE"] = args.mode

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()
YESTERDAY = TODAY - timedelta(days=1)

# ── バックテスト実行 ──────────────────────────────────────────────────────────
print(f"{'='*65}")
print(f"BTスコア変動検証: {args.symbol}  戦略={args.strategy}")
print(f"今日: {TODAY}   昨日: {YESTERDAY}")
print(f"{'='*65}")

# 戦略が stop か breakout かを判定
_stop_strats = {"MACD", "A7", "RSI2"}
_brk_strats  = {"DON", "VOL", "MOM"}

if args.strategy in _stop_strats:
    import check_signals_stop as _mod
elif args.strategy in _brk_strats:
    import check_signals_breakout as _mod
else:
    print(f"[ERROR] 不明な戦略: {args.strategy}")
    raise SystemExit(1)

if args.strategy not in _mod.STRATEGY_PARAMS:
    print(f"[ERROR] {args.strategy} は {_mod.__name__} に存在しません")
    raise SystemExit(1)

from backtest_limit_entry import fetch, run_limit_backtest, BACKTEST_DAYS

calc_fn, em, sm, tm = _mod.STRATEGY_PARAMS[args.strategy]
ENTRY_TYPE = getattr(_mod, "ENTRY_TYPE", "stop")

mode_str = os.environ.get("TRADING_MODE", "conservative")
print(f"\nトレードモード: {mode_str}")
print(f"戦略パラメータ: em={em}  sm={sm}  tm={tm}  entry_type={ENTRY_TYPE}")
print("データ取得・バックテスト実行中...")

df = fetch(args.symbol, BACKTEST_DAYS + 30)  # 余裕を持たせてフェッチ
if df is None:
    print(f"[ERROR] {args.symbol} のデータ取得失敗")
    raise SystemExit(1)

full_r = run_limit_backtest(
    args.symbol, "", df, calc_fn, em, sm, tm,
    BACKTEST_DAYS, args.strategy, entry_type=ENTRY_TYPE
)
if not full_r:
    print("[ERROR] バックテスト結果なし")
    raise SystemExit(1)

trade_log = [t for t in full_r["trade_log"] if t.get("reason") != "発注中"]
print(f"全トレード数: {len(trade_log)}件 (発注中除く)")

# ── ウィンドウ境界 ────────────────────────────────────────────────────────────
# 「昨日」のBTスコア = 昨日を基準日として365日前以降のトレード
# 「今日」のBTスコア = 今日を基準日として365日前以降のトレード
WINDOW = 365
cutoff_today     = TODAY     - timedelta(days=WINDOW)
cutoff_yesterday = YESTERDAY - timedelta(days=WINDOW)

print(f"\n{'─'*65}")
print(f"昨日の計算範囲: {cutoff_yesterday} 〜 {YESTERDAY}  (signal_dt基準)")
print(f"今日の計算範囲: {cutoff_today}     〜 {TODAY}")
print(f"境界: {cutoff_yesterday} のトレードが今日のウィンドウから【脱落】する")
print(f"{'─'*65}")

# ── トレード分類 ──────────────────────────────────────────────────────────────
def get_sig_date(t):
    sd = t.get("signal_dt")
    return sd.date() if hasattr(sd, "date") else sd

trades_in_yesterday = [t for t in trade_log if get_sig_date(t) >= cutoff_yesterday]
trades_in_today     = [t for t in trade_log if get_sig_date(t) >= cutoff_today]
trades_dropped      = [t for t in trade_log
                       if cutoff_yesterday <= get_sig_date(t) < cutoff_today]
trades_added        = [t for t in trade_log
                       if cutoff_today <= get_sig_date(t) <= TODAY
                       and get_sig_date(t) not in
                           {get_sig_date(t2) for t2 in trades_in_yesterday}]

# ── BTスコア計算 ──────────────────────────────────────────────────────────────
def calc_score_from_trades(trades, label):
    if not trades:
        return 0, "—", {}
    filled = len(trades)
    wins   = sum(1 for t in trades if t["pnl"] > 0)
    losses = filled - wins
    gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    pf_cap = min(pf if pf != float("inf") else 10, 10)
    wr     = wins / filled * 100
    total_pnl = sum(t["pnl"] for t in trades)
    stable = 1.0 if total_pnl > 0 else 0.0  # 単一期間の場合

    # 複数期間での安定性（PERIODS = [30, 60, 90, 180, 365]）
    PERIODS = [30, 60, 90, 180, 365]
    today_ref = TODAY if label == "今日" else YESTERDAY
    period_results = {}
    for d in PERIODS:
        cutoff = today_ref - timedelta(days=d)
        sub = [t for t in trade_log
               if get_sig_date(t) >= cutoff
               and (label == "今日" and get_sig_date(t) <= TODAY
                    or label == "昨日" and get_sig_date(t) <= YESTERDAY)]
        if not sub:
            continue
        s_n    = len(sub)
        s_wins = sum(1 for t in sub if t["pnl"] > 0)
        s_gp   = sum(t["pnl"] for t in sub if t["pnl"] > 0)
        s_gl   = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
        s_pf   = s_gp / s_gl if s_gl > 0 else (float("inf") if s_gp > 0 else 0.0)
        period_results[d] = {
            "trades": s_n, "wins": s_wins,
            "win_rate": s_wins / s_n * 100,
            "pf": s_pf,
            "total_pnl": sum(t["pnl"] for t in sub),
        }

    # calc_recommend_score と同じ計算
    results = [r for r in period_results.values() if r["trades"] > 0]
    if not results:
        return 0, "—", {}
    avg_wr  = sum(r["win_rate"] for r in results) / len(results)
    avg_pf  = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                  for r in results) / len(results)
    stable  = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades= sum(r["trades"] for r in results)

    wr_pt   = avg_wr * 0.4
    pf_pt   = (avg_pf / 10) * 30
    st_pt   = stable * 20
    tc_pt   = min(t_trades / 20, 1) * 10
    score   = round(wr_pt + pf_pt + st_pt + tc_pt)
    rank    = "★★★" if score >= 80 else "★★" if score >= 60 else "★" if score >= 40 else "△"

    components = {
        "取引数": filled,
        "勝率":  f"{avg_wr:.1f}% ({wr_pt:.1f}点)",
        "PF":    f"{avg_pf:.2f} ({pf_pt:.1f}点)",
        "安定性": f"{stable*100:.0f}% ({st_pt:.1f}点)",
        "取引数点": f"({tc_pt:.1f}点)",
        "合計損益": f"{sum(t['pnl'] for t in trades):+,.0f}円",
    }
    return score, rank, components

score_yday, rank_yday, comp_yday = calc_score_from_trades(trades_in_yesterday, "昨日")
score_today, rank_today, comp_today = calc_score_from_trades(trades_in_today,  "今日")

# ── 結果表示 ──────────────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"【BTスコア比較】")
print(f"{'='*65}")
print(f"{'':10}{'昨日':>15}{'今日':>15}{'差分':>10}")
print(f"{'─'*50}")
print(f"{'BTスコア':10}{score_yday:>15}{score_today:>15}{score_today - score_yday:>+10}")
print(f"{'ランク':10}{rank_yday:>15}{rank_today:>15}")
print(f"{'取引数':10}{comp_yday.get('取引数',0):>15}{comp_today.get('取引数',0):>15}"
      f"{comp_today.get('取引数',0) - comp_yday.get('取引数',0):>+10}")

for key in ["勝率", "PF", "安定性"]:
    print(f"  {key}: 昨日={comp_yday.get(key,'—')}  今日={comp_today.get(key,'—')}")
print(f"  合計損益: 昨日={comp_yday.get('合計損益','—')}  今日={comp_today.get('合計損益','—')}")

# ── 境界付近のトレード詳細 ────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"【ウィンドウ境界付近のトレード】")
print(f"  {cutoff_yesterday - timedelta(days=args.days)} 〜 {cutoff_today + timedelta(days=args.days)} のトレード")
print(f"{'='*65}")

boundary_start = cutoff_yesterday - timedelta(days=args.days)
boundary_end   = cutoff_today     + timedelta(days=args.days)

boundary_trades = [t for t in trade_log
                   if boundary_start <= get_sig_date(t) <= boundary_end]
boundary_trades.sort(key=lambda t: get_sig_date(t))

if not boundary_trades:
    print("  → この範囲にトレードなし")
else:
    print(f"{'signal_dt':12} {'entry_dt':10} {'exit_dt':10} {'pnl':>10} {'reason':10} {'status'}")
    print(f"{'─'*70}")
    for t in boundary_trades:
        sd  = get_sig_date(t)
        ed  = t.get("entry_dt")
        xd  = t.get("exit_dt")
        pnl = t.get("pnl", 0)
        rsn = t.get("reason", "")
        ed_s = ed.strftime("%m/%d") if hasattr(ed, "strftime") else str(ed)
        xd_s = xd.strftime("%m/%d") if hasattr(xd, "strftime") else str(xd)
        pnl_s = f"{pnl:+,.0f}円"

        if get_sig_date(t) < cutoff_today:
            status = "⬅ 【脱落】昨日のみカウント"
        elif TODAY - timedelta(days=1) <= get_sig_date(t) <= TODAY:
            status = "➕ 【追加】今日から追加"
        else:
            status = "  両方カウント"

        pnl_mark = "✅ 勝ち" if pnl > 0 else ("❌ 負け" if pnl < 0 else "—")
        print(f"{sd!s:12} {ed_s:10} {xd_s:10} {pnl_s:>12}  {rsn:12} {pnl_mark} {status}")

# ── 全トレードリスト ──────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"【全365日トレード一覧（signal_dt順）】")
print(f"  昨日window: {cutoff_yesterday} 〜  今日window: {cutoff_today} 〜")
print(f"{'='*65}")

all_relevant = sorted(trades_in_yesterday, key=lambda t: get_sig_date(t))
if not all_relevant:
    print("  トレードなし")
else:
    print(f"{'signal_dt':12} {'entry_dt':10} {'exit_dt':10} {'pnl':>12} {'reason':12} {'window'}")
    print(f"{'─'*75}")
    for t in all_relevant:
        sd  = get_sig_date(t)
        ed  = t.get("entry_dt")
        xd  = t.get("exit_dt")
        pnl = t.get("pnl", 0)
        rsn = t.get("reason", "")
        ed_s = ed.strftime("%m/%d") if hasattr(ed, "strftime") else str(ed)
        xd_s = xd.strftime("%m/%d") if hasattr(xd, "strftime") else str(xd)
        pnl_s = f"{pnl:+,.0f}円"
        in_yday = sd >= cutoff_yesterday
        in_today = sd >= cutoff_today
        if in_today:
            win_mark = "昨日○ 今日○"
        elif in_yday:
            win_mark = "昨日○ 今日✗ ⬅脱落"
        else:
            win_mark = "昨日✗ 今日✗"
        pnl_mark = "✅" if pnl > 0 else ("❌" if pnl < 0 else "—")
        print(f"{sd!s:12} {ed_s:10} {xd_s:10} {pnl_s:>12}  {rsn:12}  {pnl_mark} {win_mark}")

# ── 結論 ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"【結論】")
print(f"{'='*65}")
dropped_pnl = sum(t["pnl"] for t in trades_dropped)
if trades_dropped:
    wins_d = sum(1 for t in trades_dropped if t["pnl"] > 0)
    print(f"脱落したトレード: {len(trades_dropped)}件  (勝ち{wins_d}件 / 負け{len(trades_dropped)-wins_d}件)")
    for t in trades_dropped:
        sd = get_sig_date(t)
        pnl = t["pnl"]
        rsn = t.get("reason", "")
        print(f"  → signal_dt={sd}  損益={pnl:+,.0f}円  理由={rsn}")
    print(f"  合計損益影響: {dropped_pnl:+,.0f}円")
else:
    print("脱落したトレード: なし")

if score_today < score_yday:
    print(f"\nBTスコア低下の原因:")
    if trades_dropped and any(t["pnl"] > 0 for t in trades_dropped):
        print(f"  ✅ 仮説【正しい】: 勝ちトレードがウィンドウ境界で脱落したためスコアが低下")
    elif trades_dropped and all(t["pnl"] <= 0 for t in trades_dropped):
        print(f"  ❌ 仮説【別原因】: 脱落したのは負けトレードなのに下がった → 他の要因を確認")
    else:
        print(f"  ⚠ 境界付近に脱落トレードなし → 新規取引の影響か、別の期間の変化")
elif score_today > score_yday:
    print(f"\nBTスコアは上昇 (今日={score_today} > 昨日={score_yday})")
else:
    print(f"\nBTスコアは変化なし (={score_today})")
