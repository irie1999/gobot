"""
N 停止判定器 — 日経「始値→終値」上昇予測による発注制御の検証
────────────────────────────────────────────────────────────────────────
【運用課題】
  N は寄り後にギャップアップ銘柄を空売りし、引けで決済する。
  大負け日が「日経が日中に上昇した日」に集中する (実測 R^2 ≈ 0.235)。
  ⇒ 日経の日中上昇を予測し、警報日だけ N を停止 / 縮小 / ヘッジできれば
    月平均÷σ・CVaR・最大DD が改善するはず。

【判定は以下の順序で行う】
  1. 上昇警報の precision (D10 の適合率) が目標を超えるか
  2. 警報日の N 損益合計がマイナスか (= 実際に損している日を捉えているか)
  3. ランダムに同数休んだ場合より良いか
  4. 一律縮小より 月平均÷σ・CVaR が良いか
  5. 月ブロック帰無較正で有意か (探索全体の多重性への対処)
  6. 待機コストを引いても成立するか

【対照群について】
  ・ランダム休場: 何日か休めばσもDDも機械的に下がるため、無停止との比較は無意味。
  ・一律縮小:  最も鋭い対照群。全日を (1-q) 倍にすると 月平均÷σ と Sharpe は
              数学的に一切変わらない。したがって選択的 veto でこれらが改善したら、
              その改善は純粋に「日を選べている」ことに由来する。
  ・月ブロック帰無: スコアを月ブロック単位で並べ替えて帰無分布を作る。
              日内・月内の自己相関を保ったまま スコア↔損益 の対応だけを壊す。

【待機コストの非対称性 (重要)】
  日経始値を待って予測するなら、veto するかどうかに関わらず **毎日待つ**。
  つまり待機コストは「建てた全日」にかかり、休んだ日にはかからない。
  veto 率 10% なら「1割の日を避けるために9割の日で待機コストを払う」。
  --wait-cost で差し引けるほか、損益分岐待機コスト (これ以下なら元が取れる額) を出す。

使い方:
  python n225_veto.py --npnl n_pnl.csv              # N の実損益 (推奨)
  python n225_veto.py                                # 代理損益で配管確認
  python n225_veto.py --mode hedge                   # 警報日に日経で条件付きヘッジ
  python n225_veto.py --mode shrink --veto-size 0.5  # 警報日は半分に縮小
  python n225_veto.py --wait-cost 300                # 待機コスト 300円/日 を差し引く
  python n225_veto.py --holdout-start 2024-01-01     # 未使用期間を分離して報告
  python n225_veto.py --asof strict                  # 当日ギャップを使わない
  python n225_veto.py --selftest                     # ネット不要の自己テスト
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

from n225_research import (
    Gbdt,
    LogisticL2,
    PANEL_CSV,
    load_panel,
    make_features,
    make_synthetic_panel,
    make_targets,
    walk_forward,
)

TRADING_DAYS = 252
DEFAULT_R2   = 0.235      # 実測: 日経 始値→終値 が N 損益を説明する決定係数
ALARM_RATES  = (0.05, 0.10, 0.15, 0.20, 0.30)
N_BOOTSTRAP  = 2000
N_PERM       = 2000


# ══════════════════════════════════════════════════════════════
#  N の損益系列
# ══════════════════════════════════════════════════════════════
def load_n_pnl(path: Path) -> pd.Series:
    """CSV (date,pnl) を読み込む。列名が合わなければ先頭2列を使う。"""
    if not path.exists():
        raise SystemExit(
            f"[error] {path} が見つかりません。\n"
            f"  N の日次損益 CSV を用意するか、--npnl を外して代理損益で走らせてください。\n"
            f"    python n225_veto.py --asof at_open        # 代理損益 (判定1 の precision は意味を持つ)\n"
            f"  CSV の形式 (先頭2列を日付・損益として読みます。ヘッダ名は任意):\n"
            f"    date,pnl\n"
            f"    2024-01-04,-12500\n"
            f"    2024-01-05,8300\n"
            f"  1 日 1 行に集約されていなくても、同じ日付の行は自動で合計します。")
    df = pd.read_csv(path)
    date_col = next((c for c in df.columns if c.lower() in
                     ("date", "日付", "day", "trade_date", "record_date")), df.columns[0])
    pnl_col = next((c for c in df.columns if c.lower() in
                    ("pnl", "profit", "損益", "pl", "return", "ret")), df.columns[1])
    s = pd.Series(pd.to_numeric(df[pnl_col], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df[date_col]).dt.normalize(), name="pnl")
    return s.dropna().groupby(level=0).sum().sort_index()


def surrogate_n_pnl(r_day: pd.Series, r2: float = DEFAULT_R2,
                    target_sharpe: float = 1.0, seed: int = 7) -> pd.Series:
    """
    N の代理損益。日経日中リターンとの相関だけを実測 R^2 に合わせて再現する。
    ⚠ 銘柄選択・約定の効果は含まない。配管確認用であって運用判断の根拠にはならない。
    """
    rng = np.random.default_rng(seed)
    rho = -math.sqrt(r2)
    z_r = (r_day - r_day.mean()) / r_day.std()
    core = rho * z_r.to_numpy() + math.sqrt(1 - r2) * rng.standard_normal(len(r_day))
    core = core / core.std()
    return pd.Series(core + target_sharpe / math.sqrt(TRADING_DAYS),
                     index=r_day.index, name="pnl")


# ══════════════════════════════════════════════════════════════
#  リスク指標 (numpy 実装。ブートストラップで数千回回すので高速版)
# ══════════════════════════════════════════════════════════════
def month_index(idx: pd.DatetimeIndex) -> tuple[np.ndarray, int]:
    key = np.asarray(idx.year) * 12 + np.asarray(idx.month)
    _, inv = np.unique(key, return_inverse=True)
    return inv.astype(np.int64), int(inv.max()) + 1


def risk_stats(x: np.ndarray, minv: np.ndarray, nmon: int,
               active: int | None = None) -> dict:
    """日次損益配列から運用指標を計算する。x は「建てなかった日は 0」で渡す。"""
    x = np.asarray(x, dtype=float)
    if len(x) == 0 or x.std() == 0:
        return {k: float("nan") for k in
                ("days", "m_mean", "m_std", "m_ratio", "sharpe", "cvar95",
                 "cvar_per_mean", "maxdd", "maxdd_per_mean", "total")}

    monthly = np.bincount(minv, weights=x, minlength=nmon)
    m_mean = float(monthly.mean())
    m_std  = float(monthly.std(ddof=1)) if nmon > 1 else float("nan")

    k = max(int(len(x) * 0.05), 1)
    cvar95 = float(np.partition(x, k - 1)[:k].mean())      # 下位5%の平均

    eq = np.cumsum(x)
    maxdd = float((eq - np.maximum.accumulate(eq)).min())

    # CVaR と 最大DD は露出に比例して縮むので、一律縮小と比べるには
    # 月平均で割ってスケール不変にする必要がある (m_ratio と同じ理屈)。
    denom = m_mean if abs(m_mean) > 1e-12 else float("nan")
    return {
        "days":    len(x) if active is None else active,
        "m_mean":  m_mean,
        "m_std":   m_std,
        "m_ratio": m_mean / m_std if m_std and m_std > 0 else float("nan"),
        "sharpe":  float(x.mean() / x.std() * math.sqrt(TRADING_DAYS)),
        "cvar95":  cvar95,
        "cvar_per_mean": cvar95 / denom,
        "maxdd":   maxdd,
        "maxdd_per_mean": maxdd / denom,
        "total":   float(x.sum()),
    }


def _stats_header() -> None:
    print(f"{'':<26}{'建てた日':>9}{'月平均':>11}{'月σ':>10}{'月平均/σ':>10}"
          f"{'Sharpe':>9}{'CVaR95':>11}{'最大DD':>11}{'累計':>12}")
    print("─" * 99)


def _print_stats(label: str, st: dict) -> None:
    print(f"{label:<26}{st['days']:>9}{st['m_mean']:>11.3f}{st['m_std']:>10.3f}"
          f"{st['m_ratio']:>10.3f}{st['sharpe']:>9.2f}{st['cvar95']:>11.3f}"
          f"{st['maxdd']:>11.2f}{st['total']:>12.2f}")


# ══════════════════════════════════════════════════════════════
#  スコア生成 (walk-forward)
# ══════════════════════════════════════════════════════════════
def build_scores(panel: pd.DataFrame, args) -> pd.DataFrame:
    tg = make_targets(panel)
    feats = make_features(panel, "day", asof=args.asof)

    data = feats.copy()
    data["_r"] = tg["day"]
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < args.min_train + args.step:
        raise SystemExit(f"有効行 {len(data)} 件。min_train={args.min_train}+step={args.step} に不足。")

    r_day = data.pop("_r")
    thr = args.strong_pct / 100.0
    y = (r_day.to_numpy() > thr).astype(int)
    X = data.to_numpy(dtype=float)

    print(f"■ スコア生成   asof={args.asof}   特徴量 {X.shape[1]}   有効行 {len(data)} "
          f"({data.index[0].date()} .. {data.index[-1].date()})")
    print(f"  『強い上昇日』の定義: 始値→終値 > +{args.strong_pct:.2f}%  "
          f"→ 全期間で {y.mean()*100:.1f}% ({y.sum()} 日)")
    print(f"  walk-forward: min_train={args.min_train}, step={args.step}, embargo={args.embargo}")

    wanted = {"logistic": lambda: LogisticL2(lam=args.lam), "gbdt": lambda: Gbdt()}
    names = ["logistic", "gbdt"] if args.model == "both" else [args.model]

    out = None
    for name in names:
        if name == "gbdt":
            try:
                import sklearn  # noqa: F401
            except ImportError:
                print("  [info] sklearn 未導入のため GBDT はスキップ")
                continue
        idx, p = walk_forward(X, y, wanted[name], args.min_train, args.step, args.embargo)
        if len(idx) == 0:
            continue
        col = pd.DataFrame({f"score_{name}": p}, index=data.index[idx])
        out = col if out is None else out.join(col, how="outer")

    if out is None:
        raise SystemExit("walk-forward が 1 つも実行できませんでした。")

    out["r_day"]    = r_day.reindex(out.index)
    out["y_strong"] = (out["r_day"] > thr).astype(int)
    return out.dropna()


# ══════════════════════════════════════════════════════════════
#  判定 1-2: 警報の precision と 警報日の N 損益
# ══════════════════════════════════════════════════════════════
def precision_report(df: pd.DataFrame, score_col: str, args) -> None:
    print(f"\n【判定1-2】上昇警報の precision と 警報日の N 損益  ({score_col})")

    d = df.copy()
    d["dec"] = pd.qcut(d[score_col].rank(method="first"), 10, labels=False)
    base = float(d["y_strong"].mean())

    print("─" * 92)
    print(f"{'デシル':<8}{'日数':>7}{'平均スコア':>11}{'日経日中平均':>13}"
          f"{'precision':>11}{'lift':>8}{'N平均損益':>12}{'N累計損益':>13}")
    print("─" * 92)
    for dec, g in d.groupby("dec"):
        prec = float(g["y_strong"].mean())
        print(f"{'D'+str(int(dec)+1):<8}{len(g):>7}{g[score_col].mean():>11.3f}"
              f"{g['r_day'].mean()*100:>12.3f}%{prec*100:>10.1f}%{prec/base:>8.2f}"
              f"{g['pnl'].mean():>12.4f}{g['pnl'].sum():>13.2f}")
    print("─" * 92)
    print(f"  全期間の基準率 (無条件の強上昇率): {base*100:.1f}%")

    hi = d[d["dec"] == 9]
    lo = d[d["dec"] == 0]
    spread = hi["r_day"].mean() - lo["r_day"].mean()
    se = math.sqrt(hi["r_day"].var() / len(hi) + lo["r_day"].var() / len(lo))
    print(f"  D10 − D1 の日経日中リターン差: {spread*100:+.3f}%  (t = {spread/se if se>0 else 0:+.2f})")

    d10_prec = float(hi["y_strong"].mean())
    d10_pnl  = float(hi["pnl"].sum())
    ok1 = d10_prec * 100 >= args.precision_target

    # 判定2 は「たまたま負けの日が集まった」でも成立してしまうので、
    # 同数の日をランダムに選んだ場合の分布 (非復元抽出) と比較する。
    x = df["pnl"].to_numpy(dtype=float)
    n_all, k = len(x), len(hi)
    mu, var = x.mean(), x.var(ddof=1)
    exp_sum = k * mu
    sd_sum  = math.sqrt(k * (n_all - k) / (n_all - 1) * var) if n_all > 1 else float("nan")
    z = (d10_pnl - exp_sum) / sd_sum if sd_sum > 0 else 0.0
    p_one = 0.5 * math.erfc(-z / math.sqrt(2))            # 片側: 期待より悪い確率
    ok2 = d10_pnl < 0 and p_one < 0.05

    print(f"\n  判定1  D10 precision = {d10_prec*100:.1f}%  (基準率 {base*100:.1f}%, "
          f"lift {d10_prec/base:.2f}, 目標 {args.precision_target:.1f}%) → "
          f"{'合格' if ok1 else '不合格'}")
    print(f"  判定2  D10 の N 累計損益 = {d10_pnl:+.2f}  "
          f"(同数ランダム抽出の期待値 {exp_sum:+.2f} ± {sd_sum:.2f}, 片側 p={p_one:.3f}) → "
          f"{'合格' if ok2 else '不合格'}")
    if d10_pnl < 0 and not ok2:
        print(f"         ※ 損益はマイナスだが、同数をランダムに選んでも "
              f"この程度は起きる水準 (p={p_one:.3f})。")

    # 警報率別の precision (veto 率を決めるための表)
    print(f"\n  警報率別 precision (上位 q% を警報とした場合):")
    print("  " + "─" * 76)
    print(f"  {'警報率':<8}{'警報日数':>9}{'precision':>12}{'lift':>8}"
          f"{'警報日 N平均':>14}{'警報日 N累計':>14}")
    print("  " + "─" * 76)
    s = d[score_col].to_numpy()
    n = len(s)
    for q in ALARM_RATES:
        k = max(int(round(n * q)), 1)
        cut = np.partition(s, n - k)[n - k]
        m = s >= cut
        prec = float(d.loc[m, "y_strong"].mean())
        print(f"  {q*100:>4.0f}%{'':<4}{int(m.sum()):>9}{prec*100:>11.1f}%{prec/base:>8.2f}"
              f"{d.loc[m,'pnl'].mean():>14.4f}{d.loc[m,'pnl'].sum():>14.2f}")
    print("  " + "─" * 76)


# ══════════════════════════════════════════════════════════════
#  ポジション構築 (stop / shrink / hedge)
# ══════════════════════════════════════════════════════════════
def rolling_beta(pnl: pd.Series, r: pd.Series, window: int = 500, minp: int = 250) -> pd.Series:
    """t-1 までの情報だけで推定した N 損益の日経日中リターンに対するベータ。"""
    cov = pnl.rolling(window, min_periods=minp).cov(r)
    var = r.rolling(window, min_periods=minp).var()
    return (cov / var).shift(1)


def apply_action(pnl: np.ndarray, mask: np.ndarray, args,
                 r_day: np.ndarray | None = None,
                 beta: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    警報日 (mask=True) に対して mode に応じた処置を適用する。
    戻り値: (処置後の日次損益, 建玉フラグ[待機コストの対象日])
    """
    if args.mode == "stop":
        out = np.where(mask, 0.0, pnl)
        traded = ~mask
    elif args.mode == "shrink":
        out = np.where(mask, pnl * args.veto_size, pnl)
        traded = np.ones(len(pnl), dtype=bool) if args.veto_size > 0 else ~mask
    else:                                              # hedge
        b = np.nan_to_num(beta, nan=0.0)
        notional = args.hedge_ratio * b                # 日経のロング建玉 (損益単位)
        cost = np.abs(notional) * 2 * args.hedge_cost_bps / 10_000.0
        out = np.where(mask, pnl - notional * r_day - cost, pnl)
        traded = np.ones(len(pnl), dtype=bool)
    return out, traded


