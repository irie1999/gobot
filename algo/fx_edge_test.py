"""fx_edge_test.py — USDJPY の4仮説を一括検証する。

TradingView からエクスポートした CSV/XLSX を渡すだけで、
algo/EDGE_IDEAS.md の仮説を数字にする。

  python algo/fx_edge_test.py <ファイル>
  python algo/fx_edge_test.py <ファイル> --tz-src UTC   # 元データがUTCのとき

検証する仮説:
  H2 三市場の引き継ぎ — 東京時間の値動きはロンドン時間に否定されるか
  H3 時間帯構造     — 時間帯ごとに「レンジ回帰」と「継続」のどちらが優勢か
  H4 仲値の反対側   — 仲値前の上昇が大きい日ほど、仲値後の反落も大きいか
  H5 ゴトー日の日付別 — 15日・月末は他のゴトー日より効果が弱いか(米国債利払い仮説)
"""
from __future__ import annotations
import argparse, sys
import numpy as np
import pandas as pd

JST = "Asia/Tokyo"


# ── 読み込み ────────────────────────────────────────────────
def load_bars(path: str, tz_src: str | None) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]

    tcol = next((c for c in ("time", "datetime", "date", "timestamp", "日時", "日付") if c in df.columns), None)
    if tcol is None:
        sys.exit(f"時刻の列が見つかりません。列: {list(df.columns)}")

    s = df[tcol]
    if pd.api.types.is_numeric_dtype(s):          # unix タイムスタンプ。単位は桁数で判定
        mag = float(pd.to_numeric(s, errors="coerce").abs().median())
        unit = ("s" if mag < 1e11 else "ms" if mag < 1e14 else "us" if mag < 1e17 else "ns")
        idx = pd.to_datetime(s, unit=unit, utc=True)
    else:
        idx = pd.to_datetime(s, utc=False, errors="coerce")
        idx = pd.DatetimeIndex(idx)
        idx = idx.tz_localize(tz_src or "UTC") if idx.tz is None else idx
        idx = idx.tz_convert("UTC")
    df.index = pd.DatetimeIndex(idx).tz_convert(JST)

    ren = {}
    for want in ("open", "high", "low", "close"):
        c = next((c for c in df.columns if c.startswith(want)), None)
        if c is None:
            sys.exit(f"'{want}' 列が見つかりません。列: {list(df.columns)}")
        ren[c] = want
    df = df.rename(columns=ren)[["open", "high", "low", "close"]].astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _sess(day: pd.DataFrame, h0: int, h1: int) -> pd.DataFrame:
    """その日の [h0, h1) 時台(JST)のバー。"""
    return day[(day.index.hour >= h0) & (day.index.hour < h1)]


def _ret(seg: pd.DataFrame) -> float:
    """区間の変化率(%)。バーが無ければ NaN。"""
    if len(seg) == 0:
        return np.nan
    return (seg["close"].iloc[-1] / seg["open"].iloc[0] - 1) * 100


