"""merge_lss_proposals.py — lss proposal (SELECTED) を複数マージして1ファイルにする。

用途:
  「6月基準の高BT銘柄」と「12月基準の高BT銘柄」の両方に投資したい場合など、
  複数の基準月 proposal を合体(union)または共通部分(intersection)にして
  1つの lss proposal を作る。出力を run_signals_holdout_all --lss-proposal に渡せば、
  予算シミュ(BT降順)は自動で「両方の中から毎日いちばんBTが高い注文」を拾う。

  ※ BTスコアは基準月に依存しない(直近365日の直接BT)。基準月が変えるのは
    「どの銘柄が候補に入るか」だけ。だから合体=候補集合を広げる、という意味になる。

使い方:
  # union(既定): どちらかに入っていれば採用(重複は1つに集約)
  python merge_lss_proposals.py lss_proposal_2026-06.py lss_proposal_2025-12.py \
      --out lss_proposal_merged_06_12.py

  # intersection: 両方に入っている銘柄×戦略のみ(最も頑健・数は少ない)
  python merge_lss_proposals.py lss_proposal_2026-06.py lss_proposal_2025-12.py \
      --mode intersection --out lss_proposal_common_06_12.py

  # K-of-N 投票: N個中K個以上のepochが選んだ銘柄のみ(頑健さと本数のバランス)。
  # 例: 4基準月中3つ以上で選ばれた「ロバスト核」
  python merge_lss_proposals.py \
      lss_proposal_2024-12.py lss_proposal_2025-06.py lss_proposal_2025-12.py lss_proposal_2026-06.py \
      --min-votes 3 --out lss_proposal_vote3.py

  # (union = --min-votes 1 と同じ / intersection = --min-votes N と同じ)

  # --until: 基準月がその月以前の lss_proposal_YYYY-MM.py を**自動収集**してマージ。
  # 単一分割OOS用。ファイル名を書き並べなくてよいので .bat の引数だけで切替できる。
  python merge_lss_proposals.py --until 2026-01 --out lss_proposal_cumul_to202601.py
  #   → 2025-09..2026-01 を採用 / 2026-02 以降を除外 / 2026-02-01 以降が単一分割OOS

  その後:
  python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 \
      --lss-proposal lss_proposal_merged_06_12.py --long-base 2026-06-30 \
      --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 \
      --output-suffix _merged0612
"""
import argparse
import re
import runpy
from pathlib import Path


def _base_tag(path: str) -> str:
    """ファイル名から選定基準月ラベルを作る。lss_proposal_2025-12.py → '12',
    2026-03 → '3'。YYYY-MM が拾えなければファイル名(拡張子なし)を使う。"""
    m = re.search(r"(\d{4})-(\d{2})", Path(path).name)
    if m:
        return str(int(m.group(2)))  # 月の先頭0を落とす(12/3/6 表記)
    return Path(path).stem


def _full_base_month(path: str) -> str | None:
    """ファイル名から YYYY-MM を返す。lss_proposal_2026-07.py → '2026-07'。"""
    m = re.search(r"(\d{4}-\d{2})", Path(path).name)
    return m.group(1) if m else None


def _oos_start_date(yyyymm: str) -> str:
    """YYYY-MM の翌月1日を返す (OOS 開始日 YYYY-MM-DD)。2026-07 → '2026-08-01'。"""
    y, mo = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y + 1}-01-01" if mo == 12 else f"{y}-{mo + 1:02d}-01"


def _load_selected(path: str) -> list[tuple]:
    """proposal ファイルを実行して SELECTED=[(code,name,strat),...] を取り出す。"""
    ns = runpy.run_path(path)
    sel = ns.get("SELECTED")
    if not sel:
        raise SystemExit(f"[ERROR] {path} に SELECTED が見つかりません")
    # 正規化: (code, name, strat) の3要素タプルに揃える
    out = []
    for row in sel:
        if len(row) >= 3:
            out.append((str(row[0]), str(row[1]), str(row[2])))
    return out


