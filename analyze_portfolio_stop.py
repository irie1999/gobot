r"""analyze_portfolio_stop.py — **ポートフォリオ全体**の損切りを 5分足で検証する。

なぜこれが今までと違うのか (2026-08-28 ユーザー発案)
----------------------------------------------------
これまで否定してきたのは **個別の損切り**:

  ・ATR倍 (sm/tm)      … 滑り0.5%で24セル全滅。分岐点 2.6ティック (§18.55)
  ・固定円 −10,000円/件 … 保険料。平均 −61円/件 でテールを買う (§18.56 ②)

個別が負ける理由は「**銘柄固有のノイズで刈られる**」から。
ところが **合計で判定すると銘柄固有のノイズは打ち消し合う**(12銘柄なら √12 で薄まる)。
残るのは **相場全体の動き**だけ。

そして §18.60 で測ったとおり、**大負けの日は相場**:
  ・大負け日の **81%** が「日経が日中プラスの日」(全日 49%)
  ・同日の日経日中% で **R² 0.235**

**つまりこの案は、実際に見つかっている構造をそのまま狙い撃ちする。**

ヘッジ(§18.61)より優れている点
------------------------------
ヘッジは **毎日コストを払う**(分岐点 1.83bp に対し現実 4.4bp)ので棄却した。
ポートフォリオ損切りは **発火した日しかコストを払わない**。
同じ「予測せず事後で対処する」発想だが、コスト構造が根本的に違う。

⚠ 負ける筋も明確
----------------
§18.55 の決済時刻スイープ: 5分 +10.3bp → 11:05 +10.5bp → **引け +17.8bp**。
**N のエッジは『一日かけて下げ続けること』**なので、途中で降りると削る。

  → 問いは1つ: **悪い日は最後まで悪いのか、それとも戻すのか?**

本ツールはそれを直接出す(「発火した日、引けまで持っていたらどうだったか」)。

使い方
------
  # ① 実際に建てた明細を書き出す(日足の判定。数時間)
  python analyze_gap_edge.py --workers 8 --days 4200 --min-gap-bp 100 \
      --split 2020-09-01 --dump-picks picks.csv

  # ② 5分足でポートフォリオの経路を作り、閾値を掃く(軽い)
  python analyze_portfolio_stop.py --picks picks.csv

⛔ 5分足は 2024-07〜 の約2年しかない(J-Quants の分足は2年ローリング / §18.6)。
   TRAIN/TEST を分けると薄いので、**本ツールは日付で自前に分割する**
   (--split-date。既定は5分足がある期間の真ん中)。

⚠ 照会のみ。発注はしない。
"""
from __future__ import annotations

import argparse
import random as _rnd
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--picks", default="picks.csv",
                help="analyze_gap_edge --dump-picks が出した CSV")
ap.add_argument("--levels", default="20000,30000,50000,80000,100000,150000",
                help="ポートフォリオ損切りの水準(円 / 含み損がこれを超えたら全決済)")
ap.add_argument("--trail", default="",
                help="トレーリング版も掃く(その日の最大益から この額 下げたら全決済)。"
                     "例 30000,50000,80000。空で無効")
ap.add_argument("--split-date", default="",
                help="TRAIN/TEST の境界(yyyy-mm-dd)。空なら5分足のある期間の真ん中")
ap.add_argument("--budget", type=float, default=400.0,
                help="★ 予算(万円)。投入率(= その日の建玉 ÷ 予算)の分母。"
                     "picks.csv を作った analyze_gap_edge の --budget-man と"
                     "**必ず揃えること**")
ap.add_argument("--levels-pct", default="0.5,0.75,1.0,1.5,2.0",
                help="★★ **投入額比**の損切り(%%)。固定円は投入額で意味が"
                     "変わるので、③(満額のみ)が効く理由が『満額だから』なのか"
                     "『投入額比で浅いから』なのかを分離するために要る")
ap.add_argument("--arm-not-before", default="",
                help="★ 武装を **この時刻以降** に限る(例 09:10)。空なら建玉が"
                     "揃った時点から。⛔ 『10件目が建った瞬間』だと寄り直後の"
                     "最もボラが高い帯で発火する。時刻を足すと何が変わるかを"
                     "**推測せず測る**ためのつまみ(2026-09-04)")
ap.add_argument("--gate-nulls", type=int, default=200,
                help="⑥ の帰無較正の本数。**同じ日数をランダムに選んで**"
                     "同じ損切りを当て、『件数条件そのもの』が特別かを見る")
ap.add_argument("--worst-n", type=int, default=10,
                help="★ 大負け日の形を見るときの件数の下限(既定10件以上)")
ap.add_argument("--worst-k", type=int, default=20,
                help="★ その中で損益が下位いくつの日を見るか(既定20日)")
ap.add_argument("--worst-times",
                default="09:00,09:05,09:10,09:15,09:30,10:00,10:30,11:00,"
                        "11:30,12:30,13:00,14:00,14:30,15:00,15:20",
                help="★ 大負け日の断面を出す時刻")
ap.add_argument("--gate-n", default="0,4,6,8,10,12",
                help="★★ **N件以上 建てた日だけ**損切りを有効化する(件数の閾値)。"
                     "0 は全日(比較の基準)。投入率(--gate-pct)と違い、"
                     "予算を変えても『何件建てたか』は変わらないので直感的")
ap.add_argument("--gate-n-pcts", default="0.8,1.0",
                help="⑥ で使う損切り水準(投入額比%%)。件数×水準の総当たり")
ap.add_argument("--gate-pct", type=float, default=90.0,
                help="★★ **満額投資日だけ損切りを有効化**する閾値(投入率%%)。"
                     "無条件の損切りは『悪い日は戻す』で失敗した(§18.62)が、"
                     "満額の日だけなら保険料を払う日を絞れる")
ap.add_argument("--exit-next-open", action="store_true", default=True,
                help="★ 発火の**次のバーの始値**で決済する(既定ON)。検知は"
                     "5分足の終値なので、同じバーの終値で約定させると甘い")
ap.add_argument("--exit-same-close", dest="exit_next_open",
                action="store_false",
                help="旧挙動(発火したバーの終値で決済)に戻す。差を見るとき用")
ap.add_argument("--boot", type=int, default=1000,
                help="CVaR差の月ブロック・ブートストラップの本数(既定1000)")
ap.add_argument("--exit-times",
                default="09:15,09:30,10:00,11:30,13:00,14:00,15:00,15:15,15:20",
                help="★ **引け成行より早く畳んだらどうか** を掃く時刻。"
                     "§18.55 の決済時刻スイープは 5分→11:05→引け しか見ておらず、"
                     "**引け直前の10〜30分は一度も測っていない**")
ap.add_argument("--slip-pct", type=float, default=0.0005,
                help="発火時の決済スリッページ(0.0005=5bp)。全銘柄を成行で畳むので"
                     "個別損切りより不利に見積もる")
ap.add_argument("--max-dev", type=float, default=0.30,
                help="5分足と日足のズレの上限(0.30=30%%)。これを超えたら"
                     "**株式分割の汚染**(§18.27)としてその銘柄日を捨てる")
ap.add_argument("--min-cover", type=float, default=0.7,
                help="その日の建玉のうち何割が5分足で再現できれば採用するか"
                     "(0.7=7割)。これを割った日は『合計』として意味が無いので捨てる")
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--min-days", type=int, default=60,
                help="TRAIN/TEST に最低これだけの営業日が要る")
a = ap.parse_args()

try:
    from daytrade_data import load_intraday, split_by_day
except Exception as e:                                    # pragma: no cover
    sys.exit(f"[error] daytrade_data を読めません: {e}")

_LV = [float(x) for x in a.levels.split(",") if x.strip()]
_TR = [float(x) for x in a.trail.split(",") if x.strip()]
# ⛔ 使う場所より前で定義する。表の直前に置いていたら、その手前の
#   件数分布の print で NameError になった(2026-09-04)。
_LP = [float(x) for x in a.levels_pct.split(",") if x.strip()]
_GN = [int(x) for x in a.gate_n.split(",") if x.strip()]
_GNP = [float(x) for x in a.gate_n_pcts.split(",") if x.strip()]

try:
    P = pd.read_csv(a.picks)
except Exception as e:
    sys.exit(f"[error] {a.picks} を読めません: {e}\n"
             f"  先に: python analyze_gap_edge.py ... --dump-picks {a.picks}")
for _c in ("date", "symbol", "side", "entry_p", "qty", "pnl"):
    if _c not in P.columns:
        sys.exit(f"[error] {a.picks} に列 {_c} がありません")
