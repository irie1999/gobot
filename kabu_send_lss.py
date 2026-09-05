"""
kabu_send_lss.py — 今日の lss シグナルを「信用新規売りの逆指値」で発注する
============================================================================

lss(ロング銘柄ショート = 逆指値空売り)の当日シグナルを収集し、各シグナルの
注文価格(order_price = 前日終値ちょうど。em=0.0)で **信用新規売りの逆指値**
注文を kabuステーションに発注する。

  ロング(kabu_send_signals.py)   : 逆指値『買い』(高値≥注文価格で発動)
  lss  (このスクリプト)          : 逆指値『売り』(安値≤注文価格で発動 = 下落で約定)

lss は同日決済(max_hold=0)戦略なので、エントリー(この発注)が約定したら同日中に
買い戻して手仕舞う必要がある。買い戻し(引け成行 MOC)は建玉が立ってからでないと
出せないため、このスクリプトはまず **エントリー(新規売り)だけ** を出す。
決済(引け成行買戻し)は約定確認後に別途行う(§EXIT 参照)。

【安全設計】(kabu_send_signals.py と同じ)
  - デフォルト dry-run。--execute のときだけ実発注。
  - --execute でも接続先は既定デモ(18081)。本番(18080)は --prod を明示。
  - --symbol / --limit で銘柄数を絞れる(「一軒だけ」テスト発注に使う)。
  - -3% を超えて窓を開けて下落した場合は約定させない下限ガード(after_hit_price)。

使い方:
  # dry-run: 今日の lss シグアルを表示(発注しない)
  python kabu_send_lss.py

  # 一軒だけデモ口座に発注(まずこれで動作確認)
  python kabu_send_lss.py --limit 1 --execute

  # 銘柄を指定して一軒だけ
  python kabu_send_lss.py --symbol 7203 --execute

  # 本番口座に一軒だけ(要 --prod 明示)
  python kabu_send_lss.py --symbol 7203 --execute --prod

  # 銘柄リストを指定(既定は holdout_selected_symbols.py / lss 提案ファイル)
  python kabu_send_lss.py --symbols-file lss_watchlist_proposal_2026-07-15.py
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows の cp932 出力で非対応文字(⚠等)を出しても落ちないようにする。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# ── TRADING_MODE を import 前に設定(各スクリプトと同じ作法) ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import backtest_limit_entry as ble           # noqa: E402
from sameday5m_core import mod_for            # noqa: E402
from kabu_api import KabuClient, CASH_MARGIN_OPEN  # noqa: E402

JST = timezone(timedelta(hours=9))
FIXED_QTY = ble.FIXED_QTY            # 100株固定(バックテストと同じ前提)
ORDERED_LOG = "ordered_signals_lss.csv"

# lss 既定の損切/利確 ATR 倍率(scan_lss_universe.py と同じ既定)。
DEFAULT_SM = 0.1     # 損切: 注文価格の少し上
DEFAULT_TM = 1.0     # 利確: 注文価格から ATR×1.0 下
# lss の戦略は run_signals_holdout_all が集計する 6 戦略に合わせる。
LSS_STRATEGIES = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _jq_to_yf(code: str) -> str:
    """J-Quants 5桁 → yfinance形式。72030 → 7203.T / 既に .T ならそのまま。"""
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _load_symbols(symbols_file: str | None) -> list[tuple]:
    """(code, name, strategy) の一覧。--symbols-file → **holdout** → WATCHLIST。

    ⛔⛔ **`lss_watchlist_proposal_*.py`(旧命名)の自動検出をやめた** (2026-08-17)。

      それまでの優先順位は `--symbols-file → lss提案(glob) → holdout` で、
      **旧命名の提案ファイルが holdout より先**だった。フォルダに
      `lss_watchlist_proposal_2026-07-15.py`(5,639ペア)が残っていたため、
      `lss_budget_cap --execute` は **7月15日の古い提案から発注する**状態
      だった。レポートが毎回

          [export] holdout選定 3,025ペア → holdout_selected_symbols.py
                   ⚠ ライブの発注経路が読むファイルです

      と表示しているのに、実際には読んでいなかった = **発注リストと実発注が
      別の母集団**。2026-08-17 に k_open_confirm で同じ罠を踏んで発覚した。

    ★ 正本は `holdout_selected_symbols.py` ただ1つ。これはレポートが
      LSS_SIGNAL_POOL(選定あり)+価格+空売り可で絞って書き出したもので、
      **画面の発注リストと1対1で対応する**。
      別の母集団で出したい日だけ `--symbols-file` で明示する。
    """
    cand: list[str] = []
    if symbols_file:
        cand.append(symbols_file)
    cand.append("holdout_selected_symbols.py")
    for path in cand:
        p = Path(path)
        if not p.exists():
            continue
        ns: dict = {}
        try:
            exec(p.read_text(encoding="utf-8"), ns)
        except Exception as e:
            print(f"[warn] {path} 読み込み失敗: {e}", file=sys.stderr)
            continue
        sel = ns.get("SELECTED")
        if sel:
            out = [(c, n, s) for (c, n, s) in sel]
            # ★ 何を読んだかは毎回必ず出す。正本でなければ警告する。
            #   「発注リストと実発注が別の母集団」は画面では気づけないので、
            #   ここで言わないと分からない(2026-08-17 に実際そうなっていた)。
            _canon = "holdout_selected_symbols.py"
            if Path(path).name != _canon:
                _age = ""
                try:
                    import datetime as _dtm
                    _mt = _dtm.date.fromtimestamp(p.stat().st_mtime)
                    _age = f" / 更新 {_mt}"
                except Exception:
                    pass
                print(f"[info] {path} から {len(out)}ペア読み込み{_age}")
                print(f"  ⛔ **正本({_canon})ではありません**。"
                      f"レポートの発注リストと母集団が食い違います。"
                      f"意図した指定でなければ --symbols-file を外してください",
                      file=sys.stderr)
            else:
                print(f"[info] {path} から {len(out)}ペア読み込み"
                      f"(✅ レポートの発注リストと同じ母集団)")
            return out
    # フォールバック: WATCHLIST
    # ⛔ ここに落ちるのは holdout_selected_symbols.py が無いとき = レポートを
    #   一度も回していない日。組み込みWATCHLIST(265ペア)は**今日の選定ではない**。
    import check_signals_stop as _stop
    import check_signals_breakout as _brk
    out = list(_stop.WATCHLIST) + list(_brk.WATCHLIST)
    print(f"[info] WATCHLIST から {len(out)}ペア(フォールバック)")
    print(f"  ⛔ **holdout_selected_symbols.py がありません**。"
          f"組み込みWATCHLISTは今日の選定ではないので、"
          f"先に `.\\daily` を回してください", file=sys.stderr)
    return out


def _lss_signal_today(sym: str, name: str, strat: str,
                      sm: float, tm: float, days: int) -> dict | None:
    """当日引け後の lss シグナルを 1 件返す(無ければ None)。

    バックテストエンジン(run_limit_backtest, entry_type="stop_sell", max_hold=0)を
    直接回し、最新バー(=今日の終値)を signal_dt とする「発注中(未発動 pending)」
    注文を拾う。lp/sp/tp はバックテスト・レポートと完全に一致する。
    """
    mod = mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return None
    cf = params[0]                       # calc_fn(指標付与+entry_sig)
    yf_sym = _jq_to_yf(sym)
    try:
        df = ble.fetch(yf_sym, days + 420)   # スコア窓ぶん余分に取る
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        # em=0.0(前日終値ちょうど)/ 同日決済 max_hold=0 で lss を再現。
        r = ble.run_limit_backtest(yf_sym, name, df, cf, 0.0, sm, tm,
                                   days + 420, strat,
                                   entry_type="stop_sell", max_hold=0)
    except Exception:
        return None
    if not r:
        return None
    last_date = df.index[-1]
    last_d = last_date.date() if hasattr(last_date, "date") else last_date
    for t in r.get("trade_log", []):
        if t.get("reason") != "発注中":       # 未発動 pending のみ = 今日置く注文
            continue
        sdt = t.get("signal_dt")
        sd = sdt.date() if hasattr(sdt, "date") else sdt
        if sd != last_d:                      # 最新バー(今日)の新規シグナルだけ
            continue
        lp = float(t.get("order_limit", 0) or 0)   # 逆指値トリガー(=前日終値)
        sp = float(t.get("order_stop", 0) or 0)    # 損切(上側)
        tp = float(t.get("order_target", 0) or 0)  # 利確(下側)
        if lp <= 0 or sp <= 0 or tp <= 0:
            continue
        # kabu へ渡す銘柄コードは .T を外した数値コード(例 4911)。
        # yf_sym(4911.T)はバックテスト取得用で、発注時は数値コードでないと
        # 「銘柄が見つからない」(Code 4002001)になる。
        kabu_code = yf_sym.upper().removesuffix(".T").split(".")[0]
        # 平均日次売買代金(直近120日)。lss_budget_cap の流動性順発注で使う。
        # レポート(nikkei_analysis:2510)と同じ定義に揃えてある。
        try:
            import lss_order_rank as _lor
            _liq = _lor.daily_turnover(df)
        except Exception:
            _liq = 0.0
        # ATR(前日)。stop = order + atr*sm (ショート)なので逆算できる。
        # H案(寄指)は **実約定価格(寄り値)基準** で OCO を組み直すので、
        # watcher が再計算するために必要(現行の逆指値は注文価格基準なので不要)。
        _atr = (sp - lp) / sm if sm else 0.0
        return {
            "symbol": kabu_code, "name": name, "strategy": strat,
            "family": "lss",
            "order_price": lp, "stop_price": sp, "target_price": tp,
            "signal_date": str(sd), "liquidity": _liq,
            "atr": _atr, "sm": sm, "tm": tm,
        }
    return None


def _log_ordered(sig: dict, prod: bool, qty: int,
                 entry_mode: str = "stop", order_price: float | None = None,
                 status: str = "ordered") -> None:
    """lss の発注を ordered_signals_lss.csv に追記する。

    status:
      "pending" = **これから発注する**。発注の直前に書く。
      "ordered" = 発注が通った。
      "failed"  = 発注が通らなかった(同じ pending を打ち消す)。

    ⛔⛔ **記録は発注より先に書く**(2026-08-19)。
      旧実装は『発注が成功してから記録』だった。その間にプロセスが落ちると
      **注文だけが板に残り、watcher はそれを知らない**。今日の実損
      (無防備で引けまで + 強制決済手数料)と同じ状態になる。
      非対称なので順序は決まる:
        記録が無くて注文がある … **無防備**(実損)
        記録があって注文が無い … 無害(watcher が建玉を探して見つからないだけ)
      よって先に書く。失敗したら "failed" を追記して打ち消す。


    entry_mode:
      "stop"    = 現行の逆指値売り。OCO は **注文価格基準**(発注時に確定)。
      "auction" = H案の寄指売り。板寄せの約定値は指値以上になるので、OCO は
                  **実約定価格基準**で組み直す必要がある。そのため atr/sm/tm を
                  一緒に残し、lss_exit_watcher が建玉の平均約定値から再計算する。
    order_price: 実際に出した注文価格(寄指ならずらした後の指値)。省略時は sig の値。
    """
    import csv
    now = datetime.now(JST)
    _op = float(sig.get("order_price", 0) or 0) if order_price is None else float(order_price)
    row = {
        "record_date": now.strftime("%Y-%m-%d"),
        "ordered_at":  now.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":      str(sig.get("symbol", "")).upper().removesuffix(".T"),
        "name":        sig.get("name", ""),
        "strategy":    sig.get("strategy", ""),
        "family":      "lss",
        "side":        "short",
        "order_price": round(_op),
        "stop_price":  round(float(sig.get("stop_price", 0) or 0)),
        "target_price": round(float(sig.get("target_price", 0) or 0)),
        "qty":         qty,
        "prod":        int(bool(prod)),
        "cash_margin": CASH_MARGIN_OPEN,
        # ↓ 2026-08-10 追加。既存ファイルには無いので下でヘッダを合わせる。
        "entry_mode":  entry_mode,
        "atr":         round(float(sig.get("atr", 0) or 0), 2),
        "sm":          float(sig.get("sm", 0) or 0),
        "tm":          float(sig.get("tm", 0) or 0),
        # ↓ 2026-08-19 追加。既存ファイルには無い = 空欄は "ordered" 扱い(後方互換)。
        "status":      str(status or "ordered"),
    }
    p = Path(ORDERED_LOG)
    try:
        _ensure_header(p, list(row.keys()))
        with open(p, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=list(row.keys())).writerow(row)
    except Exception as e:
        print(f"  ⚠ 発注記録の書き込み失敗 ({e})")


def _ensure_header(p: Path, cols: list[str]) -> None:
    """CSV のヘッダを cols に揃える。列が増えたら既存行を保ったまま書き直す。

    ⛔ DictWriter に新しい列を渡すと、ヘッダが古いままの既存ファイルでは
       行の列数がズレる(あるいは ValueError)。列を増やしたときは必ずここを通す。
       既存行の新列は空欄になる = 読む側は「値なし」として扱えばよい。
    """
    import csv
    if not p.exists():
        with open(p, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=cols).writeheader()
        return
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        old = list(rows[0].keys()) if rows else None
    if old is None:
        with open(p, newline="", encoding="utf-8") as f:
            old = next(csv.reader(f), [])
    if list(old) == cols:
        return
    missing = [c for c in cols if c not in old]
    if not missing:
        return          # 列が減るケースは触らない(既存の並びを尊重)
    bak = p.with_suffix(p.suffix + ".bak")
    try:
        bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print(f"  ℹ {p.name} に列を追加しました: {missing} (旧ファイルは {bak.name})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="今日の lss シグナルを信用新規売りの逆指値で発注する")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--symbol", default=None,
                    help="この銘柄コードだけ発注 (例: 7203)。指定時は他を無視")
    ap.add_argument("--strat", default=None,
                    help="--symbol と併用: 戦略を固定 (例: RSI2)")
    ap.add_argument("--limit", type=int, default=None,
                    help="発注する最大件数 (「一軒だけ」なら --limit 1)")
    ap.add_argument("--symbols-file", default=None,
                    help="(code,name,strategy) の SELECTED を持つ .py")
    ap.add_argument("--budget", type=float, default=None,
                    help="1銘柄あたりの投入上限(円)。order_price×100 超の銘柄はスキップ")
    ap.add_argument("--qty", type=int, default=FIXED_QTY,
                    help=f"株数 (既定 {FIXED_QTY})")
    ap.add_argument("--sm", type=float, default=DEFAULT_SM,
                    help=f"損切ATR倍率 (既定 {DEFAULT_SM})")
    ap.add_argument("--tm", type=float, default=DEFAULT_TM,
                    help=f"利確ATR倍率 (既定 {DEFAULT_TM})")
    ap.add_argument("--days", type=int, default=60,
                    help="シグナル判定に使う日足ルックバック日数 (既定 60)")
    ap.add_argument("--gap-guard", type=float,
                    default=getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.02),
                    help="逆指値発動後の下限ガード率。-この%%超の窓開けは約定させない "
                         "(既定=バックテストのガード _INTRADAY_5M_ENTRY_GAP_LIMIT と同値=2%%)")
    ap.add_argument("--no-gap-guard", action="store_true",
                    help="下限ガードを外す (発動後は成行)")
    ap.add_argument("--margin-type", type=int, default=3,
                    help="信用取引区分 1=制度 / 2=一般(長期) / 3=一般(デイトレ)。"
                         "lssは同日決済+非貸借銘柄も売るため既定3(デイトレ)")
    ap.add_argument("--aggressive", action="store_true", help="aggressive モード")
    ap.add_argument("--conservative", action="store_true", help="conservative モード (既定)")
    args = ap.parse_args()

    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"

    now = datetime.now(JST)
    print("=" * 64)
    print(f"lss(逆指値空売り)発注  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label}  /  接続先: {env_label}  /  信用新規売り")
    print(f"sm={args.sm} / tm={args.tm} / qty={args.qty} / ルックバック{args.days}日")
    print("=" * 64)

    # ── 対象銘柄 ──
    if args.symbol:
        strat = args.strat or "RSI2"
        pairs = [(args.symbol, args.symbol, strat)]
        print(f"[info] --symbol 指定: {args.symbol} / 戦略 {strat}")
    else:
        pairs = _load_symbols(args.symbols_file)

    print("本日の lss シグナルを収集中...")
    signals: list[dict] = []
    for (code, name, strat) in pairs:
        sig = _lss_signal_today(code, name, strat, args.sm, args.tm, args.days)
        if sig:
            signals.append(sig)

    if not signals:
        print("本日の lss シグナルなし。終了します。")
        return 0

    # 予算フィルター
    if args.budget:
        kept = []
        for s in signals:
            cost = s["order_price"] * args.qty
            if cost > args.budget:
                print(f"  skip {s['symbol']} {s['name']}: "
                      f"必要資金 {cost:,.0f}円 > 予算 {args.budget:,.0f}円")
                continue
            kept.append(s)
        signals = kept
    if not signals:
        print("予算条件を満たすシグナルなし。終了します。")
        return 0

    # 件数制限(「一軒だけ」)
    if args.limit is not None and args.limit >= 0:
        if len(signals) > args.limit:
            print(f"[info] {len(signals)}件中 先頭 {args.limit}件だけ発注します。")
        signals = signals[:args.limit]

    print(f"発注対象シグナル: {len(signals)} 件\n")

    # lss は一般信用デイトレ(3)で売る。制度信用(1)の空売りは貸借銘柄限定で、
    # 非貸借銘柄(例4662)は MarginTradeType不正で弾かれるため。--margin-type で上書き可。
    cli = KabuClient(prod=args.prod, dry_run=not args.execute,
                     margin_type=args.margin_type)
    if args.execute:
        try:
            cli.connect()
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            return 1

    ok = 0
    for s in signals:
        sym, name = str(s["symbol"]).split(".")[0], s["name"]   # kabuは数値コード(.T無し)
        order_p, stop_p, tgt_p = s["order_price"], s["stop_price"], s["target_price"]
        # トリガーは呼値(ティック)に丸めた終値。kabu は無効な呼値を『下方向』に floor する
        # (例: 端数の3,024を送ると3,020に丸められる)ため、必ず最寄りの有効呼値に丸める
        # (資生堂4911の終値3,024.x → round_to_tick 3,025 = 実際の終値)。
        from backtest_limit_entry import tick_size, round_to_tick
        trig = round_to_tick(order_p)
        adj_note = ""
        # 【即約定回避・必須】逆指値売りはトリガーが現在値以上だと「即座に市場に発注」で
        # 弾かれる(kabu Code 100217)。引け後は現在値=前日終値なので、終値ちょうどの
        # トリガーは必ず弾かれる → 現在値-1ティックに引き下げる。現在値が取れない場合も
        # 弾き回避のため1ティック下げる(引け後は現値=終値のため)。
        if args.execute:
            try:
                cur = cli.get_current_price(sym)
            except Exception:
                cur = None
            if cur and cur > 0:
                if trig >= cur:
                    trig = round_to_tick(cur - tick_size(cur))
                    adj_note = f" ※現値{cur:,.0f}≧トリガー→{trig:,.0f}に引下げ"
            else:
                trig = round_to_tick(trig - tick_size(trig))
                adj_note = f" ※現値不明→{trig:,.0f}に1ティック引下げ"
        # 発動後の指値下限(-3%)も呼値に丸める(端数だと kabu が下方向に丸めるため)。
        after = None if args.no_gap_guard else round_to_tick(trig * (1.0 - args.gap_guard))
        guard_label = "成行" if after is None else f"下限¥{after:,.0f}(-{args.gap_guard*100:.0f}%)"
        print(f"・{sym} {name} [{s['strategy']}] 新規売り逆指値 "
              f"@≤{trig:,.0f}{adj_note}  損切¥{stop_p:,.0f} / 利確¥{tgt_p:,.0f} / 発動後{guard_label}")
        res = cli.send_stop_sell(sym, qty=args.qty, trigger_price=trig,
                                 cash_margin=CASH_MARGIN_OPEN,
                                 after_hit_price=after)
        if res.get("Result") == 0:
            ok += 1
            if args.execute:
                _log_ordered(s, args.prod, args.qty)

    print(f"\nエントリー(新規売り)発注完了: {ok}/{len(signals)} 件成功")
    if not args.execute:
        print("dry-run のため実発注していません。--execute で発注します。")
    print("─" * 64)
    print("【EXIT 注意】lss は同日決済です。エントリーが約定したら、同日中に")
    print("  引け成行で買い戻して手仕舞ってください:")
    print("    cli.send_moc(<symbol>, qty, side='buy', cash_margin=CASH_MARGIN_CLOSE)")
    print("  ※ 買戻しは建玉が立ってからでないと出せないため、約定確認後に実行。")
    print("  ※ 別途 close_stop_guard 相当の引け決済スクリプトを用意予定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
