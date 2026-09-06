"""
日経225mini 先物 1 分足 (JPX / J-Quants DataCube) の検査とデイリー特徴量化
────────────────────────────────────────────────────────────────────────
docs/preregistration_n225_veto.md §2 の特徴量を作る。

【最初にやること】
  1 ヶ月分だけ購入し、まず --inspect でファイル形式を確定させる。
  列名・時刻ラベル・限月コード・セッション区分が分からないうちに
  10 年分を買っても解析できない。

  python n225_intraday.py --inspect path/to/sample.csv

  特に 2015 年のいずれかの月を買って
  「08:45 の足が存在しないこと」を確認するのが重要 (事前登録 §1.1)。
  デリバティブの日中立会が 08:45 開始になったのは 2016-07-19 から。

【形式が分かったら】
  python n225_intraday.py --build data_dir/ --out n225_data/intraday.csv
  python n225_intraday.py --build data_dir/ --map mycols.json

  --map は列名の対応を書いた JSON。--inspect の出力から作る。
    {"datetime": "日時", "open": "始値", "high": "高値",
     "low": "安値", "close": "終値", "volume": "出来高", "contract": "限月"}
  日付と時刻が別列なら {"date": "...", "time": "..."} を使う。

【自己テスト】
  python n225_intraday.py --selftest      # 合成 1 分足で配管を検査
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

# 事前登録 §2 の寄り前ウィンドウ
PRE_OPEN_START = "08:45"
PRE_OPEN_END   = "08:59"
DAY_SESSION_END = "15:15"     # 期間により 15:10 / 15:15 / 15:45。実データから推定する
ENCODINGS = ("utf-8-sig", "cp932", "shift_jis", "utf-8", "latin-1")

COL_CANDIDATES = {
    "datetime": ["datetime", "date_time", "日時", "timestamp", "取引日時"],
    "date":     ["date", "日付", "取引日", "trade_date", "営業日"],
    "time":     ["time", "時刻", "時間", "bar_time"],
    "open":     ["open", "始値", "寄値", "o"],
    "high":     ["high", "高値", "h"],
    "low":      ["low", "安値", "l"],
    "close":    ["close", "終値", "引値", "c"],
    "volume":   ["volume", "出来高", "vol", "取引高", "数量"],
    "contract": ["contract", "限月", "限月コード", "contract_month", "銘柄", "code",
                 "銘柄コード", "商品", "series"],
    "session":  ["session", "セッション", "立会区分", "日中夜間", "session_type"],
}


# ══════════════════════════════════════════════════════════════
#  読み込み
# ══════════════════════════════════════════════════════════════
def read_any_csv(path: Path, nrows: int | None = None) -> tuple[pd.DataFrame, str]:
    """文字コードを総当たりで読む。JPX の CSV は cp932 のことが多い。"""
    last = None
    for enc in ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc, nrows=nrows)
            if df.shape[1] >= 2:
                return df, enc
        except Exception as exc:                                # noqa: BLE001
            last = exc
    raise SystemExit(f"[error] {path} を読めませんでした: {last}")


# 解決順。date / time を datetime より先に確定させる
# (「時刻」列が datetime 役に誤マッチして日付を失う事故を防ぐ)
ROLE_ORDER = ["date", "time", "datetime", "open", "high", "low", "close",
              "volume", "contract", "session"]


def guess_columns(df: pd.DataFrame) -> dict[str, str]:
    """列名から役割を推測する。完全一致を全役割で先に取り、その後に部分一致。"""
    lower = {c: str(c).strip().lower() for c in df.columns}
    found: dict[str, str] = {}
    for exact in (True, False):
        for role in ROLE_ORDER:
            if role in found:
                continue
            for c, lc in lower.items():
                if c in found.values():
                    continue
                hit = (lc in COL_CANDIDATES[role]) if exact else \
                      any(k in lc for k in COL_CANDIDATES[role])
                if hit:
                    found[role] = c
                    break
    return found


# ══════════════════════════════════════════════════════════════
#  検査 (--inspect)
# ══════════════════════════════════════════════════════════════
def inspect(path: Path) -> None:
    print("=" * 84)
    print(f" ファイル検査: {path}")
    print("=" * 84)

    df, enc = read_any_csv(path)
    print(f"\n■ 基本情報")
    print(f"  文字コード : {enc}")
    print(f"  行数       : {len(df):,}")
    print(f"  列数       : {df.shape[1]}")

    print(f"\n■ 列 ({df.shape[1]})")
    print("─" * 84)
    print(f"{'#':>3} {'列名':<24}{'型':<12}{'非欠損':>10}{'ユニーク':>10}  例")
    print("─" * 84)
    for i, c in enumerate(df.columns):
        col = df[c]
        sample = str(col.dropna().iloc[0]) if col.notna().any() else "-"
        print(f"{i:>3} {str(c)[:23]:<24}{str(col.dtype):<12}{col.notna().sum():>10}"
              f"{col.nunique():>10}  {sample[:22]}")
    print("─" * 84)

    guess = guess_columns(df)
    print(f"\n■ 列の役割の推測")
    for role in ("datetime", "date", "time", "open", "high", "low", "close",
                 "volume", "contract", "session"):
        print(f"  {role:<10}: {guess.get(role, '(見つからず)')}")
    print("\n  ※ 推測が外れていたら --map で JSON を渡してください。")

    print(f"\n■ 先頭 3 行")
    print(df.head(3).to_string())
    print(f"\n■ 末尾 3 行")
    print(df.tail(3).to_string())

    # ── 日時の解析 ────────────────────────────────────────────
    dt = _parse_datetime(df, guess)
    if dt is None:
        print("\n[!] 日時列を特定できませんでした。--map で datetime または date/time を指定してください。")
        return

    df = df.assign(_dt=dt).dropna(subset=["_dt"])
    print(f"\n■ 日時")
    print(f"  範囲       : {df['_dt'].min()}  ..  {df['_dt'].max()}")
    print(f"  営業日数   : {df['_dt'].dt.date.nunique()}")
    print(f"  タイムゾーン: {df['_dt'].dt.tz if df['_dt'].dt.tz else '(naive / 表記なし)'}")

    # ── 時刻の分布 (セッション境界の推定) ──────────────────────
    tod = df["_dt"].dt.strftime("%H:%M")
    counts = tod.value_counts().sort_index()
    print(f"\n■ 出現する時刻 (先頭 12 / 末尾 12)")
    print("  " + ", ".join(f"{t}({n})" for t, n in list(counts.items())[:12]))
    print("  ...")
    print("  " + ", ".join(f"{t}({n})" for t, n in list(counts.items())[-12:]))

    # ── 08:45 の存在確認 (事前登録 §1.1 の検証) ────────────────
    print(f"\n■ 寄り前ウィンドウ ({PRE_OPEN_START}-{PRE_OPEN_END}) の存在確認")
    print("─" * 84)
    n_days = df["_dt"].dt.date.nunique()
    for t in ("08:44", "08:45", "08:46", "08:55", "08:59", "09:00", "09:01"):
        n = int((tod == t).sum())
        mark = "✓" if n > 0 else "✗"
        pct = f"{n/n_days*100:5.1f}%" if n_days else "  -  "
        print(f"  {mark} {t} : {n:>6} 本  (営業日の {pct})")
    print("─" * 84)
    if (tod == "08:45").sum() == 0:
        print("  → 08:45 の足がありません。")
        print("    この期間は日中立会が 09:00 開始だった可能性が高い")
        print("    (08:45 開始になったのは 2016-07-19 から。事前登録 §1.1)。")
    else:
        print("  → 08:45 の足があります。寄り前ウィンドウの特徴量を作れます。")

    # ── 限月コード ────────────────────────────────────────────
    if "contract" in guess:
        c = guess["contract"]
        vals = df[c].astype(str).value_counts()
        print(f"\n■ 限月コード候補 列『{c}』 — {len(vals)} 種類")
        print("  " + ", ".join(f"{v}({n})" for v, n in list(vals.items())[:10]))
        per_day = df.groupby(df["_dt"].dt.date)[c].nunique()
        print(f"  1 日あたりの限月数: 最小 {per_day.min()} / 中央 {per_day.median():.0f} "
              f"/ 最大 {per_day.max()}")
        if per_day.max() > 1:
            print("  → 複数限月が混在。中心限月 (出来高最大) の選択が必要です。")
    else:
        print("\n[!] 限月コードの列が見つかりません。")
        print("    複数限月が 1 ファイルに混在していないか、--inspect の列一覧で確認してください。")

    # ── セッション区分 ────────────────────────────────────────
    if "session" in guess:
        print(f"\n■ セッション区分 列『{guess['session']}』")
        print("  " + ", ".join(f"{v}({n})" for v, n in
                               df[guess["session"]].astype(str).value_counts().items()))
    else:
        hours = sorted(df["_dt"].dt.hour.unique())
        print(f"\n■ セッション区分の列は無し。出現する『時』: {hours}")
        night = [h for h in hours if h >= 17 or h <= 6]
        if night:
            print(f"  → 夜間 (17時〜6時) の足あり: {night}")
            print("    日中/夜間は時刻から判定します。日付が繰り上がるかを末尾行で確認してください。")

    print(f"\n■ 次の手順")
    print("  1. 上の『列の役割の推測』が正しいか確認する")
    print("  2. 外れていれば JSON を作り、--map で渡す")
    print("  3. 08:45 の足があれば --build に進む")


def _parse_datetime(df: pd.DataFrame, mapping: dict) -> pd.Series | None:
    """datetime 列、または date + time 列から日時を作る。"""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        # date + time が揃っていればそちらを優先する。
        # 「時刻」だけの列を datetime として解釈すると日付が失われるため。
        if "date" in mapping and "time" in mapping:
            return pd.to_datetime(df[mapping["date"]].astype(str).str.strip() + " "
                                  + df[mapping["time"]].astype(str).str.strip(),
                                  errors="coerce")
        if "datetime" in mapping:
            return pd.to_datetime(df[mapping["datetime"]], errors="coerce")
        if "date" in mapping:
            return pd.to_datetime(df[mapping["date"]], errors="coerce")
    return None


# ══════════════════════════════════════════════════════════════
#  デイリー特徴量化 (--build)
# ══════════════════════════════════════════════════════════════
def load_minutes(paths: list[Path], mapping: dict) -> pd.DataFrame:
    frames = []
    for p in paths:
        df, _ = read_any_csv(p)
        m = {**guess_columns(df), **mapping}
        dt = _parse_datetime(df, m)
        if dt is None:
            raise SystemExit(f"[error] {p}: 日時列を特定できません。--map を指定してください。")
        out = pd.DataFrame({"dt": dt})
        for role in ("open", "high", "low", "close", "volume"):
            if role not in m:
                raise SystemExit(f"[error] {p}: '{role}' 列を特定できません。--map を指定してください。")
            out[role] = pd.to_numeric(df[m[role]], errors="coerce")
        out["contract"] = df[m["contract"]].astype(str) if "contract" in m else "_single"
        frames.append(out)
    all_df = pd.concat(frames, ignore_index=True).dropna(subset=["dt", "close"])
    return all_df.sort_values("dt").reset_index(drop=True)


def central_contract(day_df: pd.DataFrame) -> str:
    """その日の出来高最大の限月を中心限月とする (事前登録 §1.3)。"""
    return day_df.groupby("contract")["volume"].sum().idxmax()


def build_features(minutes: pd.DataFrame, pre_start: str, pre_end: str) -> pd.DataFrame:
    """
    事前登録 §2.1 の 1〜8 (先物由来の特徴量) を日次で作る。
    9〜12 は日足パネル側にあるので、ここでは作らない。
    """
    minutes = minutes.copy()
    minutes["date"] = minutes["dt"].dt.date
    minutes["hm"] = minutes["dt"].dt.strftime("%H:%M")
    minutes["hour"] = minutes["dt"].dt.hour
    # 夜間立会 = 17時〜翌6時。終了時刻は期間により変わるので固定値を使わない (§1.4)
    minutes["is_night"] = (minutes["hour"] >= 17) | (minutes["hour"] <= 6)

    rows = []
    for date, g in minutes.groupby("date"):
        day = g[~g["is_night"]]
        if day.empty:
            continue
        cc = central_contract(day)
        day = day[day["contract"] == cc].sort_values("dt")
        # 限月ロールの影響を避けるため、同一限月内でのみ計算する (§1.3)
        pre = day[(day["hm"] >= pre_start) & (day["hm"] <= pre_end)]
        if pre.empty:
            continue

        hi, lo = float(pre["high"].max()), float(pre["low"].min())
        close_859 = float(pre.iloc[-1]["close"])
        open_845  = float(pre.iloc[0]["open"])
        rng = hi - lo

        last5 = pre[pre["hm"] >= _minus_minutes(pre_end, 4)]
        night = g[(g["is_night"]) & (g["contract"] == cc)].sort_values("dt")

        rows.append({
            "date":            pd.Timestamp(date),
            "contract":        cc,
            "fut_close_0859":  close_859,
            "fut_day_close":   float(day.iloc[-1]["close"]),
            "fut_ret_15m":     close_859 / open_845 - 1.0 if open_845 else np.nan,
            "fut_ret_5m":      close_859 / float(last5.iloc[0]["open"]) - 1.0
                               if len(last5) and last5.iloc[0]["open"] else np.nan,
            "fut_range_15m":   rng / close_859 if close_859 else np.nan,
            "fut_close_pos":   (close_859 - lo) / rng if rng > 0 else 0.5,
            "fut_vol_15m":     float(pre["volume"].sum()),
            "fut_night_close": float(night.iloc[-1]["close"]) if len(night) else np.nan,
            "fut_night_open":  float(night.iloc[0]["open"]) if len(night) else np.nan,
            "n_pre_bars":      len(pre),
        })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("date").sort_index()

    # 前営業日のデイセッション終値に対するギャップ
    prev_close = df["fut_day_close"].shift(1)
    df["fut_ret_open"] = df["fut_close_0859"] / prev_close - 1.0
    df["fut_ret_night"] = df["fut_night_close"] / df["fut_night_open"] - 1.0

    # 限月ロール日は前日終値と限月が違うので gap を無効化する
    rolled = df["contract"] != df["contract"].shift(1)
    df.loc[rolled, "fut_ret_open"] = np.nan

    # 出来高の標準化 (過去 60 営業日の同時間帯平均)
    ma = df["fut_vol_15m"].rolling(60, min_periods=20).mean().shift(1)
    df["fut_vol_z"] = df["fut_vol_15m"] / ma

    # ボラ比 — 日経の実現ボラは日足パネル側にあるので、ここでは先物終値で代用
    rv = np.log(df["fut_day_close"]).diff().rolling(22).std().shift(1)
    df["fut_gap_z"] = df["fut_ret_open"] / rv.replace(0, np.nan)

    keep = ["contract", "n_pre_bars", "fut_ret_open", "fut_gap_z", "fut_ret_15m",
            "fut_ret_5m", "fut_range_15m", "fut_close_pos", "fut_vol_z", "fut_ret_night"]
    return df[keep]


def _minus_minutes(hm: str, n: int) -> str:
    h, m = map(int, hm.split(":"))
    total = h * 60 + m - n
    return f"{total//60:02d}:{total%60:02d}"


# ══════════════════════════════════════════════════════════════
#  自己テスト
# ══════════════════════════════════════════════════════════════
def make_synthetic_minutes(path: Path, days: int = 40, with_0845: bool = True) -> None:
    """合成 1 分足 CSV を書き出す (限月ロールとナイトセッション込み)。"""
    rng = np.random.default_rng(0)
    rows = []
    price = 28000.0
    dates = pd.bdate_range("2024-01-04", periods=days)
    for i, d in enumerate(dates):
        contract = f"2024{((i // 20) * 3 + 3):02d}"           # 20 日ごとに限月ロール
        # (期近。下で期先も書き出す)
        # ナイトセッション (前日 17:00 〜 当日 06:00)
        for hm in ["17:00", "20:00", "23:00", "02:00", "05:30", "05:59"]:
            price *= 1 + rng.normal(0, 0.0006)
            rows.append((d - pd.Timedelta(days=1), hm, contract, price))
        # デイセッション
        times = ([f"08:{m:02d}" for m in range(45, 60)] if with_0845 else []) + \
                [f"09:{m:02d}" for m in range(0, 60)] + ["15:00", "15:10", "15:15"]
        for hm in times:
            price *= 1 + rng.normal(0, 0.0005)
            rows.append((d, hm, contract, price))

    out = []
    for d, hm, contract, p in rows:
        o = p * (1 + rng.normal(0, 0.0002))
        c = p
        out.append({
            "日付": pd.Timestamp(d).strftime("%Y/%m/%d"),
            "時刻": hm,
            "限月": contract,
            "始値": round(o, 1),
            "高値": round(max(o, c) * 1.0003, 1),
            "安値": round(min(o, c) * 0.9997, 1),
            "終値": round(c, 1),
            "出来高": int(abs(rng.normal(500, 200)) + 1),
        })
        # 期先の板 (出来高は期近の 1/50)。中心限月の選択ロジックを検証するため
        far = f"{int(contract) + 3}"
        out.append({
            "日付": pd.Timestamp(d).strftime("%Y/%m/%d"),
            "時刻": hm,
            "限月": far,
            "始値": round(o * 1.001, 1),
            "高値": round(max(o, c) * 1.0013, 1),
            "安値": round(min(o, c) * 0.9987, 1),
            "終値": round(c * 1.001, 1),
            "出来高": int(abs(rng.normal(10, 4)) + 1),
        })
    pd.DataFrame(out).to_csv(path, index=False, encoding="cp932")


def run_selftest() -> int:
    print("=" * 84)
    print(" 自己テスト: 1 分足パイプラインの配管検査 (ネットワーク不要)")
    print("=" * 84)
    ok = True
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # A. 08:45 あり
        f1 = tmp / "with_0845.csv"
        make_synthetic_minutes(f1, days=90, with_0845=True)
        print("\n### A. 08:45 の足がある場合\n")
        inspect(f1)
        feats = build_features(load_minutes([f1], {}), PRE_OPEN_START, PRE_OPEN_END)
        print(f"\n■ 生成された特徴量: {len(feats)} 日 × {feats.shape[1]} 列")
        print(feats.tail(3).to_string())
        if len(feats) < 50:
            print("  [FAIL] 特徴量の行数が少なすぎます")
            ok = False
        elif feats["fut_ret_open"].isna().sum() > 10:
            print(f"  [FAIL] fut_ret_open の欠損が多すぎます ({feats['fut_ret_open'].isna().sum()})")
            ok = False
        else:
            n_roll = int((feats["contract"] != feats["contract"].shift(1)).sum())
            print(f"  [PASS] {len(feats)} 日ぶん生成。限月ロール {n_roll} 回を検出し、"
                  f"当日の fut_ret_open を無効化済み")

        # B. 08:45 なし (2016-07-19 より前を想定)
        f2 = tmp / "without_0845.csv"
        make_synthetic_minutes(f2, days=30, with_0845=False)
        print("\n\n### B. 08:45 の足が無い場合 (2016-07-19 より前を想定)\n")
        df2, _ = read_any_csv(f2)
        tod = pd.to_datetime(df2["日付"].astype(str) + " " + df2["時刻"].astype(str)).dt.strftime("%H:%M")
        if (tod == "08:45").sum() == 0:
            print("  [PASS] 08:45 の足が無いことを検出できました")
            print("         → --inspect がこの状態を『日中立会が 09:00 開始だった可能性』"
                  "として報告します")
        else:
            print("  [FAIL] 08:45 の足が無いはずなのに検出されました")
            ok = False
        feats2 = build_features(load_minutes([f2], {}), PRE_OPEN_START, PRE_OPEN_END)
        if len(feats2) == 0:
            print("  [PASS] 特徴量は 0 行 (寄り前ウィンドウが無いので当然)")
        else:
            print(f"  [FAIL] 08:45 が無いのに {len(feats2)} 行生成されました")
            ok = False

    print("\n" + "=" * 84)
    print(" 自己テスト: " + ("PASS" if ok else "FAIL"))
    print("=" * 84)
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="日経225mini 先物 1 分足の検査とデイリー特徴量化",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inspect", metavar="CSV", help="1 ファイルの形式を検査する")
    ap.add_argument("--build", metavar="DIR", help="ディレクトリ内の CSV から特徴量を作る")
    ap.add_argument("--out", default="n225_data/intraday.csv", help="出力先")
    ap.add_argument("--map", metavar="JSON", help="列名の対応を書いた JSON ファイル")
    ap.add_argument("--pre-start", default=PRE_OPEN_START, dest="pre_start")
    ap.add_argument("--pre-end", default=PRE_OPEN_END, dest="pre_end")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest()

    if args.inspect:
        inspect(Path(args.inspect))
        return 0

    if args.build:
        mapping = json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else {}
        paths = sorted(Path(args.build).glob("**/*.csv"))
        if not paths:
            raise SystemExit(f"[error] {args.build} に CSV がありません。")
        print(f"■ {len(paths)} ファイルを読み込み中...")
        minutes = load_minutes(paths, mapping)
        print(f"  {len(minutes):,} 本の足  ({minutes['dt'].min()} .. {minutes['dt'].max()})")
        feats = build_features(minutes, args.pre_start, args.pre_end)
        if feats.empty:
            raise SystemExit(f"[error] {args.pre_start}-{args.pre_end} の足が 1 本もありません。"
                             f" --inspect で時刻の分布を確認してください。")
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        feats.to_csv(out)
        print(f"\n  → {out}  ({len(feats)} 日, {feats.index[0].date()} .. {feats.index[-1].date()})")
        print(f"  限月ロール: {int((feats['contract'] != feats['contract'].shift(1)).sum())} 回")
        na = ", ".join(f"{c}={int(feats[c].isna().sum())}" for c in feats.columns
                       if feats[c].isna().any())
        print(f"  欠損: {na}" if na else "  欠損: なし")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
