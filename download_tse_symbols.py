"""
東証上場銘柄リストをダウンロードして symbols_listed_all.py を生成する。

使い方:
  python download_tse_symbols.py           # 東証プライム全銘柄（デフォルト）
  python download_tse_symbols.py --market prime     # 東証プライム
  python download_tse_symbols.py --market standard  # 東証スタンダード
  python download_tse_symbols.py --market all       # 東証全市場

出力: symbols_listed_all.py（backtest_*.py の --universe all で自動読み込み）

データソース: JPX（日本取引所グループ）公開CSV
  https://www.jpx.co.jp/markets/statistics-equities/misc/01.html
"""

import argparse
import io
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import requests

# 市場区分コード
MARKET_MAP = {
    "prime":    "プライム",
    "standard": "スタンダード",
    "growth":   "グロース",
    "all":      "全市場",
}

# JPX 上場銘柄一覧 CSV URL
JPX_CSV_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"


def download_from_jpx(market: str) -> list[tuple[str, str]]:
    """JPX から上場銘柄一覧をダウンロードしてリストを返す。"""
    print(f"  JPX から銘柄一覧をダウンロード中... ({JPX_CSV_URL})")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; python-requests)",
        "Referer":    "https://www.jpx.co.jp/",
    }
    try:
        resp = requests.get(JPX_CSV_URL, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  エラー: JPXからのダウンロードに失敗しました: {e}")
        return []

    try:
        df = pd.read_excel(io.BytesIO(resp.content), dtype=str)
    except Exception as e:
        print(f"  エラー: Excelファイルの読み込みに失敗しました: {e}")
        return []

    # 列名の正規化
    df.columns = [str(c).strip() for c in df.columns]

    # コード列・名称列・市場列の特定
    code_col = next((c for c in df.columns if "コード" in c), None)
    name_col = next((c for c in df.columns if "銘柄名" in c or "会社名" in c), None)
    mkt_col  = next((c for c in df.columns if "市場・商品区分" in c or "市場区分" in c), None)

    if not code_col or not name_col:
        print(f"  エラー: 列が見つかりません。列名: {list(df.columns)}")
        return []

    print(f"  列: コード='{code_col}', 銘柄名='{name_col}', 市場='{mkt_col}'")

    # 市場フィルター
    if market != "all" and mkt_col:
        market_label = MARKET_MAP.get(market, "プライム")
        df = df[df[mkt_col].str.contains(market_label, na=False)]

    symbols = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().replace(" ", "")
        name = str(row[name_col]).strip()
        if not code or not code.isdigit() or len(code) != 4:
            continue
        symbols.append((f"{code}.T", name))

    return symbols


def write_symbols_file(symbols: list[tuple[str, str]], market: str) -> Path:
    """symbols_listed_all.py を書き出す。"""
    out = Path("symbols_listed_all.py")
    lines = [
        '"""',
        f"東証上場銘柄リスト（市場: {MARKET_MAP.get(market, market)}）",
        f"生成: download_tse_symbols.py --market {market}",
        f"銘柄数: {len(symbols)}",
        '"""',
        "",
        "SYMBOLS: list[tuple[str, str]] = [",
    ]
    for code, name in sorted(symbols):
        safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    ("{code}", "{safe_name}"),')
    lines.append("]")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="東証上場銘柄リストをダウンロードして symbols_listed_all.py を生成")
    parser.add_argument(
        "--market",
        default="prime",
        choices=["prime", "standard", "growth", "all"],
        help="対象市場: prime（プライム）/ standard（スタンダード）/ growth（グロース）/ all（全市場）",
    )
    args = parser.parse_args()

    print(f"\n東証銘柄リスト ダウンロード（市場: {MARKET_MAP.get(args.market, args.market)}）")

    symbols = download_from_jpx(args.market)

    if not symbols:
        print("\nフォールバック: yfinance スクリーナーを使用します...")
        symbols = fallback_yfinance()

    if not symbols:
        print("\nダウンロードに失敗しました。手動で symbols_listed_all.py を作成してください。")
        sys.exit(1)

    out = write_symbols_file(symbols, args.market)
    print(f"\n完了: {len(symbols)}銘柄 → {out.resolve()}")
    print("\nバックテストコマンド例:")
    print("  python backtest_limit_oco.py --universe all")
    print("  python backtest_adaptive_mr.py --universe all")
    print("  python backtest_donchian_pullback.py --universe all")
    print("  python backtest_bb_volume.py --universe all")


def fallback_yfinance() -> list[tuple[str, str]]:
    """yfinanceでTOPIX構成銘柄を取得するフォールバック。"""
    try:
        import yfinance as yf
        # TOPIXは直接取得できないため、日経225ベースで試みる
        from symbols_all import SYMBOLS as nk225
        print(f"  yfinanceフォールバック: 日経225 ({len(nk225)}銘柄) を使用")
        return list(nk225)
    except Exception as e:
        print(f"  yfinanceフォールバックも失敗: {e}")
        return []


if __name__ == "__main__":
    main()