# ══════════════════════════════════════════════════════════════
#  判定 3-4: ランダム休場 / 一律縮小 との比較
# ══════════════════════════════════════════════════════════════
def veto_report(df: pd.DataFrame, score_col: str, args) -> list[dict]:
    minv, nmon = month_index(df.index)
    x = df["pnl"].to_numpy(dtype=float)
    s = df[score_col].to_numpy(dtype=float)
    r = df["r_day"].to_numpy(dtype=float)
    beta = rolling_beta(df["pnl"], df["r_day"]).to_numpy() if args.mode == "hedge" else None
    n = len(x)
    rng = np.random.default_rng(0)

    base = risk_stats(x, minv, nmon)
    mode_label = {"stop": "停止", "shrink": f"{args.veto_size:.0%} に縮小",
                  "hedge": f"日経で {args.hedge_ratio:.0%} ヘッジ"}[args.mode]

    print(f"\n【判定3-4】警報日の処置: {mode_label}   ({score_col})")
    if args.wait_cost:
        print(f"  待機コスト {args.wait_cost:.4f} / 建てた日 を差し引き済み"
              f" (現行N は待たないので 0)")
    print("─" * 99)
    _stats_header()
    _print_stats("警報なし (現行N)", base)

    rows = []
    for q in ALARM_RATES:
        k = max(int(round(n * q)), 1)
        cut = np.partition(s, n - k)[n - k]
        mask = s >= cut
        adj, traded = apply_action(x, mask, args, r, beta)
        adj = adj - traded * args.wait_cost
        st = risk_stats(adj, minv, nmon, active=int(traded.sum()))
        _print_stats(f"警報 上位{q*100:.0f}% ({int(mask.sum())}日)", st)

        # ── 対照群A: 一律縮小 (同じ平均エクスポージャー) ──────────
        expo = float(np.where(mask, args.veto_size if args.mode == "shrink" else
                              (1.0 if args.mode == "hedge" else 0.0), 1.0).mean())
        uni = risk_stats(x * expo - args.wait_cost * (expo > 0), minv, nmon)
        rows.append({"q": q, "k": int(mask.sum()), "mask": mask, "st": st,
                     "uni": uni, "expo": expo})

    print("─" * 99)
    print("\n  対照群: 一律縮小 (選択せず全日を同じ平均エクスポージャーに落とした場合)")
    print("─" * 99)
    _stats_header()
    for row in rows:
        _print_stats(f"一律 {row['expo']:.0%} (警報{row['q']*100:.0f}%相当)", row["uni"])
    print("─" * 99)
    print("  ※ 一律縮小は 月平均/σ と Sharpe を数学的に変えない (平均もσも同率で縮むため)。")
    print("    したがって選択的 veto がこの 2 指標で一律縮小を上回ったら、"
          "その改善は純粋に『日を選べている』ことに由来する。")

    # ── 対照群B: ランダム休場 ────────────────────────────────
    print(f"\n  対照群: ランダム休場 ({args.bootstrap} 回) / 一律縮小 との比較")
    print("─" * 99)
    print(f"{'警報率':<10}{'月平均/σ':>12}{'Sharpe':>10}{'CVaR95':>10}{'最大DD':>10}"
          f"{'│':>3}{'一律比 月平均/σ':>16}{'一律比 CVaR/平均':>18}{'判定':>8}")
    print("─" * 99)
    for row in rows:
        k, st, uni = row["k"], row["st"], row["uni"]
        boot = {key: [] for key in ("m_ratio", "sharpe", "cvar95", "maxdd")}
        for _ in range(args.bootstrap):
            m2 = np.zeros(n, dtype=bool)
            m2[rng.choice(n, size=k, replace=False)] = True
            a2, t2 = apply_action(x, m2, args, r, beta)
            b2 = risk_stats(a2 - t2 * args.wait_cost, minv, nmon)
            for key in boot:
                boot[key].append(b2[key])
        pv = {key: float((np.asarray(v) >= st[key]).mean()) for key, v in boot.items()}
        d_ratio = st["m_ratio"] - uni["m_ratio"]
        d_cvar  = st["cvar_per_mean"] - uni["cvar_per_mean"]
        verdict = "有意" if (min(pv.values()) < 0.05 and d_ratio > 0) else "―"
        print(f"{row['q']*100:>4.0f}%{'':<6}{pv['m_ratio']:>12.3f}{pv['sharpe']:>10.3f}"
              f"{pv['cvar95']:>10.3f}{pv['maxdd']:>10.3f}{'│':>3}"
              f"{d_ratio:>+16.3f}{d_cvar:>+18.3f}{verdict:>8}")
    print("─" * 99)
    print("  左4列 = ランダム休場の p 値 (小さいほど良い)。")
    print("  右2列 = 一律縮小との差。正なら『日を選べている』ことによる改善。")
    print("    CVaR と 最大DD は露出に比例して縮むので、生の値では一律縮小が自動的に有利になる。")
    print("    そのため CVaR は月平均で割ってスケール不変にしてから比較している。")
    return rows


