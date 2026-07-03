"""
analyze_open_confirm_entry.py ― 「寄り30分確認」エントリーの5分足検証 (スイング)
================================================================================
逆指値ブレイク買い(現行=A)と「寄り付きN分後に前日終値=トリガを超えていたら買う」
(確認エントリー=B) を、実5分足で比較する。

■ 検証の勘所（なぜ日足バックテストを再利用できるか）
  損切り/目標は『シグナル由来の絶対価格』なので、エントリー価格を変えても
  「いつ・どの価格で決済するか」は変わらない。つまり5分足が必要なのは
  『各トレードのエントリー日の 09:00+N分 時点の株価』を知るためだけ。
  保有〜決済は既存の run_limit_backtest の結果(exit_p/reason)をそのまま使う。

■ A(現行 逆指値) と B(確認) の違い
  A: 高値>=トリガで約定。約定価格 entry_p は trade_log の値(ギャップ/スリッページ込み)。
  B: 約定日の 09:30 時点株価 P を見る。
       P >  トリガ → 買う。約定価格 = P*(1+スリッページ)。決済は A と同一。
       P <= トリガ → 当日は見送り(=このトレードをスキップ)。
     → 寄りのスパイクで即反落したダマシ(9:30にはトリガ割れ)を除外できる。
     → 代償: 本物のブレイクは 9:30 の高い株価で買うため建値が不利になりうる。

■ 5分足データ
  1) --minute-dir DIR を指定すると DIR/{code}.csv (datetime,open,high,low,close) を読む
     (お手元のデイトレ5分足を流用したい場合に使用。長期履歴が使える)
  2) 無指定なら yfinance で 5分足を取得 (interval=5m は約60日しか遡れない点に注意)

■ 最適な確認時刻を探す
  「9:30固定」ではなく、寄りから何分後に確認するのが最良かを複数時刻でスイープし、
  A(現行)比で損益が最大の時刻を提示する。--sweep で候補時刻(分)を指定。

使い方:
  python analyze_open_confirm_entry.py                       # 既定スイープ(0,15,30,...,240分)
  python analyze_open_confirm_entry.py --sweep 15,30,60,90   # 候補時刻を絞る
  python analyze_open_confirm_entry.py --confirm-min 30      # 単一時刻だけ詳細評価
  python analyze_open_confirm_entry.py --margin-pct 0.3      # トリガ+0.3%超で確認(ダマシ厳格化)
  python analyze_open_confirm_entry.py --minute-dir data/minute_5m   # デイトレ5分足を流用
  python analyze_open_confirm_entry.py --aggressive
  python analyze_open_confirm_entry.py --days 60             # 直近何日のトレードを対象にするか

注意: この環境では yfinance 未導入・5分足データ無しのため、ローカルでの実行を想定。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --aggressive を import 前に反映
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
os.environ.setdefault("TRADING_MODE", "conservative")

import pandas as pd

import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import (
    run_limit_backtest, fetch, tick_size,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, FIXED_QTY,
)

JST = timezone(timedelta(hours=9))


def _round_tick_up(p: float) -> int:
    t = tick_size(float(p))
    import math
    return int(math.ceil(p / t)) * t


# ── 5分足ローダー ────────────────────────────────────────────────────────────
_MIN_CACHE: dict[str, pd.DataFrame | None] = {}
_HERE = Path(__file__).resolve().parent
_PKL_CACHE_FILE = _HERE / ".minute_data_dirs.txt"   # 発見済みディレクトリを記憶
_PKL_DIRS_MEMO: list | None = None


def locate_5m_dirs(force: bool = False, verbose: bool = False) -> list:
    """デイトレ5分足pklのディレクトリ(minute_5m / quarantine_5m)を広く自動探索。
    発見結果は .minute_data_dirs.txt に記憶し、次回以降は即返す(場所を再質問しない)。"""
    global _PKL_DIRS_MEMO
    if _PKL_DIRS_MEMO is not None and not force:
        return _PKL_DIRS_MEMO
    found, seen = [], set()

    def _add(d):
        try:
            d = Path(d)
            if d.is_dir() and any(d.glob("*.pkl")):
                key = str(d.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append(d)
        except Exception:
            pass

    # 1) 記憶済みキャッシュ
    if _PKL_CACHE_FILE.exists() and not force:
        for line in _PKL_CACHE_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                _add(line.strip())
    # 2) スクリプト位置 / CWD の祖先を数階層たどる (swingtrade→kabu station→…)
    ancestors = []
    for b in (_HERE, Path.cwd()):
        p = b
        for _ in range(6):
            if p not in ancestors:
                ancestors.append(p)
            if p.parent == p:
                break
            p = p.parent
    try:
        ancestors.append(Path.home())
    except Exception:
        pass
    for a in ancestors:
        # 直下の data/
        _add(a / "data" / "minute_5m")
        _add(a / "data" / "quarantine_5m")
        # 祖先の『子フォルダ』の data/ (= 姉妹フォルダ。例 swingtrade の隣の daytrading)
        try:
            for name in ("minute_5m", "quarantine_5m"):
                for d in a.glob(f"*/data/{name}"):
                    _add(d)
        except Exception:
            pass
    # 3) それでも無ければ home 配下を浅くグロブ (3階層まで)
    if not found:
        try:
            home = Path.home()
            for pat in ("*/data/quarantine_5m", "*/*/data/quarantine_5m",
                        "*/*/*/data/quarantine_5m", "*/*/*/*/data/quarantine_5m",
                        "*/data/minute_5m", "*/*/data/minute_5m",
                        "*/*/*/data/minute_5m", "*/*/*/*/data/minute_5m"):
                for d in home.glob(pat):
                    _add(d)
        except Exception:
            pass
    # 記憶
    if found:
        try:
            _PKL_CACHE_FILE.write_text(
                "\n".join(str(d.resolve()) for d in found), encoding="utf-8")
        except Exception:
            pass
    if verbose:
        if found:
            for d in found:
                print(f"  ✓ {d}  ({len(list(d.glob('*.pkl')))} pkl)")
        else:
            print("  ✗ 5分足pklディレクトリが見つかりませんでした")
    _PKL_DIRS_MEMO = found
    return found


def _pkl_dirs() -> list:
    return locate_5m_dirs()


def _yf_to_jq(symbol: str) -> str:
    """7203.T → 72030 (デイトレpklのファイル名は5桁jquantsコード)。"""
    code = symbol.strip().upper().replace(".T", "")
    return code + "0" if (len(code) == 4 and code.isdigit()) else code


def _jq_to_yf(jq_code: str) -> str:
    """72030 → 7203.T (pklファイル名 → yfinanceコード)。"""
    code = str(jq_code).strip()
    if len(code) == 5 and code.isdigit():
        return code[:4] + ".T"
    return code if code.endswith(".T") else code + ".T"


def universe_from_pkl() -> list[str]:
    """5分足pklが存在する全銘柄(yfinanceコード)を列挙。= 5分足で検証可能な全銘柄。"""
    syms = set()
    for d in _pkl_dirs():
        try:
            for p in Path(d).glob("*.pkl"):
                yf = _jq_to_yf(p.stem)
                # 4桁+.T の普通株のみ (ETF/指数等の5桁以上は除外)
                base = yf.replace(".T", "")
                if len(base) == 4 and base.isdigit():
                    syms.add(yf)
        except Exception:
            pass
    return sorted(syms)


def _normalize_min(raw: pd.DataFrame) -> pd.DataFrame | None:
    """デイトレpkl等の分足を open/high/low/close・naive JST index に正規化。"""
    if raw is None or getattr(raw, "empty", True):
        return None
    df = raw.copy()
    if "DateTime" in df.columns:
        dt = pd.to_datetime(df["DateTime"], errors="coerce")
    elif "Date" in df.columns and "Time" in df.columns:
        dp = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        dt = pd.to_datetime(dp + " " + df["Time"].astype(str).str.strip(), errors="coerce")
    elif "datetime" in df.columns:
        dt = pd.to_datetime(df["datetime"], errors="coerce")
    elif isinstance(df.index, pd.DatetimeIndex):
        dt = df.index
    else:
        return None
    ren = {"O": "Open", "H": "High", "L": "Low", "C": "Close"}
    df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
    cols = None
    # 日足トリガは auto_adjust=False(=生値)。5分足も生値を優先し、スケールを一致させる。
    # Adjustment*(調整値)を先に選ぶと quarantine(jquants)領域で日足と桁ズレし建値差が
    # 異常値になる(過去バグ)。生値(open/Open)が無い時のみ調整値にフォールバック。
    for c in [("open", "high", "low", "close"), ("Open", "High", "Low", "Close"),
              ("AdjustmentOpen", "AdjustmentHigh", "AdjustmentLow", "AdjustmentClose")]:
        if all(x in df.columns for x in c):
            cols = c
            break
    if cols is None:
        return None
    out = pd.DataFrame({
        "open":  pd.to_numeric(df[cols[0]], errors="coerce").values,
        "high":  pd.to_numeric(df[cols[1]], errors="coerce").values,
        "low":   pd.to_numeric(df[cols[2]], errors="coerce").values,
        "close": pd.to_numeric(df[cols[3]], errors="coerce").values,
    }, index=dt.values if hasattr(dt, "values") else dt)
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
    out.index = idx
    return out.dropna(subset=["close"]).sort_index()


def _load_local_pkl(symbol: str, extra_dirs=()) -> pd.DataFrame | None:
    """data/minute_5m + quarantine_5m (+ extra_dirs) の pkl を読み・マージして返す。"""
    jq = _yf_to_jq(symbol)
    frames = []
    for d in list(extra_dirs) + list(_pkl_dirs()):
        p = Path(d) / f"{jq}.pkl"
        if p.exists():
            try:
                n = _normalize_min(pd.read_pickle(p))
                if n is not None and not n.empty:
                    frames.append(n)
            except Exception:
                pass
    if not frames:
        return None
    m = pd.concat(frames)
    return m[~m.index.duplicated(keep="last")].sort_index()


def _load_minute(symbol: str, minute_dir: str | None) -> pd.DataFrame | None:
    """5分足を JST index の DataFrame(open/high/low/close) で返す。失敗時 None。
    優先: 明示CSV(--minute-dir) → デイトレpkl(data/minute_5m自動) → yfinance。"""
    if symbol in _MIN_CACHE:
        return _MIN_CACHE[symbol]
    df = None
    code = symbol.replace(".T", "")
    # 1) ローカル CSV (--minute-dir 明示時)
    if minute_dir:
        for cand in (f"{code}.csv", f"{symbol}.csv", f"{code}.T.csv"):
            p = Path(minute_dir) / cand
            if p.exists():
                try:
                    d = pd.read_csv(p)
                    tcol = next((c for c in d.columns
                                 if c.lower() in ("datetime", "date", "timestamp", "time")), None)
                    if tcol is None:
                        break
                    d[tcol] = pd.to_datetime(d[tcol])
                    d = d.set_index(tcol)
                    d.columns = [c.lower() for c in d.columns]
                    df = d[["open", "high", "low", "close"]].copy()
                except Exception as e:
                    print(f"  ⚠ {symbol}: CSV読込失敗 {e}")
                break
    # 2) pkl: --minute-dir 指定ディレクトリ + data/minute_5m + quarantine_5m を自動検出
    if df is None:
        df = _load_local_pkl(symbol, extra_dirs=((minute_dir,) if minute_dir else ()))
    # 3) yfinance フォールバック
    if df is None:
        try:
            import yfinance as yf
            raw = yf.download(symbol, interval="5m", period="60d",
                              progress=False, auto_adjust=False)
            if raw is not None and not raw.empty:
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = [c[0] for c in raw.columns]
                raw.columns = [c.lower() for c in raw.columns]
                df = raw[["open", "high", "low", "close"]].copy()
        except Exception as e:
            print(f"  ⚠ {symbol}: yfinance 5分足取得失敗 {e}")
            df = None
    # index を JST に統一
    if df is not None and not df.empty:
        idx = pd.DatetimeIndex(df.index)
        try:
            idx = idx.tz_convert(JST) if idx.tz is not None else idx.tz_localize("Asia/Tokyo")
        except Exception:
            try:
                idx = idx.tz_localize("UTC").tz_convert(JST)
            except Exception:
                pass
        df.index = idx
    _MIN_CACHE[symbol] = df
    return df


def _price_at(symbol: str, day, confirm_min: int, minute_dir: str | None):
    """entry_dt(日付)の 09:00+confirm_min 時点の株価。取れなければ None。"""
    df = _load_minute(symbol, minute_dir)
    if df is None or df.empty:
        return None
    d = day.date() if hasattr(day, "date") else day
    try:
        sub = df[(df.index.date == d)]
    except Exception:
        return None
    if sub.empty:
        return None
    target_t = (datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST)
                + timedelta(minutes=confirm_min))
    # target 時刻以降で最初のバーの open を「その時点の株価」とみなす
    after = sub[sub.index >= target_t]
    if not after.empty:
        return float(after.iloc[0]["open"])
    # target 時刻のバーが無ければ当日最後のバーの close
    return float(sub.iloc[-1]["close"])


# ── 対象トレード収集 ─────────────────────────────────────────────────────────
def _one_symbol_trades(mod, sym, name, strat, cutoff):
    """1 (銘柄,戦略) のバックテスト → 直近cutoff以降の決済済みロング取引リスト。"""
    try:
        bt = mod.backtest_one(sym, name, strat)
    except Exception:
        return []
    if not bt:
        return []
    pr = bt.get("period_results") or {}
    if not pr:
        return []
    try:
        rec_score, _ = mod.calc_recommend_score(pr)
    except Exception:
        rec_score = 0
    maxp = max(pr.keys())
    res = []
    for t in pr[maxp].get("trade_log", []):
        if t.get("reason") in ("発注中", "保有中"):
            continue
        edt = t.get("entry_dt")
        if edt is None:
            continue
        edate = edt.date() if hasattr(edt, "date") else edt
        if edate < cutoff:
            continue
        res.append(dict(
            symbol=sym, name=name, strategy=strat,
            entry_dt=edt, entry_p=t["entry_p"], exit_p=t["exit_p"],
            trigger=t.get("order_limit"), qty=t.get("qty", FIXED_QTY),
            reason=t.get("reason", ""), pnl_a=t["pnl"], rec_score=rec_score,
        ))
    return res


def _collect_trades(days: int, universe: list[str] | None = None,
                    workers: int = 1):
    """決済済みロング取引を収集し BTスコア(rec_score)を付与する。
    universe=None → スイングWATCHLIST。
    universe=銘柄リスト → その全銘柄 × 全戦略(逆指値系+ブレイク系)を対象。"""
    cutoff = datetime.now(JST).date() - timedelta(days=days)
    # (mod, sym, name, strat) ペアを構築
    pairs = []
    if universe is None:
        for mod in (_stop, _brk):
            for sym, name, strat in getattr(mod, "WATCHLIST", []):
                pairs.append((mod, sym, sym, strat))
    else:
        for sym in universe:
            for strat in getattr(_stop, "STRATEGY_PARAMS", {}):
                pairs.append((_stop, sym, sym, strat))
            for strat in getattr(_brk, "STRATEGY_PARAMS", {}):
                pairs.append((_brk, sym, sym, strat))

    out = []
    if workers and workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one_symbol_trades, m, s, n, st, cutoff)
                    for (m, s, n, st) in pairs]
            done = 0
            for fu in as_completed(futs):
                out.extend(fu.result() or [])
                done += 1
                if done % 200 == 0:
                    print(f"  ...収集 {done}/{len(pairs)} 済", flush=True)
    else:
        for (m, s, n, st) in pairs:
            out.extend(_one_symbol_trades(m, s, n, st, cutoff) or [])
    return out


def _pnl_long(entry_p: float, exit_p: float, qty: int) -> float:
    fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
    return (exit_p - entry_p) * qty - fee


def _stat(lst, key):
    n = len(lst)
    if n == 0:
        return (0, 0.0, 0.0)
    w = sum(1 for x in lst if (x[key] or 0) > 0)
    pnl = sum((x[key] or 0) for x in lst)
    return (n, w / n * 100, pnl)


def _eval_time(rows, confirm_min, margin_pct):
    """指定した確認時刻での B 成績を算出。rows は price_map に各時刻の株価を持つ。"""
    taken, skipped, both_prem = [], [], []
    for r in rows:
        p = r["price_map"].get(confirm_min)
        if p is None:
            continue
        thr = r["trigger"] * (1.0 + margin_pct / 100.0)
        if p > thr:
            entry_b = _round_tick_up(p * (1.0 + SLIPPAGE_STOP_PCT))
            pnl_b = _pnl_long(entry_b, r["exit_p"], r["qty"])
            taken.append({**r, "pnl_b": pnl_b})
            both_prem.append(entry_b - r["entry_p"])
        else:
            skipped.append(r)
    bn, bwr, bpnl = _stat(taken, "pnl_b")
    sn, swr, spnl = _stat(skipped, "pnl_a")
    prem = (sum(both_prem) / len(both_prem)) if both_prem else 0.0
    return dict(confirm_min=confirm_min, taken=taken, skipped=skipped,
                bn=bn, bwr=bwr, bpnl=bpnl, sn=sn, swr=swr, spnl=spnl, prem=prem)


def _fmt_hhmm(cm: int) -> str:
    return f"09:{cm:02d}" if cm < 60 else f"{9 + cm // 60}:{cm % 60:02d}"


def _normalize_report_trades(rep_trades) -> list[dict]:
    """レポートの取引dict(base+extra: 全設定・全銘柄・重複除外済)を
    寄り確認の内部形式に変換。決済済みロングのみ(保有中/発注中は除外)。"""
    out = []
    for t in rep_trades or []:
        if t.get("reason") in ("発注中", "保有中", None):
            continue
        xp = t.get("exit_p")
        trig = t.get("order_limit") or t.get("trigger")
        edt = t.get("entry_d_raw") or t.get("entry_dt")
        if not xp or not trig or edt is None:
            continue
        out.append(dict(
            symbol=t.get("symbol", ""), name=t.get("name", ""),
            strategy=t.get("strategy", ""), entry_dt=edt,
            entry_p=t.get("entry_p", 0), exit_p=xp, trigger=trig,
            qty=t.get("qty") or FIXED_QTY, reason=t.get("reason", ""),
            pnl_a=t.get("pnl", t.get("pnl_a", 0)),
            rec_score=t.get("rec_score", 0),
        ))
    return out


def build_html(minute_dir: str | None = None, times=None,
               margin_pct: float = 0.0, days: int = 400,
               is_short: bool = False, trades=None) -> str:
    """レポート詳細タブ用HTML。寄り後確認エントリー(A vs B)の時刻スイープ。
    trades 指定時はそのレポート取引セット(全設定・全銘柄)を使う(=損益タブと一致)。"""
    if is_short:
        return ('<p style="color:#94a3b8;padding:16px">'
                '寄り確認エントリー分析はロング(逆指値ブレイク買い)専用です。'
                'ショートは対象外。</p>')
    if times is None:
        times = [0, 15, 30, 45, 60, 90, 120, 150, 180, 240]

    if trades is not None:
        trades = _normalize_report_trades(trades)   # 損益タブと同じ取引セット
    else:
        trades = _collect_trades(days)
    rows, no_data = [], 0
    for t in trades:
        trig = t["trigger"]
        if not trig or trig <= 0:
            continue
        pm = {}
        for cm in times:
            p = _price_at(t["symbol"], t["entry_dt"], cm, minute_dir)
            # 安全ガード: 5分足価格がトリガ(前日終値)から±35%超はスケール不一致/
            # データ破損の疑い → 採用しない(建値差の異常値混入を防ぐ)
            if p is not None and 0.65 * trig <= p <= 1.35 * trig:
                pm[cm] = p
        if not pm:
            no_data += 1
            continue
        rows.append(dict(**t, price_map=pm))

    if minute_dir:
        src = f"CSV:{minute_dir}"
    elif _pkl_dirs():
        src = "デイトレ5分足(data/minute_5m+quarantine_5m)"
    else:
        src = "yfinance(約60日)"
    if not rows:
        return (f'<h2 style="margin-top:8px">⑭ 寄り後確認エントリー検証</h2>'
                f'<p style="color:#f87171;padding:12px">5分足データが取得できませんでした '
                f'(source={src})。<br>ローカルで <code>--minute-dir</code> にデイトレ5分足を'
                f'指定するか、yfinance導入・直近60日での実行をお試しください。</p>')

    # 一意キーを付与 (戦略別テーブルの取引照合用。id()はコピーで壊れるため)
    for _i, r in enumerate(rows):
        r["_key"] = _i

    def _c(v):
        return "#4ade80" if v >= 0 else "#f87171"

    # ── 5分足データ検証(診断) ──────────────────────────────────────────────
    diag = []
    syms = sorted({r["symbol"] for r in rows})
    for sym in syms[:200]:
        d = _MIN_CACHE.get(sym)
        if d is None or getattr(d, "empty", True):
            continue
        try:
            days_span = f"{d.index.min().date()}〜{d.index.max().date()}"
            per_day = d.groupby(d.index.date).size()
            diag.append((sym, len(d), days_span, int(per_day.median())))
        except Exception:
            pass
    diag_rows = "".join(
        f'<tr><td style="padding:2px 8px">{s}</td>'
        f'<td style="padding:2px 8px;text-align:right">{n:,}</td>'
        f'<td style="padding:2px 8px">{span}</td>'
        f'<td style="padding:2px 8px;text-align:right">{md}</td></tr>'
        for s, n, span, md in diag[:40]
    )
    diag_html = (
        '<details style="margin:8px 0"><summary style="cursor:pointer;color:#93c5fd">'
        f'▶ 5分足データ検証 ({len(diag)}銘柄が実際にロードされた・クリックで明細)</summary>'
        '<p class="footnote">1日あたり中央値バー数が概ね 50前後(9:00-15:00を5分刻み=約60本)'
        'なら正常。極端に少ない/多い・期間が飛んでいる銘柄はデータ破損の疑い。</p>'
        '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.8rem">'
        '<thead><tr style="color:#94a3b8"><th style="padding:2px 8px">銘柄</th>'
        '<th style="padding:2px 8px">総バー</th><th style="padding:2px 8px">期間</th>'
        '<th style="padding:2px 8px">日中央値バー数</th></tr></thead>'
        f'<tbody>{diag_rows}</tbody></table></div></details>'
    )

    # ── BTフィルタ別に全ブロックを描画 (全部/BT60/BT70) ─────────────────────
    bt_filters = [
        ("all",  "全部",     lambda sc: True),
        ("bt60", "BT60以上", lambda sc: (sc or 0) >= 60),
        ("bt70", "BT70以上", lambda sc: (sc or 0) >= 70),
    ]

    def _render_block(subset, key, active):
        an, awr, apnl = _stat(subset, "pnl_a")
        if not subset:
            return (f'<div id="ocblk_{key}" style="display:{"block" if active else "none"}">'
                    f'<p style="color:#94a3b8;padding:12px">該当BT帯の取引なし</p></div>')
        results = [_eval_time(subset, cm, margin_pct) for cm in times]
        best = max(results, key=lambda r: r["bpnl"])
        sweep_rows = ""
        for r in results:
            vs = r["bpnl"] - apnl
            star = ' style="background:#0d2818"' if r["confirm_min"] == best["confirm_min"] else ""
            sweep_rows += (
                f'<tr{star}>'
                f'<td style="padding:3px 10px;text-align:center">{_fmt_hhmm(r["confirm_min"])}'
                f'{" ★" if r["confirm_min"]==best["confirm_min"] else ""}</td>'
                f'<td style="padding:3px 10px;text-align:right">{r["bn"]}</td>'
                f'<td style="padding:3px 10px;text-align:right">{r["bwr"]:.1f}%</td>'
                f'<td style="padding:3px 10px;text-align:right;color:{_c(r["bpnl"])}">{r["bpnl"]:+,.0f}円</td>'
                f'<td style="padding:3px 10px;text-align:right;color:{_c(vs)};font-weight:700">{vs:+,.0f}円</td>'
                f'<td style="padding:3px 10px;text-align:right;color:#94a3b8">{r["sn"]}</td>'
                f'<td style="padding:3px 10px;text-align:right;color:{_c(r["spnl"])}">{r["spnl"]:+,.0f}円</td>'
                f'<td style="padding:3px 10px;text-align:right;color:#94a3b8">{r["prem"]:+.0f}</td>'
                f'</tr>'
            )
        # 最良時刻の戦略別 (_key で照合 = id()バグ修正)
        taken_by = {x["_key"]: x for x in best["taken"]}
        strat_rows = ""
        for s in sorted({r["strategy"] for r in subset}):
            rs = [r for r in subset if r["strategy"] == s]
            ts = [taken_by[r["_key"]] for r in rs if r["_key"] in taken_by]
            a_n, a_wr, a_p = _stat(rs, "pnl_a")
            b_n, b_wr, b_p = _stat(ts, "pnl_b")
            strat_rows += (
                f'<tr><td style="padding:3px 10px">{s}</td>'
                f'<td style="padding:3px 10px;text-align:right">{a_n}</td>'
                f'<td style="padding:3px 10px;text-align:right">{a_wr:.1f}%</td>'
                f'<td style="padding:3px 10px;text-align:right;color:{_c(a_p)}">{a_p:+,.0f}円</td>'
                f'<td style="padding:3px 10px;text-align:right">{b_n}</td>'
                f'<td style="padding:3px 10px;text-align:right">{b_wr:.1f}%</td>'
                f'<td style="padding:3px 10px;text-align:right;color:{_c(b_p)}">{b_p:+,.0f}円</td>'
                f'<td style="padding:3px 10px;text-align:right;color:#94a3b8">{a_n - b_n}</td></tr>'
            )
        disp = "block" if active else "none"
        # 判定: 全確認時刻がAに劣るなら「非推奨」。★は僅差の最大でノイズなので過信させない。
        best_vs = best['bpnl'] - apnl
        n_beat = sum(1 for r in results if (r['bpnl'] - apnl) > 0)
        if best_vs <= 0:
            verdict = (f'<div style="background:#3a1520;border:1px solid #f87171;border-radius:6px;'
                       f'padding:8px 14px;margin-bottom:8px;color:#fca5a5;font-weight:600">'
                       f'❌ 判定: 寄り確認は非推奨 — 全{len(results)}時刻でAに劣ります'
                       f'(最良でもA比 {best_vs:+,.0f}円)。'
                       f'<span style="color:#94a3b8;font-weight:400">★は僅差の最大でノイズ。'
                       f'実行ごとに位置が変わるのは「最適時刻が無い＝どの時刻もダメ」の証拠です。</span></div>')
        elif n_beat <= 2:
            verdict = (f'<div style="background:#2a2410;border:1px solid #facc15;border-radius:6px;'
                       f'padding:8px 14px;margin-bottom:8px;color:#fde68a;font-weight:600">'
                       f'△ 判定: A超えは{len(results)}時刻中{n_beat}時刻のみ(僅差)。'
                       f'<span style="color:#94a3b8;font-weight:400">過学習の可能性。'
                       f'フォワードで確認するまで採用は保留推奨。</span></div>')
        else:
            verdict = (f'<div style="background:#0d2818;border:1px solid #4ade80;border-radius:6px;'
                       f'padding:8px 14px;margin-bottom:8px;color:#86efac;font-weight:600">'
                       f'✅ 判定: A超えが{len(results)}時刻中{n_beat}時刻。寄り確認が有効な可能性あり'
                       f'(要フォワード確認)。</div>')
        return f"""<div id="ocblk_{key}" style="display:{disp}">
{verdict}
<div style="background:#111827;border:1px solid #1e293b;border-radius:6px;padding:8px 14px;margin-bottom:12px;display:inline-block">
  A(現行 逆指値): <b>{an}件</b> 勝率{awr:.1f}% 損益 <b style="color:{_c(apnl)}">{apnl:+,.0f}円</b>
  &nbsp;/&nbsp; 参考: 僅差最大の確認時刻 <b style="color:#94a3b8">{_fmt_hhmm(best['confirm_min'])}</b>
  (寄り+{best['confirm_min']}分) 損益 <b style="color:{_c(best['bpnl'])}">{best['bpnl']:+,.0f}円</b>
  (A比 <b style="color:{_c(best['bpnl']-apnl)}">{best['bpnl']-apnl:+,.0f}円</b>)
