"""
jquants_client.py  ―  J-Quants V2 互換ラッパー (公式クライアント使用)
==================================================================
公式 jquantsapi ライブラリ (pip install jquants-api-client) を使用。
このファイルは後方互換のため残しているが、実質的に jquants_fetch.py
を直接使用してください。

【セットアップ】
  pip install jquants-api-client
  .env に JQUANTS_API_KEY=... を記載

【CLI テスト (簡易)】
  python jquants_client.py auth     # 認証テスト
  python jquants_client.py daily 72030  # 日足テスト
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _load_dotenv() -> None:
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
            return
        except Exception:
            pass


_load_dotenv()


def get_client():
    """公式 jquantsapi.ClientV2 を返す。"""
    try:
        import jquantsapi
    except ImportError:
        print("[ERROR] jquants-api-client が未インストールです。\n"
              "  pip install jquants-api-client", file=sys.stderr)
        sys.exit(1)
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[ERROR] JQUANTS_API_KEY が設定されていません。\n"
              "  .env ファイルまたは環境変数に設定してください。",
              file=sys.stderr)
        sys.exit(1)
    return jquantsapi.ClientV2(api_key=api_key)


def _cmd_auth(args):
    cli = get_client()
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    preview = f"{api_key[:10]}...{api_key[-5:]}" if len(api_key) > 20 else api_key
    print(f"API Key: {preview}  (長さ {len(api_key)})")
    print(f"Client: {type(cli).__name__}")
    print()
    print("動作確認: 上場銘柄一覧 (72030 トヨタ) 取得中...")
    try:
        df = cli.get_eq_master(code="72030")
        if df is not None and not df.empty:
            print(f"  ✓ 認証成功 ({len(df)}件)")
            cols = [c for c in ["Code", "CompanyName", "MarketCodeName"]
                    if c in df.columns]
            if cols:
                print("  " + df[cols].head(1).to_string(index=False))
        else:
            print("  [warn] レスポンスが空です")
    except Exception as e:
        print(f"  [ERROR] {e}")
        sys.exit(1)


def _cmd_daily(args):
    from dateutil import tz
    cli = get_client()
    now = datetime.now(tz=tz.gettz("Asia/Tokyo"))
    start = now - timedelta(days=args.days)
    print(f"日足取得中: {args.code}  {start.date()} 〜 {now.date()}")
    try:
        df = cli.get_eq_bars_daily_range(
            start_dt=start, end_dt=now,
        )
        if df is not None and "Code" in df.columns:
            df = df[df["Code"].astype(str).str.startswith(args.code[:4])]
        if df is not None and not df.empty:
            print(f"取得結果: {len(df)}行")
            print(df.head(5).to_string(index=False))
            if len(df) > 5:
                print("...")
                print(df.tail(3).to_string(index=False))
        else:
            print("データなし")
    except Exception as e:
        print(f"[ERROR] {e}")


def _cmd_minute(args):
    from dateutil import tz
    cli = get_client()
    now = datetime.now(tz=tz.gettz("Asia/Tokyo"))
    start = now - timedelta(days=args.days)
    method_map = {
        "1m": "get_eq_bars_minute",
        "5m": "get_eq_bars_5minute",
        "15m": "get_eq_bars_15minute",
    }
    method_name = method_map.get(args.interval, "get_eq_bars_5minute")
    from_d = start.strftime("%Y%m%d")
    to_d = now.strftime("%Y%m%d")
    print(f"{args.interval} 取得中: {args.code}  {from_d} 〜 {to_d}")
    try:
        method = getattr(cli, method_name)
        df = method(code=args.code, from_yyyymmdd=from_d, to_yyyymmdd=to_d)
        if df is not None and "Code" in df.columns:
            df = df[df["Code"].astype(str).str.startswith(args.code[:4])]
        if df is not None and not df.empty:
            print(f"取得結果: {len(df)}行  columns: {list(df.columns)}")
            print(df.head(5).to_string(index=False))
            if len(df) > 5:
                print("...")
                print(df.tail(3).to_string(index=False))
        else:
            print("データなし")
    except Exception as e:
        print(f"[ERROR] {e}")


def main():
    parser = argparse.ArgumentParser(
        description="J-Quants V2 CLI (公式クライアント)")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("auth", help="認証テスト").set_defaults(func=_cmd_auth)

    p = sub.add_parser("daily", help="日足取得")
    p.add_argument("code", help="銘柄コード (例: 72030)")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=_cmd_daily)

    p = sub.add_parser("minute", help="分足取得 (アドオン必要)")
    p.add_argument("code", help="銘柄コード (例: 72030)")
    p.add_argument("--interval", choices=["1m", "5m", "15m"], default="5m")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=_cmd_minute)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