def _tt(x: np.ndarray) -> tuple[float, float]:
    """平均と t 値。"""
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return (np.nan, np.nan)
    return (x.mean(), x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


# ── H2: 三市場の引き継ぎ ──────────────────────────────────
def h2_handoff(df: pd.DataFrame) -> None:
    print("\n" + "=" * 74)
    print("H2  三市場の引き継ぎ — 東京の値動きはロンドンに否定されるか")
    print("=" * 74)
    rows = []
    for d, day in df.groupby(df.index.normalize()):
        tk = _ret(_sess(day, 9, 15))     # 東京 9-15時
        ld = _ret(_sess(day, 16, 24))    # ロンドン 16-24時
        if not (np.isnan(tk) or np.isnan(ld)):
            rows.append((d, tk, ld))
    if len(rows) < 30:
        print(f"  データ不足 ({len(rows)}日)"); return
    r = pd.DataFrame(rows, columns=["d", "tokyo", "london"])
    corr = r.tokyo.corr(r.london)
    print(f"  対象 {len(r)}日")
    print(f"  相関(東京 vs ロンドン) = {corr:+.4f}   ← マイナスなら『否定される』")

    # 東京の方向と逆に、ロンドンで入ったときの成績
    pnl = -np.sign(r.tokyo) * r.london
    m, t = _tt(pnl.values)
    print(f"  東京と逆張りでロンドンを取る: 平均 {m:+.4f}% / t = {t:+.2f} / 勝率 {(pnl>0).mean()*100:.1f}%")

    print("\n  東京の変動幅の大きさ別 (四分位):")
    r["q"] = pd.qcut(r.tokyo.abs(), 4, labels=["小", "中小", "中大", "大"])
    for q, g in r.groupby("q", observed=True):
        p = (-np.sign(g.tokyo) * g.london).values
        m, t = _tt(p)
        print(f"    {q:<4} n={len(g):>4}  逆張り平均 {m:+.4f}%  t={t:+.2f}  勝率 {(p>0).mean()*100:5.1f}%")


# ── H3: 時間帯構造 ────────────────────────────────────────
def h3_hourly(df: pd.DataFrame) -> None:
    print("\n" + "=" * 74)
    print("H3  時間帯構造 — 各時間帯は『レンジ回帰』か『継続』か")
    print("=" * 74)
    x = df.copy()
    x["h"] = x.index.hour
    x["range_pct"] = (x.high - x.low) / x.open * 100
    x["ret"] = (x.close / x.open - 1) * 100
    x["next_ret"] = x["ret"].shift(-1)
    print(f"  {'時':>3} {'本数':>6} {'平均変動幅%':>11} {'自己相関':>9}  判定")
    for h, g in x.groupby("h"):
        if len(g) < 30:
            continue
        ac = g["ret"].corr(g["next_ret"])
        judge = "継続(ブレイク向き)" if ac > 0.03 else ("回帰(レンジ向き)" if ac < -0.03 else "中立")
        print(f"  {h:>3} {len(g):>6} {g.range_pct.mean():>11.4f} {ac:>+9.3f}  {judge}")
    print("\n  ※ 自己相関がプラス=動いた方向に続く / マイナス=戻る")


# ── H4: 仲値の反対側 ──────────────────────────────────────
def h4_nakane(df: pd.DataFrame, gotobi_only: bool) -> None:
    label = "ゴトー日のみ" if gotobi_only else "全営業日"
    print("\n" + "=" * 74)
    print(f"H4  仲値の反対側 — 仲値前の上昇と仲値後の反落は比例するか ({label})")
    print("=" * 74)
    rows = []
    for d, day in df.groupby(df.index.normalize()):
        if gotobi_only and not is_gotobi(d):
            continue
        pre = _ret(_sess(day, 6, 10))     # 仲値前 (6:00-10:00)
        post = _ret(_sess(day, 10, 13))   # 仲値後 (10:00-13:00)
        if not (np.isnan(pre) or np.isnan(post)):
            rows.append((d, pre, post))
    if len(rows) < 30:
        print(f"  データ不足 ({len(rows)}日)"); return
    r = pd.DataFrame(rows, columns=["d", "pre", "post"])
    print(f"  対象 {len(r)}日")
    print(f"  相関(仲値前 vs 仲値後) = {r.pre.corr(r.post):+.4f}   ← マイナスなら反落仮説を支持")
    m, t = _tt(r.post.values)
    print(f"  仲値後の平均 {m:+.4f}% / t = {t:+.2f}")
    print("\n  仲値前の上昇の大きさ別:")
    r["q"] = pd.qcut(r.pre, 4, labels=["下落大", "下落小", "上昇小", "上昇大"])
    for q, g in r.groupby("q", observed=True):
        m, t = _tt(g.post.values)
        print(f"    {q:<5} n={len(g):>4}  仲値前 {g.pre.mean():+.4f}%  →  仲値後 {m:+.4f}%  t={t:+.2f}")


# ── H5: ゴトー日の日付別 ──────────────────────────────────
def is_gotobi(ts: pd.Timestamp) -> bool:
    d = ts.day
    return d in (5, 10, 15, 20, 25) or ts.is_month_end


def h5_gotobi(df: pd.DataFrame) -> None:
    print("\n" + "=" * 74)
    print("H5  ゴトー日の日付別 — 15日・月末は他より弱いか (米国債利払い仮説)")
    print("=" * 74)
    rows = []
    for d, day in df.groupby(df.index.normalize()):
        seg = _sess(day, 6, 10)                       # 仲値に向けた区間
        r = _ret(seg)
        if np.isnan(r):
            continue
        if d.day in (5, 10, 15, 20, 25):
            tag = f"{d.day}日"
        elif d.is_month_end:
            tag = "月末"
        else:
            tag = "非ゴトー日"
        rows.append((d, tag, r, d.dayofweek))
    if len(rows) < 60:
        print(f"  データ不足 ({len(rows)}日)"); return
    r = pd.DataFrame(rows, columns=["d", "tag", "ret", "dow"])

    order = ["5日", "10日", "15日", "20日", "25日", "月末", "非ゴトー日"]
    print(f"  {'区分':<10} {'n':>5} {'平均%':>9} {'t値':>7} {'勝率%':>7}")
    for tag in order:
        g = r[r.tag == tag]
        if len(g) < 5:
            continue
        m, t = _tt(g.ret.values)
        star = "  ★利払い重複" if tag in ("15日", "月末") else ""
        print(f"  {tag:<10} {len(g):>5} {m:>+9.4f} {t:>+7.2f} {(g.ret>0).mean()*100:>7.1f}{star}")

    gb = r[r.tag != "非ゴトー日"]
    ng = r[r.tag == "非ゴトー日"]
    m1, t1 = _tt(gb.ret.values); m2, _ = _tt(ng.ret.values)
    print(f"\n  ゴトー日 全体 {m1:+.4f}% (t={t1:+.2f}, n={len(gb)}) vs 非ゴトー日 {m2:+.4f}% (n={len(ng)})")

    pay = r[r.tag.isin(["15日", "月末"])]
    oth = r[r.tag.isin(["5日", "10日", "20日", "25日"])]
    mp, tp = _tt(pay.ret.values); mo, to = _tt(oth.ret.values)
    print(f"  ★ 利払い重複(15日/月末) {mp:+.4f}% (t={tp:+.2f}, n={len(pay)})")
    print(f"  ★ それ以外(5/10/20/25) {mo:+.4f}% (t={to:+.2f}, n={len(oth)})")
    print(f"     差 = {mo - mp:+.4f}%   ← プラスなら『利払いが打ち消している』仮説を支持")

    print("\n  曜日別 (ゴトー日のみ):")
    for dow, g in gb.groupby("dow"):
        if len(g) < 5:
            continue
        m, t = _tt(g.ret.values)
        nm = "月火水木金土日"[dow]
        print(f"    {nm}曜  n={len(g):>4}  {m:+.4f}%  t={t:+.2f}  勝率 {(g.ret>0).mean()*100:5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="USDJPY 4仮説の一括検証")
    ap.add_argument("path", help="TradingView からエクスポートした CSV/XLSX")
    ap.add_argument("--tz-src", default=None, help="元データのタイムゾーン (既定: UTC)")
    a = ap.parse_args()

    df = load_bars(a.path, a.tz_src)
    span = (df.index.max() - df.index.min()).days
    print(f"読み込み: {len(df):,}本  {df.index.min()}  〜  {df.index.max()}  ({span}日)")
    step = df.index.to_series().diff().median()
    print(f"バーの間隔(中央値): {step}")

    h2_handoff(df)
    h3_hourly(df)
    h5_gotobi(df)
    h4_nakane(df, gotobi_only=False)
    h4_nakane(df, gotobi_only=True)
    print("\n※ t値の目安: |t| > 2 で統計的に有意。1未満はノイズと区別できない。")


if __name__ == "__main__":
    main()
