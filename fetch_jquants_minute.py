"""
fetch_jquants_minute.py ― J-Quants 5分足を data/ に取り込む(デイトレが読む場所へ)
================================================================================
jquants_fetch.fetch_intraday はデータを ~/.jquants_cache/ に保存するが、デイトレ本体
(daytrade_data)は data/minute_5m / data/quarantine_5m を読む。この橋渡しが無かったため、
本スクリプトで『J-Quants 5分足を取得 → data/ に正しい形式で保存』する。

保存先(既定):
  data/quarantine_5m/{jqコード}.pkl   ← 長期(2年)5分足。デイトレWF選定が読む場所

【セットアップ】(初回のみ)
  1) pip install jquants-api-client
  2) .env.example を .env にコピーし JQUANTS_API_KEY=<Dashboard>API Key> を記入
  3) 認証テスト:  python jquants_client.py auth

【使い方】
  # まず少数で動作確認(推奨)
  python fetch_jquants_minute.py --limit 3 --days 730
  # プライム全銘柄を2年分(全量)
  python fetch_jquants_minute.py --all --days 730 --sleep 1.1
  # 明示指定
  python fetch_jquants_minute.py 6136.T 4631.T --days 730
  # スイングWATCHLISTの不足分だけ(2年)
  python fetch_jquants_minute.py --swing-watchlist --days 730

  既存pklはスキップ(再開可)。--force で上書き。--sleep でレート制限調整
  (Lightは60件/分=--sleep 1.1 目安, Standardは120件/分=--sleep 0.55)。
"""
from __future__ import annotations

import argparse
import importlib.util
import pickle
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DATA_MIN = _HERE / "data" / "minute_5m"
DATA_QUAR = _HERE / "data" / "quarantine_5m"


def _load_symbols_file(path: Path) -> list[str]:
    """SYMBOLS = [(code,name),...] or [code,...] の .py を読んで yfコード配列を返す。"""
    try:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[WARN] {path} 読込失敗: {e}")
        return []
    for attr in ("SYMBOLS", "symbols"):
        lst = getattr(mod, attr, None)
        if lst:
            out = []
            for x in lst:
                code = x[0] if isinstance(x, (tuple, list)) else x
                code = str(code)
                out.append(code if code.endswith(".T") else f"{code}.T")
            return out
    return []


def _auto_universe() -> tuple[list[str], str]:
    for name in ("symbols_listed_prime.py", "symbols_listed_all.py",
                 "symbols_listed_standard.py", "symbols_all.py"):
        p = _HERE / name
        if p.exists():
            syms = _load_symbols_file(p)
            if syms:
                return syms, name
    return [], ""


def _swing_watchlist() -> list[str]:
    import importlib
    out = set()
    for name in ("check_signals_stop", "check_signals_breakout",
                 "check_signals_short", "check_signals_short_breakout"):
        try:
            m = importlib.import_module(name)
            for s, n, st in getattr(m, "WATCHLIST", []):
                out.add(s if str(s).endswith(".T") else f"{s}.T")
        except Exception:
            pass
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="銘柄コード(例 6136.T)。省略時は --all/--swing-watchlist")
    ap.add_argument("--all", action="store_true", help="symbols_listed_*.py の全銘柄")
    ap.add_argument("--swing-watchlist", action="store_true", help="スイングWATCHLIST")
    ap.add_argument("--symbols-file", default=None, help="銘柄リストの.py")
    ap.add_argument("--days", type=int, default=730, help="取得日数(5分足は2年=730が上限)")
    ap.add_argument("--dir", choices=["quarantine", "minute"], default="quarantine",
                    help="保存先。quarantine=長期(既定)/minute=直近")
    ap.add_argument("--sleep", type=float, default=1.1, help="1銘柄ごとの待機秒(レート制限)")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(動作確認)")
    ap.add_argument("--force", action="store_true", help="既存pklも再取得")
    args = ap.parse_args()

    # jquants 取得関数(認証は .env の JQUANTS_API_KEY を自動読込)
    try:
        from jquants_fetch import fetch_intraday, yf_to_jquants
    except Exception as e:
        print(f"[ERROR] jquants_fetch を読み込めません: {e}\n"
              "  pip install jquants-api-client / .env の JQUANTS_API_KEY を確認", file=sys.stderr)
        sys.exit(1)

    # ユニバース決定
    if args.symbols:
        syms = [s if s.endswith(".T") else f"{s}.T" for s in args.symbols]
        src = "明示指定"
    elif args.swing_watchlist:
        syms, src = _swing_watchlist(), "スイングWATCHLIST"
    elif args.symbols_file:
        syms, src = _load_symbols_file(Path(args.symbols_file)), args.symbols_file
    elif args.all:
        syms, src = _auto_universe()
    else:
        print("銘柄指定がありません。--all / --swing-watchlist / --symbols-file / 明示指定 のいずれかを。")
        sys.exit(1)
    if not syms:
        print("対象銘柄が0件です(ユニバースファイルを確認)。")
        sys.exit(1)
    if args.limit and args.limit > 0:
        syms = syms[:args.limit]

    out_dir = DATA_QUAR if args.dir == "quarantine" else DATA_MIN
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"J-Quants 5分足 取得 → {out_dir}")
    print(f"対象: {src} {len(syms)}銘柄 / {args.days}日 / sleep={args.sleep}s / "
          f"{'上書き' if args.force else '既存はスキップ(再開可)'}")
    print("=" * 70)

    ok = skip = fail = 0
    for i, sym in enumerate(syms, 1):
        jq = yf_to_jquants(sym)
        out = out_dir / f"{jq}.pkl"
        if out.exists() and not args.force:
            skip += 1
            continue
        try:
            df = fetch_intraday(sym, days=args.days, interval="5m")
        except Exception as e:
            print(f"  [{i}/{len(syms)}] {sym} 取得失敗: {e}", flush=True)
            fail += 1
            time.sleep(args.sleep)
            continue
        if df is None or getattr(df, "empty", True):
            print(f"  [{i}/{len(syms)}] {sym} データ無し", flush=True)
            fail += 1
        else:
            try:
                out.write_bytes(pickle.dumps(df))
                d0 = df.index[0].date(); d1 = df.index[-1].date()
                ok += 1
                if i % 20 == 0 or i <= 5:
                    print(f"  [{i}/{len(syms)}] {sym}→{jq}.pkl  {len(df):,}本  {d0}〜{d1}", flush=True)
            except Exception as e:
                print(f"  [{i}/{len(syms)}] {sym} 保存失敗: {e}", flush=True)
                fail += 1
        time.sleep(args.sleep)     # レート制限(429回避)

    print(f"\n完了: 取得{ok} / スキップ{skip} / 失敗{fail}  → {out_dir}")
    print("次: python find_minute_data.py でカバレッジ確認 → "
          "scan_wf_strategies / build_watchlist_wf でデイトレ選定を回す")


if __name__ == "__main__":
    main()
