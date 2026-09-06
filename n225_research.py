"""
日経平均 予測リサーチ ハーネス (Phase 0: 評価基盤)
────────────────────────────────────────────────────────────────────────
docs/nikkei_forecast_research.md の Phase 0 を実装したスタンドアロン評価器。

【設計方針】
  1. リターンを gap (オーバーナイト) / day (寄り→引け) / c2c に分解して扱う。
     日経の c2c は大部分が gap = 米国市場の翌朝反映であり、
     gap の的中率が高くても夜間先物が織り込み済みで取引価値は無い。
     本命は day (寄り成行 → 引け成行で取れる)。
  2. 米国系列 (^GSPC / ^VIX / ^TNX / ^SOX) の D 日足終値は
     翌 D+1 の 05:00-06:00 JST。日経 D 日足終値 (15:00 JST) より「後」。
     日付で naive merge すると巨大なリークになるため、
     カレンダー日グリッド上で必ず 1 日シフトしてから結合する。
  3. ベースライン (常に上昇 / 前日と同符号) を必ず併記する。
     日経の上昇基準率は 52-55% あるため、的中率 54% は無情報の可能性がある。
  4. 評価は purge/embargo 付き expanding walk-forward。コスト後 PnL まで出す。

使い方:
  python n225_research.py --build --years 15        # パネル構築 (要 yfinance)
  python n225_research.py --eval --target day       # 寄り→引け 方向 (本命)
  python n225_research.py --eval --target gap       # オーバーナイト (比較用)
  python n225_research.py --eval --target c2c       # 終値→終値
  python n225_research.py --eval --task vol         # HAR-RV ボラ予測
  python n225_research.py --selftest                # ネット不要の自己テスト

出力: n / 的中率 / 基準率 / 差分 / p値 / Brier / LogLoss / AUC /
      コスト後 PnL / ターンオーバー / Sharpe
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

# ── 定数 ────────────────────────────────────────────────────────
DATA_DIR   = Path("n225_data")
PANEL_CSV  = DATA_DIR / "panel.csv"

# 日足を取得する系列。lag=True は「米国/24h市場なので必ず 1 日シフト」の意味。
SERIES = {
    "n225":   ("^N225", False),   # 東京 15:00 JST 終値。基準となる取引日
    "gspc":   ("^GSPC", True),    # 米国終値 = 翌日 05:00-06:00 JST
    "vix":    ("^VIX",  True),
    "sox":    ("^SOX",  True),    # 取得失敗しても続行
    "usdjpy": ("JPY=X", True),    # 24h。基準時刻が曖昧なので安全側で 1 日ラグ
    "tnx":    ("^TNX",  True),    # 米 10年金利
}

TRADING_DAYS = 252
DEFAULT_MIN_TRAIN = 1000   # 約 4 年
DEFAULT_STEP      = 63     # 四半期ごとに再学習
DEFAULT_EMBARGO   = 1      # 予測ホライズン 1 日ぶん purge
DEFAULT_COST_BPS  = 10.0   # 片道 0.10% (往復 0.20%) = backtest_limit_entry と整合


# ══════════════════════════════════════════════════════════════
#  1. データ取得 / パネル構築
# ══════════════════════════════════════════════════════════════
def build_panel(years: int = 15) -> pd.DataFrame:
    """yfinance から各系列を取得し、日経の取引日を基準に 1 枚のパネルにする。"""
    import yfinance as yf

    period = f"{max(years, 2)}y"
    frames: dict[str, pd.DataFrame] = {}

    for key, (ticker, _lag) in SERIES.items():
        try:
            raw = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        except Exception as exc:                                   # noqa: BLE001
            print(f"  [warn] {key} ({ticker}) 取得失敗: {exc}")
            continue
        if raw is None or raw.empty:
            print(f"  [warn] {key} ({ticker}) データ空")
            continue
        raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
        frames[key] = raw
        print(f"  {key:7s} {ticker:8s} {len(raw):5d} bars  {raw.index[0].date()} .. {raw.index[-1].date()}")

    if "n225" not in frames:
        raise SystemExit("^N225 が取得できませんでした。ネットワーク/yfinance を確認してください。")

    n = frames["n225"]
    panel = pd.DataFrame(index=n.index)
    panel["n225_open"]  = n["Open"].astype(float)
    panel["n225_high"]  = n["High"].astype(float)
    panel["n225_low"]   = n["Low"].astype(float)
    panel["n225_close"] = n["Close"].astype(float)

    # 外部系列: カレンダー日グリッドに展開 → ffill → shift(1) → 日経の取引日へ
    # shift(1) が「東京 t 日の寄り前に確実に使える最新値」を保証する (§6.1)
    cal = pd.date_range(n.index[0], n.index[-1], freq="D")
    for key, (_ticker, lag) in SERIES.items():
        if key in ("n225",) or key not in frames:
            continue
        s = frames[key]["Close"].astype(float)
        s_cal = s.reindex(cal).ffill()
        if lag:
            s_cal = s_cal.shift(1)
        panel[f"{key}_close"] = s_cal.reindex(n.index)

    panel = panel.dropna(subset=["n225_open", "n225_close"])
    DATA_DIR.mkdir(exist_ok=True)
    panel.to_csv(PANEL_CSV)
    print(f"\n  → {PANEL_CSV}  ({len(panel)} rows, {panel.index[0].date()} .. {panel.index[-1].date()})")
    return panel


def load_panel(path: Path = PANEL_CSV) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"{path} がありません。先に `python n225_research.py --build` を実行してください。")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.sort_index()


def attach_intraday(panel: pd.DataFrame, path: str | Path | None) -> pd.DataFrame:
    """n225_intraday.py --build の出力 (先物 寄り前特徴量) をパネルに結合する。"""
    if not path:
        return panel
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[error] {p} がありません。"
                         f"先に `python n225_intraday.py --build <dir>` を実行してください。")
    intra = pd.read_csv(p, index_col=0, parse_dates=True).sort_index()
    cols = [c for c in intra.columns if c.startswith("fut_")]
    if not cols:
        raise SystemExit(f"[error] {p} に fut_* 列がありません。")
    out = panel.join(intra[cols], how="left")
    have = int(out[cols[0]].notna().sum())
    print(f"  先物特徴量 {len(cols)} 列を結合: {have}/{len(out)} 日で利用可能 "
          f"({intra.index[0].date()} .. {intra.index[-1].date()})")
    return out


# ══════════════════════════════════════════════════════════════
#  2. ターゲットと特徴量
# ══════════════════════════════════════════════════════════════
def make_targets(panel: pd.DataFrame) -> pd.DataFrame:
    """gap / day / c2c の 3 ターゲットを作る。"""
    o, c = panel["n225_open"], panel["n225_close"]
    t = pd.DataFrame(index=panel.index)
    t["gap"] = o / c.shift(1) - 1.0
    t["day"] = c / o - 1.0
    t["c2c"] = c / c.shift(1) - 1.0
    return t


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100.0 - 100.0 / (1.0 + up / dn.replace(0, np.nan))


def _parkinson_rv(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """レンジベースの実現ボラ推定量 (日足のみでも RV の代用になる)。日次標準偏差。"""
    lr2 = (np.log(high / low) ** 2) / (4.0 * math.log(2.0))
    return np.sqrt(lr2.rolling(window).mean())


def make_features(panel: pd.DataFrame, target: str, asof: str = "at_open") -> pd.DataFrame:
    """
    「その予測時点で確実に使える情報」だけで特徴量を作る。

    asof で情報境界を切り替える (09:00 前が締切という運用要件に対応):

      "strict"  … 前営業日の東京大引け以降・当日 09:00:00 までに確定した情報のみ。
                  当日のギャップは一切使わない。最も保守的で、リーク疑義がゼロ。
      "pre0900" … strict + 08:45〜08:59 の先物特徴量 (`fut_*`)。
                  08:59:59 に確定するので 09:00:00 より前に予測を完了できる。
                  現物始値は使わない。事前登録 (docs/preregistration_n225_veto.md)
                  の主判定はこの境界を使う。待機コストがゼロになるのが利点。
                  パネルに fut_* 列が無ければ strict と同じになる。
      "at_open" … strict + 当日ギャップ (日経の現物始値)。09:00:00 の板寄せで確定する
                  値なので「寄り前」ではなく「寄りと同時」。これを使ってよいのは、
                  発注системが以下の順序を実測で満たす場合に限る:

                      日経始値の受信 → 予測計算の完了 → 最初の発注送信

                  順序が逆なら未来情報。また、始値を待つために発注を遅らせるなら
                  その待機コストを必ず差し引くこと (n225_veto.py --wait-cost)。
                  旧名 "preopen" はエイリアスとして残してある。

    ギャップを使えるのは target="day" のときだけ (gap / c2c 自身の予測には使えない)。
    """
    tg = make_targets(panel)
    c = panel["n225_close"]
    f = pd.DataFrame(index=panel.index)

    # ── 自己系列 (すべて t-1 以前) ─────────────────────────────
    lr = np.log(c).diff()
    for k in (1, 5, 10, 20):
        f[f"own_r{k}"] = lr.rolling(k).sum().shift(1)
    f["own_gap_prev"] = tg["gap"].shift(1)
    f["own_day_prev"] = tg["day"].shift(1)
    for ma in (25, 75, 200):
        f[f"own_ma{ma}_dist"] = (c / c.rolling(ma).mean() - 1.0).shift(1)
    f["own_rsi14"] = _rsi(c, 14).shift(1)

    rv_d = _parkinson_rv(panel["n225_high"], panel["n225_low"], 1)
    rv_w = _parkinson_rv(panel["n225_high"], panel["n225_low"], 5)
    rv_m = _parkinson_rv(panel["n225_high"], panel["n225_low"], 22)
    f["own_rv_d"] = rv_d.shift(1)
    f["own_rv_w"] = rv_w.shift(1)
    f["own_rv_m"] = rv_m.shift(1)
    f["own_rv_ratio"] = (rv_d / rv_m).shift(1)

    # ── 外部系列 (build_panel で既に 1 日シフト済み) ───────────
    if "gspc_close" in panel:
        g = np.log(panel["gspc_close"]).diff()
        f["us_r1"] = g
        f["us_r5"] = g.rolling(5).sum()
    if "sox_close" in panel:
        f["sox_r1"] = np.log(panel["sox_close"]).diff()
    if "vix_close" in panel:
        v = panel["vix_close"]
        f["vix_lvl"]  = v
        f["vix_chg1"] = v.pct_change()
        f["vix_chg5"] = v.pct_change(5)
        f["vix_z20"]  = (v - v.rolling(20).mean()) / v.rolling(20).std()
    if "usdjpy_close" in panel:
        u = np.log(panel["usdjpy_close"]).diff()
        f["fx_r1"] = u
        f["fx_r5"] = u.rolling(5).sum()
    if "tnx_close" in panel:
        f["tnx_chg1"] = panel["tnx_close"].diff()

    # ── カレンダー ─────────────────────────────────────────────
    dow = panel.index.dayofweek
    for d in range(1, 5):                       # 月曜を基準カテゴリにする
        f[f"dow_{d}"] = (dow == d).astype(float)
    f["month_sin"] = np.sin(2 * np.pi * panel.index.month / 12)
    f["month_cos"] = np.cos(2 * np.pi * panel.index.month / 12)

    # ── 先物 寄り前ウィンドウ (08:45-08:59)。09:00:00 より前に確定 ──
    if target == "day" and asof in ("pre0900", "at_open", "preopen"):
        for col in [c for c in panel.columns if c.startswith("fut_")]:
            f[col] = panel[col]

    # ── 当日ギャップ (現物始値。09:00:00 と同時に確定) ──
    if target == "day" and asof in ("at_open", "preopen"):
        f["open_gap"]   = tg["gap"]
        f["open_gap_z"] = tg["gap"] / rv_d.shift(1).replace(0, np.nan)
        if "gspc_close" in panel:
            # 「米国上昇に対してギャップが素直に開いたか / 開き過ぎたか」
            f["gap_vs_us"] = tg["gap"] - 0.5 * np.log(panel["gspc_close"]).diff()

    return f


# ══════════════════════════════════════════════════════════════
#  3. モデル (numpy のみ。GBDT は sklearn があれば使う)
# ══════════════════════════════════════════════════════════════
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class LogisticL2:
    """IRLS (Newton法) で解く L2 正則化ロジスティック回帰。標準化を内蔵。"""

    name = "logistic"

    def __init__(self, lam: float = 5.0, iters: int = 40):
        self.lam, self.iters = lam, iters

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogisticL2":
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd < 1e-12] = 1.0
        Z = np.hstack([np.ones((len(X), 1)), (X - self.mu) / self.sd])
        w = np.zeros(Z.shape[1])
        pen = np.eye(Z.shape[1]) * self.lam
        pen[0, 0] = 0.0                                    # 切片は罰則なし
        for _ in range(self.iters):
            p = _sigmoid(Z @ w)
            grad = Z.T @ (y - p) - pen @ w
            W = np.clip(p * (1 - p), 1e-6, None)
            H = (Z.T * W) @ Z + pen
            try:
                step = np.linalg.solve(H, grad)
            except np.linalg.LinAlgError:
                step = np.linalg.lstsq(H, grad, rcond=None)[0]
            w += step
            if np.max(np.abs(step)) < 1e-8:
                break
        self.w = w
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        Z = np.hstack([np.ones((len(X), 1)), (X - self.mu) / self.sd])
        return _sigmoid(Z @ self.w)


class Gbdt:
    """sklearn の HistGradientBoostingClassifier ラッパー (無ければ skip)。"""

    name = "gbdt"

    def __init__(self, **kw):
        from sklearn.ensemble import HistGradientBoostingClassifier

        self.m = HistGradientBoostingClassifier(
            max_depth=3, max_iter=200, learning_rate=0.05,
            l2_regularization=1.0, min_samples_leaf=50, random_state=0, **kw)

    def fit(self, X, y):
        self.m.fit(X, y)
        return self

    def predict_proba(self, X):
        return self.m.predict_proba(X)[:, 1]


class AlwaysUp:
    """常に上昇。訓練期間の基準率を確率として返す。"""

    name = "always_up"

    def fit(self, X, y):
        self.p = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
        return self

    def predict_proba(self, X):
        return np.full(len(X), max(self.p, 0.5 + 1e-9))


class PrevSign:
    """前日と同符号。訓練期間の条件付き頻度 P(up | 前日up/down) を確率にする。"""

    name = "prev_sign"

    def __init__(self, col: int):
        self.col = col

    def fit(self, X, y):
        up = X[:, self.col] > 0
        self.p_up = float(np.clip(y[up].mean() if up.any() else 0.5, 1e-6, 1 - 1e-6))
        self.p_dn = float(np.clip(y[~up].mean() if (~up).any() else 0.5, 1e-6, 1 - 1e-6))
        return self

    def predict_proba(self, X):
        return np.where(X[:, self.col] > 0, self.p_up, self.p_dn)


# ══════════════════════════════════════════════════════════════
#  4. Walk-forward
# ══════════════════════════════════════════════════════════════
def walk_forward(X: np.ndarray, y: np.ndarray, make_model,
                 min_train: int, step: int, embargo: int,
                 collect: list | None = None) -> tuple[np.ndarray, np.ndarray]:
    """
    expanding window walk-forward。訓練末尾から embargo 日を purge する。
    collect を渡すと各 fold の学習済みモデルを追記する (係数の確認用)。
    戻り値: (テスト行のインデックス, 予測確率)
    """
    idx_out, p_out = [], []
    n = len(X)
    split = min_train
    while split < n:
        end = min(split + step, n)
        tr_end = max(split - embargo, 1)
        Xtr, ytr = X[:tr_end], y[:tr_end]
        if len(np.unique(ytr)) < 2:
            split = end
            continue
        model = make_model().fit(Xtr, ytr)
        if collect is not None:
            collect.append(model)
        p = np.asarray(model.predict_proba(X[split:end]), dtype=float)
        idx_out.append(np.arange(split, end))
        p_out.append(p)
        split = end
    if not idx_out:
        return np.array([], dtype=int), np.array([])
    return np.concatenate(idx_out), np.concatenate(p_out)


# ══════════════════════════════════════════════════════════════
#  5. 評価指標
# ══════════════════════════════════════════════════════════════
def _auc(y: np.ndarray, p: np.ndarray) -> float:
    pos, neg = y == 1, y == 0
    if not pos.any() or not neg.any():
        return float("nan")
    r = pd.Series(p).rank().to_numpy()
    return (r[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * neg.sum())


def _binom_p(k: int, n: int, p0: float = 0.5) -> float:
    """二項検定の正規近似 (両側)。scipy 非依存。"""
    if n == 0:
        return float("nan")
    se = math.sqrt(p0 * (1 - p0) / n)
    z = abs(k / n - p0) / se if se > 0 else 0.0
    return math.erfc(z / math.sqrt(2))


def evaluate(name: str, y_bin: np.ndarray, y_ret: np.ndarray, p: np.ndarray,
             cost_bps: float, threshold: float) -> dict:
    n = len(p)
    pred = (p > 0.5).astype(int)
    acc  = float((pred == y_bin).mean())
    base = float(max(y_bin.mean(), 1 - y_bin.mean()))       # 多数派を当て続けた場合
    up_rate = float(y_bin.mean())
    pc = np.clip(p, 1e-9, 1 - 1e-9)

    # コスト後 PnL: 確信度が閾値を超えた日だけ建てる
    pos = np.where(p > 0.5 + threshold, 1.0, np.where(p < 0.5 - threshold, -1.0, 0.0))
    turn = np.abs(np.diff(np.concatenate([[0.0], pos])))
    net  = pos * y_ret - turn * (cost_bps / 10_000.0)
    sharpe = (net.mean() / net.std() * math.sqrt(TRADING_DAYS)) if net.std() > 0 else float("nan")

    return {
        "model":    name,
        "n":        n,
        "acc":      acc,
        "base":     base,
        "up_rate":  up_rate,
        "edge":     acc - base,
        "p_value":  _binom_p(int((pred == y_bin).sum()), n),
        "brier":    float(np.mean((p - y_bin) ** 2)),
        "logloss":  float(-np.mean(y_bin * np.log(pc) + (1 - y_bin) * np.log(1 - pc))),
        "auc":      _auc(y_bin, p),
        "pnl_net":  float(net.sum()),
        "pnl_gross": float((pos * y_ret).sum()),
        "trades":   float(turn.sum() / 2),
        "sharpe":   float(sharpe),
    }


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n{title}")
    print("─" * 118)
    print(f"{'model':<12}{'n':>6}{'的中率':>9}{'基準率':>9}{'差':>8}{'p値':>8}"
          f"{'Brier':>8}{'LogLoss':>9}{'AUC':>7}{'純PnL%':>9}{'建玉数':>8}{'Sharpe':>8}")
    print("─" * 118)
    for r in rows:
        print(f"{r['model']:<12}{r['n']:>6}{r['acc']*100:>8.2f}%{r['base']*100:>8.2f}%"
              f"{r['edge']*100:>+7.2f}%{r['p_value']:>8.3f}{r['brier']:>8.4f}{r['logloss']:>9.4f}"
              f"{r['auc']:>7.3f}{r['pnl_net']*100:>8.1f}%{r['trades']:>8.0f}{r['sharpe']:>8.2f}")
    print("─" * 118)


def report_coefficients(cols: list[str], models: list, top: int = 18) -> None:
    """
    各 fold のロジスティック係数 (標準化後) を集計して表示する。

    「エッジがどの特徴量から来ているか」を見るためのもの。
    標準化済みなので係数の絶対値がそのまま影響力の目安になる。
    符号が正 = その特徴量が大きいほど『日中上昇』の確率を上げる。
    fold 間で符号が反転している特徴量は不安定で、信用してはいけない。
    """
    W = np.array([m.w for m in models])          # (fold, 1+n_features)
    mean, sd = W[:, 1:].mean(axis=0), W[:, 1:].std(axis=0)
    order = np.argsort(-np.abs(mean))[:top]

    print(f"\n■ ロジスティック係数 (標準化後, {len(models)} fold の平均)")
    print("─" * 72)
    print(f"{'特徴量':<20}{'平均係数':>12}{'fold間σ':>12}{'符号一致率':>12}")
    print("─" * 72)
    for i in order:
        agree = float(np.mean(np.sign(W[:, 1 + i]) == np.sign(mean[i])))
        print(f"{cols[i]:<20}{mean[i]:>+12.4f}{sd[i]:>12.4f}{agree*100:>11.0f}%")
    print("─" * 72)
    print("  符号一致率が 100% に近い特徴量ほど安定。80% を切るものは実質ノイズ。")


# ══════════════════════════════════════════════════════════════
#  6. 方向予測タスク
# ══════════════════════════════════════════════════════════════
def run_direction(panel: pd.DataFrame, target: str, args) -> list[dict]:
    tg = make_targets(panel)
    feats = make_features(panel, target, asof=getattr(args, "asof", "at_open"))

    data = feats.copy()
    data["_y"] = tg[target]
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < args.min_train + args.step:
        raise SystemExit(f"有効行 {len(data)} 件。min_train={args.min_train} + step={args.step} に足りません。"
                         f" --years を増やすか --min-train を下げてください。")

    y_ret = data.pop("_y").to_numpy()
    cols  = list(data.columns)
    X     = data.to_numpy(dtype=float)
    y_bin = (y_ret > 0).astype(int)

    print(f"\n■ ターゲット: {target}   asof: {getattr(args, 'asof', 'at_open')}   有効行: {len(data)}  "
          f"({data.index[0].date()} .. {data.index[-1].date()})   特徴量: {len(cols)}")
    print(f"  walk-forward: min_train={args.min_train}, step={args.step}, embargo={args.embargo}")
    print(f"  コスト: 片道 {args.cost_bps:.1f}bps / 建玉閾値: p>0.5±{args.threshold:.3f}")

    prev_col = cols.index("own_r1")
    candidates = [
        ("always_up", lambda: AlwaysUp()),
        ("prev_sign", lambda: PrevSign(prev_col)),
        ("logistic",  lambda: LogisticL2(lam=args.lam)),
    ]
    try:
        import sklearn  # noqa: F401
        candidates.append(("gbdt", lambda: Gbdt()))
    except ImportError:
        print("  [info] sklearn 未導入のため GBDT はスキップします (pip install scikit-learn)")

    rows = []
    coef_models: list = []
    for name, factory in candidates:
        collect = coef_models if (name == "logistic" and args.coef) else None
        idx, p = walk_forward(X, y_bin, factory, args.min_train, args.step,
                              args.embargo, collect=collect)
        if len(idx) == 0:
            continue
        rows.append(evaluate(name, y_bin[idx], y_ret[idx], p, args.cost_bps, args.threshold))

    print_table(rows, f"【{target}】 walk-forward OOS 結果")
    print("  ※ 『基準率』は多数派を当て続けた場合の的中率。差 が有意に正でなければエッジ無し。")
    print(f"  ※ p値は正規近似の二項検定 (対 50%)。{len(candidates)} モデル試行なので "
          f"Bonferroni 閾値は {0.05/max(len(candidates),1):.4f}。")
    if target in ("gap", "c2c"):
        print("  ※ gap / c2c で高い的中率が出ても、夜間先物が織り込み済みのため取引価値は限定的。")
    if coef_models:
        report_coefficients(cols, coef_models)
    return rows


def bucket_report(panel: pd.DataFrame, n_gap: int = 5, n_vol: int = 3) -> None:
    """
    ギャップ × 直近ボラ のバケット別に、当日の日中リターンを集計する。

    ロジスティックの上位係数 (open_gap / open_gap_z / gap_vs_us) は互いに強く
    相関しているため、個別係数の符号だけでは「継続なのかフェードなのか」を
    読み取れない。ここでは経験的な 2 次元表で実際の形を見る。

    ⚠ これは全期間の記述統計 (in-sample) であって検証ではない。
      「モデルが何を使っているか」を解釈するためのもの。
      エッジの有無の判定は walk-forward の結果 (--eval) で行うこと。
    """
    tg = make_targets(panel)
    rv = _parkinson_rv(panel["n225_high"], panel["n225_low"], 22).shift(1)
    df = pd.DataFrame({"gap": tg["gap"], "day": tg["day"], "rv": rv}).dropna()
    df["gap_z"] = df["gap"] / df["rv"]
    df["gb"] = pd.qcut(df["gap"], n_gap, labels=False)
    df["vb"] = pd.qcut(df["rv"], n_vol, labels=False)

    edges = df.groupby("gb")["gap"].agg(["min", "max"])
    vol_edges = df.groupby("vb")["rv"].agg(["min", "max"])

    vol_labels = ["低ボラ", "中ボラ", "高ボラ"][:n_vol] if n_vol <= 3 else \
                 [f"ボラ{v+1}" for v in range(n_vol)]

    print("\n■ ギャップ × 直近ボラ 別の当日 日中リターン平均 "
          f"({len(df)} 日, {df.index[0].date()} .. {df.index[-1].date()})")
    print("─" * 88)
    head1 = "ギャップ帯".ljust(16)
    head2 = "".ljust(16)
    for v in range(n_vol):
        vlo = vol_edges.loc[v, "min"] * 100
        vhi = vol_edges.loc[v, "max"] * 100
        head1 += vol_labels[v].rjust(20)
        head2 += ("%.2f-%.2f%%" % (vlo, vhi)).rjust(20)
    head1 += "全体".rjust(14)
    print(head1)
    print(head2)
    print("─" * 88)

    for g in range(n_gap):
        lo = edges.loc[g, "min"] * 100
        hi = edges.loc[g, "max"] * 100
        line = ("%+.2f%% .. %+.2f%%" % (lo, hi)).ljust(16)
        for v in range(n_vol):
            cell = df[(df["gb"] == g) & (df["vb"] == v)]
            if len(cell):
                line += ("%+.3f%% (n=%d)" % (cell["day"].mean() * 100, len(cell))).rjust(20)
            else:
                line += "-".rjust(20)
        row = df[df["gb"] == g]
        line += ("%+.3f%%" % (row["day"].mean() * 100)).rjust(14)
        print(line)
    print("─" * 88)
    print("  読み方: ギャップアップ帯 (下の行) で日中リターンが")
    print("    正 → ギャップ継続 (N には逆風。警報を出すべき日)")
    print("    負 → ギャップ埋め  (N には順風。建ててよい日)")
    print("  低ボラ列と高ボラ列で符号が変わるなら、"
          "ギャップの『絶対値』ではなく『ボラ比』が効いている。")

    # ボラ比で切った 1 次元表 (N の判断に直結する形)
    df["zb"] = pd.qcut(df["gap_z"], 10, labels=False)
    print(f"\n■ ギャップ/直近ボラ (gap_z) 十分位 別の当日 日中リターン")
    print("─" * 74)
    print(f"{'十分位':<10}{'gap_z 範囲':>22}{'日数':>8}{'日中平均':>13}{'上昇率':>10}{'>+0.5%率':>11}")
    print("─" * 74)
    for z, g in df.groupby("zb"):
        rng = f"{g['gap_z'].min():+.2f} .. {g['gap_z'].max():+.2f}"
        print(f"{'Z'+str(int(z)+1):<10}{rng:>22}{len(g):>8}{g['day'].mean()*100:>12.3f}%"
              f"{(g['day']>0).mean()*100:>9.1f}%{(g['day']>0.005).mean()*100:>10.1f}%")
    print("─" * 74)


# ══════════════════════════════════════════════════════════════
#  7. ボラ予測タスク (HAR-RV)
# ══════════════════════════════════════════════════════════════
def run_vol(panel: pd.DataFrame, args) -> None:
    hi, lo = panel["n225_high"], panel["n225_low"]
    rv_d = _parkinson_rv(hi, lo, 1)
    rv_w = _parkinson_rv(hi, lo, 5)
    rv_m = _parkinson_rv(hi, lo, 22)

    df = pd.DataFrame({
        "y":  np.log(rv_d.replace(0, np.nan)),
        "x1": np.log(rv_d.replace(0, np.nan)).shift(1),
        "x2": np.log(rv_w.replace(0, np.nan)).shift(1),
        "x3": np.log(rv_m.replace(0, np.nan)).shift(1),
    }).replace([np.inf, -np.inf], np.nan).dropna()

    if len(df) < args.min_train + args.step:
        raise SystemExit(f"有効行 {len(df)} 件。ボラ評価に足りません。")

    y = df["y"].to_numpy()
    X = df[["x1", "x2", "x3"]].to_numpy()

    preds_har, preds_naive, truth = [], [], []
    split = args.min_train
    while split < len(df):
        end = min(split + args.step, len(df))
        tr = max(split - args.embargo, 1)
        A = np.hstack([np.ones((tr, 1)), X[:tr]])
        beta = np.linalg.lstsq(A.T @ A + 1e-6 * np.eye(4), A.T @ y[:tr], rcond=None)[0]
        B = np.hstack([np.ones((end - split, 1)), X[split:end]])
        preds_har.append(B @ beta)
        preds_naive.append(X[split:end, 0])       # naive: 昨日の RV をそのまま
        truth.append(y[split:end])
        split = end

    yhat  = np.concatenate(preds_har)
    naive = np.concatenate(preds_naive)
    yt    = np.concatenate(truth)

    def _r2(pred):
        ss_res = np.sum((yt - pred) ** 2)
        ss_tot = np.sum((yt - yt.mean()) ** 2)
        return 1 - ss_res / ss_tot

    def _qlike(pred):                              # 分散スケールの QLIKE (低いほど良い)
        v_true, v_pred = np.exp(2 * yt), np.exp(2 * pred)
        return float(np.mean(v_true / v_pred - np.log(v_true / v_pred) - 1))

    print(f"\n■ HAR-RV ボラ予測 (Parkinson レンジ推定量, log スケール)   OOS: {len(yt)} 日")
    print("─" * 62)
    print(f"{'model':<12}{'OOS R2':>12}{'RMSE':>12}{'QLIKE':>12}")
    print("─" * 62)
    for nm, pr in (("naive(RV_t-1)", naive), ("HAR-RV", yhat)):
        rmse = float(np.sqrt(np.mean((yt - pr) ** 2)))
        print(f"{nm:<12}{_r2(pr):>12.4f}{rmse:>12.4f}{_qlike(pr):>12.4f}")
    print("─" * 62)
    print("  ※ HAR が naive を上回れば、ボラは実際に予測可能 (方向より再現性が高い領域)。")
    print("  ※ 用途: ポジションサイジング / レジームフィルタ / 既存 ATR ベース設計の置換。")


# ══════════════════════════════════════════════════════════════
#  8. 自己テスト (ネットワーク不要)
# ══════════════════════════════════════════════════════════════
def make_synthetic_panel(n: int = 3000, signal: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """
    合成パネル。signal=0 なら予測不能 (エッジが出たら評価コードのバグ)。
    signal>0 なら「前日の米国リターン → 当日の day リターン」に既知の係数を仕込む。
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-04", periods=n)

    us_ret = rng.normal(0, 0.011, n)                       # 米国の日次リターン
    gspc   = 3000 * np.exp(np.cumsum(us_ret))

    us_prev = np.concatenate([[0.0], us_ret[:-1]])         # t-1 の米国リターン
    gap = 0.55 * us_prev + rng.normal(0, 0.004, n)         # ギャップは米国を強く反映
    day = signal * us_prev + rng.normal(0, 0.009, n)       # 日中に仕込む (= 検出対象)

    close = np.empty(n)
    open_ = np.empty(n)
    close[0] = 20000.0
    open_[0] = close[0]
    for i in range(1, n):
        open_[i] = close[i - 1] * (1 + gap[i])
        close[i] = open_[i] * (1 + day[i])

    rng_range = np.abs(rng.normal(0, 0.006, n))
    high = np.maximum(open_, close) * (1 + rng_range)
    low  = np.minimum(open_, close) * (1 - rng_range)

    return pd.DataFrame({
        "n225_open": open_, "n225_high": high, "n225_low": low, "n225_close": close,
        "gspc_close": gspc,
        "vix_close": 15 + 5 * np.abs(rng.normal(0, 1, n)),
        "usdjpy_close": 110 * np.exp(np.cumsum(rng.normal(0, 0.004, n))),
        "tnx_close": 1.5 + np.cumsum(rng.normal(0, 0.02, n)) * 0.1,
    }, index=idx)


