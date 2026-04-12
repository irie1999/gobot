"""
jquants_client.py  ―  J-Quants API V2 クライアント
==================================================================
JPX 公式 J-Quants API V2 の株価・財務データ取得クライアント。

【V2 認証】
  - Dashboard > API Key で取得した API Key を x-api-key ヘッダーで送信
  - API Key に有効期限なし (無期限)
  - refresh token / id token の交換フローは不要

【認証情報の設定】
  環境変数:
    JQUANTS_API_KEY=DMRipP16cr...h16a4
  または .env ファイル:
    JQUANTS_API_KEY=DMRipP16cr...h16a4

  .env はプロジェクトルートに置くと自動で読み込まれる。

【対応エンドポイント (Free プラン)】
  - /v2/equities/master              上場銘柄一覧
  - /v2/equities/bars/daily          日足株価
  - /v2/equities/investor-types      投資部門別売買状況
  - /v2/equities/earnings-calendar   決算発表予定
  - /v2/fins/details                 財務諸表
  - /v2/fins/dividend                配当情報
  - /v2/markets/calendar             営業日カレンダー

【使い方】
  from jquants_client import JQuantsClient
  cli = JQuantsClient()
  df = cli.get_daily_quotes(code="72030",
                            from_date="2025-01-01",
                            to_date="2025-12-31")
  print(df.head())

【CLI】
  python jquants_client.py auth              # 認証テスト
  python jquants_client.py listed            # 上場銘柄一覧
  python jquants_client.py daily 72030       # トヨタ日足
  python jquants_client.py diagnose          # 認証診断 (デバッグ用)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# ── J-Quants V2 API 設定 ─────────────────────────────────────
JQUANTS_BASE_URL = "https://api.jquants.com/v2"
CACHE_DIR = Path.home() / ".jquants_cache"
REQUEST_TIMEOUT = 60


def _load_dotenv(path: Path | None = None) -> None:
    """
    最小限の .env ローダ (python-dotenv 不要)。
    CWD または このファイルの親の .env を読み込み、
    未設定の環境変数のみを追加する (既存値は上書きしない)。
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend([
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
    ])
    for p in candidates:
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
        except Exception as e:
            print(f"[jquants] .env load error ({p}): {e}", file=sys.stderr)


# モジュール import 時に .env を自動読み込み
_load_dotenv()


class JQuantsError(Exception):
    """J-Quants API 関連のエラー。"""


