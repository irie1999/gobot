r"""analyze_market_beta.py — lss の日次損益が『その日の相場』にどれだけ支配されているかを測る。

なぜやるか
----------
lss は毎日十数銘柄を **全部ショート** し、同日に決済する。したがって
相場が上げた日はほぼ全銘柄が負け、下げた日はほぼ全銘柄が勝つ。
日々の損益の大部分が「その日の相場」という運で説明できてしまうなら、
**日経先物を買っておけばその部分だけ打ち消せる**。

  ・平均の儲け(α)は変わらない
  ・ブレ(σ)だけが小さくなる
  ・σ が半分になれば t は2倍、勝ちの確定に要する期間は 1/4

いま lss は 月平均 +50,135円 / 月次σ 106,454円 / t=+1.18 で、
確定に **約29ヶ月** かかる(CLAUDE.md 18.24)。ここがボトルネック。

ヘッジのコストはほぼゼロと分かっている: 日経の日中リターン(寄り→引け)は
無条件では **ゼロ**(18.19: Sharpe 0.42 / 95%CIがゼロをまたぐ)。
18.19 は「先物で稼ぐ案は死んだ」と結論したが、**ヘッジ用途では
ドリフトゼロのほうが好都合**(毎日買っても平均的に損得なし)。

やること
--------
  日次 lss損益  =  α  +  β × 日経の日中リターン  +  ε
を最小二乗で推定し、

  ・β が有意か(t検定)。有意でなければヘッジしても無意味 → その場で棄却
  ・ヘッジ後(= 残差 ε)の σ が元よりどれだけ小さいか
  ・ヘッジ後の月次 t がいくつになるか
  ・必要な先物の建玉(円)と枚数の目安

を出す。**日次リターンは寄り→引け**を使う(lssは寄りで建てて引けまでに閉じるため)。

使い方
------
  python analyze_market_beta.py                        # lss_trades.csv を読む
  python analyze_market_beta.py --trades lss_trades.csv --budget 4000000
  python analyze_market_beta.py --top 13               # 日内 上位N件だけで日次集計
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss日次損益 vs 日経日中リターン の回帰")
ap.add_argument("--trades", default="lss_trades.csv")
ap.add_argument("--exclude-strat", default="転換",
                help="除外する戦略(既定『転換』= lssではない / 18.5.3)")
ap.add_argument("--top", type=int, default=0,
                help="日内 流動性上位N件だけで日次損益を作る(0=全件)。"
                     "予算400万は1日十数件で飽和するので 13 程度が実運用に近い")
ap.add_argument("--budget", type=float, default=4_000_000.0,
                help="ヘッジ必要額の表示に使う想定証拠金")
args = ap.parse_args()

p = Path(args.trades)
if not p.exists():
    sys.exit(f"[error] {p} が見つかりません。先に .\\daily を実行してください。")
d = pd.read_csv(p, encoding="utf-8-sig")

if args.exclude_strat.strip() and "strategy" in d.columns:
    _ex = {s.strip() for s in args.exclude_strat.split(",") if s.strip()}
    _hit = d["strategy"].astype(str).isin(_ex)
    if _hit.any():
        print(f"[入力] 戦略 {'/'.join(sorted(_ex))} を除外: {int(_hit.sum()):,}件")
        d = d[~_hit]
# 不約定行(pnl=0/reason=約定せず)は日次損益に寄与しないので落とす
if "reason" in d.columns:
    d = d[d["reason"].astype(str) != "約定せず"]
d = d[pd.to_numeric(d["pnl"], errors="coerce").notna()].copy()
d["pnl"] = pd.to_numeric(d["pnl"], errors="coerce")
d["date"] = pd.to_datetime(d["entry_date"], errors="coerce")
d = d[d["date"].notna()]

if args.top > 0 and "liquidity" in d.columns:
    d["_liq"] = pd.to_numeric(d["liquidity"], errors="coerce").fillna(0.0)
    d = (d.sort_values(["date", "_liq"], ascending=[True, False])
           .groupby("date", group_keys=False).head(args.top))
    print(f"[入力] 日内 流動性上位{args.top}件に絞込 → {len(d):,}件")

daily = d.groupby(d["date"].dt.date)["pnl"].sum().sort_index()
print(f"[入力] {p.name}: {len(d):,}取引 / {len(daily):,}営業日 "
      f"/ {daily.index.min()}〜{daily.index.max()}")
print(f"       合計 {daily.sum():+,.0f}円 / 日平均 {daily.mean():+,.0f}円 "
      f"/ 日次σ {daily.std(ddof=1):,.0f}円")

# ── 日経の日中リターン(寄り→引け) ────────────────────────────────
try:
    import yfinance as yf
    _raw = yf.Ticker("^N225").history(
        start=str(daily.index.min() - pd.Timedelta(days=5).to_pytimedelta()),
        end=str(daily.index.max() + pd.Timedelta(days=2).to_pytimedelta()),
        interval="1d", auto_adjust=False, actions=False)
except Exception as e:
    sys.exit(f"[error] 日経(^N225)を取得できません: {e}")
if _raw is None or len(_raw) < 10:
    sys.exit("[error] 日経のデータが足りません")
_raw.index = pd.to_datetime(_raw.index).tz_localize(None)
# lss は寄りで建てて引けまでに閉じる → 見るべきは日中(寄り→引け)リターン
n_ret = ((_raw["Close"] / _raw["Open"]) - 1.0)
n_ret.index = n_ret.index.date

x, y = [], []
for dt, v in daily.items():
    r = n_ret.get(dt)
    if r is not None and r == r:
        x.append(float(r)); y.append(float(v))
if len(x) < 20:
    sys.exit(f"[error] 突合できた日が {len(x)}日しかありません")
n = len(x)
mx, my = sum(x) / n, sum(y) / n
sxx = sum((v - mx) ** 2 for v in x)
sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
beta = sxy / sxx if sxx else 0.0
alpha = my - beta * mx
resid = [y[i] - (alpha + beta * x[i]) for i in range(n)]
sse = sum(v * v for v in resid)
sst = sum((v - my) ** 2 for v in y)
r2 = 1 - sse / sst if sst else 0.0
se_b = math.sqrt((sse / (n - 2)) / sxx) if sxx and n > 2 else float("nan")
t_b = beta / se_b if se_b and se_b == se_b else float("nan")

sd_raw = (sst / (n - 1)) ** 0.5
sd_hed = (sse / (n - 1)) ** 0.5

print(f"\n{'=' * 84}")
print("■ 日次 lss損益 = α + β × 日経の日中リターン(寄り→引け)")
print(f"{'=' * 84}")
print(f"  突合 {n:,}営業日")
print(f"  β  {beta:>+14,.0f} 円 / 日経1.0(=100%)  →  日経が **+1%** 動くと "
      f"{beta * 0.01:>+10,.0f}円")
print(f"  α  {alpha:>+14,.0f} 円/日 (相場を除いた実力の部分)")
print(f"  R² {r2:>14.3f}   ← 日次損益の何割が『その日の相場』で説明できるか")
print(f"  βのt値 {t_b:>+10.2f}")

print(f"\n{'─' * 84}")
print("■ ヘッジしたらどうなるか")
print(f"{'─' * 84}")
print(f"  {'':<16}{'日次σ':>14}{'月次σ(20日)':>16}{'月平均':>14}{'月次t':>10}")
_mo_raw, _mo_hed = sd_raw * math.sqrt(20), sd_hed * math.sqrt(20)
_m_raw, _m_hed = my * 20, alpha * 20
# 月次t は「n_months = 営業日/20 ヶ月」で近似
_nm = n / 20
_t_raw = _m_raw / (_mo_raw / math.sqrt(_nm)) if _mo_raw else float("nan")
_t_hed = _m_hed / (_mo_hed / math.sqrt(_nm)) if _mo_hed else float("nan")
print(f"  {'現状':<16}{sd_raw:>+14,.0f}{_mo_raw:>+16,.0f}{_m_raw:>+14,.0f}{_t_raw:>+10.2f}")
print(f"  {'ヘッジ後':<16}{sd_hed:>+14,.0f}{_mo_hed:>+16,.0f}{_m_hed:>+14,.0f}{_t_hed:>+10.2f}")
_cut = (1 - sd_hed / sd_raw) * 100 if sd_raw else 0.0
print(f"  σ削減 {_cut:>6.1f}%")
for lab, t_, m_ in (("現状", _t_raw, _m_raw), ("ヘッジ後", _t_hed, _m_hed)):
    if t_ == t_ and t_ != 0 and m_ != 0:
        _need = (2.0 / abs(t_)) ** 2 * _nm
        print(f"  {lab}: t=2 に到達するのに **約{_need:.0f}ヶ月**")

print(f"\n{'─' * 84}")
print("■ 判定")
print(f"{'─' * 84}")
if abs(t_b) < 2:
    print(f"  βのt値 {t_b:+.2f} は有意でない。**相場との連動は測れていない。**")
    print("  → ヘッジしても分散は削れない。**この案は棄却。**")
elif _cut < 15:
    print(f"  βは有意(t={t_b:+.2f})だが、σ削減は {_cut:.1f}% しかない。")
    print("  → 先物の手数料・建玉管理の手間に見合わない。**採用は微妙。**")
else:
    _notional = -beta          # β<0(ショート本体)なら 正 = 日経を『買う』
    print(f"  βは有意(t={t_b:+.2f})で、σを {_cut:.1f}% 削減できる。**検討に値する。**")
    print(f"  必要な日経建玉: {_notional:>+,.0f}円 "
          f"({'買い(ロング)' if _notional > 0 else '売り(ショート)'})")
    print(f"    ミニ先物(指数×100) ≒ {abs(_notional) / 4_000_000:.2f}枚 相当"
          f" / マイクロ(指数×10) ≒ {abs(_notional) / 400_000:.1f}枚 相当")
    print(f"    ※ 指数40,000円想定。実際の枚数は当日の指数で計算すること")
    print(f"  想定証拠金{args.budget:,.0f}円に対する建玉比 "
          f"{abs(_notional) / args.budget:.2f}倍")

print("\n  ⚠ 前提と注意")
print("     ・日中リターンは寄り→引け。lss は寄りで建てて引けまでに閉じるため。")
print("     ・ヘッジのコストはほぼゼロ(18.19: 日経の日中ドリフトは無条件でゼロ)。")
print("       ただし先物の手数料・スプレッド・証拠金は別途かかる。")
print("     ・βは期間で変わる。採用するなら月別のβも見て安定しているか確かめること。")
print("     ・αが小さいままなら、ヘッジは『負けを見えやすくする』だけで儲けは増えない。")

# ── 月別のβ(安定しているか) ──────────────────────────────────────
_df = pd.DataFrame({"d": list(daily.index)[:0]})  # placeholder for clarity
_rows = []
_bym: dict = {}
for i, dt in enumerate([dt for dt in daily.index if n_ret.get(dt) is not None
                        and n_ret.get(dt) == n_ret.get(dt)]):
    _bym.setdefault(str(dt)[:7], []).append((float(n_ret.get(dt)), float(daily[dt])))
print(f"\n{'─' * 84}")
print("■ 月別のβ (安定していなければヘッジ比率を固定できない)")
print(f"{'─' * 84}")
print(f"  {'月':<10}{'日数':>6}{'β':>16}{'R²':>8}{'損益':>14}")
for mo in sorted(_bym):
    v = _bym[mo]
    if len(v) < 5:
        continue
    _x = [a for a, _ in v]; _y = [b for _, b in v]
    _mx, _my = sum(_x) / len(_x), sum(_y) / len(_y)
    _sxx = sum((a - _mx) ** 2 for a in _x)
    _sxy = sum((_x[i] - _mx) * (_y[i] - _my) for i in range(len(v)))
    _b = _sxy / _sxx if _sxx else 0.0
    _a = _my - _b * _mx
    _sse = sum((_y[i] - (_a + _b * _x[i])) ** 2 for i in range(len(v)))
    _sst = sum((b - _my) ** 2 for b in _y)
    _r2 = 1 - _sse / _sst if _sst else 0.0
    print(f"  {mo:<10}{len(v):>6}{_b:>+16,.0f}{_r2:>8.2f}{sum(_y):>+14,.0f}")
