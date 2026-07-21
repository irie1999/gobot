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
    ap.add_argument("files", nargs="+", help="マージする proposal ファイル(2つ以上)")
    ap.add_argument("--mode", choices=["union", "intersection"], default="union",
                    help="union=どれか1つでも採用(既定) / intersection=全ファイル共通のみ。"
                         "--min-votes 指定時はそちらが優先。")
    ap.add_argument("--min-votes", type=int, default=0,
                    help="K-of-N投票: N個中この数以上のファイルに出た銘柄×戦略のみ採用。"
                         "1=union / N=intersection。0(既定)なら --mode に従う。")
    ap.add_argument("--out", type=str, default="lss_proposal_merged.py", help="出力ファイル名")
    args = ap.parse_args()

    if len(args.files) < 2:
        raise SystemExit("[ERROR] マージには2ファイル以上が必要です")

    # 各ファイルの (code, strat) 集合と、code→name / (code,strat)→行 を集める
    per_file_keys: list[set] = []
    name_map: dict = {}
    row_map: dict = {}   # (code, strat) -> (code, name, strat)
    src_count: dict = {}  # (code, strat) -> 何ファイルに出たか
    src_bases: dict = {}  # (code, strat) -> [基準月ラベル,...] (出現ファイル順・重複なし)
    for f in args.files:
        sel = _load_selected(f)
        _tag = _base_tag(f)
        keys = set()
        for code, name, strat in sel:
            # コードを正規化(.T 有無を吸収)してキーにする。これをしないと
            # "7261"(12月) と "7261.T"(6月) が別銘柄扱いになり、かぶりが重複出力される。
            _cn = str(code).upper().removesuffix(".T").split(".")[0]
            k = (_cn, strat)
            keys.add(k)
            name_map.setdefault(_cn, name)
            row_map[k] = (_cn, name_map[_cn], strat)
        for k in keys:
            src_count[k] = src_count.get(k, 0) + 1
            _lst = src_bases.setdefault(k, [])
            if _tag not in _lst:
                _lst.append(_tag)
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
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[出力] {Path(args.out).resolve()}")
    print("  次: run_signals_holdout_all.py --lss-proposal でこのファイルを指定して比較。")


if __name__ == "__main__":
    main()