P["date"] = P["date"].astype(str)

print(f"[info] 明細 {len(P):,}件 / {P['date'].nunique():,}営業日 / "
      f"{P['symbol'].nunique():,}銘柄")
print(f"[info] 日足ベースの合計損益 {P['pnl'].sum():+,.0f}円")
print(f"[info] 発火時のスリッページ {a.slip_pct * 100:.3f}%"
      f"(全銘柄を成行で畳むので個別損切りより不利に見積もる)")

# ── 5分足を読む ───────────────────────────────────────────────────
_syms = sorted(P["symbol"].astype(str).unique())
print(f"\n[5分足] {len(_syms):,}銘柄を読み込みます(ローカルのみ)…")


def _load(sym: str):
    try:
        df = load_intraday(sym, days=1200, source="local")
    except Exception:
        return sym, None
    if df is None or df.empty:
        return sym, None
    return sym, split_by_day(df)


_bars: dict = {}
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    _fs = {ex.submit(_load, s): s for s in _syms}
    for _i, _f in enumerate(as_completed(_fs), 1):
        try:
            _s, _d = _f.result()
        except Exception:
            continue
        if _d:
            _bars[_s] = _d
        if _i % 200 == 0:
            print(f"  … {_i:,}/{len(_syms):,}", flush=True)
print(f"[5分足] 読めた {len(_bars):,}/{len(_syms):,}銘柄")
if not _bars:
    sys.exit("[error] 5分足が1銘柄も読めません(stock_5min の場所を確認)")


# ── 日ごとにポートフォリオの含み損益の**経路**を作る ─────────────────
def _paths_for_day(day: str, grp: pd.DataFrame):
    """その日の (時刻グリッド, 含み損益の経路, 最終損益, 使えた件数) を返す。

    ⛔ 経路は **銘柄の合計**。個別のノイズは打ち消し合い、相場全体の動きが残る。
    ⚠ 5分足が無い銘柄はその日から丸ごと除外する(部分的に混ぜると合計が歪む)。
    """
    _d0 = pd.Timestamp(day).date()
    _series, _sero, _fin, _n = [], [], 0.0, 0
    # ★ (銘柄が寄った時刻, その建玉)。**5分足の最初のバー = その銘柄の寄り**。
    #   picks.csv に約定時刻は無いが、これが代理になる。累積建玉が予算の
    #   gate_pct% を超えた時刻が「実建玉が満額に達した瞬間」。
    #   ⛔ 「最終的に満額だった日は朝から守られていた」ことにしない。
    _ent: list = []
    for _r in grp.itertuples():
        _b = _bars.get(str(_r.symbol), {})
        _df = _b.get(_d0)
        if _df is None or len(_df) < 5:
            _MISS.append((day, str(_r.symbol)))
            continue
        _px = _df["close"].to_numpy(dtype="float64")
        _ts = _df.index
        # ⛔⛔ **株式分割の汚染ガード**(§18.27)。
        #   5分足(J-Quants)は保存時のままで、後から分割が起きても再調整されない。
        #   日足(yfinance)は遡及調整するので、分割銘柄では
        #   **5分足の価格が日足の 2〜10倍**になる。
        #   ショートだと (建値 − 5倍の価格) × 株数 で含み損が爆発し、
        #   予算400万に対して −1億 のような数字が出る(2026-08-28 に実際に出た)。
        #   picks.csv の d1_close(その日の日足終値)と突き合わせて弾く。
        #   ⚠ 2026-08-28: 最初は **日ごと捨てて**いたので 482日 → 193日 と
        #     6割が消えた。捨てすぎ。**その銘柄だけ外す**のが正しい。
        #     『損切りなし』の基準にも同じ銘柄を外して比べるので、
        #     比較は公平に保たれる(欠けた1銘柄ぶんの摂動が両側に等しく乗る)。
        _dc = float(getattr(_r, "d1_close", 0.0) or 0.0)
        if _dc > 0 and abs(float(_px[-1]) / _dc - 1.0) > a.max_dev:
            _BAD.append((day, str(_r.symbol), float(_px[-1]) / _dc))
            continue
        # 建値との整合も見る(寄りの5分足が建値から大きく離れていたら別物)
        if float(_r.entry_p) > 0 and \
                abs(float(_px[0]) / float(_r.entry_p) - 1.0) > a.max_dev:
            _BAD.append((day, str(_r.symbol),
                         float(_px[0]) / float(_r.entry_p)))
            continue
        # ショート(side=1) は 建値-現値、ロング(side=-1) は 現値-建値
        _sgn = 1.0 if int(_r.side) > 0 else -1.0
        _pl = (float(_r.entry_p) - _px) * _sgn * int(_r.qty)
        _series.append(pd.Series(_pl, index=_ts))
        # ★ **次のバーの始値**で決済したときの含み損益も作る。
        #   検知は5分足の終値、約定はその後 = 同じバーの終値だと甘い。
        #   どちらが実運用に近いかは板次第なので、両方出して差を見る。
        _po = _df["open"].to_numpy(dtype="float64")
        _sero.append(pd.Series((float(_r.entry_p) - _po) * _sgn * int(_r.qty),
                               index=_ts))
        _ent.append((_ts[0], float(_r.entry_p) * int(_r.qty)))
        _fin += float(_r.pnl)
        _n += 1
    # ⚠ 外しすぎた日は捨てる。**ポートフォリオの合計**を見るのが目的なので、
    #   半分以上欠けた日は『合計』として意味を持たない。
    if not _series or _n < max(1, int(len(grp) * a.min_cover)):
        return None
    # ⛔⛔ **bfill してはいけない**(2026-09-04 に Codex が指摘、実在した)。
    #   09:06 に寄る銘柄の系列は 09:00〜09:05 が NaN。bfill すると 09:06 の
    #   含み損益が 09:00 まで遡って埋まり、**まだ建てていない銘柄の損失**が
    #   朝の経路に混ざる。そこで損切りが発火すると「建てていない損を助けた」
    #   ことになり、強い好転が出る。私の ⑥10件 の +70,678 はこれが主因。
    #   → 建てる前は **損益ゼロ**。ffill は建てた後の欠測にだけ効かせる。
    _m = pd.concat(_series, axis=1).ffill().fillna(0.0)
    _mo = pd.concat(_sero, axis=1).ffill().fillna(0.0)
    _OPEN[day] = _mo.sum(axis=1).to_numpy(dtype="float64")
    # 累積建玉の推移(グリッド上)。「いま何円建っているか」= 武装判定の材料
    _ent.sort(key=lambda x: x[0])
    _idx = _m.index
    # ★ 遅寄り = その日の最初のバーより後に寄った銘柄。**bfill はここに効いていた**
    _LATE[0] += sum(1 for _t, _ in _ent if _t > _idx[0])
    _LATE[1] += len(_ent)
    _cum = np.zeros(len(_idx), dtype="float64")
    _cn = np.zeros(len(_idx), dtype="float64")
    _run, _rn = 0.0, 0
    for _t, _nt in _ent:
        _run += _nt
        _rn += 1
        _p = int(_idx.searchsorted(_t))
        if _p < len(_cum):
            _cum[_p:] = _run
            _cn[_p:] = _rn
    _CUMN[day] = _cn
    return _idx, _m.sum(axis=1).to_numpy(dtype="float64"), _fin, _n, _cum


_OPEN: dict = {}          # 次のバー始値ベースの経路
_NG_N: set = set()        # 建玉を再現しきれず判定不能にした日
_LATE = [0, 0]            # [遅寄りの銘柄日, 全銘柄日]
_CUMN: dict = {}          # 累積 **件数**(グリッド上)。件数ゲート用
_BAD: list = []                # 分割汚染などで弾いた (日, 銘柄, 倍率)
_MISS: list = []               # 5分足が無くて外した (日, 銘柄)

# 日ごとの建玉(スリッページの基準)。閾値×日 の二重ループで毎回計算しないよう先に持つ
_NOTIONAL = (P.assign(_n=P["entry_p"] * P["qty"])
             .groupby("date")["_n"].sum().to_dict())

print("\n[経路] ポートフォリオの含み損益を組み立てます…")
_days: dict = {}
_skip = 0
for _day, _g in P.groupby("date"):
    _p = _paths_for_day(_day, _g)
    if _p is None:
        _skip += 1
        continue
    _days[_day] = _p