def run_selftest(args) -> int:
    """
    2 本立ての配管テスト:
      A. signal=0   → どのモデルも基準率を有意に超えないはず (超えたらリーク or バグ)
      B. signal=0.5 → logistic が基準率を明確に超えるはず (検出力がある証拠)

    注意: 合成データは gspc を 1 営業日ずらして参照する。build_panel と違い
    既にラグ済みの列として渡すため、make_features 側の扱いと整合する。
    """
    print("=" * 70)
    print(" 自己テスト: 評価ハーネスの配管検査 (ネットワーク不要)")
    print("=" * 70)

    ok = True

    for label, signal, expect in (("A. シグナル無し", 0.0, "エッジ無し"),
                                  ("B. 既知シグナル注入", 0.5, "logistic が検出")):
        print(f"\n### {label} (signal={signal}, 期待: {expect})")
        raw = make_synthetic_panel(n=3000, signal=signal, seed=42)
        # build_panel と同じラグ規則を適用 (外部系列を 1 日シフト)
        panel = raw.copy()
        for col in ("gspc_close", "vix_close", "usdjpy_close", "tnx_close"):
            panel[col] = raw[col].shift(1)
        panel = panel.dropna()

        rows = run_direction(panel, "day", args)
        by = {r["model"]: r for r in rows}
        logi = by.get("logistic")
        if logi is None:
            print("  [FAIL] logistic の結果が得られませんでした")
            ok = False
            continue

        if signal == 0.0:
            if logi["edge"] > 0.03:
                print(f"  [FAIL] シグナル無しなのに logistic のエッジが {logi['edge']*100:+.2f}% "
                      f"— リーク or バグの疑い")
                ok = False
            else:
                print(f"  [PASS] エッジ {logi['edge']*100:+.2f}% (閾値 +3.00% 未満) — リーク検出されず")
        else:
            if logi["edge"] < 0.03:
                print(f"  [FAIL] 既知シグナルを検出できず (エッジ {logi['edge']*100:+.2f}%) "
                      f"— 特徴量またはモデルの不具合")
                ok = False
            else:
                print(f"  [PASS] エッジ {logi['edge']*100:+.2f}% — 仕込んだシグナルを検出")

    print("\n" + "=" * 70)
    print(" 自己テスト: " + ("PASS" if ok else "FAIL"))
    print("=" * 70)
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════
#  9. CLI
# ══════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="日経平均 予測リサーチ ハーネス (Phase 0: 評価基盤)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build",    action="store_true", help="yfinance からパネルを構築")
    ap.add_argument("--eval",     action="store_true", help="walk-forward 評価を実行")
    ap.add_argument("--selftest", action="store_true", help="合成データで配管を自己テスト")
    ap.add_argument("--years",     type=int,   default=15, help="取得年数 (default: 15)")
    ap.add_argument("--target",    default="day", choices=["day", "gap", "c2c"],
                    help="方向予測のターゲット (default: day)")
    ap.add_argument("--asof",      default="at_open",
                    choices=["strict", "pre0900", "at_open", "preopen"],
                    help="情報境界。strict=当日情報なし / "
                         "pre0900=08:45-08:59 の先物のみ (09:00:00 より前に確定) / "
                         "at_open=09:00:00 の日経始値も使う。"
                         "preopen は at_open の旧名エイリアス (default: at_open)")
    ap.add_argument("--intraday", default=None,
                    help="n225_intraday.py --build が出力した先物特徴量 CSV。"
                         "パネルに結合して asof=pre0900 で使う")
    ap.add_argument("--task",      default="direction", choices=["direction", "vol"],
                    help="direction=方向予測 / vol=HAR-RV ボラ予測")
    ap.add_argument("--min-train", type=int,   default=DEFAULT_MIN_TRAIN, dest="min_train")
    ap.add_argument("--step",      type=int,   default=DEFAULT_STEP)
    ap.add_argument("--embargo",   type=int,   default=DEFAULT_EMBARGO)
    ap.add_argument("--cost-bps",  type=float, default=DEFAULT_COST_BPS, dest="cost_bps",
                    help="片道コスト (bps)。10 = 0.10%%")
    ap.add_argument("--threshold", type=float, default=0.0,
                    help="建玉する確信度の閾値。p>0.5+threshold で買い")
    ap.add_argument("--lam",       type=float, default=5.0, help="ロジスティックの L2 罰則")
    ap.add_argument("--buckets",   action="store_true",
                    help="ギャップ × 直近ボラ のバケット別に日中リターンを集計し、"
                         "継続とフェードの境目を経験的に見る (記述統計)")
    ap.add_argument("--coef",      action="store_true",
                    help="ロジスティック係数 (標準化後) を fold 平均で表示し、"
                         "エッジがどの特徴量から来ているかを確認する")
    ap.add_argument("--panel",     default=str(PANEL_CSV), help="パネル CSV のパス")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest(args)

    if args.build:
        print("■ パネル構築中...")
        build_panel(args.years)
        if not args.eval:
            return 0

    if args.buckets:
        bucket_report(attach_intraday(load_panel(Path(args.panel)), args.intraday))
        if not args.eval:
            return 0

    if args.eval:
        panel = attach_intraday(load_panel(Path(args.panel)), args.intraday)
        if args.task == "vol":
            run_vol(panel, args)
        else:
            run_direction(panel, args.target, args)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