</div>
<h3 style="margin:10px 0 4px;font-size:0.95rem">確認時刻スイープ</h3>
<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">
<thead><tr style="color:#94a3b8">
  <th style="padding:3px 10px">確認時刻</th><th style="padding:3px 10px">B件</th>
  <th style="padding:3px 10px">B勝率</th><th style="padding:3px 10px">B損益</th>
  <th style="padding:3px 10px">vs A</th><th style="padding:3px 10px">見送件</th>
  <th style="padding:3px 10px" title="Bが見送った取引をAで取った損益。マイナスほど見送りが正解">見送A損益</th>
  <th style="padding:3px 10px" title="両方取った取引の平均建値差(B-A円/株)。プラス=高値掴み">建値差</th>
</tr></thead><tbody>{sweep_rows}</tbody></table></div>
<h3 style="margin:14px 0 4px;font-size:0.95rem">最良時刻({_fmt_hhmm(best['confirm_min'])})の戦略別</h3>
<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">
<thead><tr style="color:#94a3b8">
  <th style="padding:3px 10px">戦略</th><th style="padding:3px 10px">A件</th>
  <th style="padding:3px 10px">A勝率</th><th style="padding:3px 10px">A損益</th>
  <th style="padding:3px 10px">B件</th><th style="padding:3px 10px">B勝率</th>
  <th style="padding:3px 10px">B損益</th><th style="padding:3px 10px">見送</th>
