"""
kabuステーション REST API トークン取得サンプル
https://kabucom.github.io/kabusapi/ptal/index.html
"""

import requests
import json


# ---- 設定 ----
API_PASSWORD = "your_password_here"   # kabuステーションのAPIパスワード
BASE_URL = "http://localhost:18080"    # 本番: 18080 / 本番(SSL): 18081
                                       # デモ:  18081 は使わず 18080 を使う
# ---- トークン取得 ----

def get_token(password: str, base_url: str = BASE_URL) -> str:
    """
    kabuステーション REST API からトークンを取得する。

    Parameters
    ----------
    password : str
        kabuステーション API のパスワード
    base_url : str
        APIのベースURL（デフォルト: http://localhost:18080）

    Returns
    -------
    str
        取得したアクセストークン
    """
    url = f"{base_url}/kabusapi/token"
    headers = {"Content-Type": "application/json"}
    body = {"APIPassword": password}

    response = requests.post(url, headers=headers, data=json.dumps(body))
    response.raise_for_status()

    token = response.json().get("Token")
    if not token:
        raise ValueError(f"トークンが取得できませんでした: {response.json()}")

    return token


if __name__ == "__main__":
    try:
        token = get_token(API_PASSWORD)
        print(f"取得成功！\nToken: {token}")
    except requests.exceptions.ConnectionError:
        print("接続エラー: kabuステーションが起動しているか確認してください。")
    except requests.exceptions.HTTPError as e:
        print(f"HTTPエラー: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        print(f"エラー: {e}")