# ══════════════════════════════════════════════════════════════
#  判定 5: 月ブロック帰無較正
# ══════════════════════════════════════════════════════════════
def month_block_null(df: pd.DataFrame, score_col: str, args, rows: list[dict]) -> None:
    """
    スコアを月ブロック単位で並べ替えて帰無分布を作る。
    月内の自己相関と各系列の周辺分布を保ったまま スコア↔損益 の対応だけを壊すため、
    「特徴量・モデル・期間・閾値を多数試した」ことによる多重性の一部を較正できる。
    """
    minv, nmon = month_index(df.index)
    x = df["pnl"].to_numpy(dtype=float)
    s = df[score_col].to_numpy(dtype=float)
    r = df["r_day"].to_numpy(dtype=float)
    beta = rolling_beta(df["pnl"], df["r_day"]).to_numpy() if args.mode == "hedge" else None
    n = len(x)
    blocks = [np.where(minv == m)[0] for m in range(nmon)]
    rng = np.random.default_rng(1)

    print(f"\n【判定5】月ブロック帰無較正 ({args.permutations} 回, {nmon} ブロック)")
    print("─" * 74)
    print(f"{'警報率':<10}{'月平均/σ':>12}{'Sharpe':>11}{'CVaR95':>11}{'最大DD':>11}{'判定':>10}")
    print("─" * 74)
    for row in rows:
        k, st = row["k"], row["st"]
        null = {key: [] for key in ("m_ratio", "sharpe", "cvar95", "maxdd")}
        for _ in range(args.permutations):
            order = rng.permutation(nmon)
            s_null = np.concatenate([s[blocks[m]] for m in order])
            cut = np.partition(s_null, n - k)[n - k]
            m2 = s_null >= cut
            a2, t2 = apply_action(x, m2, args, r, beta)
            b2 = risk_stats(a2 - t2 * args.wait_cost, minv, nmon)
            for key in null:
                null[key].append(b2[key])
        pv = {key: float((np.asarray(v) >= st[key]).mean()) for key, v in null.items()}
        verdict = "有意" if min(pv.values()) < 0.05 else "―"
        print(f"{row['q']*100:>4.0f}%{'':<6}{pv['m_ratio']:>12.3f}{pv['sharpe']:>11.3f}"
              f"{pv['cvar95']:>11.3f}{pv['maxdd']:>11.3f}{verdict:>10}")
    print("─" * 74)
    print("  ※ この p 値も『事前に決めた 1 つの設定』についてのもの。"
          "設定を多数試した場合は、\n"
          "    最終的にモデルと閾値を固定した未使用期間 (--holdout-start) での確認が必要。")


