"""
_diagnose_jquants.py  ―  J-Quants API診断
==================================================================
daily_fetch_minute.py で大量エラーが出る原因を特定するための診断ツール。

【使い方】
  python _diagnose_jquants.py                # 標準診断
  python _diagnose_jquants.py 7203          # 特定銘柄テスト
  python _diagnose_jquants.py --plan        # プラン情報も取得試行

【チェック項目】
  1. .env の APIキー読込
  2. クライアント初期化
  3. 主要銘柄(トヨタ等)の日足取得
  4. 主要銘柄の5分足取得 ★最重要
  5. 詳細なエラーメッセージ表示
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))


def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", nargs="?", default="7203",
                        help="テストする銘柄コード (デフォルト: 7203 トヨタ)")
    parser.add_argument("--days", type=int, default=7,
                        help="取得日数 (デフォルト 7)")
    args = parser.parse_args()

    print("=" * 60)
    print("  J-Quants API 診断")
    print("=" * 60)

    # 1. APIキー確認
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        print("✗ JQUANTS_API_KEY が .env に設定されていません")
        sys.exit(1)
    print(f"✓ APIキー読込: {api_key[:10]}...{api_key[-4:]}")

    # メールアドレス・パスワード認証も確認
    mail = os.environ.get("JQUANTS_MAIL", "")
    pw = os.environ.get("JQUANTS_PASSWORD", "")
    if mail or pw:
        print(f"  追加認証: mail={mail[:5]}*** password={'設定済み' if pw else '未設定'}")

    # 2. クライアント初期化
    print()
    print("[1/4] クライアント初期化...")
    try:
        from jquants_fetch import _ensure_client
        cli = _ensure_client()
        print("✓ 初期化成功")
    except ImportError:
        try:
            import jquantsapi
            cli = jquantsapi.Client(refresh_token=api_key)
            print("✓ jquantsapi 直接初期化成功")
        except Exception as e:
            print(f"✗ 初期化失敗: {e}")
            sys.exit(1)
    except Exception as e:
        print(f"✗ 初期化失敗: {e}")
        sys.exit(1)

    # 3. 日足取得テスト
    print()
    print(f"[2/4] {args.symbol} 日足取得テスト...")
    try:
        # jquantsapi 経由
        today = datetime.now(JST)
        from_date = (today - timedelta(days=args.days)).strftime("%Y%m%d")
        to_date = today.strftime("%Y%m%d")
        code5 = args.symbol if len(args.symbol) == 5 else args.symbol + "0"

        df_daily = cli.get_prices_daily_quotes(
            code=code5, from_yyyymmdd=from_date, to_yyyymmdd=to_date
        )
        if df_daily is not None and len(df_daily) > 0:
            print(f"✓ 日足取得成功: {len(df_daily)}日分")
            print(f"  期間: {df_daily.iloc[0].get('Date')} 〜 "
                  f"{df_daily.iloc[-1].get('Date')}")
        else:
            print(f"⚠ 日足データ空(銘柄が存在しないかプラン外?)")
    except Exception as e:
        print(f"✗ 日足取得失敗: {type(e).__name__}: {e}")

    # 4. 5分足取得テスト ★最重要
    print()
    print(f"[3/4] {args.symbol} 5分足取得テスト ★最重要")
    try:
        df_5min = cli.get_eq_bars_5minute(
            code=code5, from_yyyymmdd=from_date, to_yyyymmdd=to_date
        )
        if df_5min is not None and len(df_5min) > 0:
            print(f"✓ 5分足取得成功: {len(df_5min)}本")
            print(f"  カラム: {list(df_5min.columns)}")
            if "Date" in df_5min.columns:
                print(f"  期間: {df_5min.iloc[0].get('Date')} 〜 "
                      f"{df_5min.iloc[-1].get('Date')}")
        else:
            print(f"⚠ 5分足データ空")
            print(f"  → 該当銘柄が無効 or プラン契約範囲外の可能性")
    except Exception as e:
        err_type = type(e).__name__
        err_msg = str(e)
        print(f"✗ 5分足取得失敗: {err_type}")
        print(f"  メッセージ: {err_msg}")
        # よくあるエラーの解釈
        if "subscription" in err_msg.lower() or "plan" in err_msg.lower():
            print(f"  → ★プラン契約が必要 (Standard以上)")
        elif "404" in err_msg or "not found" in err_msg.lower():
            print(f"  → 銘柄が存在しない")
        elif "401" in err_msg or "403" in err_msg:
            print(f"  → 認証エラー (APIキー再確認)")
        elif "429" in err_msg:
            print(f"  → レート制限 (リクエスト過多)")
        elif "timeout" in err_msg.lower():
            print(f"  → タイムアウト (ネットワーク or サーバー問題)")

    # 5. プラン情報取得試行
    print()
    print("[4/4] プラン情報取得試行...")
    try:
        # ListedInfo は大半のプランで利用可
        info = cli.get_listed_info(code=code5, date_yyyymmdd=to_date)
        if info is not None and len(info) > 0:
            row = info.iloc[0]
            print(f"✓ ListedInfo: {row.get('CompanyName', '')} / "
                  f"{row.get('MarketCodeName', '')} / "
                  f"{row.get('Sector33CodeName', '')}")
    except Exception as e:
        print(f"⚠ ListedInfo: {type(e).__name__}: {e}")

    # 6. まとめ
    print()
    print("=" * 60)
    print("  診断結果まとめ")
    print("=" * 60)
    print()
    print("【次のアクション】")
    print()
    print("✗ 5分足取得が「プラン契約必要」エラー:")
    print("    → J-Quants Standard プラン (月3,300円) 以上が必要")
    print("    → Free/Light プランでは5分足取得不可")
    print()
    print("✗ 5分足取得がエラー(その他):")
    print("    → エラー内容を確認")
    print("    → APIキー再発行検討")
    print()
    print("✓ 5分足取得が成功:")
    print("    → 大半の銘柄が取れない原因は別")
    print("    → 銘柄コードの問題かも")
    print("    → 他の銘柄でテスト推奨")
    print()
    print("【参考: J-Quantsプラン】")
    print("  Free        : 4年遅延の日足のみ")
    print("  Light  (110円/月): 12週遅延の日足")
    print("  Standard(2,200円/月): 12週遅延の5分足 ★必要")
    print("  Premium(16,500円/月): リアルタイム")
    print("  https://jpx-jquants.com/")


if __name__ == "__main__":
    main()
