"""MIRAI Chat セキュリティ関連ユーティリティ。

パスワードハッシュ・端末フィンガープリント・メール確認コードを扱う。
MIRAI ID と同じ方式(PBKDF2-HMAC-SHA256、IP+UAのハッシュによる既知端末判定)に揃えている。

Dev yuzuki_akrdev.ofc
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Request


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120000).hex()
    return salt, digest


def verify_password(password: str, salt: str | None, expected_hash: str | None) -> bool:
    if not salt or not expected_hash:
        return False
    _, actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected_hash)


def request_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return ""


def request_fingerprint(request: Request) -> tuple[str, str]:
    ip = request_ip(request)
    ua = request.headers.get("user-agent", "")
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest() if ip else ""
    ua_hash = hashlib.sha256(ua.encode("utf-8")).hexdigest() if ua else ""
    return ip_hash, ua_hash


def make_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def code_expires_at(minutes: int = 15) -> datetime:
    return datetime.utcnow() + timedelta(minutes=minutes)


def not_expired(expires_at: datetime) -> bool:
    return datetime.utcnow() < expires_at


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