# ══════════════════════════════════════════════════════════════
#  判定 6: 待機コスト
# ══════════════════════════════════════════════════════════════
def wait_cost_report(df: pd.DataFrame, score_col: str, args, rows: list[dict]) -> None:
    """
    損益分岐待機コスト = 警報日を避けて得た損益改善 ÷ 建てた日数。
    これを超える待機コストなら、この veto は損。
    """
    x = df["pnl"].to_numpy(dtype=float)
    print(f"\n【判定6】待機コスト分析")
    print("  日経始値を待つなら、veto するか否かに関わらず毎日待つ。")
    print("  → 待機コストは『建てた日』全部にかかり、休んだ日にはかからない。")
    print("─" * 78)
    print(f"{'警報率':<10}{'警報日 N累計':>14}{'建てた日数':>12}"
          f"{'損益分岐待機コスト':>20}{'判定':>12}")
    print("─" * 78)
    for row in rows:
        mask = row["mask"]
        avoided = -float(x[mask].sum())          # 避けた損失 (正なら得)
        traded = int((~mask).sum()) if args.mode == "stop" else len(x)
        be = avoided / traded if traded else float("nan")
        verdict = "余地あり" if be > 0 else "成立しない"
        print(f"{row['q']*100:>4.0f}%{'':<6}{float(x[mask].sum()):>+14.2f}{traded:>12}"
              f"{be:>+20.4f}{verdict:>12}")
    print("─" * 78)
    print("  ※ 損益分岐待機コストは『1 日あたりこの額まで待機で損しても元が取れる』の意味。")
    print("    N の実測 (寄り後6秒での価格変化) と比べて、これを下回らないと採用できない。")
    if args.wait_cost:
        print(f"  ※ 上の表は待機コスト差引「前」。指定された {args.wait_cost:.4f} は判定3-4 に反映済み。")


