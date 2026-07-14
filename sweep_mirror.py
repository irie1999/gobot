"""sweep_mirror.py — ミラー戦略の (選定方法 × 保有日数) グリッド検証。

regime_perf_oos.py を組合せぶん呼び出し、各設定の BT<40 帯 OOS 成績
(損益 / PF / 勝率 / 取引数) を1枚の表にまとめる。

「保有は何日が最適か」「選定は勝ち銘柄と負け銘柄どちらが正しいか」を
1コマンドで比較するためのツール。

使い方:
  python sweep_mirror.py
  python sweep_mirror.py --holds 1,2,3,5,10 --band 40      # BT<40 帯を見る
  python sweep_mirror.py --band 60                          # BT<60 帯で見る(=BT<60全体)
  python sweep_mirror.py --years 8 --interval-months 6
"""
import argparse
import os
import re
import subprocess
import sys

# 子プロセス(regime_perf_oos)に UTF-8 で出力させる (Windows cp932 での文字化け/クラッシュ回避)
_CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

ap = argparse.ArgumentParser()
ap.add_argument("--holds", default="1,2,3,5,10", help="保有日数をカンマ区切り")
ap.add_argument("--band", type=int, default=40, help="見るBT帯 (40=BT<40行, 60=BT60-69行 等)")
ap.add_argument("--years", type=int, default=8)
ap.add_argument("--interval-months", type=int, default=6)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--fee", type=float, default=0.0,
                help="片道手数料/コスト(例0.001=往復0.2%相当)。ミラーのコスト評価はコレを使う(正しく差し引かれる)")
ap.add_argument("--slip", type=float, default=0.0,
                help="スリッページ。※ミラーでは幻の利益として加算されるので、コスト評価には使わない(--feeを使う)")
args = ap.parse_args()

HOLDS = [int(x) for x in args.holds.split(",") if x.strip()]
SELS = [("勝ち銘柄(long winners)", []), ("ワースト(--losers)", ["--losers"])]

# 見たいBT帯の行ラベル
if args.band <= 40:
    BAND_LABEL = "BT<40"
elif args.band < 60:
    BAND_LABEL = "BT40-59"
elif args.band < 70:
    BAND_LABEL = "BT60-69"
else:
    BAND_LABEL = "BT≥70"

# 「BT<40  512  54%  1.30  +730,045 ...」形式をパース
_ROW_RE = re.compile(
    re.escape(BAND_LABEL) + r"\s+(\d+)\s+(\d+)%\s+(\S+)\s+([+\-][\d,]+)")


def run_one(sel_args, hold):
    cmd = [sys.executable, "regime_perf_oos.py",
           "--years", str(args.years),
           "--interval-months", str(args.interval_months),
           "--mirror", "--max-hold", str(hold),
           "--slip", str(args.slip), "--fee", str(args.fee),
           "--min-price", str(args.min_price),
           "--max-price", str(args.max_price)] + sel_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_CHILD_ENV)
    except Exception as e:
        return None, f"err({e})"
    out = r.stdout or ""
    m = _ROW_RE.search(out)
    if not m:
        return None, "パース失敗"
    n = int(m.group(1))
    wr = int(m.group(2))
    pf = m.group(3)
    pnl = int(m.group(4).replace(",", ""))
    return {"n": n, "wr": wr, "pf": pf, "pnl": pnl}, None


def main():
    _cost = ("摩擦ゼロ" if (args.fee == 0 and args.slip == 0)
             else f"手数料片道{args.fee*100:.2f}%% / スリッページ{args.slip*100:.2f}%%".replace("%%", "%"))
    print("=" * 70)
    print(f"ミラー グリッド検証  {BAND_LABEL} 帯 / {args.years}年 / "
          f"再選定{args.interval_months}ヶ月 / 価格{args.min_price:.0f}-{args.max_price:.0f} / {_cost}")
    print("=" * 70)
    print(f"※ 各セルは『{BAND_LABEL} 帯』のOOS損益（先読みなし）。上=損益 / 下=PF・勝率・取引数\n")

    results = {}
    for sel_name, sel_args in SELS:
        for h in HOLDS:
            print(f"  回転中: {sel_name} / 保有{h}日 ...", flush=True)
            res, err = run_one(sel_args, h)
            results[(sel_name, h)] = (res, err)

    # 表示
    col_w = 15
    header = "選定＼保有".ljust(22) + "".join(f"{h}日".rjust(col_w) for h in HOLDS)
    print("\n" + header)
    print("-" * len(header))
    best = None
    for sel_name, _ in SELS:
        line_pnl = sel_name.ljust(22)
        line_sub = "".ljust(22)
        for h in HOLDS:
            res, err = results[(sel_name, h)]
            if res is None:
                line_pnl += err.rjust(col_w)
                line_sub += "".rjust(col_w)
                continue
            line_pnl += f"{res['pnl']:+,}".rjust(col_w)
            line_sub += f"PF{res['pf']}/{res['wr']}%".rjust(col_w)
            if best is None or res["pnl"] > best[2]:
                best = (sel_name, h, res["pnl"], res["pf"], res["wr"], res["n"])
        print(line_pnl)
        print(line_sub)
        print()

    print("-" * len(header))
    if best:
        print(f"★ 最良: {best[0]} × 保有{best[1]}日 → "
              f"{best[2]:+,}円 (PF{best[3]} / 勝率{best[4]}% / {best[5]}取引)")
    print("\n読み方:")
    print(f"  ・横に見て損益/PFが最大の列 = 最適な保有日数")
    print(f"  ・縦に見て(勝ち銘柄 vs ワースト)で大きい方 = 正しい選定方法")
    print(f"  ・摩擦ゼロなので実運用はここからスプレッド分が引かれる(特に短期保有で影響大)")


if __name__ == "__main__":
    main()
