r"""eh_trades.py — lss のシグナル集団に対して E / H の同日トレードを組み立てる。

E / H とは (CLAUDE.md 18.32)
---------------------------
シグナル・銘柄選定・発注順・決済(損切 sm×ATR / 利確 tm×ATR / 引け成行)は
**現行とまったく同一**で、違うのは**注文の出し方だけ**。

  現行 : 逆指値売り(前日終値−1ティック)。株価が**下がったら**約定
  E    : 寄成売り。9:00 の板寄せで**必ず**約定
  H    : 指値売り(前日終値)。株価が**上がったら**約定。寄りが既に上なら板寄せ。
         届かなければ**建てない**

設計
----
`nikkei_analysis.py` が持つ **元トレードの dict をコピーして**、約定値・決済値・
損益・理由だけ差し替える。銘柄名・BTスコア・WFスコア・ランク・設定ラベル等の
メタ情報がそのまま残るので、損益タブの明細・日チップ・月テーブルが
**他タブと完全に同じ見た目**になる。

⚠ この関数は「E/Hタブを出すための追加計算」であって、既存の集計には一切触れない。
   失敗しても呼び出し側が握りつぶせるように、例外は投げずに空を返す。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

_REASON = {"stop": "損切り", "target": "目標達成", "close": "タイムカット"}


def _fmt_md(d) -> str:
    """entry_d_raw(date でも 'YYYY-MM-DD' 文字列でも) を 'MM/DD' にする。"""
    try:
        return d.strftime("%m/%d")
    except Exception:
        s = str(d or "")
        return f"{s[5:7]}/{s[8:10]}" if len(s) >= 10 else s


def _tick(price: float) -> float:
    """東証の呼値(通常銘柄)。H の指値=前日終値 を作るのに使う。"""
    if price <= 3_000:
        return 1.0
    if price <= 5_000:
        return 5.0
    if price <= 30_000:
        return 10.0
    return 50.0


def build(trades, nofills, sm: float, tm: float, stop_delay_bars: int = 1,
          gap_guard: float = 0.03, qty: int = 100, workers: int = 6,
          require_open_bar: bool = True, log=print) -> dict:
    """E/H のトレード dict を作る。

    Args:
      trades  : 約定済みトレード(nikkei_analysis の _bt30_entry_sorted 相当)
      nofills : 不約定シグナル(all_nofills 相当)。E/H は建てるので母集団に要る
      sm / tm : 損切・利確の ATR 倍率(現行と同じ値を渡すこと)
      stop_delay_bars : 損切り遅延(live と同じ 1)
      gap_guard : 寄りのギャップ上限(0.03=±3%)。現行と同じ
      require_open_bar : 5分足の先頭バーが 09:00 の日だけ使う
        (09:05 始まりだと寄り直後の逆行が判定から抜けて損切りが過小に出る)

    Returns:
      {"E": [trade,...], "H": [...], "約定せず": {"E": [...], "H": [...]}}
      失敗時は {}。
    """
    try:
        from backtest_limit_entry import fetch as _fetch
        from daytrade_data import load_intraday as _li, split_by_day as _sbd
        from intraday_integrity import day_scale_ok as _ig_ok
        from sameday5m_firsttouch import short_exit_5m as _x5
        import pandas as pd
    except Exception as e:
        log(f"[E/H] 依存モジュールを読めません: {e}")
        return {}

    # ── 母集団: (銘柄, エントリー日) で1件にまとめる ────────────────
    #    同じ銘柄・同じ日は戦略が違っても同じ1トレード(トリガー/決済が同一)。
    #    代表として BT の高い行を採る(明細のバッジがそれに従う)。
    def _dk(t):
        return str(t.get("entry_d_raw") or t.get("exit_d_raw") or "")

    def _bt(t):
        return float(t.get("rec_score") or t.get("bt") or 0)

    # ⚠ 重複の扱いは**隣の400万円タブと必ず揃える**こと。
    #    現行タブは同じ銘柄が同日に複数戦略で出れば2件とも予算枠を消費する。
    #    E/H だけ (銘柄,日) で1件に畳むと、同じ400万でより多くの銘柄に分散でき、
    #    その差が『エントリー方式の効果』に化ける(2026-08-10 に発覚)。
    #    既定は畳まない=現行と同じ。LSS_EH_DEDUPE=1 で旧挙動(畳む)。
    _dedupe = str(os.environ.get("LSS_EH_DEDUPE", "0")).strip() in ("1", "true", "True", "yes")

    base: dict = {}
    rows: dict = {}      # (銘柄,日) -> [元トレード,...] 出力はこの数だけ作る
    _tk = 0
    for t in list(trades or []) + list(nofills or []):
        # ⛔ 転換(lss未約定→ロング転換)は **lss ではない**(18.5.3/18.26 で棄却済み)。
        #    隣の 400万円タブも「ショートのみ表示(転換は転換タブ参照)」なので、
        #    ここに混ぜると比較対象が揃わない。転換は全期間ぶん出力されるため、
        #    混ぜると表示窓より古い月が「転換だけの月」として並んでしまう。
        if t.get("strategy") == "転換":
            _tk += 1
            continue
        k = (str(t.get("symbol") or ""), _dk(t))
        if not k[0] or len(k[1]) < 10:
            continue
        if k not in base or _bt(t) > _bt(base[k]):
            base[k] = t
        rows.setdefault(k, []).append(t)
    if _dedupe:
        rows = {k: [v] for k, v in base.items()}
    if not base:
        log("[E/H] 母集団が空です")
        return {}

    syms = sorted({k[0] for k in base})
    if _tk:
        log(f"[E/H] 転換 {_tk:,}件を母集団から除外(ショートのみ / 転換タブ参照)")
    _nrow = sum(len(v) for v in rows.values())
    log(f"[E/H] 重複: {'畳む(LSS_EH_DEDUPE=1)' if _dedupe else '畳まない=現行タブと同じ'} "
        f"→ 出力 {_nrow:,}件 / {len(base):,}銘柄日")
    log(f"[E/H] 母集団 {len(base):,}銘柄日 / {len(syms):,}銘柄 を計算します "
        f"(sm={sm} tm={tm} 損切り遅延={stop_delay_bars}本 "
        f"ガード±{gap_guard * 100:.0f}% {qty}株 / **決済条件は現行と同一**)")

    # ── 日足(始値・ATR) と 5分足 ───────────────────────────────────
    def _load_d(sym):
        try:
            df = _fetch(sym, 900)
            if df is None or df.empty:
                return sym, None
            df = df.copy()
            df.index = pd.to_datetime(df.index).normalize()
            c = {str(x).lower(): x for x in df.columns}
            if any(c.get(x) is None for x in ("open", "high", "low", "close")):
                return sym, None
            o = df.rename(columns={c["open"]: "o", c["high"]: "h",
                                   c["low"]: "l", c["close"]: "c"})[
                ["o", "h", "l", "c"]].astype(float)
            pc = o["c"].shift(1)
            tr = pd.concat([o["h"] - o["l"], (o["h"] - pc).abs(),
                            (o["l"] - pc).abs()], axis=1).max(axis=1)
            o["atr"] = tr.ewm(span=14, adjust=False).mean()
            return sym, o
        except Exception:
            return sym, None

    def _load_5(sym):
        # ⛔ 並列読込は稀に SystemError('deallocated bytearray object has exported
        #    buffers') で失敗し、その銘柄が丸ごと落ちる。SystemError は Exception の
        #    サブクラスなので黙って {} になり、集計から消える。
        #    実測(2026-08-10): 同じ日・同じデータで .\ehm を3回走らせたら
        #    データ不足 636 / 646 / 569、E合計が ±9万円ぶれた。現行タブは
        #    このローダを使わないので3回とも1円まで同一だった。
        #    → 失敗したらこの場で直列リトライする(下の再読込パスでも拾う)。
        for _try in range(2):
            try:
                m5 = _li(sym, days=900, source="local")
                return sym, (_sbd(m5) if m5 is not None and len(m5) else {})
            except Exception:
                continue
        return sym, {}

    daily, i5 = {}, {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(_load_d, s) for s in syms]):
                s, v = f.result()
                daily[s] = v
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(_load_5, s) for s in syms]):
                s, v = f.result()
                i5[s] = v
    except Exception as e:
        log(f"[E/H] データ取得に失敗: {e}")
        return {}

    # ── 並列で落ちた銘柄を **直列で** 拾い直す(再現性のため) ──────────────
    #    ここを入れないと実行ごとに母集団が変わり、月別損益が数万円ぶれる。
    _miss = [s for s in syms if not i5.get(s)]
    for _p in range(2):
        if not _miss:
            break
        log(f"[E/H] 5分足を再読込(直列) {len(_miss):,}銘柄 …")
        _still = []
        for s in _miss:
            _, v = _load_5(s)
            if v:
                i5[s] = v
            else:
                _still.append(s)
        if len(_still) == len(_miss):
            break          # 直列でも取れない = 本当にデータが無い
        _miss = _still
    _n5d = sum(len(v) for v in i5.values() if v)
    _n5s = sum(1 for v in i5.values() if v)
    log(f"[E/H] 5分足 {_n5d:,}銘柄日 / {_n5s:,}銘柄 読込"
        + (f" (読めず {len(_miss):,}銘柄)" if _miss else "")
        + "  ← **実行ごとにこの数が変われば再現性の問題**"
          "(LSS_EH_WORKERS=1 で直列化して切り分け)")

    out = {"E": [], "H": []}
    nf = {"E": [], "H": []}
    # データ不足の内訳。合計だけだと実行ごとのブレの原因が追えない(2026-08-10)。
    _sk = {"日足なし": 0, "日足に該当日なし": 0, "5分足なし": 0,
           "分割ガード": 0, "先頭バーが09:00でない": 0, "価格/ATR異常": 0}
    for (sym, dstr), src in base.items():
        _srcs = rows.get((sym, dstr)) or [src]
        df = daily.get(sym)
        if df is None:
            _sk["日足なし"] += 1
            continue
        try:
            ts = pd.Timestamp(dstr[:10])
        except Exception:
            _sk["日足に該当日なし"] += 1
            continue
        idx = df.index
        pos = idx.searchsorted(ts)
        if pos <= 0 or pos >= len(idx) or idx[pos] != ts:
            _sk["日足に該当日なし"] += 1
            continue
        pc = float(df["c"].iloc[pos - 1])          # 前日終値
        o1 = float(df["o"].iloc[pos])              # 当日始値
        c1 = float(df["c"].iloc[pos])
        dl, dh = float(df["l"].iloc[pos]), float(df["h"].iloc[pos])
        atr = float(df["atr"].iloc[pos - 1])
        day5 = (i5.get(sym) or {}).get(ts.date())
        if not (pc > 0 and o1 > 0 and c1 > 0 and atr == atr and atr > 0):
            _sk["価格/ATR異常"] += 1
            continue
        if day5 is None or len(day5) == 0:
            _sk["5分足なし"] += 1
            continue
        if not _ig_ok(day5, c1):
            _sk["分割ガード"] += 1
            continue
        if require_open_bar:
            try:
                if pd.Timestamp(day5.index[0]).strftime("%H:%M") != "09:00":
                    _sk["先頭バーが09:00でない"] += 1
                    continue
            except Exception:
                _sk["先頭バーが09:00でない"] += 1
                continue

        # ── E: 寄成。寄りが −gap_guard を割るギャップダウンは約定不可 ──
        # ── H: 前日終値の指値。寄りが既に上なら板寄せ(=始値)で約定 ────
        for key, ep, ok in (
                ("E", o1, not (gap_guard > 0 and o1 < pc * (1 - gap_guard))),
                ("H", (o1 if o1 >= pc else pc),
                 not (gap_guard > 0 and o1 > pc * (1 + gap_guard)))):
            order_p = pc if key == "H" else o1
            if not ok or ep <= 0:
                nf[key].extend(_mk(s, order_p, 0.0, 0.0, "約定せず", "",
                                   pc, atr, sm, tm, qty, key) for s in _srcs)
                continue
            if key == "H":
                # 指値売りなので「上昇して到達」。寄りが上なら ei=0 で始値約定。
                xp, why, _e, _x = _x5(day5, ep, ep + atr * sm, ep - atr * tm, True,
                                      day_low=dl, day_high=dh, day_close=c1,
                                      stop_delay_bars=stop_delay_bars)
            else:
                # 寄りから建玉があるので ei=0 を強制する(+inf を渡す)。
                # そのまま渡すと「寄りが上に飛び昼に建値へ戻った」日の朝の
                # 含み損が判定から丸ごと抜け、負けだけが系統的に消える(18.32)。
                xp, why, _e, _x = _x5(day5, float("inf"),
                                      ep + atr * sm, ep - atr * tm, False,
                                      day_low=dl, day_high=dh, day_close=c1,
                                      stop_delay_bars=stop_delay_bars)
            if xp is None or why in ("no_5m", "no_entry"):
                nf[key].extend(_mk(s, order_p, 0.0, 0.0, "約定せず", "",
                                   pc, atr, sm, tm, qty, key) for s in _srcs)
                continue
            _t = ""
            try:
                _t = pd.Timestamp(_x).strftime("%H:%M")
            except Exception:
                pass
            out[key].extend(_mk(s, order_p, ep, float(xp),
                                _REASON.get(why, why), _t,
                                pc, atr, sm, tm, qty, key) for s in _srcs)
    _skip = sum(_sk.values())
    log(f"[E/H] 約定 E={len(out['E']):,} H={len(out['H']):,} / "
        f"不約定 E={len(nf['E']):,} H={len(nf['H']):,} / データ不足 {_skip:,}")
    log("[E/H] データ不足の内訳: "
        + " / ".join(f"{k} {v:,}" for k, v in _sk.items() if v))
    return {"E": out["E"], "H": out["H"], "約定せず": nf}


def _mk(src, order_p, entry_p, exit_p, reason, exit_time,
        pc, atr, sm, tm, qty, key):
    """元トレードをコピーして約定・決済まわりだけ差し替える。

    銘柄名・BT/WFスコア・ランク・設定ラベル等はそのまま残すので、
    明細のバッジや色が他タブと完全に一致する。
    """
    t = dict(src)
    t["entry_p"] = round(float(entry_p), 1)
    t["exit_p"] = round(float(exit_p), 1)
    t["order_limit"] = round(float(order_p), 1)
    t["order_stop"] = round(float(entry_p + atr * sm), 1) if entry_p else 0.0
    t["order_target"] = round(float(entry_p - atr * tm), 1) if entry_p else 0.0
    t["stop_price"] = t["order_stop"]
    t["target_price"] = t["order_target"]
    t["qty"] = qty
    t["reason"] = reason
    t["pnl"] = (round((float(entry_p) - float(exit_p)) * qty, 0)
                if reason != "約定せず" else 0.0)
    t["hold_days"] = 0
    # ⛔ _build_trade_row は entry_dt / exit_dt / exit_d_raw / name / pnl を
    #    **t["..."] で直接**引く(.get ではない)。不約定シグナル由来の行には
    #    entry_dt が無いことがあり、そのまま渡すと KeyError で落ちる。
    #    同日決済なので entry_dt = exit_dt。entry_d_raw から必ず作る。
    _raw = t.get("entry_d_raw") or t.get("exit_d_raw")
    t["entry_d_raw"] = _raw
    t["exit_d_raw"] = _raw
    _md = t.get("entry_dt") or t.get("exit_dt") or _fmt_md(_raw)
    t["entry_dt"] = _md
    t["exit_dt"] = _md
    t["name"] = t.get("name") or ""
    t["entry_time"] = "09:00"
    t["exit_time"] = exit_time
    t["days_to_fill"] = 0
    t["eh"] = key
    return t