# ══════════════════════════════════════════════════════════════
#  実行
# ══════════════════════════════════════════════════════════════
def analyse(df: pd.DataFrame, score_col: str, args, label: str = "") -> None:
    head = f"  期間: {df.index[0].date()} .. {df.index[-1].date()}  ({len(df)} 日)"
    print(f"\n{'='*99}\n {label or '全 OOS 期間'}\n{head}\n{'='*99}")
    precision_report(df, score_col, args)
    rows = veto_report(df, score_col, args)
    month_block_null(df, score_col, args, rows)
    wait_cost_report(df, score_col, args, rows)


def run(panel: pd.DataFrame, args, n_pnl: pd.Series | None) -> None:
    scores = build_scores(panel, args)

    if n_pnl is None:
        print("\n[!] N の実損益が未指定のため代理損益で検証します "
              f"(日経日中との R^2={args.r2:.3f}, 日次σ=1, 年率Sharpe≈{args.surrogate_sharpe:.1f})。")
        print("    銘柄選択・約定の効果は含まれません。配管確認用であって運用判断の根拠にはなりません。")
        pnl = surrogate_n_pnl(scores["r_day"], args.r2, args.surrogate_sharpe)
    else:
        pnl = n_pnl.reindex(scores.index)
        ok = int(pnl.notna().sum())
        print(f"\n[+] N 実損益: スコア期間と {ok} 日が重複")
        if ok < 120:
            print(f"    [warn] {ok} 日しかありません。結論は暫定扱いにしてください。")
        j = pd.concat([pnl, scores["r_day"]], axis=1).dropna()
        if len(j) > 30:
            c = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
            print(f"    実測: corr(N損益, 日経日中) = {c:+.3f}  (R^2 = {c*c:.3f})  "
                  f"[前提値 {args.r2:.3f}]")

    for col in [c for c in scores.columns if c.startswith("score_")]:
        df = scores[[col, "r_day", "y_strong"]].copy()
        df["pnl"] = pnl
        df = df.dropna()
        if len(df) < 200:
            print(f"\n[skip] {col}: 有効 {len(df)} 日では判定できません。")
            continue

        if args.holdout_start:
            cut = pd.Timestamp(args.holdout_start)
            dev, hold = df[df.index < cut], df[df.index >= cut]
            if len(dev) >= 200 and len(hold) >= 100:
                analyse(dev, col, args, f"{col} — 開発期間 (探索に使った側)")
                analyse(hold, col, args, f"{col} — 未使用期間 (holdout) ★最終判定はここ")
                continue
            print(f"\n[warn] holdout 分割後のサンプルが不足 "
                  f"(開発 {len(dev)} / 未使用 {len(hold)})。分割せず全期間で報告します。")
        analyse(df, col, args, col)