class JQuantsClient:
    """J-Quants API V2 クライアント。x-api-key ヘッダー認証。"""

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("JQUANTS_API_KEY")
        if not self.api_key:
            raise JQuantsError(
                "API Key が必要です。以下のどちらかを設定してください:\n"
                "  (A) 環境変数   JQUANTS_API_KEY=...\n"
                "  (B) .env ファイル  JQUANTS_API_KEY=...\n"
                "API Key は Dashboard > API Key で取得可能です。"
            )

        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────
    # 低レベル HTTP
    # ─────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key}

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET /v2{path}。リトライ/レート制限対応。"""
        url = f"{JQUANTS_BASE_URL}{path}"
        for attempt in range(4):
            r = requests.get(
                url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                raise JQuantsError(
                    f"認証失敗 (401): API Key が無効または期限切れです。\n"
                    f"  {r.text[:200]}"
                )
            if r.status_code == 403:
                raise JQuantsError(
                    f"アクセス拒否 (403): このエンドポイントは現在のプランで"
                    f"利用できません。\n  {path}\n  {r.text[:200]}"
                )
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                print(
                    f"[jquants] {r.status_code}, retry in {wait}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            raise JQuantsError(
                f"API エラー: {r.status_code} {path} {r.text[:200]}"
            )
        raise JQuantsError(f"リトライ上限: {path}")

    def _get_paginated(self, path: str, params: dict,
                       records_key: str) -> list[dict]:
        """pagination_key をたどって全件取得。"""
        all_rows: list[dict] = []
        page_params = dict(params)
        while True:
            data = self._get(path, params=page_params)
            all_rows.extend(data.get(records_key, []))
            pagination_key = data.get("pagination_key")
            if not pagination_key:
                break
            page_params["pagination_key"] = pagination_key
        return all_rows

    # ─────────────────────────────────────────────────────────
    # エンドポイント: 上場銘柄一覧
    # ─────────────────────────────────────────────────────────

    def get_listed_info(self, code: str | None = None,
                        date: str | None = None) -> pd.DataFrame:
        """
        上場銘柄一覧を取得 (V2 エンドポイント: /equities/master)。

        code: 5桁銘柄コード (例: "72030")
        date: 基準日 YYYY-MM-DD (省略時は最新)
        """
        params: dict = {}
        if code:
            params["code"] = code
        if date:
            params["date"] = date
        rows = self._get_paginated("/equities/master", params, "info")
        if not rows:
            # V2 の別レスポンス形式を試す
            data = self._get("/equities/master", params=params)
            for key in ("info", "data", "results", "items"):
                if key in data:
                    rows = data[key] or []
                    break
            if not rows and isinstance(data, list):
                rows = data
        return pd.DataFrame(rows)

    # ─────────────────────────────────────────────────────────
    # エンドポイント: 日足株価
    # ─────────────────────────────────────────────────────────

    def get_daily_quotes(
        self,
        code: str | None = None,
        date: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        """
        日足 OHLC を取得 (V2 エンドポイント: /equities/bars/daily)。

        引数:
          code      : 5桁銘柄コード (例: "72030" for トヨタ)
          date      : 特定日のみ取得 (YYYY-MM-DD)
          from_date : 取得開始日 (YYYY-MM-DD)
          to_date   : 取得終了日 (YYYY-MM-DD)

        戻り値:
          J-Quants V2 のレスポンス DataFrame。
          列名は API の返却形式に従う (通常: Date, Code, Open, High, Low,
          Close, Volume, TurnoverValue, Adjustment*)。
        """
        if not (code or date):
            raise JQuantsError("code または date のどちらか必須")

        params: dict = {}
        if code:
            params["code"] = code
        if date:
            params["date"] = date
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        # V2 レスポンスキーは複数候補があり得るため順に試す
        path = "/equities/bars/daily"
        data = self._get(path, params=params)

        # レスポンス本体 (ページネーション対応)
        rows: list[dict] = []
        for key in ("daily_quotes", "bars", "data", "quotes"):
            if key in data:
                rows.extend(data[key] or [])
                break
        else:
            if isinstance(data, list):
                rows = data

        # 残りのページ
        pagination_key = data.get("pagination_key") if isinstance(data, dict) else None
        while pagination_key:
            page_params = dict(params)
            page_params["pagination_key"] = pagination_key
            page = self._get(path, params=page_params)
            for key in ("daily_quotes", "bars", "data", "quotes"):
                if key in page:
                    rows.extend(page[key] or [])
                    break
            pagination_key = page.get("pagination_key") if isinstance(page, dict) else None

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.sort_values(["Date"]).reset_index(drop=True)

        # 数値列を float 化
        numeric_cols = [
            "Open", "High", "Low", "Close", "Volume", "TurnoverValue",
            "AdjustmentFactor", "AdjustmentOpen", "AdjustmentHigh",
            "AdjustmentLow", "AdjustmentClose", "AdjustmentVolume",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ─────────────────────────────────────────────────────────
    # エンドポイント: 分足株価 (アドオン)
    # ─────────────────────────────────────────────────────────

    def get_minute_quotes(
        self,
        code: str,
        date: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        """
        1分足 OHLCV を取得 (V2: /equities/bars/minute)。
        分足・ティックアドオン契約が必要。

        引数:
          code      : 5桁銘柄コード (例: "72030")
          date      : 特定日のみ取得 (YYYY-MM-DD)
          from_date : 取得開始日 (YYYY-MM-DD)
          to_date   : 取得終了日 (YYYY-MM-DD)

        戻り値:
          DataFrame (DateTime, Code, Open, High, Low, Close, Volume 等)

        注意:
          - 大量データなのでページネーションが発生する
          - 長期間をまとめて取ると遅い → 日付チャンクで取るのが安全
        """
        params: dict = {"code": code}
        if date:
            params["date"] = date
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        path = "/equities/bars/minute"
        data = self._get(path, params=params)

        # レスポンスキーの候補
        rows: list[dict] = []
        for key in ("minute_quotes", "bars", "data", "quotes",
                     "minute_bars", "results"):
            if key in data:
                rows.extend(data[key] or [])
                break
        else:
            if isinstance(data, list):
                rows = data

        # ページネーション
        pagination_key = data.get("pagination_key") if isinstance(data, dict) else None
        while pagination_key:
            page_params = dict(params)
            page_params["pagination_key"] = pagination_key
            page = self._get(path, params=page_params)
            for key in ("minute_quotes", "bars", "data", "quotes",
                         "minute_bars", "results"):
                if key in page:
                    rows.extend(page[key] or [])
                    break
            pagination_key = page.get("pagination_key") if isinstance(page, dict) else None

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # DateTime カラムをパース
        dt_col = None
        for cand in ["DateTime", "datetime", "Datetime", "date_time",
                      "Time", "time", "Date", "date"]:
            if cand in df.columns:
                dt_col = cand
                break
        if dt_col:
            df[dt_col] = pd.to_datetime(df[dt_col])
            df = df.sort_values(dt_col).reset_index(drop=True)

        # 数値列を float 化
        for col in ["Open", "High", "Low", "Close", "Volume",
                     "TurnoverValue", "open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df

    # ─────────────────────────────────────────────────────────
    # エンドポイント: 財務・その他
    # ─────────────────────────────────────────────────────────

    def get_financial_details(self, code: str | None = None,
                              date: str | None = None) -> pd.DataFrame:
        """財務諸表を取得 (V2: /fins/details)。"""
        params: dict = {}
        if code:
            params["code"] = code
        if date:
            params["date"] = date
        data = self._get("/fins/details", params=params)
        for key in ("statements", "details", "data"):
            if key in data:
                return pd.DataFrame(data[key] or [])
        return pd.DataFrame()

    def get_earnings_calendar(self) -> pd.DataFrame:
        """決算発表予定 (V2: /equities/earnings-calendar)。"""
        data = self._get("/equities/earnings-calendar")
        for key in ("announcement", "calendar", "data"):
            if key in data:
                return pd.DataFrame(data[key] or [])
        return pd.DataFrame()

    def get_trading_calendar(self, from_date: str | None = None,
                             to_date: str | None = None) -> pd.DataFrame:
        """営業日カレンダー (V2: /markets/calendar)。"""
        params: dict = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data = self._get("/markets/calendar", params=params)
        for key in ("trading_calendar", "calendar", "data"):
            if key in data:
                return pd.DataFrame(data[key] or [])
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _cmd_auth(args: argparse.Namespace) -> None:
    cli = JQuantsClient()
    preview = (f"{cli.api_key[:10]}...{cli.api_key[-5:]}"
               if len(cli.api_key) > 20 else cli.api_key)
    print(f"API Key: {preview}  (長さ {len(cli.api_key)})")
    print(f"Base URL: {JQUANTS_BASE_URL}")
    print()
    print("動作確認: /equities/master で1銘柄取得中...")
    try:
        df = cli.get_listed_info(code="72030")
        if df.empty:
            print("  [warn] レスポンスは空でした")
        else:
            print(f"  ✓ 認証成功 ({len(df)}件)")
            cols = [c for c in ["Code", "CompanyName", "CompanyNameEnglish",
                                "MarketCodeName", "Sector17CodeName"]
                    if c in df.columns]
            if cols:
                print("  " + df[cols].head(3).to_string(index=False).replace("\n", "\n  "))
    except JQuantsError as e:
        print(f"  [ERROR] {e}")
        sys.exit(1)


def _cmd_listed(args: argparse.Namespace) -> None:
    cli = JQuantsClient()
    df = cli.get_listed_info(code=args.code)
    print(f"上場銘柄: {len(df)}件")
    if not df.empty:
        cols = [c for c in ["Date", "Code", "CompanyName", "MarketCodeName",
                            "Sector17CodeName"] if c in df.columns]
        print(df[cols].head(20).to_string(index=False))


def _cmd_daily(args: argparse.Namespace) -> None:
    cli = JQuantsClient()
    from_d = args.from_date or (
        (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    )
    to_d = args.to_date or datetime.now().strftime("%Y-%m-%d")
    print(f"取得中: {args.code}  {from_d} 〜 {to_d}", file=sys.stderr)
    df = cli.get_daily_quotes(code=args.code, from_date=from_d, to_date=to_d)
    print(f"取得結果: {len(df)}行")
    if not df.empty:
        cols = [c for c in ["Date", "Code", "Open", "High", "Low", "Close",
                            "Volume", "AdjustmentClose"] if c in df.columns]
        print(df[cols].head(10).to_string(index=False))
        if len(df) > 10:
            print("...")
            print(df[cols].tail(5).to_string(index=False))


def _cmd_diagnose(args: argparse.Namespace) -> None:
    """
    V2 への移行確認のための診断モード。
    /v2/equities/master と /v2/equities/bars/daily を x-api-key で叩く。
    """
    token = os.environ.get("JQUANTS_API_KEY")
    if not token:
        print("[ERROR] JQUANTS_API_KEY が設定されていません", file=sys.stderr)
        sys.exit(1)

    print(f"API Key: 長さ {len(token)}, 先頭 {token[:10]}..., 末尾 ...{token[-5:]}")
    print(f"Base URL: {JQUANTS_BASE_URL}")
    print()

    headers = {"x-api-key": token}
    test_cases = [
        ("/equities/master", {"code": "72030"}),
        ("/equities/bars/daily", {"code": "72030",
                                   "from": "2025-01-01", "to": "2025-12-31"}),
        ("/markets/calendar", {"from": "2026-04-01", "to": "2026-04-10"}),
    ]
    for path, params in test_cases:
        url = f"{JQUANTS_BASE_URL}{path}"
        try:
            r = requests.get(url, headers=headers, params=params,
                             timeout=REQUEST_TIMEOUT)
            mark = "✓" if r.status_code == 200 else " "
            msg = (r.text[:120] + "...") if len(r.text) > 120 else r.text
            print(f"  {mark} GET {path:<35} → {r.status_code}")
            print(f"      {msg}")
        except Exception as e:
            print(f"  GET {path:<35} → EXCEPTION {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="J-Quants API V2 クライアント CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_auth = sub.add_parser("auth", help="認証テスト")
    p_auth.set_defaults(func=_cmd_auth)

    p_listed = sub.add_parser("listed", help="上場銘柄一覧")
    p_listed.add_argument("--code", help="5桁銘柄コード (例: 72030)")
    p_listed.set_defaults(func=_cmd_listed)

    p_daily = sub.add_parser("daily", help="日足データ取得")
    p_daily.add_argument("code", help="5桁銘柄コード (例: 72030)")
    p_daily.add_argument("--days", type=int, default=365, help="取得日数")
    p_daily.add_argument("--from-date", dest="from_date", help="YYYY-MM-DD")
    p_daily.add_argument("--to-date", dest="to_date", help="YYYY-MM-DD")
    p_daily.set_defaults(func=_cmd_daily)

    p_diag = sub.add_parser("diagnose", help="V2 エンドポイント疎通確認")
    p_diag.set_defaults(func=_cmd_diagnose)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return

    try:
        args.func(args)
    except JQuantsError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