def main():
    ap = argparse.ArgumentParser(description="lss proposal を union/intersection でマージ")
    ap.add_argument("files", nargs="*", help="マージする proposal ファイル(2つ以上)。"
                                            "省略時は --until が必須")
    ap.add_argument("--until", type=str, default=None, metavar="YYYY-MM",
                    help="基準月がこの月以前の lss_proposal_YYYY-MM.py を**自動で集めて**"
                         "マージする(files を書き並べなくてよい)。単一分割OOS用: "
                         "--until 2026-01 なら 2026-02 以降の月は全部 OOS になる。"
                         "files を明示した場合はそこからさらに絞り込む。")
    ap.add_argument("--mode", choices=["union", "intersection"], default="union",
                    help="union=どれか1つでも採用(既定) / intersection=全ファイル共通のみ。"
                         "--min-votes 指定時はそちらが優先。")
    ap.add_argument("--min-votes", type=int, default=0,
                    help="K-of-N投票: N個中この数以上のファイルに出た銘柄×戦略のみ採用。"
                         "1=union / N=intersection。0(既定)なら --mode に従う。")
    ap.add_argument("--oos-all", action="store_true",
                    help="最新基準月の翌月1日を全銘柄のOOS開始日として設定。"
                         "例: 9月+10月マージ時 --oos-all → 全銘柄のOOS開始=2024-11-01。"
                         "純粋OOS検証用。--oos-per-symbol より厳しい(最新月以降しか残らない)")
    ap.add_argument("--legacy-latest-only", action="store_true",
                    help="旧挙動: 最新基準月で初出の銘柄だけにOOS開始日を設定する。"
                         "**過去の成績評価に先読みが入る**ので比較目的以外では使わないこと")
    ap.add_argument("--out", type=str, default="lss_proposal_merged.py", help="出力ファイル名")
    args = ap.parse_args()

    # --until: 基準月がその月以前の proposal を自動収集する。
    # 単一分割OOS(『N月までで選んで、N+1月以降を検証する』)を .bat の引数だけで
    # 切り替えられるようにするためのもの。files を書き並べる必要がなくなる。
    if args.until:
        if not re.fullmatch(r"\d{4}-\d{2}", args.until):
            raise SystemExit(f"[ERROR] --until は YYYY-MM 形式で指定してください: {args.until}")
        _pool = args.files or sorted(
            str(p) for p in Path(".").glob("lss_proposal_[0-9][0-9][0-9][0-9]-[0-9][0-9].py"))
        _kept, _dropped = [], []
        for f in _pool:
            _ym = _full_base_month(f)
            if _ym and _ym <= args.until:
                _kept.append(f)
            else:
                _dropped.append(f)
        args.files = sorted(_kept, key=lambda f: _full_base_month(f) or "")
        print(f"[--until {args.until}] 採用 {len(_kept)}件: "
              f"{', '.join(_full_base_month(f) or f for f in args.files)}")
        if _dropped:
            print(f"[--until {args.until}] 除外 {len(_dropped)}件: "
                  f"{', '.join(sorted(_full_base_month(f) or f for f in _dropped))}")
        # per-symbol START_DATES は「初出基準月の翌月1日」なので、最も遅い採用月の
        # 翌月から先は **どのペアも選定に使っていない**期間になる。
        if args.files:
            print(f"[--until {args.until}] → "
                  f"{_oos_start_date(args.until)} 以降が単一分割OOS")
    elif not args.files:
        raise SystemExit("[ERROR] files も --until も指定されていません")

    # 存在するファイルだけ使う(欠損は警告してスキップ=累積の1基準月が未生成でも daily を止めない)。
    _existing = []
    for f in args.files:
        if Path(f).exists():
            _existing.append(f)
        else:
            print(f"[WARN] {f} が見つかりません → スキップ(累積から除外)")
    args.files = _existing

    if len(args.files) < 2:
        raise SystemExit("[ERROR] 有効な proposal が2つ未満です(基準月ファイルを生成してください)")

    # 各ファイルの (code, strat) 集合と、code→name / (code,strat)→行 を集める
    per_file_keys: list[set] = []
    name_map: dict = {}
    row_map: dict = {}   # (正規化code, strat) -> (出力code, name, strat)
    out_code: dict = {}  # (正規化code, strat) -> 出力コード(元表記, .T付き優先)
    src_count: dict = {}  # (code, strat) -> 何ファイルに出たか
    src_bases: dict = {}  # (code, strat) -> [基準月ラベル,...] (出現ファイル順・重複なし)
    src_yyyymm: dict = {}  # (code, strat) -> earliest YYYY-MM (OOS開始日算出用)
    for f in args.files:
        sel = _load_selected(f)
        _tag = _base_tag(f)
        keys = set()
        for code, name, strat in sel:
            # キーは正規化(.T 有無を吸収)して「かぶり」を1銘柄に集約する。これをしないと
            # "7261"(12月) と "7261.T"(6月) が別銘柄扱いになり重複出力される。
            # ただし SELECTED の出力コードは元の表記を保持する。fetch(yfinance)は
            # ".T" 付きコードを要求するため、正規化した裸コードを出力すると取得失敗する。
            # かぶった場合は ".T" 付きの表記を優先採用。
            _cn = str(code).upper().removesuffix(".T").split(".")[0]
            k = (_cn, strat)
            keys.add(k)
            name_map.setdefault(_cn, name)
            _prev = out_code.get(k)
            if _prev is None or (".T" in str(code).upper()
                                 and ".T" not in str(_prev).upper()):
                out_code[k] = code
            row_map[k] = (out_code[k], name_map[_cn], strat)
        for k in keys:
            src_count[k] = src_count.get(k, 0) + 1
            _lst = src_bases.setdefault(k, [])
            if _tag not in _lst:
                _lst.append(_tag)
        _ym = _full_base_month(f)
        if _ym:
            for k in keys:
                if k not in src_yyyymm or _ym < src_yyyymm[k]:
                    src_yyyymm[k] = _ym
        per_file_keys.append(keys)
        print(f"[読込] {f}: {len(keys)}件 (code,strat) 基準月={_tag}")

    n_files = len(args.files)
    if args.min_votes and args.min_votes > 0:
        _k = max(1, min(args.min_votes, n_files))
        merged_keys = {key for key, c in src_count.items() if c >= _k}
        print(f"[条件] K-of-N投票: {n_files}個中 {_k}個以上に出た銘柄×戦略のみ採用")
    elif args.mode == "union":
        merged_keys = set().union(*per_file_keys)
    else:  # intersection: 全ファイル共通
        merged_keys = set(per_file_keys[0])
        for ks in per_file_keys[1:]:
            merged_keys &= ks

    # BTスコア順は run 側で付くので、ここでは code,strat でソートして安定出力にするだけ
    merged = sorted(merged_keys, key=lambda k: (k[0], k[1]))

    # ── START_DATES: 各ペアを「いつから集計してよいか」──────────────────────
    # ⛔ なぜ既定を変えたか (2026-08-07):
    #   旧既定は『最新基準月で初出の銘柄だけ』にOOS開始日を付けていた。つまり
    #   2026-06 の選定で初めて入った銘柄が、2026-02 の取引の評価に使われていた。
    #   2月時点ではその銘柄はWATCHLISTに存在しない。「6月時点で良かったから選ばれた」
    #   = 2〜6月の成績が良かった、という**未来情報**で過去を評価していたことになる。
    #   これは as-of BT (CLAUDE.md 18.11) と完全に同じ構造のリーク。
    #
    #   新既定 (per-symbol): 各ペアに『初出の基準月の翌月1日』を設定する。
    #   2025-12 の提案で初めて入ったペアは 2026-01-01 以降しか集計しない。
    #   これで「その時点で実際に持っていたWATCHLIST」だけで過去を評価できる。
    #
    #   --oos-all           … 全ペアに最新基準月の翌月1日(最も厳しい・純粋OOS)
    #   --legacy-latest-only … 旧挙動(先読みあり。比較用のみ)
    #
    #   ※ START_DATES は**過去の集計フィルタ**であって、今日のシグナル生成には
    #     影響しない(発注リストは1銘柄も変わらない)。
    _latest_yyyymm = max((_full_base_month(f) for f in args.files if _full_base_month(f)),
                         default=None)
    start_dates: dict = {}
    if _latest_yyyymm:
        _oos_d = _oos_start_date(_latest_yyyymm)
        if getattr(args, "oos_all", False):
            # 全銘柄に同一のOOS開始日を設定
            for k in merged:
                _cn = str(row_map[k][0]).upper().removesuffix(".T").split(".")[0]
                start_dates[(_cn, k[1])] = _oos_d
            print(f"[START_DATES --oos-all] 全{len(start_dates)}件 "
                  f"→ OOS開始日 {_oos_d} 以降のみ集計 (最新基準月={_latest_yyyymm})")
        elif getattr(args, "legacy_latest_only", False):
            for k in merged:
                if src_yyyymm.get(k) == _latest_yyyymm:  # 最新基準月が初出 = 新規銘柄
                    _cn = str(row_map[k][0]).upper().removesuffix(".T").split(".")[0]
                    start_dates[(_cn, k[1])] = _oos_d
            print(f"[START_DATES --legacy-latest-only] 最新基準月({_latest_yyyymm})のみ新規 "
                  f"{len(start_dates)}件 → ⛔ 他のペアは過去に遡って集計される(先読みあり)")
        else:
            # 既定: ペアごとに「初出の基準月の翌月1日」
            _by_month: dict = {}
            for k in merged:
                _ym = src_yyyymm.get(k)
                if not _ym:
                    continue
                _cn = str(row_map[k][0]).upper().removesuffix(".T").split(".")[0]
                _sd = _oos_start_date(_ym)
                start_dates[(_cn, k[1])] = _sd
                _by_month[_sd] = _by_month.get(_sd, 0) + 1
            print(f"[START_DATES per-symbol] 全{len(start_dates)}件に"
                  f"『初出基準月の翌月1日』を設定 (先読み防止)")
            for _sd in sorted(_by_month):
                print(f"    {_sd} 以降のみ集計: {_by_month[_sd]:>5}件")
    n_overlap = sum(1 for k in merged_keys if src_count.get(k, 0) >= 2)
    _crit = (f"min-votes={max(1, min(args.min_votes, n_files))}/{n_files}"
             if (args.min_votes and args.min_votes > 0) else f"mode={args.mode}")
    print(f"[結果] {_crit} → {len(merged)}件 "
          f"(うち2ファイル以上に共通={n_overlap}件)")
    # 投票分布(何個のepochに出たか)を表示: ロバスト核がどれだけあるか把握用
    _dist: dict = {}
    for k in merged_keys:
        _dist[src_count.get(k, 0)] = _dist.get(src_count.get(k, 0), 0) + 1
    print("  投票分布(採用分): " + " / ".join(
        f"{v}個epoch={_dist[v]}件" for v in sorted(_dist, reverse=True)))

    lines = [
        '"""lss proposal (merged) — merge_lss_proposals.py 生成',
        f'  {_crit} / sources={", ".join(Path(x).name for x in args.files)}',
        f'  {len(merged)}件 (2ファイル以上共通={n_overlap}件)。BTは基準月非依存なので run 側でBT降順に予算配分される。',
        '"""',
        "SELECTED = [",
    ]
    for k in merged:
        code, name, strat = row_map[k]
        _name = name.replace('"', "'")
        lines.append(f'    ("{code}", "{_name}", "{strat}"),')
    lines.append("]")
    # SOURCE_BASES: (正規化コード, 戦略) -> "12/6" のような基準月ラベル。
    # run_signals_holdout_all がレポートに流し、シグナル/明細に基準月バッジを出す。
    # かぶり銘柄でも「どの基準月由来か」を見分けられるようにするため。
    lines.append("")
    lines.append("# (正規化コード, 戦略) -> 選定基準月ラベル(かぶりは '/' 連結, 例 '12/6')")
    lines.append("SOURCE_BASES = {")
    for k in merged:
        code, _name, strat = row_map[k]
        _cn = str(code).upper().removesuffix(".T").split(".")[0]
        _lab = "/".join(src_bases.get(k, []))
        lines.append(f'    ("{_cn}", "{strat}"): "{_lab}",')
    lines.append("}")
    # START_DATES: 最新基準月のみで初めて選ばれた銘柄の OOS 開始日。
    # レポートはこの日付以降の取引のみ集計する(過去日への遡及を防ぐ)。
    lines.append("")
    lines.append("# (正規化コード, 戦略) -> OOS開始日(YYYY-MM-DD)。最新基準月のみ銘柄の遡及防止。")
    lines.append("START_DATES = {")
    for (cn, st), sd in sorted(start_dates.items()):
        lines.append(f'    ("{cn}", "{st}"): "{sd}",')
    lines.append("}")
    # MERGED_BASES: マージした基準月(フル YYYY-MM)。レポートの「選定基準月」ヘッダに出す。
    _bmonths = []
    for f in args.files:
        m = re.search(r"(\d{4}-\d{2})", Path(f).name)
        if m and m.group(1) not in _bmonths:
            _bmonths.append(m.group(1))
    _merged_bases_disp = " / ".join(sorted(_bmonths)) if _bmonths else ""
    lines.append("")
    lines.append("# マージした基準月(フル YYYY-MM)。レポートの選定基準月ヘッダ表示用。")
    lines.append(f'MERGED_BASES = "{_merged_bases_disp}"')
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[出力] {Path(args.out).resolve()}")
    print("  次: run_signals_holdout_all.py --lss-proposal でこのファイルを指定して比較。")


if __name__ == "__main__":
    main()
