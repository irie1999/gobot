"""
kabu_diag.py — kabuステーション API 接続の診断ツール
=====================================================

401 などで接続できないときに、原因を切り分けるための診断専用スクリプト。
パスワードそのものは表示せず、長さ・前後の空白・文字種だけを表示する。

使い方:
  python kabu_diag.py            # デモ(18081) を診断
  python kabu_diag.py --prod     # 本番(18080) を診断
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

DEMO_URL = "http://localhost:18081"
PROD_URL = "http://localhost:18080"


def _inspect_password(pw: str | None, env_key: str) -> str | None:
    """パスワードの中身を安全に診断 (値そのものは出さない)。"""
    if not pw:
        print(f"  ✗ 環境変数 {env_key} が未設定 (または空)")
        return None
    n = len(pw)
    stripped = pw.strip()
    has_space = pw != stripped
    has_wide = any(ord(c) > 0x7F for c in pw)  # 全角・不可視文字の混入チェック
    print(f"  ✓ {env_key} 取得: 長さ={n}文字  "
          f"先頭='{pw[0]}'  末尾='{pw[-1]}'")
    if has_space:
        print(f"  ⚠ 前後に空白あり! (strip後の長さ={len(stripped)}) "
              "→ コピペ時の空白混入の可能性")
    if has_wide:
        print("  ⚠ 全角または非ASCII文字が混入しています! "
              "→ 半角英数で入力し直してください")
    return pw


def main() -> int:
    ap = argparse.ArgumentParser(description="kabuステーション API 接続診断")
    ap.add_argument("--prod", action="store_true", help="本番口座(18080)を診断")
    args = ap.parse_args()

    base = PROD_URL if args.prod else DEMO_URL
    env_key = "KABU_API_PASSWORD_PROD" if args.prod else "KABU_API_PASSWORD_DEMO"
    label = "本番(18080)" if args.prod else "デモ(18081)"

    print("=" * 60)
    print(f"kabuステーション API 診断  接続先: {label}")
    print("=" * 60)

    # 1) ポート疎通
    print("\n[1] ポート疎通チェック...")
    try:
        requests.get(f"{base}/kabusapi/", timeout=3)
        print(f"  ✓ {base} に到達 (kabuステーション起動中)")
    except requests.exceptions.ConnectionError:
        print(f"  ✗ {base} に接続できません")
        print("    → kabuステーションが起動していない、または")
        print(f"    → {'本番' if args.prod else 'デモ'}モードで起動していない可能性")
        print(f"    → 反対側も試す: python kabu_diag.py {'' if args.prod else '--prod'}")
        return 1
    except Exception:
        # 404 等でも到達はしている
        print(f"  ✓ {base} に到達 (kabuステーション起動中)")

    # 2) パスワード診断
    print("\n[2] APIパスワードの確認...")
    pw = os.environ.get(env_key) or os.environ.get("KABU_API_PASSWORD")
    if not os.environ.get(env_key) and os.environ.get("KABU_API_PASSWORD"):
        print(f"  ⚠ {env_key} は未設定。KABU_API_PASSWORD で代用します")
    pw = _inspect_password(pw, env_key)
    if not pw:
        return 1

    # 3) トークン取得
    print("\n[3] トークン取得 (POST /token)...")
    url = f"{base}/kabusapi/token"
    try:
        r = requests.post(url, json={"APIPassword": pw},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
    except Exception as e:
        print(f"  ✗ リクエスト失敗: {e}")
        return 1

    print(f"  HTTPステータス: {r.status_code}")
    try:
        body = r.json()
    except Exception:
        body = r.text
    print(f"  レスポンス: {body}")

    if r.status_code == 200 and isinstance(body, dict) and body.get("Token"):
        print("\n✓✓✓ 接続成功! トークンを取得できました。")
        print("    kabu_api.py / kabu_send_signals.py が使えます。")
        return 0

    # 401 等の場合の案内
    print("\n✗ トークン取得に失敗しました。")
    if r.status_code == 401:
        print("  401 = 認証失敗。以下を順に確認してください:")
        print("   1. kabuステーションに今日ログインし直したか (毎日リセットされます)")
        print("   2. 設定 > API で『kabuステーションAPIを使用する』にチェックがあるか")
        print("   3. そのAPIパスワードと環境変数の値が一致しているか")
        print("   4. 設定変更後 kabuステーションを再起動したか")
        print(f"   5. 接続先モードは合っているか (今: {label})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
