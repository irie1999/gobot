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

  その後:
  python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 \
      --lss-proposal lss_proposal_merged_06_12.py --long-base 2026-06-30 \
      --no-mirror --default-tab lss --force --no-news --no-risk --workers 8 \
      --output-suffix _merged0612
"""
import argparse
import runpy
from pathlib import Path


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
                    help="union=どちらかに入れば採用(既定) / intersection=全ファイル共通のみ")
    ap.add_argument("--out", type=str, default="lss_proposal_merged.py", help="出力ファイル名")
    args = ap.parse_args()

    if len(args.files) < 2:
        raise SystemExit("[ERROR] マージには2ファイル以上が必要です")

    # 各ファイルの (code, strat) 集合と、code→name / (code,strat)→行 を集める
    per_file_keys: list[set] = []
    name_map: dict = {}
    row_map: dict = {}   # (code, strat) -> (code, name, strat)
    src_count: dict = {}  # (code, strat) -> 何ファイルに出たか
    for f in args.files:
        sel = _load_selected(f)
        keys = set()
        for code, name, strat in sel:
            k = (code, strat)
            keys.add(k)
            name_map.setdefault(code, name)
            row_map[k] = (code, name_map[code], strat)
        for k in keys:
            src_count[k] = src_count.get(k, 0) + 1
        per_file_keys.append(keys)
        print(f"[読込] {f}: {len(keys)}件 (code,strat)")

    if args.mode == "union":
        merged_keys = set().union(*per_file_keys)
    else:  # intersection: 全ファイル共通
        merged_keys = set(per_file_keys[0])
        for ks in per_file_keys[1:]:
            merged_keys &= ks

    # BTスコア順は run 側で付くので、ここでは code,strat でソートして安定出力にするだけ
    merged = sorted(merged_keys, key=lambda k: (k[0], k[1]))
    n_overlap = sum(1 for k in merged_keys if src_count.get(k, 0) >= 2)
    print(f"[結果] mode={args.mode} → {len(merged)}件 "
          f"(うち2ファイル以上に共通={n_overlap}件)")

    lines = [
        '"""lss proposal (merged) — merge_lss_proposals.py 生成',
        f'  mode={args.mode} / sources={", ".join(Path(x).name for x in args.files)}',
        f'  {len(merged)}件 (共通={n_overlap}件)。BTは基準月非依存なので run 側でBT降順に予算配分される。',
        '"""',
        "SELECTED = [",
    ]
    for k in merged:
        code, name, strat = row_map[k]
        _name = name.replace('"', "'")
        lines.append(f'    ("{code}", "{_name}", "{strat}"),')
    lines.append("]")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[出力] {Path(args.out).resolve()}")
    print("  次: run_signals_holdout_all.py --lss-proposal でこのファイルを指定して比較。")


if __name__ == "__main__":
    main()
