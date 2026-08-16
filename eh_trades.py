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



# ★ ATR期間のスイープ用(2026-08-15)。既定の 14 は **スイング戦略からの継承値**で、
#   同日決済に適切かは未検証だった。sm/tm はすべて ATR 倍率なので、この期間が
#   損切り・利確の絶対幅を直接決める。
# ⛔ 作る列と掃く期間がズレると、無い列は黙って既定(14)に落ちて
#    「ap5 と書いてあるのに中身は14」になる。両方の env を **合併**して作る。
_ATR_PERIODS = tuple(sorted(
    {int(x) for x in str(os.environ.get(
        "LSS_ATR_PERIODS", "3,5,7,10,14,20,30")).split(",")
     if str(x).strip().isdigit()}
    | {int(x) for x in str(os.environ.get(
        "LSS_EQ_ATRS", "3,5,7,10,20,30")).split(",")
       if str(x).strip().isdigit()}))
_ATR_MISS: set = set()


def _atr_of(df, pos, period=None):
    """前日の ATR。period=None なら既定(14日)の列を使う。"""
    try:
        if not period:
            return float(df["atr"].iloc[pos - 1])
        _col = f"atr{int(period)}"
        if _col not in df.columns:
            # ⛔ 黙って14に落とすと嘘の行が出る。1回だけ大きく警告する。
            if period not in _ATR_MISS:
                _ATR_MISS.add(period)
                print(f"⛔ [E/H] ATR{period}日 の列がありません。"
                      f"LSS_ATR_PERIODS に {period} を足してください。"
                      f"この変種は計算しません", flush=True)
            return float("nan")
        return float(df[_col].iloc[pos - 1])
    except Exception:
        return float("nan")


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
          require_open_bar: bool | None = None, variants=None, log=print) -> dict:
    """E/H のトレード dict を作る。

    Args:
      trades  : 約定済みトレード(nikkei_analysis の _bt30_entry_sorted 相当)
      nofills : 不約定シグナル(all_nofills 相当)。E/H は建てるので母集団に要る
      sm / tm : 損切・利確の ATR 倍率(現行と同じ値を渡すこと)
      stop_delay_bars : 損切り遅延(live と同じ 1)
      gap_guard : 寄りのギャップ上限(0.03=±3%)。現行と同じ
      require_open_bar : 5分足の先頭バーが 09:00 の日だけ使う。
        **既定 False**(2026-08-12 に変更。env LSS_REQUIRE_OPEN_BAR=1 で戻せる)。
        ⛔ 元の意図は「寄り直後の逆行が判定から抜けて損切りが過小に出るのを防ぐ」
           だったが、実測で前提が2つとも崩れた:
           ① yfinance は 09:00-09:05 を **どの銘柄にも** 持っていない
              (1分足でも無い / 板寄せを分足系列に入れない仕様)。
              通っていたのは **出来高0の幻バーがある銘柄だけ** で、
              そちらも寄りは見えていなかった。幻バーは v18 で除去済み。
           ② E/H の約定は 09:00 の板寄せ。delay1 の無保護窓が 09:00-09:05 と
              **ちょうど一致**するので、その足が見えなくても損切り判定に
              影響しない(元から損切りを置かない区間)。
              残る誤差は「その5分で利確(1.0ATR)に届いた場合」だけで、
              5分で2〜3%動く必要があり稀。
        ON にすると(幻バー除去後は)全銘柄が落ちるので注意。

    Returns:
      {"E": [trade,...], "H": [...], "約定せず": {"E": [...], "H": [...]}}
      失敗時は {}。
    """
    try:
        from backtest_limit_entry import (fetch as _fetch, round_to_tick as _r2t,
                                          tick_size as _tsz)
        try:   # 銘柄ごとの実測呼値(ticks.json)。無ければ従来の丸めに落ちる
            from backtest_limit_entry import round_to_tick_sym as _r2ts
        except Exception:
            _r2ts = None
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
    if require_open_bar is None:
        require_open_bar = str(os.environ.get("LSS_REQUIRE_OPEN_BAR", "0")).strip() \
            in ("1", "true", "True", "yes")
    # H の指値位置(前日終値からのティック数)。既定0=前日終値ちょうど。
    # +1 なら1ティック上 = より高く売れるが約定率が落ちる。掃くのは sweep_h_limit.py。
    try:
        _h_ticks = int(os.environ.get("LSS_H_LIMIT_TICKS", "0") or 0)
    except Exception:
        _h_ticks = 0
    # H を **寄指(寄付のみ有効)** にする。板寄せで約定しなければ建てない。
    #   ⛔ なぜ選べるようにしたか (2026-08-10):
    #      H の約定は2種類あり、質がまったく違う。
    #        板寄せ  (寄り>=指値)  = 9:00の板寄せで確実に約定。BTと現実が一致
    #        ザラ場到達            = 価格が上がってきて指値に当たった
    #                              = **上昇中に空売りを建てている**
    #      実測(hsweep): ザラ場の 円/件 は板寄せの 1/4〜1/11。指値+1では TEST で
    #      マイナス(-173円/件)。損切りは0.1ATR(株価の約0.3%)しかないので勢いが
    #      続けばすぐ刈られ、delay1 の無保護窓も上昇の真っ最中に当たる(18.17)。
    #      さらにザラ場分は『高値がタッチ=約定』の仮定に乗っており板の待ち行列が
    #      考慮されていない(18.33)。→ 取らないという選択肢が要る。
    _h_auction_only = str(os.environ.get("LSS_H_AUCTION_ONLY", "0")).strip() \
        in ("1", "true", "True", "yes")
    # ★ 09:00確認方式で「09:00 に寄らなかった銘柄」を建てるか。
    #   既定 **スキップ**(2026-08-16 ユーザー判断)。set LSS_SKIP_LATE_OPEN=0 で建てる。
    _SKIP_LATE_OPEN = str(os.environ.get("LSS_SKIP_LATE_OPEN", "1")).strip() \
        not in ("0", "false", "no", "off")
    # ── H のバリアント ────────────────────────────────────────────
    # ⛔ 設定ごとに .\daily を回して1回ずつ比べてはいけない(18.24)。実行が変わると
    #    比較相手(現行)の数字まで動くうえ、ノイズ帯も作れない。**1回の読込で
    #    複数の設定を同時に評価する**(5分足は共通なので追加コストはほぼゼロ)。
    # variants: [(表示名, 指値ティック, 寄指か[, 損切り遅延本数]), ...]。
    #   None なら env の設定1つ。第4要素を省くと build の stop_delay_bars。
    # ⚠ 損切り遅延(delay)を H で掃くための第4要素。delay1 の根拠(18.9)は
    #   「逆指値はバーの**どこで**約定したか分からないので、そのバーの間は
    #    損切りを置けない」という機構的な理由。H は板寄せ約定なら 09:00 に
    #   約定価格が確定するので、その理由は H には当てはまらない可能性がある。
    #   = H の最適な delay は現行と違いうる。だから測る。
    if not variants:
        variants = [("H", _h_ticks, _h_auction_only)]
    # v = (ラベル, 指値ティック, 寄指か, [損切り遅延], [損切りアンカー])
    #   損切りアンカー: "fill"(既定/実約定基準) | "max"(max(実約定,前日終値)基準)
    #   ⛔ 実約定基準は **寄りが指値より上で約定するケースに必須**(前日終値基準だと
    #      損切りが約定値より下に来て、建てた瞬間に成立する)。だが **指値〜前日終値の
    #      間で約定したケースでは不要**で、そこだけ損切りが不必要にタイトになる。
    #      max(実約定, 前日終値) なら前者は今と同じ、後者だけ広がって破綻もしない。
    # (表示名, 指値ティック, 寄指か, 損切り遅延, 損切りアンカー, 指値bp)
    # ⛔ 6要素目の bp を入れると **ティック指定より優先** する。
    #    _TICK_TABLE は実測より 5〜10倍粗く、しかも呼値は銘柄ごとに違うので
    #    「-5ティック」が板の上では -50ティックになっていた(2026-08-14)。
    #    bp 指定なら呼値テーブルに依存せず連続的に探索できる。
    # 7要素目 = **寄り確認モード**のギャップ閾値(bp / 前日終値比)。
    # ⛔ 板寄せは 09:00 の一瞬で終わるので、**寄り値を見てから寄り値では売れない**。
    #    「ギャップを確認してから発注する」を正直に再現すると、約定は
    #    **最初の5分足の終値(09:05)** になる。前夜の指値(=寄り値で約定)より
    #    5分ぶん遅く、その間の値動きは取り逃がす。その差を測るための変種。
    #    None ならこのモードを使わない(従来どおり)。
    # 8要素目 = **損切り幅(sm)の上書き**。ATR倍率。None なら build の sm。
    # ⛔ delay と sm は**代替関係**(どちらも損切りを緩める方向)。
    #    2026-08-15 の実測で delay は d0<d1<d2<d3<d4<d5 と単調に良くなり、
    #    増分は逓減していた(+669/+498/+250/+169/+49 円/件)。これは
    #    「delay が効く」ではなく **「0.1ATR の損切りが効いていない」**
    #    可能性が高い。切り分けるには sm を広げた版を同じ表に並べるしかない。
    #    ★ 運用上も意味が違う: delay は『一定時間 損切りゼロ』、
    #      sm は『常に損切りあり・幅が広い』。同じ効果なら **sm のほうが
    #      尾リスクの意味で健全**(悪材料が出ても必ず切れる)。
    _HV = [(str(v[0]), int(v[1]), bool(v[2]),
            (int(v[3]) if len(v) > 3 and v[3] is not None else int(stop_delay_bars)),
            (str(v[4]) if len(v) > 4 and v[4] else "fill"),
            (float(v[5]) if len(v) > 5 and v[5] is not None else None),
            (float(v[6]) if len(v) > 6 and v[6] is not None else None),
            (float(v[7]) if len(v) > 7 and v[7] is not None else None),
            # 9要素目 = **利確幅(tm)の上書き**。ATR倍率。None なら build の tm。
            # ⛔ tm は lss(逆指値)から継承したまま **一度も掃いていない**
            #    (2026-08-15 の監査で判明)。delay を伸ばしたことで利確到達が
            #    68件→138件 と2倍になっており、最適な利確幅は以前と違いうる。
            #    §18.28 の「tm は変えない」は lss・delay1 での結論。
            (float(v[8]) if len(v) > 8 and v[8] is not None else None),
            # 11要素目 = **締切(分)**。09:00 からの経過分。None なら現行(始値約定)。
            # ★★ 2026-08-16 ユーザー発案「09:00から順次取得していけないか」。
            #   遅寄り銘柄(15.7%)は 09:02〜09:06 に集中しているので、
            #   **09:10 まで待てばほぼ全部の始値が出揃う**。出揃ってから
            #   件数を確定させれば `予算÷件数` が計算でき、遅寄りも建てられる。
            #   代償は「09:00 に寄った銘柄も締切時刻の値で建てる」こと。
            #   どちらが得かは測らないと分からないので変種にする。
            # 10要素目 = **ATR期間の上書き**(日足の EWM span)。None なら既定14。
            # ⛔ 14 は **スイング戦略からの継承値**で、同日決済(数時間保有)に
            #    適切かは一度も検証していない(2026-08-15 の監査で判明)。
            #    sm/tm はすべて ATR 倍率なので、この期間が損切り・利確の
            #    **絶対幅そのもの**を決める。
            (int(v[9]) if len(v) > 9 and v[9] is not None else None),
            (int(v[10]) if len(v) > 10 and v[10] is not None else None),
            # 12要素目 = **段階の刻み(分)**。11要素目(締切)と併用する。
            # ★★ 2026-08-16 ユーザー発案「せめて2段階とかにしてほしい」。
            #   一括締切(11要素目だけ)は 09:00 に寄った銘柄まで締切時刻まで
            #   待たせるので、そのぶんの値動きを丸ごと捨てる。段階にすれば
            #   **09:00組は09:00の値で建て、遅寄り組だけ後の回で拾える**。
            #   刻み=10・締切=10 → 2段階(09:00 / 09:10)。
            #   ⚠ **5分足なので刻みの下限は5分**。1分刻みの判定はこのデータ
            #     では測れない(先頭バーが09:05の銘柄が実際に何分に寄ったかが
            #     分からない)。測るには1分足(CLAUDE.md「データ場所メモ」)が要る。
            (int(v[11]) if len(v) > 11 and v[11] is not None else None))
           for v in variants]
    _HKEYS = [v[0] for v in _HV]

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
    if _h_auction_only:
        log("[E/H] ⚠ H は **寄指(寄付のみ)** です (LSS_H_AUCTION_ONLY)。"
            "板寄せで約定しなければ建てません = ザラ場到達を一切取りません")
    if _h_ticks:
        log(f"[E/H] ⚠ H の指値を前日終値から {_h_ticks:+d}ティック ずらしています "
            f"(LSS_H_LIMIT_TICKS)。既定0と比較するときは条件を揃えること")
    log(f"[E/H] 先頭バー09:00の要求: {'ON(LSS_REQUIRE_OPEN_BAR)' if require_open_bar else 'OFF(既定)'}"
        f" / 09:00が無い日は delay を1本前倒し(無保護窓=09:00-09:05 と一致するため)")
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
            # ★ ATR期間は **スイング戦略からの継承値(日足14日)**。sm/tm は
            #   すべて ATR 倍率なので、この14が損切り・利確の絶対幅を決める。
            #   同日決済(数時間保有)に日足14日が適切かは未検証だったので、
            #   掃けるように期間ごとの列を持つ(2026-08-15)。
            o["atr"] = tr.ewm(span=14, adjust=False).mean()
            for _ap in _ATR_PERIODS:
                o[f"atr{_ap}"] = tr.ewm(span=_ap, adjust=False).mean()
            return sym, o
        except Exception:
            return sym, None

    def _load_5(sym):
        # ⛔ 並列読込は稀に SystemError('deallocated bytearray object has exported
        #    buffers') で失敗し、その銘柄が丸ごと落ちる。SystemError は Exception の
        #    サブクラスなので黙って {} になり、集計から消える。
        #    実測(2026-08-10): 同じ日・同じデータでレポートを3回走らせたら
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

    out = {"E": []} | {k: [] for k in _HKEYS}
    nf = {"E": []} | {k: [] for k in _HKEYS}
    # データ不足の内訳。合計だけだと実行ごとのブレの原因が追えない(2026-08-10)。
    _sk = {"日足なし": 0, "日足に該当日なし": 0, "5分足なし": 0,
           "分割ガード": 0, "先頭バーが09:00でない": 0, "価格/ATR異常": 0}
    # ⛔ 件数だけだと『実発注した銘柄がテストの母集団に無い』ときに原因を追えない
    #    (2026-08-14: 3197/6323/6965 の3件が消え、当日の損失のほぼ全部だった)。
    #    **どの銘柄がどの日に落ちたか**を残す。レポートの H ペインに出すので
    #    コンソールのスクロールバックを探さなくてよい。
    _sk_syms: dict = {k: [] for k in _sk}
    _n_late: dict = {}     # 09:00に寄らなかった件数(確認方式のみ)
    _late_hm: dict = {}    # その寄り時刻の分布 (HH:MM -> 件数)
    _SK_CAP = 400                     # 1理由あたりの保持上限(HTMLが肥大しない程度)
    # H の約定の内訳。**ライブでの信頼度がまったく違う**ので必ず出す:
    #   板寄せ  = 寄りが既に前日終値以上 → 9:00の板寄せで前日終値以上の値がつく。
    #             指値売りは必ず約定する(始値で約定)。バックテストと現実が一致。
    #   ザラ場  = 寄りは下だったが日中に前日終値まで戻って約定。
    #             ⚠ バックテストは「高値が指値に触れたら約定」とみなすが、実際は
    #             **板の待ち行列**があり、触れただけでは約定しないことがある。
    #             ここの比率が高いほど H の数字は楽観側に寄る。
    _hfill = {k: {"板寄せ": 0, "ザラ場到達": 0} for k in _HKEYS}
    for (sym, dstr), src in base.items():
        _srcs = rows.get((sym, dstr)) or [src]
        df = daily.get(sym)
        if df is None:
            _sk["日足なし"] += 1
            if len(_sk_syms["日足なし"]) < _SK_CAP:
                _sk_syms["日足なし"].append((str(dstr), str(sym)))
            continue
        try:
            ts = pd.Timestamp(dstr[:10])
        except Exception:
            _sk["日足に該当日なし"] += 1
            if len(_sk_syms["日足に該当日なし"]) < _SK_CAP:
                _sk_syms["日足に該当日なし"].append((str(dstr), str(sym)))
            continue
        idx = df.index
        pos = idx.searchsorted(ts)
        if pos <= 0 or pos >= len(idx) or idx[pos] != ts:
            _sk["日足に該当日なし"] += 1
            if len(_sk_syms["日足に該当日なし"]) < _SK_CAP:
                _sk_syms["日足に該当日なし"].append((str(dstr), str(sym)))
            continue
        pc = float(df["c"].iloc[pos - 1])          # 前日終値
        o1 = float(df["o"].iloc[pos])              # 当日始値
        c1 = float(df["c"].iloc[pos])
        dl, dh = float(df["l"].iloc[pos]), float(df["h"].iloc[pos])
        atr = float(df["atr"].iloc[pos - 1])
        day5 = (i5.get(sym) or {}).get(ts.date())
        if not (pc > 0 and o1 > 0 and c1 > 0 and atr == atr and atr > 0):
            _sk["価格/ATR異常"] += 1
            if len(_sk_syms["価格/ATR異常"]) < _SK_CAP:
                _sk_syms["価格/ATR異常"].append((str(dstr), str(sym)))
            continue
        if day5 is None or len(day5) == 0:
            _sk["5分足なし"] += 1
            if len(_sk_syms["5分足なし"]) < _SK_CAP:
                _sk_syms["5分足なし"].append((str(dstr), str(sym)))
            continue
        if not _ig_ok(day5, c1):
            _sk["分割ガード"] += 1
            if len(_sk_syms["分割ガード"]) < _SK_CAP:
                _sk_syms["分割ガード"].append((str(dstr), str(sym)))
            continue
        # 先頭バー時刻は require_open_bar が OFF でも要る(delay の数え直しに使う)
        try:
            _first_hm = pd.Timestamp(day5.index[0]).strftime("%H:%M")
        except Exception:
            _first_hm = ""
        if require_open_bar and _first_hm != "09:00":
            _sk["先頭バーが09:00でない"] += 1
            if len(_sk_syms["先頭バーが09:00でない"]) < _SK_CAP:
                _sk_syms["先頭バーが09:00でない"].append((str(dstr), str(sym)))
            continue

        # ── E: 寄成。寄りが −gap_guard を割るギャップダウンは約定不可 ──
        # ── H: 指値。既定は前日終値。LSS_H_LIMIT_TICKS で上下にずらせる ──
        #      上げる = より高く売れるが約定率が落ちる / 下げる = 逆。
        #      H 固有の唯一のパラメータで、掃くなら sweep_h_limit.py。
        # ガードの基準は **前日終値**。ずらした指値を基準にすると
        # 指値を下げるほどガードも下がり、正常な日まで弾く(2026-08-10)。
        _cases = [("E", o1, o1,
                   not (gap_guard > 0 and o1 < pc * (1 - gap_guard)),
                   int(stop_delay_bars), "fill", "", sm, tm, None)]
        for (_hn, _ht_, _ha, _hd, _hanc, _hbp, _hgap, _hsm, _htm,
             _hap, _hcut, _hwv) in _HV:
            _sm_v = sm if _hsm is None else _hsm
            _tm_v = tm if _htm is None else _htm
            if _hgap is not None:
                # ── 寄り確認モード ────────────────────────────────
                # 09:00 の寄り値を見て、前日終値比のギャップが閾値以上なら
                # **その場で成行売り**。
                #
                # ★ 約定価格は2通りある。**既定は始値**(2026-08-15 ユーザー決定:
                #   「寄り付き直後に完全自動で発注するので始値で近似してよい」)。
                #   ⛔ 2026-08-16 に既定を 09:05 にしてしまったが、これは
                #     その決定を無断で戻すものだった。始値に戻す。
                #     名前に『5分』が入る変種だけが 09:05 = **保守側の下限**。
                #   実際の執行は 08:5x に板を暖めておけば 09:00 + 十数秒なので、
                #   真の値はこの2つの間。差は measure_entry_decay.py で測る。
                _g_ok = (o1 > 0 and pc > 0
                         and (o1 - pc) / pc * 1e4 >= _hgap
                         and not (gap_guard > 0 and o1 > pc * (1 + gap_guard)))
                _slow = "5分" in str(_hn)
                if _slow:
                    try:
                        _ep_c = float(day5["close"].iloc[0])
                    except Exception:
                        _ep_c = 0.0
                else:
                    _ep_c = float(o1 or 0.0)
                # ★ 損切り遅延は **第4要素を尊重する**(2026-08-16)。以前は 0
                #   固定だったので、確認方式を推奨(d4)と並べて比べられなかった。
                #   ⚠ 09:05版は建てるのが5分遅いので、同じ N でも武装が5分遅い。
                # ★★ 締切モード(2026-08-16)。09:00 から順次読んで、締切時刻に
                #   出揃った始値で一括判定する。**遅寄り銘柄も建てられる**。
                #   約定は「締切時刻以降の最初の5分足の始値」。
                #   ⚠ **時刻で選ぶこと**(インデックスではない)。遅寄り銘柄は
                #     先頭バーが09:05/09:06 なので、index だと時刻がずれる。
                # ★★ 段階モード(_hwv)= 09:00 から刻みごとに判定して建てる。
                #   一括締切(_hwv なし)は 09:00 組まで締切時刻まで待たせるが、
                #   段階なら **09:00組は09:00の値・遅寄り組は後の回**で建つ。
                #   資金の配分(何段目にいくら使うか)は後処理
                #   (_size_equal_by_day)が `wave` を見て決める。
                if _hcut:
                    try:
                        _b0 = day5.index[0]
                        _bm0 = _b0.hour * 60 + _b0.minute - 540
                    except Exception:
                        continue
                    if _hwv:
                        # ⛔ 5分足なので「先頭バーが09:05」の銘柄が実際に何分に
                        #    寄ったかは分からない。**そのバーが終わるまでは
                        #    確定しない**とみなす(保守側)。09:00バーだけは
                        #    板寄せなので 0 でよい。
                        _need = 0 if _bm0 <= 0 else _bm0 + 5
                        _wv = None
                        _w0 = 0
                        while _w0 <= int(_hcut):
                            if _w0 >= _need:
                                _wv = _w0
                                break
                            _w0 += int(_hwv)
                        if _wv is None:
                            continue      # 締切までに寄らなかった = 建てない
                    else:
                        _wv = int(_hcut)  # 一括: 全員 締切時刻で建てる
                    # ★★ 『即時』版 (2026-08-16 ユーザー指摘)。
                    #   1分ポーリングで寄りを検知できるなら、**判定は各銘柄の
                    #   始値でできる**(判定に件数は要らない)。件数が要るのは
                    #   **サイズ決定**だけ。この版は
                    #     約定価格 … 自分の始値(=検知即発注)
                    #     サイズ   … 段の配分(件数が確定してから)
                    #   に分けたときの上限を測る。段の値で建てる版との差が
                    #   そのまま **執行速度の価値** になる。
                    #   ⚠ そのままでは実装できない(サイズが決まる前に発注する
                    #     ことになる)。100株だけ即建てて段で積み増す形なら
                    #     部分的に実現できる。
                    if "即時" in str(_hn):
                        _cases.append((_hn, float(o1 or 0.0), float(o1 or 0.0),
                                       bool(_g_ok and (o1 or 0) > 0),
                                       int(_hd), _hanc, f"cut0w{_wv}",
                                       _sm_v, _tm_v, _hap))
                        continue
                    _cut_i = None
                    try:
                        for _bi2 in range(len(day5)):
                            _ts2 = day5.index[_bi2]
                            if _ts2.hour * 60 + _ts2.minute - 540 >= _wv:
                                _cut_i = _bi2
                                break
                    except Exception:
                        _cut_i = None
                    if _cut_i is None:
                        continue          # その日は締切以降のバーが無い
                    try:
                        _ep_k = float(day5["open"].iloc[_cut_i])
                    except Exception:
                        continue
                    _cases.append((_hn, _ep_k, _ep_k,
                                   bool(_g_ok and _ep_k > 0),
                                   int(_hd), _hanc, f"cut{_cut_i}w{_wv}",
                                   _sm_v, _tm_v, _hap))
                    continue
                _cases.append((_hn, _ep_c, _ep_c, bool(_g_ok and _ep_c > 0),
                               int(_hd), _hanc,
                               "confirm" if _slow else "confirm0",
                               _sm_v, _tm_v, _hap))
                continue
            if _hbp is not None:
                # bp 指定: 前日終値からの相対。銘柄の実測呼値で丸める
                # (無ければ従来の丸めに落ちる)。
                _raw = pc * (1.0 + _hbp / 1e4)
                _hl = float(_r2ts(_raw, sym)) if _r2ts else float(_r2t(_raw))
            else:
                _hl = float(_r2t(pc + _ht_ * _tsz(pc))) if _ht_ else pc
            _cases.append((
                _hn, _hl, (o1 if o1 >= _hl else _hl),
                (not (gap_guard > 0 and o1 > pc * (1 + gap_guard))
                 # 寄指: 寄りが指値に届かなければ板寄せで約定しない = 建てない
                 and (o1 >= _hl if _ha else True)),
                _hd, _hanc, "", _sm_v, _tm_v, _hap))
        # ── 09:00のバーが無い日は delay を1本ぶん前倒しする ────────────────
        # E/H の約定は **09:00 の板寄せ**。delay1 は「約定した5分足の間は損切りを
        # 置かない」なので、無保護窓は 09:00-09:05 = ちょうど欠けている足。
        # つまり **その足が見えなくても損切り判定には影響しない**(元から置かない)。
        # 問題は数え方で、先頭が09:05だと ei=0 がその足になり、delay1 で
        # 09:10武装 = 意図した delay1 が実質 delay2 になる(2026-08-12 発覚)。
        # 欠けている1本を「消費済み」とみなして1本減らせば、09:05武装に揃う。
        # ⚠ 板寄せ約定(寄りから建玉がある)のときだけ。ザラ場到達で建てた場合は
        #    約定足が日中なので、寄りの欠落は delay の数えに関係しない。
        _no_open_bar = _first_hm != "09:00"
        for (key, order_p, ep, ok, _dly, _anc, _mode, _smv, _tmv,
             _apv) in _cases:
            # ★ 変種ごとの ATR期間。None = 既定(日足14日)
            atr = _atr_of(df, pos, _apv)
            if not (atr == atr and atr > 0):
                continue
            _at_open = (key == "E") or (o1 >= ep)      # 寄り(板寄せ)で建てたか
            # ★★ 09:00 に寄らない銘柄はスキップ (2026-08-16 ユーザー判断)
            #   K は 09:00 に **件数を確定させないとサイズを決められない**。
            #   ところが 09:00 に寄らない銘柄がある(実測: 9984 は 09:06)。
            #   待つと件数が確定せず、混ぜるとサイズが後から狂う。
            #   → 寄らなかった銘柄は建てない、が唯一一貫するルール。
            #   ライブは kabu の OpeningPriceTime で同じ判定ができる。
            #   ⛔ **バックテストとライブを必ず揃える**(18.9)。ここを外すと
            #      「レポートには居るのに実運用では建てられない」ズレになる。
            #   確認方式だけに掛ける(前夜指値は寄らなくても指値が板にある)。
            # ★ 2026-08-16 変更: ここで落とさず **印を付けて通す**。
            #   落とすのは後処理(_size_equal_by_day)に任せる。こうすると
            #   「捨てているぶんが何時に寄って、いくらだったか」を同じ実行の
            #   中で測れる(遅寄り込みの変種も後処理だけで作れる)。
            # ⚠ 締切モード(cutN)は遅寄りでも建てられるので印を付けない。
            _is_late = (_mode == "confirm0" and _no_open_bar)
            if _is_late:
                _n_late[key] = _n_late.get(key, 0) + 1
                _late_hm[_first_hm] = _late_hm.get(_first_hm, 0) + 1
            # ⚠ この補正は「建玉が **その日の先頭バーから** 始まる」ときだけ。
            #   段階/締切モードで先頭より後のバーから建てる場合、スライスの
            #   先頭は実在するバーなので欠落補正は要らない(2026-08-16)。
            _from0 = (not str(_mode).startswith("cut")) \
                or str(_mode)[3:].split("w")[0] in ("", "0")
            if _no_open_bar and _at_open and _from0 and _dly > 0:
                _dly -= 1
            if not ok or ep <= 0:
                # ★ day_open を渡す(2026-08-15)。不約定側にも h_gap_bp が
                #   要る。「寄りが閾値をどれだけ下回ったか」が分からないと、
                #   8:59気配で前倒し判定したときに何件ひっくり返るかを測れない。
                for s in _srcs:
                    _nr = _mk(s, order_p, 0.0, 0.0, "約定せず", "",
                              pc, atr, _smv, _tmv, qty, key, day_open=o1)
                    # ★ 段階モード: 不約定(=ギャップ不合格)の候補も
                    #   「何段目で判定したか」を持たせる。予算をその段に
                    #   いくら配るかは『その段で判定する候補数』で決まるので、
                    #   合格分だけ数えると分母が過小になる。
                    if str(_mode).startswith("cut") and "w" in str(_mode):
                        _nr["wave"] = int(str(_mode).split("w")[-1] or 0)
                        _nr["open_hm"] = _first_hm
                    elif _is_late:
                        _nr["late_open"] = True
                        _nr["open_hm"] = _first_hm
                    nf[key].append(_nr)
                continue
            if key != "E" and not str(_mode).startswith("confirm"):
                # 指値売りなので「上昇して到達」。寄りが上なら ei=0 で始値約定。
                _sa = max(ep, pc) if _anc == "max" else ep      # 損切りアンカー
                xp, why, _e, _x = _x5(day5, ep, _sa + atr * _smv, _sa - atr * _tmv, True,
                                      day_low=dl, day_high=dh, day_close=c1,
                                      stop_delay_bars=_dly)
            elif str(_mode).startswith("cut"):
                # ── 締切モード: 締切バーから建玉を持つ ────────────────
                # ⛔ 18.32 の教訓: 建玉を持っている区間の一部が判定から抜けると
                #    **必ず利益方向に出る**。締切バーの始値で建てるので、
                #    そのバー自身から判定する(+inf で ei=0 を強制)。
                _ci = int(str(_mode)[3:].split("w")[0] or 0)
                _d3 = day5.iloc[_ci:]
                if len(_d3) == 0:
                    xp, why, _e, _x = None, "no_5m", None, None
                else:
                    _sa = ep
                    xp, why, _e, _x = _x5(_d3, float("inf"),
                                          _sa + atr * _smv, _sa - atr * _tmv,
                                          False, day_low=None, day_high=None,
                                          day_close=c1, stop_delay_bars=_dly)
            elif _mode == "confirm0":
                # ── 寄り確認・**始値約定**(既定) ──────────────────
                # 09:00 の板寄せ値で売れたとみなす。建玉は寄りからあるので
                # E と同じく **1本目のバーから**判定する(+inf で ei=0 を強制)。
                # ⛔ 18.32 の教訓: 建玉を持っている区間の一部が判定から抜けると
                #    **必ず利益方向に出る**。ここを day5.iloc[1:] にしてはいけない。
                _sa = ep
                xp, why, _e, _x = _x5(day5, float("inf"),
                                      _sa + atr * _smv, _sa - atr * _tmv, False,
                                      day_low=dl, day_high=dh, day_close=c1,
                                      stop_delay_bars=_dly)
            elif _mode == "confirm":
                # ── 寄り確認モード ────────────────────────────────
                # 09:05(最初の5分足の終値)で成行売り。したがって建玉を
                # 持っているのは **2本目のバー以降**。そこだけを判定対象に
                # 切り出し、+inf で ei=0 を強制する。
                # ⛔ 18.32 の教訓: 建玉を持っている区間の一部が判定から抜けると
                #    **必ず利益方向に出る**。ここは「1本目は建てていないので
                #    判定しない」であって、抜けているのではない。
                _d2 = day5.iloc[1:]
                if len(_d2) == 0:
                    xp, why, _e, _x = None, "no_5m", None, None
                else:
                    _sa = ep
                    xp, why, _e, _x = _x5(_d2, float("inf"),
                                          _sa + atr * _smv, _sa - atr * _tmv, False,
                                          day_low=None, day_high=None, day_close=c1,
                                          stop_delay_bars=_dly)
            else:
                # 寄りから建玉があるので ei=0 を強制する(+inf を渡す)。
                # そのまま渡すと「寄りが上に飛び昼に建値へ戻った」日の朝の
                # 含み損が判定から丸ごと抜け、負けだけが系統的に消える(18.32)。
                _sa = ep                                        # E は寄成なので常に実約定
                xp, why, _e, _x = _x5(day5, float("inf"),
                                      _sa + atr * _smv, _sa - atr * _tmv, False,
                                      day_low=dl, day_high=dh, day_close=c1,
                                      stop_delay_bars=_dly)
            if xp is None or why in ("no_5m", "no_entry"):
                # ★ day_open を渡す(2026-08-15)。不約定側にも h_gap_bp が
                #   要る。「寄りが閾値をどれだけ下回ったか」が分からないと、
                #   8:59気配で前倒し判定したときに何件ひっくり返るかを測れない。
                nf[key].extend(_mk(s, order_p, 0.0, 0.0, "約定せず", "",
                                   pc, atr, _smv, _tmv, qty, key,
                                   day_open=o1) for s in _srcs)
                continue
            if key != "E" and not str(_mode).startswith("confirm"):
                # ⛔ 判定は **指値(order_p)** であって前日終値(pc)ではない
                #    (2026-08-14 修正)。H の指値は前日終値-5ティックなので、
                #    寄りが「指値以上・前日終値未満」の帯は **板寄せで約定している**
                #    のに『ザラ場到達』と誤分類していた。実測が2営業日 8/8 すべて
                #    09:00 板寄せだったのに、レポートが 48% をザラ場と表示して
                #    食い違って見えたのはこれが原因。
                #    ep = (o1 if o1 >= order_p else order_p) なので
                #    板寄せ ⟺ o1 >= order_p。
                _hfill[key]["板寄せ" if o1 >= order_p else "ザラ場到達"] += len(_srcs)
            _t = ""
            try:
                _t = pd.Timestamp(_x).strftime("%H:%M")
            except Exception:
                pass
            for _s0 in _srcs:
                _tr = _mk(_s0, order_p, ep, float(xp),
                          _REASON.get(why, why), _t,
                          pc, atr, _smv, _tmv, qty, key, _sa, o1)
                # ★ 09:00 に寄らなかった銘柄日に印を付ける(2026-08-16)。
                #   落とすのは後処理(_size_equal_by_day)。ここで落とすと
                #   「捨てているぶんがいくらだったか」を測れない。
                if _is_late:
                    _tr["late_open"] = True
                    _tr["open_hm"] = _first_hm
                # ★ 段階モード: 何段目(09:00からの経過分)で建てたか。
                #   後処理が予算をこの段ごとに配る。
                if str(_mode).startswith("cut") and "w" in str(_mode):
                    _tr["wave"] = int(str(_mode).split("w")[-1] or 0)
                    _tr["open_hm"] = _first_hm
                out[key].append(_tr)
    _skip = sum(_sk.values())
    log("[E/H] 約定 " + " / ".join(
        f"{k}={len(out[k]):,}" for k in ["E"] + _HKEYS)
        + " / 不約定 " + " / ".join(f"{k}={len(nf[k]):,}" for k in ["E"] + _HKEYS)
        + f" / データ不足 {_skip:,}")
    log("[E/H] データ不足の内訳: "
        + " / ".join(f"{k} {v:,}" for k, v in _sk.items() if v))
    if _late_hm:
        _tot_hm = sum(_late_hm.values())
        log("[E/H] 09:00に寄らなかった銘柄日の **寄り時刻** (変種ぶん合算): "
            + " / ".join(f"{_h} {_c:,}件({_c / max(1, _tot_hm) * 100:.0f}%)"
                         for _h, _c in sorted(_late_hm.items(),
                                              key=lambda x: -x[1])[:8]))
    if _n_late:
        _tot_late = sum(_n_late.values())
        _per = max(_n_late.values())
        log(f"[E/H] 09:00に寄らなかった: 変種あたり最大 {_per:,}銘柄日 "
            f"(全変種計 {_tot_late:,})。K は 09:00 に件数を確定させないと"
            f"サイズを決められないので **既定では建てない**"
            f"(ライブは OpeningPriceTime で同判定)。"
            f"⚠ 印を付けて通してあるので、"
            f"レポートの『…遅寄り込み』の行でいくら捨てているか測れる")
    for _k in _HKEYS:
        _ht = sum(_hfill[_k].values())
        if not _ht:
            continue
        _zr = _hfill[_k]["ザラ場到達"]
        log(f"[E/H] {_k} の約定内訳: 板寄せ {_hfill[_k]['板寄せ']:,}件 "
            f"({_hfill[_k]['板寄せ'] / _ht * 100:.0f}%) / "
            f"ザラ場到達 {_zr:,}件 ({_zr / _ht * 100:.0f}%)")
    if any(_hfill[k]["ザラ場到達"] for k in _HKEYS):
        log("      ⚠ ザラ場到達分は『高値が指値に触れたら約定』とみなしている。"
            "実際は板の待ち行列があり触れただけでは約定しないことがあるので、"
            "この比率が高いほど H の数字は楽観側に寄る(板寄せ分は現実と一致)。")
    # 後方互換: 呼び出し側が dict["H"] を見るので、先頭バリアントを "H" にも入れる。
    _res = {"E": out["E"], "約定せず": nf} | {k: out[k] for k in _HKEYS}
    if "H" not in _res and _HKEYS:
        _res["H"] = out[_HKEYS[0]]
        nf["H"] = nf[_HKEYS[0]]
    _res["_h_variants"] = _HKEYS
    # レポート(H ペイン)に出すための診断。コンソールのスクロールバックを
    # 探さなくても『どの銘柄がどの日に落ちたか』が画面で分かるようにする。
    _res["_diag"] = {"skip": dict(_sk), "skip_syms": _sk_syms,
                     "fill_mix": {k: dict(v) for k, v in _hfill.items()},
                     "n_base": len(base), "cap": _SK_CAP}
    return _res