print(f"[経路] 作れた {len(_days):,}営業日 / 除外 {_skip:,}日")
if _BAD:
    _bs = sorted({b[1] for b in _BAD})
    print(f"  ⛔ **5分足と日足のズレで弾いた {len(_BAD):,}銘柄日 / "
          f"{len(_bs):,}銘柄**(株式分割の汚染 / §18.27)")
    for _d, _sy, _rt in sorted(_BAD, key=lambda x: -abs(x[2]))[:5]:
        print(f"     {_d} {_sy} … 5分足/日足 = **{_rt:.2f}倍**")
    print(f"     ⚠ **その銘柄だけ**外します(日ごとは捨てない)。"
          f"基準の『損切りなし』にも同じ銘柄を外して比べるので比較は公平です")
if _MISS:
    print(f"  ⚠ 5分足が無くて外した {len(_MISS):,}銘柄日")

# ★ 自己検算: 経路の最終値の合計 は その日の pnl の合計 と一致するはず。
#   (最後の5分足の終値 ≒ 日足の終値なので)
_e = [abs(float(_v[1][-1]) - float(_v[2])) for _v in _days.values()]
_ee = float(np.mean(_e)) if _e else 0.0
print(f"  ★ 自己検算: 経路の最終値 vs 日足の損益 … 平均誤差 "
      f"**{_ee:,.0f}円/日**")
if _ee > 20000:
    print(f"     ⛔ **誤差が大きすぎます**。5分足と日足が別物を指している"
          f"可能性があります(--max-dev を下げて再実行)")
if len(_days) < a.min_days * 2:
    sys.exit(f"[error] 営業日が少なすぎます({len(_days)}日)。"
             f"5分足は 2024-07〜 の約2年しかありません(§18.6)")

