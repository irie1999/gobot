"""
check_signals.py  ―  監視銘柄のシグナルチェック
=================================================
【今日のシグナル確認】
  python check_signals.py
  python check_signals.py --verbose

【過去期間のシグナル発生回数】
  python check_signals.py --history --days 90
  python check_signals.py --history --from 2025-10-01 --to 2026-03-31
  python check_signals.py --history --days 90 --verbose   # 発生日も表示
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import backtest_adaptive_mr       as _amr
import backtest_strong_pullback   as _sp
import backtest_donchian_pullback as _don
import backtest_bb_volume         as _bbv
import backtest_limit_oco         as _oco

JST = timezone(timedelta(hours=9))

WATCHLIST: list[tuple[str, str, list[str]]] = [
    # ── 強い押し目（上昇トレンド中の押し目・高ボラ耐性あり） ──────
    ("7012.T", "川崎重工業",       ["strong_pullback", "limit_oco"]),  # 2戦略で直近プラス
    ("7013.T", "IHI",              ["strong_pullback", "donchian"]),   # 防衛・航空
    ("5981.T", "東京製綱",         ["strong_pullback"]),               # 全期間完全一致
    ("9044.T", "南海電鉄",         ["strong_pullback"]),               # 鉄道ディフェンシブ
    ("4118.T", "カネカ",           ["strong_pullback"]),               # 30日+27k
    ("7952.T", "河合楽器",         ["strong_pullback"]),               # 全期間一定+28k
    ("8795.T", "T&Dホールディングス", ["strong_pullback"]),            # 30日+27k
    ("9042.T", "阪急阪神HD",       ["strong_pullback"]),               # 30日+28k
    ("5702.T", "大紀アルミニウム工業所", ["strong_pullback"]),         # 全期間一定+20k
    ("9503.T", "関西電力",         ["strong_pullback"]),               # 公益ディフェンシブ
    ("5333.T", "日本碍子",         ["strong_pullback"]),               # 全期間一定+24k

    # ── アダプティブMR（高ボラ逆張り・現相場に最適） ─────────────
    ("1605.T", "INPEX",            ["adaptive_mr", "donchian"]),       # MR1位
    ("6361.T", "荏原製作所",       ["adaptive_mr"]),                   # MR2位・30日+31k
    ("8802.T", "三菱地所",         ["adaptive_mr"]),                   # 30日+11k・改善傾向
    ("2809.T", "キユーピー",       ["adaptive_mr"]),                   # 30日+8k・食品DF
    ("6058.T", "ベクトル",         ["adaptive_mr"]),                   # 30日+6k・低位株

    # ── ドンチャン押し目（ブレイクアウト後の押し目） ──────────────
    ("5844.T", "京都フィナンシャルグループ", ["donchian", "strong_pullback"]),
    ("6963.T", "ローム",           ["donchian"]),                      # 30日+15k
    ("7389.T", "あいちフィナンシャルグループ", ["donchian"]),          # 30日+17k

    # ── BB出来高（BB下限＋出来高急増） ───────────────────────────
    ("5741.T", "UACJ",             ["bb_volume"]),                     # 30日+24k
    ("9742.T", "アイネス",         ["bb_volume"]),                     # 全期間一定+7k
    ("6282.T", "オイレス工業",     ["bb_volume"]),                     # 全期間一定+7k
]

STRATEGY_NAMES = {
    "adaptive_mr":    "アダプティブMR",
    "strong_pullback":"強い押し目",
    "donchian":       "ドンチャン押し目",
    "bb_volume":      "BB出来高",
    "limit_oco":      "指値OCO",
}

LOT = 100


# ─────────────────────────────────────────────────────────────────────
# 共通ユーティリティ
# ─────────────────────────────────────────────────────────────────────

def _rr(entry: float, stop: float, target: float) -> str:
    risk = entry - stop
    if risk <= 0:
        return "—"
    return f"{(target - entry) / risk:.1f}R"


def _pct(a: float, b: float) -> str:
    if b == 0:
        return "—"
    return f"{(a - b) / b * 100:+.2f}%"


def _fetch_and_calc(symbol: str, key: str, fetch_days: int) -> pd.DataFrame | None:
    """戦略に応じたfetch+calcを実行してDataFrameを返す。"""
    try:
        if key == "adaptive_mr":
            df = _amr.fetch(symbol, fetch_days)
            return _amr.calc(df) if df is not None else None
        elif key == "strong_pullback":
            df = _sp.fetch(symbol, fetch_days)
            return _sp.calc(df) if df is not None else None
        elif key == "donchian":
            df = _don.fetch(symbol, fetch_days)
            return _don.calc(df) if df is not None else None
        elif key == "bb_volume":
            df = _bbv.fetch(symbol, fetch_days)
            return _bbv.calc(df) if df is not None else None
        elif key == "limit_oco":
            df = _oco.fetch(symbol, fetch_days)
            return _oco.calc(df) if df is not None else None
    except Exception as e:
        print(f"  ※ {symbol} [{key}] fetch/calc エラー: {e}", file=sys.stderr)
    return None


def _check_signal_on_slice(df_slice: pd.DataFrame, key: str) -> dict | None:
    """dfの最終行でシグナル判定を行い、結果dictまたはNoneを返す。"""
    try:
        if key == "adaptive_mr":
            return _amr._has_signal_today(df_slice)
        elif key == "strong_pullback":
            return _sp._has_signal_today(df_slice)
        elif key == "donchian":
            return _don._has_signal_today(df_slice)
        elif key == "bb_volume":
            return _bbv._has_signal_today(df_slice)
        elif key == "limit_oco":
            return _oco._has_signal_today(df_slice, _oco.DEFAULT_PARAMS)
    except Exception:
        pass
    return None


def _sig_to_display(key: str, sig: dict) -> dict:
    """シグナルdictを表示用に正規化する。"""
    entry = sig.get("limit_price", 0.0)
    stop  = sig.get("stop_loss") or sig.get("stop", 0.0)
    tgt   = sig.get("profit_target") or sig.get("target", 0.0)
    atr   = sig.get("atr", 0.0)

    # target未設定の戦略は3R目安で補完
    if tgt == 0.0 and atr > 0:
        tgt = entry + atr * 3.0

    expire = sig.get("expire_days", 3)
    if isinstance(expire, list):
        expire = max(expire)

    if key == "adaptive_mr":
        note = f"RSI2={sig.get('rsi2', 0):.1f} IBS={sig.get('ibs', float('nan')):.2f} レジーム={sig.get('regime','—')}"
    elif key == "strong_pullback":
        note = f"RSI5={sig.get('rsi5', 0):.1f}"
    elif key == "donchian":
        vr = sig.get("vol_ratio", float("nan"))
        note = f"DC高値={sig.get('don_high', 0):.0f}" + (f" 出来高比={vr:.1f}x" if not math.isnan(vr) else "")
    elif key == "bb_volume":
        vr = sig.get("vol_ratio", float("nan"))
        note = f"BB下限={sig.get('bb_lower', 0):.0f} BB中心={sig.get('bb_mid', 0):.0f}" + (f" 出来高比={vr:.1f}x" if not math.isnan(vr) else "")
    elif key == "limit_oco":
        note = f"RSI2={sig.get('rsi2', 0):.1f} IBS={sig.get('ibs', 0):.2f}"
    else:
        note = ""

    return dict(key=key, close=sig["close"], entry=entry, stop=stop,
                target=tgt, atr=atr, note=note, expire=expire)


# ─────────────────────────────────────────────────────────────────────
# 今日のシグナル確認
# ─────────────────────────────────────────────────────────────────────

def check_today(verbose: bool, watchlist: list | None = None) -> None:
    wl  = watchlist if watchlist is not None else WATCHLIST
    now = datetime.now(JST)
    print(f"\n{'='*72}")
    print(f"  監視銘柄シグナルチェック  {now.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"  対象: {len(wl)}銘柄 / ロットサイズ: {LOT}株")
    print(f"{'='*72}\n")

    active: list[tuple[str, str, dict]] = []
    waiting: list[tuple[str, str]] = []

    for symbol, name, strategies in wl:
        hits = []
        for key in strategies:
            df = _fetch_and_calc(symbol, key, 365)
            if df is None:
                continue
            sig = _check_signal_on_slice(df, key)
            if sig:
                hits.append(_sig_to_display(key, sig))

        if hits:
            for h in hits:
                active.append((symbol, name, h))
        else:
            waiting.append((symbol, name))

    if active:
        print(f"【シグナル発生】 {len(active)}件")
        print("─" * 72)
        for symbol, name, r in active:
            strat = STRATEGY_NAMES.get(r["key"], r["key"])
            risk  = r["entry"] - r["stop"]
            print(f"  ★ {symbol}  {name}")
            print(f"     戦略     : {strat}")
            print(f"     現在値   : ¥{r['close']:,.0f}")
            print(f"     指値     : ¥{r['entry']:,.0f}  ({_pct(r['entry'], r['close'])} from 現在値)")
            print(f"     損切     : ¥{r['stop']:,.0f}  (リスク ¥{risk:,.0f} / ロット ¥{risk*LOT:,.0f})")
            print(f"     目標     : ¥{r['target']:,.0f}  RR={_rr(r['entry'], r['stop'], r['target'])}")
            print(f"     有効期限 : {r['expire']}営業日")
            if verbose:
                print(f"     指標     : {r['note']}")
            print()
    else:
        print("【シグナル発生なし】\n")

    if waiting:
        print("【待機中（シグナルなし）】")
        for sym, nm in waiting:
            print(f"  {sym}  {nm}")
        print()

    print("─" * 72)
    print("  ・指値注文は翌営業日の寄り付き前に設定してください")
    print("  ・有効期限内に約定しなければキャンセル")
    print("  ・損切は必ず同時に逆指値注文を入れてください（OCO推奨）")
    print(f"{'='*72}\n")


# ─────────────────────────────────────────────────────────────────────
# 過去期間のシグナル発生回数
# ─────────────────────────────────────────────────────────────────────

def check_history(from_date: date, to_date: date, verbose: bool, watchlist: list | None = None) -> None:
    wl = watchlist if watchlist is not None else WATCHLIST
    # 指標計算のウォームアップ期間（200日MA等のため）
    WARMUP_DAYS = 400
    fetch_days  = (to_date - from_date).days + WARMUP_DAYS + 60

    now = datetime.now(JST)
    print(f"\n{'='*80}")
    print(f"  過去シグナル発生回数  {now.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"  集計期間: {from_date}  〜  {to_date}  ({(to_date - from_date).days + 1}日間)")
    print(f"  対象: {len(wl)}銘柄 / 各銘柄の担当戦略で集計")
    print(f"{'='*80}\n")

    # ヘッダー
    col_sym  = 10
    col_name = 20
    col_strat = 12
    col_cnt   = 6
    hdr = (f"{'コード':<{col_sym}}  {'銘柄名':<{col_name}}  "
           f"{'戦略':<{col_strat}}  {'回数':>{col_cnt}}  {'発生日（直近5件）'}")
    print("  " + hdr)
    print("  " + "─" * len(hdr))

    total_signals = 0

    for symbol, name, strategies in wl:
        for key in strategies:
            df = _fetch_and_calc(symbol, key, fetch_days)
            if df is None:
                strat = STRATEGY_NAMES.get(key, key)
                print(f"  {symbol:<{col_sym}}  {name:<{col_name}}  {strat:<{col_strat}}  {'—':>{col_cnt}}  データ取得失敗")
                continue

            # 対象期間の行インデックスを特定
            signal_dates: list[str] = []
            df_index_dates = [
                (i, idx.date() if hasattr(idx, "date") else idx)
                for i, idx in enumerate(df.index)
            ]

            for i, row_date in df_index_dates:
                if row_date < from_date:
                    continue
                if row_date > to_date:
                    break
                if i < 50:  # ウォームアップ不足行はスキップ
                    continue

                df_slice = df.iloc[: i + 1]
                sig = _check_signal_on_slice(df_slice, key)
                if sig:
                    signal_dates.append(str(row_date))

            count = len(signal_dates)
            total_signals += count
            strat = STRATEGY_NAMES.get(key, key)

            # 直近5件の日付
            recent = signal_dates[-5:] if signal_dates else []
            dates_str = "  ".join(recent) if recent else "—"

            print(f"  {symbol:<{col_sym}}  {name:<{col_name}}  "
                  f"{strat:<{col_strat}}  {count:>{col_cnt}}回  {dates_str}")

            if verbose and len(signal_dates) > 5:
                # 全日付を折り返し表示
                all_dates = "  ".join(signal_dates)
                print(f"  {'':>{col_sym}}  {'':>{col_name}}  {'全発生日':>{col_strat}}        {all_dates}")

        print()  # 銘柄間の空行

    print("─" * 80)
    print(f"  合計シグナル発生: {total_signals}回")
    print(f"{'='*80}\n")


# ─────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="監視銘柄シグナルチェック",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python check_signals.py                                        # 全銘柄・今日のシグナル
  python check_signals.py --stocks 7012.T 1605.T 6361.T         # 指定銘柄のみ
  python check_signals.py --list                                 # 登録銘柄一覧
  python check_signals.py --history --days 90                   # 過去90日の発生回数
  python check_signals.py --history --days 90 --stocks 7012.T   # 指定銘柄の過去集計
  python check_signals.py --history --from 2025-10-01 --to 2026-03-31
  python check_signals.py --history --days 180 --verbose        # 全発生日も表示
""")
    parser.add_argument("--verbose",  "-v", action="store_true", help="詳細表示")
    parser.add_argument("--history",        action="store_true", help="過去期間集計モード")
    parser.add_argument("--days",     type=int, default=90,
                        help="過去N日を集計（--history時）")
    parser.add_argument("--from",  dest="from_date", metavar="YYYY-MM-DD",
                        help="集計開始日")
    parser.add_argument("--to",    dest="to_date",   metavar="YYYY-MM-DD",
                        help="集計終了日（デフォルト: 今日）")
    parser.add_argument("--stocks", nargs="+", metavar="CODE",
                        help="チェック対象を指定銘柄コードに絞る（例: 7012.T 1605.T）")
    parser.add_argument("--list",   action="store_true",
                        help="登録銘柄の一覧を表示して終了")
    args = parser.parse_args()

    # ── 銘柄一覧表示 ────────────────────────────────────────────
    if args.list:
        print(f"\n登録銘柄一覧 （{len(WATCHLIST)}銘柄）")
        print("─" * 60)
        for sym, nm, strats in WATCHLIST:
            strat_str = " + ".join(STRATEGY_NAMES.get(s, s) for s in strats)
            print(f"  {sym:<10}  {nm:<22}  {strat_str}")
        print()
        return

    # ── 銘柄フィルタ ────────────────────────────────────────────
    if args.stocks:
        requested = {s.upper() for s in args.stocks}
        wl = [row for row in WATCHLIST if row[0].upper() in requested]
        not_found = requested - {row[0].upper() for row in wl}
        if not_found:
            print(f"※ 登録されていない銘柄コード: {', '.join(sorted(not_found))}")
            print(f"  --list で登録銘柄を確認できます\n")
        if not wl:
            print("対象銘柄が見つかりませんでした。")
            return
    else:
        wl = None  # None = WATCHLIST全体を使う

    # ── 実行 ────────────────────────────────────────────────────
    if args.history:
        today = datetime.now(JST).date()
        if args.from_date:
            from_dt = date.fromisoformat(args.from_date)
            to_dt   = date.fromisoformat(args.to_date) if args.to_date else today
        else:
            to_dt   = date.fromisoformat(args.to_date) if args.to_date else today
            from_dt = to_dt - timedelta(days=args.days)
        check_history(from_dt, to_dt, args.verbose, wl)
    else:
        check_today(args.verbose, wl)


if __name__ == "__main__":
    main()
