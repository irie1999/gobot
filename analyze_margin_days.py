"""analyze_margin_days.py — 信用残の「日数」換算が N の選別軸になるか。

⛔ **発注しない。既存の CSV を読むだけ。**

────────────────────────────────────────────────────────────────────
★ この検証で測るもの (3つだけ。これで打ち切る)
────────────────────────────────────────────────────────────────────
    buy_days  = 信用買い残 ÷ ADV20     … やれ込み(戻り売りの燃料)
    sell_days = 信用売り残 ÷ ADV20     … 踏み上げリスク
    net_days  = (買残 − 売残) ÷ ADV20  … 上2つの差(独立ではない)

  ⛔ 交互作用(ギャップ幅 × / 前日上昇率 ×)は掃かない。
     2026-09-18 までに 6指標 + 12条件 + 交差4 = 22検定を消費済み。
     ここに条件を足すと帰無の期待が上がるだけ。

  ⚠ 生の株数は使わない。大型株ほど絶対値が大きいので **流動性の代理**に
     なる(流動性は 2026-08 までに3回ヌル)。必ず ADV20 で割る。

────────────────────────────────────────────────────────────────────
⛔⛔ 足してはいけない軸 (2026-09-19。一度足して取り下げた)
────────────────────────────────────────────────────────────────────
  「買残が多く売残が少ない」を **割合**で測り直そうとして 3軸を足したが、
  Codex の指摘で取り下げた。同じ失敗を繰り返さないための記録。

  ⛔ long_share = 買残 ÷ (買残 + 売残)
     L/(L+S) = r/(1+r) は信用倍率 r について **厳密に単調増加**。
     実測(20万点): 倍率との Spearman = **1.0000000000** /
     5分位が完全一致 **100.0000%** / Codex の log((L+100)/(S+100))
     とも 0.9994・98.1%一致。**名前が違うだけの同じ検定**。
     同様に (買残−売残)/(買残+売残) も Spearman 1.0 で同一。

  ★ 私(Claude)が間違えた理由:
     「倍率は売残が極小だと発散して上位分位が外れ値で埋まる」と考えたが、
     **分位は順位で切るので外れ値は binning に一切影響しない**。
     上位20%は値がいくつでも上位20%。外れ値がレバレッジを持つのは
     **連続値の回帰**のときだけで、Codex はそこに log(…+100) を
     入れて既に対策済みだった。
     → 単調変換で「新しい軸」を作れないか考えたときは、**まず順位相関を
       測る**。1.0 なら分位ベースの検定では完全に無意味。

  ⛔ neg_long_share = 一般買残 ÷ 買残  … Codex が実施済み(不合格)
  ⛔ std_short_share = 制度売残 ÷ 売残 … 唯一 未実施だが、225条件まで
     掃いた後の1軸に検出力は無い。MDE 10bp の壁は変わらない。

  ★ 信用残の次の用途は「銘柄を選ぶ」ではなく **監査**:
     IssType × MarginSell × 実注文結果 × ペーパー損益 を突き合わせ、
     「バックテスト利益のうち実際に取得可能なのは何円か」を測る。

────────────────────────────────────────────────────────────────────
★★ 合格条件 (回す前に凍結。結果を見て動かさない)
────────────────────────────────────────────────────────────────────
  ① TRAIN の最悪分位の bp が **明確にマイナス**
       N は稼働率40%前後で、除外して空いた枠を埋める代わりがいない。
       抜いた集団がプラスなら、抜くことは純損になる。
  ② TRAIN の **日クラスタ t** が |t| >= 2.0
       同日決済なので、下げた日は全銘柄がまとめて勝つ。件数で t を
       計算すると実効サンプルを誤認する。日を単位に数える。
  ③ **帰無較正の95%点**を超える
       同じ日の中で分位ラベルだけシャッフルし、「最悪分位を選ぶ」操作
       込みで較正する。日をまたぐシャッフルは日内相関を壊すので不可。
  ④ TRAIN の **前半・後半で符号が一致**
       片方だけなら期間への合わせ込み。

  → ①〜④を全部満たしたものだけ `--confirm` で TEST を1回だけ使う。
  → 1つも通らなければ **週次信用残ルートを閉じる**。

  ⛔ `--confirm` なしでは TEST を画面にも出さない(誤って見ないため)。
  ⛔ TEST は 1候補につき 1回だけ。

────────────────────────────────────────────────────────────────────
使い方
────────────────────────────────────────────────────────────────────
  # ① N の建てた明細を出す(adv20 列が要るので 2026-09-18 以降の版で)
  python analyze_gap_edge.py --days 4200 --min-gap-bp 100 --split 2020-09-01 \
      --min-ret1 1.753 --min-price 1000 --max-price 6000 --dump-picks n_picks.csv

  # ② TRAIN で掃く (TEST は出さない)
  python analyze_margin_days.py --picks n_picks.csv --margin margin_interest_n_10y.csv

  # ③ ①〜④を通ったものだけ、1回だけ
  python analyze_margin_days.py --picks n_picks.csv --margin margin_interest_n_10y.csv \
      --confirm sell_days:Q5

★ 読み方(これを外すと必ず誤読する)
  ・「候補ゼロ」は『効果がない』ではなく **『MDE より大きい効果は無い』**。
    各軸の最後に出る「検出力 SE → **Xbp より小さい効果は見えません**」を
    必ず併記すること。N のグロスは +15.7bp/件 なので、MDE が 10bp なら
    『戦略のエッジの2/3の識別力を持つ軸でないと見えない』という意味。
  ・除外案は **抜いた集団の絶対値**で判断する。「他より悪い」≠「損している」。
    N は稼働率40%で、空いた枠を埋める代わりがいない(§18.64/§18.77)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── 凍結した定数。⛔ コマンドラインから変えられない ──────────────────
_PASS_T = 2.0          # ② 日クラスタ t の下限
_PASS_NULL = 95.0      # ③ 帰無較正のパーセンタイル
_NQ = 5                # 分位数
_LAG_DAYS = 7          # 公表日 = Date + これ(暦日)。翌週金曜 = 火曜夕方より後
#   ⛔ **ここに足さない**(2026-09-18 までに 22検定を消費済み)
_AXES_DAYS = ("buy_days", "sell_days", "net_days")

# ⛔⛔ 割合3軸は 2026-09-19 に **取り下げた**(下の「足してはいけない軸」参照)。
#   空タプルのまま残すのは、再追加しようとした人がここのコメントを読むため。
_AXES_SHARE: tuple = ()
_AXES = _AXES_DAYS + _AXES_SHARE
_AXES_NEED_SPLIT: set = set()

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--picks", required=True,
                help="analyze_gap_edge.py --dump-picks の出力")
ap.add_argument("--margin", required=True,
                help="週次信用残 CSV (Date,Code,ShrtVol,LongVol,...)")
ap.add_argument("--confirm", default="",
                help="TEST を1回だけ使う。形式 axis:Qn (例 sell_days:Q5)")
ap.add_argument("--confirm-cross", default="",
                help="★ **事前登録された交差条件を1つだけ**評価する。\n"
                     "形式 <買残の上位%%>:<売残の下位%%> (例 80:20)。\n"
                     "⛔ 複数は受け付けない。掃いた結果を後から入れないこと。\n"
                     "TRAIN で境界を決め、TRAIN が通ったときだけ TEST を開く。")
ap.add_argument("--publish-bd", type=int, default=0,
                help="★ 公表日を **前週末 + N営業日** にする。0=既定の暦日7日。\n"
                     "週次残高は前週末(金)時点で、公表は翌週火曜の夕方。\n"
                     "3 なら水曜 = 公表の翌営業日(実際の公表日に基づく定義)。\n"
                     "⛔ 詰め方は1回だけ決めること。2 と 3 を試して良い方を\n"
                     "   採るのは、基準を緩めるのと同じ。")
ap.add_argument("--nulls", type=int, default=500,
                help="帰無較正の試行回数(既定500)")
ap.add_argument("--seed", type=int, default=20260918)
a = ap.parse_args()

_EPS = 1e-9


def _die(msg: str) -> None:
    sys.exit(f"[error] {msg}")


# ══════════════════════════════════════════════════════════════════
# 1. 読み込みと検品
# ══════════════════════════════════════════════════════════════════
print("=" * 74)
print(" 信用残の日数換算 — N の選別軸になるか")
print("=" * 74)

for _p in (a.picks, a.margin):
    if not Path(_p).exists():
        _die(f"{_p} がありません")

pk = pd.read_csv(a.picks)
mg = pd.read_csv(a.margin)

print(f"\n[入力] picks  {a.picks}  {len(pk):,}行")
print(f"[入力] margin {a.margin}  {len(mg):,}行")

# ── picks の検品 ──────────────────────────────────────────────────
_need = {"date", "symbol", "entry_p", "qty", "pnl"}
_miss = _need - set(pk.columns)
if _miss:
    _die(f"picks に列がありません: {sorted(_miss)}")
if "adv20" not in pk.columns:
    _die("picks に adv20 列がありません。\n"
         "         → analyze_gap_edge.py を 2026-09-18 以降の版にして\n"
         "           --dump-picks を作り直してください")
if "win" not in pk.columns:
    _die("picks に win 列(TRAIN/TEST)がありません")

pk["date"] = pd.to_datetime(pk["date"])
pk = pk.sort_values("date").reset_index(drop=True)

# ★ bp 換算。pnl は既に qty ぶんスケールされている
pk["notional"] = pk["entry_p"] * pk["qty"]
pk = pk[pk["notional"] > _EPS].copy()
pk["bp"] = pk["pnl"] / pk["notional"] * 10000.0

_n_adv = int(pk["adv20"].notna().sum())
print(f"       adv20 あり {_n_adv:,}/{len(pk):,}件 "
      f"({_n_adv / max(1, len(pk)) * 100:.1f}%)")
print(f"       窓の内訳  " + " / ".join(
    f"{_w} {int((pk['win'] == _w).sum()):,}件" for _w in pk["win"].unique()))

# ── margin の検品 ─────────────────────────────────────────────────
_needm = {"Date", "Code", "ShrtVol", "LongVol"}
_missm = _needm - set(mg.columns)
if _missm:
    _die(f"margin に列がありません: {sorted(_missm)}")

mg["Date"] = pd.to_datetime(mg["Date"])

print(f"\n[検品] margin 期間 {mg['Date'].min().date()} 〜 "
      f"{mg['Date'].max().date()} / 銘柄 {mg['Code'].nunique():,}")

# ★ 検算: 合計 == 一般 + 制度。ここが合わないとデータが壊れている
_has_split = {"ShrtNegVol", "ShrtStdVol"} <= set(mg.columns)
if _has_split:
    _d1 = (mg["ShrtVol"] - mg["ShrtNegVol"] - mg["ShrtStdVol"]).abs()
    _d2 = ((mg["LongVol"] - mg["LongNegVol"] - mg["LongStdVol"]).abs()
           if {"LongNegVol", "LongStdVol"} <= set(mg.columns) else pd.Series([0.0]))
    _bad = int((_d1 > 1.0).sum() + (_d2 > 1.0).sum())
    print(f"[検算] 合計 == 一般 + 制度 … "
          + ("✅ 全行一致" if _bad == 0 else f"⛔ **{_bad:,}行 不一致**"))
    if _bad:
        print("       ⚠ データが壊れている可能性。先に取り直してください")
else:
    print("[検算] 一般/制度の内訳列がないのでスキップ")

# ── Code を yfinance 形式へ ───────────────────────────────────────
#   J-Quants は 5桁(末尾0)。13010 → 1301.T
def _to_yf(c) -> str:
    s = str(c).strip()
    if len(s) == 5 and s.endswith("0"):
        return s[:4] + ".T"
    return s if s.endswith(".T") else s + ".T"


mg["symbol"] = mg["Code"].map(_to_yf)

_ov = len(set(mg["symbol"]) & set(pk["symbol"]))
print(f"[検品] 銘柄の重なり {_ov:,} / picks {pk['symbol'].nunique():,}銘柄")
if _ov < pk["symbol"].nunique() * 0.5:
    print("       ⛔ **半分も重なっていません**。Code の変換を疑ってください")
    print(f"         margin の例: {list(mg['Code'].head(3))}")
    print(f"         picks  の例: {list(pk['symbol'].head(3))}")

# ══════════════════════════════════════════════════════════════════
# 2. 公表日と結合  ⛔ ここが先読み防止の要
# ══════════════════════════════════════════════════════════════════
#   Date は **前週末(金)時点**。公表は翌週火曜の夕方。
#   N は前夜に候補を作るので、火曜の朝には間に合わない。
#   Date + 7暦日 = 翌週金曜 なら、どんな祝日配置でも公表後になる。
if a.publish_bd > 0:
    # ★ 営業日は **picks の取引日** から作る。これが実際に市場が開いた日
    #   なので、祝日も正しく扱える(外部カレンダー不要)。
    # ⛔ `np.array(sorted(...))` は使わない(2026-09-18)。
    #   Series.unique() は datetime64 を返すが、sorted() が Python の
    #   Timestamp のリストにしてしまい、np.array() が **object dtype** に
    #   なる。searchsorted が Timestamp と int を比べようとして落ちる。
    #   ⚠ 古い numpy では通ってしまい、Python 3.14 + 新しい numpy で
    #     初めて露見した。**dtype を暗黙に任せず、明示する。**
    #   ⚠ pandas 3.0 では pd.to_datetime(ndarray) が DatetimeArray を返し
    #     .values が無い。Series.to_numpy() なら版に依らず datetime64。
    _bdays = np.sort(pk["date"].drop_duplicates().to_numpy())
    assert _bdays.dtype.kind == "M", f"営業日の dtype が {_bdays.dtype}"
    _pos = np.searchsorted(_bdays, mg["Date"].to_numpy(), side="right")
    _tgt = _pos - 1 + a.publish_bd      # 前週末の次の営業日から数える
    _tgt = np.clip(_tgt, 0, len(_bdays) - 1)
    # 営業日カレンダーの外(取引のない期間)に落ちた行は暦日で代替。
    # ★ np.where ではなく Series.where を使う(dtype が保たれる)
    _av = pd.Series(_bdays[_tgt], index=mg.index)
    _fb = mg["Date"] + pd.Timedelta(days=_LAG_DAYS)
    mg["avail"] = _av.where(_av >= mg["Date"], _fb)
    print(f"[公表日] 前週末 + {a.publish_bd}営業日 "
          f"(picks の取引日をカレンダーに使用)")
else:
    mg["avail"] = mg["Date"] + pd.Timedelta(days=_LAG_DAYS)
    print(f"[公表日] 前週末 + {_LAG_DAYS}暦日(既定・保守側)")
mg = mg.sort_values("avail").reset_index(drop=True)

_cols = ["symbol", "avail", "Date", "ShrtVol", "LongVol"]
if _has_split:
    _cols += ["ShrtStdVol", "ShrtNegVol"]
if "IssType" in mg.columns:
    _cols.append("IssType")

# ★ merge_asof は dtype がズレると黙って壊れる/落ちるので、直前に確認する
#   (2026-09-18: np.array(sorted(...)) が object dtype を作って落ちた)
for _nm, _s in (("picks.date", pk["date"]), ("margin.avail", mg["avail"]),
                ("margin.Date", mg["Date"])):
    if not pd.api.types.is_datetime64_any_dtype(_s):
        _die(f"{_nm} の dtype が {_s.dtype} です(datetime64 でないと"
             " merge_asof が壊れます)")

jd = pd.merge_asof(
    pk.sort_values("date"),
    mg[_cols],
    left_on="date", right_on="avail", by="symbol",
    direction="backward", allow_exact_matches=True,
)

_ok = jd["ShrtVol"].notna() & jd["adv20"].notna() & (jd["adv20"] > _EPS)
print(f"\n[結合] {int(_ok.sum()):,}/{len(jd):,}件 "
      f"({_ok.sum() / max(1, len(jd)) * 100:.1f}%)")

jd = jd[_ok].copy()
if jd.empty:
    _die("結合できた取引がありません")

# ★ 先読みチェック。1件でも avail > date なら結合が壊れている
_bad_fw = int((jd["avail"] > jd["date"]).sum())
print(f"[検算] 先読み(公表日 > 取引日) … "
      + ("✅ 0件" if _bad_fw == 0 else f"⛔ **{_bad_fw:,}件**"))
if _bad_fw:
    _die("結合が壊れています")

_age = (jd["date"] - jd["Date"]).dt.days
print(f"[検算] 信用残の古さ(取引日 − 残高時点) 中央 {int(_age.median())}日 / "
      f"90%点 {int(_age.quantile(0.9))}日 / 最大 {int(_age.max())}日")

# ══════════════════════════════════════════════════════════════════
# 3. 指標
# ══════════════════════════════════════════════════════════════════
jd["buy_days"] = jd["LongVol"] / jd["adv20"]
jd["sell_days"] = jd["ShrtVol"] / jd["adv20"]
jd["net_days"] = (jd["LongVol"] - jd["ShrtVol"]) / jd["adv20"]

# ⛔ 割合(買残/(買残+売残) など)はここに足さない。信用倍率と順位が
#   完全に同じで、分位に割った時点で同一の検定になる。冒頭の
#   「足してはいけない軸」を読むこと。

# ★ 実際に使える軸だけに絞る(列が作れなかったものは落とす)
_AXES = tuple(_a for _a in _AXES if _a in jd.columns)

# ══════════════════════════════════════════════════════════════════
# 3b. ⛔ 株数の基準が揃っているかの検算 (株式分割)
# ══════════════════════════════════════════════════════════════════
#   信用残(J-Quants)は **その時点の株数**で、分割は未調整。
#   adv20 の元になる日足(yfinance)は **遡及調整済み**。
#   1:2 分割があると、分割前の週だけ 買残日数が 2倍 に化ける。
#   → 2026-08 に5分足で同じ形の汚染を踏んでいる(日足は調整・5分足は未調整)。
print("\n[検算] 株数の基準(分割)")
for _nm, _col in (("買残日数", "buy_days"), ("売残日数", "sell_days")):
    _v = jd[_col].replace([np.inf, -np.inf], np.nan).dropna()
    if _v.empty:
        continue
    print(f"    {_nm}  中央 {_v.median():.2f} / 99%点 {_v.quantile(0.99):.1f} / "
          f"最大 {_v.max():.1f}日")

# ★ 分割は **信用残そのもの** が整数比で跳ぶ(未調整なので)。
#   ⛔ 2026-09-18 の初版は「結合後の買残日数が隣接レコード間で跳ぶ件数」を
#     見ていたが、これは誤り(実データで 25.2% という非現実的な値が出た):
#       ・N の取引は散発的で、前回レコードが数ヶ月前のことがある
#       ・adv20 自体が動く。N は前日+1.75%以上の急騰銘柄を拾うので
#         出来高が急増し、買残が同じでも買残日数は跳ねる
#   → **margin CSV の中だけ**で、同一銘柄の **連続する週**(5〜10日差)の
#     比を見る。そして分割なら 2/3/4/5/10 の **整数比に集中する**
#     (2026-08 の5分足の汚染調査でも、すべて整数倍で端数は無かった)。
_m2 = mg.sort_values(["symbol", "Date"]).copy()
_prev = _m2.groupby("symbol")["LongVol"].shift(1)
_pdt = _m2.groupby("symbol")["Date"].shift(1)
_gapd = (_m2["Date"] - _pdt).dt.days
_okw = (_gapd >= 5) & (_gapd <= 10) & (_prev > 0) & (_m2["LongVol"] > 0)
_rt = (_m2["LongVol"] / _prev)[_okw].replace([np.inf, -np.inf], np.nan).dropna()
#   ⛔⛔ **分割そのものの検出は諦めた**(2026-09-18)。2案とも失敗:
#     案1「結合後の買残日数が隣接レコード間で跳ぶ割合」
#        → 実データで 25.2%。N は前日+1.75%以上の急騰銘柄を拾うので
#          adv20 が跳ね、買残が同じでも比が動く。**分割とは無関係**
#     案2「連続する週の買残の比が整数比に集中する割合 / 銘柄」
#        → 8銘柄に分割を混ぜても 175→176件で埋もれる。銘柄単位にすると
#          今度は分割なしでも 59銘柄が誤検出(156,000組あれば偶然 2.00倍に
#          なる組が出る)。**信用残の比だけでは分割と自然変動を区別できない**
#   ★ 区別するには「信用残(未調整)と出来高(調整済)の時系列が食い違う」
#     ことを見る必要があるが、picks は取引日の点しか持っていない。
#   → **実害の測り方に切り替える**: 極端値がどれだけあり、
#     それが最上位分位をどれだけ占めるか。分割由来でも低流動性由来でも、
#     効くのは「最上位分位が異常値で埋まっていないか」だけなので。
_big_syms = set()
for _nm, _col, _th in (("買残日数", "buy_days", 50.0),
                       ("売残日数", "sell_days", 20.0)):
    _ex = jd[jd[_col] > _th]
    _q5 = jd[jd[_col] >= jd[_col].quantile(0.8)]
    _share = len(_ex) / max(1, len(_q5)) * 100.0
    print(f"    {_nm} > {_th:.0f}日 … {len(_ex):,}件 "
          f"({len(_ex) / max(1, len(jd)) * 100:.2f}%) / "
          f"**上位20%分位の {_share:.1f}%** を占める")
    _big_syms |= set(_ex["symbol"].unique())
if _big_syms:
    print(f"    → 該当銘柄 {len(_big_syms):,}。上位分位の解釈はこのぶん"
          "割り引いてください")
print("    ⚠ 分割の有無そのものは判定できません(上のコメント参照)。"
      "見ているのは **異常値が上位分位をどれだけ汚しているか** だけです")

tr = jd[jd["win"] == "TRAIN"].copy()
te = jd[jd["win"] == "TEST"].copy()
if tr.empty:
    _die("TRAIN の行がありません")

print(f"\n[窓] TRAIN {len(tr):,}件 / {tr['date'].nunique():,}営業日 "
      f"({tr['date'].min().date()} 〜 {tr['date'].max().date()})")
print(f"     TEST  {len(te):,}件 / {te['date'].nunique():,}営業日  "
      "← ⛔ --confirm まで中身は見ません")
print(f"\n[基準] TRAIN 全体 {tr['bp'].mean():+.1f}bp/件")

# ── 交差条件の **規模だけ** 見る (2026-09-18) ────────────────────
#   ⛔ bp は出さない。出すと私が交互作用を検定したことになる
#     (このツールは「交互作用は掃かない」で凍結してある)。
#   ★ ただし「買残 上位20% かつ 売残 下位20%」のような交差条件が
#     何件になるかは **検品**であって判定ではない。件数が薄すぎると
#     日クラスタ t の実効サンプルが壊れるので、先に知っておく。
_b80 = tr["buy_days"].quantile(0.8)
_s20 = tr["sell_days"].quantile(0.2)
_x = tr[(tr["buy_days"] >= _b80) & (tr["sell_days"] <= _s20)]
_xd = _x["date"].nunique()
print(f"[規模] 参考: 買残日数 上位20%(>= {_b80:.2f}日) かつ "
      f"売残日数 下位20%(<= {_s20:.3f}日)")
print(f"       → {len(_x):,}件 / {_xd:,}営業日 "
      f"(TRAIN の {len(_x) / max(1, len(tr)) * 100:.1f}%, "
      f"{len(_x) / max(1, tr['date'].nunique()):.2f}件/日)")
print("       ⛔ 損益は出しません(このツールは交互作用を掃かない)。"
      "規模の確認だけです")
if len(_x) < 300:
    print(f"       ⚠ **300件未満**。日クラスタで数えると実効サンプルが"
          f"{_xd}日しかなく、t は不安定になります")

# ── 参考表示(判定に使わない) ──────────────────────────────────────
if _has_split:
    _sd = jd["ShrtStdVol"] / jd["adv20"]
    print(f"[参考] 制度信用の売残日数 中央 {_sd.median():.2f}日 "
          "(規制がかかるのは制度側。**判定には使いません**)")

# ══════════════════════════════════════════════════════════════════
# 貸借区分 — ⛔⛔ これで売建可否を判定してはいけない (2026-09-18 訂正)
# ══════════════════════════════════════════════════════════════════
#   IssueType は **制度信用の区分**:
#     1 = 信用銘柄 / 2 = 貸借銘柄 / 3 = その他
#   ⛔⛔ **N は制度信用で建てていない**。kabu の一般信用デイトレ
#     (CashMargin=2 / MarginTradeType=3)なので、信用銘柄でも
#     証券会社に在庫があれば空売りできる。
#     → 「信用銘柄 = N で売れない」は **誤り**。初版でそう書いたのは
#       自分の発注経路の読み違い。
#   ★ 実際に建てられるかを決めるのは、優先順に
#       ① 実注文の応答(エラーコード 100302 など)
#       ② kabu /symbol の MarginSell
#       ③ J-Quants の IssueType    ← いちばん弱い
#   → ここは **参考の内訳だけ**。除外の判断には使わない。
if "IssType" in jd.columns:
    print("\n" + "=" * 74)
    print(" 貸借区分(制度信用の区分) — ⛔ 売建可否の判定には使えません")
    print("=" * 74)
    _LBL = {1: "信用銘柄(制度)", 2: "貸借銘柄(制度)", 3: "その他"}
    _tot_bp = float(tr["bp"].mean())
    for _v, _g in tr.groupby("IssType"):
        _lb = _LBL.get(int(_v), f"区分{int(_v)}") if pd.notna(_v) else "不明"
        print(f"   {_lb:<26} {len(_g):>7,}件 "
              f"({len(_g) / max(1, len(tr)) * 100:>5.1f}%)  "
              f"{_g['bp'].mean():+8.1f}bp/件")
    _lend = tr[tr["IssType"] == 2]
    if len(_lend) and len(_lend) < len(tr):
        print(f"\n   TRAIN 全体      {_tot_bp:+.1f}bp/件")
        print(f"   貸借銘柄だけ    {_lend['bp'].mean():+.1f}bp/件")
    print("\n   ⛔⛔ **この差で銘柄を除外してはいけません。**")
    print("      N は制度信用ではなく **一般信用デイトレ**(MarginTradeType=3)で")
    print("      建てています。信用銘柄でも在庫があれば空売りできます。")
    print("   ★ バックテストの水準が過大かを知りたいなら、必要なのは")
    print("      **『実際に売建できなかった銘柄のペーパー損益』**です:")
    print("        ・ペーパーが **プラス** → バックテストは過大")
    print("        ・ペーパーが **マイナス** → 建てられなくて助かっている")
    print("      → ordered_signals_n.csv のエラーコードと k_paper を突合する")
    print("        (2026-09-18 の実運用: 売建規制3件のペーパーは -9,400円 =")
    print("         建てられなくて **助かった** 側でした)")


# ══════════════════════════════════════════════════════════════════
# 4. 日クラスタ t と分位
# ══════════════════════════════════════════════════════════════════
def _resid(sub: pd.DataFrame, base: pd.DataFrame):
    """(日付, その日の全体平均からの残差) を **対応を崩さずに** 返す。

    ⛔ NaN を落としたあと date を先頭から切ってはいけない(2026-09-18)。
      NaN が途中にあると日付と残差の対応がズレる。**同じマスクで両方を
      絞る**のが正しい。エラーにならず静かに間違うので、たちが悪い。
    """
    if sub.empty or len(sub) < 10:
        return None, None
    _m = base.groupby("date")["bp"].mean()
    _r = sub["bp"].values - sub["date"].map(_m).values
    _ok = ~np.isnan(_r)
    if int(_ok.sum()) < 10:
        return None, None
    return sub["date"].values[_ok], _r[_ok]


def _daily_t(sub: pd.DataFrame, base: pd.DataFrame) -> float:
    """その分位の bp が、同じ日の全体平均とどれだけ違うか(日クラスタ頑健)。

    ★ 件数で t を出すと同日相関で実効サンプルを誤認する(下げた日は全銘柄が
      まとめて勝つ)。そこで **日をクラスタとした頑健標準誤差**を使う。

    ⛔ 「日ごとに平均して、日を単位に t」は駄目だった(2026-09-18 の自己
      テストで、力のある合成データを検出できなかった)。N は1日6〜24件で、
      5分位に割ると **1日1〜5件**しかない。日次平均のノイズが巨大になり、
      検出力が壊滅する。日内の残差を **合計**するのが正しい形。
    """
    _dt, _d = _resid(sub, base)
    if _d is None:
        return float("nan")
    _mean = float(_d.mean())
    _r = pd.DataFrame({"date": _dt, "r": _d - _mean})
    _S = _r.groupby("date")["r"].sum().values
    _var = float((_S ** 2).sum()) / (len(_d) ** 2)
    if _var < _EPS:
        return float("nan")
    return _mean / np.sqrt(_var)


def _daily_se(sub: pd.DataFrame, base: pd.DataFrame) -> float:
    """上と同じ日クラスタ頑健の **標準誤差**(bp)。

    ★ これで **検出できる最小の効果(MDE)** が出る。「候補ゼロ」が
      『効果がない』のか『検出力が足りない』のかを区別するために要る。
      MDE = _PASS_T × SE。これより小さい効果は、あっても見えない。

    ⚠ ここで言う「効果」は **最悪分位と全体平均の差**であって、
      分位間(Q1 と Q5)の差ではない。分位間の差はこの約2倍になるので、
      「Q1-Q5 で 15bp」なら 最悪分位の差は 7〜8bp 程度。MDE と比べる
      ときはこの換算を忘れないこと(2026-09-18 の自己テストで実測)。
    """
    _dt, _d = _resid(sub, base)
    if _d is None:
        return float("nan")
    _r = pd.DataFrame({"date": _dt, "r": _d - _d.mean()})
    _S = _r.groupby("date")["r"].sum().values
    _var = float((_S ** 2).sum()) / (len(_d) ** 2)
    return float(np.sqrt(_var)) if _var > _EPS else float("nan")


def _quantiles(df: pd.DataFrame, ax: str, edges=None):
    """分位に割る。edges を渡すとその境界を使う(TEST 用)。

    ⛔ NaN を **先に落とす**。np.searchsorted は NaN を末尾に置くので、
      落とさないと「分母が0で計算できなかった行」が全部 最上位分位に
      積み上がり、その分位の中身が別物になる(2026-09-18 に第2弾の
      割合軸を足したとき、割合の分母が0になりうるので顕在化した)。
    """
    df = df[df[ax].notna()]
    _v = df[ax]
    if _v.empty:
        out = df.copy()
        out["_q"] = pd.Series(dtype=object)
        return out, (edges if edges is not None else [])
    if edges is None:
        _q = np.linspace(0, 1, _NQ + 1)[1:-1]
        edges = list(np.unique(_v.quantile(_q).values))
    _lab = np.searchsorted(edges, _v.values, side="right")
    out = df.copy()
    out["_q"] = [f"Q{int(i) + 1}" for i in _lab]
    return out, edges


def _report(df: pd.DataFrame, ax: str, edges=None, title=""):
    _n0 = len(df)
    _d, edges = _quantiles(df, ax, edges)
    _unit = "割合" if ax in _AXES_SHARE else "日数"
    _lost = _n0 - len(_d)
    print(f"\n  ── {ax} {title} ─────────────────────────────")
    if _lost:
        print(f"    ⚠ {ax} が計算できない行を {_lost:,}件 除外 "
              f"({_lost / max(1, _n0) * 100:.1f}%)")
    print(f"    {'分位':<5}{'件数':>8}{'bp/件':>10}{'日t':>8}   実数境界({_unit})")
    rows = []
    for i in range(_NQ):
        _q = f"Q{i + 1}"
        _s = _d[_d["_q"] == _q]
        if _s.empty:
            continue
        _bp = float(_s["bp"].mean())
        _t = _daily_t(_s, _d)
        _fmt = ".3f" if ax in _AXES_SHARE else ".2f"
        _lo = "-inf" if i == 0 else f"{edges[i - 1]:{_fmt}}"
        _hi = "+inf" if i >= len(edges) else f"{edges[i]:{_fmt}}"
        print(f"    {_q:<5}{len(_s):>8,}{_bp:>+10.1f}{_t:>+8.2f}   "
              f"{_lo} 〜 {_hi}")
        rows.append((_q, len(_s), _bp, _t))
    return _d, edges, rows


def _null_worst(df: pd.DataFrame, n: int, seed: int) -> np.ndarray:
    """帰無較正: 同じ日の中で分位ラベルだけ入れ替える。

    ⛔ 日をまたいでシャッフルしない。日内相関が壊れて帰無分布が狭くなり、
      偽陽性を過小評価する。
    ★ 「最悪分位を選ぶ」操作込みで較正する。最小を選ぶだけで平均が
      下振れするので、0 と比べてはいけない。
    """
    rng = np.random.default_rng(seed)
    # ⛔ groupby(...).index.values は **元の index ラベル**であって位置では
    #   ない。numpy 配列の添字に使うと、ラベルが 0..n-1 でないときズレる
    #   (2026-09-18 に実データで IndexError。合成データでは TRAIN が先頭に
    #    固まっていて偶然 0..n-1 だったので露見しなかった)。
    #   ⚠ 範囲内にさえ収まれば **エラーも出ず静かに間違う**ので、
    #     必ず位置ベースに直してから使うこと。
    df = df.reset_index(drop=True)
    _g = df.groupby("date")
    _idx = [g.index.values for _, g in _g]
    _lab = df["_q"].values
    _bp = df["bp"].values
    assert all(int(ix.max()) < len(_lab) for ix in _idx if len(ix)), \
        "reset_index が効いていません"
    _qs = sorted(set(_lab))
    out = np.empty(n, dtype=float)
    for k in range(n):
        _sh = _lab.copy()
        for ix in _idx:
            _sh[ix] = rng.permutation(_lab[ix])
        _m = [_bp[_sh == q].mean() if (_sh == q).any() else np.nan for q in _qs]
        out[k] = np.nanmin(_m)
    return out


# ══════════════════════════════════════════════════════════════════
# 5b. 事前登録された交差条件を1つだけ評価する (--confirm-cross)
# ══════════════════════════════════════════════════════════════════
#   ⛔ このツールは「交互作用は掃かない」で凍結してある。ここは **掃く**
#     経路ではない: 外で事前に登録された条件を **1つだけ** 受け取り、
#     独立実装で評価するためのもの。引数は1ペアしか受け付けない。
#   ★ 実数(1.58日 など)ではなく **分位** で受けるのは、母集団や adv20 の
#     出所が違うと絶対値がズレるから。分位なら「同じ位置」を選べる。
if a.confirm_cross:
    try:
        _bq, _sq = (float(x) for x in a.confirm_cross.split(":", 1))
    except Exception:
        _die("--confirm-cross は <買残の上位%>:<売残の下位%> です (例 80:20)")
    if not (0 < _bq < 100 and 0 < _sq < 100):
        _die("百分位は 0〜100 の間で指定してください")

    print("\n" + "=" * 74)
    print(f" ★ 交差条件を1つだけ評価 — 買残 上位{100 - _bq:.0f}% "
          f"かつ 売残 下位{_sq:.0f}%")
    print("=" * 74)
    print("   ⛔ これは **外で事前登録された1条件** の独立評価です。")
    print("      ここで分位を変えて試し直したら、それは掃いたことになります。")

    # ★ 境界は TRAIN で決める。TEST には同じ境界を当てる
    _b_edge = float(tr["buy_days"].quantile(_bq / 100.0))
    _s_edge = float(tr["sell_days"].quantile(_sq / 100.0))
    print(f"\n   境界(TRAIN で決定)  買残 >= {_b_edge:.3f}日 / "
          f"売残 <= {_s_edge:.4f}日")

    def _cross(df):
        _in = df[(df["buy_days"] >= _b_edge) & (df["sell_days"] <= _s_edge)]
        return _in, df.drop(_in.index)

    _tr_in, _tr_out = _cross(tr)
    print(f"\n   ── TRAIN ────────────────────────────────")
    print(f"     該当   {len(_tr_in):>7,}件 / {_tr_in['date'].nunique():>4,}営業日"
          f"  {_tr_in['bp'].mean():+8.1f}bp/件"
          f"  ({len(_tr_in) / max(1, tr['date'].nunique()):.2f}件/日)")
    print(f"     それ以外 {len(_tr_out):>5,}件"
          f"                   {_tr_out['bp'].mean():+8.1f}bp/件")
    _t_tr = _daily_t(_tr_in, tr)
    _se_tr = _daily_se(_tr_in, tr)
    print(f"     日クラスタ t {_t_tr:+.2f}  (SE {_se_tr:.1f}bp → "
          f"**{_PASS_T * _se_tr:.0f}bp より小さい効果は見えません**)")
    if len(_tr_in) < 300:
        print(f"     ⚠ **300件未満**。実効サンプルは "
              f"{_tr_in['date'].nunique()}日しかなく t は不安定です")

    # 前半・後半
    _hf = tr["date"].quantile(0.5)
    _v1 = _cross(tr[tr["date"] <= _hf])[0]["bp"].mean()
    _v2 = _cross(tr[tr["date"] > _hf])[0]["bp"].mean()
    _sgn = (not np.isnan(_v1)) and (not np.isnan(_v2)) and ((_v1 > 0) == (_v2 > 0))
    print(f"     前半 {_v1:+.1f} / 後半 {_v2:+.1f}  "
          f"{'✅ 符号一致' if _sgn else '⛔ 反転(期間依存)'}")

    # ★ 帰無較正: 同じ日の中で「該当/非該当」のラベルだけ入れ替える
    _rng = np.random.default_rng(a.seed)
    _tt = tr.reset_index(drop=True)
    _flag = np.zeros(len(_tt), dtype=bool)
    _flag[((_tt["buy_days"] >= _b_edge)
           & (_tt["sell_days"] <= _s_edge)).values] = True
    _idx = [g.index.values for _, g in _tt.groupby("date")]
    _bpv = _tt["bp"].values
    _null = np.empty(a.nulls, dtype=float)
    for _k in range(a.nulls):
        _sh = _flag.copy()
        for _ix in _idx:
            _sh[_ix] = _rng.permutation(_flag[_ix])
        _null[_k] = _bpv[_sh].mean() if _sh.any() else np.nan
    _obs = float(_tr_in["bp"].mean())
    _p_hi = float(np.nanpercentile(_null, _PASS_NULL))
    _p_lo = float(np.nanpercentile(_null, 100.0 - _PASS_NULL))
    print(f"     帰無({a.nulls}本 / 同日シャッフル) 中央 "
          f"{np.nanmedian(_null):+.1f} / {100 - _PASS_NULL:.0f}%点 {_p_lo:+.1f} "
          f"/ {_PASS_NULL:.0f}%点 {_p_hi:+.1f}bp")
    _out_null = (_obs > _p_hi) or (_obs < _p_lo)
    print(f"     帰無の外か … {'✅' if _out_null else '⛔ 帯の中'}")

    _tr_ok = (abs(_t_tr) >= _PASS_T) and _sgn and _out_null
    print(f"\n   TRAIN 判定 … "
          + ("✅ 通過。TEST を開きます" if _tr_ok
             else "⛔ **不合格。TEST は開きません**"))
    if not _tr_ok:
        print("\n   ▶ この条件は閉じます。基準を緩めて再判定しないこと。")
        print("     ⚠ TEST は1回も使っていません(温存されています)。")
        sys.exit(0)

    if te.empty:
        _die("TEST の行がありません")
    _te_in, _te_out = _cross(te)
    print(f"\n   ── TEST (この条件について、これ1回) ─────────────")
    print(f"     該当   {len(_te_in):>7,}件 / {_te_in['date'].nunique():>4,}営業日"
          f"  {_te_in['bp'].mean():+8.1f}bp/件")
    print(f"     それ以外 {len(_te_out):>5,}件"
          f"                   {_te_out['bp'].mean():+8.1f}bp/件")
    _t_te = _daily_t(_te_in, te)
    print(f"     日クラスタ t {_t_te:+.2f}")
    _hf2 = te["date"].quantile(0.5)
    _w1 = _cross(te[te["date"] <= _hf2])[0]["bp"].mean()
    _w2 = _cross(te[te["date"] > _hf2])[0]["bp"].mean()
    _sgn2 = ((not np.isnan(_w1)) and (not np.isnan(_w2))
             and ((_w1 > 0) == (_w2 > 0)))
    print(f"     前半 {_w1:+.1f} / 後半 {_w2:+.1f}  "
          f"{'✅ 符号一致' if _sgn2 else '⛔ 反転'}")
    _same = (_obs > 0) == (_te_in["bp"].mean() > 0)
    print(f"\n   TRAIN と TEST で符号一致 … {'✅' if _same else '⛔ **反転**'}")
    print(f"   TEST の日クラスタ t >= {_PASS_T} … "
          f"{'✅' if abs(_t_te) >= _PASS_T else '⛔'}")
    if _same and abs(_t_te) >= _PASS_T and _sgn2:
        print("\n   ▶ ★ 通りました。ただし採用の前に:")
        print("     ① 予算シミュを通す(総額比較だけで発注ルールを決めない)")
        print("     ② 該当が何件/日か見る。除外に使うのか選抜に使うのか決める")
        print("     ③ 実運用では規制銘柄が既に建てられない点を差し引く")
    else:
        print("\n   ▶ ⛔ 不合格。**この条件は閉じます**。")
        print("     基準を緩めて再判定しないこと。TEST は使い切りました。")
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════
# 5. TEST を1回だけ使う (--confirm)
# ══════════════════════════════════════════════════════════════════
if a.confirm:
    if ":" not in a.confirm:
        _die("--confirm は axis:Qn の形です (例 sell_days:Q5)")
    _ax, _q = a.confirm.split(":", 1)
    if _ax not in _AXES:
        _die(f"軸は {_AXES} のいずれかです")
    if te.empty:
        _die("TEST の行がありません")

    print("\n" + "=" * 74)
    print(f" ★ TEST で確認 — {_ax} {_q}  (この軸について TEST はこれ1回)")
    print("=" * 74)

    _dtr, _edges, _ = _report(tr, _ax, None, "(TRAIN / 境界を決める)")
    _dte, _, _ = _report(te, _ax, _edges, "(TEST / TRAIN の境界を当てる)")

    _s_tr = _dtr[_dtr["_q"] == _q]
    _s_te = _dte[_dte["_q"] == _q]
    if _s_tr.empty or _s_te.empty:
        _die(f"{_q} の行がありません")

    _bt, _be = float(_s_tr["bp"].mean()), float(_s_te["bp"].mean())
    _tt, _tte = _daily_t(_s_tr, _dtr), _daily_t(_s_te, _dte)
    print(f"\n  TRAIN {_q}  {_bt:+.1f}bp/件  日t {_tt:+.2f}")
    print(f"  TEST  {_q}  {_be:+.1f}bp/件  日t {_tte:+.2f}")
    _same = (_bt < 0) == (_be < 0)
    print(f"\n  符号一致 … {'✅' if _same else '⛔ **反転**'}")
    print(f"  TEST で明確にマイナス … "
          f"{'✅' if (_be < 0 and _tte <= -_PASS_T) else '⛔'}")
    if _same and _be < 0 and _tte <= -_PASS_T:
        print("\n  ▶ 通りました。次は sim(予算・上限キャンセル込み)で確認してください。")
        print("    ⚠ compare 系の総額比較だけで発注ルールを決めないこと。")
    else:
        print("\n  ▶ 不合格。**この軸は閉じます**。基準を緩めて再判定しないこと。")
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════
# 6. TRAIN で掃く
# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 74)
print(f" TRAIN で掃く — {len(_AXES)}軸。⛔ 交互作用は掃かない")
print("=" * 74)

_half = tr["date"].quantile(0.5)
_h1, _h2 = tr[tr["date"] <= _half], tr[tr["date"] > _half]

_cand: list = []
_mdes: list = []
for _ax in _AXES:
    _d, _edges, _rows = _report(tr, _ax, None, "(TRAIN)")
    if not _rows:
        continue

    # 最悪分位(= 除外候補)
    _w = min(_rows, key=lambda r: r[2])
    _q, _n, _bp, _t = _w

    # ③ 帰無較正
    _null = _null_worst(_d, a.nulls, a.seed)
    _p5 = float(np.percentile(_null, 100.0 - _PASS_NULL))
    print(f"    最悪 {_q}  {_bp:+.1f}bp/件  日t {_t:+.2f}")
    print(f"    帰無({a.nulls}本 / 同日シャッフル・最悪を選ぶ操作込み) "
          f"中央 {np.median(_null):+.1f} / {_PASS_NULL:.0f}%点 {_p5:+.1f}bp")

    # ④ 前半・後半
    _b1 = _b2 = float("nan")
    for _lbl, _sub in (("前半", _h1), ("後半", _h2)):
        _dd, _ = _quantiles(_sub, _ax, _edges)
        _ss = _dd[_dd["_q"] == _q]
        _v = float(_ss["bp"].mean()) if not _ss.empty else float("nan")
        if _lbl == "前半":
            _b1 = _v
        else:
            _b2 = _v
    _sign_ok = (not np.isnan(_b1) and not np.isnan(_b2)
                and (_b1 < 0) == (_b2 < 0))
    print(f"    前半 {_b1:+.1f} / 後半 {_b2:+.1f}  "
          f"{'✅ 符号一致' if _sign_ok else '⛔ 反転(期間依存)'}")

    # 判定
    _c1 = _bp < 0
    _c2 = (not np.isnan(_t)) and _t <= -_PASS_T
    _c3 = _bp < _p5
    _c4 = _sign_ok
    _all = _c1 and _c2 and _c3 and _c4
    print(f"    判定 ①マイナス {'✅' if _c1 else '⛔'} / "
          f"②日t<=-{_PASS_T} {'✅' if _c2 else '⛔'} / "
          f"③帰無超え {'✅' if _c3 else '⛔'} / "
          f"④符号一致 {'✅' if _c4 else '⛔'}  → "
          + ("★ 候補" if _all else "不合格"))
    # ★ 検出力。これを出さないと「候補ゼロ」を『効果なし』と読み違える
    _se = _daily_se(_d[_d["_q"] == _q], _d)
    if not np.isnan(_se):
        _mde = _PASS_T * _se
        _mdes.append(_mde)
        print(f"    検出力 SE {_se:.1f}bp → **{_mde:.1f}bp より小さい効果は"
              f"見えません**(t={_PASS_T} に必要な差)")
    if _all:
        _cand.append((_ax, _q, _bp, _t))

print("\n" + "=" * 74)
if _cand:
    print(" ★ TRAIN を通った候補")
    for _ax, _q, _bp, _t in _cand:
        print(f"    {_ax}:{_q}  {_bp:+.1f}bp/件  日t {_t:+.2f}")
    print("\n ▶ 次(1候補につき1回だけ):")
    for _ax, _q, _, _ in _cand:
        print(f"    python analyze_margin_days.py --picks {a.picks} "
              f"--margin {a.margin} --confirm {_ax}:{_q}")
else:
    print(" ⛔ **候補ゼロ。週次信用残ルートを閉じます。**")
    print("    TEST は1回も使っていません(次の軸のために温存されています)。")
    # ★★ 「効果がない」と「見えない」を混同しないための注記
    if _mdes:
        _md = float(np.median(_mdes))
        print(f"\n ⚠ ただし **検出できる下限は {_md:.0f}bp/件** です"
              f"(TRAIN {len(tr):,}件 / {tr['date'].nunique():,}営業日)。")
        print(f"    これより小さい効果は、あっても この窓では見えません。")
        print(f"    『効果がない』ではなく『{_md:.0f}bp 以上の効果は無い』"
              "が正確な結論です。")
        print(f"    ⚠ 参考: N のグロスは +15.7bp/件(2015-2020)。"
              "選別軸がその数倍の識別力を持つことは考えにくいので、")
        print(f"      **この窓で検出できる水準の効果は元から期待しにくい**"
              "という読み方になります。")
print("=" * 74)