_ks = sorted(_days)
_cut = a.split_date or _ks[len(_ks) // 2]
_tr = [k for k in _ks if k < _cut]
_te = [k for k in _ks if k >= _cut]
print(f"[分割] TRAIN {_tr[0]}〜{_tr[-1]} ({len(_tr)}日) / "
      f"TEST {_te[0]}〜{_te[-1]} ({len(_te)}日)  境界 {_cut}")
if min(len(_tr), len(_te)) < a.min_days:
    sys.exit(f"[error] どちらかの窓が {a.min_days}日 未満です。--split-date で調整を")


_BUD = a.budget * 1e4
# 投入率 = その日の建玉 ÷ 予算。satisfied なら「満額投資日」
_FULL = {d: (_NOTIONAL.get(d, 0.0) / _BUD * 100.0) for d in _days}


_NPOS = P.groupby("date").size().to_dict()     # その日 picks で建てた件数


def _clock_idx(day: str) -> int:
    """--arm-not-before で指定した時刻の最初のバー。未指定なら0。"""
    if not a.arm_not_before:
        return 0
    _idx = _days[day][0]
    _h, _m = (int(x) for x in str(a.arm_not_before).split(":"))
    for _i, _t in enumerate(_idx):
        if (_t.hour, _t.minute) >= (_h, _m):
            return _i
    return len(_idx)


def _armed_n(day: str, gate_n: int) -> int:
    """**N件目が建った時点**のバー。到達しなければ len(グリッド)。

    ⛔ _CUMN は 5分足で再現できた銘柄だけの累積なので、picks の件数より
      少ない。絶対数で切ると再現の悪い日が落ちる(_armed_idx と同じ罠)。
      → **建った割合**に直して切る。
    """
    _idx = _days[day][0]
    _cn = _CUMN.get(day)
    _np = int(_NPOS.get(day, 0))
    if _cn is None or len(_cn) == 0 or _np <= 0:
        return len(_idx)
    # ⛔⛔ **比例縮小してはいけない**(2026-09-04 指摘、実在した)。
    #   12件中8件しか再現できない日に gate_n=10 を 8×10/12=6.7 と読み替えると、
    #   **7件目で『10件到達』**にしてしまう。まだ10件建っていない。
    #   再現できないなら『いつ10件目が建ったか』は分からない → **その日は判定不能**。
    if float(_cn[-1]) < gate_n:
        # ⛔ **対象外の日と判定不能の日を混ぜない**(2026-09-04)。
        #   picks が gate_n 件に届かない日は単に条件を満たさないだけ。
        #   『再現できないので分からない』のは **picks は届いているのに
        #   5分足で再現しきれない日**だけ。混ぜると 473日中435日が
        #   判定不能に見えた(実際はほとんどが対象外)。
        if _np >= gate_n:
            _NG_N.add(day)
        return len(_idx)
    _w = np.where(_cn >= gate_n)[0]
    return max(int(_w[0]), _clock_idx(day)) if len(_w) else len(_idx)


def _armed_idx(day: str, gate: float = 0.0) -> int:
    """武装できる最初のバー。**建玉が揃うまで守られていない**。

    gate>0  … 累積建玉が **予算の gate%** に達した時点(満額条件)
    gate<=0 … その日の建玉が **全部入った**時点(無条件の腕)
    到達しなければ len(グリッド) を返す = その日は武装しない。

    ⛔⛔ ここが先読み排除の要。「最終的に満額だった日は朝から守られていた」に
      しない。累積建玉は **5分足の最初のバー = その銘柄が寄った時刻** から
      組み立てているので、遅寄りの銘柄はその時刻まで建玉に入らない。
    """
    _idx, _p, _f, _n, _cum = _days[day]
    if len(_cum) == 0:
        return len(_idx)
    # ⛔⛔ **予算に対する絶対額で切ってはいけない**(2026-09-04 に踏んだ)。
    #   _cum は 5分足が読めて汚染ガードを通った銘柄だけの累積なので、
    #   その日の建玉(_NOTIONAL)より必ず小さい(--min-cover 0.7 まで許容)。
    #   `_BUD * gate/100` で切ると、投入率が丁度90〜100%の日は3割欠けただけで
    #   届かなくなり、**満額日150日のうち69日しか対象にならなかった**。
    #   しかも残る日が「5分足が揃った日」に偏る。
    #   → 武装時刻は **再現できた建玉の中での割合**で決める。
    #     『その日が満額日か』の判定は _FULL(picks全体)で別に行う。
    # ⛔ 件数ゲートと同じ理由で、投入率も **絶対額** で切る。
    #   再現できた建玉が閾値に届かない日は「いつ到達したか」が分からない。
    if gate > 0:
        _need = _BUD * gate / 100.0
        if float(_cum[-1]) < _need:
            if _NOTIONAL.get(day, 0.0) >= _need:
                _NG_N.add(day)      # 条件は満たすのに再現できない日だけ
            return len(_idx)
    else:
        _need = float(_cum[-1])
    _w = np.where(_cum >= _need - 1e-6)[0]
    return max(int(_w[0]), _clock_idx(day)) if len(_w) else len(_idx)


def _apply(day: str, level: float = 0.0, trail: float = 0.0, start: int = 0):
    """その日にルールを当てたときの損益と、発火したかを返す。

    level … 含み損が -level を割ったら全決済
    trail … その日の最大益から trail 下げたら全決済
    ⚠ 発火は **その5分足の終値** で検知し、決済は **次の足の始値** ではなく
      同じ終値に slip を掛ける(次足始値は5分後で、実運用の成行より甘くなる)。
    """
    _ts, _path, _fin, _n, _cum = _days[day]
    if level <= 0 and trail <= 0:
        return _fin, False, -1
    _hit = -1
    if level > 0:
        # ⛔ start より前の含み損では発動させない(まだ建玉が揃っていない)
        _w = np.where(_path <= -level)[0]
        _w = _w[_w >= start]
        if len(_w):
            _hit = int(_w[0])
    if trail > 0:
        # ピークは最初から積む(武装前の含み益は実在する)が、発動は start 以降
        _peak = np.maximum.accumulate(_path)
        _w = np.where(_path <= _peak - trail)[0]
        _w = _w[_w >= start]
        if len(_w):
            _hit = int(_w[0]) if _hit < 0 else min(_hit, int(_w[0]))
    if _hit < 0:
        return _fin, False, -1
    # 発火時点の含み損益。スリッページは呼び出し側で建玉に対して引く
    return float(_path[_hit]), True, _hit


def _stat(days: list, level: float = 0.0, trail: float = 0.0,
          cap: float = 0.0):
    """窓全体の集計。cap は建玉(スリッページの基準)。"""
    _tot, _fire, _saved = 0.0, 0, 0.0
    _daily = []
    for _d in days:
        _ts, _path, _fin, _n, _cum = _days[_d]
        _v, _f, _i = _apply(_d, level, trail)
        if _f:
            # 建玉に対するスリッページ。全銘柄を成行で畳むので個別より不利に見積もる
            _v -= float(_NOTIONAL.get(_d, 0.0)) * a.slip_pct
            _fire += 1
            _saved += (_v - _fin)
        _tot += _v
        _daily.append((_d, _v))
    _m: dict = {}
    for _d, _v in _daily:
        _m[_d[:7]] = _m.get(_d[:7], 0.0) + _v
    _mv = np.array([_m[k] for k in sorted(_m)], float)
    _mu = float(_mv.mean()) if len(_mv) else 0.0
    _sd = float(_mv.std(ddof=1)) if len(_mv) > 1 else 0.0
    return {"tot": _tot, "fire": _fire, "saved": _saved,
            "mu": _mu, "sd": _sd, "ratio": (_mu / _sd if _sd else 0.0),
            "pos": int((_mv > 0).sum()), "nm": len(_mv),
            "worst": float(_mv.min()) if len(_mv) else 0.0}


def _judge(_s: dict, _b: dict) -> str:
    """✅ の判定。

    ⛔ 2026-08-28: `ratio > base * 1.10` と書いていたので、**base がマイナスの
       ときに不等号が反転**し、発火0回(=現行と完全に同一)の行に ✅ が付いた。
       比率ではなく **差** で比べ、発火が無い行は必ず — にする。
    """
    if _s["fire"] <= 0:
        return "  —"
    _d = _s["ratio"] - _b["ratio"]
    return "  ✅" if _d >= 0.10 else ""


def _table(days: list, label: str):
    print(f"\n  ── {label} ({len(days)}営業日) ──")
    _b = _stat(days)
    print(f"    {'ルール':<22}{'合計':>13}{'月平均':>11}{'月次σ':>11}"
          f"{'÷σ':>7}{'発火':>6}{'最悪月':>12}")
    print(f"    {'損切りなし ★現行':<22}{_b['tot']:>+13,.0f}{_b['mu']:>+11,.0f}"
          f"{_b['sd']:>11,.0f}{_b['ratio']:>7.2f}{'—':>6}"
          f"{_b['worst']:>+12,.0f}")
    _out = []
    for _l in _LV:
        _s = _stat(days, level=_l)
        _mk = _judge(_s, _b)
        print(f"    {'合計 −' + f'{_l:,.0f}円':<22}{_s['tot']:>+13,.0f}"
              f"{_s['mu']:>+11,.0f}{_s['sd']:>11,.0f}{_s['ratio']:>7.2f}"
              f"{_s['fire']:>6}{_s['worst']:>+12,.0f}{_mk}")
        _out.append(("level", _l, _s))
    for _t in _TR:
        _s = _stat(days, trail=_t)
        _mk = _judge(_s, _b)
        print(f"    {'最大益から −' + f'{_t:,.0f}円':<22}{_s['tot']:>+13,.0f}"
              f"{_s['mu']:>+11,.0f}{_s['sd']:>11,.0f}{_s['ratio']:>7.2f}"
              f"{_s['fire']:>6}{_s['worst']:>+12,.0f}{_mk}")
        _out.append(("trail", _t, _s))
    return _b, _out


print(f"\n{'=' * 78}")
print(f"■ ポートフォリオ損切り — **合計**で判定する(個別ではない)")
print(f"{'=' * 78}")
print(f"  ⛔ 個別の損切りは全滅している(§18.55 / §18.56)。合計で判定すると"
      f"**銘柄固有のノイズが打ち消し合う**ので別物")
print(f"  ⚠ 見るのは **月平均÷σ**。合計だけで選ぶと『降りて損を減らした』が"
      f"『利益も減らした』を隠す")

_btr, _otr = _table(_tr, f"TRAIN {_tr[0]}〜{_tr[-1]}")
_bte, _ote = _table(_te, f"TEST  {_te[0]}〜{_te[-1]}")

# ── ★ 核心の診断: 発火した日、引けまで持っていたらどうだったか ────────
print(f"\n{'=' * 78}")
print(f"■ ★ 発火した日、引けまで持っていたらどうだったか")
print(f"{'=' * 78}")
print(f"  §18.55 で『早く降りるほど悪い』と出ている(5分 +10.3bp → 引け +17.8bp)。")
print(f"  **悪い日は最後まで悪いのか、それとも戻すのか** — これが本質的な問い")
print(f"    {'水準':<14}{'発火':>6}{'降りた損益':>13}{'引けまで':>13}"
      f"{'差':>13}{'正解率':>8}")
for _l in _LV:
    _f, _cut_pnl, _hold_pnl, _win = 0, 0.0, 0.0, 0
    for _d in _ks:
        _v, _fired, _i = _apply(_d, level=_l)
        if not _fired:
            continue
        _ts, _path, _fin, _n, _cum = _days[_d]
        _v -= float(_NOTIONAL.get(_d, 0.0)) * a.slip_pct
        _f += 1
        _cut_pnl += _v
        _hold_pnl += _fin
        if _v > _fin:
            _win += 1
    if not _f:
        print(f"    {'−' + f'{_l:,.0f}円':<14}{0:>6}{'—':>13}")
        continue
    print(f"    {'−' + f'{_l:,.0f}円':<14}{_f:>6}{_cut_pnl:>+13,.0f}"
          f"{_hold_pnl:>+13,.0f}{_cut_pnl - _hold_pnl:>+13,.0f}"
          f"{_win / _f * 100:>7.0f}%")
print(f"  ⚠ 『正解率』= 降りたほうが良かった日の割合。50%前後なら**コイン投げ**")
print(f"  ⚠ 全期間(TRAIN+TEST)の集計。採否は上の TRAIN/TEST 表で判断すること")

# ══════════════════════════════════════════════════════════════════════
# ★★ 満額投資日だけの損切り (2026-09-04)
# ══════════════════════════════════════════════════════════════════════
#   ★ 位置づけは「悪い日を見抜く」ではなく **満額投資日の保険料を限定する**。
#     無条件の損切りは『悪い日は戻す』ため失敗した(§18.62)。しかし
#     n_days.csv の実測では、円の大負け日の 87.5% が 10件以上の日で、
#     しかも 件数と投入額の相関 +0.935 / 件数と投入額比損益の相関 +0.008。
#     = 件数が多い日が危険なのではなく **投入額が大きいから円損失も大きい**。
#     なら守る日を「満額の日」に絞れば保険料の総額が下がるはず、という筋。
#
#   ⛔⛔ **先読みの排除がこの検証の要**。「最終的に満額になった日だけ、朝から
#     損切りがあったことにする」は先読み。picks.csv には約定時刻が無く
#     (日足バックテストは全部 寄り約定)、『何時に満額になったか』をデータから
#     出せないので、代わりに **--gate-from(既定09:10)まで武装しない**。
#     実運用の 09:09 頃まで遅寄りが続く実測に合わせた保守側の扱い。
#
#   ⚠ 実運用は予算200万なので満額(10件級)にはほぼ届かない。件数ではなく
#     **投入率**で条件を書くのは、予算を変えても意味が変わらないため。


def _cvar(vals: list, q: float = 0.05) -> float:
    """下位 q の平均(CVaR)。**主指標**。最悪1日ではなく裾の平均を見る。"""
    if not vals:
        return 0.0
    _v = sorted(vals)
    _k = max(1, int(len(_v) * q))
    return float(np.mean(_v[:_k]))


def _arm(days: list, level: float, gate: float, use_start: bool,
         pct: float = 0.0, gate_n: int = 0):
    """1本の腕を評価する。gate>0 なら投入率がそれ以上の日だけ損切りを効かせる。

    ★ pct>0 なら損切り水準を **その日の投入額の pct%** にする。
      ⛔ 固定円の損切りは投入額で意味が変わる: −30,000円 は満額(400万)なら
        −0.75% だが 3割(120万)なら −2.5%。「満額の日だけ」に絞ることは
        実質「投入額比で浅い損切りだけ効かせる」ことになっていて、
        『保険料を限定する』とは別の機構が働いている可能性がある。
        投入額比で書けば、その2つを分離できる。

    返り値: 日次損益, 日次の投入額比bp, 発火数, 対象日数, 発火日の(降りた,引け)
    """
    _yen, _bp, _fire, _cov = [], [], 0, 0
    _cut, _hold = 0.0, 0.0
    _gain, _loss, _win = [], [], 0
    for _d in days:
        _ts, _path, _fin, _n, _cum = _days[_d]
        _nt = float(_NOTIONAL.get(_d, 0.0))
        if gate_n > 0:
            _st = _armed_n(_d, gate_n) if use_start else 0
            _elig = int(_NPOS.get(_d, 0)) >= gate_n
        else:
            _st = _armed_idx(_d, gate) if use_start else 0
            # 対象日か = **その日の建玉(picks全体)が予算の gate% 以上**。
            #   ⛔ 経路で再現できた分(_cum)で判定すると欠損の多い日が落ちる。
            _elig = (gate <= 0) or (_FULL.get(_d, 0.0) >= gate)
        # ⛔⛔ **その日の最終投入額を使ってはいけない**(2026-09-04 指摘、実在した)。
        #   武装した時点ではまだ全部建っていない。最終額で損切り幅を作ると
        #   実際に持っている額より広い閾値になり、決済コストも建てていない分まで
        #   払うことになる。→ **武装時点の建玉**で幅を、**発火時点の建玉**で
        #   コストを計算する。
        _held = float(_cum[_st]) if _st < len(_cum) else _nt
        _lv = (_held * pct / 100.0) if pct > 0 else level
        _on = (_lv > 0) and _elig and (_st < len(_ts))
        _v = _fin
        if _on:
            _cov += 1
            _v2, _f, _i = _apply(_d, level=_lv, start=_st)
            if _f:
                _pv = _OPEN.get(_d)
                if a.exit_next_open and _pv is not None and _i + 1 < len(_pv):
                    _v2 = float(_pv[_i + 1])      # 次のバーの始値で決済
                _cost = float(_cum[_i]) if _i < len(_cum) else _nt
                _v = _v2 - _cost * a.slip_pct
                _fire += 1
                _cut += _v
                _hold += _fin
                # ★ 非対称性を見るため、改善と機会損失を **別々に**貯める。
                #   正解率は分類の指標で、損益の大小を映さない(2026-09-04 指摘)。
                (_gain if _v > _fin else _loss).append(_v - _fin)
                _win += 1 if _v > _fin else 0
        _yen.append(_v)
        _bp.append(_v / _nt * 1e4 if _nt > 0 else 0.0)
    return {"yen": _yen, "bp": _bp, "fire": _fire, "cov": _cov,
            "cut": _cut, "hold": _hold, "win": _win,
            "gain": (float(np.mean(_gain)) if _gain else 0.0),
            "loss": (float(np.mean(_loss)) if _loss else 0.0),
            "ngain": len(_gain), "nloss": len(_loss)}


def _msig(days: list, yen: list) -> tuple:
    """月平均と月次σ。⛔ 従来の判定基準(月平均÷σ)を新表にも必ず出す。
    CVaR は裾、÷σ は全体のばらつき。**別の量なので片方だけ見ると食い違う**。"""
    _m: dict = {}
    for _d, _v in zip(days, yen):
        _m[_d[:7]] = _m.get(_d[:7], 0.0) + _v
    _mv = np.array([_m[k] for k in sorted(_m)], float)
    _mu = float(_mv.mean()) if len(_mv) else 0.0
    _sd = float(_mv.std(ddof=1)) if len(_mv) > 1 else 0.0
    return _mu, _sd, (_mu / _sd if _sd else 0.0)


def _boot_cvar(days: list, ya: list, yb: list, B: int = 1000) -> tuple:
    """月ブロック・ブートストラップで **CVaR差(腕 − 現行)** の95%CI。

    ⛔ 日をシャッフルすると同日相関・月内相関を壊す。**月ごと**にリサンプルする
      (§18.13 の『帰無較正は日ブロックを保つ』と同じ理由)。
    CVaR5% は 236日なら下位11〜12日の平均でしかない。CI を出さないと
    「そもそも区別できるのか」が分からない。
    """
    _mo: dict = {}
    for _i, _d in enumerate(days):
        _mo.setdefault(_d[:7], []).append(_i)
    _keys = sorted(_mo)
    if len(_keys) < 3:
        return None, None          # 月が3つ未満 → 計算しない(0と誤読させない)
    _out = []
    for _b in range(B):
        _g = _rnd.Random(_b)
        _ix: list = []
        for _ in _keys:
            _ix.extend(_mo[_g.choice(_keys)])
        _out.append(_cvar([ya[i] for i in _ix]) - _cvar([yb[i] for i in _ix]))
    _out.sort()
    return _out[int(B * 0.025)], _out[int(B * 0.975)]


# ★★ 主判定は **先に固定する**(2026-09-04 に宣言)。後から基準を足さない。
#   ⛔ 目的は『大負けを薄くする保険』なので、÷σ は拒否条件にしない。
#     通常月の効率が少し下がるのは、保険として受け入れる範囲。
_PASS = ("CVaR5% が改善 かつ 月平均が現行の90%以上 "
         "かつ 同じCVaRになる単純予算縮小より月平均が高い")


def _gate_band(days: list, pct: float, real: dict, base: dict) -> None:
    """★★ **同じ日数をランダムに選んで**同じ損切りを当てた帯と比べる。

    ⛔ 件数で絞れば必ず何かは変わる。『N件以上』という条件そのものが特別かを
      見るには、**同じ日数をランダムに選んだ場合**と比べるしかない
      (§18.24 / 2026-09-04 に Codex から指摘。私の ⑥ には無かった)。
    帯の中なら「日数を減らしただけ」で、件数条件に固有の価値は無い。
    """
    _cov = real["cov"]
    if _cov <= 0 or _cov >= len(days):
        return
    _bt, _bc = [], []
    for _s in range(a.gate_nulls):
        _g = _rnd.Random(_s)
        _pick = set(_g.sample(days, _cov))
        _y = []
        for _d in days:
            _ts, _path, _fin, _n, _cum = _days[_d]
            _nt = float(_NOTIONAL.get(_d, 0.0))
            if _d not in _pick:
                _y.append(_fin)
                continue
            _st = _armed_idx(_d, 0.0)
            if _st >= len(_ts):
                _y.append(_fin)
                continue
            _v2, _f, _i = _apply(_d, level=_nt * pct / 100.0, start=_st)
            if not _f:
                _y.append(_fin)
                continue
            _pv = _OPEN.get(_d)
            if a.exit_next_open and _pv is not None and _i + 1 < len(_pv):
                _v2 = float(_pv[_i + 1])
            _y.append(_v2 - _nt * a.slip_pct)
        _bt.append(sum(_y) - sum(base["yen"]))
        _bc.append(_cvar(_y) - base["cv"])
    _bt.sort()
    _bc.sort()
    _rt = sum(real["yen"]) - sum(base["yen"])
    _rc = _cvar(real["yen"]) - base["cv"]
    _pt = sum(1 for v in _bt if v >= _rt) / len(_bt)
    _pc = sum(1 for v in _bc if v >= _rc) / len(_bc)
    _mk = ("  ★帯の外(上位5%)" if _pt <= 0.05
           else ("  ⚠帯の中 = 日数を減らしただけ" if _pt >= 0.20 else ""))
    print(f"  {'':<22}└ 同日数ランダム{a.gate_nulls}本 … 合計差 実測"
          f"{_rt:>+11,.0f} / 帯 {_bt[0]:>+10,.0f}〜{_bt[-1]:>+10,.0f}"
          f" (上位{_pt * 100:.0f}%){_mk}")
    print(f"  {'':<22}{'':<2}  CVaR差 実測{_rc:>+11,.0f} / 帯 "
          f"{_bc[0]:>+10,.0f}〜{_bc[-1]:>+10,.0f} (上位{_pc * 100:.0f}%)")


def _row2(lbl: str, r: dict, days: list, base: dict, boot: bool = False) -> None:
    """1行。主判定は _PASS。÷σ は**副判定**として併記するだけ。"""
    _tot = sum(r["yen"])
    _mu, _sd, _rt = _msig(days, r["yen"])
    _cv, _cb = _cvar(r["yen"]), _cvar(r["bp"])
    _f = (_cv / base["cv"]) if base["cv"] < 0 else 1.0
    _eq = base["mu"] * _f
    _dif = r["cut"] - r["hold"]              # 発火日: 降りた − 引けまで
    _wr = (r["win"] / r["fire"] * 100.0) if r["fire"] else 0.0
    if r["fire"] <= 0:
        _mk = "  —"
    elif _cv <= base["cv"]:
        # ⛔ CVaR は **負**(下位5%の平均損失)。改善 = ゼロに近づく = より大きい。
        #   `_cv >= base` と書くと符号が反転して改善側に ⛔ が付く
        #   (§18.62 の _judge で踏んだのと同じ形。2026-09-04 に再発させた)。
        _mk = "  ⛔CVaR悪化"
    elif _mu < base["mu"] - abs(base["mu"]) * 0.10:
        # ⛔ `base*0.90` と書くと **base が負のとき不等号が反転**する
        #   (-53,250 の 90% は -47,925 で、元より良い値になってしまう)。
        #   「現行から1割以上 悪化していない」を符号に依らず書く。
        _mk = "  ⛔月平均が現行の90%未満"
    elif _mu <= _eq:
        _mk = "  ⛔予算縮小が上"
    else:
        # ⛔ 0.56 vs 0.63 を「横ばい」と書いていた(2026-09-04)。±5%で分ける。
        if _rt >= base["rt"]:
            _mk = "  ✅"
        elif _rt >= base["rt"] * 0.95:
            _mk = "  ✅(÷σ横ばい)"
        else:
            _mk = "  ✅⚠÷σ悪化"
    _ft = "—" if _f >= 1.0 else f"{_f:.2f}"
    _et = "—" if _f >= 1.0 else f"{_eq:+,.0f}"
    print(f"  {lbl:<22}{_tot:>+12,.0f}{_mu:>+10,.0f}{_rt:>6.2f}"
          f"{_cv:>+11,.0f}{_ft:>6}{_et:>11}{r['fire']:>5}{r['cov']:>5}"
          f"{_dif:>+11,.0f}{r['gain']:>+10,.0f}{r['loss']:>+10,.0f}"
          f"{_wr:>5.0f}%{_mk}")
    if boot and r["fire"] > 0:
        _lo, _hi = _boot_cvar(days, r["yen"], base["yen"], a.boot)
        if _lo is None:
            print(f"  {'':<22}└ CVaR差の95%CI … "
                  f"**月が3つ未満なので計算しません**")
        else:
            _sig = ("  ⚠CIがゼロをまたぐ = 区別できない"
                    if _lo <= 0 <= _hi else "  ★CIがゼロをまたがない")
            print(f"  {'':<22}{'└ CVaR差の95%CI(月ブロック)':<30}"
                  f"{_lo:>+12,.0f} 〜 {_hi:>+12,.0f}{_sig}")


def _row(lbl: str, yen: list, bp: list, fire: int, cov: int,
         base_cv: float, base_mu: float, nmon: int) -> None:
    _tot = sum(yen)
    _mu = _tot / max(nmon, 1)
    _cv, _cb = _cvar(yen), _cvar(bp)
    # ★ 予算縮小との等価比較: CVaR は予算に比例するので、同じ CVaR に
    #   するには予算を f 倍すればよい。そのとき月平均は f 倍になる。
    #   **それが腕の月平均より大きいなら、条件付き損切りは不要**。
    _f = (_cv / base_cv) if base_cv < 0 else 1.0
    _eq = base_mu * _f
    _mk = ""
    if fire <= 0:
        _mk = "  —(発火なし)"
    elif _f >= 1.0:
        # ⛔ CVaR が改善していない腕に「等価予算」は無意味。
        #   守るために払ったのに裾が悪化している = それ自体が却下理由。
        _mk = "  ⛔CVaR が改善していない"
    elif _mu > _eq:
        _mk = "  ✅"
    else:
        _mk = "  ⛔予算縮小の方が良い"
    _et = "—" if _f >= 1.0 else f"{_eq:+,.0f}"
    _ft = "—" if _f >= 1.0 else f"{_f:.2f}"
    print(f"  {lbl:<20}{_tot:>+13,.0f}{_mu:>+11,.0f}{_cv:>+12,.0f}"
          f"{_cb:>+10.0f}{fire:>5}{cov:>6}{_ft:>7}{_et:>11}{_mk}")


print(f"\n{'=' * 96}")
print(f"■ ★★ 満額投資日だけの損切り — 投入率 ≥ {a.gate_pct:.0f}% の日に絞る")
print(f"{'=' * 96}")
_nfull = sum(1 for d in _ks if _FULL.get(d, 0.0) >= a.gate_pct)
print(f"  予算 {a.budget:,.0f}万円 / 投入率 ≥ {a.gate_pct:.0f}% の日 "
      f"**{_nfull}日 / 全{len(_ks)}日 ({_nfull / max(len(_ks), 1) * 100:.0f}%)**")
print(f"  武装は **累積建玉がその水準に達した時点から**"
      f"(⑤は予算の{a.gate_pct:.0f}%、②④はその日の建玉が全部入った時点)")
print(f"  決済は **{'次のバーの始値' if a.exit_next_open else '発火バーの終値'}**"
      + (f" / 武装は **{a.arm_not_before} 以降**に限定" if a.arm_not_before
         else " / 武装の時刻制限なし"))
print(f"  ★ 累積建玉は **5分足の最初のバー = その銘柄が寄った時刻** から"
      f"組み立てています(picks.csv に約定時刻が無いための代理)")
print(f"  ★ 主指標は **CVaR5%(円)** = 下位5%の日の平均。最悪1日ではなく裾の平均")
print(f"  ★ 遅寄り(その日の最初のバーより後に寄った) "
      f"**{_LATE[0]:,}/{_LATE[1]:,}銘柄日 "
      f"({_LATE[0] / max(_LATE[1], 1) * 100:.0f}%)** — "
      f"建てる前は損益ゼロで扱っています(bfill しない)")
# ★ 経路で扱えた満額日の数を必ず出す。ここが激減していたら判定は成立しない
# ⛔ picks.csv を作った予算と --budget が食い違うと投入率が壊れる。
#   analyze_gap_edge の --budget-man と揃っていないと、満額日の判定が
#   丸ごと無意味になる(200万で作った picks を 400万で読むと投入率が半分)。
_over = sum(1 for d in _ks if _FULL.get(d, 0.0) > 105.0)
if _over > len(_ks) * 0.05:
    print(f"  ⛔⛔ **投入率が100%を超える日が {_over}/{len(_ks)}日 あります。**")
    print(f"     picks.csv を作った analyze_gap_edge の --budget-man と "
          f"--budget {a.budget:,.0f}万 が食い違っています。満額判定は無意味です")
# ★ 件数の分布。N の閾値を読む前にこれが要る(何件の日がどれだけあるか)
_npv = sorted(int(_NPOS.get(d, 0)) for d in _ks)
print(f"  ★ 1日の建玉件数: 中央 **{_npv[len(_npv) // 2]}件** / "
      f"平均 {sum(_npv) / max(len(_npv), 1):.1f}件 / "
      f"最小 {_npv[0]} 〜 最大 {_npv[-1]}")
print("     " + " / ".join(
    f"{_g}件以上 {sum(1 for v in _npv if v >= _g)}日"
    f"({sum(1 for v in _npv if v >= _g) / max(len(_npv), 1) * 100:.0f}%)"
    for _g in _GN if _g > 0))
_ftr = sum(1 for d in _tr if _FULL.get(d, 0.0) >= a.gate_pct)
_fte = sum(1 for d in _te if _FULL.get(d, 0.0) >= a.gate_pct)
print(f"  ★ 満額日の内訳: TRAIN **{_ftr}日** / TEST **{_fte}日** "
      f"(③⑤ の『対象』列がこれと大きく違うなら、武装できていない日があります)")


for _wn, _wd in (("TRAIN", _tr), ("TEST", _te)):
    print(f"\n  ── {_wn} {_wd[0]}〜{_wd[-1]} ({len(_wd)}営業日) ──")
    _b0 = _arm(_wd, 0.0, 0.0, False)
    _bmu, _bsd, _brt = _msig(_wd, _b0["yen"])
    _base = {"cv": _cvar(_b0["yen"]), "mu": _bmu, "rt": _brt,
             "yen": _b0["yen"]}
    print(f"  {'腕':<22}{'合計':>12}{'月平均':>10}{'÷σ':>6}"
          f"{'CVaR5%':>11}{'等価f':>6}{'等価月平均':>11}{'発火':>5}"
          f"{'対象':>5}{'降−引':>11}{'正解時':>10}{'誤発火':>10}{'正解率':>6}")
    print("  " + "-" * 126)
    print(f"  {'① 現行(損切りなし)':<22}{sum(_b0['yen']):>+12,.0f}"
          f"{_bmu:>+10,.0f}{_brt:>6.2f}{_base['cv']:>+11,.0f}"
          f"{1.0:>6.2f}{_bmu:>+11,.0f}{'—':>5}{'—':>5}{'—':>11}"
          f"{'—':>10}{'—':>10}{'—':>6}")
    # ★★ 2×2。**満額条件と閾値単位を同時に変えない**ため4象限すべて出す。
    #   ③ vs ④ は2つ同時に変えているので比較にならない(2026-09-04 指摘)。
    #   同じ −0.5% で ④(全日) と ⑤(満額のみ) を並べるのが本命。
    for _l in _LV:
        _row2(f"② 全日 −{_l:,.0f}円", _arm(_wd, _l, 0.0, True), _wd, _base)
    for _l in _LV:
        _row2(f"③ 満額のみ −{_l:,.0f}円",
              _arm(_wd, _l, a.gate_pct, True), _wd, _base)
    for _p in _LP:
        _row2(f"④ 全日 −投入の{_p:.1f}%",
              _arm(_wd, 0.0, 0.0, True, pct=_p), _wd, _base)
    for _p in _LP:
        _row2(f"⑤ 満額のみ −投入の{_p:.1f}%",
              _arm(_wd, 0.0, a.gate_pct, True, pct=_p), _wd, _base,
              boot=True)
    # ★★ ⑥ **件数**で切る。投入率と違い、予算を変えても意味が変わらない。
    #   N=0 は全日(=④)なので、そこからどう動くかで『件数に意味があるか』が分かる。
    for _p in _GNP:
        for _gn in _GN:
            _lbl = (f"⑥ {_gn:>2}件以上 −投入の{_p:.1f}%" if _gn > 0
                    else f"⑥ 全日(基準) −投入の{_p:.1f}%")
            _r = _arm(_wd, 0.0, 0.0, True, pct=_p, gate_n=_gn)
            _row2(_lbl, _r, _wd, _base, boot=(_gn > 0))
            if _gn > 0 and a.gate_nulls > 0:
                _gate_band(_wd, _p, _r, _base)

if _NG_N:
    print(f"\n  ⚠ **{len(_NG_N)}日は判定不能**(5分足で建玉を再現しきれず、"
          f"『いつ閾値に到達したか』が分からない)。比例縮小で埋めません")
print(f"\n  ★ 『等価f』= その腕と同じ CVaR にするために予算を何倍にするか。")
print(f"     『等価月平均』= そのとき残る月平均利益(= 現行 × f)。")
print(f"  ★ 『降−引』= **発火した日だけ**で「降りた損益 − 引けまで持った損益」。")
print(f"     マイナスなら降りて損。『正解率』が50%前後なら**コイン投げ**(§18.62)")
print(f"  ⛔ **等価月平均 を上回らない腕は採用しない**。"
      f"同じリスク低減が『予算を減らすだけ』で得られるなら、")
print(f"     条件付き損切りという複雑さを足す理由がありません")
print(f"  ⚠ **CVaR と ÷σ は別の量**(裾 vs 全体のばらつき)。CVaR が改善しても")
print(f"     ÷σ が改善しないことはあります。片方だけ見て採用しないこと")
print(f"  ⛔ ④(投入額比)を必ず見ること。**固定円は投入額で意味が変わる**"
      f"(−30,000円 は満額400万なら−0.75%、3割120万なら−2.5%)。")
print(f"     ③が効くのが『満額の日だから』なのか『投入額比で浅いから』なのかは、")
print(f"     ③と④を並べないと分離できません")
print(f"  ⚠ 予算縮小は比例縮小の近似です(実際は限界の銘柄が落ちる)。目安です")
print(f"  ⚠ ②③④ が TRAIN と TEST で同じ水準を指さなければ、固定できません(§18.36)")

# ══════════════════════════════════════════════════════════════════════
# ★★ 大負けした日は、いつ畳めば損が小さかったか (2026-09-04 ユーザー発案)
# ══════════════════════════════════════════════════════════════════════
#   ⛔⛔ **これは事後選択の診断であって、ルールではない。**
#     「その日が大負けになる」ことは寄りには分からない。ここで分かるのは
#     **どういう形で負けたか**だけ。早い時間に損が決まっているなら時刻ルールに
#     意味があり、遅ければ何をしても無理、という判断材料になる。
#   ★ 同じ表を「10件以上の全日」でも出す。**形が同じなら見分けられない**
#     ので、大負け日だけ早く畳むルールは作れない。

_WT = [t.strip() for t in a.worst_times.split(",") if t.strip()]


def _at(day: str, hhmm: str) -> float:
    """その日の hhmm 時点(以前の最後のバー)の含み損益。無ければ最終値。"""
    _idx, _path, _fin, _n, _cum = _days[day]
    _h, _m = (int(x) for x in hhmm.split(":"))
    _w = [k for k, t in enumerate(_idx) if (t.hour, t.minute) <= (_h, _m)]
    return float(_path[_w[-1]]) if _w else _fin


def _exit_all_at(day: str, hhmm: str) -> float:
    """その時刻に全部畳んだときの損益(発火時点の建玉に slip)。"""
    _idx, _path, _fin, _n, _cum = _days[day]
    _h, _m = (int(x) for x in hhmm.split(":"))
    _w = [k for k, t in enumerate(_idx) if (t.hour, t.minute) <= (_h, _m)]
    if not _w:
        return _fin
    _j = _w[-1]
    if _j >= len(_idx) - 1:
        return _fin                     # 最終バー = 引けと同じ
    _pv = _OPEN.get(day)
    _v = float(_pv[_j + 1]) if (a.exit_next_open and _pv is not None
                                and _j + 1 < len(_pv)) else float(_path[_j])
    return _v - (float(_cum[_j]) if _j < len(_cum) else
                 float(_NOTIONAL.get(day, 0.0))) * a.slip_pct


def _worst_table(days: list, label: str) -> dict:
    if not days:
        print(f"\n  ── {label} … 対象なし")
        return {}
    _fin = sum(_days[d][2] for d in days)
    print(f"\n  ── {label}（{len(days)}日 / 引けまで持った合計 "
          f"{_fin:+,.0f}円 / 平均 {_fin / len(days):+,.0f}円/日）──")
    print(f"    {'時刻':<8}{'平均 含み損益':>14}{'中央値':>12}{'最悪':>12}"
          f"{'この時刻で全部畳む':>19}{'引けとの差':>13}")
    _best, _bt = None, None
    _out = {}
    for _t in _WT:
        _v = [_at(d, _t) for d in days]
        _e = sum(_exit_all_at(d, _t) for d in days)
        _out[_t] = _e
        if _best is None or _e > _best:
            _best, _bt = _e, _t
        print(f"    {_t:<8}{np.mean(_v):>+14,.0f}{np.median(_v):>+12,.0f}"
              f"{min(_v):>+12,.0f}{_e:>+19,.0f}{_e - _fin:>+13,.0f}")
    print(f"    {'引け':<8}{_fin / len(days):>+14,.0f}{'—':>12}{'—':>12}"
          f"{_fin:>+19,.0f}{0:>+13,.0f}")
    if _best is not None and _best > _fin:
        print(f"    ★ 最良は **{_bt}** … {_best:+,.0f}円 "
              f"(引けより **{_best - _fin:+,.0f}円** 改善)")
    else:
        print(f"    ⛔ **引けを上回る時刻はありません**(いつ畳んでも同じか悪い)")
    return _out


print(f"\n{'=' * 96}")
print(f"■ ★★ 大負けした日は、いつ畳めば損が小さかったか")
print(f"{'=' * 96}")
print(f"  ⛔⛔ **これは事後選択の診断です。ルールにはできません。**")
print(f"     『その日が大負けになる』ことは寄りには分かりません。")
print(f"     分かるのは **どういう形で負けたか** だけです")

_c10 = [d for d in _ks if int(_NPOS.get(d, 0)) >= a.worst_n]
_c10.sort(key=lambda d: _days[d][2])
_wk = _c10[:a.worst_k]
_o1 = _worst_table(_wk, f"★ {a.worst_n}件以上 かつ 損益が下位{a.worst_k}日")
_o2 = _worst_table(_c10, f"参考: {a.worst_n}件以上の**全日**")

if _o1 and _o2:
    _f1 = sum(_days[d][2] for d in _wk)
    _f2 = sum(_days[d][2] for d in _c10)
    _b1 = max(_o1, key=lambda k: _o1[k])
    _b2 = max(_o2, key=lambda k: _o2[k])
    print(f"\n  ★ 最良の時刻: 大負け日 **{_b1}** / 全日 **{_b2}**")
    if _b1 == _b2:
        print(f"     → **同じ時刻**。大負け日に固有の形ではありません")
    else:
        print(f"     → 違う時刻。ただし**寄りには見分けられない**ので、")
        print(f"       大負け日だけ {_b1} に畳むルールは作れません")
    print(f"  ⚠ 全日で {_b2} に畳むと {_o2[_b2] - _f2:+,.0f}円。"
          f"これが**実際に選べる唯一の形**です")

# ══════════════════════════════════════════════════════════════════════
# ★★ 決済時刻 — 引け成行より早く畳んだらどうか (2026-09-04 ユーザー発案)
# ══════════════════════════════════════════════════════════════════════
#   「明らかに、引け成行より 15:20 とかに売る方が多く利益が出てる気がする」
#
#   ⛔ これは損切りではない。**発火条件が無く、毎日必ず その時刻に畳む**。
#     上の損切り表と混ぜないこと(あちらは条件付き)。
#
#   ⚠ 非対称なコストがある。**引け成行(MOC)は板寄せなのでスプレッドを払わない**
#     (約定値 = その日の終値 = バックテストの基準そのもの)。早く畳むのは
#     ザラ場の成行なので **必ずスプレッドを払う**。--slip-pct を掛けるのは
#     早い側だけ。ここを揃えると早い側が不当に有利に出る。
_ET = [t.strip() for t in a.exit_times.split(",") if t.strip()]


def _tod(x):
    return pd.Timestamp(x).time()


# ★ 5分足の時刻ラベルが『バーの開始』か『終了』かを**実データで**確かめる。
#   開始ラベルなら 15:20 のバーの終値は **15:25 の価格**。ここを取り違えると
#   「15:20 に売る」が実際には別の時刻になる(§18.32 の教訓)。
_lastlab: dict = {}
for _d in _ks:
    _k = _days[_d][0][-1].strftime("%H:%M")
    _lastlab[_k] = _lastlab.get(_k, 0) + 1
_lab = sorted(_lastlab.items(), key=lambda x: -x[1])
print(f"\n{'=' * 78}")
print(f"■ ★★ 決済時刻 — 引け成行より早く畳んだらどうか")
print(f"{'=' * 78}")
print(f"  5分足の最終バーのラベル: "
      + " / ".join(f"{k} ({v}日)" for k, v in _lab[:3]))
print(f"    → 最終バーの終値 = その日の終値。**このラベル以降を指定しても"
      f"引けと同じ**になります")


def _exit_stat(days: list, hhmm: str = ""):
    """毎日 hhmm に全部畳んだときの集計。hhmm が空なら引け(=現行)。"""
    _tot, _same, _late = 0.0, 0, 0
    _daily, _labs = [], {}
    for _d in days:
        _ts, _path, _fin, _n, _cum = _days[_d]
        if not hhmm:
            _v = _fin
        else:
            _t = _tod(f"2000-01-01 {hhmm}")
            _idx = [k for k, x in enumerate(_ts) if x.time() <= _t]
            if not _idx:
                _late += 1                      # その時刻より前のバーが無い
                _v = _fin
            else:
                _j = _idx[-1]
                _labs[_ts[_j].strftime("%H:%M")] = \
                    _labs.get(_ts[_j].strftime("%H:%M"), 0) + 1
                if _j == len(_ts) - 1:
                    _same += 1                  # 実質 引けと同じ
                _v = float(_path[_j]) \
                    - float(_NOTIONAL.get(_d, 0.0)) * a.slip_pct
        _tot += _v
        _daily.append((_d, _v))
    _m: dict = {}
    for _d, _v in _daily:
        _m[_d[:7]] = _m.get(_d[:7], 0.0) + _v
    _mv = np.array([_m[k] for k in sorted(_m)], float)
    _mu = float(_mv.mean()) if len(_mv) else 0.0
    _sd = float(_mv.std(ddof=1)) if len(_mv) > 1 else 0.0
    _bar = max(_labs.items(), key=lambda x: x[1])[0] if _labs else "—"
    return {"tot": _tot, "mu": _mu, "sd": _sd,
            "ratio": (_mu / _sd if _sd else 0.0),
            "pos": int((_mv > 0).sum()), "nm": len(_mv),
            "worst": float(_mv.min()) if len(_mv) else 0.0,
            "same": _same, "late": _late, "bar": _bar, "n": len(days)}


def _etable(days: list, label: str) -> tuple:
    print(f"\n  ── {label} ({len(days)}営業日) ──")
    _b = _exit_stat(days)
    print(f"    {'決済':<10}{'使うバー':>10}{'合計':>13}{'月平均':>11}"
          f"{'月次σ':>11}{'÷σ':>7}{'最悪月':>12}{'引けとの差':>13}")
    print(f"    {'引け ★現行':<10}{_b['bar']:>10}{_b['tot']:>+13,.0f}"
          f"{_b['mu']:>+11,.0f}{_b['sd']:>11,.0f}{_b['ratio']:>7.2f}"
          f"{_b['worst']:>+12,.0f}{'—':>13}")
    _r = {}
    for _t in _ET:
        _s = _exit_stat(days, _t)
        _r[_t] = _s
        _note = ""
        if _s["same"] >= len(days) * 0.5:
            _note = f"  ⛔実質引け({_s['same']}日)"
        elif _s["ratio"] - _b["ratio"] >= 0.10:
            _note = "  ✅"
        print(f"    {_t:<10}{_s['bar']:>10}{_s['tot']:>+13,.0f}"
              f"{_s['mu']:>+11,.0f}{_s['sd']:>11,.0f}{_s['ratio']:>7.2f}"
              f"{_s['worst']:>+12,.0f}{_s['tot'] - _b['tot']:>+13,.0f}{_note}")
    return _b, _r


_btr_e, _etr = _etable(_tr, f"TRAIN {_tr[0]}〜{_tr[-1]}")
_bte_e, _ete = _etable(_te, f"TEST  {_te[0]}〜{_te[-1]}")

print(f"\n  ⚠ **早い決済にだけ スリッページ {a.slip_pct * 100:.2f}% を掛けています。**")
print(f"     引け成行(MOC)は板寄せの単一価格なのでスプレッドを払いません。")
print(f"     揃えると早い側が不当に有利に出ます(実運用と食い違う)")


def _beat(t: str, key: str) -> bool:
    """TRAIN/TEST の両方で引けを上回ったか。実質引けの行は対象外。"""
    if not (_etr.get(t) and _ete.get(t)):
        return False
    if _etr[t]["same"] >= len(_tr) * 0.5 or _ete[t]["same"] >= len(_te) * 0.5:
        return False
    return (_etr[t][key] > _btr_e[key]) and (_ete[t][key] > _bte_e[key])


# ⛔ 採否は **月平均÷σ**。合計だけで見ると「降りて利益を増やした」と
#   「ばらつきを増やした」を区別できない。§18.38 で walk-forward が合計で
#   選んで σ 最悪の設定を掴んだのと同じ罠。両方出して食い違いを可視化する。
_ag_r = [t for t in _ET if _beat(t, "ratio")]
_ag_t = [t for t in _ET if _beat(t, "tot")]
print(f"\n  ★ TRAIN と TEST の**両方**で引けを上回った時刻")
print(f"      月平均÷σ（**これが採否の基準**）: "
      + (", ".join(_ag_r) if _ag_r else "**なし**"))
print(f"      合計（参考。これだけで選ばない）: "
      + (", ".join(_ag_t) if _ag_t else "なし"))
if _ag_r:
    print(f"  ★ §18.55 の『早く降りるほど悪い』と逆です。**引け直前だけ別**"
          f"かもしれないので、次は 5分刻みで詰める価値があります")
elif _ag_t:
    print(f"  ⚠ **合計では上回るが 月平均÷σ では上回りません。**"
          f"利益と一緒にばらつきも増えています。採用しないこと")
else:
    print(f"  ⛔ **どちらの基準でも上回った時刻はありません。**")
    print(f"     §18.55(5分 +10.3bp → 11:05 +10.5bp → 引け +17.8bp)と同じ向き。"
          f"引け成行のままでよい")

print(f"\n  {'=' * 68}")
print(f"  ★ 判定(§18.36 のルール):")
print(f"    ① **月平均÷σ が『損切りなし』より明確に高い**水準があるか")
print(f"    ② その水準が **TRAIN と TEST で同じ**か(違えば固定できない)")
print(f"    ③ 『引けまで持っていたら』の差がプラスか(降りて正解だったか)")
print(f"  ⛔ ①②が揃わなければ採用しない。合計が増えただけでは不十分")
print(f"  ⚠ 5分足は約2年しかないので、どちらの窓も1年程度。**弱い検定**です")
print(f"  {'=' * 68}")
