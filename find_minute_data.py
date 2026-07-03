"""
find_minute_data.py ― デイトレ5分足pkl(2年分含む)のローカル保存場所を自動特定する
================================================================================
data/minute_5m (直近) / data/quarantine_5m (~2年) の pkl ディレクトリを広く探索し、
見つかった場所・銘柄数・実データの期間を表示する。発見結果は
.minute_data_dirs.txt に記憶され、⑭寄り確認タブ等が以降その場所を自動使用する
(場所を毎回指定/質問しなくて済む)。

使い方:
  python find_minute_data.py            # 探索して一覧表示 + 記憶
  python find_minute_data.py --rescan   # キャッシュ無視で再探索
  python find_minute_data.py --sample 8551.T   # 特定銘柄の期間/バー数を検査
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from analyze_open_confirm_entry import (
    locate_5m_dirs, _normalize_min, _yf_to_jq, _PKL_CACHE_FILE,
)


def _coverage(pkl_path: Path):
    """pklの正規化後の期間・バー数・日中央値バー数を返す。"""
    try:
        df = _normalize_min(pd.read_pickle(pkl_path))
        if df is None or df.empty:
            return None
        per_day = df.groupby(df.index.date).size()
        return (len(df), df.index.min().date(), df.index.max().date(),
                int(per_day.median()), len(per_day))
    except Exception as e:
        return ("err", str(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rescan", action="store_true", help="キャッシュ無視で再探索")
    ap.add_argument("--sample", default=None, help="銘柄(例 8551.T)の期間/バー数を検査")
    args = ap.parse_args()

    print("=" * 70)
    print("デイトレ5分足pkl 保存場所の自動特定")
    print("=" * 70)
    dirs = locate_5m_dirs(force=args.rescan, verbose=False)

    if not dirs:
        print("✗ 5分足pkl(*.pkl)を含む minute_5m / quarantine_5m が見つかりません。")
        print("  デイトレのデータ取得(daily_fetch_minute_yf.py 等)を実行したフォルダに")
        print("  data/quarantine_5m /data/minute_5m があるはずです。")
        print("  そのフォルダ内で本スクリプトを実行するか、シンボリックリンクを検討してください。")
        return

    print(f"✓ {len(dirs)}個のディレクトリを特定 (記憶先: {_PKL_CACHE_FILE})\n")
    for d in dirs:
        pkls = sorted(d.glob("*.pkl"))
        kind = ("quarantine(長期~2年)" if "quarantine" in d.name
                else "minute_5m(直近)" if "minute" in d.name else "?")
        print(f"── {d}")
        print(f"   種別: {kind}  /  銘柄数(pkl): {len(pkls)}")
        # 先頭数銘柄の期間サンプル
        for p in pkls[:3]:
            cov = _coverage(p)
            if cov and cov[0] != "err":
                n, d0, d1, md, ndays = cov
                print(f"   例 {p.stem}: {d0}〜{d1}  総バー{n:,}  {ndays}日  日中央値{md}本")
            elif cov:
                print(f"   例 {p.stem}: 読込エラー ({cov[1]})")
        print()

    if args.sample:
        jq = _yf_to_jq(args.sample)
        print(f"── サンプル検査: {args.sample} (jq={jq})")
        hit = False
        for d in dirs:
            p = d / f"{jq}.pkl"
            if p.exists():
                hit = True
                cov = _coverage(p)
                if cov and cov[0] != "err":
                    n, d0, d1, md, ndays = cov
                    print(f"   {d.name}: {d0}〜{d1}  総バー{n:,}  {ndays}日  日中央値{md}本"
                          + ("  ← 正常(60本前後)" if 40 <= md <= 80 else "  ← 要確認(バー数異常)"))
                else:
                    print(f"   {d.name}: 読込エラー")
        if not hit:
            print(f"   {jq}.pkl はどのディレクトリにもありません")

    print("これで ⑭寄り確認タブは自動的にこの場所の5分足を使用します(再質問なし)。")


if __name__ == "__main__":
    main()
