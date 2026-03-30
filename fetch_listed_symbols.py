"""
東証全上場銘柄リスト取得スクリプト
────────────────────────────────────────────────────────────────────────
JPX公式データをダウンロードし、銘柄リストファイルを生成する。

■ 実行方法:
  python fetch_listed_symbols.py                # プライム市場のみ（約1,800銘柄）
  python fetch_listed_symbols.py --market all   # 全市場（プライム+スタンダード+グロース）
  python fetch_listed_symbols.py --market prime     # プライムのみ
  python fetch_listed_symbols.py --market standard  # プライム+スタンダード
  python fetch_listed_symbols.py --min-price 500    # 500円以上に絞る（要データ取得）
  python fetch_listed_symbols.py --no-cache     # キャッシュを無視して再ダウンロード

■ 出力ファイル:
  symbols_listed_prime.py     プライム市場銘柄
  symbols_listed_standard.py  プライム+スタンダード銘柄
  symbols_listed_all.py       全上場銘柄

■ データソース:
  JPX（日本取引所グループ）上場銘柄一覧
  https://www.jpx.co.jp/markets/statistics-equities/misc/01.html
"""

import argparse
import io
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# ── JPX データソース ──────────────────────────────────────────
JPX_XLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
CACHE_FILE  = Path(".jpx_listed_cache.pkl")
CACHE_DAYS  = 7   # 7日間キャッシュ

# ── 市場区分フィルター ────────────────────────────────────────
PRIME_LABEL    = "プライム（内国株式）"
STANDARD_LABEL = "スタンダード（内国株式）"
GROWTH_LABEL   = "グロース（内国株式）"

MARKET_SETS = {
    "prime":    {PRIME_LABEL},
    "standard": {PRIME_LABEL, STANDARD_LABEL},
    "all":      {PRIME_LABEL, STANDARD_LABEL, GROWTH_LABEL},
}


def fetch_jpx_raw(no_cache: bool = False) -> pd.DataFrame:
    """JPX から上場銘柄 Excel を取得し DataFrame を返す。"""
    # キャッシュ確認
    if not no_cache and CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "rb") as f:
                cached = pickle.load(f)
            if cached["ts"] >= datetime.now() - timedelta(days=CACHE_DAYS):
                print(f"  キャッシュ使用 ({cached['ts'].strftime('%Y-%m-%d')} 取得分)")
                return cached["df"]
        except Exception:
            CACHE_FILE.unlink(missing_ok=True)

    print(f"  JPX から銘柄リストをダウンロード中...")
    print(f"  URL: {JPX_XLS_URL}")
    try:
        r = requests.get(JPX_XLS_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer":    "https://www.jpx.co.jp/",
        })
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"ダウンロード失敗: {e}\n"
            f"  ブラウザで以下にアクセスして data_j.xls を手動ダウンロードし、\n"
            f"  同じフォルダに置いてから --local data_j.xls で実行してください:\n"
            f"  https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
        ) from e

    df = _parse_xls(io.BytesIO(r.content))

    # キャッシュ保存
    with open(CACHE_FILE, "wb") as f:
        pickle.dump({"ts": datetime.now(), "df": df}, f)
    print(f"  取得完了: {len(df)} 銘柄")
    return df


def load_local_xls(path: str) -> pd.DataFrame:
    """手動ダウンロードした data_j.xls を読み込む。"""
    print(f"  ローカルファイル読み込み: {path}")
    with open(path, "rb") as f:
        df = _parse_xls(f)
    print(f"  読み込み完了: {len(df)} 銘柄")
    return df


def _parse_xls(file_obj) -> pd.DataFrame:
    """JPX Excel を解析して整形済み DataFrame を返す。"""
    # JPX の xls は先頭行がヘッダ
    try:
        raw = pd.read_excel(file_obj, engine="xlrd",
                            dtype={"コード": str})
    except Exception:
        # xlrd がダメなら openpyxl (xlsx 形式の場合)
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        raw = pd.read_excel(file_obj, engine="openpyxl",
                            dtype={"コード": str})

    # カラム名を正規化（スペース除去）
    raw.columns = [str(c).strip() for c in raw.columns]

    # 必須カラムの存在確認
    required = ["コード", "銘柄名", "市場・商品区分"]
    for col in required:
        if col not in raw.columns:
            raise ValueError(
                f"カラム '{col}' が見つかりません。\n"
                f"実際のカラム: {list(raw.columns)}\n"
                "JPX の Excel フォーマットが変わった可能性があります。"
            )

    df = raw[required].copy()
    df.columns = ["code", "name", "market"]
    df = df.dropna(subset=["code"])
    df["code"]   = df["code"].str.strip().str.zfill(4)
    df["name"]   = df["name"].str.strip()
    df["market"] = df["market"].str.strip()
    # コードが4桁数字のもののみ（ETF等を除外）
    df = df[df["code"].str.match(r"^\d{4}$")].copy()
    df["ticker"] = df["code"] + ".T"
    return df.reset_index(drop=True)


