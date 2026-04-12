"""
jquants_client.py  ―  J-Quants API 低レベルクライアント
==================================================================
JPX (日本取引所グループ) 公式 J-Quants API の認証とデータ取得を扱う。

【認証フロー】
  1. https://jpx-jquants.com のマイページで「リフレッシュトークン」を取得
  2. 環境変数に設定:
        export JQUANTS_REFRESH_TOKEN="eyJhbGciOi..."
     または メール/パスワードでも可:
        export JQUANTS_MAIL="you@example.com"
        export JQUANTS_PASSWORD="yourpassword"
  3. 初回呼び出しで /v1/token/auth_refresh により ID トークン取得
  4. ID トークンを ~/.jquants_cache/id_token.json にキャッシュ
     (有効期限 24h、余裕を持って 20h で再取得)

【対応エンドポイント (Free プラン)】
  - /v1/listed/info            上場銘柄一覧
  - /v1/prices/daily_quotes    日足四本値
  - /v1/fins/statements        財務情報
  - /v1/fins/announcement      決算発表予定

【使い方】
  from jquants_client import JQuantsClient
  cli = JQuantsClient()
  df = cli.get_daily_quotes(code="72030",
                            from_date="2025-01-01",
                            to_date="2025-12-31")
  print(df.head())

【CLI テスト】
  python jquants_client.py auth            # 認証テスト
  python jquants_client.py listed          # 上場銘柄一覧取得
  python jquants_client.py daily 72030     # トヨタの日足1年分
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

JQUANTS_BASE_URL = "https://api.jquants.com/v1"
CACHE_DIR = Path.home() / ".jquants_cache"
ID_TOKEN_CACHE = CACHE_DIR / "id_token.json"
ID_TOKEN_TTL_HOURS = 20  # 実際は 24h、余裕を持たせる
REQUEST_TIMEOUT = 60


def _load_dotenv(path: Path | None = None) -> None:
    """
    最小限の .env ローダ (python-dotenv 不要)。
    プロジェクトルート (CWD or このファイルの親) の .env を読み込み、
    未設定の環境変数のみを上書きせず追加する。
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
    """J-Quants API クライアント。認証・リトライ・ページネーションを内包。

    対応する認証方式 (優先度順):
      1. API Key (新方式)           : JQUANTS_API_KEY
         → Bearer として直接使用、交換不要、短い opaque トークン
      2. ID Token (直接指定)         : JQUANTS_ID_TOKEN
         → Bearer として直接使用、24時間有効
      3. Refresh Token (従来方式)    : JQUANTS_REFRESH_TOKEN
         → /v1/token/auth_refresh で ID Token を取得
      4. メール+パスワード (フルパス) : JQUANTS_MAIL + JQUANTS_PASSWORD
         → /v1/token/auth_user で refresh token → ID token の順に取得
    """

    def __init__(
        self,
        api_key: str | None = None,
        id_token: str | None = None,
        refresh_token: str | None = None,
        mail: str | None = None,
        password: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("JQUANTS_API_KEY")
        self.direct_id_token = id_token or os.environ.get("JQUANTS_ID_TOKEN")
        self.refresh_token = refresh_token or os.environ.get("JQUANTS_REFRESH_TOKEN")
        self.mail = mail or os.environ.get("JQUANTS_MAIL")
        self.password = password or os.environ.get("JQUANTS_PASSWORD")

        has_credential = bool(
            self.api_key
            or self.direct_id_token
            or self.refresh_token
            or (self.mail and self.password)
        )
        if not has_credential:
            raise JQuantsError(
                "認証情報が必要です。以下のいずれかを設定してください:\n"
                "  (A) JQUANTS_API_KEY          新方式 API Key (推奨)\n"
                "      Dashboard > API Key で取得\n"
                "  (B) JQUANTS_ID_TOKEN         24時間有効な ID Token\n"
                "  (C) JQUANTS_REFRESH_TOKEN    従来のリフレッシュトークン\n"
                "  (D) JQUANTS_MAIL + JQUANTS_PASSWORD  メール+パスワード"
            )

        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._token_cache_path = self._cache_dir / "id_token.json"

        self._id_token: str | None = None
        self._id_token_expiry: datetime | None = None
        self._load_cached_token()

    # ─────────────────────────────────────────────────────────
    # 認証
    # ─────────────────────────────────────────────────────────

    def _load_cached_token(self) -> None:
        if not self._token_cache_path.exists():
            return
        try:
            data = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
            expiry = datetime.fromisoformat(data["expiry"])
            if expiry > datetime.now():
                self._id_token = data["id_token"]
                self._id_token_expiry = expiry
        except Exception:
            pass

    def _save_cached_token(self) -> None:
        if not (self._id_token and self._id_token_expiry):
            return
        try:
            self._token_cache_path.write_text(
                json.dumps({
                    "id_token": self._id_token,
                    "expiry": self._id_token_expiry.isoformat(),
                }),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[jquants] token cache write failed: {e}", file=sys.stderr)

    def _exchange_mail_for_refresh_token(self) -> None:
        """メール+パスワードから refresh token を取得。"""
        url = f"{JQUANTS_BASE_URL}/token/auth_user"
        r = requests.post(
            url,
            json={"mailaddress": self.mail, "password": self.password},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            raise JQuantsError(
                f"メール認証失敗: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        self.refresh_token = data.get("refreshToken")
        if not self.refresh_token:
            raise JQuantsError("refreshToken がレスポンスに含まれていません")

    def _refresh_id_token(self) -> None:
        """refresh_token から ID token を取得 (従来方式)。"""
        if not self.refresh_token:
            self._exchange_mail_for_refresh_token()

        url = f"{JQUANTS_BASE_URL}/token/auth_refresh"
        r = requests.post(
            url,
            params={"refreshtoken": self.refresh_token},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            raise JQuantsError(
                f"ID token 取得失敗: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        self._id_token = data.get("idToken")
        if not self._id_token:
            raise JQuantsError("idToken がレスポンスに含まれていません")
        self._id_token_expiry = datetime.now() + timedelta(hours=ID_TOKEN_TTL_HOURS)
        self._save_cached_token()
        print(
            f"[jquants] ID token 取得 (有効期限: "
            f"{self._id_token_expiry.strftime('%Y-%m-%d %H:%M')})",
            file=sys.stderr,
        )

    def _ensure_token(self) -> None:
        # 1. API Key 方式: 交換不要、直接 Bearer として使用
        if self.api_key:
            self._id_token = self.api_key
            # 有効期限は長期 (実質無期限扱い)
            self._id_token_expiry = datetime.now() + timedelta(days=365)
            return

        # 2. ID Token 直接指定方式: そのまま使用
        if self.direct_id_token:
            self._id_token = self.direct_id_token
            self._id_token_expiry = datetime.now() + timedelta(hours=ID_TOKEN_TTL_HOURS)
            return

        # 3. キャッシュ済みの ID Token が有効なら使用
        if (
            self._id_token
            and self._id_token_expiry
            and self._id_token_expiry > datetime.now()
        ):
            return

        # 4. refresh token / mail+password フロー
        self._refresh_id_token()

    def _headers(self) -> dict:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._id_token}"}

    def auth_method(self) -> str:
        """現在使用している認証方式名を返す (デバッグ用)。"""
        if self.api_key:
            return "API Key (新方式)"
        if self.direct_id_token:
            return "ID Token (直接指定)"
        if self.refresh_token:
            return "Refresh Token (従来方式)"
        if self.mail and self.password:
            return "メール+パスワード"
        return "なし"

    # ─────────────────────────────────────────────────────────
    # 汎用 GET (リトライ・ページネーション対応)
    # ─────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{JQUANTS_BASE_URL}{path}"
        for attempt in range(4):
            r = requests.get(
                url, headers=self._headers(), params=params, timeout=REQUEST_TIMEOUT
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                # トークン期限切れ → 再取得
                print("[jquants] 401, refreshing token", file=sys.stderr)
                self._id_token = None
                self._id_token_expiry = None
                continue
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
        上場銘柄一覧を取得。

        code: 5桁銘柄コード (例: "72030")
        date: 基準日 YYYY-MM-DD (省略時は最新)
        """
        params: dict = {}
        if code:
            params["code"] = code
        if date:
            params["date"] = date
        rows = self._get_paginated("/listed/info", params, "info")
        return pd.DataFrame(rows)

    # ─────────────────────────────────────────────────────────
    # エンドポイント: 日足四本値
    # ─────────────────────────────────────────────────────────

    def get_daily_quotes(
        self,
        code: str | None = None,
        date: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> pd.DataFrame:
        """
        日足 OHLC を取得。

        引数:
          code      : 5桁銘柄コード (例: "72030" for トヨタ)
          date      : 特定日のみ取得 (YYYY-MM-DD)
          from_date : 取得開始日 (YYYY-MM-DD)
          to_date   : 取得終了日 (YYYY-MM-DD)

        戻り値:
          DataFrame 列 (J-Quants 生フォーマット):
          [Date, Code, Open, High, Low, Close, Volume, TurnoverValue,
           AdjustmentFactor, AdjustmentOpen, AdjustmentHigh,
           AdjustmentLow, AdjustmentClose, AdjustmentVolume]

        注意: 分割調整済み (Adjustment*) 列があるので、
              バックテストでは基本 Adjustment* を使うべき。
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

        rows = self._get_paginated("/prices/daily_quotes", params, "daily_quotes")
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"])
            df = df.sort_values(["Date", "Code"]).reset_index(drop=True)

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
    # エンドポイント: 財務情報
    # ─────────────────────────────────────────────────────────

    def get_financial_statements(self, code: str | None = None,
                                 date: str | None = None) -> pd.DataFrame:
        params: dict = {}
        if code:
            params["code"] = code
        if date:
            params["date"] = date
        rows = self._get_paginated("/fins/statements", params, "statements")
        return pd.DataFrame(rows)

    def get_announcement(self) -> pd.DataFrame:
        data = self._get("/fins/announcement")
        return pd.DataFrame(data.get("announcement", []))


# ─────────────────────────────────────────────────────────────
# CLI テスト
# ─────────────────────────────────────────────────────────────

def _cmd_auth(args: argparse.Namespace) -> None:
    cli = JQuantsClient()
    print(f"認証方式: {cli.auth_method()}")
    cli._ensure_token()
    print("✓ 認証準備完了")
    if cli._id_token:
        preview = f"{cli._id_token[:20]}...{cli._id_token[-10:]}" if len(cli._id_token) > 40 else cli._id_token
        print(f"  token preview: {preview}")
        print(f"  token length : {len(cli._id_token)}")
    print(f"  有効期限     : {cli._id_token_expiry}")
    print(f"  キャッシュ   : {cli._token_cache_path}")

    # 実際に API を叩いて動作確認
    print()
    print("動作確認: /listed/info で1銘柄取得中...")
    try:
        df = cli.get_listed_info(code="72030")
        if df.empty:
            print("  [warn] レスポンスは空でした")
        else:
            print("  ✓ API 呼び出し成功")
            cols = [c for c in ["Code", "CompanyName", "MarketCodeName"]
                    if c in df.columns]
            if cols:
                print("  " + df[cols].head(1).to_string(index=False).replace("\n", "\n  "))
    except JQuantsError as e:
        print(f"  [ERROR] API 呼び出し失敗: {e}")
        raise


def _cmd_listed(args: argparse.Namespace) -> None:
    cli = JQuantsClient()
    df = cli.get_listed_info(code=args.code)
    print(f"上場銘柄: {len(df)}件")
    if not df.empty:
        cols = [c for c in ["Date", "Code", "CompanyName", "CompanyNameEnglish",
                            "Sector17CodeName", "Sector33CodeName",
                            "MarketCodeName"] if c in df.columns]
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
        print("...")
        print(df[cols].tail(5).to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="J-Quants API クライアント CLI")
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