def run_selftest(args) -> int:
    print("=" * 78)
    print(" 自己テスト: veto 検証器の配管検査 (ネットワーク不要)")
    print("=" * 78)
    args.bootstrap = min(args.bootstrap, 300)
    args.permutations = min(args.permutations, 300)
    args.model = "logistic"
    args.holdout_start = None

    for label, signal in (("A. 予測不能な相場 (signal=0) → 有意が出ないのが正常", 0.0),
                          ("B. 既知シグナルあり (signal=0.5) → 有意が出るのが正常", 0.5)):
        print(f"\n{'#'*78}\n### {label}\n{'#'*78}")
        raw = make_synthetic_panel(n=3000, signal=signal, seed=42)
        panel = raw.copy()
        for col in ("gspc_close", "vix_close", "usdjpy_close", "tnx_close"):
            panel[col] = raw[col].shift(1)
        run(panel.dropna(), args, None)

    print("\n" + "=" * 78)
    print(" 自己テスト完了 — 判定列を確認してください:")
    print("   A: 判定1/2 が不合格、判定3-4 と 判定5 が『―』であれば正常")
    print("   B: 判定1/2 が合格、判定3-4 と 判定5 が『有意』であれば正常")
    print("=" * 78)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="日経 始値→終値 上昇予測による N 発注制御の検証",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npnl", default=None, help="N の日次損益 CSV (date,pnl)")
    ap.add_argument("--panel", default=str(PANEL_CSV))
    ap.add_argument("--asof", default="at_open", choices=["strict", "at_open", "preopen"],
                    help="情報境界。strict=当日ギャップ不使用 / "
                         "at_open=09:00:00 の日経始値を使う (default)")
    ap.add_argument("--model", default="logistic", choices=["logistic", "gbdt", "both"])
    ap.add_argument("--mode", default="stop", choices=["stop", "shrink", "hedge"],
                    help="警報日の処置。stop=建てない / shrink=縮小 / "
                         "hedge=建てたまま日経で条件付きヘッジ")
    ap.add_argument("--strong-pct", type=float, default=0.5, dest="strong_pct",
                    help="『強い上昇日』の閾値 (%%)。default: 0.5")
    ap.add_argument("--precision-target", type=float, default=62.6, dest="precision_target",
                    help="D10 に要求する precision (%%)。default: 62.6")
    ap.add_argument("--veto-size", type=float, default=0.5, dest="veto_size",
                    help="mode=shrink のときの建玉倍率 (default: 0.5)")
    ap.add_argument("--hedge-ratio", type=float, default=1.0, dest="hedge_ratio",
                    help="mode=hedge のヘッジ比率。1.0=ベータ全額 (default: 1.0)")
    ap.add_argument("--hedge-cost-bps", type=float, default=5.0, dest="hedge_cost_bps",
                    help="ヘッジの片道コスト (bps)。default: 5")
    ap.add_argument("--wait-cost", type=float, default=0.0, dest="wait_cost",
                    help="日経始値を待つことによる 1 日あたりコスト。建てた日に課される")
    ap.add_argument("--holdout-start", default=None, dest="holdout_start",
                    help="この日付以降を未使用期間として分離して報告 (例: 2024-01-01)")
    ap.add_argument("--min-train", type=int, default=1000, dest="min_train")
    ap.add_argument("--step", type=int, default=63)
    ap.add_argument("--embargo", type=int, default=1)
    ap.add_argument("--lam", type=float, default=5.0)
    ap.add_argument("--r2", type=float, default=DEFAULT_R2)
    ap.add_argument("--surrogate-sharpe", type=float, default=1.0, dest="surrogate_sharpe")
    ap.add_argument("--bootstrap", type=int, default=N_BOOTSTRAP)
    ap.add_argument("--permutations", type=int, default=N_PERM)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest(args)

    n_pnl = load_n_pnl(Path(args.npnl)) if args.npnl else None   # 先に検証して早く失敗させる
    panel = load_panel(Path(args.panel))
    run(panel, args, n_pnl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
