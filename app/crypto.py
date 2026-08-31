"""Me Talk 保存時暗号化。

メッセージ本文をデータベースに書き込む直前に暗号化し、読み出す直後に復号する。
データベースのファイルが流出しても、鍵が無ければ中身は読めない。

鍵はサーバーが持つため、サーバー全体を掌握された場合は保護できない。
送信者と受信者しか読めない「エンドツーエンド暗号化」とは別物である点に注意する。

暗号方式は AES-256-GCM。保存形式は enc1: に続けて
「12バイトのノンス + 暗号文 + 認証タグ」を base64url にしたもの。
接頭辞が無い値は平文とみなしてそのまま返すため、
暗号化の導入前後や移行の途中でもデータを読み出せる。

Dev yuzuki_akrdev.ofc
"""

import base64
import os

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app import config

PREFIX = "enc1:"
NONCE_BYTES = 12
KEY_BYTES = 32

_cipher = None
_error = ""


def _load() -> None:
    global _cipher, _error
    if _cipher is not None or _error:
        return
    raw = (config.ENCRYPTION_KEY or "").strip()
    if not raw:
        _error = "no-key"
        return
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:
        _error = f"cryptography を読み込めません: {exc}"
        return
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        _error = f"ENCRYPTION_KEY を復号できません: {exc}"
        return
    if len(key) != KEY_BYTES:
        _error = f"ENCRYPTION_KEY は32バイトである必要があります (現在 {len(key)} バイト)"
        return
    _cipher = AESGCM(key)


def enabled() -> bool:
    _load()
    return _cipher is not None


def problem() -> str:
    """鍵が設定されているのに使えない場合、その理由を返す。"""
    _load()
    if _cipher is not None:
        return ""
    if _error == "no-key":
        return ""
    return _error


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode("ascii").rstrip("=")


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    if value is None or value == "":
        return value
    if is_encrypted(value):
        return value
    if not enabled():
        return value
    nonce = os.urandom(NONCE_BYTES)
    blob = nonce + _cipher.encrypt(nonce, value.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def decrypt(value):
    if not is_encrypted(value):
        return value
    if not enabled():
        raise RuntimeError(
            "暗号化されたデータがありますが復号できません。ENCRYPTION_KEY の設定を確認してください。"
        )
    body = value[len(PREFIX):]
    blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return _cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None).decode("utf-8")


class EncryptedText(TypeDecorator):
    """書き込み時に暗号化し、読み出し時に復号する列の型。

    列の型自体は TEXT のままなので、既存のテーブルを作り替える必要はない。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