def filter_by_market(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """市場区分でフィルタリング。"""
    labels = MARKET_SETS.get(market)
    if labels is None:
        raise ValueError(f"不明な市場区分: {market}. 使用可能: {list(MARKET_SETS)}")
    return df[df["market"].isin(labels)].copy()


def write_symbols_file(df: pd.DataFrame, filename: str,
                       market_label: str) -> Path:
    """symbols_*.py ファイルを生成する。"""
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [
        f'"""',
        f'東証 {market_label} 全上場銘柄リスト ({today} 取得)',
        f'fetch_listed_symbols.py により自動生成',
        f'総銘柄数: {len(df)}',
        f'"""',
        f'SYMBOLS = [',
    ]
    for _, row in df.iterrows():
        name_escaped = row["name"].replace('"', '\\"')
        lines.append(f'    ("{row["ticker"]}", "{name_escaped}"),')
    lines.append(']')

    path = Path(filename)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def print_summary(df: pd.DataFrame, market_label: str) -> None:
    """市場別集計を表示。"""
    print(f"\n  市場区分別 銘柄数:")
    for label, cnt in df["market"].value_counts().items():
        print(f"    {label}: {cnt} 銘柄")
    print(f"  合計: {len(df)} 銘柄")

    # 業種サンプル（先頭3件）
    print(f"\n  先頭3銘柄サンプル:")
    for _, row in df.head(3).iterrows():
        print(f"    {row['ticker']}  {row['name']}  ({row['market']})")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="東証全上場銘柄リスト取得・symbols ファイル生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python fetch_listed_symbols.py                   # プライム市場のみ
  python fetch_listed_symbols.py --market all      # 全市場
  python fetch_listed_symbols.py --market standard # プライム+スタンダード
  python fetch_listed_symbols.py --local data_j.xls  # 手動DLファイルを使用
  python fetch_listed_symbols.py --no-cache        # 強制再取得
""")
    parser.add_argument("--market",   default="prime",
                        choices=list(MARKET_SETS),
                        help="対象市場 (default: prime)")
    parser.add_argument("--local",    default=None, metavar="FILE",
                        help="手動ダウンロードした data_j.xls のパス")
    parser.add_argument("--no-cache", action="store_true",
                        help="キャッシュを無視して再ダウンロード")
    parser.add_argument("--out",      default=None, metavar="FILE",
                        help="出力ファイル名（省略時は市場区分から自動決定）")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  東証 上場銘柄リスト取得")
    print("=" * 60)

    # データ取得
    try:
        if args.local:
            raw_df = load_local_xls(args.local)
        else:
            raw_df = fetch_jpx_raw(no_cache=args.no_cache)
    except RuntimeError as e:
        print(f"\n  エラー: {e}\n")
        return

    # 市場区分フィルター
    df = filter_by_market(raw_df, args.market)

    market_labels = {
        "prime":    "プライム",
        "standard": "プライム+スタンダード",
        "all":      "全市場（プライム+スタンダード+グロース）",
    }
    market_label = market_labels[args.market]
    print_summary(df, market_label)

    # ファイル名決定
    if args.out:
        filename = args.out
    else:
        filename = f"symbols_listed_{args.market}.py"

    path = write_symbols_file(df, filename, market_label)
    print(f"  → {path} を出力しました  ({len(df)} 銘柄)")
    print()

    # V2 スクリプトへの接続方法を表示
    print("  ── 次のステップ ──────────────────────────────")
    print(f"  生成したリストを V2 スクリプトで使う場合:")
    print(f"    select_symbols_v2.py を編集して以下を変更:")
    print(f"      from {path.stem} import SYMBOLS as all_symbols")
    print()
    print(f"  または直接バックテストを実行:")
    print(f"    python backtest_macd_scan_v2.py --years 1  # {len(df)}銘柄対象")
    print()


if __name__ == "__main__":
    main()
