"""fetch_fx_data.py — USDJPY の過去データを取得する。

公開GitHubリポジトリ (ejtraderLabs/historical-data) から取得する。
外部のFXデータAPI(Yahoo/stooq/Alpha Vantage 等)は egress ポリシーで
遮断されるため、GitHub経由が唯一の入手経路。

  python algo/fetch_fx_data.py                  # USDJPY 15分足
  python algo/fetch_fx_data.py --pair EURUSD --tf h1

出力: algo/data/<PAIR><TF>.csv   (元データそのまま。時刻は EET/EEST)

★ 時刻はブローカー時間 (EET/EEST = Europe/Helsinki)。
  実証で確定済み — JST変換すると 9時(東京オープン)に出来高が +60% 跳ね、
  出来高上位が 21-24時JST(ロンドン・NY重複帯)になる。
"""
from __future__ import annotations
import argparse, subprocess, shutil, sys
from pathlib import Path

REPO = "https://github.com/ejtraderLabs/historical-data.git"
OUT = Path(__file__).resolve().parent / "data"


def fetch(pair: str, tf: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{pair}{tf}.csv"
    if dest.exists():
        print(f"既にあります: {dest}")
        return dest
    tmp = Path("/tmp/_fxdata_repo")
    if tmp.exists():
        shutil.rmtree(tmp)
    print(f"clone中 (blobは遅延取得): {REPO}")
    subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none",
                    "--no-checkout", REPO, str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "sparse-checkout", "init", "--cone"], check=True)
    subprocess.run(["git", "-C", str(tmp), "sparse-checkout", "set", pair], check=True)
    subprocess.run(["git", "-C", str(tmp), "checkout", "HEAD", "--", pair], check=True)
    src = tmp / pair / f"{pair}{tf}.csv"
    if not src.exists():
        avail = sorted(p.name for p in (tmp / pair).glob("*.csv")) if (tmp / pair).exists() else []
        sys.exit(f"{src.name} がありません。利用可能: {avail}")
    shutil.copy(src, dest)
    shutil.rmtree(tmp)
    print(f"保存: {dest}  ({dest.stat().st_size/1e6:.1f} MB)")
    return dest


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="USDJPY等の過去データをGitHubから取得")
    ap.add_argument("--pair", default="USDJPY")
    ap.add_argument("--tf", default="m15", choices=["m15", "m30", "h1", "h4", "d1"])
    a = ap.parse_args()
    p = fetch(a.pair, a.tf)
    print(f"\n次: python algo/fx_edge_test.py {p} --tz-src Europe/Helsinki")
