"""run_oos_folds.py
daily.bat ベースの OOS 累積フォールド実行スクリプト。

daily.bat の実行内容（参考）:
  python merge_lss_proposals.py lss_proposal_2025-09.py ... --out lss_proposal_cumul.py
  python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0
      --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30
      --no-mirror --default-tab lss --force --no-news --no-risk --workers 8

このスクリプトは merge の提案ファイルを 1 つずつ増やして実行する。
  Fold1: lss_proposal_2025-09.py のみ → OOS = 2025-10
  Fold2: 2025-09 + 2025-10          → OOS = 2025-11
  ...
--long-base は訓練終了月の月末日を自動計算。

使い方:
  python run_oos_folds.py --workers 8 --lss-only      # ★推奨(最速。lss面1枚だけ)
  python run_oos_folds.py --workers 8                 # ロング/ショート面も作る(遅い)
  python run_oos_folds.py --fold-from 2026-03         # 特定フォールドだけ再実行

★ 速度と正確性の注意 (2026-08-06):
  daily.bat は --both --price-ranges 6000,0 なので、1フォールドあたり最大6面
  (long/short/lss × 6000/無制限)を作る。しかし検証に使うのは **lss の 6000 面だけ**。
  さらに悪いことに、生CSV(LSS_OOS_RAW_CSV)は**追記**なので、lss が 6000 と無制限で
  2回走ると1つのファイルに価格帯の違うシグナルが混ざる(検証データとして不正)。
  そのため既定を --price-ranges 6000 にし、--lss-only を用意した。
  この2つを付ければ 1フォールド=1面になり、速度は約6倍・データも正しくなる。
"""
import argparse
import calendar
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


def month_end(yyyymm: str) -> str:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]}"


def next_month(yyyymm: str) -> str:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y+1}-01" if m == 12 else f"{y}-{m+1:02d}"