</tr></thead><tbody>{strat_rows}</tbody></table></div>
</div>"""

    btns = "".join(
        f'<button class="ovbt-btn{" active" if k=="all" else ""}" '
        f'id="ocbtn_{k}" onclick="switchOcBt(\'{k}\')">{lbl} '
        f'<span style="font-size:0.72rem;color:#94a3b8">'
        f'({sum(1 for r in rows if f(r["rec_score"]))})</span></button>'
        for k, lbl, f in bt_filters
    )
    blocks = "".join(
        _render_block([r for r in rows if f(r["rec_score"])], k, k == "all")
        for k, lbl, f in bt_filters
    )

    # 実際の取引(約定日)の期間レンジ
    def _d(x):
        return x.date() if hasattr(x, "date") else x
    _edts = [_d(r["entry_dt"]) for r in rows if r.get("entry_dt") is not None]
    n_syms = len({r["symbol"] for r in rows})
    if _edts:
        _dmin, _dmax = min(_edts), max(_edts)
        _range = f"{_dmin}〜{_dmax} (約{(_dmax - _dmin).days}日 / {n_syms}銘柄)"
    else:
        _range = "-"

    return f"""<h2 style="margin-top:8px">⑭ 寄り後確認エントリー検証（最適時刻スイープ）</h2>
