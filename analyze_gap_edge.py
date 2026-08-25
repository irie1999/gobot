r"""analyze_gap_edge.py — 「上げて寄った銘柄は、その日 下げるか」を最も安全な形で測る

════════════════════════════════════════════════════════════════════════════
★★★ 事前宣言 (2026-08-25)。**測る前に書いた。以後この基準を動かさない。**
════════════════════════════════════════════════════════════════════════════

これまで負けてきた原因は「測ってから良かった方を採る」を繰り返したこと。
方式を4回(lss→E→H→J / 2026-08-10〜16)、パラメータを数十回。その多重検定を
帰無較正していないので、勝った設定が本物かノイズか区別がつかなくなった。

**今回は測る前に合格ラインを決め、それをこのファイルに埋め込む。**
結果を見てからここを書き換えたら、この測定は無効になる。

── 仮説 (1つだけ) ──────────────────────────────────────────
  **ギャップアップして寄った銘柄は、その日の日中(寄り→引け)に下げる。**

  ⚠ 2026-08-25 の第1回は「**シグナルが出た銘柄が**」という条件付きで測り、
    不合格だった(§18.52)。そのとき同時に測った対照群(シグナルなし 506,547銘柄日)
    が**同じ形をしており、TESTでは対照のほうが良い帯が3つ**あった。
    → **シグナルは何も足していない**ので、条件を外して仮説そのものを縮めた。
    これは基準を緩めたのではなく、**測定が「シグナルは不要」と示したため**。

── 測るもの ────────────────────────────────────────────────
  C = (D+1 の始値 − D+1 の終値) × 100株     ← ショート。**バリアを1つも持たない**
      sm / tm / delay / 資金均等 / 予算 / 発注順 / 選定 / **シグナル** が全部無い。
      だから **パラメータ選択のバイアスが構造的に入らない**。

── 実運用の形 (--exec auction) ─────────────────────────────
  前夜に「前日終値 × (1 + N/10000)」の **寄付指値**を空売りで置くだけ。
  ギャップが N を超えた日だけ **板寄せで約定 = 約定価格は始値**。
  板寄せは呼値を払わないので **エントリーの執行コストはゼロ**。
  引けの買い戻し(MOC)で片道1ティック(≒3.3bp)だけ払う。
  ⚠ 09:00 に板を読んでから発注する形(--exec confirm)は実測 **−30.9bp**
    (2026-08-24 / 5件)。J が負けていた主因はここ。

── 母集団 (lss を1ミリも通らない) ──────────────────────────
  6戦略(MACDTF/A7/RSI2/DON/VOLTF/MOM)の `entry_sig` が True の日 D をすべて。
  **lss が約定したかどうかと無関係**。ここが §18.50 で汚染された部分:
    - `run_limit_backtest` の trade_log は「発注中」を捨てる
    - `scan_lss_universe._scan_symbol` も `reason in ("発注中","保有中")` を捨てる
    - `eh_trades` は trades+nofills から拾うので、期限切れが落ちていた
  → **どれも「ギャップアップして前日終値まで戻らなかった日」= ショートに有利な日
    を系統的に落としていた**。本ツールはシグナルから直接作るので起こりえない。

── 対照群 (これが無いと何も言えない) ───────────────────────
  同じ銘柄の **シグナルが出ていない日**。同じギャップ帯で同じ計算。
  §18.19 で「無条件の日中ドリフトはゼロ」(604,626銘柄日 / t=0.59)と確定済み。
  だから対照がプラスに出たら、それは測定の誤りかレジームであって仮説の支持ではない。

════════════════════════════════════════════════════════════════════════════
★ 合格ライン — **5つ全部**を満たしたときだけ「土台がある」と言う
════════════════════════════════════════════════════════════════════════════

  ① bp/件 ≥ **執行コストの3倍**。--exec で自動的に決まる:
       auction (前夜 寄付指値) … 板寄せは呼値を払わない。引けMOCの片道 3.3bp だけ
                                  → **PASS_BP = 10.0**
       confirm (09:00に板を読んでから発注) … 実測 −30.9bp (2026-08-24 / 5件)
                                  → **PASS_BP = 35.0**
       ⚠ 第1回(§18.52)は confirm を前提にしながら 15.0 と置いた。**甘かった**。
         執行方式ごとに水準が変わるのは当然なので、**方式を引数にして測る前に
         紐付けた**。結果を見てから水準をいじることはしない。
       ⚠ 呼値は下限。実スプレッドはこれ以上。

  ② 日クラスタ頑健 t ≥ +2.0
       同じ日の全銘柄はその日の方向を共有するので、実効サンプルは
       件数ではなく **営業日数**(§18.13)。件数ベースの t は使わない。

  ③ **既見期間と未使用期間の両方**で ①② を満たす
       ★★ ここが第2回の肝。**日足は2020-09から6年ある**(5分足の2年制約は
          同日決済を5分足で判定していたときのもの。寄りで建てて引けで返すだけなら
          日足の始値・終値で足りる)。

            2024-03 〜 2026-08  … 第1回で見た = **既見**
            2020-09 〜 2024-03  … **一度も見ていない = 本物のホールドアウト(3.5年)**

          ⛔ 第1回で「75〜150bp が良い」という表を見てしまった以上、
             既見期間で N を選ぶのは in-sample。未使用期間が唯一の検証手段。
       ⚠ ホールドアウトは **上限で切る**(§18.25 の事故: 下限しか動かさず
         TRAIN ⊇ TEST になっていた)。窓を変えて数字が動くことを必ず確認する。
       ⚠ 2020-2021 はコロナ相場でレジームが違う。落ちたとき「レジームのせい」と
         言い訳しないために、**先に「落ちたら不合格」と決めておく**。

  ④ ギャップ帯で単調
       帯を7つ切れば偶然どれかは良く見える。**特定の帯を選ぶのは多重検定**。
       単調でなければ軸として機能していない(§18.31 の流動性・§18.38 の建値が
       まさにそうだった)。Spearman ≥ +0.7 を単調とみなす。

  ⑤ 帰無較正の95%点を超える
       帰無は **同じ日の中で帯ラベルをシャッフル**(日効果を完全に保つ / §18.48⑪)。
       日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなり、偽陽性を
       過小評価する(§18.13)。
       ⚠ 第1回の「対照群(シグナルなし)がゼロ」は、シグナルの寄与を測るための条件。
         シグナルを土台から外したので、この条件は役目を終えた。代わりに ③ を
         『既見 vs 未使用』に強化してある(条件は減っていない)。
         シグナル有無の比較は **参考として出し続ける**(§18.52 の再確認)。

  ⛔ 1つでも落ちたら **不合格。パラメータを足して救わない。**
     救おうとした瞬間に、これまでと同じ多重検定が始まる。

════════════════════════════════════════════════════════════════════════════
★ 試行の記録 — `gap_edge_trials.csv` に**自動で追記される**
════════════════════════════════════════════════════════════════════════════
  多重検定の補正には「何回試したか」が要る。これまで記録していなかったので
  補正できなかった(方式4回 / パラメータ数十回 / §18.51 B2)。
  **本ツールは実行のたびに1行書く。消さないこと。**
  10回試して1回通ったなら、その1回は帰無でも起こりうる(1 − 0.95^10 = 40%)。

════════════════════════════════════════════════════════════════════════════
⚠ このツールが測っていないもの (合格しても残る未知数)
════════════════════════════════════════════════════════════════════════════
  * スプレッド・執行遅延 — §18.44 の実測で 1分 −15.8bp / 5分 −36.6bp。
    C の bp から**引く**こと。バックテストでは永遠に確定しない。
  * 空売り在庫・貸株可否 — 今日のリストしか無い(§18.51 B5)。
  * 生存バイアス — 今日の上場銘柄しか見ていない(§18.51 B6)。
  * 決算・特別気配・IPO — 全部建てる前提。

════════════════════════════════════════════════════════════════════════════
★★ 第3回 (2026-08-25 / 回す前に宣言。以後 書き換えない)
════════════════════════════════════════════════════════════════════════════
  第2回も **全帯合計**で判定してしまった(母集団の39%がギャップダウン = 仮説に
  含まれない集団)。第1回は「閾値をどこに置いても両窓は通らない」と確認できたので
  判定は変わらなかったが、**第2回は変わる**(100〜150bp が未使用・既見の両方で
  bp≥9.9 かつ t≥2.0 を満たした)。だから閾値を持つ形を**初めて事前宣言して測る**。

  仮説   : 寄りが前日終値 **+100bp 以上**の銘柄を空売りし、**引けで買い戻す**と勝つ
  閾値   : **100bp 固定。スイープしない。**
           ⚠ これは第1回・第2回で見た表から決めた値。**既見期間では検証できない。**
  母集団 : シグナル不問(2回とも「シグナルの寄与ゼロ」と出た)
  執行   : 前夜に寄付指値 → 板寄せ約定 → 引けMOC。執行コスト 3.3bp
  検証   : **2015-05 〜 2020-09 (5.4年)。一度も見ていない。**
           第1回=2024-03以降 / 第2回=2020-09以降 しか見ていない。

  合格 (全部):
    ① bp/件 ≥ 9.9 (執行3.3bp×3)
    ② 日クラスタ頑健 t ≥ 2.0
    ④ **検証期間の前半・後半で両方プラス**(閾値ありだと帯が2つしか残らず
       単調性 ρ が意味をなさないので、④をこれに差し替える。条件は減っていない)
    ⑤ 帰無較正の95%点を超える

  ⛔ 落ちたら **終わり**。閾値を動かして再挑戦しない。
  ⛔ これを回すと **手持ちの未使用データは尽きる**。次は前向きに貯めるしかない。
  ⛔ 2015-2020 には **コロナショック(2020-03)** が入る。落ちたときに
     「相場が特殊だった」と言わない。先にそう決めた。

  コマンド:
    python analyze_gap_edge.py --workers 8 --days 4200 --min-gap-bp 100 \
                               --split 2020-09-01,2024-03-01

使い方
------
  ★ 第2回(ギャップだけを土台にする / 未使用期間で検証):
      python analyze_gap_edge.py --workers 8 --days 2100 --split 2024-03-01

  参考:
  python analyze_gap_edge.py --workers 8 --pool sig      # 第1回と同じ(シグナル限定)
  python analyze_gap_edge.py --workers 8 --exec confirm  # 09:00に板を読む形
  python analyze_gap_edge.py --workers 8 --limit 200     # デバッグ

⚠ `--days 2100` はキャッシュ(.rsi2_cache)がそのまま使える。fetch は
  (days+230)×1.5 日を取るので、第1回(--days 800)の時点で既に 2020-09 まで
  入っている。再ダウンロードは起きない。

⚠ 照会のみ。発注も設定変更もしない。日足だけで完結する(5分足不要)ので軽い。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# ── 事前宣言した合格ライン。**結果を見てから書き換えない** ──────────────
# ① は執行方式で決まる(執行コストの3倍)。方式は --exec で選ぶ。
EXEC_COST_BP = {
    # 前夜に寄付指値 → 板寄せ約定。エントリーは呼値を払わない。引けMOCの片道のみ
    "auction": 3.3,
    # 09:00に板を読んでから発注 → 実測 −30.9bp (2026-08-24 / 5件 / §18.48⑩)
    "confirm": 30.9,
}
PASS_T = 2.0          # ② 日クラスタ頑健 t の下限
PASS_RHO = 0.7        # ④ ギャップ帯の単調性 (Spearman)
TRIALS_CSV = "gap_edge_trials.csv"   # 試行の記録(多重検定の補正に使う)

# ギャップ帯 (bp)。J の合格判定 +75bp をまたぐように切ってある。
GAP_EDGES = [(-1e9, 0.0), (0.0, 25.0), (25.0, 50.0), (50.0, 75.0),
             (75.0, 100.0), (100.0, 150.0), (150.0, 1e9)]

ap = argparse.ArgumentParser(
    description="ギャップアップ後の日中ショートに地力があるかを、lss を通さず測る")
ap.add_argument("--days", type=int, default=2100,
                help="遡及日数(既定2100=約6年。キャッシュがそのまま使える)")
ap.add_argument("--split", type=str, default="2024-03-01",
                help="期間の境界 yyyy-MM-dd。**カンマ区切りで複数可**"
                     "(例 2020-09-01,2024-03-01 → 3期間)。"
                     "**判定は最も古い窓に対して行う**(そこが未使用期間)。空なら分割しない")
ap.add_argument("--min-gap-bp", type=float, default=0.0,
                help="判定対象をこのギャップ以上に絞る(bp)。"
                     "0=絞らない。**仮説が『ギャップアップした銘柄』なら必ず指定する**"
                     "(第1回・第2回はこれを付けず、母集団の39%%を占める"
                     "ギャップダウンを混ぜて薄めていた)")
ap.add_argument("--pool", choices=["all", "sig", "nosig"], default="all",
                help="判定に使う母集団。all=シグナル不問(第2回の土台) / "
                     "sig=シグナルが出た日だけ(第1回と同じ) / nosig=出ていない日だけ")
ap.add_argument("--exec", dest="exec_mode", choices=["auction", "confirm"],
                default="auction",
                help="執行方式。auction=前夜に寄付指値(板寄せ約定・コスト3.3bp) / "
                     "confirm=09:00に板を読んでから発注(実測30.9bp)。"
                     "**合格ライン①がこれで決まる**")
ap.add_argument("--min-price", type=float, default=1000.0, help="建値の下限")
ap.add_argument("--max-price", type=float, default=6000.0, help="建値の上限")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部)")
ap.add_argument("--seeds", type=int, default=200, help="帰無較正の本数")
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--out", type=str, default="", help="銘柄日の明細をCSVに書く")
ap.add_argument("--note", type=str, default="",
                help="試行記録(gap_edge_trials.csv)に残すメモ。何を試したのか")
ap.add_argument("--no-refetch", action="store_true",
                help="キャッシュが --days ぶん遡っていなくても再ダウンロードしない。"
                     "⛔ 古い窓のデータが欠けたまま測ることになる(第3回の事故)")
ap.add_argument("--min-density", type=float, default=0.10,
                help="判定窓の1日あたり銘柄日数が、最も濃い窓の何倍を下回ったら"
                     "『測定不能』にするか(既定0.10=1/10)")
a = ap.parse_args()

# ① の水準は執行方式で決まる(執行コストの3倍)。**測る前に紐付けてある**
EXEC_BP = EXEC_COST_BP[a.exec_mode]
PASS_BP = round(EXEC_BP * 3.0, 1)

import backtest_limit_entry as ble                       # noqa: E402
from daytrade_data import available_local_symbols        # noqa: E402
from sameday5m_core import mod_for                       # noqa: E402

# キャッシュにこの日まで遡って入っていることを要求する。足りなければ再ダウンロード。
# ⚠ これを渡さないと fetch はキャッシュの開始日を見ない(上の _scan のコメント参照)。
from datetime import timedelta as _td                    # noqa: E402
_MIN_START = None
if not a.no_refetch:
    _MIN_START = (ble.datetime.now(ble.JST).date() - _td(days=a.days + 420))

# エンジンのモードグローバルは触らせない(pnl は自前で日足から計算する)
ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._INTRADAY_5M = False

STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _jq_to_yf(code: str) -> str:
    c = str(code).strip()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


def _scan(sym: str) -> list[dict]:
    """1銘柄の全営業日について (ギャップ, 日中ショート損益, シグナル有無) を返す。

    ⛔ lss のバックテスト(run_limit_backtest)を**呼ばない**。日足のシグナル列だけを
       読むので、「lss が約定したか」は母集団に一切影響しない。
    """
    try:
        # ⛔ min_start_date を渡さないと、fetch は **キャッシュの開始日を一切見ずに**
        #   返す(backtest_limit_entry.py:456 の else 枝)。--days をいくら伸ばしても
        #   キャッシュが2020年始まりならそのまま使われる。
        #   2026-08-25 の第3回はこれで判定窓が 1日1.3銘柄になり、判定不能だった。
        df = ble.fetch(sym, a.days + 420, min_start_date=_MIN_START)
    except Exception:
        return []
    if df is None or len(df) < 250:
        return []
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()

    # 各戦略のシグナル日を集める(和集合。どれか1つでも出ていればシグナル日)
    sig_days: dict[pd.Timestamp, list[str]] = {}
    for st in STRATS:
        try:
            mod = mod_for(st)
            params = getattr(mod, "STRATEGY_PARAMS", {}).get(st)
            if not params:
                continue
            cf = params[0]
            d = cf(df.copy())
        except Exception:
            continue
        if "entry_sig" not in d.columns:
            continue
        try:
            for ts in d.index[d["entry_sig"].fillna(False).astype(bool)]:
                sig_days.setdefault(pd.Timestamp(ts).normalize(), []).append(st)
        except Exception:
            continue

    # 全営業日について D → D+1 を測る。シグナル日かどうかはフラグで持つだけ。
    out: list[dict] = []
    _idx = df.index
    _cut = _idx[-1] - pd.Timedelta(days=a.days)
    for pos in range(1, len(_idx) - 1):
        d0 = _idx[pos]                    # D  (シグナル判定日 = 終値で判定)
        if d0 < _cut:
            continue
        d1 = _idx[pos + 1]                # D+1(建てる日)
        try:
            pc = float(df["close"].iloc[pos])
            o1 = float(df["open"].iloc[pos + 1])
            c1 = float(df["close"].iloc[pos + 1])
        except Exception:
            continue
        if not (pc > 0 and o1 > 0 and c1 > 0):
            continue
        # 建値(= D+1 の始値)で価格フィルタ。実運用と同じく「その日いくらで建てるか」
        if o1 < a.min_price or o1 > a.max_price:
            continue
        _st = sig_days.get(d0, [])
        out.append({
            "date": str(d1.date()),
            "symbol": sym,
            "entry_p": o1,
            "gap_bp": (o1 - pc) / pc * 10_000.0,
            "pnl": (o1 - c1) * a.qty,          # C: 寄りで売って引けで買い戻す
            "sig": 1 if _st else 0,
            "strats": ",".join(sorted(set(_st))),
        })
    return out


def _band(g: float) -> str:
    for lo, hi in GAP_EDGES:
        if lo <= g < hi:
            return ("< 0" if hi == 0.0 else
                    f"{int(lo)}〜" if hi > 1e8 else f"{int(lo)}〜{int(hi)}")
    return "?"


def _cluster_t(sub: pd.DataFrame) -> float:
    """日クラスタ頑健 t。実効サンプル = 営業日数(件数ではない / §18.13)。"""
    if sub.empty:
        return 0.0
    dm = sub.groupby("date")["pnl"].mean()
    if len(dm) < 2:
        return 0.0
    sd = dm.std(ddof=1)
    if not (sd == sd) or sd <= 0:
        return 0.0
    return float(dm.mean() / (sd / (len(dm) ** 0.5)))


def _bp(sub: pd.DataFrame) -> float:
    if sub.empty:
        return 0.0
    return float((sub["pnl"] / (sub["entry_p"] * a.qty) * 10_000.0).mean())


def _spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0

    def _rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        for pos, i in enumerate(order):
            rk[i] = float(pos)
        return rk

    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return float(num / (dx * dy)) if dx > 0 and dy > 0 else 0.0


def _band_table(r: pd.DataFrame, title: str) -> list[tuple[str, int, float, float, float]]:
    """ギャップ帯別の表を出し、(帯, 件数, 円/件, bp, t) を返す。"""
    print(f"\n  {title}")
    print(f"    {'ギャップ(bp)':<13}{'件数':>9}{'円/件':>10}{'bp/件':>9}{'日t':>8}")
    print("    " + "-" * 49)
    rows = []
    for lo, hi in GAP_EDGES:
        lbl = ("< 0" if hi == 0.0 else
               f"{int(lo)}〜" if hi > 1e8 else f"{int(lo)}〜{int(hi)}")
        sub = r[r["band"] == lbl]
        if len(sub) < 30:                 # 30件未満は出さない(読み違えのもと)
            continue
        _b, _t = _bp(sub), _cluster_t(sub)
        rows.append((lbl, len(sub), float(sub["pnl"].mean()), _b, _t))
        print(f"    {lbl:<13}{len(sub):>9,}{sub['pnl'].mean():>+10,.0f}"
              f"{_b:>+9.1f}{_t:>+8.2f}")
    return rows


def _null_calib(r: pd.DataFrame, hi_lbl: str, lo_lbl: str) -> tuple[float, float, float]:
    """帰無較正: **同じ日の中で** 帯ラベルをシャッフル(日効果を完全に保つ)。

    ⛔ 日をまたぐシャッフルは日内相関を壊し、帰無分布が狭くなって偽陽性を
       過小評価する(§18.13)。日ブロックは必ず保つ。
    """
    import random as _rnd
    rng = _rnd.Random(a.seed)
    obs_hi = r[r["band"] == hi_lbl]
    obs_lo = r[r["band"] == lo_lbl]
    obs = _bp(obs_hi) - _bp(obs_lo)
    groups = [g for _, g in r.groupby("date")]
    spreads = []
    for _ in range(max(1, a.seeds)):
        hi_v, lo_v = [], []
        for g in groups:
            bands = list(g["band"])
            rng.shuffle(bands)
            _bpv = (g["pnl"] / (g["entry_p"] * a.qty) * 10_000.0).tolist()
            for bnd, v in zip(bands, _bpv):
                if bnd == hi_lbl:
                    hi_v.append(v)
                elif bnd == lo_lbl:
                    lo_v.append(v)
        if hi_v and lo_v:
            spreads.append(sum(hi_v) / len(hi_v) - sum(lo_v) / len(lo_v))
    if not spreads:
        return obs, 0.0, 0.0
    spreads.sort()
    p95 = spreads[min(len(spreads) - 1, int(len(spreads) * 0.95))]
    med = spreads[len(spreads) // 2]
    return obs, med, p95


# ── 実行 ────────────────────────────────────────────────────────────────
_seen, universe = set(), []
for s in available_local_symbols():
    y = _jq_to_yf(s)
    if y not in _seen:
        _seen.add(y)
        universe.append(y)
if a.limit > 0:
    universe = universe[:a.limit]
if not universe:
    sys.exit("[error] stock_5min に銘柄がありません(available_local_symbols が空)")

_POOL_LBL = {"all": "シグナル不問(ギャップだけが土台)",
             "sig": "シグナルが出た日だけ(第1回と同じ)",
             "nosig": "シグナルが出ていない日だけ"}
_EXEC_LBL = {"auction": "前夜に寄付指値 → 板寄せ約定(エントリーの呼値なし)",
             "confirm": "09:00に板を読んでから発注(実測 −30.9bp)"}
print(f"[info] 母集団 {len(universe):,}銘柄 / 遡及{a.days}日 / "
      f"建値 {a.min_price:,.0f}〜{a.max_price:,.0f}円 / {a.qty}株")
print(f"[info] 判定する母集団 = **{_POOL_LBL[a.pool]}**")
print(f"[info] 執行方式 = {a.exec_mode} ({_EXEC_LBL[a.exec_mode]}) / "
      f"執行コスト {EXEC_BP:.1f}bp")
print(f"[info] 測るもの = C(寄りで売って引けで買い戻す)。"
      f"**sm/tm/delay/資金均等/予算/発注順/選定を1つも持たない**")
print(f"[info] 母集団は **シグナルから直接**作る(lss のバックテストを通らない / §18.50)")
print(f"[info] 合格ライン(事前宣言・不変): bp≥{PASS_BP}(=執行{EXEC_BP:.1f}bp×3) / "
      f"t≥{PASS_T} / 単調ρ≥{PASS_RHO} / 帰無95%点超え / **既見・未使用 両方**")

_rows: list[dict] = []
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    futs = {ex.submit(_scan, s): s for s in universe}
    for i, f in enumerate(as_completed(futs), 1):
        try:
            _rows.extend(f.result() or [])
        except Exception:
            pass
        if i % 200 == 0:
            print(f"  … {i}/{len(universe)}銘柄 / {len(_rows):,}銘柄日", flush=True)

if not _rows:
    sys.exit("[error] 1件も集まりませんでした")
r_all = pd.DataFrame(_rows)
r_all["band"] = r_all["gap_bp"].map(_band)
if a.out:
    r_all.to_csv(a.out, index=False, encoding="utf-8-sig")
    print(f"[info] 明細を書きました: {a.out} ({len(r_all):,}行)")

_sig = r_all[r_all["sig"] == 1]
_ctl = r_all[r_all["sig"] == 0]
print(f"\n{'=' * 78}")
print(f"■ 母集団 — シグナル {len(_sig):,}銘柄日 / 対照 {len(_ctl):,}銘柄日 / "
      f"{r_all['date'].nunique():,}営業日")
print(f"{'=' * 78}")
print(f"  ⚠ シグナル有無は **参考**(§18.52 で寄与ゼロと出た)。判定は --pool = "
      f"{a.pool} に対して行う")


def _pool_of(w: pd.DataFrame) -> pd.DataFrame:
    """判定対象。--pool でシグナル有無、--min-gap-bp でギャップ閾値を掛ける。"""
    if a.pool == "sig":
        w = w[w["sig"] == 1]
    elif a.pool == "nosig":
        w = w[w["sig"] == 0]
    if a.min_gap_bp > 0:
        w = w[w["gap_bp"] >= a.min_gap_bp]
    return w


# ── 期間分割。**上限で切る**(§18.25) ───────────────────────────────
#    --split はカンマ区切りで複数可。判定は **最も古い窓**(=未使用期間)に対して行う。
_windows = [("全期間", r_all)]
_judge_win = "全期間"
_DENSITY_FAIL = ""      # 空でなければ「データが入っていない」= 判定を出さない
if a.split:
    _cuts = [str(pd.Timestamp(x.strip()).date()) for x in a.split.split(",") if x.strip()]
    _cuts.sort()
    _bounds = [None] + _cuts + [None]
    _segs = []
    for _i in range(len(_bounds) - 1):
        _lo_b, _hi_b = _bounds[_i], _bounds[_i + 1]
        _m = pd.Series(True, index=r_all.index)
        if _lo_b is not None:
            _m &= r_all["date"] >= _lo_b
        if _hi_b is not None:
            _m &= r_all["date"] < _hi_b
        _seg = r_all[_m]
        if _seg.empty:
            continue
        _nm = (f"{str(_seg['date'].min())[:7]}〜{str(_seg['date'].max())[:7]}")
        _segs.append((_nm, _seg))
    if _segs:
        _judge_win = _segs[0][0]          # 最も古い = 未使用期間
        print(f"\n  ★★ **判定するのは最も古い窓だけ** = {_judge_win} "
              f"({_segs[0][1]['date'].nunique():,}営業日)")
        _dens = []
        for _i, (_nm, _seg) in enumerate(_segs):
            _tag = "★ 未使用(判定対象)" if _i == 0 else "既見(参考)"
            _d = len(_seg) / max(1, _seg["date"].nunique())
            _dens.append(_d)
            print(f"     {_nm}  {_seg['date'].nunique():>5,}営業日  "
                  f"{len(_seg):>9,}銘柄日  {_d:>8,.1f}銘柄/日   {_tag}")
        # ⛔ データ欠落を『不合格』と報告しないための門番。
        #   2026-08-25 の第3回は判定窓が 1.3銘柄/日(他窓の1/900)で、
        #   中身が空なのに「不合格」と出た。相場ではこの差は出ない。
        _dmax = max(_dens) if _dens else 0.0
        if _dmax > 0 and _dens[0] / _dmax < a.min_density:
            _DENSITY_FAIL = (f"判定窓の密度 {_dens[0]:.1f}銘柄/日 は "
                             f"最も濃い窓 {_dmax:.1f}銘柄/日 の "
                             f"{_dens[0] / _dmax * 100:.1f}% しかない")
            print(f"\n  ⛔⛔ **{_DENSITY_FAIL}**")
            print(f"      相場でこの差は出ない = **データが入っていない**。")
            print(f"      --no-refetch を外して再実行し、キャッシュを "
                  f"{a.days + 420}日ぶん遡らせること。")
            print(f"      この実行の判定は **測定不能** として記録する"
                  f"(不合格ではない)。")
        print(f"  ⚠ ホールドアウトは **上限で切る**(§18.25 の事故)")
        print(f"  ⚠ 落ちたときに『相場が特殊だった』と言わないこと(宣言済み)")
        _windows += _segs

_verdict: dict[str, dict] = {}
for _wname, _w in _windows:
    if _w.empty:
        continue
    print(f"\n{'=' * 78}\n■ {_wname}\n{'=' * 78}")
    _wp = _pool_of(_w)                       # 判定対象(--pool)
    _rows_pool = _band_table(_wp, f"★ 判定対象: {_POOL_LBL[a.pool]} ({len(_wp):,}銘柄日)")
    # 参考: シグナル有無で分けた表(§18.52 の再確認。判定には使わない)
    if a.pool == "all":
        _ws, _wc = _w[_w["sig"] == 1], _w[_w["sig"] == 0]
        if not _ws.empty:
            _band_table(_ws, f"参考: シグナルあり ({len(_ws):,}銘柄日)")
        if not _wc.empty:
            _band_table(_wc, f"参考: シグナルなし ({len(_wc):,}銘柄日)")
    # 単調性(帯の並び vs bp)
    _rho = 0.0
    if len(_rows_pool) >= 3:
        _xs = list(range(len(_rows_pool)))
        _rho = _spearman([float(x) for x in _xs], [x[3] for x in _rows_pool])
    _all_bp, _all_t = _bp(_wp), _cluster_t(_wp)
    print(f"\n    {'★ 判定対象 合計':<22}{len(_wp):>9,}"
          f"{_wp['pnl'].mean():>+10,.0f}{_all_bp:>+9.1f}{_all_t:>+8.2f}")
    # ④ 閾値ありだと帯が2つしか残らず ρ が意味をなさない。前半/後半の符号一致に差し替え
    _h1_bp = _h2_bp = 0.0
    if a.min_gap_bp > 0 and not _wp.empty:
        _ds = sorted(_wp["date"].unique())
        _mid = _ds[len(_ds) // 2]
        _h1, _h2 = _wp[_wp["date"] < _mid], _wp[_wp["date"] >= _mid]
        _h1_bp, _h2_bp = _bp(_h1), _bp(_h2)
        print(f"    前半({str(_ds[0])[:7]}〜) {_bp(_h1):+.1f}bp / "
              f"後半({str(_mid)[:7]}〜) {_bp(_h2):+.1f}bp   ← ④ 両方プラスが合格")
    else:
        print(f"    単調性 Spearman = {_rho:+.2f} (合格 ≥ {PASS_RHO})")
    _verdict[_wname] = {"bp": _all_bp, "t": _all_t, "rho": _rho,
                        "h1": _h1_bp, "h2": _h2_bp,
                        "n": len(_wp), "rows": _rows_pool}

# ── 帰無較正 ──────────────────────────────────────────────────────
#   閾値ありのとき: **判定窓の中で**「閾値以上」vs「ギャップダウン」を比べる。
#     ⚠ 判定窓に揃えないと、既見期間の数字で較正することになる。
#   閾値なしのとき: 全期間の最上位帯 vs 最下位帯 (第1回・第2回と同じ)。
_null = None
_pool_all = _pool_of(r_all)
_wdict = dict(_windows)
if a.min_gap_bp > 0:
    _nsrc = _wdict.get(_judge_win, r_all)
    if a.pool == "sig":
        _nsrc = _nsrc[_nsrc["sig"] == 1]
    elif a.pool == "nosig":
        _nsrc = _nsrc[_nsrc["sig"] == 0]
    _nsrc = _nsrc.copy()
    _hi, _lo = f"≥{int(a.min_gap_bp)}bp", "< 0"
    _nsrc["band"] = _nsrc["gap_bp"].map(
        lambda g: _hi if g >= a.min_gap_bp else (_lo if g < 0 else "中間"))
    _nwin = _judge_win
else:
    _nsrc = _pool_all
    _rws = _verdict.get("全期間", {}).get("rows", [])
    _hi, _lo = (_rws[-1][0], _rws[0][0]) if len(_rws) >= 2 else ("", "")
    _nwin = "全期間"
if _hi and _lo and not _nsrc.empty:
    print(f"\n{'=' * 78}\n■ 帰無較正 — 同じ日の中で帯ラベルをシャッフル ({a.seeds}本)\n{'=' * 78}")
    print(f"  ⛔ 日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなり、"
          f"偽陽性を過小評価する(§18.13)")
    print(f"  対象窓 = {_nwin} ({_nsrc['date'].nunique():,}営業日 / {len(_nsrc):,}銘柄日)")
    _obs, _med, _p95 = _null_calib(_nsrc, _hi, _lo)
    print(f"\n  スプレッド『{_hi}』−『{_lo}』")
    print(f"    実測      {_obs:+8.1f} bp")
    print(f"    帰無 中央 {_med:+8.1f} bp   ← 0 中心とは限らない。ここと比べること")
    print(f"    帰無 95%  {_p95:+8.1f} bp")
    _null = (_obs, _med, _p95)
    print(f"    → {'✅ 帰無の95%点を超えた' if _obs > _p95 else '⛔ 帰無の中。軸として機能していない'}")

# ── 判定 (事前宣言した条件。ここを書き換えたらこの測定は無効) ──────────
print(f"\n{'=' * 78}\n■ 判定 — 事前宣言した条件\n{'=' * 78}")
print(f"  母集団={a.pool} / 執行={a.exec_mode}({EXEC_BP:.1f}bp) / "
      f"①の水準は執行コストの3倍 = {PASS_BP}bp")
if a.min_gap_bp > 0:
    print(f"  ★ 判定は **最も古い窓({_judge_win}) だけ**。他は既見なので参考")
_need = [_judge_win] if a.min_gap_bp > 0 else (
    [w for w, _ in _windows if w != "全期間"] or ["全期間"])
_pass = []
for _k in _need:
    v = _verdict.get(_k)
    if not v:
        _pass.append((f"{_k}", False, "窓が空"))
        continue
    _pass.append((f"① {_k} bp/件 ≥ {PASS_BP}", v["bp"] >= PASS_BP, f"{v['bp']:+.1f}bp"))
    _pass.append((f"② {_k} 日クラスタ t ≥ {PASS_T}", v["t"] >= PASS_T, f"t={v['t']:+.2f}"))
    if a.min_gap_bp > 0:
        _pass.append((f"④ {_k} 前半・後半とも プラス",
                      v["h1"] > 0 and v["h2"] > 0,
                      f"前半 {v['h1']:+.1f} / 後半 {v['h2']:+.1f}bp"))
    else:
        _pass.append((f"④ {_k} 単調 ρ ≥ {PASS_RHO}", v["rho"] >= PASS_RHO,
                      f"ρ={v['rho']:+.2f}"))
if _null:
    _pass.append(("⑤ 帰無の95%点を超える", _null[0] > _null[2],
                  f"{_null[0]:+.1f} vs {_null[2]:+.1f}bp"))
else:
    _pass.append(("⑤ 帰無較正", False, "計算できず"))

for _lbl, _ok, _det in _pass:
    print(f"  {'✅' if _ok else '⛔'} {_lbl:<34}{_det}")
_ok_all = all(x[1] for x in _pass)
_result = "合格" if _ok_all else "不合格"
if _DENSITY_FAIL:
    _result = "測定不能"          # データ欠落を「不合格」と数えない
print(f"\n  {'=' * 60}")
if _DENSITY_FAIL:
    print(f"  ⛔⛔ **測定不能。上の判定は読まないこと。**")
    print(f"     {_DENSITY_FAIL}")
    print(f"     判定窓にデータが入っていないので、①②が落ちるのは当たり前。")
    print(f"     **これは仮説の不合格ではない。** 試行回数にも数えない。")
    print(f"     → キャッシュを遡らせて測り直すこと(--no-refetch を付けない)。")
elif _ok_all:
    print(f"  ✅ **全条件 合格。土台がある。**")
    print(f"     ⛔ ここでパラメータ(sm/tm/delay/予算)を足して最適化しないこと。")
    print(f"        足した瞬間に、これまでと同じ多重検定が始まる。")
    print(f"     次は **前向きに小ロットで回して実約定を測る**(バックテストはここまで)。")
else:
    print(f"  ⛔ **不合格。この方向は追わない。**")
    print(f"     パラメータを足して救おうとしないこと。救おうとした瞬間に")
    print(f"     『測ってから良かった方を採る』が再開する(方式4回・パラメータ数十回の再現)。")
print(f"  {'=' * 60}")

# ── 試行の記録。多重検定の補正に使う。**消さないこと** ────────────────
try:
    import csv as _csv
    from datetime import datetime as _dtn
    from pathlib import Path as _Pth
    # ⚠ 列は **固定**。判定窓の名前で列名を作ると、窓が変わったときヘッダと
    #   値がズレる(第2回=未使用/既見・第3回=年月レンジ)。
    _HDR = ["実行時刻", "母集団", "執行", "閾値bp", "PASS_BP", "遡及日", "境界",
            "判定窓", "件数", "判定bp", "判定t", "判定④", "帰無実測", "帰無95",
            "判定", "メモ"]
    _tp = _Pth(TRIALS_CSV)
    _old_hdr = None
    if _tp.exists():
        try:
            with open(_tp, encoding="utf-8-sig") as _rh:
                _old_hdr = next(_csv.reader(_rh), None)
        except Exception:
            pass
    _jw = _need[0] if _need else "全期間"
    _v0 = _verdict.get(_jw, {})
    _c4 = (f"前半{_v0.get('h1', 0):+.1f}/後半{_v0.get('h2', 0):+.1f}"
           if a.min_gap_bp > 0 else f"ρ={_v0.get('rho', 0):+.2f}")
    with open(_tp, "a", newline="", encoding="utf-8-sig") as _fh:
        _w = _csv.writer(_fh)
        if _old_hdr != _HDR:
            _w.writerow(_HDR)
        _w.writerow([_dtn.now().strftime("%Y-%m-%d %H:%M:%S"), a.pool, a.exec_mode,
                     a.min_gap_bp, PASS_BP, a.days, a.split or "-",
                     _jw, len(_pool_all),
                     f"{_v0.get('bp', 0):.1f}", f"{_v0.get('t', 0):.2f}", _c4,
                     f"{_null[0]:.1f}" if _null else "",
                     f"{_null[2]:.1f}" if _null else "",
                     _result,
                     (a.note or (f"⛔ {_DENSITY_FAIL}" if _DENSITY_FAIL else ""))])
    # ヘッダ行を除き、**測定不能は数えない**(データ欠落は仮説を試したことにならない)
    with open(_tp, encoding="utf-8-sig") as _rh:
        _n_trials = sum(1 for _r in _csv.reader(_rh)
                        if _r and _r[0] != "実行時刻" and "測定不能" not in _r)
    print(f"\n  [試行記録] {TRIALS_CSV} に追記 — **通算 {_n_trials} 回目**")
    if _n_trials >= 3:
        _fp = 1.0 - (0.95 ** _n_trials)
        print(f"    ⚠ {_n_trials}回試すと、**中身がランダムでも {_fp*100:.0f}% の確率で"
              f"どれか1つは 5% 水準を通る**。")
        print(f"       合格が出ても、この回数を必ず併記すること(§18.51 B2 の反省)。")
except Exception as _e:
    print(f"\n  [warn] 試行記録を書けませんでした: {_e}")

print(f"""
{'=' * 78}
■ このツールが測っていないもの (合格しても残る未知数)
{'=' * 78}
  * スプレッド・執行遅延 — §18.44 実測 1分 −15.8bp / 3分 −29.4bp / 5分 −36.6bp。
    しかも 60〜71%が不利側。**上の bp から引くこと。**
  * 空売り在庫・貸株可否 — 今日のリストしか無い
  * 生存バイアス — 今日の上場銘柄しか見ていない
  * 決算・特別気配・IPO — 全部建てる前提
""")