def extract_ym(path: Path) -> str:
    m = re.search(r"(\d{4}-\d{2})", path.name)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--insample", action="store_true",
                    help="★診断用★ OOS月の提案ファイルも訓練に含める(=その月を選定に使ってから"
                         "その月を評価する)。これは in-sample なので成績としては無意味だが、"
                         "**.\\daily の損益タブと同じ条件**になる。"
                         "daily.bat は全提案(2026-07まで)をマージするので、7月の数字も"
                         "『7月の選定を使って7月を評価』した in-sample 値。"
                         "OOS版と並べて差を見るために使う")
    ap.add_argument("--daily-bat", type=str, default="daily.bat",
                    help="この .bat の merge_lss_proposals 行から提案ファイル一覧を読み、"
                         "**本番と完全に同じ構成**でフォールドを組む。"
                         "空文字にすると lss_proposal_YYYY-MM.py を全部拾う(構成が本番とズレる)")
    ap.add_argument("--price-ranges", type=str, default="6000",
                    help="価格帯パネル。既定 6000 = 1,000〜6,000円の1枚だけ(実発注と同じ帯)。"
                         "★ daily.bat の \"6000,0\" にすると lss 面が2回走り、"
                         "生CSV(LSS_OOS_RAW_CSV)は**追記**なので価格無制限ぶんが混ざる。"
                         "検証データが壊れるので、意図が無い限り 6000 のままにすること")
    ap.add_argument("--lss-only", action="store_true",
                    help="ロング/ショート面を作らず lss 面だけ生成する(--no-long --no-short)。"
                         "予算タブ・検証CSVは lss 面で作られるので結果は同一で、"
                         "フォールドあたりの時間が大幅に短くなる。"
                         "既定OFF = daily.bat と完全に同じ構成で走らせる")
    ap.add_argument("--manifest", type=str, default="oos_folds_manifest.csv",
                    help="このrunが作ったフォールドの台帳(fold,train_months,oos_month,raw_csv)。"
                         "集計側がファイル名から推測せずに済む。空文字で無効")
    ap.add_argument("--budget-csv", type=str, default="oos_budget_folds.csv",
                    help="レポート自身の予算タブ(月別P&L)を追記するCSV。**.\\daily と同じ計算経路**"
                         "なので、生CSVを sim_oos_budget.py で再シミュした値とズレたときの正解はこちら。"
                         "空文字で無効")
    ap.add_argument("--stop-delay-bars", type=int, default=1,
                    help="損切り遅延(既定1=watch.bat と一致)。2 にすると delay2 の"
                         "ローリングOOSが測れる。⚠ 採用するならライブ(watch.bat "
                         "--stop-delay-bars)とレポート(daily.bat LSS_STOP_DELAY_BARS)を"
                         "必ず同時に変えること(18.9)。BTキャッシュは sd<N> で別管理"
                         "なので、切り替えた初回は作り直しで遅い")
    ap.add_argument("--no-bt-filter", action="store_true",
                    help="BTの床を3箇所すべて0にして回す(LSS_NO_BT_FILTER=1)。"
                         "生CSVにBT30未満の取引が入るので、sim_oos_budget で "
                         "--bt-mins 0 が測れるようになる。既定の生CSVはプール床30で"
                         "作られており、後からBT0を測ることはできない")
    ap.add_argument("--bt-tiers", type=str, default="30,40,50,60,70",
                    help="予算CSVに出すBT閾値(カンマ区切り)。1回の実行で複数層を比較できる")
    ap.add_argument("--days", type=int, default=730, help="レポートの集計窓(日)")
    ap.add_argument("--start-from", type=str, default="",
                    help="訓練に使う最初の基準月 (例: --start-from 2025-09)")
    ap.add_argument("--fold-from", type=str, default="",
                    help="このOOS月以降のフォールドだけ実行 (例: 2026-03)")
    ap.add_argument("--include-current", action="store_true",
                    help="**当月**もフォールドに含める(既定は除外)。月が終わっていないので"
                         "営業日数が揃わず、月次の平均・検定には使えない。"
                         "レポートで当月の日別を見たいときだけ使う")
    ap.add_argument("--fold-to", type=str, default="",
                    help="このOOS月以前のフォールドだけ実行 (例: 2026-06)")
    args = ap.parse_args()

    # lss_proposal_YYYY-MM.py を自動収集・ソート
    dated = sorted(
        [(p, extract_ym(p)) for p in Path(".").glob("lss_proposal_????-??.py") if extract_ym(p)],
        key=lambda x: x[1],
    )
    # ★ 本番(daily.bat)と同じ提案ファイル構成にする。
    #   glob だと lss_proposal_2024-12.py などの古い提案まで拾い、daily.bat が
    #   使っていない銘柄が候補に混ざる(実測: 2,074ペア vs 本番相当は約1,400ペア)。
    #   候補が変われば発注順も結果も変わるので、検証の前提が崩れる。
    if args.daily_bat:
        _bp = Path(args.daily_bat)
        if _bp.exists():
            _txt = _bp.read_text(encoding="utf-8", errors="replace")
            _want = re.findall(r"lss_proposal_(\d{4}-\d{2})\.py", _txt)
            if _want:
                _wset = set(_want)
                _before = len(dated)
                dated = [(p, ym) for p, ym in dated if ym in _wset]
                print(f"[提案] {_bp.name} の merge 行に合わせて {_before}→{len(dated)}件に絞込")
                print(f"       ({', '.join(ym for _, ym in dated)})")
                _missing = sorted(_wset - {ym for _, ym in dated})
                if _missing:
                    print(f"  [!] {_bp.name} にあるが見つからない提案: {', '.join(_missing)}")
            else:
                print(f"  [!] {_bp.name} に lss_proposal_YYYY-MM.py の記述が見つかりません")
        else:
            print(f"  [!] {_bp} が見つかりません。glob で拾った全提案を使います"
                  f"(本番と構成がズレる可能性)")

    if args.start_from:
        dated = [(p, ym) for p, ym in dated if ym >= args.start_from]
        print(f"--start-from {args.start_from} 以降の {len(dated)} 件を使用")

    if len(dated) < 2:
        print("[ERROR] lss_proposal_YYYY-MM.py が2件以上必要です。")
        sys.exit(1)

    today_ym = date.today().strftime("%Y-%m")

    # daily.bat と同一の環境変数
    env = os.environ.copy()
    # env = os.environ.copy() なので、シェルに残った研究用フラグがそのまま漏れる。
    # daily.bat は毎回これらを明示クリアしている。**1つでも欠けると条件が変わる**ので
    # daily.bat の 40-45 行と1対1で対応させること(2026-08-08 点検で4つ欠けていた)。
    env["LSS_CLOSESTOP_RESWEEP"] = ""
    env["LSS_GUARD_ONLY"] = ""
    env["LSS_ENTRY_DELAY_BARS"] = ""
    env["LSS_BUDGET_MIN_BT"] = ""
    env["LSS_MONTH_FROM"] = ""
    env["LSS_REALISTIC_ENTRY"] = ""
    # 本番の lss_trades.csv をフォールドごとに10回上書きしてしまうのを防ぐ。
    # 検証に不要なうえ、.\fills の突合相手が壊れる。
    env["LSS_TRADES_CSV"] = ""
    # ⛔ ここは daily.bat / watch.bat と1文字でも食い違わせないこと。
    #    2026-08-08 点検で2件ズレていた:
    #      LSS_STOP_DELAY_BARS  ここ=2 / 実機(daily.bat・watch.bat)=1
    #      LSS_ASOF_BT          未設定(=OFF) → 過去の取引を『今日のBT』で並べる = **先読み**
    #    後者は致命的で、18.11 の実測では6ヶ月の損益が +2,270,229 → +276,975(-88%)動いた。
    #    このスクリプトの過去の出力(oos_raw_fold*.csv)は全部その状態で作られている。
    # ⚠ 既定1 = ライブ(watch.bat --stop-delay-bars 1)と揃える。--stop-delay-bars で変更可。
    env["LSS_STOP_DELAY_BARS"] = str(int(args.stop_delay_bars))
    if int(args.stop_delay_bars) != 1:
        print(f"[env] ★ 損切り遅延を {args.stop_delay_bars} で測ります "
              f"(ライブは watch.bat = 1)。採用するなら両方揃えること")
    env["LSS_BT_TAB_MIN"] = "30"   # 2026-08-08: 40→30 (18.24)。daily.bat と揃える
    if args.no_bt_filter:
        # BTの床は3箇所にあり、うち2つはここで上書き/消去している。
        # まとめて0にするスイッチを子プロセスへ渡す(nikkei_analysis 側で解釈)。
        env["LSS_NO_BT_FILTER"] = "1"
        if args.bt_tiers == "30,40,50,60,70":     # 既定のままなら0を含める
            args.bt_tiers = "0,10,20,30,40"
        print("[env] ★ BTフィルタ全部OFF (--no-bt-filter): "
              "プール床 / 予算下限 / 明細タブ閾値 = 0")
    env.setdefault("LSS_ASOF_BT", "1") # 先読みなしのBT (18.11)。daily.bat と同じ
    # 発注順は lss_order_rank の既定(流動性順)を継承する。比較用に旧BT降順で回すなら
    # 呼び出し前に set LSS_ORDER_RANK=bt (18.21: BT降順はランダム6本すべてを下回る)。
    _rank = env.get("LSS_ORDER_RANK", "") or "(既定=流動性順)"
    print(f"[env] stop_delay={env['LSS_STOP_DELAY_BARS']} / BT_TAB_MIN={env['LSS_BT_TAB_MIN']}"
          f"{' (無効化)' if args.no_bt_filter else ''} / "
          f"as-of BT={env['LSS_ASOF_BT']} / 発注順={_rank}")
    # ★ レポート自身の予算タブ(= .\daily と同じ _run_budget_sim)の月別P&Lを出させる。
    #    生CSV(LSS_OOS_RAW_CSV)を sim_oos_budget.py で再シミュした値とは経路が違うので、
    #    両者がズレたら **こちらが正**(実際に発注リストを作っているコードだから)。
    if args.budget_csv:
        # レポートは追記するので、実行前に必ず消す。残っていると前回の行が混ざり、
        # 集計側(aggregate_oos_budget.py)が『各月の最初の出現』を採る仕様のため
        # 古いフォールドの値を拾ってしまう。
        _bp = Path(args.budget_csv)
        if _bp.exists():
            if args.fold_from or args.fold_to:
                print(f"[!] {_bp} が既にあります。--fold-from/--fold-to での部分実行なので"
                      f"残します(集計時は月の重複に注意)")
            else:
                _bp.unlink()
                print(f"[初期化] 既存の {_bp} を削除(レポートは追記するため)")
        env["LSS_OOS_BUDGET_CSV"] = args.budget_csv
    # ★ このrunが作ったフォールドの台帳。集計側はファイル名から推測せずこれを使う。
    #   同じフォルダに開始月の違う過去実行の oos_raw_fold*.csv が残っていると、
    #   ファイル名由来の fold→OOS月 対応が壊れる(実測: fold01_2025-01 と
    #   fold01_2025-10 が同居していた)。
    manifest = Path(args.manifest) if args.manifest else None
    if manifest is not None:
        if not (args.fold_from or args.fold_to):
            manifest.write_text("fold,train_months,oos_month,raw_csv\n", encoding="utf-8")
        elif not manifest.exists():
            # 部分実行でも、ファイルが無ければヘッダから作る。
            # ヘッダ無しで追記すると集計側の DictReader が1行目を見出しに誤読する。
            manifest.write_text("fold,train_months,oos_month,raw_csv\n", encoding="utf-8")
        env["LSS_OOS_BUDGET_DAYS"] = str(args.days)   # days が一致しないと出力されない
        env["LSS_OOS_BUDGET_BT_TIERS"] = args.bt_tiers
    env.pop("LSS_REALISTIC_ENTRY", None)

    if "," in args.price_ranges:
        print(f"[!] --price-ranges {args.price_ranges} は価格帯パネルを複数作ります。")
        print("    lss 面がその回数だけ走り、生CSV(oos_raw_fold*.csv)は追記なので")
        print("    価格帯の違うシグナルが1ファイルに混ざります(検証データとしては不正)。")
        print("    検証目的なら --price-ranges 6000 にしてください。")

    print(f"提案ファイル {len(dated)} 件検出:")
    for p, ym in dated:
        print(f"  {ym}: {p.name}")

    for i in range(len(dated) - 1):
        train_end_ym = dated[i][1]
        oos_ym = next_month(train_end_ym)

        if oos_ym > today_ym or (oos_ym == today_ym and not args.include_current):
            _why = ("今月以降" if oos_ym > today_ym
                    else "今月(未完了。--include-current で含める)")
            print(f"\n[fold {i+1}] OOS={oos_ym} は{_why}のためスキップ")
            continue
        if oos_ym == today_ym:
            print(f"\n[fold {i+1}] OOS={oos_ym} は**今月(未完了)**。"
                  f"営業日が揃っていないので月次の集計には使わないこと")
        if args.fold_from and oos_ym < args.fold_from:
            print(f"[fold {i+1}] OOS={oos_ym} < --fold-from={args.fold_from} スキップ")
            continue
        if args.fold_to and oos_ym > args.fold_to:
            print(f"[fold {i+1}] OOS={oos_ym} > --fold-to={args.fold_to} スキップ")
            continue

        long_base = month_end(train_end_ym)
        # 既定: 訓練は OOS月の1つ前まで(真のOOS)。
        # --insample: OOS月の提案も含める = daily.bat と同じ条件(in-sample)。
        _tr_end = i + 2 if args.insample else i + 1
        train_files = [str(p) for p, _ in dated[:_tr_end]]
        merged = f"lss_proposal_fold{i+1:02d}.py"
        out_raw = f"oos_raw_fold{i+1:02d}_{oos_ym}.csv"

        print(f"\n{'='*60}")
        _tr_lbl = (dated[min(i + 1, len(dated) - 1)][1] if args.insample else train_end_ym)
        print(f"[fold {i+1}] 訓練: {dated[0][1]}〜{_tr_lbl}  OOS: {oos_ym}  long-base: {long_base}"
              + ("  ★in-sample(診断用)" if args.insample else ""))
        if args.lss_only:
            print("           (--lss-only: ロング/ショート面はスキップ。lss面の結果は同一)")

        # Step 1: merge (daily.bat の1行目と同じ)
        # 1ファイルの場合は merge_lss_proposals.py が2件必要なので同じファイルを2回渡す
        merge_files = train_files if len(train_files) >= 2 else train_files * 2
        subprocess.run(
            [sys.executable, "merge_lss_proposals.py"] + merge_files + ["--out", merged],
            check=True,
        )

        # Step 2: run (daily.bat の2行目と同じ、--lss-proposal と --long-base のみ変更)
        fold_env = env.copy()
        fold_env["LSS_OOS_RAW_CSV"] = out_raw
        fold_env["LSS_OOS_MONTH"] = oos_ym
        fold_env["LSS_OOS_FOLD"] = str(i + 1)
        fold_env["LSS_OOS_TRAIN_MONTHS"] = f"{dated[0][1]}〜{train_end_ym}"

        subprocess.run([
            sys.executable, "run_signals_holdout_all.py",
            "--both",
            "--min-price", "1000",
            "--price-ranges", args.price_ranges,
            "--no-analysis",
            # 銘柄詳細タブは **シグナル銘柄1つにつき損益ビルドを丸ごとやり直す**
            # (88銘柄なら88回)。1フォールドの時間の大半がここ。検証には一切不要。
            "--no-symbol-detail",
            # 市場分析タブ(相場環境/トレンド/エントリー分析)と転換トレードは
            # 予算タブ・損益タブに一切影響しないが、非常に重い。
            "--no-market",
            "--no-tenkan",
            "--lss-proposal", merged,
            "--long-base", long_base,
            "--no-mirror",
            "--default-tab", "lss",
            "--force",
            "--no-news",
            "--no-risk",
            "--days", str(args.days),
            "--workers", str(args.workers),
            "--no-browser",
            "--no-serve",
            # ★ 出力HTMLに接尾辞を付けて .\daily のレポートを上書きしないようにする。
            #   既定名は signals_holdout_all_both_<日付>.html で daily と同じなので、
            #   検証を回すと当日のレポートが消えていた(実測: fold10 の結果が
            #   .\daily のものだと誤認された)。
            "--output-suffix", f"_oosfold{i + 1:02d}_{oos_ym}",
        ] + (["--no-long", "--no-short"] if args.lss_only else []), env=fold_env)

        Path(merged).unlink(missing_ok=True)

        if manifest is not None:
            with open(manifest, "a", encoding="utf-8") as _mf:
                _mf.write(f"{i + 1},{dated[0][1]}〜{train_end_ym},{oos_ym},{out_raw}\n")

    print(f"\n{'='*60}")
    print("完了。生成されたOOS CSV:")
    for f in sorted(Path(".").glob("oos_raw_fold*.csv")):
        print(f"  {f.name}")
    if args.budget_csv and Path(args.budget_csv).exists():
        print(f"\n  {args.budget_csv}  ← レポート自身の予算タブ(.\\daily と同じ経路)")
        print(f"  集計:  python aggregate_oos_budget.py --csv {args.budget_csv}")
    if manifest is not None and manifest.exists():
        print(f"  台帳:  {manifest}  (このrunのfold→OOS月。古いCSVが混ざっても集計は安全)")


if __name__ == "__main__":
    main()
