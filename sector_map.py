"""
sector_map.py — 証券コード帯からセクター(業種)を推定する
=========================================================

日本株の4桁証券コードは、伝統的に番号帯がゆるく業種に対応している
(東証の旧・業種別コード割当)。これを使ってセクターを推定し、
WATCHLIST のセクター集中(例: 地銀ばかり)を検出・制限するための分類器。

【限界】
  - 2010年代以降の持株会社化で銀行が 7xxx/5xxx の新コードになった例があり、
    コード帯だけでは銀行と判定できない → _OVERRIDE 辞書で個別補正。
  - 新規上場(連番)は業種と無関係なので推定が外れることがある。
  - あくまで「集中リスクの目安」。厳密な業種分類ではない。

使い方:
  from sector_map import get_sector
  get_sector("8387")    # → "金融"
  get_sector("8387.T")  # → "金融" (.T は除去)

CLI (現 WATCHLIST のセクター集中度を表示):
  python sector_map.py
  python sector_map.py --family stop
"""

from __future__ import annotations

# ── コード帯 → セクター (始点コードを昇順に。bisect で帯を判定) ──────
# (range_start, sector) のリスト。range_start 以上 次の range_start 未満。
_RANGES: list[tuple[int, str]] = [
    (1300, "水産・農林"),
    (1500, "鉱業"),
    (1700, "建設"),
    (2000, "食品"),
    (3000, "繊維・紙"),
    (4000, "化学"),
    (4500, "医薬品"),
    (4600, "化学"),
    (5000, "石油・ゴム・窯業"),
    (5400, "鉄鋼"),
    (5700, "非鉄・金属"),
    (6000, "機械"),
    (6500, "電機"),
    (7000, "輸送用機器"),
    (7300, "輸送用機器"),
    (7400, "その他製品"),
    (8000, "商業(卸・小売)"),
    (8300, "金融"),      # 8300-8799: 銀行・その他金融・証券・保険
    (8800, "不動産"),
    (9000, "運輸・倉庫"),
    (9300, "運輸・倉庫"),
    (9400, "情報通信"),
    (9500, "電力・ガス"),
    (9600, "サービス"),
]

# ── 個別補正 (コード帯だけでは誤判定する銘柄) ───────────────────────
# 特に持株会社化で番号帯が業種とズレた金融グループを補正。
_OVERRIDE: dict[str, str] = {
    # 地銀・金融持株会社 (新コードで銀行帯に入っていない)
    "7167": "金融",   # めぶきフィナンシャルグループ
    "7322": "金融",   # 三十三フィナンシャルグループ
    "5831": "金融",   # しずおかフィナンシャルグループ
    "7186": "金融",   # コンコルディア・FG (参考)
    "7180": "金融",   # 九州FG (参考)
    "7182": "金融",   # ゆうちょ銀行 (参考)
    # 化学帯に入っているが情報通信
    "4390": "情報通信",   # IPS
    # 水産帯(1300台)に入っているが建設(通信建設)
    "1417": "建設",   # ミライト・ワン
}


def _code(symbol: str) -> str:
    """'8387.T' や '8387' から数値コード部分を取り出す。"""
    return symbol.split(".")[0].strip()


def get_sector(symbol: str) -> str:
    """証券コード(または '<code>.T') からセクター名を返す。不明は 'その他'。"""
    code = _code(symbol)
    if code in _OVERRIDE:
        return _OVERRIDE[code]
    try:
        n = int(code[:4])
    except (ValueError, IndexError):
        return "その他"
    sector = "その他"
    for start, name in _RANGES:
        if n >= start:
            sector = name
        else:
            break
    return sector


# ── CLI: 現 WATCHLIST のセクター集中度レポート ─────────────────────
def _concentration_report(family: str) -> None:
    from collections import Counter, defaultdict

    rows: list[tuple[str, str, str]] = []
    if family in ("all", "stop"):
        import check_signals_stop as _stop
        rows += [(s, n, st) for s, n, st in _stop.WATCHLIST]
    if family in ("all", "breakout"):
        import check_signals_breakout as _brk
        rows += [(s, n, st) for s, n, st in _brk.WATCHLIST]

    sec_count: Counter = Counter()
    sec_syms: dict[str, list[str]] = defaultdict(list)
    for sym, name, strat in rows:
        sec = get_sector(sym)
        sec_count[sec] += 1
        sec_syms[sec].append(f"{sym.split('.')[0]}{name[:8]}")

    total = len(rows)
    print("=" * 70)
    print(f"WATCHLIST セクター集中度  ({family})  全{total}銘柄×戦略")
    print("=" * 70)
    for sec, cnt in sec_count.most_common():
        bar = "█" * cnt
        pct = cnt / total * 100
        print(f"  {sec:<16}{cnt:>3}件 ({pct:4.0f}%) {bar}")
        print(f"      {', '.join(sec_syms[sec])}")
    # 集中の警告
    top_sec, top_cnt = sec_count.most_common(1)[0]
    if top_cnt / total > 0.25:
        print(f"\n⚠ {top_sec} が {top_cnt}件 ({top_cnt/total*100:.0f}%) と集中。"
              f"同セクター同時下落のリスクあり。")
        print("  build_watchlist.py --max-per-sector で選定時に制限できます。")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="WATCHLIST セクター集中度レポート")
    ap.add_argument("--family", choices=["all", "stop", "breakout"], default="all")
    args = ap.parse_args()
    _concentration_report(args.family)
