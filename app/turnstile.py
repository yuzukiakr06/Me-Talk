"""Me Talk ロボット確認(Cloudflare Turnstile)。

フロントから送られたトークンをCloudflareに問い合わせて検証する。
シークレットが未設定のときは検証をスキップする(開発時の利便性)。

Dev yuzuki_akrdev.ofc
"""

import requests

from app import config

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def enabled() -> bool:
    """ロボット確認が有効かどうか。"""
    return bool(config.TURNSTILE_SECRET_KEY and config.TURNSTILE_SITE_KEY)


def verify(token: str, remote_ip: str = "") -> bool:
    """トークンを検証する。未設定なら常に True。"""
    if not enabled():
        return True
    if not token:
        return False
    payload = {"secret": config.TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        res = requests.post(VERIFY_URL, data=payload, timeout=10)
    except Exception as exc:
        print(f"[turnstile] 検証リクエストに失敗しました: {exc}")
        return False
    if res.status_code != 200:
        print(f"[turnstile] 検証に失敗しました: status={res.status_code} body={res.text[:200]}")
        return False
    data = res.json()
    if not data.get("success"):
        codes = data.get("error-codes") or []
        print(f"[turnstile] 検証NG: {codes}")
        if "invalid-input-secret" in codes:
            print("[turnstile] シークレットキーが正しくありません。config.jsonのTURNSTILE_SECRET_KEYを確認してください。")
        return False
    return True