<p class="footnote">
逆指値ブレイク買い(A=現行)と「寄り(09:00)後N分に前日終値=トリガ超えを確認して買う」(B)を実5分足で比較。<br>
損切り/目標は絶対価格なので決済はA/B共通。5分足は各取引の約定日の株価取得のみに使用。<br>
<b>対象期間(約定日): {_range}</b> &nbsp;/&nbsp; 対象 <b>{len(rows)}件</b>(5分足取得済 / データ無し {no_data}件)
&nbsp; source={src} &nbsp; margin={margin_pct}%<br>
<span style="color:#94a3b8">※ 損益タブと同じ取引セット(全設定=con/agg×全ホールドアウト・監視対象の全銘柄・
重複除外・決済済みロングのみ)。市場全体の全銘柄を見るには
<code>python analyze_open_confirm_entry.py --all-minute --days 60</code>。</span></p>
{diag_html}
<div class="detail-tab-nav" style="margin:10px 0">{btns}</div>
{blocks}
<p class="footnote" style="margin-top:10px">
読み方: 『vs A』最大の時刻が最適。プラスなら寄り確認が有効。<br>
『見送A損益』が大きくマイナス=見送った取引がAでは『勝ち』だった=見送りは損。
プラスに近い/マイナスなら見送り(ダマシ除外)が効いている。
『建値差』大=本物のブレイクを高値掴み(出遅れコスト)。<br>
※ source=yfinance の場合は 5分足pkl が見つからず約60日のyfinanceで検証。
長期(~2年)で検証するには <code>python find_minute_data.py</code> を一度実行すると
data/quarantine_5m 等を自動特定して記憶し、以降このタブが自動使用します。
最適時刻は過学習しやすいので数ヶ月のフォワードで確認を。</p>
<script>
function switchOcBt(key){{
  ['all','bt60','bt70'].forEach(function(k){{
    var b=document.getElementById('ocblk_'+k); if(b) b.style.display=(k===key)?'block':'none';
    var t=document.getElementById('ocbtn_'+k); if(t) t.classList.toggle('active', k===key);
  }});
}}
</script>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default="0,15,30,45,60,90,120,150,180,240",
                    help="評価する確認時刻(寄り09:00からの分)をカンマ区切り。"
                         "最良の時刻を探す。0=寄り(09:00)そのもの")
    ap.add_argument("--confirm-min", type=int, default=None,
                    help="単一時刻だけ詳細評価したい場合に指定 (--sweep を無視)")
    ap.add_argument("--margin-pct", type=float, default=0.0,
                    help="確認しきい値: トリガ×(1+margin%%)超で買う (既定0=トリガ超)")
    ap.add_argument("--minute-dir", default=None,
                    help="5分足CSVディレクトリ ({code}.csv)。無指定はyfinance(60日)")
    ap.add_argument("--days", type=int, default=400,
                    help="対象トレードのエントリー日を直近何日に絞るか (既定400=ほぼ全期間)")
    ap.add_argument("--all-minute", action="store_true",
                    help="WATCHLISTでなく『5分足がある全銘柄』を対象にする(ユニバース検証)")
    ap.add_argument("--limit", type=int, default=0,
                    help="--all-minute 時、先頭N銘柄だけ(デバッグ/時短)")
    ap.add_argument("--workers", type=int, default=4,
                    help="--all-minute 時の並列数")
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    if args.confirm_min is not None:
        times = [args.confirm_min]
    else:
        times = [int(x) for x in args.sweep.split(",") if x.strip() != ""]

    mode = os.environ.get("TRADING_MODE", "conservative")
    # ユニバース決定
    universe = None
    if args.all_minute:
        universe = universe_from_pkl()
        if args.limit and args.limit > 0:
            universe = universe[:args.limit]

    print("=" * 82)
    print(f"寄り後 確認エントリー検証(最適時刻スイープ)  mode={mode}  "
          f"margin={args.margin_pct}%")
    print(f"確認時刻(分): {times}   5分足={'CSV:'+args.minute_dir if args.minute_dir else 'pkl自動/yfinance'}")
    if universe is not None:
        print(f"対象ユニバース: 5分足がある全銘柄 {len(universe)}銘柄 × 全戦略 "
              f"(直近{args.days}日, workers={args.workers})")
        print("  ※ 銘柄数が多いと時間がかかります(--limit で時短可)")
    print("=" * 82)

    trades = _collect_trades(args.days, universe=universe, workers=args.workers)
    _traded_syms = {t["symbol"] for t in trades}
    if universe is not None:
        print(f"探索母集団: {len(universe)}銘柄 → 直近{args.days}日に"
              f"『取引があった銘柄』: {len(_traded_syms)}銘柄 / {len(trades)}件")
    else:
        print(f"対象決済済みロング取引: {len(trades)}件 "
              f"(WATCHLIST {len(_traded_syms)}銘柄・直近{args.days}日)")

    # 各トレードについて、全候補時刻の株価をまとめて取得 (5分足は1回ロード)
    rows, no_data = [], 0
    for t in trades:
        trig = t["trigger"]
        if not trig or trig <= 0:
            continue
        pm = {}
        for cm in times:
            p = _price_at(t["symbol"], t["entry_dt"], cm, args.minute_dir)
            if p is not None and 0.65 * trig <= p <= 1.35 * trig:
                pm[cm] = p
        if not pm:
            no_data += 1
            continue
        rows.append(dict(**t, price_map=pm, _key=len(rows)))

    cover = len(rows)
    print(f"5分足が取得できた取引: {cover}件 / データ無し {no_data}件"
          f"{' (yfinanceは約60日のみ)' if not args.minute_dir else ''}\n")
    if cover == 0:
        print("※ 5分足が1件も取れませんでした。--minute-dir でCSVを指定するか、"
              "yfinance導入・直近60日での実行をご確認ください。")
        return

    an, awr, apnl = _stat(rows, "pnl_a")
    print("── ベースライン ──────────────────────────────────────────────")
    print(f"  A(現行 逆指値): {an:3d}件  勝率{awr:5.1f}%  損益 {apnl:+12,.0f}円\n")

    # 時刻スイープ
    results = [_eval_time(rows, cm, args.margin_pct) for cm in times]
    print("── 確認時刻スイープ (B=その時刻に前日終値超えを確認して買う) ──────────")
    print(f"  {'確認時刻':>8}{'B件':>5}{'B勝率':>7}{'B損益':>13}"
          f"{'vs A':>13}{'見送件':>6}{'見送A損益':>13}{'建値差':>8}")
    best = None
    for r in results:
        hhmm = f"09:{r['confirm_min']:02d}" if r['confirm_min'] < 60 else f"{9 + r['confirm_min']//60}:{r['confirm_min']%60:02d}"
        vs = r["bpnl"] - apnl
        print(f"  {hhmm:>8}{r['bn']:>5}{r['bwr']:>6.1f}%{r['bpnl']:>+13,.0f}"
              f"{vs:>+13,.0f}{r['sn']:>6}{r['spnl']:>+13,.0f}{r['prem']:>+8.0f}")
        if best is None or r["bpnl"] > best["bpnl"]:
            best = r

    bh = best["confirm_min"]
    hhmm = f"09:{bh:02d}" if bh < 60 else f"{9 + bh//60}:{bh%60:02d}"
    print(f"\n★ 最良の確認時刻: {hhmm} (寄り+{bh}分)  "
          f"B損益 {best['bpnl']:+,.0f}円 (A比 {best['bpnl']-apnl:+,.0f}円)")

    # 最良時刻の戦略別内訳
    print("\n── 最良時刻の戦略別 (A損益 / B損益 / 見送り件数) ──────────────")
    print(f"  {'戦略':<8}{'A件':>5}{'A勝率':>7}{'A損益':>12}"
          f"{'B件':>5}{'B勝率':>7}{'B損益':>12}{'見送':>5}")
    taken_by = {x["_key"]: x for x in best["taken"]}
    strats = sorted({r["strategy"] for r in rows})
    for s in strats:
        rs = [r for r in rows if r["strategy"] == s]
        ts = [taken_by[r["_key"]] for r in rs if r["_key"] in taken_by]
        a_n, a_wr, a_p = _stat(rs, "pnl_a")
        b_n, b_wr, b_p = _stat(ts, "pnl_b")
        print(f"  {s:<8}{a_n:>5}{a_wr:>6.1f}%{a_p:>+12,.0f}"
              f"{b_n:>5}{b_wr:>6.1f}%{b_p:>+12,.0f}{a_n-b_n:>5}")

    print("\n読み方:")
    print("  ・『vs A』が最も大きい確認時刻が最適。プラスなら寄り確認が有効")
    print("  ・『見送A損益』が大きくマイナス → その時刻の見送りが効いている(ダマシ除外)")
    print("  ・『建値差』(円/株, B-A) が大きい → 本物のブレイクを高値掴み(出遅れコスト)")
    print("  ※ yfinanceは約60日のみ。判断には --minute-dir で長期5分足を強く推奨")
    print("  ※ 最適時刻は過信禁物(過学習)。数ヶ月のフォワードで確認を推奨")


if __name__ == "__main__":
    main()