def _mk(src, order_p, entry_p, exit_p, reason, exit_time,
        pc, atr, sm, tm, qty, key, stop_anchor=None, day_open=None):
    """元トレードをコピーして約定・決済まわりだけ差し替える。

    銘柄名・BT/WFスコア・ランク・設定ラベル等はそのまま残すので、
    明細のバッジや色が他タブと完全に一致する。
    """
    t = dict(src)
    t["entry_p"] = round(float(entry_p), 1)
    t["exit_p"] = round(float(exit_p), 1)
    t["order_limit"] = round(float(order_p), 1)
    _sa = float(stop_anchor if stop_anchor else entry_p or 0.0)
    t["order_stop"] = round(float(_sa + atr * sm), 1) if entry_p else 0.0
    t["order_target"] = round(float(_sa - atr * tm), 1) if entry_p else 0.0
    t["stop_price"] = t["order_stop"]
    t["target_price"] = t["order_target"]
    t["qty"] = qty
    t["reason"] = reason
    # ── 新しいフィルタ候補を測るための素材 (2026-08-14) ──────────────
    #   BT に代わる軸を探すため、**エントリー時点で分かる**属性だけを残す。
    #   h_fill : 板寄せ(寄りが指値以上=09:00約定) / ザラ場到達(日中に上がってきた)
    #            18.32 の実測でザラ場は板寄せの 1/4〜1/11 しかない。最有力の軸。
    #   h_gap_bp: 寄りが指値をどれだけ上回ったか(bp)。板寄せの中の強弱。
    #   h_atr_pct: ATR/建値(%)。損切り0.1ATR の実効幅そのもの。
    t["h_atr"] = float(atr or 0)
    if entry_p and float(entry_p) > 0:
        t["h_atr_pct"] = float(atr or 0) / float(entry_p) * 100.0
    if day_open is not None and order_p and float(order_p) > 0:
        _o1 = float(day_open)
        t["h_open"] = _o1
        t["h_fill"] = "板寄せ" if _o1 >= float(order_p) else "ザラ場到達"
        t["h_gap_bp"] = (_o1 - float(order_p)) / float(order_p) * 1e4
    if reason == "約定せず":
        # ⛔ h_fill は「寄りが指値以上か」で決めるので、不約定行では意味が無い。
        #    そのままにすると『ザラ場到達』と表示されて誤読される。
        t["h_fill"] = "不約定"
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
